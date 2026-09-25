"""QA-033: a saved .vae reopens with its transcript and its undo history.

Real round trip through the HTTP routes: upload a clip, import an .srt (the
hand-corrected transcript), edit, Save, Open the saved bytes as a new session,
then ask the NEW session for its transcript and press Undo.
"""
from __future__ import annotations

import importlib
import io
import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def api(monkeypatch, tmp_path: Path):
    from video_ai_editor import storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path / "wd")
    from video_ai_editor import main as _main
    importlib.reload(_main)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path / "wd")
    _main._STORES.clear()
    return TestClient(_main.app)


def _clip(path: Path) -> Path:
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=160x120:r=25:d=4",
                    "-f", "lavfi", "-i", "sine=f=440:d=4", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
                   check=True, capture_output=True)
    return path


def _dispatch(api, sid, tool, **args):
    r = api.post(f"/api/sessions/{sid}/dispatch", json={"tool": tool, "args": args})
    assert r.status_code == 200, r.text
    return r.json()


def _save_and_open(api, sid) -> str:
    saved = api.post(f"/api/sessions/{sid}/save_project")
    assert saved.status_code == 200, saved.text
    blob = api.get(saved.json()["url"]).content
    r = api.post("/api/load_project",
                 files={"file": ("project.vae", io.BytesIO(blob), "application/zip")})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _project(api, tmp_path) -> str:
    sid = api.post("/api/sessions").json()["id"]
    with _clip(tmp_path / "talk.mp4").open("rb") as fh:
        r = api.post(f"/api/sessions/{sid}/upload",
                     files={"file": ("talk.mp4", fh, "video/mp4")},
                     data={"add_to_timeline": "true", "transcribe": "false"})
    assert r.status_code == 200, r.text
    return sid


def test_opened_project_keeps_an_imported_transcript_and_can_undo(api, tmp_path):
    sid = _project(api, tmp_path)
    srt = b"1\n00:00:00,500 --> 00:00:02,000\nHello corrected world\n"
    up = api.post(f"/api/sessions/{sid}/subtitle_upload",
                  files={"file": ("fixed.srt", io.BytesIO(srt), "text/plain")})
    assert up.status_code == 200, up.text
    _dispatch(api, sid, "import_srt", path=up.json()["path"])
    _dispatch(api, sid, "split_at", time=2.0)
    edl_after = api.get(f"/api/sessions/{sid}/edl").json()
    v1_after = next(t for t in edl_after["tracks"] if t["id"] == "v1")["clips"]
    assert len(v1_after) == 2

    new = _save_and_open(api, sid)
    assert new != sid

    tx = api.get(f"/api/sessions/{new}/transcript").json()
    text = " ".join(seg["text"] for seg in tx.get("segments", []))
    assert "Hello corrected world" in text, tx

    undo = _dispatch(api, new, "undo")
    assert undo["result"]["ok"] is True, undo
    v1 = next(t for t in api.get(f"/api/sessions/{new}/edl").json()["tracks"]
              if t["id"] == "v1")["clips"]
    assert len(v1) == 1, "undo after reopening must restore the pre-split clip"
    # The restored state still points at the NEW session's media, not the old one.
    assert f"/{new}/" in v1[0]["src"].replace("\\", "/")
    assert Path(v1[0]["src"]).exists()

    redo = _dispatch(api, new, "redo")
    assert redo["result"]["ok"] is True


def test_opened_project_keeps_the_whisper_transcript_of_its_upload(api, tmp_path):
    sid = _project(api, tmp_path)
    v1 = next(t for t in api.get(f"/api/sessions/{sid}/edl").json()["tracks"]
              if t["id"] == "v1")["clips"]
    ingest = Path(v1[0]["src"]).parent / "ingest.json"
    data = json.loads(ingest.read_text(encoding="utf-8"))
    data["transcript"] = {"language": "en", "duration": 4.0, "segments": [
        {"id": 0, "start": 0.4, "end": 1.9, "text": "whisper heard this",
         "words": [{"start": 0.4, "end": 0.9, "word": "whisper"},
                   {"start": 0.9, "end": 1.4, "word": "heard"},
                   {"start": 1.4, "end": 1.9, "word": "this"}]}]}
    ingest.write_text(json.dumps(data), encoding="utf-8")

    new = _save_and_open(api, sid)
    tx = api.get(f"/api/sessions/{new}/transcript").json()
    assert [s["text"] for s in tx["segments"]] == ["whisper heard this"]
    assert tx["segments"][0]["words"][1]["start"] == 0.9
