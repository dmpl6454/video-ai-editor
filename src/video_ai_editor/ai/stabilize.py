"""Two-pass stabilization via ffmpeg's libvidstab.

Brew's plain `ffmpeg` formula doesn't include libvidstab. The companion
`ffmpeg-full` formula does. We try paths in order: ffmpeg-full → ffmpeg.
If neither has vidstab, raises RuntimeError with a clean install hint.
"""
from __future__ import annotations
import hashlib
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from .. import platformutil as _pu

_FFMPEG_CANDIDATES = [
    _pu.FFMPEG,                                       # PATH (works on Windows/Linux/Mac)
    "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",       # mac brew ffmpeg-full (has vidstab)
    "/usr/local/opt/ffmpeg-full/bin/ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
]


def available() -> bool:
    """True iff a vidstab-enabled ffmpeg is locatable on this machine."""
    return _ffmpeg_with_vidstab() is not None


@lru_cache(maxsize=1)
def _ffmpeg_with_vidstab() -> str | None:
    """Return the ffmpeg path that has vidstab, or None."""
    for cand in _FFMPEG_CANDIDATES:
        if cand.startswith("/") and not Path(cand).exists():
            continue
        try:
            out = subprocess.run([cand, "-hide_banner", "-filters"],
                                 capture_output=True, text=True, check=True)
            if "vidstabdetect" in out.stdout:
                return cand
        except Exception:
            continue
    return None


def stabilize(src: Path, cache_dir: Path) -> Path:
    """Two-pass libvidstab. Returns a new mp4 at `cache_dir/stable_<hash>.mp4`."""
    ff = _ffmpeg_with_vidstab()
    if not ff:
        raise RuntimeError(
            "Stabilization needs ffmpeg with libvidstab. Install a full build:\n"
            "  macOS:   brew install ffmpeg-full\n"
            "  Windows: winget install Gyan.FFmpeg  (the 'full' variant)\n"
            "and retry."
        )
    cache_dir.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256(f"{src}|{src.stat().st_mtime}".encode()).hexdigest()[:14]
    dst = cache_dir / f"stable_{h}.mp4"
    if dst.exists() and dst.stat().st_size > 0:
        return dst
    transforms = cache_dir / f"stable_{h}.trf"

    # Pass 1: detect motion
    p1 = subprocess.run(
        [ff, "-y", "-i", str(src),
         "-vf", f"vidstabdetect=shakiness=5:accuracy=15:result={transforms}",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    if p1.returncode != 0:
        raise RuntimeError(f"vidstabdetect failed (rc={p1.returncode}):\n{p1.stderr[-1200:]}")

    # Pass 2: apply transforms
    p2 = subprocess.run(
        [ff, "-y", "-i", str(src),
         "-vf", f"vidstabtransform=input={transforms}:zoom=0:smoothing=10,unsharp=5:5:0.8:3:3:0.4",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
         "-c:a", "copy", str(dst)],
        capture_output=True, text=True,
    )
    if p2.returncode != 0:
        raise RuntimeError(f"vidstabtransform failed (rc={p2.returncode}):\n{p2.stderr[-1200:]}")
    transforms.unlink(missing_ok=True)
    return dst
