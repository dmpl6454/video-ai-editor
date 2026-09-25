"""QA-035: an exported PiP must follow its scale keyframes.

pip.py sized the element once with `_scalar_or_last(tx.scale)`, so a PiP keyed
to grow from 0.2 to 0.4 exported at 0.4 for its whole appearance while the
browser preview (pipDraw) animated it. Real export, measured per frame: the
red PiP's width at three instants must track the keyed scale.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from video_ai_editor.edl.keyframes import sample
from video_ai_editor.edl.schema import Clip, Keyframe, Track, Transform
from video_ai_editor.render import render_export

sys.path.insert(0, str(Path(__file__).parent))
from overlay_render_helpers import base_edl, bbox, frame_at, gray_clip, mask_of  # noqa: E402

W, H, FPS = 640, 360, 30
PIP_START = 1.0


def _edl(tmp: Path, scale, mask=None):
    edl = base_edl(tmp, W, H, 6.0)
    red = gray_clip(tmp / "red.mp4", 320, 180, 5.0, color="red")
    clip = Clip(id="p1", src=str(red), in_=0.0, out=4.5, start=PIP_START,
                transform=Transform(x=320, y=180, scale=scale))
    if mask is not None:
        clip.mask = mask
    edl.tracks.append(Track(id="v2", type="video", z=5, clips=[clip]))
    edl.recompute_duration()
    return edl


def _red_width(video: Path, t: float) -> int:
    b = bbox(mask_of(frame_at(video, t, FPS), (255, 0, 0), 90))
    assert b is not None, f"no PiP at t={t}"
    return b[2] - b[0] + 1


KF = Keyframe(keyframes=[(0.0, 0.2), (4.0, 0.4)])


def test_pip_scale_keyframes_animate_in_export(tmp_path: Path):
    out = render_export(_edl(tmp_path, KF), tmp_path, height=H).path
    widths = {}
    for t in (1.3, 3.0, 4.9):
        widths[t] = _red_width(out, t)
        want = W * 0.35 * sample(KF, t - PIP_START)   # out_long * 0.35 * S(t)
        assert abs(widths[t] - want) <= 4, f"t={t}: width {widths[t]} vs expected {want:.1f}"
    assert widths[1.3] < widths[3.0] < widths[4.9]


def test_pip_scale_keyframes_animate_with_a_shape_mask(tmp_path: Path):
    from video_ai_editor.edl.schema import Mask
    out = render_export(_edl(tmp_path, KF, mask=Mask(type="circle")), tmp_path, height=H).path
    small, big = _red_width(out, 1.3), _red_width(out, 4.9)
    assert big >= small * 1.6, f"circle PiP did not grow: {small} -> {big}"


def test_static_pip_scale_unchanged(tmp_path: Path):
    out = render_export(_edl(tmp_path, 0.3), tmp_path, height=H).path
    w1, w2 = _red_width(out, 1.3), _red_width(out, 4.9)
    assert w1 == w2 and abs(w1 - W * 0.35 * 0.3) <= 3
