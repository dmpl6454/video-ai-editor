"""GET /api/sessions/{sid}/preview_loudness?h= (INSTANT_PREVIEW_SPEC §3.6, §5.2, §7).

Final QA r3: Instant preview played the raw mix, ~11 dB under the server
preview and the export on a project with the default −16 LUFS target, and
called it EXACT. The route hands the client the gain the server preview
applies (render/preview_loudness), and says whether it was measured for the
sound of THIS render (`current`) or is the session's last-known gain.

Real ffmpeg: the gain the route names is checked against the level of the
preview file ffmpeg actually rendered.
"""
from __future__ import annotations

import math
import subprocess
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from video_ai_editor.api import hardening
from video_ai_editor.ingest.proxy_queue import MANAGER
from video_ai_editor.main import app

from test_preview_loudness import _talk


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    from video_ai_editor import main as _main, storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    monkeypatch.delenv("VAI_PREVIEW_ENGINE", raising=False)
    monkeypatch.delenv("VAI_PROXY_EAGER", raising=False)
    _main._STORES.clear()
    hardening.RATE.windows.clear()
    c = TestClient(app)
    yield c
    MANAGER.wait_idle(30)


def _dispatch(client, sid: str, tool: str, **args) -> dict:
    r = client.post(f"/api/sessions/{sid}/dispatch", params={"include": "edl"},
                    json={"tool": tool, "args": args})
    assert r.status_code == 200, r.text
    return r.json()


def _rms(path: Path, t0: float, t1: float) -> float:
    pcm = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", str(t0), "-t", str(t1 - t0), "-i", str(path),
                          "-map", "0:a:0", "-ac", "1", "-ar", "48000", "-f", "f32le", "pipe:1"],
                         capture_output=True, check=True).stdout
    x = np.frombuffer(pcm, dtype=np.float32).astype(np.float64)
    return float(np.sqrt(np.mean(x * x)))


def _loudness(client, sid: str, h: str):
    return client.get(f"/api/sessions/{sid}/preview_loudness", params={"h": h})


def test_the_route_names_the_gain_the_server_preview_plays(client, tmp_path):
    sid = client.post("/api/sessions").json()["id"]
    # −33 dBFS talk under the default −16 LUFS target: a ~+17 dB lift
    src = tmp_path / sid / "uploads" / "talk" / "talk.mov"
    src.parent.mkdir(parents=True)
    _talk(src, peak=0.022)
    _dispatch(client, sid, "set_canvas", w=320, h=180, fps=30)
    h = _dispatch(client, sid, "add_clip", src=str(src), track="v1", start=0, **{"in": 0, "out": 6.0})["render_hash"]

    # Nothing measured yet in this session: no gain to give, not current.
    r = _loudness(client, sid, h)
    assert r.status_code == 200, r.text
    assert r.json() == {"gain_db": None, "current": False, "target_lufs": -16.0}

    # The server preview of this hash measures it: the gain is current, and
    # it is the gain the preview file carries over the raw sound (the
    # client's master gain; the spec's live-sink bar, 0.5 dB).
    res = client.post(f"/api/sessions/{sid}/preview")
    assert res.status_code == 200, res.text
    body = _loudness(client, sid, h).json()
    assert body["current"] is True and body["target_lufs"] == -16.0
    assert isinstance(body["gain_db"], float) and body["gain_db"] > 10, body
    preview = next((tmp_path / sid).rglob(f"{h}.mp4"))
    carried = 20 * math.log10(_rms(preview, 0.5, 5.5) / _rms(src, 0.5, 5.5))
    assert abs(carried - body["gain_db"]) <= 0.5, (carried, body)

    # A title changes the render hash, not the sound: still current.
    h2 = _dispatch(client, sid, "add_text", text="Hello", start=0, end=1)["render_hash"]
    assert h2 != h
    assert _loudness(client, sid, h2).json() == body

    # A louder clip is a new sound: the last-known gain, not current (APPROX
    # on the client until the next preview render measures it).
    clip = next(t for t in _dispatch(client, sid, "get_timeline")["edl"]["tracks"] if t["id"] == "v1")["clips"][0]
    h3 = _dispatch(client, sid, "set_volume", target=clip["id"], db=6)["render_hash"]
    stale = _loudness(client, sid, h3).json()
    assert stale["current"] is False and stale["gain_db"] == pytest.approx(body["gain_db"], abs=0.01)

    # An older hash than the session's: 409, like /frame_map.
    r = _loudness(client, sid, h)
    assert r.status_code == 409 and r.json()["error"]["details"]["render_hash"] == h3

    # No loudness target: nothing to apply, and nothing to wait for.
    h4 = _dispatch(client, sid, "set_loudness_target", lufs=None)["render_hash"]
    assert _loudness(client, sid, h4).json() == {"gain_db": None, "current": True, "target_lufs": None}


def test_the_route_refuses_a_malformed_hash(client):
    sid = client.post("/api/sessions").json()["id"]
    assert _loudness(client, sid, "../x").status_code == 400
