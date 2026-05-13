"""End-to-end API tests + production hardening checks.

Uses FastAPI's TestClient (in-process, no real socket). Verifies:
  - every documented endpoint is reachable
  - request IDs are echoed back
  - error envelope is consistent across exception types
  - /livez, /readyz, /metrics behave as documented
  - rate limiter trips at the right threshold
"""
from __future__ import annotations
import io
import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor.main import app
from video_ai_editor.api.hardening import RATE


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """TestClient pinned at a tmp WORKDIR so we don't pollute the user's
    real workdir while running tests."""
    from video_ai_editor import storage as _storage, main as _main
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    # Reset rate limiter so test order doesn't matter.
    RATE.windows.clear()
    # Reset session cache so a fresh tmp path doesn't see old in-memory stores.
    _main._STORES.clear()
    return TestClient(app)


def _make_video(p: Path, *, dur: float = 1.0):
    keyed = p.with_suffix(".keyed.mp4")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                    "-i", f"color=c=blue:s=320x180:d={dur}:r=30",
                    "-pix_fmt", "yuv420p", str(keyed)],
                   check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-i", str(keyed),
                    "-f", "lavfi", "-i", f"sine=f=440:duration={dur}",
                    "-c:v", "copy", "-c:a", "aac", "-shortest", str(p)],
                   check=True, capture_output=True)


# -------------------------------------------------------------------------
# Health + metrics

def test_livez_returns_200(client):
    r = client.get("/livez")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_readyz_returns_200_with_ffmpeg(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json().get("ok") is True
    assert "ffmpeg" in r.json()


def test_metrics_returns_prom_text(client):
    # Trigger at least one request so metrics exist.
    client.get("/livez")
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    # Should have per-path request counters.
    assert "vai_http_requests_total" in body
    assert "vai_http_request_duration_seconds_bucket" in body


# -------------------------------------------------------------------------
# Request ID + error envelope

def test_request_id_echoed(client):
    r = client.get("/api/health", headers={"X-Request-ID": "abc-test-123"})
    assert r.headers.get("X-Request-ID") == "abc-test-123"


def test_request_id_generated_when_missing(client):
    r = client.get("/api/health")
    assert r.headers.get("X-Request-ID")  # generated


def test_error_envelope_for_404(client):
    r = client.get("/api/sessions/nonexistent")
    assert r.status_code == 404
    body = r.json()
    assert "error" in body
    err = body["error"]
    assert err["code"] == "NOT_FOUND"
    assert "request_id" in err
    assert err["message"]


def test_error_envelope_for_validation(client):
    # Empty body → validation error
    r = client.post("/api/sessions/abc/dispatch", json={})
    assert r.status_code in (400, 422)
    err = r.json().get("error", {})
    assert err.get("code") in {"VALIDATION_ERROR", "BAD_REQUEST",
                                "UNPROCESSABLE", "NOT_FOUND"}


def test_error_envelope_for_value_error(client):
    """Tools that raise ValueError land in our handler with BAD_REQUEST + envelope."""
    sid = client.post("/api/sessions").json()["id"]
    r = client.post(f"/api/sessions/{sid}/dispatch",
                    json={"tool": "trim_clip",
                          "args": {"clip_id": "doesnt-exist", "in": 0.0, "out": 1.0}})
    assert r.status_code in (400, 422)
    err = r.json().get("error", {})
    assert err.get("request_id")


# -------------------------------------------------------------------------
# Rate limiter

def test_rate_limit_trips_at_threshold(client, monkeypatch):
    monkeypatch.setattr(RATE, "default_rps", 5.0)
    RATE.windows.clear()
    statuses = [client.get("/api/health").status_code for _ in range(20)]
    assert 429 in statuses, f"rate limit never tripped: {statuses}"


# -------------------------------------------------------------------------
# Endpoints

def test_full_flow_upload_render_export(client, tmp_path: Path):
    src = tmp_path / "v.mp4"
    _make_video(src, dur=2.0)

    sid = client.post("/api/sessions").json()["id"]
    assert sid

    # Upload
    with src.open("rb") as f:
        r = client.post(
            f"/api/sessions/{sid}/upload",
            files={"file": ("v.mp4", f, "video/mp4")},
            data={"add_to_timeline": "true", "transcribe": "false"},
        )
    assert r.status_code == 200
    assert r.json().get("edl_hash")

    # EDL fetch
    edl = client.get(f"/api/sessions/{sid}/edl").json()
    assert edl["tracks"][0]["clips"][0]["src"]

    # Preview render
    pv = client.post(f"/api/sessions/{sid}/preview").json()
    assert pv.get("edl_hash")
    mp4 = client.get(f"/api/sessions/{sid}/preview.mp4?h={pv['edl_hash']}")
    assert mp4.status_code == 200 and len(mp4.content) > 1024

    # Dispatch a tool
    r = client.post(f"/api/sessions/{sid}/dispatch",
                    json={"tool": "set_speed",
                          "args": {"clip_id": edl["tracks"][0]["clips"][0]["id"],
                                   "factor": 1.5}})
    assert r.status_code == 200

    # Ops log records the change
    ops = client.get(f"/api/sessions/{sid}/ops").json()
    assert len(ops.get("ops", [])) >= 1


def test_audio_upload_endpoint(client, tmp_path: Path):
    """Already-covered by test_audio_import_preview, but here we hit the API
    once more to confirm the endpoint shape stays stable."""
    src = tmp_path / "music.mp3"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                    "-i", "sine=f=200:duration=2",
                    "-c:a", "mp3", str(src)],
                   check=True, capture_output=True)
    sid = client.post("/api/sessions").json()["id"]
    with src.open("rb") as f:
        r = client.post(f"/api/sessions/{sid}/audio_upload",
                        files={"file": ("m.mp3", f, "audio/mpeg")},
                        data={"add_to_music": "true", "duck": "true"})
    assert r.status_code == 200
    assert r.json().get("duration", 0) > 1.5
