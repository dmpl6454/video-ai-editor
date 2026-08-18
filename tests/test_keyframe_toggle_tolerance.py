"""Unmarking a keyframe from the panel — the tolerance the two sides must share.

Reported as "I was unable to unmark the keyframes(button), when I move the play
head on the marked keyframe."

The panel's ◆ button armed itself when the playhead was within HALF A FRAME
(0.017s) of a stored key, and `remove_keyframe` only matched within 1e-3. So
between 1ms and 17ms the button went solid, its tooltip read "Remove the keyframe
at the playhead", and the handler returned "no keyframe at 5.99s" and committed
nothing.

That band is where the playhead practically always is. It is a rAF wall clock, so
it lands on an arbitrary float and essentially never re-hits the exact time a key
was stored at — the reported screenshot shows 5.99s against a key at 6.00s.
Measured before the fix: removal worked at exactly 6.000 and failed at 5.999,
5.995, 5.99 and 5.985, with the button lit for every one.
"""
import pathlib
import tempfile

import pytest

from video_ai_editor.agent.dispatch import _kf_tol, dispatch
from video_ai_editor.edl.schema import Clip
from video_ai_editor.edl.snapshot import EDLStore

PROPS = ["scale", "rotation", "opacity", "x", "y"]


def _store(fps: int = 30) -> EDLStore:
    s = EDLStore(pathlib.Path(tempfile.mkdtemp()))
    s.edl.canvas.fps = fps
    s.edl.get_track("v1").clips.append(
        Clip(id="c1", src="/x/a.mp4", in_=0, out=8, start=0.0))
    s.commit("seed", {}, "seed")
    for t in (2.0, 3.98, 6.00):
        dispatch(s, "add_keyframe", {"clip_id": "c1", "props": PROPS, "time": t})
    return s


def _times(s: EDLStore) -> list[float]:
    _, c = s.edl.get_clip("c1")
    v = c.transform.scale
    return [round(k[0], 4) for k in getattr(v, "keyframes", [])]


@pytest.mark.parametrize("t", [6.000, 5.999, 5.995, 5.99, 5.985])
def test_a_playhead_within_half_a_frame_removes_the_key(t):
    """Every one of these lit the button; only the exact 6.000 used to work."""
    s = _store()
    dispatch(s, "remove_keyframe", {"clip_id": "c1", "props": PROPS, "time": t})
    assert 6.0 not in _times(s), f"key at 6.00 survived a remove at {t}"


@pytest.mark.parametrize("t", [5.97, 5.90, 6.05])
def test_a_playhead_further_away_removes_nothing(t):
    """The other half of the contract: the button is NOT armed out here, so a
    click must not reach past a frame and delete a key the user cannot see."""
    s = _store()
    res = dispatch(s, "remove_keyframe", {"clip_id": "c1", "props": PROPS, "time": t})
    assert _times(s) == [2.0, 3.98, 6.0]
    assert "no keyframe" in res["summary"]


def test_adding_inside_the_band_replaces_rather_than_stacks():
    """add and remove MUST share the tolerance. If add stayed strict, a click at
    5.99 would drop a second key 10ms from the 6.00 one, and the matching remove
    would then delete them in pairs — two keys destroyed by one click."""
    s = _store()
    dispatch(s, "add_keyframe", {"clip_id": "c1", "props": PROPS, "time": 5.99})
    assert _times(s) == [2.0, 3.98, 5.99], "expected an upsert, not a 4th key"


def test_the_tolerance_follows_fps_rather_than_a_fixed_17ms():
    """0.017 is half a frame only at 30fps. At 60 it is a WHOLE frame, which
    would arm the control a full frame early — the same mismatch reversed."""
    assert _kf_tol(_store(fps=30)) == pytest.approx(1 / 60, rel=1e-6)
    assert _kf_tol(_store(fps=60)) == pytest.approx(1 / 120, rel=1e-6)
    # Floored, so a nonsense fps can never make it stricter than the old 1e-3.
    assert _kf_tol(_store(fps=240)) >= 1e-3


def test_the_client_mirrors_the_same_rule():
    """Pinned by source: the two implementations only disagree at runtime, in a
    browser, as a button that lights up and does nothing."""
    ts = pathlib.Path("frontend/src/lib/overlay.ts").read_text(encoding="utf-8")
    assert "export function keyEps" in ts
    assert "0.5 / Math.max(1, fps || 30)" in ts, (
        "keyEps must be half a frame derived from fps, matching _kf_tol")
    props = pathlib.Path("frontend/src/components/Properties.tsx").read_text(encoding="utf-8")
    assert "keyEps(" in props and "0.017" not in props, (
        "the panel must not keep a hardcoded half-frame constant")


def test_removing_the_only_key_still_restores_the_value_it_held():
    """The widened tolerance must not disturb the round-5 rule that a lone key
    collapses to the value it HELD, not to 0.0 (which made the clip vanish).

    Set the scalar FIRST, then key it through the FIVE-prop form the ◆ button
    actually sends. Anchor seeding is `len(props) == 1`, so both the single-`prop`
    form and a one-element list would add an extra key at t=0 — the removal would
    then collapse onto the anchor and prove nothing about the value the removed
    key itself held.
    """
    s = EDLStore(pathlib.Path(tempfile.mkdtemp()))
    s.edl.get_track("v1").clips.append(
        Clip(id="c1", src="/x/a.mp4", in_=0, out=8, start=0.0))
    s.commit("seed", {}, "seed")
    dispatch(s, "set_clip_transform", {"clip_id": "c1", "scale": 1.75})
    dispatch(s, "add_keyframe", {"clip_id": "c1", "props": PROPS, "time": 3.0})
    _, c = s.edl.get_clip("c1")
    assert len(c.transform.scale.keyframes) == 1, c.transform.scale

    # Removed from INSIDE the tolerance band, not at the exact stored time —
    # the whole point is that this is the only way the UI ever asks.
    dispatch(s, "remove_keyframe", {"clip_id": "c1", "props": PROPS, "time": 2.995})
    _, c = s.edl.get_clip("c1")
    assert c.transform.scale == pytest.approx(1.75), c.transform.scale
