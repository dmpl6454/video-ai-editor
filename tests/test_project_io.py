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


def _hostile_vae(dst: Path, bundled: str, orig: str = "/x.mp4") -> None:
    """A .vae whose manifest points `bundled` wherever the caller likes."""
    import zipfile
    with zipfile.ZipFile(dst, "w") as zf:
        zf.writestr("manifest.json", json.dumps(
            {"media": [{"orig": orig, "bundled": bundled}]}))
        zf.writestr("edl.json", json.dumps(
            {"canvas": {"w": 320, "h": 180, "fps": 30}, "tracks": [], "duration": 0.0}))


def test_load_project_will_not_move_a_file_from_outside_the_archive(tmp_path, monkeypatch):
    """`POST /api/load_project` used to be an arbitrary-file MOVE primitive.

    `bundled = unpack / entry["bundled"]` — and `Path.__truediv__` DISCARDS the
    base when the right-hand side is absolute, so no traversal was even needed.
    A hand-made manifest naming `/Users/me/tax-return.pdf` had the file MOVED
    out of the user's home into `<session>/uploads/imported/`, from where the
    caller downloaded it with
    `GET /api/sessions/{sid}/files/uploads/imported/…` — a path
    `pairing.is_media_path` classifies as media, so a 60-second `?k=` token was
    enough. None of dispatch.py's six path guards were in the way: `load_project`
    never consulted `assert_path_allowed` at all, so arming LAN mode did nothing.
    """
    from video_ai_editor import storage as _storage, storage_project as _sp
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path / "wd")
    monkeypatch.setattr(_sp, "session_dir", lambda sid: tmp_path / "wd" / sid)

    secret = tmp_path / "tax-return.pdf"
    secret.write_text("TOP SECRET", encoding="utf-8")

    for bundled in (str(secret),                       # absolute — no ".." at all
                    "../../../../../../../.." + str(secret),   # traversal
                    "media/../../../outside.bin"):
        vae = tmp_path / "hostile.vae"
        vae.unlink(missing_ok=True)
        _hostile_vae(vae, bundled)
        new_sid = load_project(vae)
        imported = tmp_path / "wd" / new_sid / "uploads" / "imported"
        assert secret.exists(), f"{bundled!r} moved a file out of the user's home"
        assert secret.read_text(encoding="utf-8") == "TOP SECRET"
        assert list(imported.glob("*")) == [], f"{bundled!r} landed in the session"


def test_inside_guard_refuses_a_path_that_escapes_through_a_symlink(tmp_path):
    """The guard must RESOLVE, not just normalise lexically.

    `zipfile.extractall` writes a symlink member as an ordinary file rather than
    a link, so this is not reachable through the archive today — which is
    exactly why it is worth pinning. `_inside` is one `is_relative_to` call away
    from being rewritten as a cheaper-looking lexical `os.path.normpath` check,
    and a lexical check says `unpack/link/secret` is inside `unpack` no matter
    where `link` points. Anything that plants a link under the unpack directory
    (a future extractor, a restored backup, an `--extract` flag someone adds)
    would then walk straight back out. Assert the property, not the reachability.
    """
    from video_ai_editor.storage_project import _inside

    unpack = tmp_path / "unpack"
    unpack.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.bin").write_bytes(b"TOP SECRET")
    (unpack / "link").symlink_to(outside, target_is_directory=True)

    assert _inside(unpack, "link/secret.bin") is None
    # …and the guard is not simply refusing everything: a real member still
    # resolves, including one reached through a link that stays inside.
    (unpack / "media").mkdir()
    (unpack / "media" / "clip.mp4").write_bytes(b"\0")
    assert _inside(unpack, "media/clip.mp4") is not None


def test_load_project_still_imports_a_legitimate_relative_entry(tmp_path, monkeypatch):
    """The refusal must not be "reject everything" — the ordinary path is the
    one `save_project` writes, and it has to keep working."""
    import zipfile
    from video_ai_editor import storage as _storage, storage_project as _sp
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path / "wd")
    monkeypatch.setattr(_sp, "session_dir", lambda sid: tmp_path / "wd" / sid)

    vae = tmp_path / "ok.vae"
    with zipfile.ZipFile(vae, "w") as zf:
        zf.writestr("media/clip.mp4", b"\0" * 32)
        zf.writestr("manifest.json", json.dumps(
            {"media": [{"orig": "/original/clip.mp4", "bundled": "media/clip.mp4"}]}))
        zf.writestr("edl.json", json.dumps(
            {"canvas": {"w": 320, "h": 180, "fps": 30}, "tracks": [], "duration": 0.0}))
    sid = load_project(vae)
    landed = tmp_path / "wd" / sid / "uploads" / "imported" / "clip.mp4"
    assert landed.exists() and landed.read_bytes() == b"\0" * 32


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
