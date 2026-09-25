"""Segment-level preview cache for LONG clips (QA-005, partial).

The chunk cache (render/chunks.py) renders each v1 clip to one mp4. A long
clip is therefore one unit of work: splitting a 12-minute clip re-encoded all
twelve minutes (two new chunks, one per half), and a first preview encoded it
in a single ffmpeg process, one core's worth of decode + scale.

Here a long, time-invariant clip's PICTURE is rendered as fixed segments on a
grid that is global to the SOURCE file (every `step` frames of source time),
each cached under its own key. A split, a trim or a move of such a clip then
reuses every segment the edit did not touch; only the one or two segments that
contain a new edge are rendered. The clip's chunk is assembled from its
segments with `-c:v copy` and ONE sound render of the whole clip — sound is
never segmented, because every independently encoded AAC stream carries its
own priming and padding and joining them clicks (see compositor's QA-030 note).

The CHUNK keeps its key (chunks.fingerprint_clip, untouched), its streams,
its frame count and its audio recipe (render_clip_to_chunk with streams="a");
only its sound CODEC differs — lossless FLAC instead of AAC (SOUND_CODEC_ARGS).
Segments are a cache BENEATH the chunk, used for previews only.

Eligibility is deliberately narrow — the clip must render each frame from the
source frame at the same instant with no dependence on its position in the
clip: speed 1, not reversed, no video fades, no keyframed transform/framing,
no mask/chroma key/matte/tracking, and in/out on the project frame grid (so the
whole clip and its segments sample identical source instants). Anything else
takes the old whole-clip path, unchanged.
"""
from __future__ import annotations

import contextvars
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from .. import platformutil as _pu
from ..edl import timebase as _tb
from ..edl.schema import AudioProps, Clip
from . import cancel as _cancel

#: Segment length in seconds of source time. 0 disables segmentation.
SEGMENT_S = float(os.environ.get("VAI_PREVIEW_SEGMENT_S", "6") or 0)
#: Clips shorter than this many segments gain nothing and keep the old path.
MIN_SEGMENTS = 2
#: Cached segment files kept per session cache (≈1-2 MB each at preview size).
KEEP_SEGMENTS = 400
#: Marker appended to the encoder args in a SEGMENT's key, so a segment key
#: can never equal a whole-clip chunk key (chunks.fingerprint_clip hashes the
#: encoder args; its payload is otherwise untouched).
_SEGMENT_KEY_MARK = "--vae-video-segment-v1"

#: The segmented chunk's SOUND is stored lossless (FLAC in mp4), not AAC.
#: A chunk is an ffmpeg-only intermediate — every consumer (the streamcopy
#: assembly, the re-encode assembly) decodes it and encodes the preview's AAC
#: once — so an AAC encode here was pure cost: 11-34 s for a 12-min clip, the
#: single largest step of a long clip's edit once the picture is segmented.
#: FLAC also has no encoder priming, so the sound stays sample-exact.
SOUND_CODEC_ARGS = ["-c:a", "flac", "-ar", "48000", "-ac", "2"]

_SLOTS_LOCK = threading.Lock()
_SLOTS: threading.BoundedSemaphore | None = None


def _segment_slots() -> threading.BoundedSemaphore:
    """One process-wide ceiling on concurrent segment ffmpegs, so two long
    clips rebuilding at once (both halves of a split) share the cores instead
    of each starting a full pool."""
    global _SLOTS
    with _SLOTS_LOCK:
        if _SLOTS is None:
            from .chunks import _chunk_workers
            _SLOTS = threading.BoundedSemaphore(_chunk_workers(64))
        return _SLOTS


def _is_scalar_model(m) -> bool:
    """True when no field of a pydantic model is a list/dict (keyframes)."""
    if m is None:
        return True
    data = m.model_dump() if hasattr(m, "model_dump") else m
    return all(not isinstance(v, (list, dict)) for v in data.values())


def _on_grid(t: float, fps) -> bool:
    return abs(_tb.quantize(t, fps) - float(t)) < 1e-6


def segment_bounds(c: Clip, fps, segment_s: float | None = None) -> list[tuple[float, float]] | None:
    """The [in, out] source spans clip `c` is rendered from, or None when the
    clip must be rendered whole (ineligible, or too short to be worth it).

    Boundaries are k·step frames of SOURCE time — a grid shared by every clip
    cut from the same file, which is what lets the two halves of a split reuse
    the original clip's segments. The spans tile [in, out] exactly, and their
    frame counts sum to the whole clip's (checked, not assumed)."""
    seg = SEGMENT_S if segment_s is None else float(segment_s)
    if seg <= 0 or fps is None:
        return None
    if c.speed not in (None, 1, 1.0) or getattr(c, "reverse", False):
        return None
    if (float(getattr(c, "video_fade_in", 0.0) or 0.0) > 0
            or float(getattr(c, "video_fade_out", 0.0) or 0.0) > 0):
        return None
    if c.mask is not None or getattr(c, "chromakey", None) is not None:
        return None
    if getattr(c, "matte_src", None) or getattr(c, "track_to", None):
        return None
    if not _is_scalar_model(c.transform) or not _is_scalar_model(getattr(c, "framing", None)):
        return None
    if any(not _is_scalar_model(e.params) for e in (c.effects or [])):
        return None
    a, b = float(c.in_), float(c.out)
    if not (_on_grid(a, fps) and _on_grid(b, fps)):
        return None
    step = max(1, _tb.frame_of(seg, fps))
    fa, fb = _tb.frame_of(a, fps), _tb.frame_of(b, fps)
    if fb - fa < MIN_SEGMENTS * step:
        return None
    cuts = [fa] + [k for k in range((fa // step + 1) * step, fb, step)] + [fb]
    spans = [(_tb.time_of(x, fps), _tb.time_of(y, fps)) for x, y in zip(cuts, cuts[1:])]
    from .compositor import clip_frames
    total = sum(clip_frames(_sub_clip(c, x, y), fps) for x, y in spans)
    if total != clip_frames(c, fps):
        return None
    return spans


def _sub_clip(c: Clip, a: float, b: float) -> Clip:
    """The clip restricted to source [a, b]. Sound props are reset: a segment
    is picture only, and a volume edit must not invalidate its key."""
    return c.model_copy(update={"in_": a, "out": b, "start": 0.0,
                                "audio": AudioProps()}, deep=True)


def segment_path(cache_dir: Path, sub: Clip, *, canvas_w: int, canvas_h: int, fps,
                 encoder_args: list[str]) -> Path:
    from .chunks import fingerprint_clip
    fp = fingerprint_clip(sub, canvas_w=canvas_w, canvas_h=canvas_h, fps=fps,
                          encoder_args=[*encoder_args, _SEGMENT_KEY_MARK])
    return cache_dir / f"seg_{fp}.mp4"


def _present(p: Path) -> bool:
    try:
        return p.stat().st_size >= 1024
    except OSError:
        return False


def _evict(cache_dir: Path, keep: int = KEEP_SEGMENTS) -> None:
    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:          # removed by a concurrent eviction
            return 0.0
    files = sorted(cache_dir.glob("seg_*.mp4"), key=_mtime)
    for f in files[:-keep]:
        _pu.unlink_with_retry(f)


def build_segmented_chunk(
    c: Clip,
    spans: list[tuple[float, float]],
    *,
    dst: Path,
    canvas_w: int,
    canvas_h: int,
    fps,
    encoder_args: list[str],
    build_video_chain: Callable[..., str],
    build_audio_chain: Callable[..., str],
    cache_dir: Path,
) -> None:
    """Write clip `c`'s chunk to `dst` from cached picture segments (rendering
    only the missing ones, in parallel) plus one sound render of the clip."""
    from .chunks import render_clip_to_chunk
    from .compositor import clip_frames

    cache_dir.mkdir(parents=True, exist_ok=True)
    subs = [_sub_clip(c, a, b) for a, b in spans]
    seg_paths = [segment_path(cache_dir, s, canvas_w=canvas_w, canvas_h=canvas_h,
                              fps=fps, encoder_args=encoder_args) for s in subs]
    # A size check, not chunk_is_valid's ffprobe: segments are only ever
    # published by an atomic rename of a finished render, so a present file is
    # a complete one — and a 12-min clip has ~120 of them to check per build.
    missing = [(s, p) for s, p in zip(subs, seg_paths) if not _present(p)]
    # Reused segments are the youngest cache entries now (QA-106 LRU).
    from .cache_budget import touch as _touch
    for p in seg_paths:
        if _present(p):
            _touch(p)
    # Unique per build: two requests may build the same chunk at once.
    audio_path = dst.with_name(
        f".{dst.stem}.{os.getpid()}.{threading.get_ident()}.sound.mp4")

    def _render_segment(item) -> None:
        sub, path = item
        slots = _segment_slots()
        _cancel.acquire(slots)
        try:
            render_clip_to_chunk(sub, dst=path, canvas_w=canvas_w, canvas_h=canvas_h,
                                 fps=fps, encoder_args=encoder_args,
                                 build_video_chain=build_video_chain,
                                 build_audio_chain=build_audio_chain,
                                 cache_dir=cache_dir, streams="v")
        finally:
            slots.release()

    def _render_sound(_item=None) -> None:
        render_clip_to_chunk(c, dst=audio_path, canvas_w=canvas_w, canvas_h=canvas_h,
                             fps=fps, encoder_args=encoder_args,
                             build_video_chain=build_video_chain,
                             build_audio_chain=build_audio_chain,
                             cache_dir=cache_dir, streams="a",
                             audio_codec_args=SOUND_CODEC_ARGS)

    tasks: list[tuple[Callable, object]] = [(_render_sound, None)]
    tasks += [(_render_segment, m) for m in missing]
    from .chunks import _chunk_workers
    workers = max(1, min(len(tasks), _chunk_workers(len(tasks)) + 1))
    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="vae-seg") as ex:
            # Copy the context per task: render.cancel's superseded-preview
            # scope must reach every ffmpeg (pool threads inherit nothing).
            futs = [ex.submit(contextvars.copy_context().run, fn, arg) for fn, arg in tasks]
            for f in futs:
                f.result()
        _mux(seg_paths, [clip_frames(s, fps) for s in subs], audio_path, dst,
             fps=fps, expect_frames=clip_frames(c, fps))
    finally:
        _pu.unlink_with_retry(audio_path)
    _evict(cache_dir)


def _mux(seg_paths: list[Path], frames: list[int], audio_path: Path, dst: Path,
         *, fps, expect_frames: int) -> None:
    """Concatenate the picture segments (packet copy, each file's length pinned
    to its exact frame count) with the clip's sound (copy) into `dst`."""
    tmp = _pu.part_path(dst)
    list_path = tmp.with_name(tmp.name + ".concat.txt")
    lines = []
    for p, n in zip(seg_paths, frames):
        esc = p.resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{esc}'")
        lines.append(f"duration {_tb.time_of(n, fps):.6f}")
    _pu.write_text_utf8(list_path, "\n".join(lines) + "\n")
    try:
        args = [_pu.FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
                "-i", str(audio_path), "-map", "0:v", "-map", "1:a", "-c", "copy",
                "-movflags", "+faststart", str(tmp)]
        try:
            proc = _cancel.run(args, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", **_pu.SUBPROCESS_FLAGS)
            if proc.returncode != 0:
                raise RuntimeError(f"segment mux failed (rc={proc.returncode}):\n"
                                   f"{proc.stderr[-1500:]}")
            got = _count_frames(tmp)
            if got is not None and got != expect_frames:
                raise RuntimeError(f"segment mux produced {got} frames, expected {expect_frames}")
            _pu.replace_with_retry(tmp, dst)
        except BaseException:
            _pu.unlink_with_retry(tmp)
            raise
    finally:
        _pu.unlink_with_retry(list_path)


def _count_frames(p: Path) -> int | None:
    """Video packet count (no decode) — the chunk's frame count."""
    import subprocess
    try:
        out = subprocess.run(
            [_pu.FFPROBE, "-v", "error", "-select_streams", "v:0", "-count_packets",
             "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(p)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, **_pu.SUBPROCESS_FLAGS)
        return int(out.stdout.strip().split(",")[0])
    except Exception:
        return None
