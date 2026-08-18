"""A copied clip must share NOTHING MUTABLE with the clip it came from.

`model_copy(update=...)` is SHALLOW in Pydantic v2, so every clip-duplicating
tool used to hand both pieces the same `Transform`, the same `effects` list and
the same `ClipAudio`. Editing one edited both, with no error anywhere.

It was reported as a keyframe bug — three symptoms that are all this one cause:

  1. "applied on the second split, it didn't show up in the video layer, yet
     the keyframe was marked"
  2. "applied on the first half, the keyframe was marked at the starting of the
     video AND at the end of the first half"   <- ONE key at t=0, drawn on both
     halves at `c.start + 0` = 0.0s and 4.0s
  3. "applied the keyframe on the second half ... also appeared on first half at
     the same place"

Keyframe times are CLIP-LOCAL, so an aliased transform draws the same key at a
different absolute position on each half — which is why it looked like the
keyframe had moved rather than been duplicated.

The damage was never limited to keyframes: scale, effects and volume aliased
too. And it persists, because the aliased object is serialised once per clip —
both clips genuinely carry the duplicated state on disk.
"""
from __future__ import annotations
import ast
from pathlib import Path

import pytest

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Clip
from video_ai_editor.agent.dispatch import dispatch

KF_PROPS = ["scale", "rotation", "opacity", "x", "y"]
KF_VALUES = {"scale": 1.0, "rotation": 0.0, "opacity": 1.0, "x": 0.0, "y": 0.0}


@pytest.fixture
def store(tmp_path):
    s = EDLStore(tmp_path)
    v1 = next(t for t in s.edl.tracks if t.id == "v1")
    v1.clips.append(Clip(id="c1", src="x.mp4", **{"in": 0.0}, out=8.0, start=0.0))
    s.commit("seed", {}, "seed")
    return s


def _v1(store) -> list:
    return next(t for t in store.edl.tracks if t.id == "v1").clips


@pytest.mark.parametrize("tool, args", [
    ("split_at", {"time": 4.0, "track": "v1"}),
    ("duplicate_clip", {"clip_id": "c1"}),
    ("cut_range", {"start": 2.0, "end": 3.0, "track": "v1"}),
])
def test_copies_share_no_mutable_state(store, tool, args):
    dispatch(store, tool, args)
    clips = _v1(store)
    assert len(clips) >= 2, f"{tool} did not produce two clips"
    a, b = clips[0], clips[1]
    assert a.transform is not b.transform, "aliased Transform"
    assert a.effects is not b.effects, "aliased effects list"
    assert a.audio is not b.audio, "aliased ClipAudio"


@pytest.mark.parametrize("tool, args", [
    ("split_at", {"time": 4.0, "track": "v1"}),
    ("duplicate_clip", {"clip_id": "c1"}),
])
def test_editing_one_copy_leaves_the_other_alone(store, tool, args):
    """Identity checks alone would pass a copy that aliases something deeper.
    These are the edits a user actually makes."""
    dispatch(store, tool, args)
    target = _v1(store)[1]

    dispatch(store, "set_clip_transform", {"clip_id": target.id, "scale": 1.75})
    dispatch(store, "add_effect", {"clip_id": target.id, "type": "vignette"})

    a, b = _v1(store)[0], _v1(store)[1]
    assert a.transform.scale == 1.0, "scaling one clip scaled the other"
    assert b.transform.scale == 1.75
    assert [e.type for e in a.effects] == [], "an effect leaked onto the other clip"
    assert [e.type for e in b.effects] == ["vignette"]


def test_the_reported_keyframe_scenario(store):
    """Split at 4s, key the SECOND half 0.67s in. The key belongs to that half
    and to nothing else."""
    dispatch(store, "split_at", {"time": 4.0, "track": "v1"})
    left, right = _v1(store)

    dispatch(store, "add_keyframe", {"clip_id": right.id, "props": KF_PROPS,
                                     "time": 0.67, "values": KF_VALUES})

    left, right = _v1(store)
    assert not hasattr(left.transform.scale, "keyframes"), \
        "the keyframe appeared on the first half too"
    assert right.transform.scale.keyframes == [(0.67, 1.0)]
    # Times are clip-local; the timeline draws at `c.start + t`.
    assert right.start + right.transform.scale.keyframes[0][0] == pytest.approx(4.67)


def test_keying_the_first_half_marks_only_the_first_half(store):
    """Symptom 2: one key at t=0 showed up at BOTH 0.0s and 4.0s, which read as
    "the start of the video and the end of the first half"."""
    dispatch(store, "split_at", {"time": 4.0, "track": "v1"})
    left, _ = _v1(store)

    dispatch(store, "add_keyframe", {"clip_id": left.id, "props": KF_PROPS,
                                     "time": 0.0, "values": KF_VALUES})

    left, right = _v1(store)
    assert left.transform.scale.keyframes == [(0.0, 1.0)]
    assert not hasattr(right.transform.scale, "keyframes"), \
        "the second half picked up the first half's keyframe"


def test_a_split_half_can_be_keyframed_independently_of_its_sibling(store):
    """Both halves animated, differently — the thing the aliasing made
    impossible."""
    dispatch(store, "split_at", {"time": 4.0, "track": "v1"})
    left, right = _v1(store)
    dispatch(store, "add_keyframe", {"clip_id": left.id, "props": KF_PROPS,
                                     "time": 1.0, "values": {**KF_VALUES, "scale": 2.0}})
    dispatch(store, "add_keyframe", {"clip_id": right.id, "props": KF_PROPS,
                                     "time": 2.0, "values": {**KF_VALUES, "scale": 3.0}})
    left, right = _v1(store)
    assert left.transform.scale.keyframes == [(1.0, 2.0)]
    assert right.transform.scale.keyframes == [(2.0, 3.0)]


def test_the_copy_survives_a_reload(store):
    """The aliased object was serialised once per clip, so the duplication was
    written to disk, not just held in memory. Prove the fix reaches the file."""
    dispatch(store, "split_at", {"time": 4.0, "track": "v1"})
    right = _v1(store)[1]
    dispatch(store, "add_keyframe", {"clip_id": right.id, "props": KF_PROPS,
                                     "time": 0.5, "values": KF_VALUES})

    reloaded = EDLStore(store.dir)
    left2, right2 = next(t for t in reloaded.edl.tracks if t.id == "v1").clips
    assert not hasattr(left2.transform.scale, "keyframes")
    assert right2.transform.scale.keyframes == [(0.5, 1.0)]


def test_no_bare_shallow_model_copy_survives_in_dispatch():
    """The fix is one helper, and its whole value is that every duplicating
    tool goes through it. A new tool written with a bare
    `c.model_copy(update=...)` reintroduces the bug in a form that looks
    completely ordinary — and nothing else in the codebase would complain."""
    # importlib, not `from ... import dispatch`: agent/__init__.py re-exports
    # the FUNCTION under the module's own name, so the plain import gives a
    # function with no __file__.
    import importlib
    mod = importlib.import_module("video_ai_editor.agent.dispatch")
    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "model_copy"):
            deep = next((k for k in node.keywords if k.arg == "deep"), None)
            if deep is None or not (isinstance(deep.value, ast.Constant)
                                    and deep.value.value is True):
                offenders.append(node.lineno)
    assert not offenders, (
        f"shallow model_copy at dispatch.py line(s) {offenders} — use "
        f"_clone_clip(), or the copy will share its Transform/effects/audio "
        f"with the original")
