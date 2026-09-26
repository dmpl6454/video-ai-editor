"""QA-004: a superseded preview render is cancelled server-side.

The editor asks for a preview after every edit. Before this fix the server ran
EVERY request to completion — an edit followed by an undo 0.7 s later ran the
edited state's full render alongside (or ahead of, in `_RENDER_SLOTS`) the one
the user was actually waiting for, and a burst of edits queued N full renders.
Now a request for a DIFFERENT EDL hash of the same session terminates the older
render's ffmpeg, and the older request answers 409 `preview_superseded`.

Real renders, measured by wall clock and by what lands on disk:
  * the superseded request returns within ~1 s of the newer request instead of
    running its whole render (and leaves no `.part` file or preview behind);
  * the newer request still returns the CURRENT EDL's render;
  * a stale `preview.mp4?h=` no longer renders the current EDL just to 404.
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor.main import app
from video_ai_editor.api.hardening import RATE


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    from video_ai_editor import storage as _storage, main as _main
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    RATE.windows.clear()
    _main._STORES.clear()
    return TestClient(app)


def _make_slow_source(p: Path, dur: float) -> None:
    """1080p noise: expensive to decode + scale, so a render takes seconds."""
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc2=s=1920x1080:r=30:d={dur}",
         "-f", "lavfi", "-i", f"sine=f=440:duration={dur}",
         "-vf", "noise=alls=40:allf=t", "-c:v", "libx264", "-preset", "ultrafast",
         "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(p)],
        check=True, capture_output=True)


@pytest.fixture(scope="module")
def slow_src(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("media") / "slow.mp4"
    _make_slow_source(p, 40.0)
    return p


def _session(c: TestClient, src: Path, *, transition: bool) -> str:
    sid = c.post("/api/sessions", json={"name": "supersede"}).json()["id"]
    if transition:
        halves = [(0.0, 20.0, 0.0), (20.0, 40.0, 20.0)]
    else:
        halves = [(0.0, 40.0, 0.0)]
    for a, b, start in halves:
        r = c.post(f"/api/sessions/{sid}/dispatch", json={
            "tool": "add_clip",
            "args": {"track": "v1", "src": str(src), "in": a, "out": b, "start": start}})
        assert r.status_code == 200, r.text
    if transition:
        r = c.post(f"/api/sessions/{sid}/dispatch", json={
            "tool": "add_transition", "args": {"at": 20.0, "type": "fade"}})
        assert r.status_code == 200, r.text
    return sid


def _edit(c: TestClient, sid: str, db: float) -> str:
    edl = c.get(f"/api/sessions/{sid}/edl").json()
    cid = next(t for t in edl["tracks"] if t["id"] == "v1")["clips"][0]["id"]
    # A picture-changing edit (not audio-only) so the cheap remux path can't
    # answer it: rotate the first clip a little.
    r = c.post(f"/api/sessions/{sid}/dispatch", json={
        "tool": "set_clip_transform", "args": {"clip_id": cid, "rotation": db}})
    assert r.status_code == 200, r.text
    # The key a preview of this state is rendered and served under: the
    # RENDER hash (QA-131 — a marker edit changes the EDL hash, not this).
    from video_ai_editor import main as _main
    return _main._store(sid).edl.render_hash()


@pytest.mark.parametrize("transition", [False, True], ids=["chunk-path", "monolithic-path"])
def test_newer_preview_cancels_the_render_it_supersedes(client, slow_src, transition):
    c = client
    sid = _session(c, slow_src, transition=transition)
    old_hash = _edit(c, sid, 3.0)

    out: dict = {}

    def _old_request():
        t0 = time.perf_counter()
        r = c.post(f"/api/sessions/{sid}/preview")
        out["status"] = r.status_code
        out["body"] = r.json()
        out["end"] = time.perf_counter()
        out["took"] = out["end"] - t0

    th = threading.Thread(target=_old_request)
    th.start()
    time.sleep(1.5)                      # the old render is now well under way
    assert th.is_alive(), "render finished too fast to observe a supersede"

    new_hash = _edit(c, sid, 6.0)
    assert new_hash != old_hash
    t_new = time.perf_counter()
    r = c.post(f"/api/sessions/{sid}/preview")
    t_new_done = time.perf_counter()
    th.join(timeout=300)

    # The newer request renders the CURRENT EDL.
    assert r.status_code == 200, r.text
    assert r.json()["edl_hash"] == new_hash
    # The superseded request was cancelled — promptly, not after running its
    # whole render — and says so.
    assert out["status"] == 409, out
    assert out["body"]["error"]["details"]["error"] == "preview_superseded", out["body"]
    assert out["end"] - t_new < 1.5, (
        f"superseded render ran on for {out['end'] - t_new:.2f}s after the newer "
        f"request (newer render itself took {t_new_done - t_new:.2f}s)")
    from video_ai_editor import main as _main
    pdir = _main._store(sid).dir / "previews"
    assert not (pdir / f"{old_hash}.mp4").exists(), "cancelled render was published"
    assert not list(pdir.glob("*.part*")), list(pdir.glob("*.part*"))
    assert (pdir / f"{new_hash}.mp4").exists()


def test_identical_concurrent_requests_share_one_render(client, slow_src):
    c = client
    sid = _session(c, slow_src, transition=False)
    h = _edit(c, sid, 2.0)
    res: list = []

    def _req():
        r = c.post(f"/api/sessions/{sid}/preview")
        res.append((r.status_code, r.json().get("edl_hash")))

    ths = [threading.Thread(target=_req) for _ in range(2)]
    for t in ths:
        t.start()
        time.sleep(0.3)
    for t in ths:
        t.join(timeout=300)
    assert res == [(200, h), (200, h)], res


def test_stale_hash_stream_does_not_render_the_current_edl(client, tmp_path):
    c = client
    src = tmp_path / "small.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=4",
         "-f", "lavfi", "-i", "sine=f=440:duration=4", "-c:v", "libx264",
         "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
         str(src)], check=True, capture_output=True)
    sid = c.post("/api/sessions", json={"name": "stale"}).json()["id"]
    r = c.post(f"/api/sessions/{sid}/dispatch", json={
        "tool": "add_clip", "args": {"track": "v1", "src": str(src), "in": 0.0,
                                     "out": 4.0, "start": 0.0}})
    cur = r.json()["edl_hash"]
    from video_ai_editor import main as _main
    pdir = _main._store(sid).dir / "previews"

    r = c.get(f"/api/sessions/{sid}/preview.mp4", params={"h": "0123456789abcdef"})
    assert r.status_code == 404
    # Nothing was rendered to answer it.
    assert not (pdir / f"{cur}.mp4").exists()

    # The current hash still renders on demand.
    r = c.get(f"/api/sessions/{sid}/preview.mp4", params={"h": cur})
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
