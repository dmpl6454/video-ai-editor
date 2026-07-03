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
                 t_start: float = 0.0, t_end: float | None = None) -> Path:
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
        capture_output=True, text=True, check=True,
    )
    import json as _json
    s = _json.loads(probe.stdout)["streams"][0]
    w, hh = int(s["width"]), int(s["height"])
    fps_str = s["avg_frame_rate"]
    if "/" in fps_str:
        a, b = fps_str.split("/")
        fps_val = float(a) / max(1.0, float(b))
    else:
        fps_val = 30.0

    work = cache_dir / f"lama_work_{h}"
    if work.exists():
        shutil.rmtree(work)
    in_dir = work / "in"
    out_dir = work / "out"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [_pu.FFMPEG, "-y", "-i", str(src), "-q:v", "2", str(in_dir / "f%05d.png")],
        capture_output=True, check=True,
    )

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
    for i, fp in enumerate(frames):
        t = i / fps_val
        out_path = out_dir / fp.name
        if t < t_start or t > end:
            shutil.copyfile(fp, out_path)
            continue
        img = Image.open(fp).convert("RGB")
        result = lama(img, mask)
        result.save(out_path)

    # Re-encode preserving original audio
    subprocess.run(
        [_pu.FFMPEG, "-y",
         "-framerate", f"{fps_val:.4f}", "-i", str(out_dir / "f%05d.png"),
         "-i", str(src),
         "-map", "0:v", "-map", "1:a?",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(dst)],
        capture_output=True, check=True,
    )
    shutil.rmtree(work, ignore_errors=True)
    return dst
