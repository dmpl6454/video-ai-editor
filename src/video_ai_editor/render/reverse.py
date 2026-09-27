"""Reversed clips (QA-037): `Clip.reverse` plays the clip backwards.

`Clip.reverse` has been a settable field since the first schema, and
`set_property reverse=true` reported success — but nothing in render/ read it,
so the export played forwards (frames 0, 30, 180 at t = 0, 1, 6 s).

HOW. ffmpeg's `reverse`/`areverse` filters hold the WHOLE stream in memory
before emitting a frame — minutes of 1080p is gigabytes — so a reversed clip
is never reversed inside the main graph. Instead its source range is rendered
ONCE into a cached intermediate that already plays backwards, and the
renderer substitutes that intermediate for the clip (`with_reversed_sources`:
same clip, `src` = the intermediate, `in` = 0, `reverse` = False). Everything
downstream — frame-exact trimming, speed, transforms, effects, fades, chunk
and segment caches, preview and export alike — is then the ordinary forward
path, unchanged.

The intermediate is built in SEGMENTS of a bounded number of frames: each
segment is decoded on the project grid (the same seek-preroll / fps / clone-
pad / frame-count recipe the compositor uses), reversed on its own — so memory
is bounded by one segment — and the segments are joined LAST-FIRST with the
concat demuxer, packet copy. Sound is PCM, reversed per segment on exactly the
samples of that segment's frames, so it stays sample-locked to the picture.

The file holds the clip's WHOLE source range `[in, out)` at the project rate,
M = frame_of(out - in) frames: frame j of it is source frame (M-1-j), so the
reversed clip's first frame is the last frame the forward clip would show.
It is keyed on the source's on-disk identity, the range and the rate, lives
in `cache/reversed/` and is a render cache like any other (LRU byte budget:
render.cache_budget).
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

from .. import platformutil as _pu
from ..edl import timebase as _tb
from ..edl.schema import Clip
from . import cancel as _cancel

#: Bumped whenever the intermediate's recipe changes (it is part of the key).
_RECIPE = "rev-v1"
#: Raw decoded frames one segment may hold while `reverse` buffers it.
_SEGMENT_BYTES = 384 * 1024 * 1024
#: Segment length bounds, in frames.
_MIN_SEG_FRAMES, _MAX_SEG_SECONDS = 8, 4
#: Frames of decode slack past a segment's last frame.
_SLACK_FRAMES = 2
_SAMPLE_RATE = 48000

_BUILD_LOCKS: dict[str, threading.Lock] = {}
_BUILD_LOCKS_GUARD = threading.Lock()


@lru_cache(maxsize=256)
def _probe(src: str, mtime_ns: int, size: int) -> tuple[int, int, bool, bool]:
    """(width, height, has_video, has_audio) of a source (cached on identity)."""
    try:
        out = subprocess.run(
            [_pu.FFPROBE, "-v", "error", "-show_entries",
             "stream=codec_type,width,height", "-of", "json", src],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, **_pu.SUBPROCESS_FLAGS).stdout
        streams = json.loads(out or "{}").get("streams", [])
    except Exception:
        streams = []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    has_a = any(s.get("codec_type") == "audio" for s in streams)
    if v is None:
        return 0, 0, False, has_a
    return int(v.get("width") or 1920), int(v.get("height") or 1080), True, has_a


def _source_info(src: str) -> tuple[int, int, bool, bool]:
    try:
        st = os.stat(src)
    except OSError:
        return 1920, 1080, True, True     # a missing file fails later, clearly
    return _probe(str(src), st.st_mtime_ns, st.st_size)


def _sar_of(c: Clip) -> str:
    """`setsar` of the intermediate: the source's own sample aspect for an
    anamorphic source (render/sar.py — it used to be squared to 1:1 without
    resampling, so a reversed PAL/HDV clip exported squeezed), else 1."""
    from .sar import source_anamorphic
    ana = source_anamorphic(c.src)
    return ana.sar_text if ana is not None else "1"


def _key(c: Clip, fps) -> str:
    from .chunks import file_identity
    payload = {"r": _RECIPE, "file": file_identity(c.src), "src": str(c.src),
               "in": float(c.in_), "out": float(c.out), "fps": _tb.ffmpeg_rate(fps)}
    sar = _sar_of(c)
    if sar != "1":
        payload["sar"] = sar          # only then: every square key is unchanged
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def reversed_frames(c: Clip, fps) -> int:
    """Frames the intermediate holds: the clip's whole source range."""
    return max(1, _tb.frame_of(float(c.out) - float(c.in_), fps))


def _segment_frames(w: int, h: int, fps) -> int:
    per_frame = max(1, int(w * h * 1.5))
    by_mem = _SEGMENT_BYTES // per_frame
    by_time = int(_MAX_SEG_SECONDS * float(_tb.rate_of(fps)))
    return max(_MIN_SEG_FRAMES, min(by_mem, by_time))


def _video_codec_args() -> list[str]:
    """A near-lossless intermediate: libx264 at crf 10 when it works here,
    otherwise the export encoder ladder at high quality."""
    from .compositor import _usable_encoder, _video_encoder_args
    if _usable_encoder("libx264"):
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "10",
                "-pix_fmt", "yuv420p"]
    return _video_encoder_args(preview=False, crf=10)


def _render_segment(c: Clip, fps, j0: int, n: int, s0: int, m: int, dst: Path, *,
                    has_video: bool, has_audio: bool, vcodec: list[str]) -> None:
    """Source frames [j0, j0+n) of clip `c` (and samples [s0, s0+m) of its
    sound), reversed, into `dst`."""
    t0 = float(c.in_) + _tb.time_of(j0, fps)
    pre = _tb.seek_preroll(t0, fps)
    seek = max(0.0, t0 - pre)
    span = pre + _tb.time_of(n + _SLACK_FRAMES, fps)
    rate = _tb.ffmpeg_rate(fps)
    parts, maps = [], []
    from .audio_mix import input_seek     # no `-ss 0` on AAC (QA-120)
    inputs = [*input_seek(seek), "-t", f"{span:.6f}", "-i", str(c.src)]
    if has_video:
        parts.append(f"[0:v]setpts=PTS-STARTPTS,fps={rate},"
                     f"tpad=stop={n}:stop_mode=clone,trim=end_frame={n},"
                     f"reverse,setpts=PTS-STARTPTS,setsar={_sar_of(c)}[v]")
        maps += ["-map", "[v]", "-r", rate, *vcodec]
    if has_audio:
        a_in = "[0:a]"
    else:
        a_in = f"anullsrc=channel_layout=stereo:sample_rate={_SAMPLE_RATE},"
    trim_pre = f"atrim=start={pre:.6f},asetpts=PTS-STARTPTS," if has_audio and pre > 1e-9 else ""
    parts.append(f"{a_in}aresample=async=1:first_pts=0,"
                 f"aformat=sample_fmts=s16:channel_layouts=stereo:sample_rates={_SAMPLE_RATE},"
                 f"{trim_pre}apad=whole_len={m},atrim=end_sample={m},"
                 f"areverse,asetpts=PTS-STARTPTS[a]")
    maps += ["-map", "[a]", "-c:a", "pcm_s16le", "-ar", str(_SAMPLE_RATE), "-ac", "2"]
    args = [_pu.FFMPEG, "-y", "-v", "error", *inputs,
            "-filter_complex", ";".join(parts), *maps, str(dst)]
    proc = _cancel.run(args, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", **_pu.SUBPROCESS_FLAGS)
    if proc.returncode != 0:
        raise RuntimeError(f"reverse segment failed (rc={proc.returncode}):\n"
                           f"{(proc.stderr or '')[-1500:]}")


def build_reversed(c: Clip, fps, cache_dir: Path) -> Path:
    """The cached reversed intermediate for clip `c` (built if missing)."""
    out_dir = Path(cache_dir) / "reversed"
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f"rev_{_key(c, fps)}.mov"
    with _BUILD_LOCKS_GUARD:
        lock = _BUILD_LOCKS.setdefault(str(dst), threading.Lock())
    _cancel.acquire(lock)
    try:
        if dst.exists() and dst.stat().st_size > 0:
            from .cache_budget import touch
            touch(dst)
            return dst
        _build(c, fps, dst)
        return dst
    finally:
        lock.release()


def _build(c: Clip, fps, dst: Path) -> None:
    w, h, has_video, has_audio = _source_info(str(c.src))
    total = reversed_frames(c, fps)
    seg = _segment_frames(w, h, fps) if has_video else max(total, 1)
    spans = [(j0, min(seg, total - j0)) for j0 in range(0, total, seg)]
    vcodec = _video_codec_args() if has_video else []
    work = Path(tempfile.mkdtemp(prefix=".rev_", dir=dst.parent))
    try:
        jobs = []
        for k, (j0, n) in enumerate(spans):
            s0 = _tb.samples_for_frames(j0, fps)
            m = _tb.samples_for_frames(j0 + n, fps) - s0
            jobs.append((k, j0, n, s0, m, work / f"seg_{k:05d}.mov"))

        def _one(job) -> None:
            _k, j0, n, s0, m, path = job
            _render_segment(c, fps, j0, n, s0, m, path, has_video=has_video,
                            has_audio=has_audio, vcodec=vcodec)

        from .chunks import _chunk_workers
        workers = max(1, min(len(jobs), _chunk_workers(len(jobs))))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="vae-rev") as ex:
            # Each task in a COPY of the caller's context, so the render's
            # cancel scope (supersede, deadline, export cancel) reaches it.
            futs = [ex.submit(contextvars.copy_context().run, _one, j) for j in jobs]
            for f in futs:
                f.result()
        # Last segment first: that is the start of the reversed clip.
        lines = []
        for _k, j0, n, _s0, _m, path in reversed(jobs):
            esc = path.resolve().as_posix().replace("'", "'\\''")
            lines += [f"file '{esc}'", f"duration {_tb.time_of(n, fps):.6f}"]
        listing = work / "list.txt"
        _pu.write_text_utf8(listing, "\n".join(lines) + "\n")
        tmp = _pu.part_path(dst)
        try:
            proc = _cancel.run(
                [_pu.FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0",
                 "-i", str(listing), "-c", "copy", str(tmp)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                **_pu.SUBPROCESS_FLAGS)
            if proc.returncode != 0:
                raise RuntimeError(f"reverse join failed (rc={proc.returncode}):\n"
                                   f"{(proc.stderr or '')[-1500:]}")
            _pu.replace_with_retry(tmp, dst)
        except BaseException:
            _pu.unlink_with_retry(tmp)
            raise
    finally:
        _pu.rmtree_with_retry(work)


def view_out(c: Clip) -> float:
    """The reversed view's `out` (its `in` is 0): the clip's OWN source span
    `out - in`, the very float `Clip.effective_duration` divides — so the
    view keeps the original's timeline footprint bit for bit (review RD3).
    It was the intermediate's length `time_of(M)`: equal in exact
    arithmetic, but a reversed 2x/4x clip with an odd frame count sits on a
    .5-frame tie, which the two floats rounded differently (the export and
    the program map then laid a frame more or less than the EDL did, and
    every later v1 clip slid); and a curve piece cut off the grid played its
    curve over M/R instead of its real span."""
    return float(c.out) - float(c.in_)


def reversed_view(c: Clip, fps, cache_dir: Path) -> Clip:
    """`c` playing its reversed intermediate forwards (a deep copy)."""
    path = build_reversed(c, fps, cache_dir)
    return c.model_copy(deep=True, update={
        "src": str(path), "in_": 0.0, "out": view_out(c), "reverse": False})


def has_reversed(edl) -> bool:
    return any(getattr(c, "reverse", False) and isinstance(c, Clip)
               for t in edl.tracks for c in t.clips)


def with_reversed_sources(edl, cache_dir: Path | None, fps):
    """`edl` with every reversed media clip swapped for its intermediate.

    Returns `edl` itself when nothing is reversed (the common case costs one
    walk of the tracks); otherwise a deep copy — the caller's EDL, its hash
    and every cache key derived from it are untouched."""
    if not has_reversed(edl):
        return edl
    if cache_dir is None:
        cache_dir = Path(tempfile.gettempdir()) / "vae-reversed"
    out = edl.model_copy(deep=True)
    for t in out.tracks:
        t.clips = [reversed_view(c, fps, cache_dir)
                   if isinstance(c, Clip) and getattr(c, "reverse", False) else c
                   for c in t.clips]
    return out
