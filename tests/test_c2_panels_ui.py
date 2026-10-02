"""C2-panels lane, measured in a real browser against the real app (ported
to the 2026-10-02 desktop shell: the asset browser's tabs and sub-nav, the
clip inspector, Home for a new project).

What 0.7.2 shipped, and what each test pins:

- QA-110  Help was a hand-written 17-row table: N (snap), ⌘\\ (fit), Home/End,
          ⌥←/⌥→ (nudge), ⌘C/⌘V and ⌘A were bound and not listed.
- QA-119  Transition tiles cut names to one line ("Diagonal Bot…" twice) and
          the Basic family was five identical swatches at rest.
- QA-124  A 4700-character prompt was sent, failed as "invalid request" and
          the text was wiped.
- QA-126  With Stickers open, the first click on Effects only closed Stickers.
- QA-128  A new sticker was not selected (new text was).
- QA-129  The right-panel collapse was a full-width bar; removing media used
          the native window.confirm.
- QA-048  Text edited Start/End (Start TRIMMED it), stickers Start/Duration
          (Start MOVED it), media In/Out/Start.
- QA-068  A shorts run listed raw s_… ids and offered no way to open them.

The server is the real backend (a subprocess with its own HOME and WORKDIR)
serving frontend/dist — build the frontend first. Set VAE_C2_BASE_URL to run
against a server that is already up (e.g. a Vite dev server proxying /api to
a backend). Skips cleanly without Playwright/Chromium or a built frontend.
"""
from __future__ import annotations

import glob
import io
import itertools
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "frontend" / "dist"

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _clip(dst: Path, src: str) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"{src}=size=640x360:rate=30:duration=4",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(dst)],
                   check=True, capture_output=True)
    return dst


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    url = os.environ.get("VAE_C2_BASE_URL")
    if url:
        yield url.rstrip("/")
        return
    if not (DIST / "index.html").exists():
        pytest.skip("frontend/dist is not built (npx vite build in frontend/)")
    tmp = tmp_path_factory.mktemp("c2ui")
    home = tmp / "home"
    home.mkdir()
    port = _free_port()
    env = {**os.environ, "HOME": str(home), "WORKDIR": str(tmp / "wd"),
           "ANTHROPIC_API_KEY": "", "HUGGINGFACE_TOKEN": "", "PYTHONPATH": str(ROOT / "src")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "video_ai_editor.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("c2media")
    return [_clip(d / "c2_bars.mp4", "testsrc2"), _clip(d / "c2_smpte.mp4", "smptebars")]


def _session(base_url, media, name, clips=2) -> str:
    import httpx
    with httpx.Client(base_url=base_url, timeout=180) as c:
        sid = c.post("/api/sessions", json={"name": name}).json()["id"]
        for f in (media * 2)[:clips]:
            with f.open("rb") as fh:
                r = c.post(f"/api/sessions/{sid}/upload", files={"file": (f.name, fh, "video/mp4")},
                           data={"add_to_timeline": "true", "transcribe": "false"})
            assert r.status_code in (200, 202), r.text
    return sid


def _edl(base_url, sid) -> dict:
    import httpx
    return httpx.get(f"{base_url}/api/sessions/{sid}/edl", timeout=30).json()


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


def _contrast_failures(page, root: str) -> list:
    """Every text node under `root` measured against its composited background
    (tests/test_frontend_a11y.py's CONTRAST_JS, scoped to one element)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_a11y", Path(__file__).with_name("test_frontend_a11y.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    js = mod.CONTRAST_JS.replace("document.createTreeWalker(document.body,",
                                 f"document.createTreeWalker(document.querySelector({json.dumps(root)}),")
    assert js != mod.CONTRAST_JS
    return page.evaluate(js)


def _tab(page, name):
    """An asset-browser tab, scoped to the strip (the clip inspector has tabs
    of the same names)."""
    return page.locator(".ab-tabs").get_by_role("tab", name=name, exact=True)


def _open(browser, base_url, sid, *, keymap: dict | None = None, width=1440, height=900):
    ctx = browser.new_context(viewport={"width": width, "height": height})
    script = f"try {{ localStorage.setItem('vai.sessionId', {sid!r});"
    if keymap is not None:
        script += f" localStorage.setItem('vae.keymap.v1', {json.dumps(json.dumps(keymap))});"
    script += " localStorage.setItem('aive.assetTab', 'Media'); } catch (e) {}"
    ctx.add_init_script(script)
    page = ctx.new_page()
    page.goto(base_url + "/?vae-test")
    _tab(page, "Media").wait_for()
    page.locator(".timeline-canvas-wrap canvas").first.wait_for()
    page.wait_for_timeout(1200)
    return page


# ---- QA-110 -----------------------------------------------------------------

def test_help_lists_every_bound_command_with_the_live_keys(browser, base_url, media):
    sid = _session(base_url, media, "c2 help", clips=1)
    page = _open(browser, base_url, sid)
    page.keyboard.press("Shift+Slash")
    page.get_by_role("dialog", name="Keyboard shortcuts").wait_for()
    rows = page.locator("[data-help-row]")
    ids = [rows.nth(i).get_attribute("data-help-row") for i in range(rows.count())]
    assert len(ids) == len(set(ids)) and len(ids) >= 35, ids
    keys = {i: page.locator(f"[data-help-row='{i}'] .help-keys").inner_text().strip() for i in ids}
    # Bound in the CapCut preset and missing from the hand-written table.
    for cmd in ("toggleSnap", "zoomFit", "goToStart", "goToEnd", "nudgeLeft", "nudgeRight",
                "copy", "paste", "selectAll"):
        assert cmd in keys and keys[cmd] and keys[cmd] != "No key", (cmd, keys.get(cmd))
    assert keys["toggleSnap"] == "N" and keys["goToStart"] == "Home"
    assert keys["clearMarks"] == "No key"     # registered, unbound in CapCut: listed, honestly
    assert _contrast_failures(page, ".help-dialog") == []
    page.keyboard.press("Escape")
    page.context.close()

    # Another preset: Help follows it (Premiere splits with ⌘K, snaps with S).
    page = _open(browser, base_url, sid, keymap={"presetId": "premiere", "overrides": {"zoomFit": ["Shift+KeyF"]}})
    page.keyboard.press("Shift+Slash")
    page.get_by_role("dialog", name="Keyboard shortcuts").wait_for()
    split = page.locator("[data-help-row='split'] .help-keys").inner_text()
    snap = page.locator("[data-help-row='toggleSnap'] .help-keys").inner_text()
    fit = page.locator("[data-help-row='zoomFit'] .help-keys").inner_text()
    assert "K" in split and "B" not in split, split
    assert snap.strip() == "S" and fit.strip() in ("⇧F", "Shift+F"), (snap, fit)
    page.context.close()


# ---- QA-119 -----------------------------------------------------------------

def test_transition_tiles_have_whole_names_and_distinct_stills(browser, base_url, media):
    from PIL import Image, ImageChops, ImageStat
    sid = _session(base_url, media, "c2 transitions", clips=3)
    page = _open(browser, base_url, sid)
    _tab(page, "Transitions").click()
    page.locator(".trp-tile").first.wait_for()
    page.mouse.move(700, 400)
    for tab in page.locator(".trp-tab").all():
        tab.click()
        page.mouse.move(700, 400)
        page.wait_for_timeout(250)
        cut = page.evaluate("""() => [...document.querySelectorAll('.trp-tile-name')]
            .filter(e => e.scrollHeight > e.clientHeight + 1 || e.scrollWidth > e.clientWidth + 1)
            .map(e => e.textContent)""")
        assert not cut, (tab.inner_text(), cut)
        stills = {}
        for t in page.locator(".trp-tile").all():
            name = t.locator(".trp-tile-name").inner_text()
            stills[name] = Image.open(io.BytesIO(t.locator(".tp-prev").screenshot())).convert("RGB")
        for (a, ia), (b, ib) in itertools.combinations(stills.items(), 2):
            if ia.size != ib.size:
                continue
            diff = sum(ImageStat.Stat(ImageChops.difference(ia, ib)).mean) / 3
            assert diff > 4, f"{tab.inner_text()}: {a!r} and {b!r} look the same at rest ({diff:.2f})"
    # Three clips → two cuts → the all-cuts action is live and says how many.
    assert page.locator(".trp-every").inner_text() == "Apply to all 2 cuts"
    assert page.locator(".trp-every").is_enabled()
    assert _contrast_failures(page, ".transitions-panel") == []
    page.context.close()


# ---- QA-124 -----------------------------------------------------------------

def test_an_over_long_prompt_is_counted_refused_and_kept(browser, base_url, media):
    sid = _session(base_url, media, "c2 prompt", clips=1)
    page = _open(browser, base_url, sid)
    posts = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" and r.url.endswith("/prompt") else None)
    box = page.get_by_role("textbox", name="What should happen to this video?")
    box.fill("a" * 4700)
    note = page.locator("#prompt-length-note")
    assert "Prompt is too long (4,700 / 4,000 characters)" in note.inner_text()
    assert page.locator("button.prompt-run").is_disabled()
    assert box.get_attribute("aria-invalid") == "true"
    box.press("Enter")
    page.wait_for_timeout(500)
    assert posts == [] and len(box.input_value()) == 4700
    assert _contrast_failures(page, "#prompt-length-note") == []

    # Near the limit it only counts.
    box.fill("b" * 3900)
    assert note.inner_text() == "3,900 / 4,000 characters"
    assert page.locator("button.prompt-run").is_enabled()

    # A refusal that does come back (an older client, the phone) is said in
    # words and the sentence stays — the engine's real 422 body.
    body = json.dumps({"error": {"code": "VALIDATION_ERROR",
                                 "message": "invalid request: message — String should have at most 4000 characters",
                                 "details": [{"type": "string_too_long", "loc": ["body", "message"],
                                              "msg": "String should have at most 4000 characters",
                                              "input": "b" * 200, "ctx": {"max_length": 4000}}]}})
    page.route("**/api/sessions/*/prompt", lambda route: route.fulfill(
        status=422, content_type="application/json", body=body))
    box.fill("make it a reel")
    box.press("Enter")
    err = page.locator(".prompt-log .prompt-error")
    err.wait_for()
    assert err.inner_text() == "Prompt is too long — the limit is 4,000 characters. Shorten it and run it again."
    assert box.input_value() == "make it a reel"
    page.context.close()


# ---- QA-126, QA-128 -----------------------------------------------------------

def test_effects_opens_on_the_first_click_and_a_new_sticker_is_selected(browser, base_url, media):
    sid = _session(base_url, media, "c2 stickers", clips=1)
    page = _open(browser, base_url, sid)
    _tab(page, "Stickers").click()
    emoji = page.locator(".sticker-picker button[draggable='true']").nth(2)
    emoji.wait_for()
    emoji.click()
    # QA-128: selected at once — the Inspector is on the sticker.
    try:
        page.locator(".in-clip[data-clip-kind='sticker']").first.wait_for(timeout=5000)
    except Exception:  # noqa: BLE001
        if "no sticker artwork" in page.locator(".toast-host").inner_text():
            pytest.skip("emoji artwork is fetched and this harness is offline")
        raise
    stickers = [c for t in _edl(base_url, sid)["tracks"] if t["id"] == "stickers" for c in t["clips"]]
    assert len(stickers) == 1
    # QA-126: one click on Effects shows it, hiding Stickers. The rail keeps
    # every panel mounted (LEFT_RAIL_SPEC §2.7), so the picker is hidden,
    # not gone: assert visibility, not count.
    effects = _tab(page, "Effects")
    effects.click()
    page.wait_for_timeout(300)
    assert effects.get_attribute("aria-selected") == "true"
    assert page.locator(".effects-panel .fx-btn").first.is_visible()
    assert not page.locator(".sticker-picker").is_visible()
    page.context.close()


# ---- QA-129 -----------------------------------------------------------------

def test_chat_toggle_is_an_icon_and_remove_confirms_in_app(browser, base_url, media):
    sid = _session(base_url, media, "c2 chrome", clips=1)
    page = _open(browser, base_url, sid)
    native = []
    page.on("dialog", lambda d: (native.append(d.message), d.dismiss()))
    # The right column has no collapse rail any more; its one toggle (the
    # Chat) is an icon button, not a bar.
    toggle = page.get_by_role("button", name="Chat with the assistant")
    box = toggle.bounding_box()
    assert box["width"] <= 32 and box["height"] <= 32, box
    assert toggle.locator("svg").count() == 1
    toggle.click()
    assert page.locator("#right-panel-chat").is_visible()
    toggle.click()
    assert page.locator("#right-panel-chat").count() == 0

    page.locator("[data-media-row]").first.hover()
    page.locator("[data-media-row] .ab-card-x").first.click()
    dlg = page.get_by_role("alertdialog")
    dlg.wait_for()
    assert "Its clip is deleted from the timeline too" in dlg.inner_text()
    page.keyboard.press("Escape")
    assert page.get_by_role("alertdialog").count() == 0 and page.locator("[data-media-row]").count() == 1
    page.locator("[data-media-row]").first.hover()
    page.locator("[data-media-row] .ab-card-x").first.click()
    page.get_by_role("alertdialog").get_by_role("button", name="Remove").click()
    page.wait_for_timeout(800)
    assert native == []
    assert not [c for t in _edl(base_url, sid)["tracks"] if t["id"] == "v1" for c in t["clips"]]

    # A new, empty project asks for no preview (QA-129: two preview.mp4 404s
    # with the previous project's hash — fixed by QA-026's resetTransient).
    bad = []
    page.on("response", lambda r: bad.append((r.status, r.url)) if r.status >= 400 else None)
    page.locator("button[aria-label='Home']").click()
    page.locator(".home-banner").wait_for()
    page.locator(".home-banner").click()
    page.locator(".timeline-canvas-wrap canvas").first.wait_for()
    page.wait_for_timeout(2500)
    assert bad == [], bad
    page.context.close()


# ---- QA-048 -----------------------------------------------------------------

def _set_field(page, label: str, value: str):
    f = page.locator(".props").get_by_role("textbox", name=label, exact=True)
    f.click()
    f.fill(value)
    f.press("Enter")
    page.wait_for_timeout(700)


def test_one_timing_model_start_moves_end_and_duration_trim(browser, base_url, media):
    sid = _session(base_url, media, "c2 timing", clips=2)
    page = _open(browser, base_url, sid)
    # A new text clip is selected at once (the Text panel's "Add text at
    # playhead", LEFT_RAIL_SPEC R2): Start / End / Duration.
    _tab(page, "Text").click()
    page.get_by_role("button", name="Add text", exact=True).click()
    page.locator("[data-text-presets] > button").first.click()
    page.locator(".props").get_by_role("textbox", name="Duration", exact=True).wait_for()

    def text_clip():
        return [c for t in _edl(base_url, sid)["tracks"] for c in t["clips"] if c.get("text") is not None][0]
    t0 = text_clip()
    dur = t0["end"] - t0["start"]
    _set_field(page, "Start", "2")                      # MOVES — the duration is kept
    t1 = text_clip()
    assert t1["start"] == pytest.approx(2.0, abs=0.02) and t1["end"] - t1["start"] == pytest.approx(dur, abs=0.04), t1
    _set_field(page, "Duration", "1")                   # trims the end
    t2 = text_clip()
    assert t2["start"] == pytest.approx(2.0, abs=0.02) and t2["end"] == pytest.approx(3.0, abs=0.04), t2
    _set_field(page, "End", "3.5")                      # trims the end
    assert text_clip()["end"] == pytest.approx(3.5, abs=0.04)

    # A media clip: the same triple plus its source In / Out.
    canvas = page.locator(".timeline-canvas-wrap canvas").first.bounding_box()
    main = [t for t in _edl(base_url, sid)["tracks"] if t["id"] == "v1"][0]
    # Click into the main lane about a second in — find its row by stepping
    # down from below the ruler (a ruler click would park the playhead there).
    # An empty-lane click parks the playhead under the pointer, so each try
    # moves right too: the next one never lands on the playhead's grab zone.
    for i, dy in enumerate(range(34, int(canvas["height"]) - 4, 8)):
        page.mouse.click(canvas["x"] + 110 + 22 * i, canvas["y"] + dy)
        page.wait_for_timeout(150)
        if page.locator(".props").get_by_role("textbox", name="Source in", exact=True).count():
            break
    page.screenshot(path=str(Path(os.environ.get("C2_SHOTS", "/tmp")) / "c2_timing_media.png"))
    assert page.locator(".props").get_by_role("textbox", name="Source in", exact=True).count() == 1, canvas
    first = main["clips"][0]
    _set_field(page, "Duration", "2")
    after = [t for t in _edl(base_url, sid)["tracks"] if t["id"] == "v1"][0]["clips"][0]
    assert after["id"] == first["id"] and after["start"] == pytest.approx(first["start"], abs=0.02)
    assert after["out"] - after["in"] == pytest.approx(2.0, abs=0.04), after
    page.context.close()


# ---- QA-068 -----------------------------------------------------------------

def _sse(events: list[dict]) -> str:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events)


def test_a_shorts_run_offers_open_for_each_new_project_by_name(browser, base_url, media):
    import httpx
    sid = _session(base_url, media, "c2 shorts parent", clips=1)
    with httpx.Client(base_url=base_url, timeout=30) as c:
        kids = [c.post("/api/sessions", json={"name": n}).json()["id"] for n in ("talk short 1", "talk short 2")]
    plan = {"version": 1, "id": "p_1234abcd", "intent": "shorts", "title": "2 shorts", "brain": "recipes",
            "confidence": 1.0, "needs_input": [], "postconditions": [],
            "steps": [{"tool": "make_shorts", "args": {"target_count": 2}, "why": "find moments"}]}
    # The executor's own frames (agent/prompt/executor.py), shorts finished.
    events = [
        {"type": "brain", "status": "answered", "brain": "recipes", "label": "Recipes"},
        {"type": "plan", "plan": plan},
        {"type": "step", "index": 0, "total": 1, "tool": "make_shorts", "status": "running"},
        {"type": "tool_use", "name": "make_shorts", "args": {}, "id": "p_1234abcd_s0"},
        {"type": "tool_result", "name": "make_shorts", "id": "p_1234abcd_s0",
         "result": {"summary": "Made 2 short(s)", "shorts": [], "new_sessions": kids}},
        {"type": "step", "index": 0, "total": 1, "tool": "make_shorts", "status": "ok", "summary": "Made 2 short(s)"},
        *[e for k, (kid, name) in enumerate(zip(kids, ("talk short 1", "talk short 2"))) for e in (
            {"type": "tool_use", "name": "finish_short", "args": {"session": kid}, "id": f"p_1234abcd_child{k}"},
            {"type": "tool_result", "name": "finish_short", "id": f"p_1234abcd_child{k}",
             "result": {"session": kid, "status": "ok", "name": name, "error": None, "applied": 3, "op": None}})],
        {"type": "text_delta", "text": "via Recipes — 2 shorts: 1 step(s) applied. 2 shorts ready: talk short 1 · talk short 2."},
        {"type": "done"},
    ]
    page = _open(browser, base_url, sid)
    page.route("**/api/sessions/*/prompt", lambda route: route.fulfill(
        status=200, headers={"content-type": "text/event-stream"}, body=_sse(events)))
    box = page.get_by_role("textbox", name="What should happen to this video?")
    box.fill("make 2 shorts")
    box.press("Enter")
    projects = page.get_by_role("list", name="New projects")
    projects.wait_for()
    text = projects.inner_text()
    assert "talk short 1" in text and "talk short 2" in text and "s_" not in text, text
    assert _contrast_failures(page, ".prompt-projects") == []
    page.get_by_role("button", name="Open talk short 2").click()
    page.wait_for_function(f"() => localStorage.getItem('vai.sessionId') === {kids[1]!r}")
    page.locator(".topbar", has_text="talk short 2").first.wait_for()
    page.context.close()
