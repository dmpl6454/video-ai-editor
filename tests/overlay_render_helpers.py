"""Shared helpers for the render/overlay fidelity tests (QA-003/015/016/035/036).

Everything here drives REAL ffmpeg: synthesize media with lavfi, export
through `render_export`, then decode exact frames by index and measure pixels.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from video_ai_editor.edl.schema import EDL, Canvas, Clip, Track


def run_ffmpeg(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True, capture_output=True)


def gray_clip(path: Path, w: int, h: int, dur: float, fps: int = 30,
              color: str = "gray") -> Path:
    run_ffmpeg(["-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}:d={dur}:r={fps}",
                "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={dur}",
                "-shortest", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-c:a", "aac",
                str(path)])
    return path


def base_edl(tmp: Path, w: int, h: int, dur: float, fps: int = 30,
             color: str = "gray") -> EDL:
    tmp.mkdir(parents=True, exist_ok=True)
    src = gray_clip(tmp / f"base_{w}x{h}_{color}.mp4", w, h, dur, fps, color)
    edl = EDL(canvas=Canvas(w=w, h=h, fps=fps), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(id="c1", src=str(src), in_=0.0, out=dur, start=0.0)]),
        Track(id="tx", type="text", z=15, clips=[]),
    ])
    return edl


def frame_rgb(video: Path, index: int) -> np.ndarray:
    """Decode frame number `index` (0-based, by decode order) as HxWx3 uint8."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True)
    w, h = (int(v) for v in probe.stdout.strip().split(",")[:2])
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(video),
         # "-fps_mode passthrough" == the old "-vsync 0"; -vsync was removed in ffmpeg 9.
         "-vf", f"select=eq(n\\,{index})", "-fps_mode", "passthrough", "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True).stdout
    assert len(raw) == w * h * 3, f"frame {index} not decoded ({len(raw)} bytes)"
    return np.frombuffer(raw, np.uint8).reshape(h, w, 3)


def frame_at(video: Path, t: float, fps: float) -> np.ndarray:
    return frame_rgb(video, int(round(t * fps)))


def mask_of(img: np.ndarray, rgb: tuple[int, int, int], tol: int = 60) -> np.ndarray:
    d = np.abs(img.astype(int) - np.array(rgb, int)).sum(axis=2)
    return d <= tol


def bbox(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def centroid(mask: np.ndarray) -> tuple[float, float] | None:
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None
    return float(xs.mean()), float(ys.mean())
