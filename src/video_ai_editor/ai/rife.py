"""Smooth slow-motion via RIFE frame interpolation.

Extract source frames → run rife-ncnn-vulkan to insert in-between frames →
re-encode at the original FPS for true smooth slow-mo (factor × playback time
with no judder). Cached by (src, factor) hash.
"""
from __future__ import annotations
import hashlib
import shutil
import subprocess
from pathlib import Path

from .. import platformutil as _pu


def _rife_dir() -> Path:
    candidates = [
        _pu.user_data_dir("Video AI Editor") / "models" / "rife",             # new, per-OS
        Path.home() / ".local" / "share" / "video-ai-editor" / "models" / "rife"
            / "rife-ncnn-vulkan-20221029-macos",                              # legacy
        Path(__file__).resolve().parents[3] / "models" / "rife",              # repo
    ]
    for c in candidates:
        if (c / _pu.exe_name("rife-ncnn-vulkan")).exists():
            return c
    return candidates[0]


RIFE_DIR = _rife_dir()
RIFE_BIN = RIFE_DIR / _pu.exe_name("rife-ncnn-vulkan")


def available() -> bool:
    return RIFE_BIN.exists()


def smooth_slow_motion(src: Path, cache_dir: Path, *, factor: int = 2,
                       model: str = "rife-v4.6",
                       on_progress=None, cancel_event=None) -> Path:
    """Generate `factor`× the input frames via RIFE so playing the result at
    the original fps yields smooth `factor`× slow-mo. Returns the new mp4.

    QA-066: progress (extract → interpolated frames counted as RIFE writes
    them → encode) and cancel, with the work directory always removed.
    """
    from . import jobio
    if not available():
        raise RuntimeError(f"RIFE binary not found at {RIFE_BIN}")
    if factor < 2:
        raise ValueError("RIFE factor must be ≥ 2")
    cache_dir.mkdir(parents=True, exist_ok=True)
    # "|a1": outputs from before QA-040 carry no audio and must not be reused.
    h = hashlib.sha256(f"{src}|{factor}|{model}|{src.stat().st_mtime}|a1".encode()).hexdigest()[:14]
    dst = cache_dir / f"smooth_{h}_x{factor}.mp4"
    if dst.exists() and dst.stat().st_size > 0:
        return dst

    work = cache_dir / f"rife_work_{h}"
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
        from fractions import Fraction
        fps_val = float(Fraction(rate)) if rate else 30.0
        n_in = jobio.extract_frames(src, frames_in, on_progress=stages.sub("extract"),
                                    cancel_event=cancel_event)
        if n_in < 2:
            raise RuntimeError("RIFE needs at least 2 frames")

        # RIFE wants total target frame count; -j flag controls threads.
        # Windows CreateProcess resolves argv[0] against PATH + the PARENT cwd,
        # NOT the `cwd=` we pass — so a bare exe name fails even with cwd set.
        # Use the absolute binary path on Windows. On POSIX the "./exe" form
        # works because the child chdir's into cwd before exec.
        target = n_in * factor
        exe = _pu.exe_name("rife-ncnn-vulkan")
        argv0 = str(RIFE_BIN) if _pu.IS_WINDOWS else f"./{exe}"
        rc, _out, err = jobio.run(
            [argv0,
             "-i", str(frames_in.resolve()), "-o", str(frames_out.resolve()),
             "-m", model, "-n", str(target), "-f", "f%05d.png"],
            cwd=str(RIFE_DIR), cancel_event=cancel_event, what="slow motion",
            on_tick=jobio.count_ticker(frames_out, target, stages.sub("model")))
        if rc != 0:
            raise RuntimeError(f"rife failed (rc={rc}):\n{err[-1500:]}")
        stages.done("model")
        jobio.check(cancel_event)
        encode_slowmo(frames_out, src, part, fps=fps_val, factor=factor, rate=rate)
        stages.done("encode")
        _pu.replace_with_retry(part, dst)
    finally:
        _pu.unlink_with_retry(part)
        shutil.rmtree(work, ignore_errors=True)
    return dst


def _slow_atempo_chain(factor: int) -> str:
    """atempo stages for 1/factor (each stage is limited to [0.5, 2]), each
    preceded by the compositor's WSOLA lag compensation so the stretched
    sound sits on the interpolated picture (see compositor._ATEMPO_LAG)."""
    from ..render.compositor import _ATEMPO_LAG
    stages = []
    remaining = 1.0 / float(factor)
    while remaining < 0.5:
        stages.append(f"{_ATEMPO_LAG},atempo=0.5")
        remaining /= 0.5
    if abs(remaining - 1.0) > 0.001:
        stages.append(f"{_ATEMPO_LAG},atempo={remaining:.6f}")
    return ",".join(stages) or "anull"


def encode_slowmo(frames_dir: Path, src: Path, dst: Path, *, fps: float,
                  factor: int, rate: str | None = None) -> Path:
    """Encode interpolated frames at the SOURCE fps (so playback lasts
    factor× the original) WITH the source's audio stretched to match.

    QA-040: this used to encode with ``-an``. smooth_slow_motion then swapped
    the clip's src for a picture-only file, and because every v1 source is
    assumed to carry audio (ingest adds a silent track to one that doesn't),
    the whole timeline's preview and export failed with "missing a needed
    stream", and every later audio tool failed on the clip. The audio is
    time-stretched with atempo (pitch kept) to the new length; a source with
    no audio yields a SILENT track rather than none, so the output always
    satisfies the invariant ingest establishes.
    """
    from ..render.compositor import source_has_audio
    frames = sorted(frames_dir.glob("f*.png"))
    n = len(frames)
    video_s = n / float(fps) if fps else 0.0
    # The exact rational when known (QA-066): `29.970030` is not 30000/1001.
    args = [_pu.FFMPEG, "-y",
            "-framerate", rate or f"{fps:.6f}", "-i", str(frames_dir / "f%05d.png")]
    if source_has_audio(str(src)):
        args += ["-i", str(src)]
        af = (f"[1:a:0]aresample=async=1:first_pts=0,{_slow_atempo_chain(factor)},"
              f"apad,atrim=end={video_s:.6f}[a]")
    else:
        args += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        af = f"[1:a]atrim=end={video_s:.6f}[a]"
    args += ["-filter_complex", af, "-map", "0:v", "-map", "[a]",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
             "-frames:v", str(n), str(dst)]
    subprocess.run(args, capture_output=True, check=True, **_pu.SUBPROCESS_FLAGS)
    return dst
