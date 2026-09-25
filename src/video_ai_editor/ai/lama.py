"""Object removal via LaMa inpainting.

Lazy-imports `simple-lama-inpainting` (the convenient PyPI wrapper around the
LaMa model). First call downloads ~200 MB of model weights to a per-user
cache; subsequent calls are fast.

API: object_erase(src_video, bbox, t_start, t_end, cache_dir) → new mp4 with
the bbox region inpainted across the chosen time range.
"""
from __future__ import annotations
import hashlib
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

from .. import platformutil as _pu


def available() -> bool:
    try:
        import importlib
        importlib.import_module("simple_lama_inpainting")
        return True
    except ImportError:
        return False


def object_erase(src: Path, cache_dir: Path, *,
                 bbox: tuple[float, float, float, float],
                 t_start: float = 0.0, t_end: float | None = None,
                 on_progress=None, cancel_event=None) -> Path:
    """Erase the rectangular region `bbox` (x, y, w, h in normalized 0..1 coords)
    from frames between t_start..t_end. Outside the range, frames are passed
    through unchanged. Returns the new mp4.
    """
    if not available():
        raise RuntimeError(
            "LaMa not installed. Install with `uv add simple-lama-inpainting`."
        )

    cache_dir.mkdir(parents=True, exist_ok=True)
    bbox_key = ",".join(f"{v:.4f}" for v in bbox)
    h = hashlib.sha256(
        f"{src}|{bbox_key}|{t_start}|{t_end}|{src.stat().st_mtime}".encode()
    ).hexdigest()[:14]
    dst = cache_dir / f"erased_{h}.mp4"
    if dst.exists() and dst.stat().st_size > 0:
        return dst

    # Probe duration + size
    probe = subprocess.run(
        [_pu.FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,avg_frame_rate", "-of", "json", str(src)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
        **_pu.SUBPROCESS_FLAGS,
    )
    import json as _json
    s = _json.loads(probe.stdout)["streams"][0]
    w, hh = int(s["width"]), int(s["height"])

    work = cache_dir / f"lama_work_{h}"
    if work.exists():
        shutil.rmtree(work)
    try:
        return _erase_frames(src, dst, work, w, hh, bbox, t_start, t_end,
                             on_progress=on_progress, cancel_event=cancel_event)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _erase_frames(src: Path, dst: Path, work: Path, w: int, hh: int,
                  bbox: tuple[float, float, float, float], t_start: float,
                  t_end: float | None, *, on_progress=None, cancel_event=None) -> Path:
    """QA-066: extract → inpaint frame by frame (progress per frame, a cancel
    point between frames) → frame-exact encode at the source's exact rate."""
    from fractions import Fraction
    from . import jobio
    stages = jobio.Stages(on_progress, extract=0.05, model=0.85, encode=0.10)
    rate, _dur, _n = jobio.video_facts(src)
    fps_val = float(Fraction(rate)) if rate else 30.0
    in_dir = work / "in"
    out_dir = work / "out"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    jobio.extract_frames(src, in_dir, on_progress=stages.sub("extract"), cancel_event=cancel_event)

    # Build the binary mask once — same shape for every frame.
    mask = Image.new("L", (w, hh), 0)
    d = ImageDraw.Draw(mask)
    bx, by, bw, bh = bbox
    rect = (int(bx * w), int(by * hh), int((bx + bw) * w), int((by + bh) * hh))
    d.rectangle(rect, fill=255)
    mask_path = work / "mask.png"
    mask.save(mask_path)

    # SimpleLama defaults to CUDA at construction, but the bundled torchscript
    # model has CUDA-tagged ops baked in — `torch.jit.load(...)` without
    # `map_location` therefore explodes on Mac with `aten::empty_strided` on
    # CUDA. Patch by loading manually with `map_location='cpu'` (or 'mps' if
    # the user wants Metal acceleration in future) and dropping the loaded
    # model into a SimpleLama instance bypassing its constructor.
    import torch
    from simple_lama_inpainting import SimpleLama  # type: ignore
    from simple_lama_inpainting.utils import download_model  # type: ignore
    import os as _os

    device = torch.device("cpu")
    lama = SimpleLama.__new__(SimpleLama)
    if _os.environ.get("LAMA_MODEL"):
        model_path = _os.environ["LAMA_MODEL"]
    else:
        from simple_lama_inpainting.models.model import LAMA_MODEL_URL  # type: ignore
        model_path = download_model(LAMA_MODEL_URL)
    lama.model = torch.jit.load(model_path, map_location=device)
    lama.model.eval()
    lama.model.to(device)
    lama.device = device

    frames = sorted(in_dir.glob("*.png"))
    n = len(frames)
    end = t_end if t_end is not None else n / fps_val
    model_step = stages.sub("model")
    for i, fp in enumerate(frames):
        jobio.check(cancel_event)
        t = i / fps_val
        out_path = out_dir / fp.name
        if t < t_start or t > end:
            shutil.copyfile(fp, out_path)
        else:
            img = Image.open(fp).convert("RGB")
            result = lama(img, mask)
            result.save(out_path)
        model_step((i + 1) / n)

    # Re-encode preserving original audio, every frame kept.
    part = _pu.part_path(dst)
    try:
        jobio.encode_frames(out_dir, src, part, rate=rate,
                            on_progress=stages.sub("encode"), cancel_event=cancel_event)
        _pu.replace_with_retry(part, dst)
    finally:
        _pu.unlink_with_retry(part)
    return dst
