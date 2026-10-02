"""The top bar's Open control, handed a video, in a real browser (Chromium AND WebKit).

"Open" is where anyone looks first to open a video, and it only knew saved
.vae projects: picking IMG_0623.MOV there ended in

    Couldn't open that .vae project: That file is not a Video AI Editor
    project. ... The file you chose was named "IMG_0623.MOV".

Now (frontend/src/lib/openPick.ts):

  * a video, audio or picture file picked in Open is imported into the current
    project exactly like a drop, and one line says so;
  * a real .vae still opens as a new project;
  * anything unrecognised still reaches the project loader and gets its
    explanation (that path also serves `<sid>.vae.txt`, see v0.7.2);
  * the File menu (the project menu, since the 2026-10-02 shell) has an
    "Import media…" entry that opens a media picker.

Harness (test_frontend_a11y's): VAE_A11Y_BASE_URL = a Vite dev server proxying
/api to a backend, otherwise the suite starts its own over frontend/dist
(skipped when dist is not built). Screenshots go to VAE_A11Y_SHOTS.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from test_frontend_a11y import base_url  # noqa: F401  (fixture)
from test_speed_ui_e2e import engine, pw  # noqa: F401  (fixtures: chromium + webkit)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
SHOTS = Path(os.environ.get("VAE_A11Y_SHOTS", "/tmp"))
REFUSAL = "Couldn't open that .vae project"


@pytest.fixture(scope="module")
def phone_video(tmp_path_factory) -> Path:
    """A one-second iPhone-shaped clip: the file name and container of the report."""
    dst = tmp_path_factory.mktemp("phone") / "IMG_0623.MOV"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=1",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(dst)],
        check=True, capture_output=True)
    return dst


def _client(base_url):  # noqa: F811
    import httpx
    return httpx.Client(base_url=base_url, timeout=60)


def _new_project(base_url, name: str) -> str:  # noqa: F811
    with _client(base_url) as c:
        return c.post("/api/sessions", json={"name": name}).json()["id"]


def _media(base_url, sid: str) -> list[dict]:  # noqa: F811
    with _client(base_url) as c:
        return c.get(f"/api/sessions/{sid}/media").json().get("media", [])


def _wait_media(base_url, sid: str, want: int, timeout: float = 45.0) -> list[dict]:  # noqa: F811
    end = time.time() + timeout
    items: list[dict] = []
    while time.time() < end:
        items = _media(base_url, sid)
        if len(items) >= want:
            return items
        time.sleep(0.4)
    pytest.fail(f"media never reached {want}; the bin holds {[m.get('name') or m for m in items]}")


def _open(engine, base_url, sid: str):  # noqa: F811
    ctx = engine.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_init_script(f"try {{ localStorage.setItem('vai.sessionId', {sid!r}) }} catch (e) {{}}")
    page = ctx.new_page()
    page.goto(base_url + "/")
    page.locator(".ab-tabs").get_by_role("tab", name="Media", exact=True).wait_for()
    page.locator(".timeline-canvas-wrap canvas").first.wait_for()
    page.wait_for_timeout(1000)
    return page


def _shot(page, engine, name: str) -> None:  # noqa: F811
    try:
        page.screenshot(path=str(SHOTS / f"open-media-{engine.engine_name}-{name}.png"))
    except Exception:  # noqa: BLE001 — a screenshot is evidence, never the assertion
        pass


def test_a_video_picked_in_open_is_imported_not_refused(engine, base_url, phone_video):  # noqa: F811
    sid = _new_project(base_url, "open a video")
    page = _open(engine, base_url, sid)
    page.set_input_files("[data-testid=open-file]", str(phone_video))
    page.get_by_text("is a video, not a project").first.wait_for(timeout=15000)
    text = page.inner_text("body")
    assert REFUSAL not in text and "not a Video AI Editor project" not in text
    assert "“IMG_0623.MOV” is a video, not a project" in text
    items = _wait_media(base_url, sid, 1)
    assert any("IMG_0623" in (m.get("name") or m.get("filename") or str(m)) for m in items), items
    _shot(page, engine, "video-in-open")
    page.context.close()


def test_open_still_opens_a_saved_project(engine, base_url, phone_video):  # noqa: F811
    src = _new_project(base_url, "to be saved")
    with _client(base_url) as c:
        with phone_video.open("rb") as fh:
            r = c.post(f"/api/sessions/{src}/upload", files={"file": ("IMG_0623.MOV", fh, "video/quicktime")},
                       data={"add_to_timeline": "true", "transcribe": "false"})
        assert r.status_code in (200, 202), r.text
        saved = c.post(f"/api/sessions/{src}/save_project")
        assert saved.status_code == 200, saved.text
        vae = Path(saved.json()["path"])
        before = {s["id"] for s in c.get("/api/sessions").json()["sessions"]}
    page = _open(engine, base_url, _new_project(base_url, "somewhere else"))
    before |= {s["id"] for s in _client(base_url).get("/api/sessions").json()["sessions"]}
    page.set_input_files("[data-testid=open-file]", str(vae))
    end = time.time() + 45
    fresh: set[str] = set()
    while time.time() < end and not fresh:
        with _client(base_url) as c:
            fresh = {s["id"] for s in c.get("/api/sessions").json()["sessions"]} - before
        time.sleep(0.4)
    assert fresh, "the .vae was not opened as a new project"
    text = page.inner_text("body")
    assert REFUSAL not in text and "is a video, not a project" not in text
    _shot(page, engine, "project-opened")
    page.context.close()


def test_open_still_explains_a_file_it_cannot_use(engine, base_url, tmp_path):  # noqa: F811
    notes = tmp_path / "notes.txt"
    notes.write_text("not a project and not media\n")
    page = _open(engine, base_url, _new_project(base_url, "open a text file"))
    page.set_input_files("[data-testid=open-file]", str(notes))
    page.get_by_text(REFUSAL).first.wait_for(timeout=15000)
    assert "notes.txt" in page.inner_text("body")
    assert "is a video, not a project" not in page.inner_text("body")
    page.context.close()


def test_import_media_is_in_the_project_menu(engine, base_url, phone_video):  # noqa: F811
    sid = _new_project(base_url, "import from the menu")
    page = _open(engine, base_url, sid)
    page.locator("button[aria-label='File']").click()
    menu = page.locator("[role=menu][aria-label=File]")
    item = menu.get_by_role("menuitem", name="Import media…")
    item.wait_for()
    assert item.locator("svg[data-icon]").count() == 1
    with page.expect_file_chooser() as chooser:
        item.click()
    chooser.value.set_files(str(phone_video))
    items = _wait_media(base_url, sid, 1)
    assert any("IMG_0623" in (m.get("name") or m.get("filename") or str(m)) for m in items), items
    assert REFUSAL not in page.inner_text("body")
    _shot(page, engine, "import-media-menu")
    page.context.close()
