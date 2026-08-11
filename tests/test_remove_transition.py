"""remove_transition — the missing half of transition editing.

Removal must clear EVERY entry near the cut, not just the newest.

`add_transition` no longer creates duplicates (it replaces on the same cut, so
the EDL matches what renders — round 8), but stacks still reach us from older
projects, direct EDL edits and MCP callers, and the compositor's last-match-wins
matcher means the extras are invisible until you try to delete the transition
and one of them survives. So the sweep stays, and the test that covers it now
builds the stack directly instead of via two add calls.
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
    """A legacy stack: two transitions on one cut, as older projects hold them."""
    from video_ai_editor.edl.schema import Transition
    v1 = store.edl.get_track("v1")
    v1.transitions.extend([Transition(at=2.0, type="fade", duration=0.5),
                           Transition(at=2.0, type="dissolve", duration=0.5)])
    dispatch(store, "add_transition", {"at": 5.0, "type": "fade"})

    out = dispatch(store, "remove_transition", {"at": 2.0})

    assert out["removed"] == 2
    remaining = _transitions(store)
    assert len(remaining) == 1 and abs(remaining[0].at - 5.0) < 1e-6


def test_add_transition_no_longer_stacks_on_one_cut(store: EDLStore):
    """The other end of the same problem: adding twice used to leave two
    entries, only one of which rendered."""
    dispatch(store, "add_transition", {"at": 2.0, "type": "fade"})
    dispatch(store, "add_transition", {"at": 2.0, "type": "dissolve"})
    trs = _transitions(store)
    assert len(trs) == 1 and trs[0].type == "dissolve"


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
