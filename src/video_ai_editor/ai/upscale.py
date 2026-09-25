"""Real-ESRGAN upscale via the bundled realesrgan-ncnn-vulkan binary.

We unpack frames from the source clip → upscale each PNG → re-encode to mp4.
Heavy operation; cached by source hash + factor so re-renders are instant.
"""
from __future__ import annotations
import hashlib
import shutil
from pathlib import Path

from .. import platformutil as _pu

def _esrgan_dir() -> Path:
    """Find the Real-ESRGAN install. Look first in the per-OS user data dir,
    then legacy XDG location, then the project's models/ directory."""
    candidates = [
        _pu.user_data_dir("Video AI Editor") / "models" / "realesrgan",       # new
        Path.home() / ".local" / "share" / "video-ai-editor" / "models" / "realesrgan",  # legacy
        Path(__file__).resolve().parents[3] / "models" / "realesrgan",        # repo
    ]
    for c in candidates:
        if (c / _pu.exe_name("realesrgan-ncnn-vulkan")).exists():
            return c
    return candidates[0]  # default for error message


ESRGAN_DIR = _esrgan_dir()
ESRGAN_BIN = ESRGAN_DIR / _pu.exe_name("realesrgan-ncnn-vulkan")


def available() -> bool:
    return ESRGAN_BIN.exists()


def upscale_clip(src: Path, cache_dir: Path, *, factor: int = 2,
                 model: str = "realesrgan-x4plus",
                 on_progress=None, cancel_event=None) -> Path:
    """Upscale a video clip and return the path to the new mp4.

    QA-066: reports progress (frames extracted → frames upscaled, counted as
    the binary writes them → encode) and stops on `cancel_event`, leaving no
    work directory behind. Every source frame is kept at the source's exact
    rate (see ai/jobio.encode_frames)."""
    from . import jobio
    if not available():
        raise RuntimeError(f"Real-ESRGAN binary not found at {ESRGAN_BIN}")
    cache_dir.mkdir(parents=True, exist_ok=True)
    # "|f1": outputs from before the frame-exact encode may be a frame short.
    h = hashlib.sha256(f"{src}|{factor}|{model}|f1".encode()).hexdigest()[:14]
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
    stages = jobio.Stages(on_progress, extract=0.05, model=0.85, encode=0.10)
    part = _pu.part_path(dst)
    try:
        rate, _dur, _n = jobio.video_facts(src)
        n = jobio.extract_frames(src, frames_in, on_progress=stages.sub("extract"),
                                 cancel_event=cancel_event)
        # Upscale each frame. The binary segfaults if it can't find models/ on a
        # relative path, so we cd into its directory and pass `-m models`.
        # Windows CreateProcess resolves argv[0] against PATH + the PARENT cwd,
        # NOT the `cwd=` we pass — so a bare exe name fails even with cwd set.
        # Use the absolute binary path on Windows. On POSIX the "./exe" form
        # works because the child chdir's into cwd before exec.
        exe = _pu.exe_name("realesrgan-ncnn-vulkan")
        argv0 = str(ESRGAN_BIN) if _pu.IS_WINDOWS else f"./{exe}"
        rc, out, err = jobio.run(
            [argv0,
             "-i", str(frames_in.resolve()), "-o", str(frames_out.resolve()),
             "-s", str(factor), "-n", model, "-f", "png",
             "-m", "models"],
            cwd=str(ESRGAN_DIR), cancel_event=cancel_event, what="upscale",
            on_tick=jobio.count_ticker(frames_out, n, stages.sub("model")))
        if rc != 0:
            raise RuntimeError(f"realesrgan failed (rc={rc}):\n{err[-1500:]}\n{out[-500:]}")
        stages.done("model")
        jobio.encode_frames(frames_out, src, part, rate=rate,
                            on_progress=stages.sub("encode"), cancel_event=cancel_event)
        _pu.replace_with_retry(part, dst)
    finally:
        _pu.unlink_with_retry(part)
        shutil.rmtree(work, ignore_errors=True)
    return dst
