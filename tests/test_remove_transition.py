"""remove_transition — the missing half of transition editing.

add_transition APPENDS unconditionally (the compositor's last-match-wins
boundary matcher makes re-adding the de-facto update), so removal must
clear EVERY entry near the cut, not just the newest.
"""
from __future__ import annotations
from pathlib import Path

import pytest

from video_ai_editor.edl import EDLStore
from video_ai_editor.agent.dispatch import dispatch


@pytest.fixture
def store(tmp_path: Path) -> EDLStore:
    return EDLStore(tmp_path)


def _transitions(store: EDLStore):
    return store.edl.get_track("v1").transitions


def test_remove_transition_clears_all_entries_at_cut(store: EDLStore):
    dispatch(store, "add_transition", {"at": 2.0, "type": "fade"})
    dispatch(store, "add_transition", {"at": 2.0, "type": "dissolve"})
    dispatch(store, "add_transition", {"at": 5.0, "type": "fade"})

    out = dispatch(store, "remove_transition", {"at": 2.0})

    assert out["removed"] == 2
    remaining = _transitions(store)
    assert len(remaining) == 1 and abs(remaining[0].at - 5.0) < 1e-6


def test_remove_transition_tolerance_matches_compositor(store: EDLStore):
    dispatch(store, "add_transition", {"at": 2.0, "type": "fade"})
    out = dispatch(store, "remove_transition", {"at": 2.04})
    assert out["removed"] == 1 and _transitions(store) == []


def test_remove_transition_all(store: EDLStore):
    dispatch(store, "add_transition", {"at": 1.0, "type": "fade"})
    dispatch(store, "add_transition", {"at": 3.0, "type": "fade"})
    out = dispatch(store, "remove_transition", {"all": True})
    assert out["removed"] == 2 and _transitions(store) == []


def test_remove_transition_no_match_is_benign(store: EDLStore):
    out = dispatch(store, "remove_transition", {"at": 9.0})
    assert out["removed"] == 0


def test_remove_transition_requires_target(store: EDLStore):
    with pytest.raises(ValueError):
        dispatch(store, "remove_transition", {})
