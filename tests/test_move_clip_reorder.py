"""move_clip's `close_gap` — which lane closes, and whether a drag can reorder.

Both defects here are drag-gesture bugs that never showed up in the tool-level
tests, because both are about the RELATIONSHIP between the moved clip and the
lane it left:

  * the hole was closed on the DESTINATION track (`track` is already the
    destination by the time the close_gap block runs), so a cross-lane drag
    left the real hole open on the origin and shuffled clips on a lane the
    user never touched;
  * `_first_free_gap` snapped a leftward reorder straight back past every clip
    it was trying to jump, so dragging the last clip to the front was a total
    no-op — while the UI announced "Snapped to the nearest free gap".

No media is synthesized: none of these paths render, and the videoless-lane
guard fails open on an unprobeable path (see _reject_videoless_on_video_lane).
"""
from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl.schema import Clip, TextClip
from video_ai_editor.edl.snapshot import EDLStore


def _store(tmp_path: Path, *, v1=((0.0, 2.0), (2.0, 2.0), (4.0, 2.0))) -> EDLStore:
    """A store whose v1 holds `len(v1)` back-to-back clips, ids c0, c1, c2…"""
    s = EDLStore(tmp_path)
    t = s.edl.get_track("v1")
    for i, (start, dur) in enumerate(v1):
        t.clips.append(Clip(id=f"c{i}", src=f"/x/{i}.mp4", in_=0.0, out=dur, start=start))
    s.commit("seed", {}, "seed")
    return s


def _starts(store: EDLStore, track: str) -> list[tuple[str, float]]:
    return [(c.id, round(float(c.start), 3)) for c in store.edl.get_track(track).clips]


# --------------------------------------------------- a drag can move a clip LEFT

def test_dragging_the_last_clip_to_the_front_actually_reorders(tmp_path):
    """The reported no-op. c2 dropped at 0 must land FIRST.

    Pre-fix: _first_free_gap saw 0-2 and 2-4 occupied, walked forward to 4.0,
    and put c2 back exactly where it started; the repack then re-derived the
    original order. The clip did not move, and the UI still claimed it had
    been snapped to a free gap.
    """
    s = _store(tmp_path)
    dispatch(s, "move_clip", {"clip_id": "c2", "new_start": 0.0, "close_gap": True})
    assert _starts(s, "v1") == [("c2", 0.0), ("c0", 2.0), ("c1", 4.0)]


def test_a_reorder_inserts_between_neighbours(tmp_path):
    """Dropping onto an occupied slot is HOW you reorder — the drop start
    decides the order, and the repack closes up around it."""
    s = _store(tmp_path)
    dispatch(s, "move_clip", {"clip_id": "c2", "new_start": 2.0, "close_gap": True})
    assert _starts(s, "v1") == [("c0", 0.0), ("c2", 2.0), ("c1", 4.0)]


def test_dragging_the_first_clip_to_the_end_still_works(tmp_path):
    """The case close_gap was originally added for must not regress."""
    s = _store(tmp_path)
    before = s.edl.duration
    dispatch(s, "move_clip", {"clip_id": "c0", "new_start": 99.0, "close_gap": True})
    assert _starts(s, "v1") == [("c1", 0.0), ("c2", 2.0), ("c0", 4.0)]
    assert s.edl.duration == pytest.approx(before), "the timeline must not grow"


def test_a_reorder_leaves_no_hole_and_no_overlap(tmp_path):
    s = _store(tmp_path, v1=((0.0, 3.0), (3.0, 1.0), (4.0, 5.0)))
    dispatch(s, "move_clip", {"clip_id": "c1", "new_start": 0.0, "close_gap": True})
    assert _starts(s, "v1") == [("c1", 0.0), ("c0", 1.0), ("c2", 4.0)]
    assert s.edl.duration == pytest.approx(9.0)


def test_without_close_gap_a_leftward_move_still_snaps(tmp_path):
    """The gap snap is NOT gone — it is the correct answer for an absolute
    placement. apply_template / b-roll insertion / MCP callers name a time and
    must never have neighbours shuffle underneath them, so an occupied slot
    still pushes the clip to the first free one.
    """
    s = _store(tmp_path)
    dispatch(s, "move_clip", {"clip_id": "c2", "new_start": 0.0})
    assert _starts(s, "v1") == [("c0", 0.0), ("c1", 2.0), ("c2", 4.0)]


# ------------------------------------------------- the hole is on the ORIGIN lane

def test_cross_lane_drag_closes_the_hole_on_the_lane_it_left(tmp_path):
    """v1 → v2. Pre-fix the gap-close ran on v2 (already reassigned into
    `track`), so v1 kept a 2s hole in the middle and stayed 6s long."""
    s = _store(tmp_path)
    dispatch(s, "move_clip", {"clip_id": "c1", "new_track": "v2",
                              "new_start": 10.0, "close_gap": True})
    assert _starts(s, "v1") == [("c0", 0.0), ("c2", 2.0)], "hole closed on v1"
    assert _starts(s, "v2") == [("c1", 10.0)], "dropped where it was dropped"


def test_cross_lane_drop_does_not_repack_the_destination(tmp_path):
    """A v2 clip is placed against v1's picture at an absolute time. Repacking
    the destination pulled deliberately-gapped PIPs to t=0 — the same damage
    test_set_speed_rejected_on_v2_pip pins down (8.0/20.0 -> 0.0/2.0)."""
    s = _store(tmp_path)
    v2 = s.edl.get_track("v2")
    v2.clips.append(Clip(id="p0", src="/x/p0.mp4", in_=0, out=4, start=8.0))
    v2.clips.append(Clip(id="p1", src="/x/p1.mp4", in_=0, out=4, start=20.0))
    s.commit("seed2", {}, "seed2")

    dispatch(s, "move_clip", {"clip_id": "c0", "new_track": "v2",
                              "new_start": 30.0, "close_gap": True})
    assert _starts(s, "v2") == [("p0", 8.0), ("p1", 20.0), ("c0", 30.0)]
    assert _starts(s, "v1") == [("c1", 0.0), ("c2", 2.0)], "v1's head hole closed"


def test_a_same_lane_v2_drag_never_repacks_the_pip_lane(tmp_path):
    """v1 is the SEQUENCE; v2 is not. The lane gate is the id, not the type —
    `type == 'video'` matched v2 too, so dragging one PIP collapsed every
    other PIP on the lane to the head."""
    s = _store(tmp_path)
    v2 = s.edl.get_track("v2")
    v2.clips.append(Clip(id="p0", src="/x/p0.mp4", in_=0, out=4, start=8.0))
    v2.clips.append(Clip(id="p1", src="/x/p1.mp4", in_=0, out=4, start=20.0))
    s.commit("seed2", {}, "seed2")

    dispatch(s, "move_clip", {"clip_id": "p1", "new_start": 30.0, "close_gap": True})
    assert _starts(s, "v2") == [("p0", 8.0), ("p1", 30.0)]


def test_cross_lane_drag_off_a_non_v1_lane_leaves_both_lanes_alone(tmp_path):
    """v2 → v1: nothing to close on v2 (its clips are not a sequence), and the
    destination keeps the drop position."""
    s = _store(tmp_path)
    v2 = s.edl.get_track("v2")
    v2.clips.append(Clip(id="p0", src="/x/p0.mp4", in_=0, out=4, start=8.0))
    v2.clips.append(Clip(id="p1", src="/x/p1.mp4", in_=0, out=4, start=20.0))
    s.commit("seed2", {}, "seed2")

    dispatch(s, "move_clip", {"clip_id": "p1", "new_track": "v1",
                              "new_start": 40.0, "close_gap": True})
    assert _starts(s, "v2") == [("p0", 8.0)]
    assert _starts(s, "v1") == [("c0", 0.0), ("c1", 2.0), ("c2", 4.0), ("p1", 40.0)]


# ------------------------------------------------------------ overlays are exempt

def test_close_gap_never_touches_an_overlay(tmp_path):
    """A text clip's start is a coordinate against the picture, not a slot —
    and its `end` is absolute, so the span must survive the move."""
    s = _store(tmp_path)
    tx = s.edl.get_track("tx_super")
    tx.clips.append(TextClip(id="t0", text="HELLO", start=2.0, end=5.0))
    s.commit("seed2", {}, "seed2")

    dispatch(s, "move_clip", {"clip_id": "t0", "new_start": 20.0, "close_gap": True})
    t = s.edl.get_clip("t0")[1]
    assert (t.start, t.end) == pytest.approx((20.0, 23.0))
    assert _starts(s, "v1") == [("c0", 0.0), ("c1", 2.0), ("c2", 4.0)], "v1 untouched"


def test_a_reorder_is_one_commit_and_undoes_whole(tmp_path):
    """The repack moves several clips, but it is a single op — Undo must not
    leave the lane half-reordered."""
    s = _store(tmp_path)
    dispatch(s, "move_clip", {"clip_id": "c2", "new_start": 0.0, "close_gap": True})
    assert _starts(s, "v1") == [("c2", 0.0), ("c0", 2.0), ("c1", 4.0)]
    assert s.undo() is True
    assert _starts(s, "v1") == [("c0", 0.0), ("c1", 2.0), ("c2", 4.0)]
