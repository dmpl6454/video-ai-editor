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

    def _extract(seek: list[str]) -> bool:
        proc = subprocess.run(
            [_pu.FFMPEG, "-y", *seek, "-i", str(src),
             "-frames:v", "1", "-vf", f"scale=-2:{int(height)}",
             "-q:v", "5", str(tmp)],
            capture_output=True,
            **_pu.SUBPROCESS_FLAGS,
        )
        return proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0

    ok = _extract(["-ss", f"{max(0.0, t):.3f}"])
    if not ok:
        # A seek AT or PAST the last frame decodes nothing, so ffmpeg writes no
        # output and this raised — the Timeline asks for a thumb at the tail of
        # a clip, so the user got a broken thumbnail and a console error for a
        # perfectly valid file. Observed on a 4.017s clip: t=3.9 fine, t=3.967
        # a 422. Retry relative to END of file, which always lands on a real
        # frame. Failure path only: a normal thumbnail costs nothing extra.
        _pu.unlink_with_retry(tmp)
        ok = _extract(["-sseof", "-0.2"])
    if not ok:
        _pu.unlink_with_retry(tmp)
        raise RuntimeError(
            f"thumbnail extraction failed for {src.name} at t={t:.2f}")
    _pu.replace_with_retry(tmp, out)
    return out
