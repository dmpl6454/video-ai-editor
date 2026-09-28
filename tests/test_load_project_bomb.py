"""Final QA: opening a .vae has a byte budget, and only the app's own page may
open one.

  * zip bomb: a 1 MB archive with a 1 GiB zeros member filled a 200 MB volume
    to 4 MB free in half a second (`zf.extractall` had no budget; the only
    guard counted the COMPRESSED upload). The unpacked size is now checked
    against a ratio cap and the free space BEFORE anything is written, and
    extraction counts bytes as it goes.
  * a page on another 127.0.0.1 port could POST a project blind (multipart
    needs no preflight; `same-site` was allowed): /api/load_project now
    requires the settings routes' same-origin fetch metadata.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

from test_load_project_by_content import (  # noqa: F401  (fixtures)
    _post, client, lavfi_clip, saved_project, workdir,
)


def _with_member(archive: bytes, name: str, data: bytes) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(archive))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for info in src.infolist():
            out.writestr(info, src.read(info))
        out.writestr(zipfile.ZipInfo(name), data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return buf.getvalue()


def _sessions(workdir: Path) -> set[str]:
    return {p.name for p in workdir.glob("s_*")}


def test_a_zip_bomb_is_refused_before_anything_is_unpacked(client, workdir, saved_project, monkeypatch):
    archive, _sid = saved_project
    bomb = _with_member(archive, "media/zeros.bin", bytes(96 * 1024 * 1024))
    assert len(bomb) < 2 * 1024 * 1024
    from video_ai_editor import storage_project
    opened: list[str] = []
    real_open = zipfile.ZipFile.open

    def spy(self, name, *a, **kw):
        opened.append(getattr(name, "filename", name))
        return real_open(self, name, *a, **kw)

    monkeypatch.setattr(zipfile.ZipFile, "open", spy)
    before = _sessions(workdir)
    r = _post(client, "bomb.vae", bomb)
    assert r.status_code == 507, r.text
    assert "unpack" in r.text.lower() or "too large" in r.text.lower()
    assert _sessions(workdir) == before
    assert not list(workdir.glob("_import_*"))
    assert "media/zeros.bin" not in opened          # refused before a byte of it was written
    assert storage_project.MAX_UNPACK_RATIO >= 50


def test_a_project_larger_than_the_free_space_is_refused(client, workdir, saved_project, monkeypatch):
    archive, _sid = saved_project
    big = _with_member(archive, "media/pad.bin", bytes(6 * 1024 * 1024))   # 6 MB unpacked, tiny packed
    from video_ai_editor.api import uploads
    # Room for the upload itself, not for what it unpacks to.
    monkeypatch.setattr(uploads, "free_space_limit", lambda dest_dir=None: 3 * len(big))
    before = _sessions(workdir)
    r = _post(client, "big.vae", big)
    assert r.status_code == 507, r.text
    assert "space" in r.text.lower()
    assert _sessions(workdir) == before


def test_the_saved_project_still_opens(client, workdir, saved_project):
    archive, _sid = saved_project
    r = _post(client, "ok.vae", archive)
    assert r.status_code == 200, r.text


def test_a_page_on_another_local_port_cannot_open_a_project(client, workdir, saved_project):
    archive, _sid = saved_project
    before = _sessions(workdir)
    for headers in ({"Sec-Fetch-Site": "same-site"}, {"Sec-Fetch-Site": "cross-site"},
                    {"Origin": "http://127.0.0.1:9106"}):
        r = client.post("/api/load_project", headers=headers,
                        files={"file": ("p.vae", io.BytesIO(archive), "application/octet-stream")})
        assert r.status_code == 403, (headers, r.status_code, r.text)
    assert _sessions(workdir) == before
    ok = client.post("/api/load_project", headers={"Sec-Fetch-Site": "same-origin"},
                     files={"file": ("p.vae", io.BytesIO(archive), "application/octet-stream")})
    assert ok.status_code == 200, ok.text
