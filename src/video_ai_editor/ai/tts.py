"""Piper text-to-speech wrapper.

Synthesizes a WAV per input text + voice. Voices live in
~/.cache/video-ai-editor/voices and are downloaded on first use.
"""
from __future__ import annotations
import hashlib
import wave
from pathlib import Path

from .. import platformutil as _pu

_LEGACY_VOICES_DIR = Path.home() / ".cache" / "video-ai-editor" / "voices"
VOICES_DIR = _LEGACY_VOICES_DIR if _LEGACY_VOICES_DIR.exists() else \
    _pu.user_cache_dir("Video AI Editor") / "voices"


def voice_paths(name: str) -> tuple[Path, Path]:
    return (VOICES_DIR / f"{name}.onnx", VOICES_DIR / f"{name}.onnx.json")


def ensure_voice(name: str = "en_US-amy-medium") -> Path:
    """Download the voice model if missing. Returns the .onnx path."""
    onnx, _ = voice_paths(name)
    if onnx.exists():
        return onnx
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    from piper.download_voices import download_voice
    download_voice(name, VOICES_DIR)
    return onnx


def synthesize(text: str, dst: Path, *, voice: str = "en_US-amy-medium") -> Path:
    """Render `text` → `dst` (.wav). Cached by content+voice hash."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_size > 1024:
        return dst
    onnx = ensure_voice(voice)
    from piper import PiperVoice
    pv = PiperVoice.load(str(onnx))
    with wave.open(str(dst), "wb") as wf:
        pv.synthesize_wav(text, wf)
    return dst


def cached_path(text: str, voice: str, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(f"{voice}|{text}".encode()).hexdigest()[:16]
    return cache_dir / f"tts_{key}.wav"
