"""Whisper transcription with two backends:

  - `faster-whisper` (default) — pip-installed, runs on CPU with int8.
  - `whisper-cli` (whisper.cpp from `ffmpeg-full`) — Metal-accelerated on
    Apple Silicon, ~3-5× faster than faster-whisper on CPU. Opt-in by setting
    WHISPER_BACKEND=whisper_cpp env var, or by passing backend='whisper_cpp'
    to transcribe().
"""
from __future__ import annotations
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from pydantic import BaseModel

from ..config import WHISPER_MODEL, WHISPER_DEVICE
from .. import platformutil as _pu

_log = logging.getLogger("video_ai_editor")


class Word(BaseModel):
    start: float
    end: float
    word: str
    prob: float = 1.0


class Segment(BaseModel):
    id: int
    start: float
    end: float
    text: str
    words: list[Word] = []


class Transcript(BaseModel):
    language: str
    duration: float
    segments: list[Segment] = []

    @property
    def words(self) -> list[Word]:
        return [w for s in self.segments for w in s.words]

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments).strip()


# Per-model cache so picking `tiny.en` once doesn't have to re-load `small`
# the next time it's requested.
_models: dict[str, Any] = {}


def _resolve_device() -> str:
    """Sanitize WHISPER_DEVICE into something ctranslate2 accepts.

    Users' .env files may carry a polluted value (an inline comment that a
    pre-fix parser kept as part of the value), and 'mps'/'auto' are not
    ctranslate2 devices — everything unknown degrades to cpu instead of
    crashing the Captions button with "unsupported device".
    """
    device = str(WHISPER_DEVICE).split("#", 1)[0].strip().lower()
    return device if device in ("cpu", "cuda") else "cpu"


def _get_model(model_size: str | None = None):
    name = model_size or WHISPER_MODEL
    cached = _models.get(name)
    if cached is not None:
        return cached
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        # A bare ImportError here is neither ValueError nor RuntimeError, so
        # main.py's dispatch mapping let it through as an HTTP 500 "internal
        # server error" — the Captions button just failed with no explanation.
        # RuntimeError maps to a 422 carrying this text straight to the toast.
        # (The macOS .app deliberately excludes faster-whisper to stay ~150MB;
        # that build reaches exactly this line.)
        raise RuntimeError(
            "Speech-to-text is unavailable in this build — the 'faster-whisper' "
            "package is not installed. Run the app from source (`uv sync "
            "--all-extras`) to enable captions and transcription."
        ) from e
    compute_type = "int8"
    cached = WhisperModel(name, device=_resolve_device(), compute_type=compute_type)
    _models[name] = cached
    return cached


# Hunt for ggml-* models in the per-OS data dir, legacy XDG/brew locations, and
# ~/.cache. The per-OS dir is checked first; legacy paths are kept so an
# existing macOS install keeps finding its models.
_WHISPER_CPP_MODEL_DIRS = [
    Path(os.environ.get("WHISPER_CPP_MODELS", ""))
        if os.environ.get("WHISPER_CPP_MODELS") else None,
    _pu.user_data_dir("Video AI Editor") / "whisper-cpp",   # new, per-OS
    Path.home() / ".local" / "share" / "video-ai-editor" / "whisper-cpp",  # legacy mac/linux
    Path("/opt/homebrew/share/whisper-cpp/ggml-models"),    # legacy brew (harmless on win)
    Path("/opt/homebrew/share/whisper-cpp"),
    Path.home() / ".cache" / "whisper-cpp",
]
_WHISPER_CPP_MODEL_DIRS = [p for p in _WHISPER_CPP_MODEL_DIRS if p is not None]

_WHISPER_CPP_BIN = _pu.find_binary("whisper-cli", _WHISPER_CPP_MODEL_DIRS) or _pu.exe_name("whisper-cli")


def _whisper_cpp_available() -> bool:
    return Path(_WHISPER_CPP_BIN).exists()


def _whisper_cpp_model_path(name: str) -> Path:
    """Map faster-whisper model names → whisper.cpp ggml model file path.
    Walks the candidate dirs and returns the first hit, else the canonical
    path under the user cache (so error messages are stable)."""
    aliases = {
        "tiny.en": "ggml-tiny.en.bin",
        "tiny":    "ggml-tiny.bin",
        "base.en": "ggml-base.en.bin",
        "base":    "ggml-base.bin",
        "small.en":"ggml-small.en.bin",
        "small":   "ggml-small.bin",
        "medium":  "ggml-medium.bin",
        "large":   "ggml-large-v3.bin",
        "large-v3": "ggml-large-v3.bin",
        "large-v3-turbo": "ggml-large-v3-turbo.bin",
        "turbo":   "ggml-large-v3-turbo.bin",
    }
    fname = aliases.get(name, f"ggml-{name}.bin")
    for d in _WHISPER_CPP_MODEL_DIRS:
        cand = d / fname
        if cand.exists():
            return cand
    # Default for error message
    return _WHISPER_CPP_MODEL_DIRS[0] / fname


def _transcribe_via_whisper_cpp(audio_path: Path, language: str | None,
                                model_size: str | None) -> Transcript:
    """Run whisper-cli (Metal-accelerated on Apple Silicon) and parse its JSON output."""
    name = model_size or WHISPER_MODEL
    model_path = _whisper_cpp_model_path(name)
    if not model_path.exists():
        hint = ("Download it with whisper.cpp's download-ggml-model script "
                "(models/download-ggml-model.cmd on Windows, .sh on macOS), or "
                "fetch https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
                f"ggml-{name}.bin into {_WHISPER_CPP_MODEL_DIRS[0]}")
        raise RuntimeError(f"whisper-cpp model not found at {model_path}. {hint}")
    # whisper-cli wants 16k mono wav input
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "in.wav"
        subprocess.run(
            [_pu.FFMPEG, "-y", "-i", str(audio_path),
             "-vn", "-ac", "1", "-ar", "16000", str(wav)],
            capture_output=True, check=True,
            **_pu.SUBPROCESS_FLAGS,
        )
        out_prefix = Path(td) / "out"
        # `-l auto` is REQUIRED when no language is given: whisper-cli's
        # default is `-l en` (not auto-detect), which force-decodes Hindi /
        # any non-English audio as English garbage. faster-whisper
        # auto-detects when language=None; this keeps the backends consistent.
        #
        # Anti-hallucination flags (the difference between clean captions and
        # the "लिए भी लिए लिए" repetition-loop garbage weak models emit on
        # music/ambient):
        #   -et 2.8  entropy threshold → fall back to a higher temperature when
        #            the decode looks degenerate, breaking repetition loops.
        #   -mc 0    max-context 0 → don't condition on previous text, so a
        #            hallucinated phrase can't snowball across segments.
        #
        # We do NOT use `-ml 1` (one token per segment): on Devanagari it splits
        # multibyte characters at token boundaries, writing invalid UTF-8 into
        # the JSON ('कौन' → 'क' + two broken bytes + 'न'). Segment mode keeps
        # each segment's `text` field whole and valid; we synthesize word-level
        # timing below by spreading the segment duration across its words.
        cmd = [_WHISPER_CPP_BIN, "-m", str(model_path), "-f", str(wav),
               "-of", str(out_prefix), "-oj",
               "-et", "2.8", "-mc", "0",
               "-l", language if language else "auto"]
        # errors="replace" on the captured pipes (progress meter can split a
        # multibyte char across buffers).
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", **_pu.SUBPROCESS_FLAGS)
        if proc.returncode != 0:
            raise RuntimeError(f"whisper-cli failed (rc={proc.returncode}):\n{proc.stderr[-1500:]}")
        json_path = Path(f"{out_prefix}.json")
        if not json_path.exists():
            raise RuntimeError(f"whisper-cli produced no JSON output:\n{proc.stdout[-500:]}")
        # Read bytes + decode with errors="replace": the per-token array in the
        # JSON can carry split-multibyte garbage, but each segment's `text`
        # field is valid UTF-8 and survives intact (only the already-broken
        # token bytes become U+FFFD, which we never read).
        data = json.loads(json_path.read_bytes().decode("utf-8", "replace"))

    # Segment mode: each `transcription` entry is a whole sentence/segment with
    # millisecond offsets and a clean `text`. Build Segment objects directly and
    # synthesize even word timing across each segment so word_emphasis captions
    # and word-level tools still work.
    segments: list[Segment] = []
    for seg in data.get("transcription", []) or []:
        offsets = seg.get("offsets") or {}
        start = float(offsets.get("from", 0)) / 1000.0
        end = float(offsets.get("to", 0)) / 1000.0
        text = (seg.get("text") or "")
        # Drop U+FFFD replacement chars left by any rare segment-text byte split,
        # then collapse the whitespace they leave behind.
        text = text.replace("�", "").strip().lstrip("-").strip()
        text = " ".join(text.split())
        # whisper emits "[Music]" / "[_TT_*]" style non-speech markers; drop them.
        if not text or (text.startswith("[") and text.endswith("]")):
            continue
        # Drop degenerate zero/near-zero-duration fragments (a sub-character
        # split occasionally produces a 14.2→14.2 stub).
        if end - start < 0.06:
            continue
        toks = [t for t in text.split(" ") if t]
        words: list[Word] = []
        if toks and end > start:
            step = (end - start) / len(toks)
            for j, tok in enumerate(toks):
                words.append(Word(start=start + j * step,
                                  end=start + (j + 1) * step, word=tok))
        segments.append(Segment(id=len(segments), start=start, end=end,
                                text=text, words=words))

    duration = segments[-1].end if segments else 0.0
    detected_lang = data.get("result", {}).get("language") if isinstance(data.get("result"), dict) else None
    return Transcript(language=str(detected_lang or language or "en"),
                      duration=duration, segments=segments)


def transcribe(audio_path: Path, language: str | None = None,
               model_size: str | None = None,
               backend: str | None = None) -> Transcript:
    """Run whisper. `language=None` triggers auto-detect.

    `backend`:
      - "auto" (default)  — whisper-cli (Metal-accelerated) when the binary
        AND the ggml model for the requested size are present; otherwise
        faster-whisper. On Apple Silicon this is ~4-5x faster (measured:
        12s vs 54s for 40s of Hindi audio) — the difference between
        captions feeling instant and feeling stuck.
      - "faster_whisper"  — force CPU int8 via faster-whisper
      - "whisper_cpp"     — force whisper-cli (falls back if unavailable)
    """
    backend = backend or os.environ.get("WHISPER_BACKEND") or "auto"
    if backend == "auto":
        name = model_size or WHISPER_MODEL
        if _whisper_cpp_available() and _whisper_cpp_model_path(name).exists():
            backend = "whisper_cpp"
        else:
            backend = "faster_whisper"
    if backend == "whisper_cpp" and _whisper_cpp_available():
        return _transcribe_via_whisper_cpp(audio_path, language, model_size)
    model = _get_model(model_size)
    try:
        segments_iter, info = model.transcribe(
            str(audio_path),
            language=language,
            word_timestamps=True,
            vad_filter=True,
        )
    except Exception as e:
        # `vad_filter=True` loads Silero VAD, which faster-whisper ships as a
        # DATA FILE inside its own package (`faster_whisper/assets/
        # silero_vad_v6.onnx`) — not as Python code. PyInstaller collects the
        # module but NOT a package's data files unless the spec asks, so the
        # packaged Windows app raised onnxruntime's `NoSuchFile` right here.
        # That type is neither ValueError nor RuntimeError, so it escaped
        # main.py's dispatch mapping and reached the user as a bare HTTP 500
        # "internal server error" with no captions and no clue — while the
        # identical click worked in dev, where site-packages has the asset.
        #
        # The spec now collects those data files (see "Video AI Editor.spec"),
        # but degrade here too: VAD only trims silence before decoding, so
        # transcription is fully functional without it. A packaging regression
        # should cost caption *quality*, never the whole feature. If the retry
        # fails as well, that exception propagates — this is not a blanket
        # swallow.
        _log.warning("whisper VAD unavailable (%s) — transcribing without vad_filter", e)
        segments_iter, info = model.transcribe(
            str(audio_path),
            language=language,
            word_timestamps=True,
            vad_filter=False,
        )
    segments: list[Segment] = []
    for s in segments_iter:
        segments.append(Segment(
            id=s.id,
            start=s.start,
            end=s.end,
            text=s.text,
            words=[
                Word(start=w.start, end=w.end, word=w.word, prob=getattr(w, "probability", 1.0))
                for w in (s.words or [])
            ],
        ))
    return Transcript(
        language=info.language,
        duration=info.duration,
        segments=segments,
    )
