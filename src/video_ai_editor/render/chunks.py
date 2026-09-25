"""Chunk-level render cache.

Each V1 clip is rendered ONCE to a canvas-resolution mp4 with all per-clip
work baked in (scale + transform + effects + speed + mask). The result is
cached by a content fingerprint, so editing one clip only re-renders THAT
clip — the timeline assembly + overlays + audio mix is then a fast concat.

The cache is keyed on the clip itself + the IDENTITY of the file it plays
(size, mtime, inode — `file_identity`) + canvas dims + fps + encoder args, so
previews and exports get separate (matched-quality) chunks and a file replaced
at the same path is never served stale.

Caveats:
- xfade transitions span two clips; chunks are independent so we fall back
  to monolithic render when transitions are present.
- Chunk renders use VideoToolbox at preview/export quality; chunks are not
  reused across the two qualities.
"""
from __future__ import annotations
import contextvars
import hashlib
import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable
from ..edl.schema import Clip, RENDER_BEHAVIOR_VERSION
from .. import platformutil as _pu


def _chunk_workers(n_clips: int) -> int:
    """How many chunk renders to run at once.

    VideoToolbox encode is GPU-bound but the decode + CPU filter graph (scale,
    pad, transform, effects) is the real cost, and that parallelizes across the
    P-cores. Cap at the physical performance-core count so we don't thrash the
    E-cores or oversubscribe the single hardware encode queue. Override with
    VAI_CHUNK_WORKERS.
    """
    env = os.environ.get("VAI_CHUNK_WORKERS")
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    # hw.perflevel0.physicalcpu = performance cores (10 on M4 Max). Fall back
    # to half of logical CPUs elsewhere. sysctl is macOS/BSD-only; skip the
    # doomed subprocess spawn on other platforms.
    p_cores = None
    if _pu.IS_MAC:
        try:
            out = subprocess.run(
                ["sysctl", "-n", "hw.perflevel0.physicalcpu"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=2,
                **_pu.SUBPROCESS_FLAGS,
            )
            p_cores = int(out.stdout.strip())
        except Exception:
            p_cores = None
    if p_cores is None:
        p_cores = max(2, (os.cpu_count() or 4) // 2)
    return max(1, min(p_cores, n_clips))


def _canonical(obj):
    """Stable JSON-friendly serialization for hashing."""
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump(by_alias=True, mode="json")
    if isinstance(obj, (list, tuple)):
        return [_canonical(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _canonical(v) for k, v in obj.items()}
    return obj


def file_identity(path: str | os.PathLike) -> list[int] | None:
    """[size, mtime_ns, inode] of the file a clip plays, or None if missing.

    QA-001: the chunk key used to hold only ``str(c.src)``, so a file replaced
    in place at the same path (two uploads whose names sanitised alike used to
    do exactly that) kept serving the OLD footage from a cached chunk in the
    preview while the export, which re-reads the file, showed the new one.
    Keying on what is actually on disk makes a replaced file a cache miss.
    A stat is microseconds; hashing the content would cost a full read of
    every clip on every preview."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [int(st.st_size), int(st.st_mtime_ns), int(st.st_ino)]


def fingerprint_clip(c: Clip, *, canvas_w: int, canvas_h: int, fps: int,
                     encoder_args: list[str]) -> str:
    payload = {
        "file": file_identity(c.src),
        # RENDER_BEHAVIOR_VERSION (edl/schema.py) is shared with EDL.hash()
        # and the video-only fingerprint — one salt for every render cache,
        # bumped whenever the same clip fields would render to different
        # pixels than before (e.g. apply_lut intensity now actually blends;
        # a pre-fix cached chunk baked full-strength pixels for any partial
        # intensity).
        "v": RENDER_BEHAVIOR_VERSION,
        "src": str(c.src),
        "in": float(c.in_),
        "out": float(c.out),
        "speed": _canonical(c.speed),
        # The chunk bakes gain/fade/mute (build_audio_chain runs at chunk
        # render time), so audio props are part of the chunk's identity —
        # omitting them served stale audio on every volume/fade edit.
        "audio": _canonical(c.audio),
        # The chunk also bakes the visual fade (build_video_chain runs at
        # chunk render time) — omitting these would silently serve a stale
        # un-faded chunk after a set_video_fade edit (the exact bug class
        # the audio comment above documents).
        "video_fade_in": float(getattr(c, "video_fade_in", 0.0) or 0.0),
        "video_fade_out": float(getattr(c, "video_fade_out", 0.0) or 0.0),
        "transform": _canonical(c.transform),
        "effects": _canonical(c.effects),
        "mask": _canonical(c.mask) if c.mask else None,
        "canvas": [canvas_w, canvas_h, fps],
        "enc": encoder_args,
    }
    j = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(j.encode()).hexdigest()[:16]


def chunk_path_for(cache_dir: Path, fp: str) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"chunk_{fp}.mp4"


def chunk_is_valid(p: Path) -> bool:
    """True if the cached chunk is decodable.

    A chunk left over from an interrupted render exists with non-zero size
    but is missing the trailing `moov` atom, which makes ffmpeg refuse to
    open it on the next render ("moov atom not found"). The cheap probe here
    catches that — if ffprobe can't list a video stream we declare the chunk
    invalid and it gets re-rendered.
    """
    try:
        if not p.exists() or p.stat().st_size < 1024:
            return False
        proc = subprocess.run(
            [_pu.FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of",
             "default=nokey=1:noprint_wrappers=1", str(p)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
            **_pu.SUBPROCESS_FLAGS,
        )
        return proc.returncode == 0 and bool(proc.stdout.strip())
    except Exception:
        return False


def evict_old_chunks(cache_dir: Path, keep: int = 200) -> None:
    if not cache_dir.exists():
        return
    files = sorted(cache_dir.glob("chunk_*.mp4"), key=lambda p: p.stat().st_mtime)
    for f in files[:-keep]:
        f.unlink(missing_ok=True)


def render_clip_to_chunk(
    c: Clip,
    *,
    dst: Path,
    canvas_w: int,
    canvas_h: int,
    fps: int,
    encoder_args: list[str],
    build_video_chain: Callable[..., str],
    build_audio_chain: Callable[..., str],
    cache_dir: Path | None = None,
    streams: str = "av",
    audio_codec_args: list[str] | None = None,
) -> None:
    """Run a standalone ffmpeg invocation that produces this clip's chunk.

    `streams` is "av" (a normal chunk), "v" (picture only) or "a" (sound
    only). The single-stream forms exist so render.segments can build a LONG
    clip's chunk from cached picture segments plus one sound render (QA-005)
    through exactly this recipe, rather than a copy of it. `audio_codec_args`
    replaces the AAC codec args (segments.py stores a segmented chunk's sound
    losslessly: chunks are ffmpeg-only intermediates, re-encoded to AAC by
    every assembly path)."""
    if streams not in ("av", "v", "a"):
        raise ValueError(f"streams must be 'av', 'v' or 'a', not {streams!r}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    # Frame-exact trim (QA-002): half-frame seek pre-roll + an exact frame
    # count from the chain, never a float `-ss/-to` that rounds each chunk up
    # a frame. The same input recipe the monolithic renderer uses.
    from .compositor import clip_input_args
    from ..edl import timebase as _tb
    inputs = clip_input_args(c, fps)
    extras: list[str] = []

    v_chain = build_video_chain(
        c, input_label="[0:v]", label_out="[v]",
        canvas_w=canvas_w, canvas_h=canvas_h, fps=fps,
    ) if "v" in streams else ""

    # Mask: same alphamerge pattern as the monolithic renderer
    v_label = "[v]"
    if c.mask is not None and cache_dir is not None and "v" in streams:
        from .effects import render_mask_png, mask_png_is_valid
        mask_path = cache_dir / f"mask_{c.id}_{c.mask.type}_{int(c.mask.feather)}_{canvas_w}x{canvas_h}.png"
        if not mask_png_is_valid(mask_path):
            render_mask_png(c.mask, canvas_w, canvas_h, mask_path)
        extras += ["-i", str(mask_path)]
        v_chain += (
            f";[v][1:v]alphamerge,format=yuva420p[vmrgba];"
            f"color=c=black:s={canvas_w}x{canvas_h}:r={_tb.ffmpeg_rate(fps)}[bg];"
            f"[bg][vmrgba]overlay=format=auto:shortest=1[vm]"
        )
        v_label = "[vm]"

    a_chain = (build_audio_chain(c, input_label="[0:a]", label_out="[a]", fps=fps)
               if "a" in streams else "")
    fc = ";".join(x for x in (v_chain, a_chain) if x)
    v_args = (["-map", v_label, "-r", _tb.ffmpeg_rate(fps), *encoder_args]
              if "v" in streams else ["-vn"])
    # Pin AAC output rate/channels so the encoder never hits EINVAL
    # (-22) on a negotiated PCM layout. See compositor._AAC_OUT.
    a_codec = audio_codec_args or ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    a_args = ["-map", "[a]", *a_codec] if "a" in streams else ["-an"]

    args = [_pu.FFMPEG, "-y", *inputs, *extras,
            "-filter_complex", fc,
            *v_args, *a_args,
            "-movflags", "+faststart",
            # Stage, then swap. This was the ONLY mp4 writer in the render
            # pipeline that wrote straight to its final path, and here the final
            # path is the CONTENT FINGERPRINT — i.e. the cache key. An ffmpeg
            # terminated by SIGTERM (or by a Windows console close, which maps to
            # SIGTERM) still flushes a complete, decodable trailer, so a
            # half-length chunk landed under a valid-looking key and was then
            # served forever: the timeline silently rendered short, and no
            # validity check could tell, because the file genuinely is valid.
            str(tmp := _pu.part_path(dst))]
    try:
        # subprocess.run unless a superseded-preview scope is active, in which
        # case the chunk's ffmpeg is terminated early (render.cancel, QA-004).
        from .cancel import run as _cancellable_run
        proc = _cancellable_run(args, capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                **_pu.SUBPROCESS_FLAGS)
        if proc.returncode != 0:
            raise RuntimeError(
                f"chunk render failed (rc={proc.returncode}):\n{proc.stderr[-1500:]}")
        _pu.replace_with_retry(tmp, dst)
    except BaseException:
        # Includes KeyboardInterrupt/SystemExit — a killed render must not leave
        # the staged file behind to be swept up as a cache entry later.
        _pu.unlink_with_retry(tmp)
        raise


def get_or_build_chunks(
    clips: list[Clip],
    *,
    cache_dir: Path,
    canvas_w: int,
    canvas_h: int,
    fps: int,
    encoder_args: list[str],
    build_video_chain: Callable[..., str],
    build_audio_chain: Callable[..., str],
    segment: bool = False,
    on_progress: Callable[[float], None] | None = None,
) -> list[Path]:
    """Return one cached chunk path per clip; render any that are missing.

    `on_progress(frac)` (QA-097) reports the share of the missing chunks'
    frames rendered so far, once per finished chunk — an export that has to
    build its chunks used to show 0 % for that whole stage.

    `segment=True` (previews) builds a missing chunk of a LONG eligible clip
    from cached picture segments (render.segments, QA-005) instead of one
    whole-clip render; the chunk and its key are the same either way.

    Missing chunks render in parallel across the P-cores — on a cold multi-clip
    timeline this is the difference between "8 clips × 0.5s = 4s serial" and
    "~0.6s wall" because the decode + filter graph runs concurrently. Cache hits
    are free (no render), so warm re-renders stay instant regardless.
    """
    # First pass: resolve every clip's chunk path + decide which need building.
    chunk_paths: list[Path] = []
    to_build: list[tuple[int, Clip, Path]] = []
    for i, c in enumerate(clips):
        fp = fingerprint_clip(c, canvas_w=canvas_w, canvas_h=canvas_h, fps=fps,
                              encoder_args=encoder_args)
        chunk = chunk_path_for(cache_dir, fp)
        chunk_paths.append(chunk)
        if not chunk_is_valid(chunk):
            try:
                chunk.unlink()
            except FileNotFoundError:
                pass
            to_build.append((i, c, chunk))

    from .compositor import clip_frames as _clip_frames
    total_frames = sum(_clip_frames(c, fps) for _i, c, _p in to_build) or 1
    done_frames = [0]
    progress_lock = threading.Lock()

    def _built(clip: Clip) -> None:
        if on_progress is None:
            return
        with progress_lock:
            done_frames[0] += _clip_frames(clip, fps)
            frac = done_frames[0] / total_frames
        on_progress(frac)

    def _build(item: tuple[int, Clip, Path]) -> None:
        _build_one(item)
        _built(item[1])

    def _build_one(item: tuple[int, Clip, Path]) -> None:
        _, clip, dst = item
        if segment:
            from .segments import build_segmented_chunk, segment_bounds
            spans = segment_bounds(clip, fps)
            if spans:
                build_segmented_chunk(
                    clip, spans, dst=dst,
                    canvas_w=canvas_w, canvas_h=canvas_h, fps=fps,
                    encoder_args=encoder_args,
                    build_video_chain=build_video_chain,
                    build_audio_chain=build_audio_chain,
                    cache_dir=cache_dir,
                )
                return
        render_clip_to_chunk(
            clip, dst=dst,
            canvas_w=canvas_w, canvas_h=canvas_h, fps=fps,
            encoder_args=encoder_args,
            build_video_chain=build_video_chain,
            build_audio_chain=build_audio_chain,
            cache_dir=cache_dir,
        )

    if len(to_build) == 1:
        # Single missing chunk (the common edit-one-clip case): no thread
        # pool overhead, render inline.
        _build(to_build[0])
    elif to_build:
        workers = _chunk_workers(len(to_build))
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="vae-chunk") as ex:
            # list() forces every future to resolve and re-raises the first
            # exception so a failed chunk still surfaces (caller falls back to
            # the monolithic render).
            # Each task runs in a COPY of the caller's context: pool threads
            # do not inherit context variables, and render.cancel's
            # superseded-preview scope must reach every chunk's ffmpeg.
            ctxs = [contextvars.copy_context() for _ in to_build]
            list(ex.map(lambda cx, it: cx.run(_build, it), ctxs, to_build))

    # LRU recency for the byte budget (render.cache_budget, QA-106): every
    # chunk this render uses — hit or freshly built — is now the youngest.
    from .cache_budget import touch as _touch
    for p in chunk_paths:
        _touch(p)
    evict_old_chunks(cache_dir, keep=200)
    return chunk_paths
