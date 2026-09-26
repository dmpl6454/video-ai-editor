"""Wave C lane C7 (ingest / API): first-run, import-error and export-param honesty.

Every test drives the real routes (FastAPI TestClient) with real ffmpeg-made
media and measures the answer the user would see, the persisted EDL, or the
rendered file (ffprobe / idet).

* QA-108 without ffmpeg/ffprobe every route that needs them answers ONE 503
  `ffmpeg_missing` that says what to install, and /api/health reports it —
  instead of "may not be a valid video", bare 500s and "corrupt frames";
* QA-111 the first import no longer overrides an aspect ratio the user picked,
  and one Undo removes an import together with the canvas change it made;
* QA-112 an empty file, a truncated video and a text document each get their
  own accurate refusal instead of one "export it as H.264" line;
* QA-114 a local (desktop) import is bounded by free disk space, not a fixed
  4 GB, and a refusal speaks to the user (no environment variable, no "0 MB");
* QA-123 an export of an empty timeline says there is nothing to export, and a
  1 kbps bitrate target is refused at the request boundary;
* coverage: an interlaced AVCHD (.MTS) clip is deinterlaced on import, and a
  Windows-authored project's missing clip is named by its file name.
"""
from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor import platformutil as _pu


# --------------------------------------------------------------------------- fixtures

@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    from video_ai_editor import config as _config, storage as _storage, main as _main
    wd = tmp_path / "wd"
    wd.mkdir()
    monkeypatch.setattr(_config, "WORKDIR", wd)
    monkeypatch.setattr(_storage, "WORKDIR", wd)
    monkeypatch.setattr(_main, "WORKDIR", wd)
    monkeypatch.delenv("VAI_MAX_UPLOAD_BYTES", raising=False)
    _main._STORES.clear()
    c = TestClient(_main.app)
    c.wd = wd  # type: ignore[attr-defined]
    return c


def _ff(dst: Path, *args: str) -> Path:
    subprocess.run([_pu.FFMPEG, "-loglevel", "error", "-y", *args, str(dst)], check=True)
    return dst


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("c7media")
    full = _ff(d / "clip.mp4", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=2",
               "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-shortest",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac")
    truncated = d / "truncated.mp4"
    truncated.write_bytes(full.read_bytes()[:20_000])
    zero = d / "zero.mp4"
    zero.write_bytes(b"")
    notes = d / "notes.txt"
    notes.write_text("meeting notes\nbring the tripod\n", encoding="utf-8")
    # 1080i AVCHD: 59.94p motion woven into top-field-first 29.97i frames,
    # MPEG-TS with AC-3 audio — what a camcorder's PRIVATE/AVCHD/STREAM holds.
    mts = _ff(d / "00001.MTS", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=60000/1001:duration=2",
              "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-shortest",
              "-vf", "interlace=scan=tff:lowpass=0,setfield=tff",
              "-c:v", "libx264", "-pix_fmt", "yuv420p", "-flags", "+ildct+ilme",
              "-x264opts", "tff=1", "-c:a", "ac3", "-f", "mpegts")
    return {"clip": full, "truncated": truncated, "zero": zero, "notes": notes, "mts": mts}


def _session(c: TestClient) -> str:
    r = c.post("/api/sessions", json={"name": "c7"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _upload(c: TestClient, sid: str, path: Path, name: str | None = None):
    with path.open("rb") as fh:
        return c.post(f"/api/sessions/{sid}/upload",
                      files={"file": (name or path.name, fh, "application/octet-stream")},
                      data={"transcribe": "false"})


def _details(r) -> dict:
    return r.json()["error"]["details"]


# --------------------------------------------------------------------------- QA-108

@pytest.fixture()
def no_ffmpeg(tmp_path: Path, monkeypatch):
    """A Mac with no ffmpeg anywhere: PATH points at an empty directory, so
    `shutil.which` and `subprocess` both fail exactly as they would for a user
    who never ran `brew install ffmpeg` (config.py's PATH widening has nothing
    to add)."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert shutil.which(_pu.FFMPEG) is None


def _assert_ffmpeg_missing(r) -> None:
    assert r.status_code == 503, (r.status_code, r.text[:400])
    d = _details(r)
    assert d["error"] == "ffmpeg_missing"
    assert d["missing"] == ["ffmpeg", "ffprobe"]
    assert d["install_command"] in d["message"]
    # Never the user's file's fault.
    assert "valid video" not in d["message"] and "corrupt" not in d["message"]


def test_qa108_health_reports_missing_ffmpeg(client, no_ffmpeg):
    r = client.get("/api/health")
    assert r.status_code == 200                      # the process is alive
    tools = r.json()["media_tools"]
    assert tools["ok"] is False
    assert tools["missing"] == ["ffmpeg", "ffprobe"]
    assert tools["install_command"] in tools["message"]


def test_qa108_health_reports_ffmpeg_present(client):
    tools = client.get("/api/health").json()["media_tools"]
    assert tools == {**tools, "ok": True, "missing": [], "message": None}


def test_qa108_every_route_that_needs_ffmpeg_says_so(client, media, monkeypatch, tmp_path):
    sid = _session(client)
    first = _upload(client, sid, media["clip"])                    # ffmpeg still present
    assert first.status_code == 200
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    _assert_ffmpeg_missing(_upload(client, sid, media["clip"], "second.mp4"))
    # An unguarded route (a filmstrip thumbnail) reaches the app-level safety net.
    thumb = client.get(f"/api/sessions/{sid}/thumb",
                       params={"src": first.json()["normalized"], "t": 1})
    assert thumb.status_code == 503 and thumb.json()["error"]["code"] == "FFMPEG_MISSING"
    _assert_ffmpeg_missing(thumb)
    _assert_ffmpeg_missing(client.post(f"/api/sessions/{sid}/preview"))
    _assert_ffmpeg_missing(client.get(f"/api/sessions/{sid}/preview.mp4"))
    _assert_ffmpeg_missing(client.post(f"/api/sessions/{sid}/export", json={"height": 180}))
    _assert_ffmpeg_missing(client.post(f"/api/sessions/{sid}/export?wait=0", json={"height": 180}))
    _assert_ffmpeg_missing(client.post(f"/api/sessions/{sid}/dispatch",
                                       json={"tool": "remove_silences", "args": {}}))
    for route, name in (("audio_upload", "song.wav"), ("vo_record", "take.webm")):
        r = client.post(f"/api/sessions/{sid}/{route}",
                        files={"file": (name, io.BytesIO(b"RIFF0000WAVEfmt "), "audio/wav")})
        _assert_ffmpeg_missing(r)
    # The refused import left nothing behind.
    assert len([p for p in (client.wd / sid / "uploads").iterdir() if p.is_dir()]) == 1


def test_qa108_a_background_job_fails_with_the_install_sentence(client, media, monkeypatch, tmp_path):
    """A queued job (`/dispatch?wait=0`) shows the same sentence as its error,
    not `FileNotFoundError: [Errno 2] … 'ffmpeg'`."""
    import time
    sid = _session(client)
    assert _upload(client, sid, media["clip"]).status_code == 200
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    r = client.post(f"/api/sessions/{sid}/dispatch?wait=0",
                    json={"tool": "remove_silences", "args": {}})
    assert r.status_code == 202, r.text
    job = {}
    for _ in range(200):
        job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
        if job["status"] in ("failed", "completed"):
            break
        time.sleep(0.05)
    assert job["status"] == "failed", job
    from video_ai_editor.ingest.tools import install_command
    assert install_command() in job["error"], job["error"]
    assert "FileNotFoundError" not in job["error"] and "Errno" not in job["error"]


# --------------------------------------------------------------------------- QA-111

def _landscape(tmp_path: Path) -> Path:
    return _ff(tmp_path / "wide.mp4", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=1",
               "-c:v", "libx264", "-pix_fmt", "yuv420p")


def test_qa111_first_import_respects_a_ratio_the_user_picked(client, tmp_path):
    sid = _session(client)
    r = client.post(f"/api/sessions/{sid}/dispatch", json={"tool": "set_aspect_ratio",
                                                           "args": {"ratio": "1:1"}})
    assert r.status_code == 200, r.text
    assert _upload(client, sid, _landscape(tmp_path)).status_code == 200
    canvas = client.get(f"/api/sessions/{sid}/edl").json()["canvas"]
    assert (canvas["w"], canvas["h"]) == (1080, 1080)


def test_qa111_choosing_the_ratio_the_project_already_has_still_counts(client, tmp_path):
    """REVIEW-XLANE-QA130-QA111-RATIO: 9:16 IS the default canvas, so choosing
    it changes nothing and (QA-130) records no op — which left QA-111's
    op-log check blind, and the first landscape import flipped the project to
    16:9 anyway. The choice is remembered outside the undo log."""
    sid = _session(client)
    before = client.get(f"/api/sessions/{sid}/edl").json()["canvas"]
    assert (before["w"], before["h"]) == (1080, 1920)
    r = client.post(f"/api/sessions/{sid}/dispatch", json={"tool": "set_aspect_ratio",
                                                           "args": {"ratio": "9:16"}})
    assert r.status_code == 200, r.text
    assert r.json().get("op") is None                     # QA-130 still holds: no dead undo
    assert _upload(client, sid, _landscape(tmp_path)).status_code == 200
    canvas = client.get(f"/api/sessions/{sid}/edl").json()["canvas"]
    assert (canvas["w"], canvas["h"]) == (1080, 1920)
    # A reopened store (process restart / LRU eviction) still knows.
    from video_ai_editor import main as _main
    _main._STORES.clear()
    sid2 = _session(client)
    client.post(f"/api/sessions/{sid2}/dispatch", json={"tool": "set_canvas",
                                                        "args": {"w": 1080, "h": 1920}})
    _main._STORES.clear()
    assert _upload(client, sid2, _landscape(tmp_path)).status_code == 200
    canvas = client.get(f"/api/sessions/{sid2}/edl").json()["canvas"]
    assert (canvas["w"], canvas["h"]) == (1080, 1920)


def test_qa111_one_undo_removes_the_import_and_its_canvas_change(client, tmp_path):
    sid = _session(client)
    before = client.get(f"/api/sessions/{sid}/edl").json()["canvas"]
    assert _upload(client, sid, _landscape(tmp_path)).status_code == 200
    after = client.get(f"/api/sessions/{sid}/edl").json()
    assert (after["canvas"]["w"], after["canvas"]["h"]) == (1920, 1080)   # auto-matched
    r = client.post(f"/api/sessions/{sid}/dispatch", json={"tool": "undo", "args": {}})
    assert r.status_code == 200, r.text
    edl = client.get(f"/api/sessions/{sid}/edl").json()
    assert not [c for t in edl["tracks"] for c in t["clips"]]
    assert (edl["canvas"]["w"], edl["canvas"]["h"]) == (before["w"], before["h"])


# --------------------------------------------------------------------------- QA-112

@pytest.fixture(scope="module")
def half_readable(tmp_path_factory) -> dict[str, Path]:
    """Files ffmpeg can HALF read, so the import used to succeed: a text file
    long enough for the `tty` (ANSI-art) demuxer, random bytes the `bin`
    demuxer decodes as a picture, and a faststart mp4 cut short with its index
    intact (the container still says 20 s; 1-2 s of it decodes)."""
    import random
    d = tmp_path_factory.mktemp("c7half")
    notes = d / "notes.txt"
    notes.write_text("hello world\n" * 50 + "\n".join(f"line {i}: shot list" for i in range(40)),
                     encoding="utf-8")
    assert notes.stat().st_size >= 600
    garbage = d / "garbage.bin"
    garbage.write_bytes(random.Random(7).randbytes(200_000))
    full = _ff(d / "long.mp4", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=20",
               "-f", "lavfi", "-i", "sine=frequency=440:duration=20", "-shortest",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart")
    cut = d / "cut_short.mp4"
    data = full.read_bytes()
    cut.write_bytes(data[: len(data) // 8])
    drawing = d / "logo.svg"
    drawing.write_text('<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="64" '
                       'height="64"><rect width="64" height="64" fill="red"/></svg>\n', encoding="utf-8")
    return {"notes": notes, "garbage": garbage, "cut": cut, "svg": drawing}


@pytest.mark.parametrize("key,code,phrase", [
    ("notes", "not_media", "not a video"),
    ("garbage", "not_media", "not a video"),
    ("cut", "damaged_file", "incomplete or damaged"),
    ("svg", "not_media", "Export it as a PNG"),
])
def test_qa112_half_readable_files_are_refused_not_imported(client, half_readable, key, code, phrase):
    """QA-112 remainder: diagnose_unreadable only ran when normalising FAILED,
    so anything ffmpeg can half-read was imported as a 0.04-0.16 s clip — and,
    as the first import, flipped the canvas to the fake picture's shape."""
    sid = _session(client)
    before = client.get(f"/api/sessions/{sid}/edl").json()
    r = _upload(client, sid, half_readable[key])
    assert r.status_code == 422, (r.status_code, r.text[:300])
    d = _details(r)
    assert d["error"] == code, d
    assert phrase in d["message"], d["message"]
    after = client.get(f"/api/sessions/{sid}/edl").json()
    assert not [c for t in after["tracks"] for c in t["clips"]]
    assert after["canvas"] == before["canvas"]
    # Nothing of the refused file is left in the session's uploads.
    from video_ai_editor.storage import session_dir
    uploads = session_dir(sid) / "uploads"
    assert not [p for p in uploads.rglob("*") if p.is_file()] if uploads.exists() else True



@pytest.mark.parametrize("key,code,phrase", [
    ("zero", "empty_file", "empty"),
    ("truncated", "damaged_file", "incomplete or damaged"),
    ("notes", "not_media", "text document"),
])
def test_qa112_each_unreadable_file_gets_its_own_accurate_message(client, media, key, code, phrase):
    sid = _session(client)
    r = _upload(client, sid, media[key])
    assert r.status_code == 422, r.text
    d = _details(r)
    assert d["error"] == code, d
    assert phrase in d["message"], d["message"]
    assert d["file"] == media[key].name
    # The codec advice is for codec problems only.
    assert "H.264" not in d["message"]


def test_qa112_a_text_file_renamed_mp4_is_still_called_a_text_document(client, media, tmp_path):
    fake = tmp_path / "holiday.mp4"
    fake.write_bytes(media["notes"].read_bytes())
    sid = _session(client)
    d = _details(_upload(client, sid, fake))
    assert d["error"] == "not_media" and "text document" in d["message"]


# --------------------------------------------------------------------------- QA-114

def _free_space(monkeypatch, free: int) -> None:
    from video_ai_editor.api import uploads
    monkeypatch.setattr(uploads.shutil, "disk_usage",
                        lambda p: shutil._ntuple_diskusage(total=free * 2, used=free, free=free))


def test_qa114_health_advertises_a_free_space_limit_for_the_desktop(client, monkeypatch):
    from video_ai_editor.api import uploads
    free = 400 * 1024 ** 3
    _free_space(monkeypatch, free)
    health = client.get("/api/health").json()
    assert health["upload_limit"] == "free_space"
    assert health["max_upload_bytes"] == int(free / uploads.FREE_SPACE_HEADROOM)
    assert health["max_upload_bytes"] > uploads.DEFAULT_MAX_UPLOAD_BYTES


async def _through_limit_middleware(client_host: str, declared: int) -> int:
    """Send a request DECLARING `declared` bytes through the real
    UploadLimitMiddleware; return the status (200 = it reached the route)."""
    from video_ai_editor.api.uploads import UploadLimitMiddleware
    from starlette.responses import PlainTextResponse

    async def inner(scope, receive, send):
        await PlainTextResponse("reached")(scope, receive, send)

    mw = UploadLimitMiddleware(inner)
    scope = {"type": "http", "method": "POST", "path": "/api/sessions/s_abcdef/upload",
             "raw_path": b"/api/sessions/s_abcdef/upload", "root_path": "",
             "scheme": "http", "server": ("127.0.0.1", 8765), "http_version": "1.1",
             "headers": [(b"content-length", str(declared).encode()), (b"host", b"127.0.0.1")],
             "client": (client_host, 50000), "query_string": b""}
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    await mw(scope, receive, send)
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


@pytest.mark.anyio
async def test_qa114_a_5gb_import_from_the_desktop_is_not_refused_by_a_fixed_cap(monkeypatch):
    monkeypatch.delenv("VAI_MAX_UPLOAD_BYTES", raising=False)
    _free_space(monkeypatch, 400 * 1024 ** 3)
    assert await _through_limit_middleware("127.0.0.1", 5 * 1024 ** 3) == 200
    # …while a phone/LAN peer declaring the same body still gets the 413.
    assert await _through_limit_middleware("192.168.1.20", 5 * 1024 ** 3) == 413


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_qa114_a_network_peer_keeps_the_fixed_cap(monkeypatch):
    from starlette.requests import Request
    from video_ai_editor.api import uploads
    scope = {"type": "http", "method": "POST", "path": "/x", "headers": [],
             "client": ("192.168.1.20", 5000), "query_string": b""}
    assert uploads.upload_limit_for(Request(scope)) == uploads.DEFAULT_MAX_UPLOAD_BYTES


def test_qa114_the_refusal_speaks_to_users(client, monkeypatch):
    monkeypatch.setenv("VAI_MAX_UPLOAD_BYTES", "10000")
    sid = _session(client)
    r = client.post(f"/api/sessions/{sid}/upload",
                    files={"file": ("big.mp4", io.BytesIO(b"\0" * 30_000), "video/mp4")})
    assert r.status_code == 413
    msg = r.json()["error"]["message"]
    assert "VAI_MAX_UPLOAD_BYTES" not in msg and "restart" not in msg.lower()
    assert "0 MB" not in msg
    assert re.search(r"\b9\.8 KB\b", msg), msg        # 10000 bytes, said plainly


def test_qa114_a_chunked_local_body_past_free_space_is_stopped(client, monkeypatch):
    from video_ai_editor.api import uploads
    monkeypatch.setattr(uploads.shutil, "disk_usage",
                        lambda p: shutil._ntuple_diskusage(total=1 << 30, used=1 << 30, free=5000))
    sid = _session(client)
    boundary = "----c7"
    payload = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"s.srt\"\r\n"
               f"Content-Type: application/octet-stream\r\n\r\n").encode() + b"1" * 9000 + \
        f"\r\n--{boundary}--\r\n".encode()
    r = client.post(f"/api/sessions/{sid}/subtitle_upload", content=iter([payload]),
                    headers={"content-type": f"multipart/form-data; boundary={boundary}"})
    assert r.status_code == 507, r.text
    assert _details(r)["error"] == "insufficient_space"
    assert list((client.wd / sid / "uploads").rglob("*.srt")) == []


# --------------------------------------------------------------------------- QA-123

def test_qa123_exporting_an_empty_timeline_says_nothing_to_export(client):
    sid = _session(client)
    for q in ("", "?wait=0"):
        r = client.post(f"/api/sessions/{sid}/export{q}", json={})
        assert r.status_code == 422, r.text
        d = _details(r)
        assert d["error"] == "nothing_to_export"
        assert "Nothing to export" in d["message"]


@pytest.mark.parametrize("kbps", [1, 50, 99])
def test_qa123_a_nonsense_bitrate_is_refused(client, kbps):
    sid = _session(client)
    r = client.post(f"/api/sessions/{sid}/export", json={"bitrate_kbps": kbps})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "bitrate" in r.json()["error"]["message"]


# --------------------------------------------------------------------------- coverage

def _idet(path: Path) -> dict[str, int]:
    out = subprocess.run([_pu.FFMPEG, "-hide_banner", "-i", str(path), "-vf", "idet",
                          "-frames:v", "50", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    # The LAST summary: ffmpeg can print an empty one for a probe instance first.
    line = [ln for ln in out.splitlines() if "Multi frame detection" in ln][-1]
    return {k: int(v) for k, v in re.findall(r"(TFF|BFF|Progressive|Undetermined):\s*(\d+)", line)}


def test_coverage_interlaced_avchd_is_deinterlaced_on_import(client, media):
    assert _idet(media["mts"])["TFF"] > 40               # the source really is interlaced
    sid = _session(client)
    r = _upload(client, sid, media["mts"])
    assert r.status_code == 200, r.text
    assert r.json()["fps"] == "30000/1001"               # frame rate kept, not doubled
    counts = _idet(Path(r.json()["normalized"]))
    assert counts["TFF"] + counts["BFF"] <= 5, counts     # no combing burned in
    assert counts["Progressive"] >= 40, counts


def test_coverage_windows_project_missing_clip_is_named_by_its_file_name():
    from video_ai_editor.media_offline import derived_tag, display_name_for
    src = (r"C:\Users\Asha\AppData\Roaming\Video AI Editor\workdir\s_ab12cd"
           r"\uploads\interview_1a2b3c4d\interview.normalized.mp4")
    assert display_name_for(None, src) == "interview.mp4"
    assert derived_tag(r"C:\Users\Asha\workdir\s_ab\cache\reframe_ab12.mp4") == "reframed"
    assert _pu.path_leaf(r"\\nas\footage\day 1\A001.mov") == "A001.mov"
    assert _pu.path_leaf("/Users/me/clip.mp4") == "clip.mp4"


def test_coverage_anamorphic_hdv_avchd_is_imported_with_square_pixels(client, tmp_path):
    """1440x1080 with a 4:3 sample aspect (HDV, AVCHD 'LP') is displayed
    1920x1080. It used to be normalised as-is, and the compositor — which sizes
    a clip from its coded width — rendered it squeezed into a 4:3 pillarbox
    inside the 16:9 export. Measured on the exported frame: the left edge is
    picture, not bars."""
    src = _ff(tmp_path / "hdv.MTS", "-f", "lavfi", "-i",
              "testsrc2=size=1440x1080:rate=30000/1001:duration=1,setsar=4/3",
              "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-shortest",
              "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "ac3", "-f", "mpegts")
    sid = _session(client)
    r = _upload(client, sid, src)
    assert r.status_code == 200, r.text
    norm = subprocess.run([_pu.FFPROBE, "-v", "error", "-select_streams", "v:0",
                           "-show_entries", "stream=width,height,sample_aspect_ratio",
                           "-of", "json", r.json()["normalized"]],
                          capture_output=True, text=True, check=True)
    v = json.loads(norm.stdout)["streams"][0]
    assert (v["width"], v["height"]) == (1920, 1080), v
    assert v.get("sample_aspect_ratio") in (None, "1:1", "0:1"), v
    ex = client.post(f"/api/sessions/{sid}/export", json={"height": 360})
    assert ex.status_code == 200, ex.text
    frame = subprocess.run([_pu.FFMPEG, "-v", "error", "-ss", "0.5", "-i", ex.json()["path"],
                            "-frames:v", "1", "-vf", "crop=40:360:0:0,format=gray",
                            "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    assert sum(frame) / len(frame) > 40, "the left edge is black: the clip was pillarboxed as 4:3"


def test_coverage_a_windows_authored_project_opens_on_a_mac(client, media):
    """A .vae saved on Windows: every path in it is `C:\\Users\\…`. The bundled
    clip must be remapped onto this Mac and play; a clip that was NOT bundled
    (offline when it was saved) must be named by its file name — it used to be
    the whole Windows path, in the bin and in the export refusal."""
    import zipfile
    from video_ai_editor.storage_project import save_project
    sid = _session(client)
    assert _upload(client, sid, media["clip"]).status_code == 200
    vae = client.wd / "mac.vae"
    save_project(sid, vae)
    mac_root = str(client.wd / sid)
    win_root = "C:\\Users\\Asha\\AppData\\Roaming\\Video AI Editor\\workdir\\" + sid

    def winify(text: str) -> str:
        def sub(m):
            raw = json.loads('"' + m.group(1) + '"')
            return json.dumps(win_root + raw[len(mac_root):].replace("/", "\\"))
        return re.sub(r'"(' + re.escape(mac_root) + r'[^"]*)"', sub, text)

    missing_src = win_root + "\\uploads\\interview_1a2b3c4d\\interview.normalized.mp4"
    out = client.wd / "win.vae"
    with zipfile.ZipFile(vae) as zin, zipfile.ZipFile(out, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename.endswith(".json"):
                data = winify(data.decode("utf-8")).encode("utf-8")
            if info.filename == "edl.json":
                edl = json.loads(data)
                v1 = next(t for t in edl["tracks"] if t["id"] == "v1")
                extra = dict(v1["clips"][0], id="c_offline1", src=missing_src,
                             start=v1["clips"][0]["out"])
                v1["clips"].append(extra)
                data = json.dumps(edl).encode("utf-8")
            zout.writestr(info, data)
    with out.open("rb") as fh:
        r = client.post("/api/load_project", files={"file": ("win.vae", fh, "application/octet-stream")})
    assert r.status_code == 200, r.text
    nsid = r.json()["id"]
    clips = [c for t in client.get(f"/api/sessions/{nsid}/edl").json()["tracks"] for c in t["clips"]]
    bundled = next(c for c in clips if c["id"] != "c_offline1")
    assert Path(bundled["src"]).is_file() and str(client.wd) in bundled["src"]
    rows = client.get(f"/api/sessions/{nsid}/media").json()["media"]
    offline = [m for m in rows if m.get("missing")]
    assert offline and offline[0]["name"] == "interview.mp4", offline
    ex = client.post(f"/api/sessions/{nsid}/export", json={"height": 180})
    assert ex.status_code == 422 and _details(ex)["error"] == "media_missing"
    assert "interview.mp4" in _details(ex)["message"]
    assert "C:\\" not in _details(ex)["message"]
