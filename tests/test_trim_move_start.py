"""trim_clip(move_start=True) — a HEAD trim on a non-magnetic lane keeps the
kept frames where they play (QA-115 trim-to-playhead, timeline left-edge drag).

Without it a left trim of a PiP/music clip kept `start`, so the edge snapped
back and everything after the new in-point played earlier by the trimmed length.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Canvas, Clip, Track


def _store(lane: list[tuple[float, float, float]], *, speed: float = 1.0) -> EDLStore:
    """v1 0-40 s plus a v2 PiP lane of (start, in, out) clips."""
    tmp = tempfile.mkdtemp()
    src = str(Path(tmp) / "missing" / "x.mp4")   # never probed: no source clamp
    clips = []
    for i, (s, a, b) in enumerate(lane):
        c = Clip(id=f"p{i}", src=src, in_=a, out=b, start=s)
        if speed != 1.0:
            c.speed = speed
        clips.append(c)
    edl = EDL(canvas=Canvas(w=1920, h=1080, fps=30), tracks=[
        Track(id="v1", type="video", clips=[Clip(id="m0", src=src, in_=0.0, out=40.0, start=0.0)]),
        Track(id="v2", type="video", z=5, clips=clips),
    ])
    edl.recompute_duration()
    (Path(tmp) / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(Path(tmp))


def _clip(store, cid):
    return store.edl.get_clip(cid)[1]


def test_head_trim_moves_start_with_the_head():
    store = _store([(10.0, 0.0, 6.0)])
    dispatch(store, "trim_clip", {"clip_id": "p0", "in": 2.0, "move_start": True})
    c = _clip(store, "p0")
    assert (c.in_, c.out) == pytest.approx((2.0, 6.0))
    # Source second 2 played at 12 s before the trim, and still does.
    assert c.start == pytest.approx(12.0)


def test_head_trim_on_a_fast_clip_moves_by_timeline_seconds():
    store = _store([(10.0, 0.0, 8.0)], speed=2.0)
    dispatch(store, "trim_clip", {"clip_id": "p0", "in": 4.0, "move_start": True})
    assert _clip(store, "p0").start == pytest.approx(12.0)   # 4 source s at 2x = 2 s


def test_extending_the_head_stops_at_the_previous_clip():
    store = _store([(0.0, 0.0, 5.0), (8.0, 4.0, 10.0)])
    dispatch(store, "trim_clip", {"clip_id": "p1", "in": 0.0, "move_start": True})
    c = _clip(store, "p1")
    assert c.start == pytest.approx(5.0), "must not overlap p0 (ends at 5 s)"
    assert c.in_ == pytest.approx(1.0)                       # only 3 s could be revealed
    assert _clip(store, "p0").start == pytest.approx(0.0)


def test_without_the_flag_trim_keeps_its_documented_contract():
    store = _store([(10.0, 0.0, 6.0)])
    dispatch(store, "trim_clip", {"clip_id": "p0", "in": 2.0})
    assert _clip(store, "p0").start == pytest.approx(10.0)


def test_main_lane_ignores_the_flag_and_stays_magnetic():
    store = _store([])
    dispatch(store, "trim_clip", {"clip_id": "m0", "in": 5.0, "move_start": True})
    c = _clip(store, "m0")
    assert c.start == pytest.approx(0.0) and c.in_ == pytest.approx(5.0)
