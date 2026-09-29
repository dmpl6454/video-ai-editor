"""Final sweep 4 — opening a project (.vae): two findings from the
robustness/security sweep, each written to FAIL on the pre-fix tree.

1. `POST /api/load_project` unpacked the archive ON the event loop. The
   upload stream was awaited, but `_open_project_archive` (content probe +
   inflate every member + `shutil.move` the media + rewrite the state files)
   was a plain synchronous call inside the `async def`, so for ~2 s per GB of
   project nothing else was served: no SSE frame, no preview span fetch, no
   thumbnail, no /livez. The desktop's fetches have no timeout, so the UI
   simply hung for that long. `/upload` had the identical shape and was fixed
   the same way (QA-007); this mirrors it with `run_in_threadpool`.

2. A `.vae` whose manifest listed one of the archive's OWN state files (or
   `.`, the unpack root) as bundled media opened as an EMPTY project. Media is
   moved into the session BEFORE the state files are copied, so `edl.json`
   was moved into uploads/imported and `_write_state_files` skipped the now
   missing file — exactly the "my project opened blank" state that
   `_assert_timeline_importable` exists to prevent, one Save away from
   overwriting the user's good copy. Bundled media is now accepted only as a
   regular file under the archive's `media/` folder, and a missing `edl.json`
   after the media move refuses the open instead of blanking it.
"""
from __future__ import annotations

import asyncio
import json
import time
import zipfile
from pathlib import Path

import httpx
import pytest

import prompt_fixtures as F
from video_ai_editor.edl import EDLStore
from video_ai_editor.storage import session_dir


# --- fixtures -----------------------------------------------------------------

@pytest.fixture
def workdir(tmp_path: Path, monkeypatch) -> Path:
    """Every module that binds WORKDIR by name, pointed at tmp (the shape
    test_load_project_by_content uses) — missing one would write into the
    user's real Application Support directory."""
    from video_ai_editor import config, storage, storage_project, main as _main
    from video_ai_editor.api.hardening import RATE
    wd = tmp_path / "wd"
    wd.mkdir()
    for mod in (config, storage, storage_project, _main):
        monkeypatch.setattr(mod, "WORKDIR", wd)
    RATE.windows.clear()
    _main._STORES.clear()
    yield wd
    _main._STORES.clear()


def _crafted_vae(tmp_path: Path, extra_entries: list[dict]) -> tuple[Path, int]:
    """A legitimate archive (edl.json with one clip, its media under media/,
    a manifest) plus `extra_entries` appended to the manifest's media list.
    Returns the archive and the clip count the opened project must keep."""
    src_root = tmp_path / "srcwd"
    src_root.mkdir()
    src_store = F.make_store(src_root, name="s_src")
    clip = src_store.edl.get_track("v1").clips[0]
    vae = tmp_path / "crafted.vae"
    with zipfile.ZipFile(vae, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("edl.json", src_store.edl.to_json())
        zf.write(clip.src, arcname="media/talk.normalized.mp4")
        zf.writestr("manifest.json", json.dumps({"session_id": "s_src", "media": [
            {"orig": str(clip.src), "bundled": "media/talk.normalized.mp4"},
            *extra_entries,
        ]}))
    return vae, len(src_store.edl.get_track("v1").clips)


# --- 1. the unpack runs off the event loop -----------------------------------

STALL_S = 0.6          # what the fake unpack costs — a ~300 MB project's worth
WORST_ALLOWED_S = 0.25  # the finder's bar: a poll must never wait behind it


async def test_opening_a_project_does_not_stall_every_other_request(workdir, monkeypatch):
    """A /livez poller runs while a project is opened whose unpack takes
    STALL_S of plain CPU/IO time. On the loop, the poller's next answer waits
    the whole unpack out (its sleep cannot wake); in the threadpool it keeps
    answering every ~20 ms. The fake stands in for `_open_project_archive`
    so the test is deterministic and needs no 300 MB fixture; it still
    creates a real session, so `name_reopened_copy` runs on one."""
    from video_ai_editor import main as _main
    from video_ai_editor.storage import new_session_id

    def slow_open(tmp: Path, sent_name: str) -> str:
        time.sleep(STALL_S)                      # the inflate + move + rewrite
        sid = new_session_id()
        EDLStore(session_dir(sid))
        return sid

    monkeypatch.setattr(_main, "_open_project_archive", slow_open)
    transport = httpx.ASGITransport(app=_main.app)
    gaps: list[float] = []
    stop = asyncio.Event()

    async def poll(client: httpx.AsyncClient) -> None:
        last = time.monotonic()
        while not stop.is_set():
            await client.get("/livez")
            now = time.monotonic()
            gaps.append(now - last)
            last = now
            await asyncio.sleep(0.02)

    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765",
                                 timeout=60) as client:
        poller = asyncio.create_task(poll(client))
        await asyncio.sleep(0.1)
        r = await client.post("/api/load_project",
                              files={"file": ("p.vae", b"PK\x03\x04 not really", "application/zip")},
                              headers={"Sec-Fetch-Site": "same-origin"})
        stop.set()
        await poller
    assert r.status_code == 200, r.text
    assert session_dir(r.json()["id"]).is_dir()
    worst = max(gaps)
    assert worst < WORST_ALLOWED_S, (
        f"/livez waited {worst:.2f}s behind the project unpack over {len(gaps)} polls")


def test_a_refusal_raised_inside_the_unpack_keeps_its_status(workdir):
    """HTTPException raised in the threadpool must propagate unchanged: the
    415 for a non-project (and the 422/507 branches with it) keep their
    status and message, and the temp file is still removed."""
    from fastapi.testclient import TestClient
    from video_ai_editor.main import app
    r = TestClient(app).post("/api/load_project",
                             files={"file": ("notes.txt", b"just some text", "text/plain")},
                             headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 415, r.text
    assert "not a Video AI Editor project" in r.text
    assert list(workdir.glob("_import_*")) == []


# --- 2. a manifest entry cannot name a state file as media ------------------

@pytest.mark.parametrize("bundled", ["edl.json", ".", "manifest.json", "media", "../srcwd"])
def test_a_manifest_entry_naming_a_state_file_cannot_blank_the_opened_project(
        tmp_path, workdir, bundled):
    from video_ai_editor.storage_project import load_project
    vae, n_clips = _crafted_vae(tmp_path, [{"orig": "/tmp/nothing.mp4", "bundled": bundled}])
    sid = load_project(vae)
    sd = session_dir(sid)
    assert (sd / "edl.json").is_file(), sorted(str(p.relative_to(sd)) for p in sd.rglob("*"))[:20]
    opened = EDLStore(sd)
    v1 = opened.edl.get_track("v1")
    assert v1 is not None and len(v1.clips) == n_clips, "the opened project lost its timeline"
    imported = sd / "uploads" / "imported"
    assert not list(imported.rglob("manifest.json")), "the unpack dir was moved into the session"
    assert not (imported / "edl.json").exists()
    assert (imported / "talk.normalized.mp4").is_file(), "the real media was still imported"


def test_an_ingest_entry_outside_the_media_folder_is_ignored(tmp_path, workdir):
    """The `ingest` sidecar is confined the same way: an entry pointing it at
    edl.json must not write the timeline's JSON out as an ingest.json."""
    from video_ai_editor.storage_project import load_project
    src_root = tmp_path / "srcwd"
    src_root.mkdir()
    src_store = F.make_store(src_root, name="s_src")
    clip = src_store.edl.get_track("v1").clips[0]
    vae = tmp_path / "crafted.vae"
    with zipfile.ZipFile(vae, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("edl.json", src_store.edl.to_json())
        zf.write(clip.src, arcname="media/talk.normalized.mp4")
        zf.writestr("manifest.json", json.dumps({"session_id": "s_src", "media": [
            {"orig": str(clip.src), "bundled": "media/talk.normalized.mp4", "ingest": "edl.json"},
        ]}))
    sid = load_project(vae)
    sd = session_dir(sid)
    assert (sd / "edl.json").is_file()
    sidecars = list((sd / "uploads" / "imported").rglob("ingest.json"))
    assert sidecars == [], [p.read_text()[:80] for p in sidecars]


def test_a_missing_edl_after_the_media_move_refuses_the_open(tmp_path, workdir, monkeypatch):
    """The second lock: should anything ever take edl.json away between the
    importability check and the state-file copy, the open is REFUSED (422 at
    the endpoint) and the half-made session is removed — never a blank
    project the picker lists."""
    from video_ai_editor import storage_project as SP
    vae, _ = _crafted_vae(tmp_path, [])
    real_import = SP._import_media

    def import_and_lose_edl(manifest, unpack, imported):
        remap = real_import(manifest, unpack, imported)
        (unpack / "edl.json").unlink()
        return remap

    monkeypatch.setattr(SP, "_import_media", import_and_lose_edl)
    before = set(workdir.glob("s_*"))
    with pytest.raises(ValueError, match="edl.json"):
        SP.load_project(vae)
    assert set(workdir.glob("s_*")) == before, "a half-imported session reached the picker"
    assert list(workdir.glob("_import_*")) == []
