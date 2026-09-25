"""QA-013: only the MAIN lane (v1) is magnetic.

`_ripple_close_gap` used to repack EVERY lane it was handed from t=0, so one
trim / ripple_delete / duplicate / bulk op on a PiP, music or voiceover lane
collapsed every hand-placed clip on that lane to the start of the timeline
(v2 [8.0, 25.0] -> [0.0, 6.005]). MediaBin's "Remove music" sends
`ripple_delete`, so removing one music bed dragged the next one to 0.

Each test places clips at deliberate absolute starts on a non-v1 lane, runs
the real dispatch op, and asserts that only the edited clip changed. One v1
test pins that the main lane is still magnetic.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Canvas, Clip, Track


def _store(lane_id: str, lane_type: str, starts: list[float], dur: float = 6.0,
           v1: list[tuple[float, float]] | None = None) -> EDLStore:
    tmp = tempfile.mkdtemp()
    src = str(Path(tmp) / "missing" / "x.mp4")  # never probed successfully
    v1_clips = [Clip(id=f"m{i}", src=src, in_=0.0, out=d, start=s)
                for i, (s, d) in enumerate(v1 or [(0.0, 40.0)])]
    lane = [Clip(id=f"p{i}", src=src, in_=0.0, out=dur, start=s)
            for i, s in enumerate(starts)]
    edl = EDL(canvas=Canvas(w=1920, h=1080, fps=30), tracks=[
        Track(id="v1", type="video", clips=v1_clips),
        Track(id=lane_id, type=lane_type, z=5, clips=lane),
    ])
    edl.recompute_duration()
    (Path(tmp) / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(Path(tmp))


def _starts(store: EDLStore, lane_id: str) -> dict[str, float]:
    return {c.id: round(c.start, 6) for c in store.edl.get_track(lane_id).clips}


LANES = [("v2", "video"), ("music", "music"), ("vo", "vo"), ("a1", "audio")]


@pytest.mark.parametrize("lane_id,lane_type", LANES)
def test_trim_on_a_non_main_lane_moves_nothing(lane_id, lane_type):
    store = _store(lane_id, lane_type, [8.0, 25.0])
    dispatch(store, "trim_clip", {"clip_id": "p0", "in": 0.0, "out": 2.0})
    assert _starts(store, lane_id) == {"p0": 8.0, "p1": 25.0}
    assert store.edl.get_clip("p0")[1].out == pytest.approx(2.0)


@pytest.mark.parametrize("lane_id,lane_type", LANES)
def test_ripple_delete_on_a_non_main_lane_is_a_plain_delete(lane_id, lane_type):
    # This is exactly what MediaBin's "Remove music" button sends.
    store = _store(lane_id, lane_type, [2.0, 20.0])
    dispatch(store, "ripple_delete", {"clip_id": "p0"})
    assert _starts(store, lane_id) == {"p1": 20.0}


def test_duplicate_on_a_pip_lane_keeps_neighbours_and_lands_in_free_space():
    store = _store("v2", "video", [8.0, 25.0], dur=6.0)
    out = dispatch(store, "duplicate_clip", {"clip_id": "p0"})
    starts = _starts(store, "v2")
    assert starts["p0"] == 8.0 and starts["p1"] == 25.0
    # Right after the original's footprint, where there is room.
    assert starts[out["new_clip_id"]] == pytest.approx(14.0)
    # ...and the lane stays in timeline order, the invariant every other op
    # (and the old repack) keeps: the copy sits BETWEEN p0 and p1 in the list.
    assert [c.id for c in store.edl.get_track("v2").clips] == ["p0", out["new_clip_id"], "p1"]


def test_bulk_duplicate_on_a_pip_lane_keeps_the_lane_in_timeline_order():
    store = _store("v2", "video", [8.0, 25.0], dur=6.0)
    out = dispatch(store, "bulk_duplicate", {"clip_ids": ["p0"]})
    (new_id,) = out["new_ids"]
    assert [c.id for c in store.edl.get_track("v2").clips] == ["p0", new_id, "p1"]


def test_duplicate_on_a_pip_lane_never_overlaps_the_next_clip():
    # p0 [8,14) and p1 [16,22): a copy of p0 does not fit at 14 (needs 6s),
    # so it goes after p1 instead of overprinting it — and p1 does not move.
    store = _store("v2", "video", [8.0, 16.0], dur=6.0)
    out = dispatch(store, "duplicate_clip", {"clip_id": "p0"})
    starts = _starts(store, "v2")
    assert starts["p0"] == 8.0 and starts["p1"] == 16.0
    assert starts[out["new_clip_id"]] == pytest.approx(22.0)


def test_bulk_delete_and_bulk_duplicate_leave_other_lane_clips_in_place():
    store = _store("v2", "video", [8.0, 25.0, 40.0])
    dispatch(store, "bulk_delete", {"clip_ids": ["p1"]})
    assert _starts(store, "v2") == {"p0": 8.0, "p2": 40.0}
    res = dispatch(store, "bulk_duplicate", {"clip_ids": ["p0"]})
    starts = _starts(store, "v2")
    assert starts["p0"] == 8.0 and starts["p2"] == 40.0
    assert starts[res["new_ids"][0]] == pytest.approx(14.0)


def test_cut_range_on_a_non_main_lane_closes_only_the_cut():
    # p0 [2,8), p1 [20,26). Cutting [4,6) on v2 shortens p0 by 2s and slides
    # what is AFTER the cut left by 2s; the clip before is untouched and the
    # lane is NOT repacked from zero.
    store = _store("v2", "video", [2.0, 20.0])
    dispatch(store, "cut_range", {"track": "v2", "start": 4.0, "end": 6.0})
    clips = sorted(store.edl.get_track("v2").clips, key=lambda c: c.start)
    spans = [(round(c.start, 6), round(c.start + c.effective_duration, 6)) for c in clips]
    assert spans == [(2.0, 4.0), (4.0, 6.0), (18.0, 24.0)]


def test_reorder_on_a_non_main_lane_keeps_the_lane_offset():
    store = _store("v2", "video", [8.0, 25.0], dur=6.0)
    dispatch(store, "reorder_clips", {"track": "v2", "order": ["p1", "p0"]})
    # Reordered, packed from where the lane began — never from t=0.
    assert _starts(store, "v2") == {"p1": 8.0, "p0": 14.0}


def test_main_lane_is_still_magnetic():
    store = _store("v2", "video", [8.0], v1=[(0.0, 10.0), (10.0, 10.0)])
    dispatch(store, "ripple_delete", {"clip_id": "m0"})
    assert _starts(store, "v1") == {"m1": 0.0}
    # ...and a v1 ripple still does not repack the PiP lane.
    assert _starts(store, "v2") == {"p0": 8.0}
