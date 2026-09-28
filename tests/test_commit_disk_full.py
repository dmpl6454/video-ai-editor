"""Final QA: when the disk is full a failed edit must leave NOTHING behind.

EDLStore.commit mutated nothing itself but wrote the snapshot and edl.json
after the handler had already changed `store.edl` in memory, and nothing put
the tree back when a write raised ENOSPC. So the server answered 422 "An
external tool failed: [Errno 28] No space left on device", yet /head reported
the failed edit's hash, /edl listed its text, preview and export included it,
and the next successful edit's single Undo removed BOTH (the phantom edit was
never in history). Now every write goes to a temp file first; a failure drops
the temps, takes the op back and restores the tree from the last good
edl.json. /dispatch maps ENOSPC to 507 disk_full.
"""
from __future__ import annotations

import errno
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore


def _texts(store: EDLStore) -> list[str]:
    return [c.text for t in store.edl.tracks for c in t.clips if hasattr(c, "text")]


def _full_disk(monkeypatch, after_calls: int = 0):
    """Path.write_text raises ENOSPC (after `after_calls` successful writes)."""
    real = Path.write_text
    calls = {"n": 0}

    def write_text(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] > after_calls:
            raise OSError(errno.ENOSPC, "No space left on device", str(self))
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", write_text)
    return lambda: monkeypatch.setattr(Path, "write_text", real)


@pytest.mark.parametrize("after_calls", [0, 1, 2])
def test_a_commit_that_hits_a_full_disk_leaves_memory_and_disk_unchanged(tmp_path, monkeypatch, after_calls):
    store = EDLStore(tmp_path)
    dispatch(store, "add_text", {"text": "before", "start": 0.0, "end": 1.0})
    good_hash, n_ops = store.edl.hash(), len(store.ops.ops)
    on_disk = store.edl_path.read_text()
    ops_on_disk = store.ops_path.read_text()
    snaps = sorted(p.name for p in store.snapshots_dir.iterdir())
    restore = _full_disk(monkeypatch, after_calls)
    with pytest.raises(OSError):
        dispatch(store, "add_text", {"text": "full2", "start": 2.0, "end": 3.0})
    restore()
    assert _texts(store) == ["before"]
    assert store.edl.hash() == good_hash and len(store.ops.ops) == n_ops
    assert store.edl_path.read_text() == on_disk and store.ops_path.read_text() == ops_on_disk
    assert sorted(p.name for p in store.snapshots_dir.iterdir()) == snaps
    assert not list(tmp_path.rglob("*.tmp"))
    # Space is back: the next edit commits, and ONE undo takes only it away.
    dispatch(store, "add_text", {"text": "after", "start": 4.0, "end": 5.0})
    assert _texts(store) == ["before", "after"]
    assert store.undo()
    assert _texts(store) == ["before"]


@pytest.fixture()
def api(tmp_path, monkeypatch):
    from video_ai_editor import config, main as _main, storage as _storage
    from video_ai_editor.api.hardening import RATE
    wd = tmp_path / "wd"
    for mod in (config, _storage, _main):
        monkeypatch.setattr(mod, "WORKDIR", wd, raising=False)
    wd.mkdir()
    RATE.windows.clear()
    _main._STORES.clear()
    return _main, TestClient(_main.app)


def test_dispatch_answers_507_disk_full_and_head_keeps_the_last_good_state(api, monkeypatch):
    main, c = api
    sid = c.post("/api/sessions", json={"name": "full"}).json()["id"]
    r = c.post(f"/api/sessions/{sid}/dispatch", json={"tool": "add_text",
                                                      "args": {"text": "before", "start": 0, "end": 1}})
    assert r.status_code == 200, r.text
    head = c.get(f"/api/sessions/{sid}/head").json()
    restore = _full_disk(monkeypatch)
    r = c.post(f"/api/sessions/{sid}/dispatch", json={"tool": "add_text",
                                                      "args": {"text": "full2", "start": 2, "end": 3}})
    restore()
    assert r.status_code == 507, r.text
    err = r.json()["error"]
    assert "disk_full" in (err.get("code"), (err.get("details") or {}).get("error"))
    assert "An external tool failed" not in r.text
    assert c.get(f"/api/sessions/{sid}/head").json()["edl_hash"] == head["edl_hash"]
    edl = c.get(f"/api/sessions/{sid}/edl").json()
    assert [cl.get("text") for t in edl["tracks"] for cl in t["clips"] if "text" in cl] == ["before"]
