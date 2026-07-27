"""GET /api/sessions/{sid}/files/{kind}/{name:path} — subpath serving + traversal guard.

The route param became {name:path} so StickerLayer's
uploads/stickers/<file> URLs resolve (they 404'd with a single-segment
{name}, which made mid-drag sticker feedback fall back to the translucent
white circle). With the router no longer rejecting slashes, the handler's
containment check is the only traversal guard — these tests pin it.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor import storage as _storage
from video_ai_editor import main as _main
from video_ai_editor.main import app


@pytest.fixture()
def client_and_sid(tmp_path: Path, monkeypatch):
    # Same isolation pattern as test_thumbs_endpoint.py: WORKDIR is imported
    # by value into storage/main, so patch both modules (no reloads — a
    # reload would leak env-driven state into later tests in this process).
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    _main._STORES.clear()

    sid = _storage.new_session_id()
    sd = _storage.session_dir(sid)
    (sd / "uploads" / "stickers").mkdir(parents=True)
    (sd / "uploads" / "stickers" / "smile.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    (sd / "uploads" / "clip").mkdir(parents=True)
    (sd / "uploads" / "clip" / "clip.normalized.mp4").write_bytes(b"mp4")
    (sd / "snapshots").mkdir(exist_ok=True)
    (sd / "snapshots" / "secret.json").write_text("{}")

    # A sibling session whose dir name EXTENDS sid — the old bare
    # startswith(str(session_dir)) guard treated it as inside sid.
    evil = tmp_path / (sd.name + "x")
    (evil / "uploads").mkdir(parents=True)
    (evil / "uploads" / "other.mp4").write_bytes(b"leak")

    with TestClient(app) as client:
        yield client, sid, sd.name + "x"


def test_subpath_name_is_served(client_and_sid):
    client, sid, _ = client_and_sid
    r = client.get(f"/api/sessions/{sid}/files/uploads/stickers/smile.png")
    assert r.status_code == 200
    assert r.content.startswith(b"\x89PNG")


def test_bare_name_fallback_still_works(client_and_sid):
    client, sid, _ = client_and_sid
    r = client.get(f"/api/sessions/{sid}/files/uploads/clip.normalized.mp4")
    assert r.status_code == 200
    assert r.content == b"mp4"


def test_cross_kind_hop_is_forbidden(client_and_sid):
    client, sid, _ = client_and_sid
    # TestClient/Starlette normalize literal "../" — use encoded dots so the
    # hostile name reaches the handler, which must reject it itself.
    r = client.get(f"/api/sessions/{sid}/files/uploads/%2e%2e/snapshots/secret.json")
    assert r.status_code in (403, 404)


def test_sibling_session_prefix_is_forbidden(client_and_sid):
    client, sid, evil_name = client_and_sid
    r = client.get(
        f"/api/sessions/{sid}/files/uploads/%2e%2e/%2e%2e/{evil_name}/uploads/other.mp4"
    )
    assert r.status_code in (403, 404)


def test_malformed_sid_rejected_before_fs(client_and_sid):
    client, _, _ = client_and_sid
    r = client.get("/api/sessions/not-a-sid/files/uploads/x.png")
    assert r.status_code == 400


def test_unknown_kind_rejected(client_and_sid):
    client, sid, _ = client_and_sid
    r = client.get(f"/api/sessions/{sid}/files/cache/x.png")
    assert r.status_code == 404
