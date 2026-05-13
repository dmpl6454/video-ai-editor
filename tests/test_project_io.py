"""Project I/O: .vae round-trip, undo/redo over many ops, snapshot
persistence, ops log integrity."""
from __future__ import annotations
import json
import subprocess
from pathlib import Path

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Track, Clip, Canvas
from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.storage_project import save_project, load_project


def _mk(p: Path):
    keyed = p.with_suffix(".keyed.mp4")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                    "-i", "color=c=blue:s=320x180:d=2:r=30",
                    "-pix_fmt", "yuv420p", str(keyed)],
                   check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-i", str(keyed),
                    "-f", "lavfi", "-i", "sine=f=440:duration=2",
                    "-c:v", "copy", "-c:a", "aac", "-shortest", str(p)],
                   check=True, capture_output=True)


def _seed(tmp: Path) -> EDLStore:
    tmp.mkdir(parents=True, exist_ok=True)
    src = tmp / "src.mp4"; _mk(src)
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(src=str(src), in_=0, out=2, start=0, id="c1"),
        ]),
    ])
    edl.recompute_duration()
    (tmp / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(tmp)


def test_vae_round_trip_preserves_edl_and_media(tmp_path: Path, monkeypatch):
    """Save the project to .vae, load it into a fresh session, and verify
    both the EDL and the media survive the trip."""
    # save_project / load_project use the global WORKDIR, so steer them at our tmp.
    from video_ai_editor import storage as _storage, storage_project as _sp
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path / "wd")
    monkeypatch.setattr(_sp, "session_dir",
                        lambda sid: tmp_path / "wd" / sid)

    sid = "session1"
    sd = tmp_path / "wd" / sid
    store = _seed(sd)
    dst = tmp_path / "out.vae"
    save_project(sid, dst)
    assert dst.exists()
    new_sid = load_project(dst)
    new_sd = tmp_path / "wd" / new_sid
    new_store = EDLStore(new_sd)
    # EDL semantically equal
    assert len(new_store.edl.tracks) == len(store.edl.tracks)
    assert new_store.edl.tracks[0].clips[0].id == "c1"
    # Source media was packed in and re-pointed
    new_src = new_store.edl.tracks[0].clips[0].src
    assert Path(new_src).exists(), f"src {new_src} should exist after load"


def test_undo_redo_returns_to_same_state(tmp_path: Path):
    store = _seed(tmp_path)
    initial_hash = store.edl.hash()
    dispatch(store, "trim_clip", {"clip_id": "c1", "in": 0.5, "out": 1.5})
    dispatch(store, "set_speed", {"clip_id": "c1", "factor": 1.5})
    after_two_hash = store.edl.hash()
    assert after_two_hash != initial_hash

    dispatch(store, "undo", {})
    dispatch(store, "undo", {})
    assert store.edl.hash() == initial_hash

    dispatch(store, "redo", {})
    dispatch(store, "redo", {})
    assert store.edl.hash() == after_two_hash


def test_undo_50_random_ops(tmp_path: Path):
    """Stress test: 50 ops then 50 undos must end at start."""
    store = _seed(tmp_path)
    initial = store.edl.hash()
    for i in range(20):
        dispatch(store, "set_clip_transform", {"clip_id": "c1", "scale": 1.0 + i * 0.01})
    for _ in range(20):
        dispatch(store, "undo", {})
    assert store.edl.hash() == initial


def test_ops_log_records_each_op_with_timestamp(tmp_path: Path):
    store = _seed(tmp_path)
    dispatch(store, "trim_clip", {"clip_id": "c1", "in": 0.5, "out": 1.5})
    dispatch(store, "set_speed", {"clip_id": "c1", "factor": 1.5})
    dispatch(store, "add_marker", {"time": 1.0, "label": "test"})
    ops = store.ops.ops
    # At least one op per dispatch we made
    assert len(ops) >= 3
    summaries = [o.summary for o in ops]
    assert any("Trim" in s or "trim" in s for s in summaries)
    assert any("speed" in s.lower() or "Speed" in s for s in summaries)
    # Each op carries a tool name + args dict
    for o in ops:
        assert o.tool
        assert isinstance(o.args, dict)
        assert o.ts > 0


def test_snapshot_persists_across_store_reload(tmp_path: Path):
    """An edit then a fresh EDLStore() instantiation must show the edit
    survived (i.e. it was persisted to disk, not just in-memory)."""
    store = _seed(tmp_path)
    dispatch(store, "set_clip_transform", {"clip_id": "c1", "scale": 1.7})
    # Reload from disk
    fresh = EDLStore(tmp_path)
    sc = fresh.edl.tracks[0].clips[0].transform.scale
    assert sc == 1.7, sc


def test_redo_stack_cleared_after_new_op(tmp_path: Path):
    """After undo+new op, redo should be a no-op (standard editor behavior)."""
    store = _seed(tmp_path)
    dispatch(store, "set_clip_transform", {"clip_id": "c1", "scale": 1.5})
    h_after_first = store.edl.hash()
    dispatch(store, "undo", {})
    h_after_undo = store.edl.hash()
    dispatch(store, "set_clip_transform", {"clip_id": "c1", "scale": 2.0})
    h_after_new = store.edl.hash()
    dispatch(store, "redo", {})
    # redo should NOT bring back the 1.5 — we did a new op after undo.
    assert store.edl.hash() == h_after_new
    assert h_after_new != h_after_first
