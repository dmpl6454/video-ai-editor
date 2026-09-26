"""QA-099-THUMBS: the project picker's poster frame, cached per project.

The picker showed a name and a date but no picture, because a picture means
reading each project's edl.json and GET /api/sessions must not read every EDL
on every call. Now `storage.poster_source` caches the answer in `poster.json`,
stamped with edl.json's (mtime_ns, size); the listing only stats edl.json, and
the EDL is read again only when it changed. Everything here runs the real app,
real dispatch, and real ffmpeg; the poster's pixels are decoded and measured.
"""
from __future__ import annotations

import pathlib
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch) -> Path:
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


def _clip(dirpath: Path, color: str) -> Path:
    p = dirpath / f"{color}.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    f"color=c={color}:s=320x180:d=3:r=30", "-pix_fmt", "yuv420p",
                    "-c:v", "libx264", str(p)], check=True, capture_output=True)
    return p


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("c3poster")
    return {"blue": _clip(d, "blue"), "red": _clip(d, "red")}


def _center_rgb(jpeg: bytes, tmp: Path) -> tuple[int, int, int, int]:
    """(r, g, b, height) of the decoded poster's centre pixel."""
    f = tmp / "poster.jpg"
    f.write_bytes(jpeg)
    h = int(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=height", "-of", "csv=p=0", str(f)],
                           check=True, capture_output=True, text=True).stdout.strip())
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(f), "-vf", "crop=2:2", "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         check=True, capture_output=True).stdout
    return raw[0], raw[1], raw[2], h


class _EdlReads:
    """Counts every read of an edl.json while installed."""

    def __init__(self, monkeypatch):
        self.paths: list[str] = []
        real = pathlib.Path.read_text

        def spy(p, *a, **kw):
            if p.name == "edl.json":
                self.paths.append(str(p))
            return real(p, *a, **kw)
        monkeypatch.setattr(pathlib.Path, "read_text", spy)


def _poster_row(client: TestClient, sid: str) -> dict:
    return next(s for s in client.get("/api/sessions").json()["sessions"] if s["id"] == sid)


def _add(client: TestClient, sid: str, src: Path, start: float = 0.0) -> None:
    r = client.post(f"/api/sessions/{sid}/dispatch", json={
        "tool": "add_clip", "args": {"track": "v1", "src": str(src), "in": 0.0, "out": 3.0, "start": start}})
    assert r.status_code == 200, r.text


def test_poster_is_the_first_video_frame_and_follows_every_edl_change(client, media, tmp_path, monkeypatch):
    sid = client.post("/api/sessions", json={"name": "Poster test"}).json()["id"]

    # An empty project: the first ask derives "nothing", after which the
    # listing knows not to offer an image at all.
    row = _poster_row(client, sid)
    assert row["poster"] and row["poster"].startswith(f"/api/sessions/{sid}/poster?v=")
    assert client.get(row["poster"]).status_code == 204
    assert _poster_row(client, sid)["poster"] is None

    _add(client, sid, media["blue"])
    url_blue = _poster_row(client, sid)["poster"]
    r = client.get(url_blue)
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert "immutable" in r.headers["cache-control"]
    red, green, blue, height = _center_rgb(r.content, tmp_path)
    assert height == 72 and blue > 150 and red < 80, (red, green, blue)

    # A cached poster costs no EDL read; neither does the listing.
    reads = _EdlReads(monkeypatch)
    assert client.get(url_blue).status_code == 200
    client.get("/api/sessions")
    assert reads.paths == []

    # A new first clip changes the poster: add red after blue, then ripple-
    # delete blue so red moves to the front.
    _add(client, sid, media["red"], start=3.0)
    clips = client.get(f"/api/sessions/{sid}/edl").json()["tracks"][0]["clips"]
    blue_id = next(c["id"] for c in clips if Path(c["src"]).name == "blue.mp4")
    assert client.post(f"/api/sessions/{sid}/dispatch", json={
        "tool": "ripple_delete", "args": {"clip_id": blue_id}}).status_code == 200
    reads.paths.clear()   # the edits themselves read nothing we care about here
    first = client.get(f"/api/sessions/{sid}/edl").json()["tracks"][0]["clips"]
    assert Path(min(first, key=lambda c: c["start"])["src"]).name == "red.mp4"
    url_red = _poster_row(client, sid)["poster"]
    assert url_red != url_blue
    r, g, b, _ = _center_rgb(client.get(url_red).content, tmp_path)
    assert r > 150 and b < 80, (r, g, b)
    assert len(reads.paths) == 1          # re-derived once, for the one changed project

    # Undo writes edl.json without commit(): the stamp still catches it
    # (blue is back at the front).
    assert client.post(f"/api/sessions/{sid}/dispatch", json={"tool": "undo", "args": {}}).status_code == 200
    url_undo = _poster_row(client, sid)["poster"]
    assert url_undo not in (url_red,)
    r, g, b, _ = _center_rgb(client.get(url_undo).content, tmp_path)
    assert b > 150 and r < 80, (r, g, b)

    # A stale `v` still gets the right picture, just not an immutable one.
    stale = client.get(url_red)
    assert stale.status_code == 200 and stale.headers["cache-control"] == "no-cache"


def test_listing_many_projects_reads_no_edl(client, media, monkeypatch):
    for i in range(6):
        sid = client.post("/api/sessions", json={"name": f"P{i}"}).json()["id"]
        _add(client, sid, media["blue"])
    reads = _EdlReads(monkeypatch)
    sessions = client.get("/api/sessions").json()["sessions"]
    assert len(sessions) == 6 and all(s["poster"] for s in sessions)
    assert reads.paths == []


def test_poster_rejects_a_bad_id_and_answers_204_for_offline_media(client, media, tmp_path, workdir):
    assert client.get("/api/sessions/s_nothere999/poster").status_code == 404
    assert client.get("/api/sessions/..%2F..%2Fetc/poster").status_code in (400, 404)
    gone = tmp_path / "gone.mp4"
    gone.write_bytes(media["blue"].read_bytes())
    sid = client.post("/api/sessions", json={"name": "Offline"}).json()["id"]
    _add(client, sid, gone)
    gone.unlink()
    assert client.get(_poster_row(client, sid)["poster"]).status_code == 204
