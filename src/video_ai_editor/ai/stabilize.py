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
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", check=True, **_pu.SUBPROCESS_FLAGS)
            if "vidstabdetect" in out.stdout:
                return cand
        except Exception:
            continue
    return None


def stabilize(src: Path, cache_dir: Path, *, on_progress=None, cancel_event=None) -> Path:
    """Two-pass libvidstab. Returns a new mp4 at `cache_dir/stable_<hash>.mp4`.

    QA-066: both passes report ffmpeg's own progress (detect = first half,
    transform = second) and stop on `cancel_event`."""
    from . import jobio
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
    # The .trf path is embedded inside the -vf filtergraph (result=/input=), not
    # passed as an -i argv, so it needs filtergraph escaping — a raw Windows
    # `C:\...` path breaks the parser (drive colon + backslashes).
    trf_filt = _pu.ffmpeg_filter_path(transforms)
    _rate, dur, _n = jobio.video_facts(src)
    stages = jobio.Stages(on_progress, detect=0.45, transform=0.55)
    part = _pu.part_path(dst)
    try:
        # Pass 1: detect motion
        jobio.run_ffmpeg(
            [ff, "-y", "-i", str(src),
             "-vf", f"vidstabdetect=shakiness=5:accuracy=15:result={trf_filt}",
             "-f", "null", "-"],
            duration=dur, on_progress=stages.sub("detect"), cancel_event=cancel_event,
            what="vidstabdetect")
        # Pass 2: apply transforms
        jobio.run_ffmpeg(
            [ff, "-y", "-i", str(src),
             "-vf", f"vidstabtransform=input={trf_filt}:zoom=0:smoothing=10,unsharp=5:5:0.8:3:3:0.4",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
             "-c:a", "copy", str(part)],
            duration=dur, on_progress=stages.sub("transform"), cancel_event=cancel_event,
            what="vidstabtransform")
        _pu.replace_with_retry(part, dst)
    finally:
        _pu.unlink_with_retry(part)
        transforms.unlink(missing_ok=True)
    return dst
