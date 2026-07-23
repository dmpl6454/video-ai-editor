"""Single-frame JPEG thumbnails for timeline filmstrips + media-bin previews."""
from __future__ import annotations
import hashlib
import os
import subprocess
import threading
from pathlib import Path

from .. import platformutil as _pu


def thumbnail_for(src: Path, cache_dir: Path, *, t: float, height: int = 54) -> Path:
    """Extract (and cache) one scaled frame of `src` at time `t`.

    The cache key includes the source's mtime+size so a re-normalized file at
    the same path can't serve stale frames. Extraction writes to a
    PID/thread-scoped temp and swaps in atomically — same posture as the
    overlay-PNG cache, so a killed request never leaves a torn JPEG behind.
    """
    st = src.stat()
    key = hashlib.sha256(
        f"{src.resolve().as_posix()}|{st.st_mtime_ns}|{st.st_size}"
        f"|{t:.3f}|{height}".encode()
    ).hexdigest()[:16]
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / f"th_{key}.jpg"
    if out.exists() and out.stat().st_size > 0:
        return out
    tmp = cache_dir / f".th_{key}.{os.getpid()}_{threading.get_ident()}.part.jpg"
    proc = subprocess.run(
        [_pu.FFMPEG, "-y", "-ss", f"{max(0.0, t):.3f}", "-i", str(src),
         "-frames:v", "1", "-vf", f"scale=-2:{int(height)}",
         "-q:v", "5", str(tmp)],
        capture_output=True,
        **_pu.SUBPROCESS_FLAGS,
    )
    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        _pu.unlink_with_retry(tmp)
        raise RuntimeError(
            f"thumbnail extraction failed for {src.name} at t={t:.2f}")
    _pu.replace_with_retry(tmp, out)
    return out
