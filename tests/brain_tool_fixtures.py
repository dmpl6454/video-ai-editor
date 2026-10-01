"""Synthetic sources for the Editor Brain tool tests (EB1-B).

Two kinds of source, both lavfi so no real footage is needed, plus the small
session helpers the three tool test files share:

* ``make_bar_angle`` — a bar-coded camera angle (``frame_map_golden_lib.
  make_bar_source``): every frame carries its own index and a 4-bit source
  id, so a decoded render says which ANGLE and which FRAME each output frame
  shows — the proof that `apply_camera_plan` shifted `in`/`out` by the right
  offset.
* ``make_click_camera`` / ``make_recorder`` — a camera whose picture flashes
  white and whose own microphone clicks at every REFERENCE second, recorded
  with a per-file clock offset, and the reference recorder that clicks at the
  same instants. `timing_fixtures.av_offsets_ms` on a render then measures how
  far the dialogue lane's clicks sit from the picture's flashes.

The offset convention is the one `apply_camera_plan` is frozen to
(`in_ += offsets[angle] − offsets[src]`), which is spec §2.4's "positive =
that file lags the reference": an event at reference second ``r`` is at file
second ``r + offset`` of that camera. `sync_dialogue_lane` therefore lays
``in_ = piece.in_ + offsets[dialogue] − offsets[piece.src]`` (see the
deviation note in tests/test_sync_dialogue_lane_tool.py).

Until lane EB1-A's P2 fixture lands (`tests/brain_fixtures.py`), these are the
click-tracked sources the sync tests run on; the assertions are the same.
"""
from __future__ import annotations

import subprocess
import sys
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))

import frame_map_golden_lib as bars  # noqa: E402

from video_ai_editor.edl.schema import Canvas, Clip, Track  # noqa: E402
from video_ai_editor.edl.snapshot import EDLStore  # noqa: E402

W, H = 320, 180
FPS = 30
SR = 48000
CLICK_S = 0.004


def _run(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
                   check=True, capture_output=True)


def make_bar_angle(path: Path, *, sid: int, seconds: float, fps: int = FPS) -> Path:
    """A bar-coded angle (source id ``sid``), ``seconds`` long, with an AAC tone."""
    spec = bars.SourceSpec(key=path.stem, sid=sid, rate=Fraction(fps), seconds=seconds)
    return bars.make_bar_source(path, spec)


def make_click_camera(path: Path, *, offset_s: float, seconds: float, fps: int = FPS) -> Path:
    """A camera whose clock runs ``offset_s`` AHEAD of the reference: the
    reference's whole second ``r`` is seen (one white frame) and heard (a
    4 ms 1 kHz click on its own microphone) at file time ``r + offset_s``.
    ``offset_s`` must sit on the frame grid so the flash is one frame."""
    k = round(offset_s * fps)
    if abs(k - offset_s * fps) > 1e-6:
        raise ValueError(f"offset {offset_s} is not on the {fps} fps grid")
    # +300 frames / +10 s keep the modulus positive for every t (ffmpeg's mod
    # is fmod: a negative remainder would flash and click on every frame
    # before the first reference second).
    lum = f"if(eq(mod(N-({k})+300\\,{fps})\\,0)\\,235\\,16)"
    click = (f"aevalsrc='if(lt(mod(t-({offset_s})+10\\,1)\\,{CLICK_S})\\,"
             f"0.8*sin(2*PI*1000*t)\\,0)':s={SR}:d={seconds}")
    _run(["-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={fps}:d={seconds}",
          "-f", "lavfi", "-i", click,
          "-vf", f"format=gray,geq=lum='{lum}',format=yuv420p",
          "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast", "-g", str(fps),
          "-c:a", "aac", "-b:a", "192k", "-ac", "1", "-shortest", str(path)])
    return path


def make_recorder(path: Path, *, seconds: float) -> Path:
    """The reference recorder: a click at every whole second, PCM wav."""
    click = (f"aevalsrc='if(lt(mod(t\\,1)\\,{CLICK_S})\\,0.8*sin(2*PI*1000*t)\\,0)'"
             f":s={SR}:d={seconds}")
    _run(["-f", "lavfi", "-i", click, "-c:a", "pcm_s16le", "-ac", "1", str(path)])
    return path


def make_silent_broll(path: Path, *, seconds: float, fps: int = FPS, colour: str = "blue") -> Path:
    """A silent B-roll shot (a picture with a silent audio stream)."""
    _run(["-f", "lavfi", "-i", f"color=c={colour}:s={W}x{H}:r={fps}:d={seconds}",
          "-f", "lavfi", "-i", f"anullsrc=r={SR}:cl=mono",
          "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast",
          "-c:a", "aac", "-shortest", "-pix_fmt", "yuv420p", str(path)])
    return path


def session(root: Path, *, fps: int = FPS, name: str = "s") -> EDLStore:
    """An empty session on a 320x180 canvas (bar codes decode at that size)
    with loudness normalisation off, so a render is fast and sample-exact."""
    store = EDLStore(root / name)
    store.edl.canvas = Canvas(w=W, h=H, fps=fps, loudness_lufs=None)
    store.edl.recompute_duration()
    store.commit("init", {}, "init")
    return store


def v1_pieces(store: EDLStore) -> list[Clip]:
    t = store.edl.get_track("v1")
    return sorted((c for c in t.clips if isinstance(c, Clip)), key=lambda c: c.start)


def lane_clips(store: EDLStore, lane: str) -> list[Clip]:
    t: Track | None = store.edl.get_track(lane)
    return sorted((c for c in (t.clips if t else []) if isinstance(c, Clip)), key=lambda c: c.start)


def layout(store: EDLStore, lane: str = "v1") -> list[tuple[str, float, float, float]]:
    """(src, in, out, start) per media clip in timeline order, rounded so two
    stores that made the same edit compare equal."""
    return [(c.src, round(c.in_, 6), round(c.out, 6), round(c.start, 6))
            for c in lane_clips(store, lane)]


def half_frame_ms(fps: int = FPS) -> float:
    return 1000.0 / fps / 2.0


def click_onsets(path: Path, *, rate: int = SR, thresh: float = 0.2, min_gap_s: float = 0.05) -> list[float]:
    """Onset times of every click in ``path``'s audio. `timing_fixtures.
    click_times` merges clicks closer than 0.3 s; after a tighten cut two
    reference seconds can play 0.1-0.3 s apart, so the refractory window here
    is 50 ms (a click is 4 ms long)."""
    from timing_fixtures import audio_samples
    a = audio_samples(path, rate=rate)
    out: list[float] = []
    last = -1e9
    for i, v in enumerate(a):
        if abs(v) > thresh and i - last > min_gap_s * rate:
            out.append(i / rate)
            last = i
        elif abs(v) > thresh:
            last = i
    return out


def av_offsets_ms(path: Path, *, window_s: float = 0.25) -> list[float]:
    """For every flash onset of the picture, (nearest click − flash) in ms —
    `timing_fixtures.av_offsets_ms` with `click_onsets` above; a flash with
    no click within `window_s` yields nothing (the caller counts them)."""
    from timing_fixtures import flash_onsets
    clicks = click_onsets(path)
    out: list[float] = []
    for f in flash_onsets(path):
        if not clicks:
            break
        c = min(clicks, key=lambda x: abs(x - f))
        if abs(c - f) <= window_s:
            out.append((c - f) * 1000.0)
    return out


@contextmanager
def restriction_off() -> Iterator[None]:
    """Run under the shipped desktop posture (filesystem allowlist OFF), as
    tests/test_transcript_timemap.py explains; restored afterwards."""
    from video_ai_editor import config
    before = config._FORCED_RESTRICT
    config.enable_path_restriction(False)
    try:
        yield
    finally:
        config.enable_path_restriction(before)


__all__ = ["W", "H", "FPS", "SR", "make_bar_angle", "make_click_camera", "make_recorder",
           "make_silent_broll", "session", "v1_pieces", "lane_clips", "layout",
           "half_frame_ms", "click_onsets", "av_offsets_ms", "restriction_off"]
