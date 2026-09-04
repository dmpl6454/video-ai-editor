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


def _require_espeak_data() -> None:
    """Refuse to synthesize when piper's espeak-ng dictionaries are absent.

    Not defensive padding: espeak-ng does not raise when it cannot find
    `phontab`, it calls exit(1), which takes the ENTIRE app process down —
    the user clicks "AI voiceover" and the editor vanishes. A RuntimeError
    here is mapped to HTTP 422 by main.py, so the user gets a message and
    keeps their session. build_app.sh has a fatal gate so a bundle missing
    this cannot ship; this is the second line of defence at the crash site,
    and it also covers a source checkout whose piper install is incomplete.
    """
    import importlib.util
    spec = importlib.util.find_spec("piper")   # find_spec does NOT execute piper
    root = Path(spec.origin).parent if (spec and spec.origin) else None
    if root is None or not (root / "espeak-ng-data" / "phontab").is_file():
        raise RuntimeError(
            "Text-to-speech is unavailable: piper's espeak-ng data is missing "
            "from this install. Running the app from source restores it."
        )


def synthesize(text: str, dst: Path, *, voice: str = "en_US-amy-medium") -> Path:
    """Render `text` → `dst` (.wav). Cached by content+voice hash."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_size > 1024:
        return dst
    onnx = ensure_voice(voice)
    _require_espeak_data()
    from piper import PiperVoice
    pv = PiperVoice.load(str(onnx))
    with wave.open(str(dst), "wb") as wf:
        pv.synthesize_wav(text, wf)
    return dst


def cached_path(text: str, voice: str, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(f"{voice}|{text}".encode()).hexdigest()[:16]
    return cache_dir / f"tts_{key}.wav"
