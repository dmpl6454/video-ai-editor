"""There was no upload cap before 0.6.0.

Not a permissive one — none. Any client that could reach `/upload` could stream
bytes to the Mac's disk until the volume filled, and the frontend's 413 branch
was dead code because nothing ever produced a 413. These tests pin all three
defences and the two places the limit is advertised.
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from lan_fixtures import lan_home, lan_peer  # noqa: F401

from video_ai_editor.main import app


@pytest.fixture
def small_cap(monkeypatch, tmp_path):
    """A 4 KB cap and a temp WORKDIR, so a test can exceed the limit with a
    string literal instead of four gigabytes."""
    from video_ai_editor import config, storage
    monkeypatch.setenv("VAI_MAX_UPLOAD_BYTES", "4096")
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    monkeypatch.setattr(storage, "WORKDIR", tmp_path)
    return tmp_path


def _session(client: TestClient) -> str:
    r = client.post("/api/sessions", json={"name": "cap"})
    assert r.status_code == 200
    return r.json()["id"]


# --- 1. the Content-Length pre-check -----------------------------------------

def test_an_oversized_body_is_refused_before_it_is_read(small_cap):
    """The cheapest defence and the only one that can say no BEFORE the client
    has spent five minutes pushing bytes."""
    c = TestClient(app)
    sid = _session(c)
    r = c.post(f"/api/sessions/{sid}/upload",
               files={"file": ("big.mp4", io.BytesIO(b"\0" * 9000), "video/mp4")})
    assert r.status_code == 413
    assert r.json()["error"]["details"]["limit_bytes"] == 4096
    # Nothing was written.
    assert list((small_cap / sid / "uploads").glob("*")) == []


def test_the_cap_covers_every_ingress_not_just_video(small_cap):
    """A middleware rather than a per-route dependency, so a route added later
    is covered by default instead of by remembering."""
    c = TestClient(app)
    sid = _session(c)
    body = io.BytesIO(b"\0" * 9000)
    for path, field, name in (
            (f"/api/sessions/{sid}/audio_upload", "file", "a.mp3"),
            (f"/api/sessions/{sid}/sticker_upload", "file", "s.png"),
            (f"/api/sessions/{sid}/subtitle_upload", "file", "s.srt"),
            ("/api/load_project", "file", "p.zip")):
        body.seek(0)
        r = c.post(path, files={field: (name, body, "application/octet-stream")})
        assert r.status_code == 413, path


def test_a_body_under_the_cap_is_not_refused_by_the_cap(small_cap):
    """The limit must not become the reason ordinary imports fail. A tiny
    non-video still fails — with the 422 it always did, not a 413."""
    c = TestClient(app)
    sid = _session(c)
    r = c.post(f"/api/sessions/{sid}/upload",
               files={"file": ("tiny.mp4", io.BytesIO(b"\0" * 64), "video/mp4")})
    assert r.status_code == 422
    assert r.json()["error"]["details"]["error"] == "couldn't_import"


# --- 2. the mid-stream abort --------------------------------------------------

@pytest.mark.anyio
async def test_stream_upload_deletes_the_partial_file(small_cap, monkeypatch):
    """Content-Length is a claim, not a fact, and a chunked body has none at
    all. A rejected upload that leaves 4 GB of garbage in the session directory
    has not really been rejected."""
    from fastapi import HTTPException, UploadFile
    from video_ai_editor.api.uploads import stream_upload_to

    dst = small_cap / "partial.bin"
    upload = UploadFile(filename="x.bin", file=io.BytesIO(b"\0" * 9000))
    with pytest.raises(HTTPException) as excinfo:
        await stream_upload_to(upload, dst)
    assert excinfo.value.status_code == 413
    assert not dst.exists()


@pytest.mark.anyio
async def test_stream_upload_writes_what_fits(small_cap):
    from fastapi import UploadFile
    from video_ai_editor.api.uploads import stream_upload_to

    dst = small_cap / "ok.bin"
    written = await stream_upload_to(
        UploadFile(filename="x.bin", file=io.BytesIO(b"ab" * 100)), dst)
    assert written == 200
    assert dst.read_bytes() == b"ab" * 100


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _chunked_multipart(field: str, filename: str, payload: bytes) -> tuple[dict, object]:
    """A multipart body delivered as an ITERATOR, so httpx sends it with
    `Transfer-Encoding: chunked` and no `Content-Length`.

    This is the shape `UploadLimitMiddleware` cannot see: its check begins
    `if raw and ...`, so a body with no declared length walks straight past it.
    Everything below therefore tests the route's OWN second layer, which three
    of the six ingresses did not have until 0.6.0.
    """
    boundary = b"----vaetestboundary"
    body = (b"--" + boundary + b"\r\n"
            + f'Content-Disposition: form-data; name="{field}"; '
              f'filename="{filename}"\r\n'.encode()
            + b"Content-Type: application/octet-stream\r\n\r\n"
            + payload + b"\r\n--" + boundary + b"--\r\n")
    headers = {"content-type": f"multipart/form-data; boundary={boundary.decode()}"}
    return headers, iter([body])


@pytest.mark.parametrize("route,field,filename", [
    ("sticker_upload", "file", "s.png"),
    ("subtitle_upload", "file", "s.srt"),
])
def test_a_chunked_body_is_still_capped_on_the_session_routes(small_cap, route, field, filename):
    """The regression this file previously could not have caught.

    `test_the_cap_covers_every_ingress_not_just_video` asserts the MIDDLEWARE,
    which every one of those requests declared a Content-Length for. Send the
    same bytes without one and, before the fix, `sticker_upload` and
    `subtitle_upload` wrote all of them to disk with their own `while chunk :=
    await file.read(...)` loops and returned 200.
    """
    c = TestClient(app)
    sid = _session(c)
    headers, body = _chunked_multipart(field, filename, b"\0" * 9000)
    r = c.post(f"/api/sessions/{sid}/{route}", content=body, headers=headers)
    assert r.status_code == 413, r.text
    # And the partial file was cleaned up rather than left on the volume.
    written = list((small_cap / sid / "uploads").rglob("*.*"))
    assert written == [], written


def test_a_chunked_body_is_still_capped_on_load_project(small_cap):
    """The worst of the three: this one writes into WORKDIR itself, not into a
    session directory a user can delete."""
    c = TestClient(app)
    headers, body = _chunked_multipart("file", "p.vae", b"\0" * 9000)
    r = c.post("/api/load_project", content=body, headers=headers)
    assert r.status_code == 413, r.text
    assert list(small_cap.glob("_import_*")) == []


# --- 3. the free-space precondition ------------------------------------------

def test_an_import_larger_than_the_free_volume_is_refused(small_cap, monkeypatch):
    """Refusing up front beats accepting, spending five minutes, and failing at
    97% with a disk-full error the user has to interpret."""
    import shutil as _shutil
    from video_ai_editor.api import uploads

    monkeypatch.setenv("VAI_MAX_UPLOAD_BYTES", str(64 * 1024 * 1024))
    monkeypatch.setattr(
        uploads.shutil, "disk_usage",
        lambda p: _shutil._ntuple_diskusage(total=1 << 30, used=1 << 30, free=1024))
    c = TestClient(app)
    sid = _session(c)
    r = c.post(f"/api/sessions/{sid}/upload",
               files={"file": ("m.mp4", io.BytesIO(b"\0" * 200_000), "video/mp4")})
    assert r.status_code == 507
    assert r.json()["error"]["details"]["error"] == "insufficient_space"


# --- 4. the limit is advertised, not just enforced ----------------------------

def test_health_reports_the_cap(small_cap):
    """So the phone's Import screen can refuse a too-large pick locally rather
    than spending the user's battery on a 413."""
    body = TestClient(app).get("/api/health").json()
    assert body["max_upload_bytes"] == 4096


def test_whoami_reports_the_cap_and_the_job_pool(lan_home, small_cap, monkeypatch):
    monkeypatch.setenv("VAE_LAN", "1")
    from video_ai_editor.api import pairing
    code = pairing.new_pair_code()
    token = pairing.claim_pair_code(code, "iPhone")["token"]
    server = lan_peer().get("/api/pair/whoami",
                            headers={"X-VAE-Client": "1",
                                     "Authorization": f"Bearer {token}"}).json()["server"]
    assert server["max_upload_bytes"] == 4096
    # Shared with the desktop: one export plus one upscale parks every preview
    # in `queued` at 0%, and the phone shows the queue rather than a stalled bar.
    assert server["job_workers"] >= 1


def test_a_bad_env_value_falls_back_to_the_default(monkeypatch):
    from video_ai_editor.api.uploads import DEFAULT_MAX_UPLOAD_BYTES, max_upload_bytes
    for bad in ("", "banana", "0", "-1"):
        monkeypatch.setenv("VAI_MAX_UPLOAD_BYTES", bad)
        assert max_upload_bytes() == DEFAULT_MAX_UPLOAD_BYTES
