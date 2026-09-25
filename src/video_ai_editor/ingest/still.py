"""Still images (PNG / JPEG / HEIC / WebP …) as timeline media — QA-090.

A photo used to go through the video normaliser like any clip and came out as
exactly what it is to ffmpeg: ONE frame. The clip landed on v1 as a 0.033 s
hairline (a PNG) or 1 s (a HEIC, whose container says 1 fps), and it could not
be lengthened, because every trim is clamped to the source's real length.

A still is therefore normalised into a real video SOURCE that is long enough
to edit — `STILL_SOURCE_SECONDS` of the picture at the project's frame rate,
CFR, with silent audio like every other normalised file — and placed on the
timeline at `STILL_DEFAULT_SECONDS`. From there it is an ordinary clip: trim,
extend (up to the source length), split, transition, keyframe. Nothing in the
render path needs to know it was a photo.

Cost is kept flat by encoding the picture ONCE into a short closed-GOP segment
and stitching copies of that segment with the concat demuxer (stream copy,
continuous timestamps), then muxing silence: ~0.7 s and ~3 MB for five minutes
of a 12 MP photo, against ~18 s for encoding every frame. The concat demuxer,
not `-stream_loop`, because the latter leaves a timestamp gap at every seam
(29.908 fps average where 29.970 was asked) — a variable-rate file is exactly
what normalisation exists to prevent.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable

from .. import platformutil as _pu
from .probe import ProbeResult, probe

#: The length a photo gets when it is placed on the timeline.
STILL_DEFAULT_SECONDS = 5.0
#: How long the normalised source is — the most a still can be extended to.
STILL_SOURCE_SECONDS = 300.0
#: Frames per encoded segment (the concat unit), before stitching.
_SEGMENT_SECONDS = 10.0

_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".jpe", ".heic", ".heif", ".avif",
                         ".webp", ".bmp", ".tif", ".tiff"})


class StillCancelled(RuntimeError):
    """The caller's cancel_event fired while a still was being prepared."""


def is_still_image(src: Path) -> bool:
    """True for a single-picture file. By extension first (cheap, and the
    reliable signal for HEIC, whose container is plain `mov`), then by what
    ffprobe sees: an image demuxer, or only single-frame video streams and
    no audio. An animated GIF has many frames and stays a video."""
    if Path(src).suffix.lower() in _IMAGE_EXTS:
        return True
    try:
        out = subprocess.run(
            [_pu.FFPROBE, "-v", "error", "-show_entries",
             "format=format_name:stream=codec_type,nb_frames", "-of", "json", str(src)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, **_pu.SUBPROCESS_FLAGS)
        data = json.loads(out.stdout or "{}")
    except Exception:
        return False
    fmt = (data.get("format") or {}).get("format_name", "")
    streams = data.get("streams") or []
    if any(s.get("codec_type") == "audio" for s in streams):
        return False
    video = [s for s in streams if s.get("codec_type") == "video"]
    if not video:
        return False
    if fmt == "image2" or fmt.endswith("_pipe"):
        return True
    return all(str(s.get("nb_frames", "")) == "1" for s in video)


def _run(args: list[str], cancel_event: "threading.Event | None") -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise StillCancelled("import cancelled")
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", **_pu.SUBPROCESS_FLAGS)
    if proc.returncode != 0:
        raise RuntimeError(f"Couldn't read this image.\n\nffmpeg said:\n{proc.stderr[-800:]}")


def _flatten_filter(short_side: int | None) -> str:
    """One frame, composited over black (a transparent PNG would otherwise
    show whatever RGB its transparent pixels happen to hold), clamped on the
    short side like video (QA-008), with even dimensions for yuv420p."""
    if short_side is not None:
        s = int(short_side)
        scale = (f"scale=w='if(gte(iw,ih),-2,min({s},trunc(iw/2)*2))'"
                 f":h='if(gte(iw,ih),min({s},trunc(ih/2)*2),-2)'")
    else:
        scale = "scale=w='trunc(iw/2)*2':h='trunc(ih/2)*2'"
    return f"{scale},format=rgba,premultiply=inplace=1,format=rgb24"


def normalize_still(src: Path, dst: Path, *, fps_arg: str = "30",
                    short_side: int | None = 1080,
                    source_seconds: float = STILL_SOURCE_SECONDS,
                    on_progress: Callable[[float], None] | None = None,
                    cancel_event: "threading.Event | None" = None) -> ProbeResult:
    """Write `dst`: `source_seconds` of the still at `fps_arg` (an ffmpeg rate
    string from `edl.timebase.ffmpeg_rate`), H.264 yuv420p CFR + silent AAC."""
    from fractions import Fraction
    dst.parent.mkdir(parents=True, exist_ok=True)
    rate = Fraction(fps_arg)
    seg_frames = max(1, round(_SEGMENT_SECONDS * rate))
    copies = max(1, -(-round(source_seconds * rate) // seg_frames))   # ceil
    part = _pu.part_path(dst)
    with tempfile.TemporaryDirectory(prefix=".still_", dir=str(dst.parent)) as td:
        tmp = Path(td)
        raw, flat = tmp / "raw.png", tmp / "flat.png"
        seg, listing = tmp / "seg.mp4", tmp / "list.txt"
        # Decode first, filter second: a tiled HEIC (every iPhone photo) is a
        # stream GROUP that ffmpeg assembles with its own complex filtergraph,
        # and a `-vf` on the same run is refused ("Simple and complex filtering
        # cannot be used together"). Decoding alone yields the whole picture.
        _run([_pu.FFMPEG, "-v", "error", "-y", "-i", str(src), "-frames:v", "1",
              "-update", "1", str(raw)], cancel_event)
        _run([_pu.FFMPEG, "-v", "error", "-y", "-i", str(raw), "-frames:v", "1",
              "-vf", _flatten_filter(short_side), str(flat)], cancel_event)
        if on_progress:
            on_progress(0.3)
        _run([_pu.FFMPEG, "-v", "error", "-y", "-loop", "1", "-framerate", fps_arg,
              "-i", str(flat), "-frames:v", str(seg_frames),
              "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-crf", "18",
              "-g", str(seg_frames), "-pix_fmt", "yuv420p", "-r", fps_arg, "-an", str(seg)],
             cancel_event)
        if on_progress:
            on_progress(0.7)
        listing.write_text("".join(f"file '{seg.name}'\n" for _ in range(copies)),
                           encoding="utf-8")
        try:
            _run([_pu.FFMPEG, "-v", "error", "-y", "-f", "concat", "-safe", "0",
                  "-i", str(listing), "-f", "lavfi",
                  "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                  "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac",
                  "-ar", "48000", "-ac", "2", "-shortest", "-movflags", "+faststart",
                  "-f", "mp4", str(part)], cancel_event)
            _pu.replace_with_retry(part, dst)
        finally:
            _pu.unlink_with_retry(part)
    if on_progress:
        on_progress(1.0)
    return probe(dst)


__all__ = ["STILL_DEFAULT_SECONDS", "STILL_SOURCE_SECONDS", "StillCancelled",
           "is_still_image", "normalize_still"]
