"""EX-03: a `.vae` carries the Editor Brain's session state.

`save_project` used to bundle the timeline, the snapshots and the media but
none of `<session>/brain/` (versions, decisions, graph header, angles,
footprint) and none of `snapshots/pinned/`, so a saved-then-opened brain
project had no versions, a prompt op pointing at a missing EDP, a Plan tab
and graph route that 404ed, and a V1 that lost its pin.

Proved here as a full round trip on a real session: build a brain session
(hand EDP + graph through the store, one prompt op, version V1 Reel), save,
open into a FRESH WORKDIR (the original session and its analysis layers are
gone), then GET brain/versions (V1 Reel pinned and current), the decisions
and graph routes, restore V1 through the route, and resolve the EDP against
the reopened session's own graph. Plus: a 0.8.0 archive (no brain/) still
opens; hostile brain entries (symlink, escaping names, oversize, wrong id,
a graph naming /etc/passwd) are refused or confined.
"""
from __future__ import annotations

import json
import stat
import sys
import threading
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
import brain_contract_fixtures as BF  # noqa: E402
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture  # noqa: E402,F401

from video_ai_editor import config, storage, storage_brain  # noqa: E402
from video_ai_editor import main as _main  # noqa: E402
from video_ai_editor import storage_project as SP  # noqa: E402
from video_ai_editor.agent.prompt import executor, validate  # noqa: E402
from video_ai_editor.agent.prompt.schema import Step  # noqa: E402
from video_ai_editor.api.hardening import RATE  # noqa: E402
from video_ai_editor.brain import resolve as R  # noqa: E402
from video_ai_editor.brain import store as BS  # noqa: E402
from video_ai_editor.brain import versions as V  # noqa: E402
from video_ai_editor.edl.snapshot import EDLStore  # noqa: E402
from video_ai_editor.main import app  # noqa: E402

pytestmark = pytest.mark.usefixtures("desktop_posture")


def _point(mp: pytest.MonkeyPatch, wd: Path) -> None:
    """Every module that binds WORKDIR by name, pointed at `wd`."""
    for mod in (config, storage, SP, _main):
        mp.setattr(mod, "WORKDIR", wd)
    RATE.windows.clear()
    _main._STORES.clear()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    first = tmp_path / "wd_first"
    first.mkdir()
    _point(monkeypatch, first)
    return tmp_path, first


def _brain_session(wd: Path, *, extra_edit: bool = True) -> tuple[str, EDLStore]:
    """A session with a graph, an EDP, ONE applied prompt op and a pinned version V1 Reel."""
    sid = "s_bundle0001"
    store = F.make_store(wd, name=sid)
    src = str(next(c.src for c in store.edl.get_track("v1").clips))
    BF.write_brain_files(Path(store.dir), src, workdir=wd)
    facts = F.facts_for(store)
    steps = [Step(tool="split_at", args={"track": "v1", "time": "$brain:story_splits", "plan_ref": BF.DID}, why="isolate the hook"),
             Step(tool="reorder_clips", args={"track": "v1", "order": "$brain:story_order", "plan_ref": BF.DID}, why="open on it")]
    plan = validate.validate_plan(F.plan_of(*steps, intent="edit", title="Reel"), facts)
    res = executor.run_plan(store, plan, facts, emit=lambda e: None, cancel_event=threading.Event(), prompt="make a reel")
    assert res.error is None and res.committed, res.error
    V.record_after_run(store, decisions_id=BF.DID, title="Reel")
    if extra_edit:
        BF_edit(store)
    return sid, store


def BF_edit(store: EDLStore) -> None:
    from video_ai_editor.agent.dispatch import dispatch
    dispatch(store, "cut_range", {"track": "v1", "start": 1.0, "end": 1.5})


def _save(sid: str, dst: Path) -> Path:
    return SP.save_project(sid, dst)


def _names(vae: Path) -> list[str]:
    with zipfile.ZipFile(vae) as zf:
        return zf.namelist()


def _fresh_workdir(tmp_path: Path, mp: pytest.MonkeyPatch, name: str = "wd_second") -> Path:
    wd = tmp_path / name
    wd.mkdir()
    _point(mp, wd)
    return wd


# ---------------------------------------------------------------- the round trip

def test_the_archive_carries_the_brain_directory_and_says_what_it_left_out(env):
    tmp_path, wd = env
    sid, store = _brain_session(wd)
    vae = _save(sid, tmp_path / "p.vae")
    names = set(_names(vae))
    assert {"brain/versions.json", f"brain/decisions/{BF.DID}.json", f"brain/graph/{BF.GID}.json",
            "brain/graph/current.json", "brain/angles.json", "brain/scenes.json"} <= names
    assert not any(n.startswith("brain/") and "analysis" in n for n in names)          # the layers are not bundled ...
    with zipfile.ZipFile(vae) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    b = manifest["brain"]
    assert b["files"] >= 6 and b["left_out"] == [] and any("analysis layers" in x for x in b["not_bundled"])   # ... and it says so


def test_full_round_trip_into_a_fresh_workdir(env, monkeypatch):
    tmp_path, wd = env
    sid, store = _brain_session(wd)
    before = TestClient(app).get(f"/api/sessions/{sid}/brain/versions").json()
    assert [(v["label"], v["pinned"], v["restorable"]) for v in before["versions"]] == [("V1 Reel", True, True)]
    vae = _save(sid, tmp_path / "p.vae")
    # the original session and every analysis layer are gone: only the archive remains
    new_wd = _fresh_workdir(tmp_path, monkeypatch)
    assert not (new_wd / "analysis").exists()
    new_sid = SP.load_project(vae)
    client = TestClient(app)
    got = client.get(f"/api/sessions/{new_sid}/brain/versions")
    assert got.status_code == 200, got.text
    rows = got.json()["versions"]
    assert [(v["label"], v["pinned"], v["restorable"], v["decisions_id"]) for v in rows] == [("V1 Reel", True, True, BF.DID)]
    new_store = EDLStore(new_wd / new_sid)
    # V1 is a version of the state right after the run; one edit was made after it, so it is not current ...
    assert rows[0]["current"] is False and rows[0]["edl_hash"] != new_store.edl.hash()
    # ... the prompt op's EDP is on file and served, the graph header too
    prompt_op = next(o for o in new_store.ops.ops if o.tool == "prompt")
    assert prompt_op.args["decisions"] == BF.DID
    d = client.get(f"/api/sessions/{new_sid}/brain/decisions/{BF.DID}")
    assert d.status_code == 200 and d.json()["id"] == BF.DID and len(d.json()["decisions"]) == 9
    g = client.get(f"/api/sessions/{new_sid}/brain/graph")
    assert g.status_code == 200 and g.json()["id"] == BF.GID
    # restore V1 through the route: ONE op, the timeline is what it was right after the run
    r = client.post(f"/api/sessions/{new_sid}/brain/versions/{rows[0]['id']}/restore",
                    headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200, r.text
    after = client.get(f"/api/sessions/{new_sid}/brain/versions").json()["versions"][0]
    assert after["current"] is True and after["pinned"] is True
    # the EDP resolves against ITS OWN graph in the reopened session: sources are the new session's files
    store2 = _main._store(new_sid) if hasattr(_main, "_store") else EDLStore(new_wd / new_sid)
    clip_src = next(c.src for c in store2.edl.get_track("v1").clips)
    args, _ = R.resolve(store2, "cut_source_ranges", {"track": "v1", "ranges": "$brain:cuts", "plan_ref": BF.DID})
    assert args is not None and args["ranges"] and all(x["src"] == clip_src for x in args["ranges"]), args
    assert str(new_wd) in clip_src and str(wd) not in json.dumps(BS.read_json(new_wd / new_sid / "brain" / "angles.json"))


def test_a_pinned_snapshot_parked_by_the_undo_pruning_travels(env, monkeypatch):
    """The pin's point: an old version whose snapshot left the undo sequence still restores after a reopen."""
    tmp_path, wd = env
    sid, store = _brain_session(wd)
    v = V.list_versions(store.dir)[0]
    store._retire_snapshot(Path(store.dir) / "snapshots" / V.snapshot_name(v))          # what a prune does to a pinned one
    assert V.snapshot_file(store.dir, v).parent.name == "pinned"
    vae = _save(sid, tmp_path / "p.vae")
    assert any(n.startswith("snapshots/pinned/") for n in _names(vae))
    new_wd = _fresh_workdir(tmp_path, monkeypatch)
    new_sid = SP.load_project(vae)
    new_store = EDLStore(new_wd / new_sid)
    rows = V.list_versions(new_store.dir)
    assert len(rows) == 1 and rows[0].pinned and rows[0].restorable
    assert V.snapshot_file(new_store.dir, rows[0]).parent.name == "pinned"
    op = V.restore(new_store, rows[0].id)
    assert op is not None and op.tool == "restore_version" and V.current_version(new_store).id == rows[0].id


# ---------------------------------------------------------------- old and hostile archives

def test_a_080_archive_without_brain_still_opens(env, monkeypatch):
    tmp_path, wd = env
    store = F.make_store(wd, name="s_plain0001")
    vae = _save("s_plain0001", tmp_path / "plain.vae")
    assert not [n for n in _names(vae) if n.startswith("brain/")]
    with zipfile.ZipFile(vae) as zf:
        assert "brain" not in json.loads(zf.read("manifest.json"))                    # nothing to say about what is not there
    # and an archive written by 0.8.0 from a session that HAD a brain dir is the same shape: strip it
    sid, _ = _brain_session(wd)
    full = _save(sid, tmp_path / "full.vae")
    old = tmp_path / "old.vae"
    with zipfile.ZipFile(full) as src, zipfile.ZipFile(old, "w") as dst:
        for info in src.infolist():
            if info.filename.startswith(("brain/", "snapshots/pinned/")):
                continue
            body = src.read(info.filename)
            if info.filename == "manifest.json":
                m = json.loads(body)
                m.pop("brain", None)
                body = json.dumps(m).encode()
            dst.writestr(info.filename, body)
    new_wd = _fresh_workdir(tmp_path, monkeypatch)
    new_sid = SP.load_project(old)
    assert TestClient(app).get(f"/api/sessions/{new_sid}/brain/versions").json()["versions"] == []
    assert not (new_wd / new_sid / "brain").exists()
    assert SP.load_project(vae)


def _rewrite(vae: Path, dst: Path, *, drop=(), add: dict | None = None, edit: dict | None = None,
             symlink: str | None = None) -> Path:
    with zipfile.ZipFile(vae) as src, zipfile.ZipFile(dst, "w") as out:
        for info in src.infolist():
            if info.filename in drop:
                continue
            body = src.read(info.filename)
            if edit and info.filename in edit:
                body = edit[info.filename](body)
            out.writestr(info.filename, body)
        for name, body in (add or {}).items():
            out.writestr(name, body)
        if symlink:
            zi = zipfile.ZipInfo(symlink)
            zi.external_attr = (stat.S_IFLNK | 0o777) << 16
            out.writestr(zi, "/etc/passwd")
    return dst


def test_hostile_brain_entries_are_refused_or_ignored(env, monkeypatch):
    tmp_path, wd = env
    sid, _ = _brain_session(wd)
    vae = _save(sid, tmp_path / "p.vae")
    new_wd = _fresh_workdir(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="symbolic link"):
        SP.load_project(_rewrite(vae, tmp_path / "sym.vae", symlink="brain/decisions/d_00000001.json"))
    with pytest.raises(ValueError, match="unsafe path"):
        SP.load_project(_rewrite(vae, tmp_path / "up.vae", add={"brain/../../evil.json": "{}"}))
    with pytest.raises(ValueError, match="unsafe path"):
        SP.load_project(_rewrite(vae, tmp_path / "abs.vae", add={"/brain/versions.json": "{}"}))
    assert [p.name for p in new_wd.iterdir() if p.name.startswith(("s_", "_import"))] == []       # nothing left behind
    # names outside the whitelist, a wrong id, invalid JSON: not restored, the project still opens
    hostile = _rewrite(vae, tmp_path / "junk.vae", add={
        "brain/evil.json": "{}", "brain/decisions/notanid.json": "{}",
        "brain/graph/g_ffffffffffff.json": json.dumps({"id": "g_000000000000"}),      # names another id than its file
        "brain/decisions/d_deadbeef.json": "not json"})
    new_sid = SP.load_project(hostile)
    bdir = new_wd / new_sid / "brain"
    assert sorted(p.relative_to(bdir).as_posix() for p in bdir.rglob("*") if p.is_file()) == sorted(
        ["versions.json", "footprint.json", "scenes.json", "angles.json", f"decisions/{BF.DID}.json",
         f"graph/{BF.GID}.json", "graph/current.json"] + (["timings.jsonl"] if (bdir / "timings.jsonl").exists() else []))


def test_a_brain_file_naming_a_file_outside_the_project_is_confined(env, monkeypatch):
    """A tampered graph and angles.json naming /etc/passwd: the path never reaches the reopened session."""
    tmp_path, wd = env
    sid, _ = _brain_session(wd)
    vae = _save(sid, tmp_path / "p.vae")

    def evil(body: bytes) -> bytes:
        return json.dumps(_tamper(json.loads(body))).encode()

    def _tamper(node):
        if isinstance(node, dict):
            return {k: ("/etc/passwd" if k == "path" else _tamper(v)) for k, v in node.items()}
        if isinstance(node, list):
            return [_tamper(x) for x in node]
        return node

    hostile = _rewrite(vae, tmp_path / "t.vae", edit={f"brain/graph/{BF.GID}.json": evil, "brain/angles.json": evil})
    new_wd = _fresh_workdir(tmp_path, monkeypatch)
    new_sid = SP.load_project(hostile)
    for name in (f"graph/{BF.GID}.json", "angles.json"):
        text = (new_wd / new_sid / "brain" / name).read_text(encoding="utf-8")
        assert "/etc/passwd" not in text and "_missing/passwd" in text, name


def test_size_caps_are_kept_and_said(env, monkeypatch):
    tmp_path, wd = env
    sid, _ = _brain_session(wd)
    monkeypatch.setattr(storage_brain, "BRAIN_FILE_CAP", 400)                 # smaller than the graph header and the EDP
    vae = _save(sid, tmp_path / "small.vae")
    with zipfile.ZipFile(vae) as zf:
        b = json.loads(zf.read("manifest.json"))["brain"]
    said = {x["name"] for x in b["left_out"]}
    assert f"brain/decisions/{BF.DID}.json" in said and all(x["why"] == "over the size cap" for x in b["left_out"])
    assert f"brain/decisions/{BF.DID}.json" not in _names(vae)
    monkeypatch.setattr(storage_brain, "BRAIN_FILE_CAP", 64 * 1024 * 1024)
    big = _save(sid, tmp_path / "big.vae")
    monkeypatch.setattr(storage_brain, "BRAIN_FILE_CAP", 400)                 # and on OPEN the same cap holds
    new_wd = _fresh_workdir(tmp_path, monkeypatch, "wd_cap")
    new_sid = SP.load_project(big)
    assert not (new_wd / new_sid / "brain" / "decisions" / f"{BF.DID}.json").exists()
    assert (new_wd / new_sid / "edl.json").is_file()


def test_a_versions_file_with_garbage_rows_is_left_out_of_an_import_and_undo_still_works(env, monkeypatch):
    """Closer review: the import accepted `{versions: [1, "x", {op_seq: null, …}]}` and every Undo and prune in the
    new session then raised inside `_retire_snapshot`, half-way through the edit."""
    tmp_path, wd = env
    sid, _ = _brain_session(wd)
    vae = _save(sid, tmp_path / "p.vae")
    new_wd = _fresh_workdir(tmp_path, monkeypatch)
    bad = json.dumps({"versions": [1, "x", None, {"op_seq": None, "edl_hash": "abc", "pinned": True}]})
    new_sid = SP.load_project(_rewrite(vae, tmp_path / "garbage.vae", edit={"brain/versions.json": lambda _b: bad.encode()}))
    assert not (new_wd / new_sid / "brain" / "versions.json").exists()          # not the shape this app writes: left out
    store = EDLStore(new_wd / new_sid)
    assert store.undo() is not None
    assert V.list_versions(new_wd / new_sid) == []
