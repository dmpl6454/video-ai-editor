"""set_clip_timing enforces a positive window and rejects media clips.

This is the invariant the timeline's kind-aware edge-drag (text/sticker →
set_clip_timing) now relies on: a left-edge (start) drag past the end, or a
right-edge (end) drag before the start, must never produce end <= start.
"""
from __future__ import annotations
import tempfile
from pathlib import Path

import pytest

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Canvas, Track, TextClip
from video_ai_editor.agent.dispatch import dispatch


def _store_with_text() -> EDLStore:
    tmp = tempfile.mkdtemp()
    edl = EDL(
        canvas=Canvas(w=1080, h=1920, fps=30),
        tracks=[Track(id="tx_super", type="text",
                      clips=[TextClip(id="t1", text="hi", start=5.0, end=8.0)])],
    )
    edl.recompute_duration()
    (Path(tmp) / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(Path(tmp))


def test_left_edge_past_end_clamps_to_positive_span():
    store = _store_with_text()
    dispatch(store, "set_clip_timing", {"clip_id": "t1", "start": 100.0})
    _, c = store.edl.get_clip("t1")
    assert c.end > c.start


def test_end_before_start_clamps_to_positive_span():
    store = _store_with_text()
    dispatch(store, "set_clip_timing", {"clip_id": "t1", "end": 1.0})
    _, c = store.edl.get_clip("t1")
    assert c.end > c.start


def test_start_clamped_non_negative():
    store = _store_with_text()
    dispatch(store, "set_clip_timing", {"clip_id": "t1", "start": -10.0})
    _, c = store.edl.get_clip("t1")
    assert c.start >= 0.0
