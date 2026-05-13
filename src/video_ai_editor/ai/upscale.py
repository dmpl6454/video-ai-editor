"""Real-ESRGAN upscale via the bundled realesrgan-ncnn-vulkan binary.

We unpack frames from the source clip → upscale each PNG → re-encode to mp4.
Heavy operation; cached by source hash + factor so re-renders are instant.
"""
from __future__ import annotations
import hashlib
import shutil
import subprocess
from pathlib import Path

def _esrgan_dir() -> Path:
    """Find the Real-ESRGAN install. Look first in the user cache, then in the
    project's models/ directory."""
    candidates = [
        Path.home() / ".local" / "share" / "video-ai-editor" / "models" / "realesrgan",
        Path(__file__).resolve().parents[3] / "models" / "realesrgan",
    ]
    for c in candidates:
        if (c / "realesrgan-ncnn-vulkan").exists():
            return c
    return candidates[0]  # default for error message


ESRGAN_DIR = _esrgan_dir()
ESRGAN_BIN = ESRGAN_DIR / "realesrgan-ncnn-vulkan"


def available() -> bool:
    return ESRGAN_BIN.exists()


def upscale_clip(src: Path, cache_dir: Path, *, factor: int = 2,
                 model: str = "realesrgan-x4plus") -> Path:
    """Upscale a video clip and return the path to the new mp4."""
    if not available():
        raise RuntimeError(f"Real-ESRGAN binary not found at {ESRGAN_BIN}")
    cache_dir.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256(f"{src}|{factor}|{model}".encode()).hexdigest()[:14]
    dst = cache_dir / f"upscaled_{h}.mp4"
    if dst.exists() and dst.stat().st_size > 0:
        return dst

    work = cache_dir / f"esrgan_work_{h}"
    if work.exists():
        shutil.rmtree(work)
    frames_in = work / "in"
    frames_out = work / "out"
    frames_in.mkdir(parents=True, exist_ok=True)
    frames_out.mkdir(parents=True, exist_ok=True)

    # Extract frames at source fps
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-q:v", "2", str(frames_in / "f%05d.png")],
        capture_output=True, check=True,
    )
    # Upscale each frame. The binary segfaults if it can't find models/ on a
    # relative path, so we cd into its directory and pass `-m models`.
    proc = subprocess.run(
        ["./realesrgan-ncnn-vulkan",
         "-i", str(frames_in.resolve()), "-o", str(frames_out.resolve()),
         "-s", str(factor), "-n", model, "-f", "png",
         "-m", "models"],
        cwd=str(ESRGAN_DIR),
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"realesrgan failed (rc={proc.returncode}):\n{proc.stderr[-1500:]}\n{proc.stdout[-500:]}")
    # Probe original audio + fps
    fps = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=avg_frame_rate", "-of", "default=nokey=1:noprint_wrappers=1",
         str(src)], capture_output=True, text=True, check=True,
    ).stdout.strip()
    if "/" in fps:
        n, d = fps.split("/")
        fps_val = float(n) / max(1.0, float(d))
    else:
        fps_val = 30.0
    # Re-encode upscaled frames + original audio
    subprocess.run(
        ["ffmpeg", "-y",
         "-framerate", f"{fps_val:.3f}", "-i", str(frames_out / "f%05d.png"),
         "-i", str(src),
         "-map", "0:v", "-map", "1:a?",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(dst)],
        capture_output=True, check=True,
    )
    shutil.rmtree(work, ignore_errors=True)
    return dst
