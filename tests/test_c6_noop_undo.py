"""QA-130: an edit that changes nothing must not record an undo step.

`EDLStore.commit()` used to snapshot and append an op even when the EDL hash
was unchanged, so splitting twice at the same playhead or dropping a clip back
where it already sat produced a "dead" ⌘Z that restored an identical
timeline. Each common no-op below goes through the real FastAPI `/dispatch`
route and asserts: no op recorded (`op` is null), undo depth unchanged, and the
very next ⌘Z undoes the last REAL edit.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor.edl import EDLStore


def _clip(root: Path) -> Path:
    d = root / "uploads" / "c"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "c.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc2=s=320x180:d=6:r=30",
                    "-f", "lavfi", "-i", "sine=f=440:d=6:r=48000",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(p)], check=True, capture_output=True)
    return p


@pytest.fixture
def api(tmp_path, monkeypatch):
    from video_ai_editor import config, main as _main, storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    before = config._FORCED_RESTRICT
    config.enable_path_restriction(False)
    _main._STORES.clear()
    client = TestClient(_main.app)
    sid = client.post("/api/sessions", json={"name": "noop"}).json()["id"]
    src = _clip(tmp_path)

    def d(tool: str, **args):
        r = client.post(f"/api/sessions/{sid}/dispatch", json={"tool": tool, "args": args})
        assert r.status_code == 200, r.text
        return r.json()

    d("add_clip", track="v1", src=str(src), start=0, **{"in": 0, "out": 6})
    try:
        yield d
    finally:
        _main._STORES.clear()
        config.enable_path_restriction(before)


def test_splitting_twice_at_the_same_playhead_records_one_step(api):
    d = api
    before = d("get_timeline")["edl_hash"]
    first = d("split_at", time=2.0)
    assert first["op"] is not None and first["edl_hash"] != before
    depth = first["undo_depth"]
    second = d("split_at", time=2.0)
    assert second["op"] is None
    assert second["edl_hash"] == first["edl_hash"]
    assert second["undo_depth"] == depth
    u = d("undo")
    assert u["result"]["ok"] is True and u["edl_hash"] == before


def test_moving_a_clip_to_its_own_start_records_nothing(api):
    d = api
    before = d("get_timeline")["edl_hash"]
    split = d("split_at", time=3.0)
    second = _v1_clips(d)[1]
    r = d("move_clip", clip_id=second["id"], new_start=second["start"])
    assert r["op"] is None and r["edl_hash"] == split["edl_hash"]
    assert r["undo_depth"] == split["undo_depth"]
    u = d("undo")
    assert u["result"]["ok"] is True and u["edl_hash"] == before


def test_setting_a_property_to_its_current_value_records_nothing(api):
    d = api
    before = d("get_timeline")["edl_hash"]
    cid = _v1_clips(d)[0]["id"]
    real = d("set_volume", target=cid, db=-6)
    assert real["op"] is not None
    for tool, args in (("set_volume", {"target": cid, "db": -6}),
                       ("set_property", {"clip_id": cid, "path": "audio.gain_db", "value": -6}),
                       ("trim_clip", {"clip_id": cid, "in": 0, "out": 6})):
        r = d(tool, **args)
        assert r["op"] is None, f"{tool} with unchanged values recorded an op"
        assert r["edl_hash"] == real["edl_hash"] and r["undo_depth"] == real["undo_depth"]
    u = d("undo")
    assert u["result"]["ok"] is True and u["edl_hash"] == before


def test_a_noop_keeps_the_redo_stack(api):
    d = api
    cid = _v1_clips(d)[0]["id"]
    d("set_volume", target=cid, db=-12)
    u = d("undo")
    assert u["result"]["redo_available"] is True
    d("set_volume", target=cid, db=0)      # already 0 dB after the undo
    r = d("redo")
    assert r["result"]["ok"] is True, "a no-op edit must not clear Redo"


def test_store_commit_skips_an_unchanged_tree(tmp_path):
    s = EDLStore(tmp_path / "s")
    s.edl.canvas.bg = "#123456"
    assert s.commit("a", {}, "a") is not None
    ops, snaps = len(s.ops.ops), len(list(s.snapshots_dir.glob("*.json")))
    assert s.commit("b", {}, "b") is None
    assert (len(s.ops.ops), len(list(s.snapshots_dir.glob("*.json")))) == (ops, snaps)


def _v1_clips(d) -> list[dict]:
    return [c for t in d("get_timeline")["result"]["tracks"] if t["id"] == "v1"
            for c in t["clips"]]
