"""Whisper transcription with two backends:

  - `faster-whisper` (default) — pip-installed, runs on CPU with int8.
  - `whisper-cli` (whisper.cpp from `ffmpeg-full`) — Metal-accelerated on
    Apple Silicon, ~3-5× faster than faster-whisper on CPU. Opt-in by setting
    WHISPER_BACKEND=whisper_cpp env var, or by passing backend='whisper_cpp'
    to transcribe().
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from pydantic import BaseModel

from ..config import WHISPER_MODEL, WHISPER_DEVICE


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


def _get_model(model_size: str | None = None):
    name = model_size or WHISPER_MODEL
    cached = _models.get(name)
    if cached is not None:
        return cached
    from faster_whisper import WhisperModel
    device = WHISPER_DEVICE
    compute_type = "int8"
    if device == "auto":
        device = "cpu"  # CoreML path is opt-in; default cpu+int8 is fine
    cached = WhisperModel(name, device=device, compute_type=compute_type)
    _models[name] = cached
    return cached


_WHISPER_CPP_BIN = shutil.which("whisper-cli") or "/opt/homebrew/bin/whisper-cli"

# Hunt for ggml-* models in user cache, brew share, and ~/.cache.
_WHISPER_CPP_MODEL_DIRS = [
    Path(os.environ.get("WHISPER_CPP_MODELS", ""))
        if os.environ.get("WHISPER_CPP_MODELS") else None,
    Path.home() / ".local" / "share" / "video-ai-editor" / "whisper-cpp",
    Path("/opt/homebrew/share/whisper-cpp/ggml-models"),
    Path("/opt/homebrew/share/whisper-cpp"),
    Path.home() / ".cache" / "whisper-cpp",
]
_WHISPER_CPP_MODEL_DIRS = [p for p in _WHISPER_CPP_MODEL_DIRS if p is not None]


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
        raise RuntimeError(
            f"whisper-cpp model not found at {model_path}. "
            f"Try `brew reinstall whisper-cpp` or download with "
            f"`/opt/homebrew/share/whisper-cpp/download-ggml-model.sh {name}`."
        )
    # whisper-cli wants 16k mono wav input
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "in.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(audio_path),
             "-vn", "-ac", "1", "-ar", "16000", str(wav)],
            capture_output=True, check=True,
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
                              encoding="utf-8", errors="replace")
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
    segments_iter, info = model.transcribe(
        str(audio_path),
        language=language,
        word_timestamps=True,
        vad_filter=True,
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
