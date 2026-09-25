"""Progress, cancel and frame-exact encoding for the heavy per-frame AI tools
(QA-066).

Upscale, smooth slow-mo, stabilise, noise reduction, object erase and
background removal each ran as a chain of blocking `subprocess.run` calls and
Python loops with no way to report how far they were or to stop: the AI card
said "Working… this tool can't be interrupted" for 273 s on a 5 s clip, and
/api/tools honestly advertised them as `cancellable=false,
reports_progress=false`. dispatch() already injects `set_progress` /
`cancel_event` into any handler that declares them (the auto_caption
convention); these helpers are what lets the tools themselves honour them:

* `Stages` maps each step onto its slice of one 0..1 bar;
* `run` is `subprocess.run` that polls a cancel event (killing the child) and
  can report progress while it waits — by counting output frames for the
  ncnn binaries, or from ffmpeg's own `-progress` stream (`run_ffmpeg`);
* `check` is the between-frames cancel point for Python loops;
* `encode_frames` re-encodes a frame sequence at the source's EXACT rate
  string with every frame kept. The old encoders passed a rounded rate
  (`29.970`) and `-shortest`, so a source whose audio ended before its
  picture lost its last frames (the reported 4.967 s from 5.000 s).

A cancel raises `ToolCancelled`, a RuntimeError (→ 422, and recorded by the
job manager as `cancelled` because the cancel is pending).
"""
from __future__ import annotations

import functools
import json
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable, Sequence

from .. import platformutil as _pu

Progress = Callable[[float], None]
_POLL_S = 0.1


class ToolCancelled(RuntimeError):
    """The user cancelled the job while the tool was running."""


def check(cancel_event: "threading.Event | None") -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ToolCancelled("cancelled")


class Stages:
    """Split one progress bar across named steps: `Stages(cb, extract=0.1,
    model=0.8, encode=0.1)`; `stages.sub("model")(0.5)` reports 0.5 of the
    bar (0.1 + 0.8 * 0.5). Monotonic: a step never moves the bar backwards."""

    def __init__(self, on_progress: Progress | None, **weights: float):
        total = sum(weights.values()) or 1.0
        self._cb = on_progress
        self._span: dict[str, tuple[float, float]] = {}
        at = 0.0
        for name, w in weights.items():
            self._span[name] = (at / total, w / total)
            at += w
        self._last = 0.0

    def sub(self, name: str) -> Progress:
        lo, width = self._span[name]

        def report(p: float) -> None:
            if self._cb is None:
                return
            v = lo + width * max(0.0, min(1.0, float(p)))
            if v > self._last + 1e-4:
                self._last = v
                self._cb(v)
        return report

    def done(self, name: str) -> None:
        self.sub(name)(1.0)


def run(args: Sequence[str], *, cancel_event: "threading.Event | None" = None,
        cwd: str | None = None, on_tick: Callable[[], None] | None = None,
        what: str = "tool") -> tuple[int, str, str]:
    """Run a child process; poll `cancel_event` (terminate → kill) and call
    `on_tick` every poll. Returns (rc, stdout, stderr). Output goes to temp
    files so a chatty child can never fill a pipe and deadlock."""
    check(cancel_event)
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(list(args), stdout=out, stderr=err, cwd=cwd,
                                **_pu.SUBPROCESS_FLAGS)
        try:
            while True:
                try:
                    proc.wait(timeout=_POLL_S)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if cancel_event is not None and cancel_event.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    raise ToolCancelled(f"{what} cancelled")
                if on_tick is not None:
                    on_tick()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        out.seek(0)
        err.seek(0)
        return (proc.returncode, out.read().decode("utf-8", "replace"),
                err.read().decode("utf-8", "replace"))


@functools.lru_cache(maxsize=4)
def _stats_period(ffmpeg: str) -> tuple[str, ...]:
    """`-stats_period 0.25` when this ffmpeg has it (5.0+): the default 0.5 s
    means a pass shorter than that never reports a midpoint at all."""
    try:
        rc = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-stats_period", "0.25",
                             "-f", "lavfi", "-i", "nullsrc=s=16x16:d=0.04", "-f", "null", "-"],
                            capture_output=True, timeout=20, **_pu.SUBPROCESS_FLAGS).returncode
    except Exception:
        return ()
    return ("-stats_period", "0.25") if rc == 0 else ()


def run_ffmpeg(args: Sequence[str], *, duration: float,
               on_progress: Progress | None = None,
               cancel_event: "threading.Event | None" = None,
               what: str = "ffmpeg") -> str:
    """ffmpeg with `-progress` into a file that is polled for `out_time`.
    `args` is the full command WITHOUT the output-side `-progress`; it is
    inserted right after the binary. Raises RuntimeError on failure."""
    with tempfile.TemporaryDirectory() as td:
        prog = Path(td) / "progress.txt"
        cmd = [args[0], "-progress", str(prog), "-nostats", *_stats_period(args[0]), *args[1:]]

        def tick() -> None:
            if on_progress is None or duration <= 0:
                return
            try:
                text = prog.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return
            for line in reversed(text.splitlines()):
                key, _, value = line.partition("=")
                if key in ("out_time_us", "out_time_ms"):
                    try:
                        on_progress(max(0.0, min(1.0, int(value) / 1e6 / duration)))
                    except ValueError:
                        pass
                    return

        rc, _out, err = run(cmd, cancel_event=cancel_event, on_tick=tick, what=what)
    if rc != 0:
        raise RuntimeError(f"{what} failed (rc={rc}):\n{err[-1200:]}")
    if on_progress is not None:
        on_progress(1.0)
    return err


def video_facts(src: Path) -> tuple[str, float, int | None]:
    """(exact rate string, duration s, frame count or None) of the first
    video stream — the rate exactly as ffprobe states it (`30000/1001`),
    never rounded."""
    out = subprocess.run(
        [_pu.FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=avg_frame_rate,r_frame_rate,nb_frames,duration:format=duration",
         "-of", "json", str(src)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
        **_pu.SUBPROCESS_FLAGS)
    data = json.loads(out.stdout or "{}")
    s = (data.get("streams") or [{}])[0]
    rate = s.get("avg_frame_rate") or ""
    if not rate or rate.startswith("0"):
        rate = s.get("r_frame_rate") or "30"
    try:
        dur = float(s.get("duration") or (data.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        dur = 0.0
    try:
        n = int(s.get("nb_frames")) if s.get("nb_frames") else None
    except (TypeError, ValueError):
        n = None
    return rate, dur, n


def extract_frames(src: Path, frames_dir: Path, *, on_progress: Progress | None = None,
                   cancel_event: "threading.Event | None" = None) -> int:
    """Every frame of `src` as `f%05d.png` (no rate conversion). Returns the count."""
    frames_dir.mkdir(parents=True, exist_ok=True)
    _rate, dur, _n = video_facts(src)
    run_ffmpeg([_pu.FFMPEG, "-y", "-i", str(src), "-fps_mode", "passthrough", "-q:v", "2",
                str(frames_dir / "f%05d.png")],
               duration=dur, on_progress=on_progress, cancel_event=cancel_event,
               what="frame extract")
    return len(list(frames_dir.glob("f*.png")))


def count_ticker(frames_dir: Path, total: int, on_progress: Progress | None) -> Callable[[], None]:
    """An `on_tick` reporting how many of `total` output frames exist yet."""
    def tick() -> None:
        if on_progress is None or total <= 0:
            return
        try:
            done = sum(1 for _ in frames_dir.glob("*.png"))
        except OSError:
            return
        on_progress(min(1.0, done / total))
    return tick


def encode_frames(frames_dir: Path, src: Path, dst: Path, *, rate: str,
                  video_args: Sequence[str] | None = None,
                  on_progress: Progress | None = None,
                  cancel_event: "threading.Event | None" = None) -> Path:
    """Frames → `dst` at exactly `rate`, EVERY frame kept, with the source's
    audio padded (never allowed to shorten the picture) or silence when the
    source has none."""
    from ..render.compositor import source_has_audio
    n = len(list(frames_dir.glob("f*.png")))
    if n == 0:
        raise RuntimeError("no frames to encode")
    vargs = list(video_args or ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                                "-pix_fmt", "yuv420p"])
    args = [_pu.FFMPEG, "-y", "-framerate", rate, "-i", str(frames_dir / "f%05d.png")]
    if source_has_audio(str(src)):
        args += ["-i", str(src), "-filter_complex", "[1:a:0]apad[a]"]
    else:
        args += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                 "-filter_complex", "[1:a]anull[a]"]
    from fractions import Fraction
    seconds = float(n / Fraction(rate)) if rate else 0.0
    args += ["-map", "0:v", "-map", "[a]", *vargs, "-c:a", "aac", "-b:a", "192k",
             "-frames:v", str(n), "-t", f"{seconds:.6f}", str(dst)]
    run_ffmpeg(args, duration=seconds, on_progress=on_progress, cancel_event=cancel_event,
               what="encode")
    return dst


__all__ = ["Stages", "ToolCancelled", "check", "count_ticker", "encode_frames",
           "extract_frames", "run", "run_ffmpeg", "video_facts"]
