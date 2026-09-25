"""QA-016: abutting overlays must not both draw on the boundary frame.

Every overlay gate used `enable='between(t,rs,re)'`, which is closed at BOTH
ends, so at an SRT cue change (cue 1 ends at 3.000, cue 2 starts at 3.000) the
frame at exactly t=3.000 carried both captions overprinted. Windows are now
half-open `[start, end)` on frame-exact times (`timebase.enable_window`).

Real exports, decoded frame by frame: cue 1 is RED text on the left, cue 2 is
BLUE text on the right, so each frame says unambiguously which cues it holds.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from video_ai_editor.edl import timebase
from video_ai_editor.edl.schema import Clip, Sticker, TextClip, TextStyle, Track, Transform
from video_ai_editor.render import render_export

sys.path.insert(0, str(Path(__file__).parent))
from overlay_render_helpers import base_edl, frame_rgb, gray_clip, mask_of  # noqa: E402

RED, BLUE = (255, 0, 0), (0, 0, 255)


def _cue(cid: str, text: str, start: float, end: float, x: float, color: str) -> TextClip:
    return TextClip(id=cid, text=text, start=start, end=end, role=None,
                    style=TextStyle(color=color, size=90),
                    transform=Transform(x=x, y=180))


def test_enable_window_is_half_open_on_frames():
    lo, hi = timebase.enable_window(3.0, 6.0, 30)
    frames = [n for n in range(0, 200) if lo <= n / 30 < hi]
    assert frames[0] == 90 and frames[-1] == 179
    # A second cue starting where the first ends shares no frame with it.
    lo2, hi2 = timebase.enable_window(0.5, 3.0, 30)
    first = {n for n in range(200) if lo2 <= n / 30 < hi2}
    assert 90 not in first and 89 in first
    # Robust to float noise on the stream clock at the boundary frame.
    assert lo <= 90 / 30 - 1e-9 < hi and not (lo2 <= 90 / 30 - 1e-9 < hi2)


@pytest.mark.parametrize("fps", [30, 25])
def test_abutting_text_cues_share_no_frame_in_export(tmp_path: Path, fps: int):
    edl = base_edl(tmp_path, 640, 360, 5.0, fps=fps)
    tx = edl.get_track("tx")
    tx.clips.append(_cue("t1", "ONE", 0.5, 3.0, 150, "#FF0000"))
    tx.clips.append(_cue("t2", "TWO", 3.0, 4.5, 490, "#0000FF"))
    edl.recompute_duration()
    out = render_export(edl, tmp_path, height=360).path

    boundary = 3 * fps          # the frame at exactly t = 3.000
    before = frame_rgb(out, boundary - 1)
    at = frame_rgb(out, boundary)
    assert mask_of(before, RED).sum() > 200 and mask_of(before, BLUE).sum() < 20
    assert mask_of(at, BLUE).sum() > 200, "cue 2 must start ON its first frame"
    assert mask_of(at, RED).sum() < 20, "cue 1 must be gone on the frame cue 2 starts"


def test_abutting_stickers_share_no_frame_in_export(tmp_path: Path):
    from PIL import Image
    red_png, blue_png = tmp_path / "r.png", tmp_path / "b.png"
    Image.new("RGBA", (64, 64), (255, 0, 0, 255)).save(red_png)
    Image.new("RGBA", (64, 64), (0, 0, 255, 255)).save(blue_png)
    edl = base_edl(tmp_path, 640, 360, 4.0)
    edl.tracks.append(Track(id="stickers", type="sticker", z=11, clips=[
        Sticker(id="s1", src=str(red_png), start=0.0, end=2.0, transform=Transform(x=150, y=180)),
        Sticker(id="s2", src=str(blue_png), start=2.0, end=3.5, transform=Transform(x=490, y=180)),
    ]))
    edl.recompute_duration()
    out = render_export(edl, tmp_path, height=360).path
    at = frame_rgb(out, 60)
    assert mask_of(at, BLUE).sum() > 500 and mask_of(at, RED).sum() < 20


def test_abutting_pips_share_no_frame_in_export(tmp_path: Path):
    edl = base_edl(tmp_path, 640, 360, 4.0)
    red = gray_clip(tmp_path / "red.mp4", 320, 180, 2.0, color="red")
    blue = gray_clip(tmp_path / "blue.mp4", 320, 180, 2.0, color="blue")
    edl.tracks.append(Track(id="v2", type="video", z=5, clips=[
        Clip(id="p1", src=str(red), in_=0.0, out=2.0, start=0.0,
             transform=Transform(x=160, y=180, scale=0.4)),
        Clip(id="p2", src=str(blue), in_=0.0, out=1.5, start=2.0,
             transform=Transform(x=480, y=180, scale=0.4)),
    ]))
    edl.recompute_duration()
    out = render_export(edl, tmp_path, height=360).path
    at = frame_rgb(out, 60)
    assert mask_of(at, BLUE, 90).sum() > 500
    assert mask_of(at, RED, 90).sum() < 20, "outgoing PiP drawn on the incoming PiP's first frame"
