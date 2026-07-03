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
import subprocess
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
                      bg_color: str | None = "#00FF00") -> Path:
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
    in_dir = work / "in"
    out_dir = work / "out"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Probe fps so the re-encode keeps timing
    probe = subprocess.run(
        [_pu.FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=avg_frame_rate", "-of",
         "default=nokey=1:noprint_wrappers=1", str(src)],
        capture_output=True, text=True, check=True,
    )
    fps_str = probe.stdout.strip()
    if "/" in fps_str:
        n, d = fps_str.split("/")
        fps_val = float(n) / max(1.0, float(d))
    else:
        fps_val = 30.0

    # Extract source frames
    subprocess.run(
        [_pu.FFMPEG, "-y", "-i", str(src), "-q:v", "2",
         str(in_dir / "f%05d.png")],
        capture_output=True, check=True,
    )
    n_in = len(list(in_dir.glob("*.png")))
    if n_in < 1:
        raise RuntimeError("rembg: no frames extracted from source")

    # Run rembg per-frame. The session is reused across frames so model
    # weights load just once.
    from rembg import new_session, remove  # type: ignore
    from PIL import Image
    session = new_session(model)
    for fp in sorted(in_dir.glob("*.png")):
        img = Image.open(fp).convert("RGBA")
        out_img = remove(img, session=session)
        if not keep_alpha:
            # Composite against bg_color
            bg = Image.new("RGBA", out_img.size, _hex_to_rgba(bg_color))
            bg.paste(out_img, mask=out_img.split()[3])
            bg.convert("RGB").save(out_dir / fp.name)
        else:
            out_img.save(out_dir / fp.name)

    if keep_alpha:
        # Use qtrle / .mov to preserve alpha through the encode
        subprocess.run(
            [_pu.FFMPEG, "-y",
             "-framerate", f"{fps_val:.4f}", "-i", str(out_dir / "f%05d.png"),
             "-i", str(src),
             "-map", "0:v", "-map", "1:a?",
             "-c:v", "qtrle",
             "-c:a", "aac", "-shortest", str(dst)],
            capture_output=True, check=True,
        )
    else:
        subprocess.run(
            [_pu.FFMPEG, "-y",
             "-framerate", f"{fps_val:.4f}", "-i", str(out_dir / "f%05d.png"),
             "-i", str(src),
             "-map", "0:v", "-map", "1:a?",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
             "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-shortest", str(dst)],
            capture_output=True, check=True,
        )
    shutil.rmtree(work, ignore_errors=True)
    return dst


def _hex_to_rgba(hex_color: str) -> tuple[int, int, int, int]:
    s = hex_color.lstrip("#")
    if len(s) == 6:
        r = int(s[0:2], 16); g = int(s[2:4], 16); b = int(s[4:6], 16)
        return (r, g, b, 255)
    return (0, 255, 0, 255)
