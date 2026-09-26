"""Single-frame JPEG thumbnails for timeline filmstrips + media-bin previews."""
from __future__ import annotations
import hashlib
import json
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


# ---------------------------------------------------------------------------
# Filmstrip sprites (QA-059): one JPEG per (source, grid step, page).
#
# The timeline used to ask for one /thumb per filmstrip tile — one ffmpeg spawn
# each, ~24 requests per paint on a long clip. A sprite carries SPRITE_MAX
# tiles side by side: tile i of page p shows the frame at
# `(p * n + i) * step` seconds. `step` is the client's zoom-stable grid
# (lib/filmstrip.thumbGrid: 0.5 s doubled), so nearby zoom levels share a
# sprite. Built in ONE ffmpeg run: one input-seek per tile (fast keyframe seek,
# accurate decode to the frame), each trimmed to its first frame and scaled to
# `height`, then `hstack`ed. Slots past the end of the file are padded black,
# so every sprite is exactly `n` equal tiles wide and the client can address a
# tile as `width / n` without knowing the source's duration.

SPRITE_MAX = 32
_STEP_MIN = 0.5
_STEP_MAX = 4096.0
# Frames at the very tail decode nothing (see thumbnail_for's -sseof note), so
# a tile inside this margin of the end reads the frame just before it.
_TAIL_MARGIN = 0.25


def valid_sprite_step(step: float) -> bool:
    """`step` must lie on the client's grid: 0.5 s × 2^k, up to 4096 s. Any
    other value would mint unbounded cache entries for the same footage."""
    s = _STEP_MIN
    while s <= _STEP_MAX:
        if abs(step - s) < 1e-9:
            return True
        s *= 2
    return False


def _probe_video(src: Path) -> tuple[float, int, int]:
    """(picture duration s, width, height) of the first video stream.

    The PICTURE's own duration when the stream states one: a container whose
    audio runs past its last frame reports the longer format duration, and a
    seek into that overhang decodes no frame at all."""
    proc = subprocess.run(
        [_pu.FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,duration:format=duration",
         "-of", "json", str(src)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=20, **_pu.SUBPROCESS_FLAGS)
    try:
        info = json.loads(proc.stdout or "{}")
        vs = (info.get("streams") or [{}])[0]
        w, h = int(vs.get("width", 0)), int(vs.get("height", 0))
        durs = [float(d) for d in (vs.get("duration"), (info.get("format") or {}).get("duration"))
                if d not in (None, "N/A")]
        dur = min(durs) if durs else 0.0
    except (ValueError, TypeError, IndexError) as e:
        raise RuntimeError(f"cannot read {src.name}: no video stream") from e
    if not (dur > 0) or w <= 0 or h <= 0:
        raise RuntimeError(f"cannot read {src.name}: no video stream")
    return dur, w, h


def sprite_times(duration: float, *, step: float, page: int, n: int) -> list[float]:
    """The source time of each REAL tile of `page` (slots at or past the end of
    the file are omitted — they are padded). A tile inside the tail margin
    reads a frame just before the end instead of nothing."""
    out: list[float] = []
    last = max(0.0, duration - _TAIL_MARGIN)
    for i in range(n):
        t = (page * n + i) * step
        if t >= duration - 1e-6:
            break
        out.append(min(t, last))
    return out


def sprite_for(src: Path, cache_dir: Path, *, step: float, page: int,
               n: int = 16, height: int = 72) -> Path:
    """Build (and cache) one filmstrip sprite of `src`. Raises ValueError for
    parameters off the grid / past the end, RuntimeError when ffmpeg fails.

    Cached on FILE IDENTITY (resolved path + mtime + size) like thumbnail_for,
    so a re-normalised file at the same path never serves stale frames."""
    if not valid_sprite_step(step):
        raise ValueError(f"step {step} is not on the filmstrip grid")
    if not (1 <= n <= SPRITE_MAX):
        raise ValueError(f"n must be 1..{SPRITE_MAX}")
    if page < 0:
        raise ValueError("page must be >= 0")
    st = src.stat()
    key = hashlib.sha256(
        f"sprite|{src.resolve().as_posix()}|{st.st_mtime_ns}|{st.st_size}"
        f"|{step:.3f}|{page}|{n}|{height}".encode()
    ).hexdigest()[:16]
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / f"sp_{key}.jpg"
    if out.exists() and out.stat().st_size > 0:
        return out
    duration, w, h = _probe_video(src)
    times = sprite_times(duration, step=step, page=page, n=n)
    if not times:
        raise ValueError(f"page {page} starts past the end of {src.name}")
    # One decoder per tile: two threads each keeps a 16-tile 1080p sprite
    # near 370 MB peak (ffmpeg's default thread count took it to 1.5 GB);
    # above 1080p a single thread each.
    threads = "2" if w * h <= 1920 * 1080 else "1"
    args: list[str] = [_pu.FFMPEG, "-v", "error", "-y"]
    chains: list[str] = []
    for i, t in enumerate(times):
        args += ["-threads", threads, "-ss", f"{t:.3f}", "-i", str(src)]
        chains.append(f"[{i}:v]trim=end_frame=1,setpts=PTS-STARTPTS,"
                      f"scale=-2:{int(height)},setsar=1[t{i}]")
    k = len(times)
    joined = "".join(f"[t{i}]" for i in range(k))
    stack = f"{joined}hstack=inputs={k}" if k > 1 else "[t0]null"
    if k < n:
        stack += f",pad=w=iw/{k}*{n}:h=ih:x=0:y=0:color=black"
    graph = ";".join(chains) + ";" + stack + "[out]"
    tmp = cache_dir / f".sp_{key}.{os.getpid()}_{threading.get_ident()}.part.jpg"
    proc = subprocess.run(
        [*args, "-filter_complex", graph, "-map", "[out]",
         "-frames:v", "1", "-q:v", "5", str(tmp)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        **_pu.SUBPROCESS_FLAGS)
    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        _pu.unlink_with_retry(tmp)
        raise RuntimeError(
            f"sprite extraction failed for {src.name} (step {step}, page {page}): "
            f"{(proc.stderr or '')[-300:]}")
    _pu.replace_with_retry(tmp, out)
    return out
