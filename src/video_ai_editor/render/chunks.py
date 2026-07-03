"""Chunk-level render cache.

Each V1 clip is rendered ONCE to a canvas-resolution mp4 with all per-clip
work baked in (scale + transform + effects + speed + mask). The result is
cached by a content fingerprint, so editing one clip only re-renders THAT
clip — the timeline assembly + overlays + audio mix is then a fast concat.

The cache is keyed on the clip itself + canvas dims + fps + encoder args
so previews and exports get separate (matched-quality) chunks.

Caveats:
- xfade transitions span two clips; chunks are independent so we fall back
  to monolithic render when transitions are present.
- Chunk renders use VideoToolbox at preview/export quality; chunks are not
  reused across the two qualities.
"""
from __future__ import annotations
import hashlib
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable
from ..edl.schema import Clip
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


def fingerprint_clip(c: Clip, *, canvas_w: int, canvas_h: int, fps: int,
                     encoder_args: list[str]) -> str:
    payload = {
        "src": str(c.src),
        "in": float(c.in_),
        "out": float(c.out),
        "speed": _canonical(c.speed),
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
) -> None:
    """Run a standalone ffmpeg invocation that produces this clip's chunk."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    inputs = ["-ss", f"{c.in_:.3f}", "-to", f"{c.out:.3f}", "-i", str(c.src)]
    extras: list[str] = []

    v_chain = build_video_chain(
        c, input_label="[0:v]", label_out="[v]",
        canvas_w=canvas_w, canvas_h=canvas_h,
    )

    # Mask: same alphamerge pattern as the monolithic renderer
    v_label = "[v]"
    if c.mask is not None and cache_dir is not None:
        from .effects import render_mask_png
        mask_path = cache_dir / f"mask_{c.id}_{c.mask.type}_{int(c.mask.feather)}_{canvas_w}x{canvas_h}.png"
        if not mask_path.exists():
            render_mask_png(c.mask, canvas_w, canvas_h, mask_path)
        extras += ["-i", str(mask_path)]
        v_chain += (
            f";[v][1:v]alphamerge,format=yuva420p[vmrgba];"
            f"color=c=black:s={canvas_w}x{canvas_h}:r={fps}[bg];"
            f"[bg][vmrgba]overlay=format=auto:shortest=1[vm]"
        )
        v_label = "[vm]"

    a_chain = build_audio_chain(c, input_label="[0:a]", label_out="[a]")
    fc = f"{v_chain};{a_chain}"

    args = [_pu.FFMPEG, "-y", *inputs, *extras,
            "-filter_complex", fc,
            "-map", v_label, "-map", "[a]",
            "-r", str(fps),
            *encoder_args,
            # Pin AAC output rate/channels so the encoder never hits EINVAL
            # (-22) on a negotiated PCM layout. See compositor._AAC_OUT.
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart",
            str(dst)]
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"chunk render failed (rc={proc.returncode}):\n{proc.stderr[-1500:]}")


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
) -> list[Path]:
    """Return one cached chunk path per clip; render any that are missing.

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

    def _build(item: tuple[int, Clip, Path]) -> None:
        _, clip, dst = item
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
            list(ex.map(_build, to_build))

    evict_old_chunks(cache_dir, keep=200)
    return chunk_paths
