"""PIP (v2 overlay) — placement when a clip is DRAGGED onto the lane, and the
time offset that makes it play its own footage.

Reported as "PIP/overlay not working properly, instead of adding a layer on the
video, it just applies a black box on the top left". That is two defects at
once, and each on its own is enough to produce the description:

  * a clip dragged onto v2 kept Transform's plain x=0/y=0, which the PIP
    renderer reads as "centre this on the canvas ORIGIN" — three-quarters
    off-screen, only the bottom-right corner showing, in the top-left;
  * the PIP's input carried no `-itsoffset`, so its frames started at t=0 in
    the filtergraph while `enable=between(t,start,…)` only revealed it later.
    By then the stream had ended and overlay's default eof_action=repeat held
    the LAST decoded frame for the entire appearance — a still image, and a
    black one whenever the clip ends dark.

The PIP AUDIO path has always applied that offset via `adelay`, so the sound
played in the right place while the picture was frozen.
"""
from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl.schema import Clip, Keyframe
from video_ai_editor.edl.snapshot import EDLStore
from video_ai_editor.render.pip import build_pip_overlay_chain


def _store(tmp_path: Path) -> EDLStore:
    s = EDLStore(tmp_path)
    s.edl.canvas.w, s.edl.canvas.h = 1080, 1920
    s.edl.get_track("v1").clips.append(
        Clip(id="base", src="/x/base.mp4", in_=0, out=10, start=0.0))
    s.commit("seed", {}, "seed")
    return s


def _chain(edl, **kw):
    return build_pip_overlay_chain(
        edl, source_label="[v]", out_label="[out]", first_input_index=1,
        out_w=kw.get("w", 1080), out_h=kw.get("h", 1920))


# ------------------------------------------------- placement on a dragged clip

def test_dragging_a_clip_onto_v2_centres_it(tmp_path):
    """The reported top-left box. Transform defaults are 0,0 — fine as a v1
    crop pan, nonsense as a PIP's canvas centre."""
    s = _store(tmp_path)
    v1 = s.edl.get_track("v1")
    v1.clips.append(Clip(id="c1", src="/x/a.mp4", in_=0, out=3, start=0.0))
    s.commit("seed2", {}, "seed2")

    res = dispatch(s, "move_clip", {"clip_id": "c1", "new_track": "v2",
                                    "new_start": 2.0})
    _, c = s.edl.get_clip("c1")
    assert (c.transform.x, c.transform.y) == pytest.approx((540.0, 960.0))
    assert c.transform.scale == pytest.approx(0.6)
    assert res["transform_rebased"] == "centred as a PIP"


def test_the_centred_pip_is_not_drawn_off_canvas(tmp_path):
    """Prove it through the filtergraph, not just the stored numbers: at 0,0
    the overlay x expression is `(0.00)-overlay_w/2`, i.e. negative."""
    s = _store(tmp_path)
    v2 = s.edl.get_track("v2")
    off = Clip(id="off", src="/x/a.mp4", in_=0, out=3, start=0.0)
    v2.clips.append(off)
    chain, *_ = _chain(s.edl)
    assert "x='(0.00)-overlay_w/2'" in chain, "pre-fix placement (kept as the foil)"

    off.transform.x, off.transform.y = 540.0, 960.0
    chain, *_ = _chain(s.edl)
    assert "x='(540.00)-overlay_w/2'" in chain
    assert "y='(960.00)-overlay_h/2'" in chain


def test_moving_a_pip_back_to_v1_clears_the_placement(tmp_path):
    """A PIP's centre is meaningless as a v1 crop pan, where it instead slides
    an already-fitted picture and crops the far edge."""
    s = _store(tmp_path)
    v2 = s.edl.get_track("v2")
    pip = Clip(id="p", src="/x/a.mp4", in_=0, out=3, start=0.0)
    pip.transform.x, pip.transform.y, pip.transform.scale = 800.0, 1500.0, 0.6
    v2.clips.append(pip)
    s.commit("seed2", {}, "seed2")

    dispatch(s, "move_clip", {"clip_id": "p", "new_track": "v1", "new_start": 20.0})
    _, c = s.edl.get_clip("p")
    assert (c.transform.x, c.transform.y, c.transform.scale) == pytest.approx((0.0, 0.0, 1.0))


def test_a_keyframed_transform_is_left_alone(tmp_path):
    """Same rule set_clip_fit follows: one lane change cannot express what an
    authored curve should become, and destroying it silently is worse."""
    s = _store(tmp_path)
    v1 = s.edl.get_track("v1")
    c = Clip(id="k", src="/x/a.mp4", in_=0, out=3, start=0.0)
    c.transform.x = Keyframe(keyframes=[(0.0, 100.0), (2.0, 400.0)])
    v1.clips.append(c)
    s.commit("seed2", {}, "seed2")

    res = dispatch(s, "move_clip", {"clip_id": "k", "new_track": "v2", "new_start": 1.0})
    _, moved = s.edl.get_clip("k")
    assert not isinstance(moved.transform.x, (int, float)), "the curve survives"
    assert res["transform_rebased"] is None


def test_moving_between_two_pip_lanes_changes_nothing(tmp_path):
    """v2 and any other non-v1 video lane mean the same thing, so a move
    between them must preserve the layout exactly."""
    s = _store(tmp_path)
    s.edl.tracks.append(type(s.edl.get_track("v2"))(
        id="v3", type="video", z=2, label="PIP 2"))
    v2 = s.edl.get_track("v2")
    pip = Clip(id="p", src="/x/a.mp4", in_=0, out=3, start=0.0)
    pip.transform.x, pip.transform.y, pip.transform.scale = 800.0, 1500.0, 0.4
    v2.clips.append(pip)
    s.commit("seed2", {}, "seed2")

    dispatch(s, "move_clip", {"clip_id": "p", "new_track": "v3", "new_start": 0.0})
    _, c = s.edl.get_clip("p")
    assert (c.transform.x, c.transform.y, c.transform.scale) == pytest.approx((800.0, 1500.0, 0.4))


def test_a_same_lane_move_never_rebases(tmp_path):
    s = _store(tmp_path)
    v2 = s.edl.get_track("v2")
    pip = Clip(id="p", src="/x/a.mp4", in_=0, out=3, start=0.0)
    pip.transform.x, pip.transform.y = 800.0, 1500.0
    v2.clips.append(pip)
    s.commit("seed2", {}, "seed2")

    dispatch(s, "move_clip", {"clip_id": "p", "new_start": 7.0})
    _, c = s.edl.get_clip("p")
    assert (c.transform.x, c.transform.y) == pytest.approx((800.0, 1500.0))


# ------------------------------------------------------------ the time offset

def test_a_pip_input_is_offset_to_its_timeline_position(tmp_path):
    s = _store(tmp_path)
    s.edl.get_track("v2").clips.append(
        Clip(id="p", src="/x/a.mp4", in_=1.0, out=5.0, start=8.0))
    _, inputs, _, _ = _chain(s.edl)
    assert "-itsoffset" in inputs
    assert inputs[inputs.index("-itsoffset") + 1] == "8.000"
    # …decoding only the trimmed span, expressed as a DURATION.
    assert inputs[inputs.index("-ss") + 1] == "1.000"
    assert inputs[inputs.index("-t") + 1] == "4.000"


def test_a_duration_is_used_not_an_absolute_end(tmp_path):
    """`-to` is an absolute input timestamp and `-itsoffset` shifts the
    timestamps it is compared against, so the two together can truncate the
    input to nothing. A duration is immune."""
    s = _store(tmp_path)
    s.edl.get_track("v2").clips.append(
        Clip(id="p", src="/x/a.mp4", in_=2.0, out=6.0, start=30.0))
    _, inputs, _, _ = _chain(s.edl)
    assert "-to" not in inputs


def test_a_pip_at_zero_needs_no_offset(tmp_path):
    """Nothing to shift, and the argv stays exactly what it always was."""
    s = _store(tmp_path)
    s.edl.get_track("v2").clips.append(
        Clip(id="p", src="/x/a.mp4", in_=0.0, out=4.0, start=0.0))
    _, inputs, _, _ = _chain(s.edl)
    assert "-itsoffset" not in inputs


def test_every_pip_gets_its_own_offset(tmp_path):
    s = _store(tmp_path)
    v2 = s.edl.get_track("v2")
    v2.clips.append(Clip(id="p1", src="/x/a.mp4", in_=0, out=2, start=1.0))
    v2.clips.append(Clip(id="p2", src="/x/b.mp4", in_=0, out=2, start=6.0))
    _, inputs, _, _ = _chain(s.edl)
    offsets = [inputs[i + 1] for i, a in enumerate(inputs) if a == "-itsoffset"]
    assert offsets == ["1.000", "6.000"]


def test_the_render_behaviour_salt_moved_for_this_fix():
    """A cache that outlives the fix hides the fix.

    The preview cache keys on `edl.hash()`, the chunk cache and the audio-only
    remux fast path key on their own fingerprints, and all three fold in
    RENDER_BEHAVIOR_VERSION. A session holding a cached render of a PIP at
    start>0 (or of a rotated clip — same round) would otherwise be served the
    pre-fix pixels forever for an EDL nobody edited, which reads as "the fix
    did nothing" and is exactly the failure this salt exists to prevent.
    """
    from video_ai_editor.edl.schema import RENDER_BEHAVIOR_VERSION
    assert RENDER_BEHAVIOR_VERSION >= 8


def test_the_enable_window_still_matches_the_clip(tmp_path):
    """The offset places the frames; `enable` still gates the appearance. If
    these two ever disagree the PIP flashes the wrong footage at its edges."""
    s = _store(tmp_path)
    s.edl.get_track("v2").clips.append(
        Clip(id="p", src="/x/a.mp4", in_=1.0, out=5.0, start=8.0))
    chain, _, _, _ = _chain(s.edl)
    assert "between(t\\,8.000\\,12.000)" in chain
