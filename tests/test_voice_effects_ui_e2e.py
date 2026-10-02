"""Voice effects through the Inspector, in Chromium AND WebKit (wave E, F3).

Driven like a person drives it: a clip is selected, the Voice effects grid
(one radio group: None + the eleven presets from `GET /api/voice/presets`)
is clicked and walked with the arrow keys, Enter chooses, the Strength
slider is nudged with the arrow keys (ONE op for the burst), Preview plays a
server-rendered WAV of the clip through the effect (nothing committed), the
section's Reset takes it off, and ⌘Z undoes. Every step is checked on the
EDL the server holds; the voice-over lane gets its own effect. Screenshots
of the section go to VAE_VOICE_SHOTS (default /tmp).

Harness: `test_frontend_a11y`'s server fixture (VAE_A11Y_BASE_URL = a Vite
dev server proxying /api to a backend, else frontend/dist).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_frontend_a11y import base_url  # noqa: E402,F401  (fixture)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
SHOTS = Path(os.environ.get("VAE_VOICE_SHOTS", "/tmp"))
STORE = "(await (window.__vaeTest ?? import('/src/store.ts'))).useStore.getState()"
PRESETS = ["none", "chipmunk", "deep", "monster", "robot", "echo", "reverb", "telephone", "megaphone", "radio",
           "underwater", "vibrato"]


@pytest.fixture(scope="module")
def pw():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def engine(request, pw):
    try:
        b = getattr(pw, request.param).launch(args=["--autoplay-policy=no-user-gesture-required"]
                                              if request.param == "chromium" else [])
    except Exception as e:  # noqa: BLE001 — a missing engine is a skip, not a failure
        pytest.skip(f"no Playwright {request.param}: {e}")
    b.engine_name = request.param
    yield b
    b.close()


@pytest.fixture(scope="module")
def voice_src(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("voice-ui") / "talk.mp4"
    expr = "+".join(f"{0.25 / k:.4f}*sin(2*PI*{140 * k}*t)" for k in range(1, 16))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=320x180:r=30:d=8",
                    "-f", "lavfi", "-i", f"aevalsrc=exprs='{expr}|{expr}':s=48000:d=8",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(p)],
                   check=True, capture_output=True)
    return p


def _client(base_url):  # noqa: F811
    import httpx
    return httpx.Client(base_url=base_url, timeout=180)


def _edl(base_url, sid) -> dict:  # noqa: F811
    with _client(base_url) as c:
        return c.get(f"/api/sessions/{sid}/edl").json()


def _clips(edl: dict, track: str) -> list[dict]:
    return next(t for t in edl["tracks"] if t["id"] == track)["clips"]


def _project(base_url, src: Path, name: str) -> tuple[str, str, str]:  # noqa: F811
    """A session: the talking clip on the main track, a slice of it on the
    voice-over lane. Returns (sid, v1 clip id, vo clip id)."""
    with _client(base_url) as c:
        sid = c.post("/api/sessions", json={"name": name}).json()["id"]
        with src.open("rb") as fh:
            r = c.post(f"/api/sessions/{sid}/upload", files={"file": (src.name, fh, "video/mp4")},
                       data={"add_to_timeline": "true", "transcribe": "false"})
        assert r.status_code in (200, 202), r.text
        deadline = time.time() + 120
        while not _clips(c.get(f"/api/sessions/{sid}/edl").json(), "v1"):
            assert time.time() < deadline, "the import never reached the timeline"
            time.sleep(0.3)
        v1 = _clips(c.get(f"/api/sessions/{sid}/edl").json(), "v1")[0]
        r = c.post(f"/api/sessions/{sid}/dispatch", json={"tool": "add_clip", "args": {
            "track": "vo", "src": v1["src"], "in": 2.0, "out": 5.0, "start": 1.0}})
        assert r.status_code == 200, r.text
        vo = _clips(c.get(f"/api/sessions/{sid}/edl").json(), "vo")[0]
    return sid, v1["id"], vo["id"]


def _wait(base_url, sid, pred, what: str, timeout=20.0) -> dict:  # noqa: F811
    deadline = time.time() + timeout
    while True:
        edl = _edl(base_url, sid)
        if pred(edl):
            return edl
        assert time.time() < deadline, f"timed out waiting for {what}"
        time.sleep(0.2)


def _fx(edl: dict, track: str) -> tuple:
    a = _clips(edl, track)[0].get("audio", {})
    return a.get("voice_effect"), a.get("voice_intensity", 1.0)


def _ops(base_url, sid) -> int:  # noqa: F811
    with _client(base_url) as c:
        return len(c.get(f"/api/sessions/{sid}/ops").json()["ops"])


def _select(page, cid: str) -> None:
    """Select a clip once the page's store holds it (the EDL arrives after the
    first paint; a selection of an id it does not know yet shows nothing)."""
    page.wait_for_function("""(id) => { const s = window.__vaeTest?.useStore.getState();
        return !!s?.edl?.tracks?.some((t) => t.clips.some((c) => c.id === id)) }""", arg=cid, timeout=20000)
    page.evaluate(f"async () => {{ const s = {STORE}; s.setSelection({json.dumps(cid)}) }}")


def test_voice_effects_in_the_inspector(engine, base_url, voice_src):  # noqa: F811
    sid, v1_id, vo_id = _project(base_url, voice_src, f"Voice {engine.engine_name}")
    ctx = engine.new_context(viewport={"width": 1280, "height": 800})
    ctx.add_init_script("try { " + " ".join(
        f"localStorage.setItem({json.dumps(k)}, {json.dumps(v)});"
        for k, v in {"vai.sessionId": sid, "vai.rightTab": "inspect"}.items()) + " } catch (e) {}")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(base_url + "/?vae-test")
        page.locator(".timeline-canvas-wrap canvas").first.wait_for()
        _select(page, v1_id)
        # The clip inspector (2026-10-02 shell): a video clip's voice effects
        # are Audio › Voice changer; a sound clip's are its Voice changer tab.
        page.locator(".in-tabs").get_by_role("tab", name="Audio", exact=True).click()
        page.get_by_role("radiogroup", name="Audio").get_by_role("radio", name="Voice changer", exact=True).click()
        grid = page.get_by_role("radiogroup", name="Voice effect")
        grid.wait_for(timeout=15000)
        grid.scroll_into_view_if_needed()
        radios = grid.get_by_role("radio")
        # None + the served presets, in the table's order, each with a name and a hint
        assert [radios.nth(i).get_attribute("data-effect") for i in range(radios.count())] == PRESETS
        assert radios.nth(0).get_attribute("aria-checked") == "true"
        for i in range(radios.count()):
            assert radios.nth(i).inner_text().strip() and radios.nth(i).get_attribute("title")
        preview = page.get_by_role("button", name="Preview None on this clip")
        assert preview.is_disabled()
        section = grid.locator("xpath=ancestor::div[contains(@class,'voice-section')]")
        SHOTS.mkdir(parents=True, exist_ok=True)
        section.screenshot(path=str(SHOTS / f"voice-grid-{engine.engine_name}.png"))

        # a click chooses Robot: one op, the EDL carries it, the radio says so
        n0 = _ops(base_url, sid)
        grid.get_by_role("radio", name="Robot").click()
        _wait(base_url, sid, lambda e: _fx(e, "v1")[0] == "robot", "robot on the clip")
        page.wait_for_function("() => document.querySelector('[data-effect=robot]')?.getAttribute('aria-checked') === 'true'")
        assert _ops(base_url, sid) == n0 + 1

        # the keyboard: arrows move within the group, Enter chooses (Echo is right of Robot)
        grid.get_by_role("radio", name="Robot").focus()
        page.keyboard.press("ArrowRight")
        assert page.evaluate("document.activeElement?.dataset.effect") == "echo"
        page.keyboard.press("Enter")
        _wait(base_url, sid, lambda e: _fx(e, "v1") == ("echo", 1.0), "echo on the clip")
        page.keyboard.press("ArrowDown")                       # one row (three tiles) down
        assert page.evaluate("document.activeElement?.dataset.effect") == "megaphone"

        # Strength: a burst of arrow keys is ONE op
        n1 = _ops(base_url, sid)
        slider = page.get_by_role("slider", name="Echo strength")
        slider.focus()
        for _ in range(3):
            page.keyboard.press("ArrowLeft")
        _wait(base_url, sid, lambda e: abs(_fx(e, "v1")[1] - 0.85) < 1e-6, "strength 85 %")
        time.sleep(0.6)
        assert _ops(base_url, sid) == n1 + 1
        section.screenshot(path=str(SHOTS / f"voice-echo-{engine.engine_name}.png"))

        # Preview: a server-rendered WAV of THIS clip through the effect; nothing committed
        before = _edl(base_url, sid)
        with page.expect_response(lambda r: "/voice/preview" in r.url, timeout=30000) as resp:
            page.get_by_role("button", name="Preview Echo on this clip").click()
        r = resp.value
        assert r.status == 200 and r.headers["content-type"] == "audio/wav"
        # ≈ 4 s of 16-bit stereo (Chromium's CDP hands back no body for a
        # response the page consumed as a Blob, so the length is the header's)
        assert int(r.headers["content-length"]) > 48000 * 2 * 2 * 3, r.headers
        page.wait_for_function(
            "() => /Playing|Preview failed/.test(document.querySelector('.voice-status')?.textContent ?? '')",
            timeout=15000)
        status = page.locator(".voice-status").inner_text()
        print(engine.engine_name, "preview status:", status)
        assert "Playing Echo" in status, status
        assert _edl(base_url, sid) == before
        page.get_by_role("button", name="Stop the voice preview").click()

        # the voice-over lane: its own clip, its own effect
        _select(page, vo_id)
        page.locator(".in-tabs").get_by_role("tab", name="Voice changer", exact=True).click()
        grid.wait_for()
        page.wait_for_function("() => document.querySelector('[data-effect=none]')?.getAttribute('aria-checked') === 'true'")
        grid.get_by_role("radio", name="Hall").click()
        _wait(base_url, sid, lambda e: _fx(e, "vo")[0] == "reverb" and _fx(e, "v1")[0] == "echo", "Hall on the VO")

        # Reset takes it off; ⌘Z brings it back
        page.locator("button[title='Reset voice effects to default']").click()
        _wait(base_url, sid, lambda e: _fx(e, "vo")[0] is None, "the VO effect removed")
        page.locator("body").click(position={"x": 640, "y": 40})
        page.keyboard.press("Meta+z")
        _wait(base_url, sid, lambda e: _fx(e, "vo")[0] == "reverb", "undo brings Hall back")
        assert not errors, errors
    finally:
        ctx.close()


def test_the_spinner_stops_for_reduced_motion(engine, base_url):  # noqa: F811
    ctx = engine.new_context(reduced_motion="reduce")
    page = ctx.new_page()
    try:
        page.goto(base_url + "/?vae-test")
        name = page.evaluate("""() => { const i = document.createElement('span'); i.className = 'icon icon-spin';
                                document.body.appendChild(i); return getComputedStyle(i).animationName }""")
        assert name in ("none", ""), name
    finally:
        ctx.close()
