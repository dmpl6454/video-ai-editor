"""`POST /api/load_project` judges an upload by its BYTES, never by its name.

The shipped 0.7.1 app refused its own saved projects with "415 Unsupported
Media Type". Two things lined up: the saved `.vae` was served as
`text/plain` (mimetypes does not know the extension), which is the exact case
in which WebKit/macOS appends `.txt` to a download — so the user picked
`<sid>.vae.txt` — and the endpoint gated on `name.endswith(".vae"|".zip")`
without ever looking at the content. `P.VAE`, an extensionless pick and a
picker that reports `blob` failed the same gate with the same valid bytes.

Everything here goes through the real FastAPI app against a temp WORKDIR, and
the project under test is a genuine archive written by `save_project` from a
session holding a real (lavfi) clip, fetched back through the same
`/files/exports` URL the "Saved" link uses.
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor.main import app
from video_ai_editor.api.hardening import RATE
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Track, Clip, Canvas

NOT_A_PROJECT = ("That file is not a Video AI Editor project. Choose the .vae "
                 "file you saved with Save (a project file is a zip that "
                 "contains manifest.json).")


# --- fixtures -----------------------------------------------------------------

@pytest.fixture
def workdir(tmp_path: Path, monkeypatch) -> Path:
    """Every module that binds WORKDIR by name, pointed at tmp — main.py owns
    the `_import_*` temp file, storage the sessions, storage_project the
    archive; missing one would write into the user's real Application
    Support directory."""
    from video_ai_editor import config, storage, storage_project, main as _main
    for mod in (config, storage, storage_project, _main):
        monkeypatch.setattr(mod, "WORKDIR", tmp_path)
    RATE.windows.clear()
    _main._STORES.clear()
    return tmp_path


@pytest.fixture
def client(workdir: Path) -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module")
def lavfi_clip(tmp_path_factory) -> Path:
    """One tiny real clip per module; each session gets a copy."""
    p = tmp_path_factory.mktemp("clip") / "src.mp4"
    keyed = p.with_suffix(".keyed.mp4")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                    "-i", "color=c=blue:s=320x180:d=1:r=30",
                    "-pix_fmt", "yuv420p", str(keyed)],
                   check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-i", str(keyed),
                    "-f", "lavfi", "-i", "sine=f=440:duration=1",
                    "-c:v", "copy", "-c:a", "aac", "-shortest", str(p)],
                   check=True, capture_output=True)
    return p


@pytest.fixture
def saved_project(workdir: Path, client: TestClient, lavfi_clip: Path) -> tuple[bytes, str]:
    """A genuine .vae: a session with one clip, saved through the API and read
    back through the export URL. Returns (archive bytes, source sid)."""
    from video_ai_editor.storage import new_session_id
    sid = new_session_id()
    src = workdir / sid / "uploads" / "src.mp4"
    src.parent.mkdir(parents=True)
    shutil.copy(lavfi_clip, src)
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(src=str(src), in_=0, out=1, start=0, id="c1"),
        ]),
    ])
    edl.recompute_duration()
    (workdir / sid / "edl.json").write_text(edl.model_dump_json())

    saved = client.post(f"/api/sessions/{sid}/save_project")
    assert saved.status_code == 200, saved.text
    fetched = client.get(saved.json()["url"])
    assert fetched.status_code == 200, fetched.text
    return fetched.content, sid


# --- helpers ------------------------------------------------------------------

def _post(client: TestClient, name: str, data: bytes):
    return client.post("/api/load_project",
                       files={"file": (name, io.BytesIO(data), "application/octet-stream")})


def _multipart(field: str, filename: str, data: bytes) -> tuple[dict, bytes]:
    """A hand-built body: httpx drops an empty filename from the part header
    entirely (the server then sees a plain form field), so the one case that
    matters — `filename=""` on the wire — has to be written by hand."""
    boundary = "vae-test-boundary"
    head = (f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
            f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n')
    body = head.encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return {"Content-Type": f"multipart/form-data; boundary={boundary}"}, body


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _assert_imported(workdir: Path, response) -> None:
    """The imported session is the saved one: same clip id, media re-pointed
    into the new session's uploads/imported/ and present on disk."""
    assert response.status_code == 200, response.text
    new_sid = response.json()["id"]
    new_edl = EDLStore(workdir / new_sid).edl
    clip = new_edl.tracks[0].clips[0]
    assert clip.id == "c1"
    assert clip.out == 1
    assert Path(clip.src).is_relative_to(workdir / new_sid / "uploads" / "imported")
    assert Path(clip.src).is_file()


def _no_import_leftovers(workdir: Path) -> None:
    """Nothing survives a refusal: neither the endpoint's `_import_<name>` temp
    file nor the loader's `_import_unpack_<sid>` extraction directory."""
    assert list(workdir.glob("_import_*")) == []


def _nothing_was_created(workdir: Path) -> None:
    """A refused file must leave no session behind.

    `GET /api/sessions` globs `s_*`, so a session created before validation and
    abandoned on failure shows up in the project picker as an empty project the
    user never made — and its `/edl` answers 200 with a blank default timeline,
    so it is indistinguishable from real work that lost its content.
    """
    assert sorted(p.name for p in workdir.glob("s_*")) == []
    _no_import_leftovers(workdir)


# --- 1. content wins: a real project opens whatever it is called --------------

@pytest.mark.parametrize("name", [
    "p.vae",
    "P.VAE",          # 415 before: endswith(".vae") is case-sensitive
    "p.zip",
    "p",              # 415 before: no extension
    "blob",           # 415 before: what a File picker without a name reports
    "{sid}.vae.txt",  # 415 before: THE bug — macOS renamed the text/plain download
])
def test_a_real_project_opens_whatever_the_file_is_called(workdir, client, saved_project, name):
    data, sid = saved_project
    r = _post(client, name.format(sid=sid), data)
    _assert_imported(workdir, r)
    _no_import_leftovers(workdir)


def test_a_real_project_named_like_a_video_still_opens(workdir, client, saved_project):
    """The same bytes under `p.mp4` are still a project and still open.

    Content wins over the name on purpose: the name is the one thing the OS,
    the browser and the user all feel free to rewrite (the `.vae.txt` case IS
    that), and refusing correct bytes because of a wrong label is exactly the
    failure this replaces. What an `.mp4` name would have meant is judged by
    the probe on the bytes — a real video is not a zip and gets the 415."""
    data, _ = saved_project
    _assert_imported(workdir, _post(client, "p.mp4", data))
    _no_import_leftovers(workdir)


def test_an_empty_filename_still_works(workdir, client, saved_project):
    """The name is only a hint for the temp file now; `filename=""` on the
    wire must not turn into an empty temp path or a refusal."""
    data, _ = saved_project
    headers, body = _multipart("file", "", data)
    r = client.post("/api/load_project", content=body, headers=headers)
    _assert_imported(workdir, r)
    _no_import_leftovers(workdir)


# --- 2. not a project: 415 with an honest sentence ----------------------------

def test_garbage_named_vae_is_refused_with_a_user_facing_message(workdir, client):
    r = _post(client, "p.vae", b"\x00\x01\x02 this is not a zip " * 8)
    assert r.status_code == 415, r.text
    err = r.json()["error"]
    assert err["code"] == "HTTP_415"
    # The name was what we expected, so the message needs no mention of it.
    assert err["message"] == NOT_A_PROJECT
    _no_import_leftovers(workdir)


def test_a_refusal_names_the_file_when_its_name_was_unexpected(workdir, client):
    """A `.vae.txt` (or `.mp4`) pick that is also NOT a project gets told what
    it sent, so the rename is visible in the toast instead of a mystery."""
    r = _post(client, "s_deadbeef01.vae.txt", b"definitely a text file\n")
    assert r.status_code == 415, r.text
    msg = r.json()["error"]["message"]
    assert msg.startswith(NOT_A_PROJECT)
    assert "s_deadbeef01.vae.txt" in msg
    _no_import_leftovers(workdir)


def test_a_zip_with_no_manifest_is_not_a_project(workdir, client):
    r = _post(client, "p.vae", _zip({"edl.json": b"{}", "notes.txt": b"hi"}))
    assert r.status_code == 415, r.text
    assert r.json()["error"]["message"] == NOT_A_PROJECT
    _no_import_leftovers(workdir)


@pytest.mark.parametrize("member", [
    "backup/manifest.json",         # nested: not the root manifest
    "../manifest.json",             # escapes the archive outright
    "/manifest.json",               # absolute
    "manifest.json/",               # a DIRECTORY entry, not a manifest
    "../vae-probe/manifest.json",   # leaves the base and comes back
    "vae-probe/../manifest.json",   # ditto, the other way round
])
def test_a_manifest_off_the_archive_root_is_not_proof(workdir, client, member):
    """Only a FILE member that extractall places at the archive root counts.

    The last three shapes are the ones a `Path.resolve()`-based probe let
    through: resolve NORMALISES `..` while `extractall` DROPS it, and a
    directory entry resolves to the same path a file would. Each got a 422
    (one of them leaking the absolute WORKDIR path out of `IsADirectoryError`)
    plus an orphan session, where the rule is 415 and no trace.
    """
    r = _post(client, "p.vae", _zip({member: b'{"media": []}', "edl.json": b"{}"}))
    assert r.status_code == 415, r.text
    assert r.json()["error"]["message"] == NOT_A_PROJECT
    _nothing_was_created(workdir)


def test_an_empty_archive_is_not_a_project(workdir, client):
    """`PK\\x05\\x06` (an end-of-central-directory record on its own) is a
    legitimate zip that zipfile opens with zero members — accepted as a zip,
    refused as a project, no crash in between."""
    buf = io.BytesIO()
    zipfile.ZipFile(buf, "w").close()
    assert buf.getvalue().startswith(b"PK\x05\x06")
    r = _post(client, "p.vae", buf.getvalue())
    assert r.status_code == 415, r.text
    _no_import_leftovers(workdir)


# --- 3. a real archive whose import fails keeps its 422 -----------------------

def test_a_project_archive_that_fails_to_import_is_still_422(workdir, client):
    """The probe says 'project' (zip, root manifest.json); the loader then
    chokes on the manifest. That is a different failure from 'not a project'
    and keeps the status it always had — and, like a 415, leaves nothing."""
    r = _post(client, "p.vae", _zip({"manifest.json": b"{ not json"}))
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "UNPROCESSABLE"
    assert err["message"].startswith("failed to load project")
    _nothing_was_created(workdir)


def test_a_failed_import_does_not_leak_a_filesystem_path(workdir, client):
    """A user-facing message must not carry the app's own directory layout.

    `manifest.json/` as a directory member reached `read_text`, which raised
    `IsADirectoryError: /…/workdir/s_xxx/_unpack/manifest.json` — the whole
    absolute WORKDIR path, session id included, pasted into a toast. The probe
    refuses that member now; this pins the property for every refusal on the
    route, not just that one member.
    """
    for data in (_zip({"manifest.json/": b"{}", "edl.json": b"{}"}),
                 _zip({"manifest.json": b"{ not json"}),
                 _zip({"manifest.json": b'{"media": []}', "edl.json": b"nope{"}),
                 b"not a zip at all"):
        r = _post(client, "p.vae", data)
        assert r.status_code in (415, 422), r.text
        msg = r.json()["error"]["message"]
        assert str(workdir) not in msg, msg
        assert "_unpack" not in msg, msg
    _nothing_was_created(workdir)


def test_an_archive_whose_edl_is_unreadable_is_422_not_a_blank_project(workdir, client):
    """A damaged timeline must be an ERROR, never a blank project.

    `EDLStore` falls back to an empty EDL when `edl.json` will not parse (and
    an imported session has no snapshots to recover from), so this archive used
    to import with 200 and open as a default 1080x1920 timeline with zero
    clips and no message at all. That is the same silent-empty state
    `save_project`'s `is_data_loss_state` guard refuses to WRITE — and the
    user's instinct on seeing a blank project is to save a backup, which would
    then overwrite their last good copy.
    """
    r = _post(client, "p.vae", _zip({"manifest.json": b'{"media": []}',
                                     "edl.json": b"garbage{"}))
    assert r.status_code == 422, r.text
    assert "edl.json is unreadable" in r.json()["error"]["message"]
    _nothing_was_created(workdir)


def test_an_archive_with_no_edl_at_all_is_422(workdir, client):
    """`save_project` always writes `edl.json`, so an archive without one did
    not come from Save and has no timeline to open — 422 for the same reason as
    an unreadable one, not a 200 that opens blank."""
    r = _post(client, "p.vae", _zip({"manifest.json": b'{"media": []}'}))
    assert r.status_code == 422, r.text
    assert "no edl.json" in r.json()["error"]["message"]
    _nothing_was_created(workdir)


def test_a_manifest_that_is_not_an_object_is_422(workdir, client):
    """A root `manifest.json` holding a JSON array is a project by the probe's
    rule but `manifest.get(...)` on a list is an AttributeError — which reads
    as a crash, not as 'this file is not a project'."""
    r = _post(client, "p.vae", _zip({"manifest.json": b"[1, 2, 3]",
                                     "edl.json": b"{}"}))
    assert r.status_code == 422, r.text
    assert "not a project manifest" in r.json()["error"]["message"]
    _nothing_was_created(workdir)


# --- 4. the byte budget speaks before the content probe -----------------------

def test_the_cap_answers_before_the_content_probe(workdir, client, monkeypatch):
    """Guard order is unchanged: an oversized body is a 413 from the cap, not
    a 415 from a probe that never gets to run — and nothing is left behind."""
    monkeypatch.setenv("VAI_MAX_UPLOAD_BYTES", "64")
    headers, body = _multipart("file", "p.vae", b"not a zip " * 40)
    headers["Transfer-Encoding"] = "chunked"  # no Content-Length: the route's own counter must catch it
    r = client.post("/api/load_project", content=iter([body]), headers=headers)
    assert r.status_code == 413, r.text
    _no_import_leftovers(workdir)


# --- 5. the saved .vae is served as a zip attachment --------------------------

def test_a_saved_vae_is_served_as_application_zip(workdir, client, lavfi_clip, saved_project):
    """`text/plain` + unknown extension is the condition under which
    WebKit/macOS appends `.txt` to a download. Serving the real type with the
    real name closes the front door the `.vae.txt` rename walked through."""
    _, sid = saved_project
    r = client.get(f"/api/sessions/{sid}/files/exports/{sid}.vae")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    disposition = r.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert f'filename="{sid}.vae"' in disposition
    assert r.content.startswith(b"PK\x03\x04")


def test_other_export_types_are_served_as_before(workdir, client, lavfi_clip, saved_project):
    """Only `.vae` gets the explicit type; an exported video keeps the guessed
    one and the attachment disposition it already had."""
    _, sid = saved_project
    shutil.copy(lavfi_clip, workdir / sid / "exports" / "final.mp4")
    r = client.get(f"/api/sessions/{sid}/files/exports/final.mp4")
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers["content-disposition"].startswith("attachment")
