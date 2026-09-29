"""Every state-changing route answers only the editor's own page (SEC-SAME-ORIGIN).

The cross-site refusal in api/auth.PairAuthMiddleware only fired on
`Sec-Fetch-Site: cross-site`. A page on ANOTHER PORT of the same host (any dev
server the owner runs: http://127.0.0.1:3000, http://localhost:5173 of some
other project) is `same-site`, not `cross-site`, and a multipart/form-data or
text/plain POST is a CORS "simple request" — no preflight. So that page could
create a session, upload into it, dispatch edits, rename or delete projects.
Round 1 fixed /api/load_project alone; this is the one guard for all of them.

Rules under test (api/auth.same_origin, enforced in the middleware for every
method but GET/HEAD/OPTIONS):
  * with fetch metadata, only `same-origin` / `none` pass;
  * without it, a named Origin must equal the Host the request was sent to;
  * a native client (no Sec-Fetch-Site, no Origin) passes — the phone
    companion and MCP clients are not browsers;
  * the Vite dev proxy (Origin :5173, Host rewritten, `same-origin`) passes.

The route list is ENUMERATED from the live FastAPI app, so a route added later
is covered without anyone remembering to add it here.
"""
from __future__ import annotations

import io
import re
import struct
import subprocess
import wave
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Route

from lan_fixtures import lan_home, lan_on  # noqa: F401  (fixtures)
from test_load_project_by_content import client, lavfi_clip, workdir  # noqa: F401  (fixtures)

from video_ai_editor.main import app

GUARD_CODE = "CROSS_ORIGIN_WRITE"
_SAFE = {"GET", "HEAD", "OPTIONS"}
_PARAM = re.compile(r"\{([^}:]+)(?::[^}]+)?\}")
_FILL = {"sid": "s_00000000aa", "media_id": "m_nope", "job_id": "j_nope",
         "clip_id": "c_nope", "h": "0" * 16, "key": "0" * 16}

#: What a page on another local port looks like to this server.
HOSTILE = [
    pytest.param({"Sec-Fetch-Site": "same-site"}, id="same-site"),
    pytest.param({"Sec-Fetch-Site": "cross-site"}, id="cross-site"),
    pytest.param({"Origin": "http://127.0.0.1:9303", "Host": "127.0.0.1:8765"},
                 id="foreign-origin-no-fetch-metadata"),
    pytest.param({"Origin": "null", "Host": "127.0.0.1:8765"}, id="opaque-origin"),
    pytest.param({"Origin": "http://localhost:5173", "Host": "localhost:8765",
                  "Sec-Fetch-Site": "same-site"}, id="another-vite-project"),
]


def _write_routes() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for r in app.routes:
        if not isinstance(r, Route):
            continue
        for m in sorted((r.methods or set()) - _SAFE):
            out.append((m, _PARAM.sub(lambda g: _FILL.get(g.group(1), "x"), r.path)))
    return out


WRITE_ROUTES = _write_routes()


def _refused_by_guard(resp) -> bool:
    if resp.status_code != 403:
        return False
    try:
        return resp.json()["error"]["code"] == GUARD_CODE
    except (ValueError, KeyError, TypeError):
        return False


def test_the_enumeration_sees_every_kind_of_write():
    """If this shrinks, the enumeration broke, not the app."""
    paths = {p for _, p in WRITE_ROUTES}
    methods = {m for m, _ in WRITE_ROUTES}
    assert methods >= {"POST", "PATCH", "DELETE", "PUT"}
    for must in ("/api/sessions", "/api/load_project", "/mcp",
                 "/api/sessions/s_00000000aa/upload",
                 "/api/sessions/s_00000000aa/audio_upload",
                 "/api/sessions/s_00000000aa/vo_record",
                 "/api/sessions/s_00000000aa/sticker_upload",
                 "/api/sessions/s_00000000aa/lut_upload",
                 "/api/sessions/s_00000000aa/subtitle_upload",
                 "/api/sessions/s_00000000aa/canvas-bg/upload",
                 "/api/sessions/s_00000000aa/dispatch",
                 "/api/sessions/s_00000000aa/voice/preview"):
        assert must in paths, must
    assert len(WRITE_ROUTES) >= 35


@pytest.mark.parametrize("headers", HOSTILE)
def test_every_write_route_refuses_a_foreign_page(client, workdir, headers):  # noqa: F811
    before = {p.name for p in workdir.iterdir()}
    let_through = []
    for method, path in WRITE_ROUTES:
        r = client.request(method, path, headers=headers)
        if not _refused_by_guard(r):
            let_through.append((method, path, r.status_code, r.text[:120]))
    assert not let_through, let_through
    assert {p.name for p in workdir.iterdir()} == before, "a refused request wrote to WORKDIR"


@pytest.mark.parametrize("headers", [
    pytest.param({"Sec-Fetch-Site": "same-origin"}, id="app-window"),
    pytest.param({"Sec-Fetch-Site": "none"}, id="top-level"),
    pytest.param({"Origin": "http://localhost:5173", "Host": "127.0.0.1:8765",
                  "Sec-Fetch-Site": "same-origin"}, id="vite-dev-proxy"),
    pytest.param({"Origin": "http://127.0.0.1:8765", "Host": "127.0.0.1:8765"},
                 id="own-origin-no-fetch-metadata"),
    pytest.param({}, id="native-client"),
])
def test_every_write_route_still_reaches_its_handler_from_the_app(client, headers):  # noqa: F811
    """No body and a session that does not exist, so each handler answers with
    its own 404/415/422 — anything but the guard's refusal."""
    blocked = [(m, p) for m, p in WRITE_ROUTES
               if _refused_by_guard(client.request(m, p, headers=headers))]
    assert not blocked, blocked


def test_the_refusal_is_the_apps_error_envelope(client):  # noqa: F811
    r = client.post("/api/sessions", headers={"Sec-Fetch-Site": "same-site"})
    body = r.json()["error"]
    assert r.status_code == 403 and body["code"] == GUARD_CODE
    assert "editor" in body["message"].lower() and body["request_id"]


# --- the uploads, end to end ----------------------------------------------------

def _png() -> bytes:
    raw = b"\x00\xff\x00\x00\xff"                     # 1x1 RGBA, filter byte 0
    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _wav(seconds: float = 0.5) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x10" * int(16000 * seconds))
    return buf.getvalue()


_CUBE = b"LUT_3D_SIZE 2\n" + b"".join(
    f"{r} {g} {b}\n".encode() for b in (0, 1) for g in (0, 1) for r in (0, 1))
_SRT = b"1\n00:00:00,000 --> 00:00:01,000\nhello\n"


def _uploads(sid: str, clip: Path) -> list[tuple[str, dict, dict]]:
    base = f"/api/sessions/{sid}"
    return [
        (f"{base}/upload", {"file": ("clip.mp4", clip.read_bytes(), "video/mp4")},
         {"add_to_timeline": "true", "transcribe": "false"}),
        (f"{base}/audio_upload", {"file": ("tone.wav", _wav(), "audio/wav")}, {}),
        (f"{base}/vo_record", {"file": ("take.wav", _wav(), "audio/wav")}, {}),
        (f"{base}/sticker_upload", {"file": ("dot.png", _png(), "image/png")}, {}),
        (f"{base}/lut_upload", {"file": ("id.cube", _CUBE, "text/plain")}, {}),
        (f"{base}/subtitle_upload", {"file": ("s.srt", _SRT, "text/plain")}, {}),
        (f"{base}/canvas-bg/upload", {"file": ("bg.png", _png(), "image/png")}, {}),
    ]


def _uploads_dir_files(workdir: Path, sid: str) -> set[str]:
    return {str(p.relative_to(workdir)) for p in (workdir / sid).rglob("*") if p.is_file()}


def test_a_foreign_page_cannot_upload_into_a_session(client, workdir, lavfi_clip):  # noqa: F811
    sid = client.post("/api/sessions", headers={"Sec-Fetch-Site": "same-origin"}).json()["id"]
    before = _uploads_dir_files(workdir, sid)
    head_before = client.get(f"/api/sessions/{sid}/edl").json()
    for url, files, data in _uploads(sid, lavfi_clip):
        for hostile in ({"Sec-Fetch-Site": "same-site"},
                        {"Origin": "http://127.0.0.1:9303", "Host": "127.0.0.1:8765"}):
            r = client.post(url, headers=hostile, files=files, data=data)
            assert _refused_by_guard(r), (url, hostile, r.status_code, r.text[:200])
    assert _uploads_dir_files(workdir, sid) == before
    assert client.get(f"/api/sessions/{sid}/edl").json() == head_before


def test_the_apps_own_page_still_uploads_everything(client, workdir, lavfi_clip):  # noqa: F811
    own = {"Sec-Fetch-Site": "same-origin"}
    sid = client.post("/api/sessions", headers=own).json()["id"]
    for url, files, data in _uploads(sid, lavfi_clip):
        r = client.post(url, headers=own, files=files, data=data)
        assert r.status_code == 200, (url, r.status_code, r.text[:300])
    # And the rest of a session's life: rename, edit, clear, delete.
    assert client.patch(f"/api/sessions/{sid}", headers=own,
                        json={"name": "Mine"}).status_code == 200
    r = client.post(f"/api/sessions/{sid}/dispatch", headers=own,
                    json={"tool": "add_text", "args": {"text": "hi", "start": 0, "end": 1}})
    assert r.status_code == 200, r.text[:300]
    assert client.delete(f"/api/sessions/{sid}/render-cache", headers=own).status_code == 200
    assert client.delete(f"/api/sessions/{sid}", headers=own).status_code == 200


def test_a_foreign_page_cannot_rename_or_delete_a_session(client):  # noqa: F811
    sid = client.post("/api/sessions").json()["id"]
    hostile = {"Sec-Fetch-Site": "same-site"}
    assert _refused_by_guard(client.patch(f"/api/sessions/{sid}", headers=hostile,
                                          json={"name": "pwned"}))
    assert _refused_by_guard(client.delete(f"/api/sessions/{sid}", headers=hostile))
    assert client.get(f"/api/sessions/{sid}").status_code == 200


# --- the phone companion, when its flag is on -----------------------------------

def test_paired_phone_writes_still_work_with_the_flag_on(monkeypatch, lan_on):
    """A native client sends neither fetch metadata nor an Origin, so the guard
    must not stand between a paired phone and its writes."""
    from lan_fixtures import CLIENT_HEADERS, lan_peer, pair_a_device
    token = pair_a_device()
    r = lan_peer().post("/api/sessions",
                        headers={**CLIENT_HEADERS, "Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    # The desktop's own Phone panel mints codes from its own page ...
    own = TestClient(app).post("/api/pair/new", headers={"Sec-Fetch-Site": "same-origin"})
    assert own.status_code == 200, own.text
    # ... and a page on another port cannot.
    foreign = TestClient(app).post("/api/pair/new", headers={"Sec-Fetch-Site": "same-site"})
    assert _refused_by_guard(foreign), foreign.text


def test_pair_routes_stay_closed_with_the_flag_off(client, monkeypatch):  # noqa: F811
    monkeypatch.delenv("VAE_PHONE_PAIRING", raising=False)
    monkeypatch.delenv("VAE_LAN", raising=False)
    r = client.post("/api/pair/new", headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 404


def test_the_same_rule_is_shared_with_the_settings_routes():
    from video_ai_editor.api import auth, settings_routes
    assert settings_routes._same_origin is auth.same_origin
