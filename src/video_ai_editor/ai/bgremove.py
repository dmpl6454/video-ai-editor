"""Background removal via rembg (u2net by default).

Per-frame matte extraction with on-disk cache. The output is a video with the
background composited against a chosen colour (transparent on a green-screen
swatch, or any solid colour) — usable directly on V2 PiP, or chromakeyed in
post if the user wants further compositing freedom.

Heavy: first run downloads ~170 MB of model weights to ~/.u2net/. Cached after.
"""
from __future__ import annotations
import hashlib
import shutil
from pathlib import Path

from .. import platformutil as _pu


def available() -> bool:
    try:
        import importlib
        importlib.import_module("rembg")
        return True
    except ImportError:
        return False


def remove_background(src: Path, cache_dir: Path, *,
                      model: str = "u2net",
                      bg_color: str | None = "#00FF00",
                      on_progress=None, cancel_event=None) -> Path:
    """Strip the background of `src`. Returns the new mp4.

    `bg_color`:
      - "#RRGGBB" string → flatten alpha onto that solid colour. Pass "#00FF00"
        and chroma-key downstream if you want full transparency.
      - None → keep alpha (output uses .mov + qtrle to preserve the alpha plane).
    """
    if not available():
        raise RuntimeError("rembg not installed. `uv add rembg` first.")

    cache_dir.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256(
        f"{src}|{model}|{bg_color}|{src.stat().st_mtime}".encode()
    ).hexdigest()[:14]
    keep_alpha = bg_color is None
    ext = "mov" if keep_alpha else "mp4"
    dst = cache_dir / f"bgr_{h}.{ext}"
    if dst.exists() and dst.stat().st_size > 0:
        return dst

    work = cache_dir / f"rembg_work_{h}"
    if work.exists():
        shutil.rmtree(work)
    try:
        return _remove_frames(src, dst, work, model, bg_color, keep_alpha,
                              on_progress=on_progress, cancel_event=cancel_event)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _remove_frames(src: Path, dst: Path, work: Path, model: str, bg_color: str | None,
                   keep_alpha: bool, *, on_progress=None, cancel_event=None) -> Path:
    """QA-066: extract → matte frame by frame (progress per frame, a cancel
    point between frames) → frame-exact encode at the source's exact rate."""
    from . import jobio
    stages = jobio.Stages(on_progress, extract=0.05, model=0.85, encode=0.10)
    rate, _dur, _n = jobio.video_facts(src)
    in_dir = work / "in"
    out_dir = work / "out"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    n_in = jobio.extract_frames(src, in_dir, on_progress=stages.sub("extract"),
                                cancel_event=cancel_event)
    if n_in < 1:
        raise RuntimeError("rembg: no frames extracted from source")

    # Run rembg per-frame. The session is reused across frames so model
    # weights load just once.
    from rembg import new_session, remove  # type: ignore
    from PIL import Image
    session = new_session(model)
    model_step = stages.sub("model")
    for i, fp in enumerate(sorted(in_dir.glob("*.png"))):
        jobio.check(cancel_event)
        img = Image.open(fp).convert("RGBA")
        out_img = remove(img, session=session)
        if not keep_alpha:
            # Composite against bg_color
            bg = Image.new("RGBA", out_img.size, _hex_to_rgba(bg_color))
            bg.paste(out_img, mask=out_img.split()[3])
            bg.convert("RGB").save(out_dir / fp.name)
        else:
            out_img.save(out_dir / fp.name)
        model_step((i + 1) / n_in)

    # qtrle in a .mov keeps the alpha plane; otherwise H.264. Every frame kept.
    video_args = (["-c:v", "qtrle"] if keep_alpha else
                  ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"])
    part = _pu.part_path(dst)
    try:
        jobio.encode_frames(out_dir, src, part, rate=rate, video_args=video_args,
                            on_progress=stages.sub("encode"), cancel_event=cancel_event)
        _pu.replace_with_retry(part, dst)
    finally:
        _pu.unlink_with_retry(part)
    return dst


def _hex_to_rgba(hex_color: str) -> tuple[int, int, int, int]:
    s = hex_color.lstrip("#")
    if len(s) == 6:
        r = int(s[0:2], 16); g = int(s[2:4], 16); b = int(s[4:6], 16)
        return (r, g, b, 255)
    return (0, 255, 0, 255)
