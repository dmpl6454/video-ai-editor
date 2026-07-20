"""Claude co-editor grounding: the live-context block must let Claude bind
"this clip" (UI selection), "here" (playhead), and "the second clip"
(ordinal enumeration) to real clip ids — the difference between guessing
and editing what the user is pointing at.
"""
from __future__ import annotations
from pathlib import Path

import pytest

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Canvas, Clip, Track
from video_ai_editor.agent.loop import _live_context_block


@pytest.fixture
def store(tmp_path: Path) -> EDLStore:
    s = EDLStore(tmp_path)
    v1 = s.edl.get_track("v1")
    v1.clips = [
        Clip(src=str(tmp_path / "intro.mp4"), in_=0, out=3.5, start=0, id="c_one"),
        Clip(src=str(tmp_path / "broll.mp4"), in_=0, out=2.5, start=3.5, id="c_two"),
    ]
    s.edl.recompute_duration()
    return s


def test_context_block_enumerates_clips_with_ordinals(store: EDLStore):
    block = _live_context_block(store)
    assert "c_one" in block and "c_two" in block
    assert "[1]" in block and "[2]" in block
    assert "intro" in block  # a human-recognizable name, not just ids


def test_context_block_reports_selection_and_playhead(store: EDLStore):
    block = _live_context_block(store, ui_state={
        "selection": "c_two", "multi_selection": [], "playhead": 1.0,
    })
    assert "c_two" in block
    assert "selected" in block.lower()
    assert "playhead" in block.lower()
    # playhead 1.0 falls inside clip 1 — the block should resolve "here"
    assert "c_one" in block


def test_context_block_without_ui_state_has_no_selection_line(store: EDLStore):
    block = _live_context_block(store)
    assert "selected" not in block.lower()


def test_chat_request_accepts_ui_state_fields():
    from video_ai_editor.main import ChatRequest
    req = ChatRequest(message="speed this up", selection="c_two",
                      multi_selection=["c_two"], playhead=4.2)
    assert req.selection == "c_two" and req.playhead == 4.2
    # Old callers sending only message must keep working
    assert ChatRequest(message="hi").selection is None
