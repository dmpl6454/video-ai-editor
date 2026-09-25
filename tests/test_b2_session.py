"""Wave B lane B2 (session): project names, the multi-window stale-view guard
and clipboard paste — all through the real FastAPI app / real dispatch.

* QA-099 — projects had no rename route and were named after their raw
  ``s_xxxxxxxxxx`` id; a reopened .vae was indistinguishable from the copy it
  was saved from.
* QA-105 — two windows on one project diverged silently, and a STALE window's
  Undo reverted the other window's edit. The client now sends the EDL hash its
  view was built from (``base_hash``) and the server refuses an edit made
  against a timeline that has since changed; ``GET …/head`` lets a window
  notice cheaply.
* QA-056 — paste was ``duplicate_clip`` of the copied ids, so it ignored the
  playhead and failed once the source clip was deleted. ``paste_clips`` takes
  the copied clip CONTENT and a time.
"""
from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Canvas, Clip, TextClip, Track, Transform
from video_ai_editor.agent.dispatch import dispatch


# --- fixtures -----------------------------------------------------------------

@pytest.fixture
def workdir(tmp_path: Path, monkeypatch) -> Path:
    """Every module that binds WORKDIR by name, pointed at tmp — never the
    owner's real Application Support directory."""
    from video_ai_editor import config, storage, storage_project, main as _main
    from video_ai_editor.api.hardening import RATE
    for mod in (config, storage, storage_project, _main):
        monkeypatch.setattr(mod, "WORKDIR", tmp_path)
    RATE.windows.clear()
    _main._STORES.clear()
    return tmp_path


@pytest.fixture
def client(workdir: Path) -> TestClient:
    from video_ai_editor.main import app
    return TestClient(app)


@pytest.fixture(scope="module")
def clip_file(tmp_path_factory) -> Path:
    """A real 4 s lavfi clip (picture + sound)."""
    p = tmp_path_factory.mktemp("b2clip") / "src.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x90:d=4:r=30",
                    "-f", "lavfi", "-i", "sine=f=440:duration=4", "-pix_fmt", "yuv420p",
                    "-c:v", "libx264", "-c:a", "aac", "-shortest", str(p)],
                   check=True, capture_output=True)
    return p


def _seed(workdir: Path, sid: str, clip_file: Path, n: int = 2) -> list[str]:
    """Put `n` copies of the 4 s clip end to end on v1 of session `sid`."""
    src = workdir / sid / "uploads" / "src.mp4"
    src.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(clip_file, src)
    ids = [f"c{i}" for i in range(n)]
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video",
              clips=[Clip(id=cid, src=str(src), in_=0, out=4, start=4 * i) for i, cid in enumerate(ids)]),
        Track(id="text", type="text", z=10, clips=[]),
    ])
    edl.recompute_duration()
    (workdir / sid / "edl.json").write_text(edl.model_dump_json())
    from video_ai_editor import main as _main
    _main._STORES.pop(sid, None)
    return ids


# --- QA-099: project names ------------------------------------------------------

def test_new_projects_get_friendly_numbered_names_not_their_id(client):
    a = client.post("/api/sessions").json()
    b = client.post("/api/sessions", json={}).json()
    assert a["name"] == "Untitled project 1"
    assert b["name"] == "Untitled project 2"
    assert not a["name"].startswith("s_")
    listed = {s["id"]: s for s in client.get("/api/sessions").json()["sessions"]}
    assert listed[a["id"]]["name"] == "Untitled project 1"
    # The picker shows a date instead of the id, so it needs one.
    assert isinstance(listed[a["id"]]["modified_at"], float)


def test_an_explicit_name_is_kept(client):
    r = client.post("/api/sessions", json={"name": "Trip to Goa"}).json()
    assert r["name"] == "Trip to Goa"


def test_rename_route_persists_the_name_and_keeps_other_meta(client, workdir):
    sid = client.post("/api/sessions").json()["id"]
    from video_ai_editor.storage import read_meta, write_meta
    write_meta(sid, {**read_meta(sid), "source": "imported"})
    r = client.patch(f"/api/sessions/{sid}", json={"name": "  My Reel \n"})
    assert r.status_code == 200, r.text
    assert r.json() == {"id": sid, "name": "My Reel"}
    assert client.get(f"/api/sessions/{sid}").json()["name"] == "My Reel"
    assert read_meta(sid)["source"] == "imported"


@pytest.mark.parametrize("name", ["", "   ", "x" * 121])
def test_rename_rejects_empty_or_overlong_names(client, name):
    sid = client.post("/api/sessions").json()["id"]
    r = client.patch(f"/api/sessions/{sid}", json={"name": name})
    assert r.status_code == 400, r.text
    assert client.get(f"/api/sessions/{sid}").json()["name"] == "Untitled project 1"


def test_rename_of_a_missing_or_malformed_session(client):
    assert client.patch("/api/sessions/s_doesnotexist1", json={"name": "a"}).status_code == 404
    assert client.patch("/api/sessions/not-a-session", json={"name": "a"}).status_code == 400


def test_reopening_a_saved_project_twice_gives_distinguishable_names(client, workdir, clip_file):
    sid = client.post("/api/sessions", json={"name": "Wedding cut"}).json()["id"]
    _seed(workdir, sid, clip_file, n=1)
    saved = client.post(f"/api/sessions/{sid}/save_project")
    assert saved.status_code == 200, saved.text
    data = client.get(saved.json()["url"]).content
    names = []
    for _ in range(2):
        r = client.post("/api/load_project",
                        files={"file": ("Wedding.vae", io.BytesIO(data), "application/octet-stream")})
        assert r.status_code == 200, r.text
        names.append(client.get(f"/api/sessions/{r.json()['id']}").json()["name"])
    all_names = [s["name"] for s in client.get("/api/sessions").json()["sessions"]]
    assert len(set(all_names)) == len(all_names), all_names
    assert all(n.startswith("Wedding cut (opened ") for n in names), names


# --- QA-105: stale window guard ---------------------------------------------------

def test_head_reports_the_current_hash(client, workdir, clip_file):
    sid = client.post("/api/sessions").json()["id"]
    _seed(workdir, sid, clip_file)
    head = client.get(f"/api/sessions/{sid}/head")
    assert head.status_code == 200, head.text
    body = head.json()
    assert body["edl_hash"] == client.get(f"/api/sessions/{sid}").json()["summary"]["edl_hash"]
    assert set(body) >= {"edl_hash", "ops", "redo_available"}


def test_a_stale_windows_undo_is_refused_and_changes_nothing(client, workdir, clip_file):
    sid = client.post("/api/sessions").json()["id"]
    ids = _seed(workdir, sid, clip_file)
    h0 = client.get(f"/api/sessions/{sid}/head").json()["edl_hash"]
    # Window 1 deletes the first clip (its view was h0: accepted).
    r1 = client.post(f"/api/sessions/{sid}/dispatch",
                     json={"tool": "ripple_delete", "args": {"clip_id": ids[0]}, "base_hash": h0})
    assert r1.status_code == 200, r1.text
    h1 = r1.json()["edl_hash"]
    # Window 2 still shows h0 and presses Cmd+Z: it must NOT restore the clip
    # window 1 deleted.
    r2 = client.post(f"/api/sessions/{sid}/dispatch",
                     json={"tool": "undo", "args": {}, "base_hash": h0})
    assert r2.status_code == 409, r2.text
    err = r2.json()["error"]
    assert err["details"]["code"] == "stale_edl"
    assert err["details"]["edl_hash"] == h1
    edl = client.get(f"/api/sessions/{sid}/edl").json()
    assert [c["id"] for c in edl["tracks"][0]["clips"]] == [ids[1]]
    # An ordinary edit from the stale view is refused the same way.
    r3 = client.post(f"/api/sessions/{sid}/dispatch",
                     json={"tool": "duplicate_clip", "args": {"clip_id": ids[1]}, "base_hash": h0})
    assert r3.status_code == 409
    assert client.get(f"/api/sessions/{sid}/head").json()["edl_hash"] == h1
    # Once refreshed (base = h1) the same undo goes through.
    r4 = client.post(f"/api/sessions/{sid}/dispatch",
                     json={"tool": "undo", "args": {}, "base_hash": h1})
    assert r4.status_code == 200, r4.text


def test_callers_without_a_base_hash_are_unaffected(client, workdir, clip_file):
    """MCP, Claude and older UI builds send no base_hash."""
    sid = client.post("/api/sessions").json()["id"]
    ids = _seed(workdir, sid, clip_file)
    r = client.post(f"/api/sessions/{sid}/dispatch",
                    json={"tool": "ripple_delete", "args": {"clip_id": ids[0]}})
    assert r.status_code == 200, r.text


def test_the_async_path_checks_the_base_too(client, workdir, clip_file):
    sid = client.post("/api/sessions").json()["id"]
    ids = _seed(workdir, sid, clip_file)
    h0 = client.get(f"/api/sessions/{sid}/head").json()["edl_hash"]
    client.post(f"/api/sessions/{sid}/dispatch", json={"tool": "ripple_delete", "args": {"clip_id": ids[0]}})
    r = client.post(f"/api/sessions/{sid}/dispatch?wait=0",
                    json={"tool": "undo", "args": {}, "base_hash": h0})
    assert r.status_code == 409, r.text


# --- QA-056: paste clip CONTENT at a time -----------------------------------------

@pytest.fixture
def store(tmp_path: Path, clip_file: Path) -> EDLStore:
    src = tmp_path / "src.mp4"
    shutil.copy(clip_file, src)
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(id="red", src=str(src), in_=0, out=4, start=0),
            Clip(id="blue", src=str(src), in_=0, out=4, start=4),
        ]),
        Track(id="v2", type="video", z=1, clips=[]),
        Track(id="text", type="text", z=10, clips=[
            TextClip(id="t1", text="HELLO", start=0.0, end=2.0, transform=Transform(x=160, y=40)),
        ]),
    ])
    edl.recompute_duration()
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(tmp_path)


def _payload(store: EDLStore, cid: str) -> dict:
    track, c = store.edl.get_clip(cid)
    return {"track": track.id, "clip": c.model_dump(by_alias=True, mode="json")}


def _v1(store: EDLStore) -> list[tuple[str, float, float, float]]:
    return [(c.id, round(c.start, 3), round(c.in_, 3), round(c.out, 3)) for c in store.edl.tracks[0].clips]


def test_paste_inserts_at_the_playhead_splitting_the_clip_under_it(store):
    payload = [_payload(store, "red")]
    ops_before = len(store.ops.ops)
    res = dispatch(store, "paste_clips", {"clips": payload, "at": 6.0})
    new_id = res["clip_ids"][0]
    v1 = _v1(store)
    # blue is cut at the playhead (6 s), the copy sits AT the playhead, and the
    # rest of blue follows it.
    assert v1[0] == ("red", 0.0, 0.0, 4.0)
    assert v1[1] == ("blue", 4.0, 0.0, 2.0)
    assert v1[2] == (new_id, 6.0, 0.0, 4.0)
    assert v1[3][1:] == (10.0, 2.0, 4.0)
    assert new_id not in ("red", "blue")
    # One gesture, one undo step.
    assert len(store.ops.ops) == ops_before + 1
    dispatch(store, "undo", {})
    assert [c.id for c in store.edl.tracks[0].clips] == ["red", "blue"]


def test_paste_still_works_after_the_source_clip_is_deleted(store):
    payload = [_payload(store, "red")]
    dispatch(store, "ripple_delete", {"clip_id": "red"})
    res = dispatch(store, "paste_clips", {"clips": payload, "at": 0.0})
    ids = [c.id for c in store.edl.tracks[0].clips]
    assert ids[0] == res["clip_ids"][0] and ids[1] == "blue"


def test_paste_past_the_end_of_the_main_lane_appends_without_a_gap(store):
    res = dispatch(store, "paste_clips", {"clips": [_payload(store, "red")], "at": 30.0})
    assert _v1(store)[-1] == (res["clip_ids"][0], 8.0, 0.0, 4.0)


def test_paste_shares_nothing_mutable_with_the_payload_and_keeps_its_look(store):
    dispatch(store, "set_clip_transform", {"clip_id": "red", "scale": 1.5})
    payload = [_payload(store, "red")]
    res = dispatch(store, "paste_clips", {"clips": payload, "at": 4.0})
    _, copy = store.edl.get_clip(res["clip_ids"][0])
    _, red = store.edl.get_clip("red")
    assert copy.transform.scale == 1.5
    assert copy.transform is not red.transform


def test_paste_of_a_title_lands_at_the_playhead_on_its_own_lane(store):
    res = dispatch(store, "paste_clips", {"clips": [_payload(store, "t1")], "at": 5.0})
    _, t = store.edl.get_clip(res["clip_ids"][0])
    assert (t.start, t.end, t.text) == (5.0, 7.0, "HELLO")
    # Over an occupied span it takes the first free span after the playhead.
    res2 = dispatch(store, "paste_clips", {"clips": [_payload(store, "t1")], "at": 1.0})
    _, t2 = store.edl.get_clip(res2["clip_ids"][0])
    assert (t2.start, t2.end) == (2.0, 4.0)


def test_paste_keeps_the_relative_spacing_of_a_multi_clip_copy_on_a_free_lane(store):
    src = store.edl.tracks[0].clips[0].src
    a = {"track": "v2", "clip": {"id": "p1", "src": src, "in": 0, "out": 1, "start": 2.0}}
    b = {"track": "v2", "clip": {"id": "p2", "src": src, "in": 0, "out": 1, "start": 5.0}}
    res = dispatch(store, "paste_clips", {"clips": [b, a], "at": 1.0})
    starts = sorted(c.start for c in store.edl.tracks[1].clips)
    assert starts == [1.0, 4.0]
    assert len(res["clip_ids"]) == 2


def test_paste_rejects_a_missing_media_file_and_changes_nothing(store, tmp_path):
    bad = _payload(store, "red")
    bad["clip"]["src"] = str(tmp_path / "gone.mp4")
    before = store.edl.hash()
    with pytest.raises(ValueError, match="no longer"):
        dispatch(store, "paste_clips", {"clips": [bad], "at": 0.0})
    assert store.edl.hash() == before


def test_paste_needs_clips(store):
    with pytest.raises(ValueError):
        dispatch(store, "paste_clips", {"clips": [], "at": 0.0})
