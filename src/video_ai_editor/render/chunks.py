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
import subprocess
from pathlib import Path
from typing import Callable
from ..edl.schema import Clip


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
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of",
             "default=nokey=1:noprint_wrappers=1", str(p)],
            capture_output=True, text=True, timeout=10,
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

    args = ["ffmpeg", "-y", *inputs, *extras,
            "-filter_complex", fc,
            "-map", v_label, "-map", "[a]",
            "-r", str(fps),
            *encoder_args,
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(dst)]
    proc = subprocess.run(args, capture_output=True, text=True)
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
    """Return one cached chunk path per clip; render any that are missing."""
    paths: list[Path] = []
    for c in clips:
        fp = fingerprint_clip(c, canvas_w=canvas_w, canvas_h=canvas_h, fps=fps,
                              encoder_args=encoder_args)
        chunk = chunk_path_for(cache_dir, fp)
        if not chunk_is_valid(chunk):
            # Corrupt or missing — wipe and rebuild.
            try: chunk.unlink()
            except FileNotFoundError: pass
            render_clip_to_chunk(
                c, dst=chunk,
                canvas_w=canvas_w, canvas_h=canvas_h, fps=fps,
                encoder_args=encoder_args,
                build_video_chain=build_video_chain,
                build_audio_chain=build_audio_chain,
                cache_dir=cache_dir,
            )
        paths.append(chunk)
    evict_old_chunks(cache_dir, keep=200)
    return paths
