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
        cmd = [_WHISPER_CPP_BIN, "-m", str(model_path), "-f", str(wav),
               "-of", str(out_prefix), "-oj", "-ml", "1"]
        if language:
            cmd += ["-l", language]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"whisper-cli failed (rc={proc.returncode}):\n{proc.stderr[-1500:]}")
        json_path = Path(f"{out_prefix}.json")
        if not json_path.exists():
            raise RuntimeError(f"whisper-cli produced no JSON output:\n{proc.stdout[-500:]}")
        data = json.loads(json_path.read_text())

    # whisper.cpp's transcription JSON has top-level `transcription` (segments)
    # with offsets in milliseconds. Convert to our Segment/Word model.
    segments: list[Segment] = []
    for i, seg in enumerate(data.get("transcription", []) or []):
        offsets = seg.get("offsets") or {}
        start = float(offsets.get("from", 0)) / 1000.0
        end = float(offsets.get("to", 0)) / 1000.0
        text = (seg.get("text") or "").strip()
        # whisper-cli with -ml 1 emits one segment per token, which is great for
        # word-level timing. Treat each segment as a single-word unit.
        words = [Word(start=start, end=end, word=text)] if text else []
        segments.append(Segment(id=i, start=start, end=end, text=text, words=words))
    duration = segments[-1].end if segments else 0.0
    detected_lang = data.get("result", {}).get("language") if isinstance(data.get("result"), dict) else None
    return Transcript(language=str(detected_lang or language or "en"),
                      duration=duration, segments=segments)


def transcribe(audio_path: Path, language: str | None = None,
               model_size: str | None = None,
               backend: str | None = None) -> Transcript:
    """Run whisper. `language=None` triggers auto-detect.

    `backend`:
      - "faster_whisper" (default) — CPU int8 via faster-whisper
      - "whisper_cpp"             — whisper-cli with Metal on Apple Silicon

    Falls back to faster_whisper if whisper_cpp is requested but unavailable.
    """
    backend = backend or os.environ.get("WHISPER_BACKEND") or "faster_whisper"
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
