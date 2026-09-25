"""QA-098: one export file per project + settings, named after the project.

Before: every export of a timeline wrote `export_<edl hash>.mp4`. A 480p export
overwrote the 1080p one, two concurrent requests at different sizes both got
the same path (the 1080p caller received an 854x480 file) and the user saved a
file named by a hash. Real exports over HTTP, dimensions read with ffprobe.
"""
from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from video_ai_editor.api.hardening import RATE
from video_ai_editor.main import app
from video_ai_editor.render.compositor import export_filename


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    from video_ai_editor import main as _main, storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    RATE.windows.clear()
    _main._STORES.clear()
    return TestClient(app)


@pytest.fixture(scope="module")
def src(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("media") / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=3",
         "-f", "lavfi", "-i", "sine=f=440:duration=3", "-c:v", "libx264",
         "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
         str(p)], check=True, capture_output=True)
    return p


def _dims(p: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "json", str(p)],
        check=True, capture_output=True, text=True).stdout
    s = json.loads(out)["streams"][0]
    return int(s["width"]), int(s["height"])


def _session(c: TestClient, src: Path, name: str) -> str:
    sid = c.post("/api/sessions", json={"name": name}).json()["id"]
    r = c.post(f"/api/sessions/{sid}/dispatch", json={
        "tool": "add_clip", "args": {"track": "v1", "src": str(src), "in": 0.0,
                                     "out": 3.0, "start": 0.0}})
    assert r.status_code == 200, r.text
    return sid


def test_concurrent_exports_at_different_sizes_each_get_their_own_file(client, src):
    c = client
    sid = _session(c, src, "Goa Trip")
    results: dict[int, dict] = {}

    def _export(height: int) -> None:
        r = c.post(f"/api/sessions/{sid}/export", json={"height": height})
        assert r.status_code == 200, r.text
        results[height] = r.json()

    ths = [threading.Thread(target=_export, args=(h,)) for h in (360, 180)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=300)

    big, small = results[360], results[180]
    assert big["filename"] != small["filename"], big["filename"]
    # The default canvas is 9:16, so "360" names the short side: 360x640.
    assert _dims(Path(big["path"])) == (360, 640)
    assert _dims(Path(small["path"])) == (180, 320)
    # Named after the project and the settings, not a hash.
    assert big["filename"] == "Goa Trip 360x640 30fps q18.mp4"
    assert small["filename"].startswith("Goa Trip 180x320 ")

    # The URL downloads THAT file, under that name.
    r = c.get(big["url"])
    assert r.status_code == 200
    assert "Goa%20Trip%20360x640" in r.headers["content-disposition"] or \
        "Goa Trip 360x640" in unquote(r.headers["content-disposition"])
    assert len(r.content) == Path(big["path"]).stat().st_size


def test_quality_and_container_variants_do_not_overwrite_each_other(client, src):
    c = client
    sid = _session(c, src, "Promo")
    a = c.post(f"/api/sessions/{sid}/export", json={"height": 180, "crf": 18}).json()
    b = c.post(f"/api/sessions/{sid}/export", json={"height": 180, "crf": 30}).json()
    m = c.post(f"/api/sessions/{sid}/export", json={"height": 180, "container": "mov"}).json()
    assert len({a["filename"], b["filename"], m["filename"]}) == 3
    for x in (a, b, m):
        assert Path(x["path"]).exists()
    assert m["filename"].endswith(".mov")
    # Re-exporting identical settings is the same deliverable: same name.
    again = c.post(f"/api/sessions/{sid}/export", json={"height": 180, "crf": 18}).json()
    assert again["filename"] == a["filename"]


def test_export_filename_is_safe_for_any_project_name():
    name = export_filename('a/b\\c:d*e?f"g<h>i|j#k%l', 1920, 1080, 30000 / 1001,
                           crf=18, bitrate_kbps=None, ext="mp4")
    assert "/" not in name and "\\" not in name and "#" not in name and "%" not in name
    assert name.endswith(" 1920x1080 29.97fps q18.mp4")
    long_hi = export_filename("नमस्ते " * 80, 1080, 1920, 30, crf=18,
                              bitrate_kbps=8000, ext="mov")
    assert len(long_hi.encode("utf-8")) <= 255
    assert long_hi.endswith(" 1080x1920 30fps 8000kbps.mov")
    assert export_filename("   ", 2, 2, 30, crf=18, bitrate_kbps=None, ext="mp4") \
        == "Export 2x2 30fps q18.mp4"
    assert not export_filename("..hidden", 2, 2, 30, crf=18, bitrate_kbps=None,
                               ext="mp4").startswith(".")
