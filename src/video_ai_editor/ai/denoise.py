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


def _has_video_stream(src: Path) -> bool:
    """True when `src` carries a real video stream (not just cover art).

    `attached_pic` is excluded on purpose: an MP3 with embedded album art DOES
    report a video stream, and treating it as a video would put us straight back
    into the failing `-map 0:v -c:v copy` path this guard exists to avoid.
    """
    try:
        proc = subprocess.run(
            [_pu.FFPROBE, "-v", "error", "-select_streams", "v",
             "-show_entries", "stream=codec_type:stream_disposition=attached_pic",
             "-of", "csv=p=0", str(src)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, **_pu.SUBPROCESS_FLAGS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    for line in (proc.stdout or "").splitlines():
        parts = [p for p in line.strip().split(",") if p != ""]
        if not parts or parts[0] != "video":
            continue
        if len(parts) > 1 and parts[1] == "1":
            continue  # attached_pic → cover art, not video
        return True
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
    # An audio-only source (an MP3 on the music lane) has no video stream to copy,
    # so it gets an .m4a container and a mux that never mentions `0:v`. Without
    # this branch the mux below asked for `-map 0:v`, ffmpeg answered
    # "Stream map '' matches no streams", and the user saw "ffmpeg mux failed" —
    # noise_reduce worked on a video clip's audio but always failed on music.
    has_video = _has_video_stream(src)
    ext = "mp4" if has_video else "m4a"
    # `has_video`/ext are part of the cache key so a path cached under the old
    # shape can never be served for the new one.
    h = hashlib.sha256(
        f"{src}|{strength}|{sample_rate}|{src.stat().st_mtime}|{ext}".encode()
    ).hexdigest()[:14]
    dst = cache_dir / f"denoise_{h}.{ext}"
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
            # text= is required for the error string below to be readable — without
            # it stderr is bytes and the message interpolated a b"..." repr. The
            # encoding/errors kwargs are mandatory alongside text on Windows
            # (cp1252-strict would raise on a Devanagari path). See CLAUDE.md.
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            **_pu.SUBPROCESS_FLAGS,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg audio extract failed: {(proc.stderr or '')[-500:]}")
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

        # Mux back. With video: keep the original video, swap in cleaned audio.
        # Without video: just encode the cleaned wav — the source isn't an input
        # at all, so there is no `0:v` to fail to match.
        if has_video:
            mux = [_pu.FFMPEG, "-y", "-i", str(src), "-i", str(wav_out),
                   "-map", "0:v", "-map", "1:a",
                   "-c:v", "copy", "-c:a", "aac", "-shortest", str(dst)]
        else:
            mux = [_pu.FFMPEG, "-y", "-i", str(wav_out),
                   "-c:a", "aac", "-b:a", "192k", str(dst)]
        proc = subprocess.run(
            mux, capture_output=True, text=True, encoding="utf-8",
            errors="replace", **_pu.SUBPROCESS_FLAGS,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg mux failed: {(proc.stderr or '')[-500:]}")
    return dst
