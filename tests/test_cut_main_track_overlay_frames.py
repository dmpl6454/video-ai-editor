"""Cutting the main track must not move an overlay by a frame (final QA,
round 3).

THE DEFECT: v1 is assembled with `concat`, whose output runs on a 1/1000000
time base, and each segment's offset is truncated to whole microseconds.
After a 76-frame clip at 30 fps, output frame 107 had pts 3566666 µs, not
107/30 s. The PiP chain and the looped text/sticker inputs sit exactly on the
1/R grid, and `overlay` takes the newest overlay frame with pts ≤ the main
pts — so a main frame 1 µs early got the PREVIOUS overlay frame. The same
source range as ONE clip exported every PiP frame right; cut into eleven
contiguous pieces, 174 of 180 PiP frames showed the frame before, a keyframed
title animated a frame late, and the preview (which draws the right frame)
disagreed with the export. The assembled picture is now put back on the frame
grid (`settb=1/R`) before anything is overlaid on it.

THE CHECK: the two timelines are the same picture, so their exports must be
the same frames — with a PiP whose every frame has its own brightness and a
keyframed title on top.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from video_ai_editor.edl import timebase as tb
from video_ai_editor.edl.schema import EDL, Canvas, Clip, TextClip, TextStyle, Track, Transform
from video_ai_editor.render import render_export

W, H = 320, 180
#: Cut points inside the main clip, in FRAMES of the project rate (the
#: finder's 30 fps cuts: 0.4333 s = 13 frames, 1.1 s = 33, ...).
CUTS = [13, 33, 54, 71, 91, 117, 137, 154]


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True, capture_output=True)


@pytest.fixture(scope="module", params=["30", "24000/1001"])
def fx(request, tmp_path_factory):
    rate = request.param
    d = tmp_path_factory.mktemp("cutfx")
    _ff("-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:r={rate}:d=8", "-f", "lavfi",
        "-i", "anullsrc=r=48000:cl=stereo:d=8", "-shortest", "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast", "-c:a", "aac", str(d / "main.mp4"))
    # every PiP frame its own flat brightness: a frame off is a big step
    _ff("-f", "lavfi", "-i", f"color=c=gray:s=160x90:r={rate}:d=8,format=yuv420p,"
        "geq=lum='16+mod(N*29\\,220)':cb=128:cr=128", "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast", str(d / "pip.mp4"))
    return rate, d


def _edl(rate: str, d: Path, *, cut: bool) -> EDL:
    fps = float(tb.rate_of(rate)) if "/" not in rate else 24000 / 1001
    R = tb.rate_of(fps)
    t = lambda k: float(k / R)                                    # noqa: E731
    in0, n = 1.0, 160                                             # 160 frames of main.mp4 from 1.0 s
    bounds = [0, *CUTS, n] if cut else [0, n]
    v1 = [Clip(id=f"m{i}", src=str(d / "main.mp4"), in_=in0 + t(a), out=in0 + t(b), start=t(a))
          for i, (a, b) in enumerate(zip(bounds, bounds[1:]))]
    pip = Clip(id="p", src=str(d / "pip.mp4"), in_=0.5, out=4.5, start=0.8,
               transform=Transform(x=W / 2, y=H / 2, scale=0.6))
    title = TextClip(id="t", text="HELLO", start=0.3, end=4.9, role="hook",
                     style=TextStyle(color="#FFFF00", size=60),
                     transform=Transform(x=W / 2, y=40, rotation={"keyframes": [[0.0, -30.0], [4.0, 30.0]]}))
    edl = EDL(canvas=Canvas(w=W, h=H, fps=fps, loudness_lufs=None), tracks=[
        Track(id="v1", type="video", z=0, clips=v1),
        Track(id="v2", type="video", z=1, clips=[pip]),
        Track(id="tx", type="text", z=10, clips=[title]),
    ])
    edl.recompute_duration()
    return edl


def _frames(path: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo",
                          "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, H, W).astype(int)


def test_cutting_the_main_track_keeps_every_overlay_on_its_frame(tmp_path, fx):
    rate, d = fx
    one = _frames(render_export(_edl(rate, d, cut=False), tmp_path / "one", height=H).path)
    many = _frames(render_export(_edl(rate, d, cut=True), tmp_path / "many", height=H).path)
    assert one.shape == many.shape
    diff = np.abs(one - many).reshape(len(one), -1).mean(axis=1)
    bad = [i for i, v in enumerate(diff) if v > 0.5]
    assert not bad, (f"{rate}: {len(bad)} of {len(one)} frames differ between one clip and the "
                     f"same range cut into {len(CUTS) + 1} pieces (first {bad[:8]})")
