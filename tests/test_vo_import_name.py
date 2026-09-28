"""Final QA: an imported voiceover file was renamed `vo_<timestamp>.m4a` in the
media library, on the timeline clip and in the Inspector header — the server
named the stored take `vo_<secs>.m4a` and never kept the picked file's name
(and the frontend renamed every upload to `vo.wav` besides). A picked file
now keeps its own name; a live recording (the recorder's generic `vo.webm`)
keeps the generated one."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor import platformutil as _pu


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from video_ai_editor import config, main as _main, storage as _storage
    from video_ai_editor.api.hardening import RATE
    wd = tmp_path / "wd"
    for mod in (config, _storage, _main):
        monkeypatch.setattr(mod, "WORKDIR", wd, raising=False)
    wd.mkdir()
    RATE.windows.clear()
    _main._STORES.clear()
    return TestClient(_main.app)


def _wav(path: Path) -> Path:
    subprocess.run([_pu.FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=330:duration=1:sample_rate=48000", str(path)], check=True)
    return path


def _names(c: TestClient, sid: str) -> list[str]:
    return [r["name"] for r in c.get(f"/api/sessions/{sid}/media").json()["media"]]


def test_a_picked_voiceover_file_keeps_its_name(client, tmp_path):
    sid = client.post("/api/sessions", json={"name": "vo"}).json()["id"]
    take = _wav(tmp_path / "voiceover.wav")
    with take.open("rb") as f:
        r = client.post(f"/api/sessions/{sid}/vo_record",
                        files={"file": ("voiceover.wav", f, "audio/wav")}, data={"start": "0"})
    assert r.status_code == 200, r.text
    assert r.json().get("display_name") == "voiceover.wav"
    assert "voiceover.wav" in _names(client, sid)


def test_a_live_recording_keeps_the_generated_name(client, tmp_path):
    sid = client.post("/api/sessions", json={"name": "vo"}).json()["id"]
    take = _wav(tmp_path / "x.wav")
    with take.open("rb") as f:
        r = client.post(f"/api/sessions/{sid}/vo_record",
                        files={"file": ("vo.webm", f, "audio/webm")}, data={"start": "0"})
    assert r.status_code == 200, r.text
    assert r.json().get("display_name") is None
    assert not any(n in ("vo.webm", "vo.wav") for n in _names(client, sid))


def test_the_voiceover_history_line_names_the_rulers_time(client, tmp_path):
    """Final QA: after v1 dissolves the EDL `start` is not where a clip plays.
    A voiceover imported at the playhead 06:00 (layout 6.5 behind one 0.5 s
    dissolve) read "Voiceover 7.0s @ 6.5s" in History — 00:00:06:15 — for a
    voiceover the ruler and the export put at 6.000 s."""
    from video_ai_editor import main as _main
    from video_ai_editor.edl.schema import Clip, Transition
    sid = client.post("/api/sessions", json={"name": "vo"}).json()["id"]
    store = _main._store(sid)
    v1 = store.edl.get_track("v1")
    v1.clips[:] = [Clip(src=str(tmp_path / f"{n}.mp4"), in_=0.0, out=5.0, start=5.0 * i, id=n)
                   for i, n in enumerate(("beach", "city"))]
    v1.transitions[:] = [Transition(at=5.0, type="dissolve", duration=0.5)]
    take = _wav(tmp_path / "voiceover.wav")
    with take.open("rb") as f:
        r = client.post(f"/api/sessions/{sid}/vo_record",
                        files={"file": ("voiceover.wav", f, "audio/wav")}, data={"start": "6.5"})
    assert r.status_code == 200, r.text
    clip = next(c for t in store.edl.tracks for c in t.clips if getattr(c, "id", "") == r.json()["clip_id"])
    assert clip.start == pytest.approx(6.5)          # the EDL keeps layout time
    assert "@ 6.00s" in r.json()["summary"], r.json()["summary"]
