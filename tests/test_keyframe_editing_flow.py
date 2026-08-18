"""Editing an ANIMATED property must not silently delete the animation.

`set_clip_transform` assigns straight onto the Transform, so on a keyframed
property it replaced a whole Keyframe object with a float. That is not a corner
case: it is the exact sequence the Properties panel instructs the user to follow
("move the playhead, press this, then change scale/position"). Press Keyframe,
drag the Scale slider, and the key you just set is gone — the clip is static and
the button falls back to "not animated". Reported as "still the keyframes
doesn't work as it should be working".

The fix is the optional clip-local `time`: with it, a value written to an
already-keyframed property becomes a keyframe AT that time. Without it the
scalar overwrite stands, which is how a caller deliberately flattens an
animation — every pre-existing Claude/MCP/template caller depends on that.
"""
from __future__ import annotations

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl.keyframes import sample
from video_ai_editor.edl.schema import Clip, Track, empty_edl
from video_ai_editor.edl.snapshot import EDLStore

KF5 = ["scale", "rotation", "opacity", "x", "y"]


@pytest.fixture()
def store(tmp_path):
    st = EDLStore(tmp_path / "s_kf")
    edl = empty_edl()
    v1 = next(t for t in edl.tracks if t.id == "v1")
    v1.clips.append(Clip(id="c1", src="/tmp/x.mp4", start=0.0, in_=0.0, out=6.0))
    st.edl = edl
    st.commit("test_setup", {}, "seed")
    return st


def _scale(store):
    return store.edl.get_clip("c1")[1].transform.scale


def _keys(v):
    return [tuple(k) for k in v.keyframes]


def test_the_panels_own_instructions_produce_an_animation(store):
    """Key it, move the playhead, change the value -> a ramp, not a static clip."""
    dispatch(store, "add_keyframe", {"clip_id": "c1", "props": KF5, "time": 0.0})
    assert _keys(_scale(store)) == [(0.0, 1.0)]

    dispatch(store, "set_clip_transform", {"clip_id": "c1", "scale": 2.0, "time": 3.0})

    sc = _scale(store)
    assert not isinstance(sc, float), "the animation was replaced by a scalar"
    assert _keys(sc) == [(0.0, 1.0), (3.0, 2.0)]
    # It genuinely ramps rather than merely retaining two keys.
    assert sample(sc, 1.5) == pytest.approx(1.5)


def test_one_key_is_enough_to_count_as_animated(store):
    """The reported flow breaks on a property holding exactly ONE key.

    `keyframes.is_keyframed` wants >= 2, so reusing it here would leave that
    first press clobberable — i.e. fix nothing for the case actually reported.
    One press of the panel's Keyframe button is exactly this state: the `props`
    form does not seed a t=0 anchor, so each property gets precisely one key.
    """
    from video_ai_editor.edl.keyframes import is_keyframed

    dispatch(store, "add_keyframe", {"clip_id": "c1", "props": KF5, "time": 2.0})
    sc = _scale(store)
    assert len(_keys(sc)) == 1
    assert not is_keyframed(sc), \
        "if this ever returns True the test no longer pins the >= 2 distinction"

    dispatch(store, "set_clip_transform", {"clip_id": "c1", "scale": 3.0, "time": 4.0})
    sc = _scale(store)
    assert not isinstance(sc, float)
    assert _keys(sc) == [(2.0, 1.0), (4.0, 3.0)]


def test_editing_at_an_existing_key_time_replaces_that_key(store):
    dispatch(store, "add_keyframe", {"clip_id": "c1", "props": KF5, "time": 0.0})
    dispatch(store, "set_clip_transform", {"clip_id": "c1", "scale": 2.0, "time": 3.0})
    dispatch(store, "set_clip_transform", {"clip_id": "c1", "scale": 2.5, "time": 3.0})
    assert _keys(_scale(store)) == [(0.0, 1.0), (3.0, 2.5)]


def test_no_time_still_flattens_an_animation(store):
    """The deliberate escape hatch — do not "fix" this into keyframe-preservation."""
    dispatch(store, "add_keyframe", {"clip_id": "c1", "props": KF5, "time": 0.0})
    dispatch(store, "set_clip_transform", {"clip_id": "c1", "scale": 2.0, "time": 3.0})
    dispatch(store, "set_clip_transform", {"clip_id": "c1", "scale": 1.25})
    assert _scale(store) == pytest.approx(1.25)


def test_time_on_an_unanimated_property_is_a_plain_set(store):
    """So the UI can always pass the playhead without thinking about it."""
    dispatch(store, "set_clip_transform", {"clip_id": "c1", "rotation": 12.0, "time": 2.5})
    assert store.edl.get_clip("c1")[1].transform.rotation == pytest.approx(12.0)


def test_a_move_does_not_key_the_properties_it_did_not_touch(store):
    """Only the properties named in the call become keyframes."""
    dispatch(store, "add_keyframe", {"clip_id": "c1", "props": KF5, "time": 0.0})
    dispatch(store, "set_clip_transform", {"clip_id": "c1", "x": 100.0, "time": 2.0})
    tx = store.edl.get_clip("c1")[1].transform
    assert _keys(tx.x) == [(0.0, 0.0), (2.0, 100.0)]
    assert _keys(tx.scale) == [(0.0, 1.0)], "scale gained a key it was never given"


def test_keying_mid_animation_pins_the_interpolated_pose(store):
    """A keyframe that says "leave it exactly as it looks" must not move it.

    The panel used to compute the values itself and sent the LAST key's value,
    so with keys at 0->1.0 and 4->3.0 pressing Keyframe at t=2 stored 3.0 where
    the clip actually showed 2.0. It now sends no `values` at all and the
    handler samples, which it already did correctly for omitted properties.
    """
    dispatch(store, "add_keyframe", {"clip_id": "c1", "props": ["scale"],
                                     "time": 0.0, "values": {"scale": 1.0}})
    dispatch(store, "add_keyframe", {"clip_id": "c1", "props": ["scale"],
                                     "time": 4.0, "values": {"scale": 3.0}})
    before = sample(_scale(store), 2.0)
    assert before == pytest.approx(2.0)

    dispatch(store, "add_keyframe", {"clip_id": "c1", "props": KF5, "time": 2.0})

    assert sample(_scale(store), 2.0) == pytest.approx(before), \
        "pinning the current pose changed the animation"
    assert (2.0, 2.0) in _keys(_scale(store))


def test_stickers_and_text_are_covered_too(tmp_path):
    """Both carry a Transform and are keyframable, so both could be clobbered.

    `set_clip_transform`'s own comment claims text clips have no transform;
    schema.py gives TextClip one (defaulting to x=540,y=1700), which is what
    makes that easy to miss.
    """
    from video_ai_editor.edl.schema import Sticker, TextClip

    st = EDLStore(tmp_path / "s_ov")
    edl = empty_edl()
    tr_s = next((t for t in edl.tracks if t.type == "sticker"), None)
    tr_t = next((t for t in edl.tracks if t.type == "text"), None)
    assert tr_s is not None and tr_t is not None
    tr_s.clips.append(Sticker(id="sk1", src="/tmp/a.png", start=0.0, end=5.0))
    tr_t.clips.append(TextClip(id="tx1", text="HI", start=0.0, end=5.0))
    st.edl = edl
    st.commit("test_setup", {}, "seed")

    for cid in ("sk1", "tx1"):
        dispatch(st, "add_keyframe", {"clip_id": cid, "props": KF5, "time": 0.0})
        dispatch(st, "set_clip_transform", {"clip_id": cid, "opacity": 0.25, "time": 2.0})
        opa = st.edl.get_clip(cid)[1].transform.opacity
        assert not isinstance(opa, float), f"{cid}: animation replaced by a scalar"
        assert len(_keys(opa)) == 2


def test_summary_reports_values_not_a_keyframe_repr(store):
    """The ops log and Claude's tool result both read this string."""
    dispatch(store, "add_keyframe", {"clip_id": "c1", "props": KF5, "time": 0.0})
    res = dispatch(store, "set_clip_transform",
                   {"clip_id": "c1", "scale": 2.0, "time": 3.0})
    s = res["summary"]
    assert "Keyframe(" not in s and "keyframes=" not in s
    assert "scale=2" in s
    assert "keyed @ 3.00s" in s
