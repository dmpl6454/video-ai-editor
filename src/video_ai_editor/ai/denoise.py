"""Spectral noise reduction for clip audio.

Uses `noisereduce` (a stationary-noise spectral-gate method that's good for
constant background hiss / fans / room tone). For speech-only clips this is
a sweet spot — fast, no model download, runs on CPU.

Output: new audio-replaced video at `cache/denoise/<hash>.mp4`. Original
video stream is `-c:v copy`'d so this is fast to chain into renders.
"""
from __future__ import annotations
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import platformutil as _pu


def available() -> bool:
    try:
        import importlib
        importlib.import_module("noisereduce")
        importlib.import_module("soundfile")
        return True
    except ImportError:
        return False


def denoise_clip(src: Path, cache_dir: Path, *,
                 strength: float = 0.85, sample_rate: int = 48000) -> Path:
    """Return a new mp4 with the audio track noise-reduced.

    `strength` ∈ [0,1]: higher = more aggressive (with diminishing returns and
    growing artifacts above ~0.9). Default 0.85 is the speech sweet spot.
    """
    if not available():
        raise RuntimeError("noisereduce not installed (uv add noisereduce soundfile)")

    cache_dir.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256(
        f"{src}|{strength}|{sample_rate}|{src.stat().st_mtime}".encode()
    ).hexdigest()[:14]
    dst = cache_dir / f"denoise_{h}.mp4"
    if dst.exists() and dst.stat().st_size > 0:
        return dst

    import noisereduce as nr  # type: ignore
    import soundfile as sf  # type: ignore
    import numpy as np

    with tempfile.TemporaryDirectory() as td:
        wav_in = Path(td) / "in.wav"
        wav_out = Path(td) / "out.wav"
        # Extract audio to mono 48k float wav for noisereduce
        proc = subprocess.run(
            [_pu.FFMPEG, "-y", "-i", str(src),
             "-vn", "-ac", "1", "-ar", str(sample_rate),
             "-c:a", "pcm_s16le", str(wav_in)],
            capture_output=True,
            **_pu.SUBPROCESS_FLAGS,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg audio extract failed: {proc.stderr[-500:]}")
        if not wav_in.exists() or wav_in.stat().st_size < 100:
            raise RuntimeError("source has no usable audio track")

        data, sr = sf.read(str(wav_in))
        if data.ndim > 1:
            data = data.mean(axis=1)
        clean = nr.reduce_noise(
            y=data.astype(np.float32),
            sr=sr,
            stationary=True,
            prop_decrease=max(0.0, min(1.0, float(strength))),
        )
        sf.write(str(wav_out), clean, sr, subtype="PCM_16")

        # Mux back: keep original video, swap in the cleaned audio.
        proc = subprocess.run(
            [_pu.FFMPEG, "-y", "-i", str(src), "-i", str(wav_out),
             "-map", "0:v", "-map", "1:a",
             "-c:v", "copy", "-c:a", "aac", "-shortest", str(dst)],
            capture_output=True,
            **_pu.SUBPROCESS_FLAGS,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg mux failed: {proc.stderr[-500:]}")
    return dst
