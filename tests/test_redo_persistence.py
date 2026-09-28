"""edl/snapshot.py — the redo stack survives an EDLStore re-creation
(process restart / LRU eviction from main.py's _STORES cache), issues 12/13.

Before this fix, `EDLStore._redo_stack` was a plain in-process Python list,
never written to disk — a session's edl.json/ops.json persisted fine, but
"what Redo would bring back" silently emptied whenever the store object was
recreated (a real restart, or eviction from the 64-entry LRU cache), with no
error or explanation. Undo/redo felt "inconsistent" because whether Redo
worked depended on whether the SAME process/cache-entry was still alive, not
on the session's actual edit history.
"""
from __future__ import annotations
import tempfile
from pathlib import Path

from video_ai_editor.edl import EDLStore
from video_ai_editor.agent.dispatch import dispatch


def _store_with_one_clip(tmp: Path) -> EDLStore:
    store = EDLStore(tmp)
    dispatch(store, "add_clip", {
        "track": "v1", "src": str(tmp / "nonexistent" / "x.mp4"),
        "in": 0.0, "out": 10.0, "start": 0.0,
    })
    return store


def test_redo_survives_a_fresh_edlstore_instance(tmp_path: Path):
    store = _store_with_one_clip(tmp_path)
    cid = store.edl.tracks[0].clips[0].id
    dispatch(store, "ripple_delete", {"clip_id": cid})
    assert len(store.edl.tracks[0].clips) == 0

    assert store.undo()
    assert len(store.edl.tracks[0].clips) == 1
    assert store.redo_available

    # Simulate the store being evicted from main.py's _STORES LRU cache and
    # recreated fresh for the same session_dir — this is exactly what used
    # to silently drop the redo stack.
    fresh = EDLStore(tmp_path)
    assert fresh.redo_available, "redo stack must survive a fresh EDLStore instance"

    assert fresh.redo()
    assert len(fresh.edl.tracks[0].clips) == 0


def test_redo_stack_file_is_removed_once_empty(tmp_path: Path):
    store = _store_with_one_clip(tmp_path)
    cid = store.edl.tracks[0].clips[0].id
    dispatch(store, "ripple_delete", {"clip_id": cid})
    store.undo()
    assert store.redo_stack_path.exists()

    store.redo()
    assert not store.redo_available
    assert not store.redo_stack_path.exists(), "no stale redo_stack.json once the stack is empty"


def test_a_commit_after_undo_clears_the_persisted_redo_stack(tmp_path: Path):
    """Making a new edit after an undo must invalidate redo — same as the
    existing in-memory behavior, but now also on disk."""
    store = _store_with_one_clip(tmp_path)
    cid = store.edl.tracks[0].clips[0].id
    dispatch(store, "ripple_delete", {"clip_id": cid})
    store.undo()
    assert store.redo_available

    dispatch(store, "add_clip", {
        "track": "v1", "src": str(tmp_path / "nonexistent" / "y.mp4"),
        "in": 0.0, "out": 5.0, "start": 20.0,
    })
    assert not store.redo_available
    assert not store.redo_stack_path.exists()

    fresh = EDLStore(tmp_path)
    assert not fresh.redo_available


def test_redo_available_false_on_a_fresh_session_with_no_history(tmp_path: Path):
    store = EDLStore(tmp_path)
    assert not store.redo_available


def test_multiple_undos_persist_a_multi_entry_redo_stack(tmp_path: Path):
    store = _store_with_one_clip(tmp_path)
    cid1 = store.edl.tracks[0].clips[0].id
    dispatch(store, "add_clip", {
        "track": "v1", "src": str(tmp_path / "nonexistent" / "y.mp4"),
        "in": 0.0, "out": 5.0, "start": 20.0,
    })
    dispatch(store, "ripple_delete", {"clip_id": cid1})
    assert len(store.edl.tracks[0].clips) == 1

    assert store.undo()  # undoes the ripple_delete
    assert store.undo()  # undoes the second add_clip
    assert len(store.edl.tracks[0].clips) == 1

    fresh = EDLStore(tmp_path)
    assert fresh.redo_available
    assert fresh.redo()
    assert fresh.redo()
    assert len(fresh.edl.tracks[0].clips) == 1


def test_redo_brings_back_the_operation_name_not_a_generic_redo(tmp_path: Path):
    """Final QA: History showed "Redo — Redo" three times after ⌘Z ×3, ⌘⇧Z ×3,
    and the undone ops' names were gone for good — undo popped the op without
    keeping it and redo appended a generic {tool:'redo', summary:'Redo'}."""
    store = _store_with_one_clip(tmp_path)
    cid = store.edl.tracks[0].clips[0].id
    dispatch(store, "trim_clip", {"clip_id": cid, "in": 0.0, "out": 8.0})
    dispatch(store, "split_at", {"track": "v1", "time": 4.0})
    before = [(o.tool, o.summary, o.args, o.edl_hash_after) for o in store.ops.ops]
    for _ in range(2):
        assert store.undo()
    # Survives a fresh store (the redo file carries the ops too).
    fresh = EDLStore(tmp_path)
    for _ in range(2):
        assert fresh.redo()
    after = [(o.tool, o.summary, o.args, o.edl_hash_after) for o in fresh.ops.ops]
    assert after == before
    redone = fresh.ops.ops[-2:]
    assert all(o.redo for o in redone)
    # edl_hash_before is the real previous state, not ''.
    assert redone[0].edl_hash_before == fresh.ops.ops[-3].edl_hash_after
    assert redone[1].edl_hash_before == redone[0].edl_hash_after


def test_an_old_redo_stack_without_ops_still_redoes(tmp_path: Path):
    import json
    store = _store_with_one_clip(tmp_path)
    cid = store.edl.tracks[0].clips[0].id
    dispatch(store, "ripple_delete", {"clip_id": cid})
    store.undo()
    # A redo file written by 0.7.x: EDLs only, no ops file beside it.
    (tmp_path / "redo_ops.json").unlink(missing_ok=True)
    raw = json.loads(store.redo_stack_path.read_text())
    assert isinstance(raw, list) and "tracks" in raw[0]
    fresh = EDLStore(tmp_path)
    assert fresh.redo()
    assert fresh.ops.ops[-1].tool == "redo"
