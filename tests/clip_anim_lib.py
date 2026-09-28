"""Shared helpers for the clip-animation render tests (wave E, F1).

Fixtures are synthesised: a SOURCE whose picture is a white, non-square box
(W/4 × H/6) centred on black, and a white non-square STICKER PNG. Every
render is decoded and each frame is MEASURED (the white region's centroid,
area, orientation, brightness and edge softness), then compared with what
`edl/clip_animations.AnimPlan.value` says the animation is at that frame's
own clip-local time `k/R`. A frame early or late moves a slide by W/(d·R)
pixels (43 px at 30 fps for a 0.5 s slide on a 640 px canvas), far outside
the tolerances below.
"""
from __future__ import annotations

import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

FFMPEG = shutil.which("ffmpeg")

#: canvas / render size for every test
W, H = 640, 360


def make_box_source(path: Path, *, w: int, h: int, fps: int, seconds: float) -> Path:
    """A `w`×`h` video at `fps`: black with a white W/4 × H/6 box centred."""
    bw, bh = w // 4, h // 6
    subprocess.run(
        [FFMPEG, "-nostdin", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"color=c=black:s={w}x{h}:r={fps}:d={seconds}",
         "-vf", f"drawbox=x={(w - bw) // 2}:y={(h - bh) // 2}:w={bw}:h={bh}:color=white:t=fill,format=yuv420p",
         "-c:v", "libx264", "-crf", "10", "-preset", "veryfast", "-g", str(fps), str(path)],
        check=True, capture_output=True)
    return path


def make_white_source(path: Path, *, w: int, h: int, fps: int, seconds: float) -> Path:
    subprocess.run(
        [FFMPEG, "-nostdin", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"color=c=white:s={w}x{h}:r={fps}:d={seconds}", "-vf", "format=yuv420p",
         "-c:v", "libx264", "-crf", "10", "-preset", "veryfast", "-g", str(fps), str(path)],
        check=True, capture_output=True)
    return path


def make_sticker_png(path: Path, *, w: int = 200, h: int = 100) -> Path:
    from PIL import Image
    Image.new("RGBA", (w, h), (255, 255, 255, 255)).save(path)
    return path


def decode_y(path: Path, w: int = W, h: int = H) -> np.ndarray:
    raw = subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-i", str(path), "-f", "rawvideo",
                          "-pix_fmt", "gray", "-"], check=True, capture_output=True).stdout
    n = len(raw) // (w * h)
    return np.frombuffer(raw[: n * w * h], np.uint8).reshape(n, h, w)


@dataclass
class Blob:
    cx: float
    cy: float
    area: float
    angle: float        # degrees, clockwise on screen, of the major axis (mod 180)
    level: float        # mean luma of the core (0-255 full range)
    soft: float         # share of the lit pixels that are mid-grey (edge softness)
    lit: int
    sharp: float = 1.0  # steepest one-pixel step / core level (≈1 a hard edge)


def measure(y: np.ndarray, *, floor: int = 24) -> Blob | None:
    """The bright region of a frame, relative to its own peak (a faded box is
    still a box): weighted centroid, area at half the peak, orientation from
    the second moments of that area, core level, and softness — the share of
    the region's pixels between 15 % and 85 % of the core level."""
    peak = int(y.max())
    if peak < floor:
        return None
    f = y.astype(np.float64)
    mask = y > 0.5 * peak
    lit = int(mask.sum())
    if lit < 4:
        return None
    level = float(np.percentile(y[mask], 90))
    wgt = np.clip(f - 0.15 * level, 0, None)
    tot = wgt.sum()
    ys, xs = np.mgrid[0:y.shape[0], 0:y.shape[1]]
    cx = float((wgt * xs).sum() / tot)
    cy = float((wgt * ys).sum() / tot)
    mx, my = xs[mask] - cx, ys[mask] - cy
    cxx, cyy, cxy = float((mx * mx).mean()), float((my * my).mean()), float((mx * my).mean())
    angle = 0.5 * math.degrees(math.atan2(2 * cxy, cxx - cyy))
    around = y > 0.15 * level
    soft = float((around & (y < 0.85 * level)).sum()) / max(1, int(around.sum()))
    step = max(float(np.abs(np.diff(f, axis=0)).max()), float(np.abs(np.diff(f, axis=1)).max()))
    return Blob(cx=cx, cy=cy, area=float(lit), angle=angle, level=level, soft=soft, lit=lit,
                sharp=step / max(1.0, level))


def angle_diff(a: float, b: float) -> float:
    """Difference of two axis angles modulo 180°."""
    d = (a - b) % 180.0
    return min(d, 180.0 - d)
