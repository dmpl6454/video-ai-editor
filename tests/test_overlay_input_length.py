"""A looped overlay input lasts exactly its window (wave E gate, X2).

A sticker with keyframed opacity, an animated text (anim_in / anim_out or a
keyed opacity) and a keyframed-transform text are still PNGs looped into a
stream (`-loop 1 -itsoffset rs -t D`). `D` was the window plus 0.5 s, so an
item ending at the timeline's end outlived the v1 base and `overlay` went on
emitting frames driven by it: 255 frames for a 240-frame plan (F1's
measurement). The plan cap (wave E, item 24; `trim=end_frame` in the graph
since gate RX, it was `-frames:v`) stops the picture at the plan,
which hid it; the input now ends where its window does (`re - rs`), so the
graph itself emits the plan, and `_check_picture` (fatal) holds every render
to it on this path too.
"""
from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
from overlay_render_helpers import base_edl  # noqa: E402

from video_ai_editor.edl.schema import Keyframe, Sticker, TextClip, TextStyle, Track, Transform  # noqa: E402
from video_ai_editor.render import compositor, frame_map  # noqa: E402
from video_ai_editor.render.text_overlay import build_overlay_chain  # noqa: E402

W, H = 320, 180


def _count(path: Path) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                          "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)],
                         check=True, capture_output=True, text=True).stdout
    return int(out.strip().splitlines()[0].rstrip(","))


def _edl(tmp: Path, kind: str, *, fps: int = 30, start: float = 2.01, end: float = 8.0):
    edl = base_edl(tmp, W, H, end, fps=fps)
    if kind == "sticker_kf_opacity":
        png = tmp / "s.png"
        Image.new("RGBA", (40, 40), (255, 0, 0, 255)).save(png)
        edl.tracks.append(Track(id="stickers", type="sticker", z=12, clips=[Sticker(
            id="s1", src=str(png), start=start, end=end,
            transform=Transform(x=160, y=90, opacity=Keyframe(keyframes=[(0.0, 0.2), (3.0, 1.0)])))]))
    elif kind == "anim_text":
        edl.get_track("tx").clips.append(TextClip(
            id="t1", text="HI", start=start, end=end, role=None, anim_in="fade", anim_out="fade",
            style=TextStyle(color="#FF0000", size=40), transform=Transform()))
    elif kind == "anim_text_kf_opacity":
        edl.get_track("tx").clips.append(TextClip(
            id="t1", text="HI", start=start, end=end, role=None, style=TextStyle(color="#FF0000", size=40),
            transform=Transform(opacity=Keyframe(keyframes=[(0.0, 0.0), (2.0, 1.0)]))))
    elif kind == "xform_text":
        edl.get_track("tx").clips.append(TextClip(
            id="t1", text="MOVING", start=start, end=end, role=None, style=TextStyle(color="#FF0000", size=40),
            transform=Transform(x=Keyframe(keyframes=[(0.0, 60.0), (3.5, 260.0)]), y=90)))
    else:
        raise AssertionError(kind)
    edl.recompute_duration()
    return edl


KINDS = ["sticker_kf_opacity", "anim_text", "anim_text_kf_opacity", "xform_text"]


def _looped_t(extra_inputs: list[str]) -> list[float]:
    """The `-t` of every looped (`-loop 1`) input."""
    out = []
    for i, a in enumerate(extra_inputs):
        if a == "-loop":
            j = extra_inputs.index("-t", i)
            out.append(float(extra_inputs[j + 1]))
    return out


@pytest.mark.parametrize("kind", KINDS)
def test_a_looped_overlay_input_lasts_exactly_its_window(tmp_path, kind):
    edl = _edl(tmp_path, kind)
    _chain, extra, _lab = build_overlay_chain(edl, tmp_path / "cache", source_label="[v]", out_label="[vo]",
                                              first_input_index=1, out_w=W, out_h=H, preview=False)
    ts = _looped_t(extra)
    assert ts == [pytest.approx(8.0 - 2.01, abs=1e-6)], extra


@pytest.mark.parametrize("fps", [30, 24, 25, 60])
@pytest.mark.parametrize("kind", KINDS)
def test_an_overlay_ending_at_the_end_does_not_lengthen_the_graph(tmp_path, monkeypatch, kind, fps):
    """The graph ALONE emits the plan: the plan cap is lifted far past it (the
    render path's own call), while `_check_picture` still checks the real
    plan — before the fix this render came out 0.5 s long and was rejected."""
    real = frame_map.planned_frames

    def planned(e, f=None):
        n = real(e, f)
        if inspect.stack()[1].function == "_render_locked":
            return n + 1000
        return n

    monkeypatch.setattr(frame_map, "planned_frames", planned)
    edl = _edl(tmp_path, kind, fps=fps)
    out = compositor._render(edl, tmp_path / "x.mp4", height=H, fps=fps, preview=False,
                             cache_dir=tmp_path / "cache", chunked=False)
    assert _count(out) == real(edl, fps) == 8 * fps


@pytest.mark.parametrize("kind", ["sticker_kf_opacity", "anim_text"])
def test_the_export_of_an_overlay_ending_at_the_end_is_the_plan(tmp_path, kind):
    """The shipped path (with the plan cap), through the fatal check."""
    from video_ai_editor.render import render_export
    edl = _edl(tmp_path, kind)
    out = render_export(edl, tmp_path, height=H).path
    assert _count(out) == frame_map.planned_frames(edl) == 240
