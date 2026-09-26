"""Settings dialog and project-picker posters, in a real browser against the
real app (QA-063-SETTINGS, QA-106-CACHE-UI, QA-099-THUMBS).

The backend is a subprocess with its own HOME and WORKDIR, the repo `.env`'s
key blocked (so the Keychain path is what runs), the Keychain pointed at a
uuid-named TEST service that is deleted afterwards, and the Anthropic "Test
key" probe stubbed — this suite never talks to api.anthropic.com and never
touches the owner's "Video AI Editor" Keychain item. It serves frontend/dist,
so build the frontend first; or set VAE_SETTINGS_UI_BASE_URL to a server
that is already up (a Vite dev server proxying /api to such a backend).
"""
from __future__ import annotations

import glob
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "frontend" / "dist"
SECURITY = "/usr/bin/security"

pytestmark = [
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH"),
    pytest.mark.skipif(not Path(SECURITY).exists(), reason="needs the macOS Keychain"),
]

# Runs INSIDE the backend subprocess: block .env's key, stub the network probe.
_LAUNCH = r"""
import os, sys
os.environ["ANTHROPIC_API_KEY"] = ""
from video_ai_editor import config
del os.environ["ANTHROPIC_API_KEY"]
config.ANTHROPIC_API_KEY = ""
from video_ai_editor.main import app
from video_ai_editor.api import settings_routes
class AuthenticationError(Exception):
    status_code = 401
def _probe(key):
    raise AuthenticationError("stubbed: the suite never calls Anthropic")
settings_routes.PROBE = _probe
import uvicorn
uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _dist_has_settings() -> bool:
    return any("settings-dialog" in Path(p).read_text(errors="ignore")
               for p in glob.glob(str(DIST / "assets" / "*.js")))


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    url = os.environ.get("VAE_SETTINGS_UI_BASE_URL")
    if url:
        yield {"url": url.rstrip("/"), "service": os.environ.get("VAI_KEYCHAIN_SERVICE", "")}
        return
    if not (DIST / "index.html").exists() or not _dist_has_settings():
        pytest.skip("frontend/dist is missing or predates the Settings dialog (npx vite build in frontend/)")
    tmp = tmp_path_factory.mktemp("c3ui")
    (tmp / "home").mkdir()
    service = f"Video AI Editor TEST c3-ui {uuid.uuid4().hex[:12]}"
    port = _free_port()
    env = {**os.environ, "HOME": str(tmp / "home"), "WORKDIR": str(tmp / "wd"), "HUGGINGFACE_TOKEN": "",
           "PYTHONPATH": str(ROOT / "src"), "VAI_KEYCHAIN_SERVICE": service}
    env.pop("ANTHROPIC_API_KEY", None)
    proc = subprocess.Popen([sys.executable, "-c", _LAUNCH, str(port)], cwd=str(ROOT), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import httpx
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            if httpx.get(f"{url}/api/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if proc.poll() is not None:
            pytest.fail("backend exited during startup")
        time.sleep(0.3)
    else:
        proc.kill()
        pytest.fail("backend did not come up")
    try:
        yield {"url": url, "service": service}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        subprocess.run([SECURITY, "delete-generic-password", "-s", service], capture_output=True)


@pytest.fixture(scope="module")
def projects(server, tmp_path_factory):
    import httpx
    clip = tmp_path_factory.mktemp("media") / "c3_clip.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:d=4:r=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(clip)], check=True, capture_output=True)
    tag = uuid.uuid4().hex[:6]        # unique names: a reused dev server keeps older runs' projects
    names = {"full": f"C3 with video {tag}", "empty": f"C3 empty {tag}"}
    with httpx.Client(base_url=server["url"], timeout=180) as c:
        empty = c.post("/api/sessions", json={"name": names["empty"]}).json()["id"]
        full = c.post("/api/sessions", json={"name": names["full"]}).json()["id"]
        with clip.open("rb") as fh:
            r = c.post(f"/api/sessions/{full}/upload", files={"file": ("c3_clip.mp4", fh, "video/mp4")},
                       data={"add_to_timeline": "true", "transcribe": "false"})
        assert r.status_code in (200, 202), r.text
        for _ in range(120):
            if c.get(f"/api/sessions/{full}/edl").json().get("duration", 0) > 0:
                break
            time.sleep(0.5)
        assert c.post(f"/api/sessions/{full}/preview").status_code == 200
    return {"full": full, "empty": empty, "names": names}


@pytest.fixture(scope="module")
def browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            shells = sorted(glob.glob(os.path.expanduser(
                "~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell")))
            exe = os.environ.get("VAE_CHROMIUM") or (shells[-1] if shells else None)
            if not exe:
                pytest.skip("no Chromium for Playwright")
            b = p.chromium.launch(executable_path=exe)
        yield b
        b.close()


def _open(browser, url, sid, *, width=1440, height=900):
    ctx = browser.new_context(viewport={"width": width, "height": height})
    ctx.add_init_script(f"try {{ localStorage.setItem('vai.sessionId', {sid!r}) }} catch (e) {{}}")
    page = ctx.new_page()
    page.goto(url + "/")
    page.locator(".topbar").wait_for()
    page.wait_for_timeout(1000)
    return page


def _keychain_has(service: str) -> bool:
    return subprocess.run([SECURITY, "find-generic-password", "-s", service, "-a", "anthropic_api_key"],
                          capture_output=True).returncode == 0


def test_settings_saves_the_key_to_the_keychain_and_never_shows_it(browser, server, projects, tmp_path):
    from playwright.sync_api import expect
    key = "sk-ant-api03-UItest" + uuid.uuid4().hex + uuid.uuid4().hex
    page = _open(browser, server["url"], projects["full"])
    responses: list[str] = []
    page.on("response", lambda r: responses.append(r.text()) if "/api/" in r.url and "json" in (r.headers.get("content-type") or "") else None)

    page.keyboard.press("ControlOrMeta+Comma")                   # ⌘, / Ctrl+,
    dlg = page.get_by_role("dialog", name="Settings")
    expect(dlg).to_be_visible()
    field = dlg.get_by_label("Anthropic API key")
    expect(field).to_have_attribute("type", "password")
    field.fill("sk-ant-nope")
    expect(dlg.get_by_role("button", name="Save")).to_be_disabled()
    field.fill(key)
    dlg.get_by_role("button", name="Save").click()
    expect(dlg.locator(".settings-status[data-tone=ok]")).to_have_text(f"Saved in your Keychain (sk-ant-…{key[-4:]}).")
    expect(dlg.locator(".settings-result")).to_contain_text("rejected")      # the stubbed probe's 401
    expect(field).to_have_value("")
    if server["service"]:
        assert _keychain_has(server["service"])
    expect(dlg.locator(".brain-list-row", has_text="Claude")).to_contain_text("Key added")
    page.screenshot(path=str(tmp_path / "settings_saved.png"))

    dlg.get_by_role("button", name="Remove key").click()
    page.get_by_role("alertdialog", name="Remove your Anthropic key?").get_by_role("button", name="Remove key").click()
    expect(dlg.locator(".settings-status").first).to_contain_text("Not set up")
    if server["service"]:
        assert not _keychain_has(server["service"])

    assert key not in page.content() and key[20:50] not in page.content()
    assert not any(key in r or key[20:50] in r for r in responses)
    page.keyboard.press("Escape")
    expect(dlg).to_be_hidden()
    page.context.close()


def test_gear_opens_settings_and_clear_render_cache_frees_space(browser, server, projects):
    import httpx
    from playwright.sync_api import expect
    before = httpx.get(f"{server['url']}/api/sessions/{projects['full']}/render-cache").json()["bytes"]
    assert before > 0
    page = _open(browser, server["url"], projects["full"])
    page.get_by_role("button", name="Settings", exact=True).click()
    dlg = page.get_by_role("dialog", name="Settings")
    expect(dlg).to_be_visible()
    expect(dlg.locator(".settings-cache-line")).to_contain_text(" of ")
    dlg.get_by_role("button", name="Clear render cache").click()
    page.get_by_role("alertdialog", name="Clear the render cache?").get_by_role("button", name="Clear cache").click()
    expect(page.get_by_text("Freed", exact=False).first).to_be_visible()
    after = httpx.get(f"{server['url']}/api/sessions/{projects['full']}/render-cache").json()["bytes"]
    assert after < before
    page.context.close()


def test_project_picker_shows_a_poster_frame_per_project(browser, server, projects):
    from playwright.sync_api import expect
    page = _open(browser, server["url"], projects["full"])
    page.locator(".topbar-session").click()
    menu = page.get_by_role("menu", name="Projects")
    expect(menu).to_be_visible()
    full_row = menu.get_by_role("menuitemradio", name=projects["names"]["full"])
    empty_row = menu.get_by_role("menuitemradio", name=projects["names"]["empty"])
    img = full_row.locator(".project-poster img")
    expect(img).to_have_count(1)
    page.wait_for_function("(el) => el.complete && el.naturalWidth > 0", arg=img.element_handle(), timeout=15000)
    # The empty project draws the placeholder (its poster answers 204).
    expect(empty_row.locator(".project-poster svg")).to_have_count(1, timeout=15000)
    page.context.close()
