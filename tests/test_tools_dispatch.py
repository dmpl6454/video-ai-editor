"""Tools dispatch — verify each mutation produces the expected EDL diff + ops entry."""
import tempfile
from pathlib import Path
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Clip
from video_ai_editor.agent.dispatch import dispatch


def _store_with_one_clip() -> EDLStore:
    tmp = tempfile.mkdtemp()
    store = EDLStore(Path(tmp))
    dispatch(store, "add_clip", {
        "track": "v1", "src": str(Path(tmp) / "nonexistent" / "x.mp4"),
        "in": 0.0, "out": 10.0, "start": 0.0,
    })
    return store


def test_add_clip_creates_clip_and_op():
    store = _store_with_one_clip()
    assert len(store.edl.tracks[0].clips) == 1
    assert store.ops.last().tool == "add_clip"


def test_cut_range_removes_inner_segment():
    store = _store_with_one_clip()
    # cut 3..6 from a single 10s clip → expect two clips: [0..3] and [6..10],
    # ripple-collapsed to [0..3] then [3..7] (start at 3 because we close the gap).
    dispatch(store, "cut_range", {"track": "v1", "start": 3.0, "end": 6.0})
    clips = store.edl.tracks[0].clips
    assert len(clips) == 2
    assert abs(clips[0].duration - 3.0) < 1e-6
    assert abs(clips[1].duration - 4.0) < 1e-6
    assert abs(clips[1].start - 3.0) < 1e-6  # ripple closed the 3s gap


def test_split_at_produces_two_clips():
    store = _store_with_one_clip()
    dispatch(store, "split_at", {"track": "v1", "time": 4.0})
    clips = store.edl.tracks[0].clips
    assert len(clips) == 2
    assert abs(clips[0].duration - 4.0) < 1e-6
    assert abs(clips[1].duration - 6.0) < 1e-6


def test_ripple_delete_then_undo():
    store = _store_with_one_clip()
    cid = store.edl.tracks[0].clips[0].id
    dispatch(store, "ripple_delete", {"clip_id": cid})
    assert len(store.edl.tracks[0].clips) == 0
    dispatch(store, "undo", {})
    assert len(store.edl.tracks[0].clips) == 1


def test_set_aspect_ratio_changes_canvas():
    store = _store_with_one_clip()
    dispatch(store, "set_aspect_ratio", {"ratio": "16:9"})
    assert store.edl.canvas.w == 1920
    assert store.edl.canvas.h == 1080
