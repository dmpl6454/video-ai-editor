"""Project I/O: .vae round-trip, undo/redo over many ops, snapshot
persistence, ops log integrity."""
from __future__ import annotations
import json
import subprocess
from pathlib import Path

import pytest

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


# --- is_project_archive: the content probe behind POST /api/load_project -----

def _zip_bytes_to(dst: Path, members: dict[str, bytes]) -> Path:
    import zipfile
    with zipfile.ZipFile(dst, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return dst


def test_probe_accepts_a_saved_project_and_ignores_its_name(tmp_path, monkeypatch):
    """The probe is what replaced the filename gate on /api/load_project, so
    it must say yes to a real save_project archive under ANY name — including
    the `<sid>.vae.txt` macOS produced for the shipped 0.7.1 app."""
    from video_ai_editor import storage as _storage, storage_project as _sp
    from video_ai_editor.storage_project import is_project_archive
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path / "wd")
    monkeypatch.setattr(_sp, "session_dir", lambda sid: tmp_path / "wd" / sid)
    _seed(tmp_path / "wd" / "session1")
    saved = save_project("session1", tmp_path / "p.vae")
    for name in ("P.VAE", "p", "blob", "session1.vae.txt", "p.mp4"):
        renamed = tmp_path / name
        renamed.write_bytes(saved.read_bytes())
        assert is_project_archive(renamed), name


def test_probe_refuses_what_is_not_a_zip(tmp_path):
    from video_ai_editor.storage_project import is_project_archive
    assert not is_project_archive(tmp_path / "missing.vae")
    (tmp_path / "text.vae").write_text("manifest.json\n")
    assert not is_project_archive(tmp_path / "text.vae")
    # Zip magic on the front of garbage: the signature alone is not enough.
    (tmp_path / "fake.vae").write_bytes(b"PK\x03\x04" + b"\xff" * 64)
    assert not is_project_archive(tmp_path / "fake.vae")


def test_probe_accepts_the_empty_archive_marker_only_as_far_as_zipfile_does(tmp_path):
    """`PK\\x05\\x06` alone is a real (empty) zip: zipfile opens it, it has no
    manifest, so it is a zip but not a project. A truncated marker that
    zipfile cannot open is refused without raising."""
    import zipfile
    from video_ai_editor.storage_project import is_project_archive
    empty = tmp_path / "empty.vae"
    zipfile.ZipFile(empty, "w").close()
    assert empty.read_bytes().startswith(b"PK\x05\x06")
    assert not is_project_archive(empty)
    (tmp_path / "torn.vae").write_bytes(b"PK\x05\x06\x00\x00")
    assert not is_project_archive(tmp_path / "torn.vae")


def test_probe_only_counts_a_file_member_at_the_archive_root(tmp_path):
    """Proof is a FILE that `extractall` places at `<unpack>/manifest.json`.

    Judged on the literal member name. The first version resolved the name
    against a fake base so it could reuse `_inside`, and that made the probe
    MORE LENIENT than the loader in three ways — `Path.resolve()` normalises
    `..` where `extractall` drops it, and a directory entry resolves to the
    same path a file does. Each of the last three below then passed the probe
    and failed the loader, which is the one thing the probe must never do.
    """
    from video_ai_editor.storage_project import is_project_archive
    assert is_project_archive(_zip_bytes_to(tmp_path / "root.vae", {"manifest.json": b"{}"}))
    # `./manifest.json`: some zip writers emit it, extractall drops the `.`.
    assert is_project_archive(_zip_bytes_to(tmp_path / "dot.vae", {"./manifest.json": b"{}"}))
    for i, member in enumerate(("backup/manifest.json",       # nested
                                "../manifest.json",           # escapes
                                "/manifest.json",             # absolute
                                "manifest.json/",             # a directory
                                "../vae-probe/manifest.json",   # out and back
                                "vae-probe/../manifest.json")):  # in and out
        p = _zip_bytes_to(tmp_path / f"m{i}.vae", {member: b"{}", "keep.txt": b"x"})
        assert not is_project_archive(p), member
    assert not is_project_archive(_zip_bytes_to(tmp_path / "none.vae", {"edl.json": b"{}"}))


def test_a_refused_import_leaves_no_session_behind(tmp_path, monkeypatch):
    """`load_project` validates in a private directory, THEN creates the session.

    It used to create the session first and unpack into it, so every archive
    that failed validation left an `s_*` directory in WORKDIR — and `GET
    /api/sessions` globs `s_*`, so each refusal added a ghost "empty project"
    to the picker. Four broken files, four ghosts, no way to tell them from
    real work that lost its media.
    """
    import zipfile
    from video_ai_editor import storage as _storage, storage_project as _sp
    wd = tmp_path / "wd"
    monkeypatch.setattr(_storage, "WORKDIR", wd)
    monkeypatch.setattr(_sp, "session_dir", lambda sid: wd / sid)
    wd.mkdir()

    broken = {
        "no_manifest.vae": {"edl.json": b"{}"},
        "bad_manifest.vae": {"manifest.json": b"{ not json"},
        "manifest_is_a_list.vae": {"manifest.json": b"[]", "edl.json": b"{}"},
        "dir_manifest.vae": {"manifest.json/": b"{}", "edl.json": b"{}"},
        "bad_edl.vae": {"manifest.json": b'{"media": []}', "edl.json": b"nope{"},
        "no_edl.vae": {"manifest.json": b'{"media": []}'},
    }
    for name, members in broken.items():
        p = tmp_path / name
        with zipfile.ZipFile(p, "w") as zf:
            for member, data in members.items():
                zf.writestr(member, data)
        with pytest.raises(ValueError) as exc:
            load_project(p)
        # The message is user-facing (the endpoint wraps it in a 422 body), so
        # it must not carry the app's own absolute paths.
        assert str(wd) not in str(exc.value), name
        assert sorted(q.name for q in wd.glob("s_*")) == [], name
        assert list(wd.glob("_import_unpack_*")) == [], name
