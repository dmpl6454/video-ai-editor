"""The AI tool cards reached from the asset browser, measured in a real
browser, in Chromium AND WebKit.

Written for wave D's deep links (docs/design/LEFT_RAIL_SPEC.md §2.5 M5, §8.3
case 8: a row in Media / Audio / Text / Effects / Captions that jumped to ONE
catalogue card, with a back chip) and ported to the 2026-10-02 desktop shell,
where the catalogue is Media › AI media and a tool is opened by its own card.
The rows, the back chip and their focus/scroll choreography are gone, so
those cases are skipped with that reason; what stays is the half that proves
the cards do what their labels say, against the real backend:

- Find b-roll (Media) finds the matching file in a folder; Reduce noise
  (Audio) replaces the selected clip with a denoised file; Hook overlay,
  Lower third and Brand kit (Text) add that text, that name and that handle;
  Chroma key (Effects) keys the selected clip; Import subtitles then Export
  .srt (Captions) round-trips a subtitle file. The rest need a model
  download, an API key or a transcript this harness does not have;
- a card's badge mirrors the feature report (stems missing, a voice not
  downloaded yet), from the same sources the old rows read.

Harness (server, sessions) is test_frontend_a11y's: set VAE_A11Y_BASE_URL to
run against a server that is already up (a Vite dev server proxying /api),
otherwise it serves frontend/dist. Engines missing from the Playwright install
skip. Reference screenshots go to VAE_RAIL_SHOTS (default /tmp).
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import pytest

from test_frontend_a11y import _clip, base_url, sessions  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
SHOTS = Path(os.environ.get("VAE_RAIL_SHOTS", "/tmp"))
SUPERSEDED = "superseded: the 2026-10-02 desktop shell opens AI tools from Media › AI media; the deep-link rows and back chip are gone"

# Tool → catalogue label (the card's title).
TOOLS = {
    "search_media": "Search footage", "find_broll": "Find b-roll",
    "noise_reduce": "Reduce noise", "vocal_isolate": "Isolate vocals", "instrumental_isolate": "Isolate instrumental",
    "tts_voiceover": "AI voiceover",
    "add_hook_overlay": "Hook overlay", "generate_hook": "Suggest hooks", "add_lower_third": "Lower third",
    "apply_brand_kit": "Brand kit",
    "remove_background": "Remove background", "chroma_key": "Chroma key",
    "translate_captions": "Translate captions", "diarize": "Detect speakers", "name_speakers": "Name speakers",
    "import_srt": "Import subtitles", "export_srt": "Export .srt",
}


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
        b = getattr(pw, request.param).launch()
    except Exception as e:  # noqa: BLE001 — a missing engine is a skip, not a failure
        pytest.skip(f"no Playwright {request.param}: {e}")
    b.engine_name = request.param
    yield b
    b.close()


def _project(base_url, tmp_path_factory, name: str) -> str:  # noqa: F811
    """A fresh project with one 4 s clip on v1 (tools below edit it)."""
    import httpx
    clip = _clip(tmp_path_factory.mktemp("dl-media") / "deep_link_clip.mp4")
    with httpx.Client(base_url=base_url, timeout=120) as c:
        sid = c.post("/api/sessions", json={"name": name}).json()["id"]
        with clip.open("rb") as fh:
            r = c.post(f"/api/sessions/{sid}/upload", files={"file": ("deep_link_clip.mp4", fh, "video/mp4")},
                       data={"add_to_timeline": "true", "transcribe": "false"})
        assert r.status_code in (200, 202), r.text
    return sid


def _edl(base_url, sid) -> dict:  # noqa: F811
    import httpx
    with httpx.Client(base_url=base_url, timeout=60) as c:
        return c.get(f"/api/sessions/{sid}/edl").json()


def _open(browser, base_url, sid, width=1280, height=800, routes=None):  # noqa: F811
    ctx = browser.new_context(viewport={"width": width, "height": height})
    items = {"vai.sessionId": sid, "vai.rightTab": "inspect", "aive.assetTab": "Media"}
    ctx.add_init_script("try { " + " ".join(
        f"localStorage.setItem({json.dumps(k)}, {json.dumps(v)});" for k, v in items.items()) + " } catch (e) {}")
    page = ctx.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    # React's own warnings (a flushSync inside a lifecycle, a setState during
    # another component's render) are console errors in a dev build.
    page.on("console", lambda m: page.errors.append(m.text) if m.type == "error" and "React" in m.text else None)
    for pattern, handler in (routes or {}).items():
        page.route(pattern, handler)
    page.goto(base_url + "/")
    page.locator(".ab-tabs").get_by_role("tab", name="Media", exact=True).wait_for()
    page.locator(".timeline-canvas-wrap canvas").first.wait_for()
    page.wait_for_timeout(1200)
    return page


def _ai_media(page):
    """Media › AI media: the catalogue."""
    page.locator(".ab-tabs").get_by_role("tab", name="Media", exact=True).click()
    page.get_by_role("button", name="AI media", exact=True).click()
    page.locator(".ai-search").wait_for(timeout=5000)


def _open_tool(page, tool: str):
    """Expand the card of `tool` (its title is TOOLS[tool]) and return its body."""
    _ai_media(page)
    page.locator(".ai-search").fill("")
    head = page.locator(f"button.ai-card-head[aria-controls='ai-body-{tool}']")
    head.wait_for(timeout=10000)
    assert head.locator(".ai-card-title").inner_text() == TOOLS[tool], tool
    head.scroll_into_view_if_needed()
    if head.get_attribute("aria-expanded") != "true":
        head.click()
    body = page.locator(f"#ai-body-{tool}")
    body.wait_for(timeout=3000)
    return body


# ------------------------------------------------------ the old rows ----

@pytest.mark.skip(reason=SUPERSEDED)
def test_case8_detect_speakers_lands_on_its_card_and_the_chip_returns(engine, base_url, sessions):  # noqa: F811
    pass


@pytest.mark.skip(reason=SUPERSEDED)
def test_every_deep_link_lands_on_the_card_its_label_names(engine, base_url, sessions):  # noqa: F811
    pass


@pytest.mark.skip(reason=SUPERSEDED)
def test_rows_and_back_chip_work_from_the_keyboard(engine, base_url, sessions):  # noqa: F811
    pass


@pytest.mark.skip(reason=SUPERSEDED)
def test_the_back_chip_goes_away_on_the_next_tab_change(engine, base_url, sessions):  # noqa: F811
    pass


def test_every_catalogue_card_is_titled_by_its_label(engine, base_url, sessions):  # noqa: F811
    import httpx
    # The panel shows a card for every catalogue tool the engine advertises
    # (/api/tools), so a tool this build leaves out has no card to find.
    advertised = {t["name"] for t in httpx.get(f"{base_url}/api/tools", timeout=30).json().get("tools", [])}
    page = _open(engine, base_url, sessions["full"], 1440, 900)
    _ai_media(page)
    for tool, name in TOOLS.items():
        head = page.locator(f"button.ai-card-head[aria-controls='ai-body-{tool}']")
        if advertised and tool not in advertised:
            assert head.count() == 0, tool
            continue
        assert head.count() == 1, tool
        assert head.locator(".ai-card-title").inner_text() == name, (tool, name)
    assert page.errors == []
    page.context.close()


def test_a_card_mirrors_the_feature_report(engine, base_url, sessions):  # noqa: F811
    """Stubbed reports: stems missing, the TTS voice not downloaded yet. The
    cards' badges say what the old rows said, from the same sources."""
    def features(route):
        route.fulfill(json={
            "packaged_app": False, "python": "3.13", "anthropic_key_set": False, "summary": "", "available": [],
            "unavailable": [{"key": "stems", "feature": "Voice separation", "tools": ["vocal_isolate"],
                             "fix": "uv sync --extra stems"}]})

    def downloads(route):
        route.fulfill(json={"downloads": {"tts": {"what": "the voiceover voice", "bytes": 63_000_000, "cached": False}}})

    page = _open(engine, base_url, sessions["full"], routes={"**/api/features*": features, "**/api/downloads": downloads})
    _ai_media(page)
    card = page.locator(".ai-card:has(> .ai-card-head[aria-controls='ai-body-vocal_isolate'])")
    card.locator(".ai-badge", has_text="Not installed").wait_for(timeout=15000)
    card = page.locator(".ai-card:has(> .ai-card-head[aria-controls='ai-body-tts_voiceover'])")
    card.locator(".ai-badge", has_text="Downloads 63 MB first").wait_for(timeout=15000)
    page.context.close()


# ------------------------------------------- the card does what it says ----

def _run_card(page, tool, timeout=60000):
    body = page.locator(f"#ai-body-{tool}")
    body.locator("button.ai-run").click()
    page.locator(f".ai-card:has(> .ai-card-head[aria-controls='ai-body-{tool}']) .ai-state-done").wait_for(timeout=timeout)


def test_hook_overlay_from_the_catalogue_adds_that_text(engine, base_url, tmp_path_factory):  # noqa: F811
    sid = _project(base_url, tmp_path_factory, f"dl hook {engine.engine_name}")
    page = _open(engine, base_url, sid)
    body = _open_tool(page, "add_hook_overlay")
    words = f"Deep link hook {engine.engine_name}"
    body.get_by_label(re.compile("^Text")).fill(words)
    _run_card(page, "add_hook_overlay")
    texts = [c.get("text") for t in _edl(base_url, sid)["tracks"] for c in t["clips"]]
    assert words in texts, texts
    page.context.close()


def _select_v1_clip(page):
    """Select the v1 clip the way a user does: click it on the timeline."""
    box = page.locator(".timeline-canvas-wrap canvas").first.bounding_box()
    page.mouse.click(box["x"] + 130, box["y"] + 42)
    page.wait_for_timeout(300)


def test_chroma_key_from_the_catalogue_keys_the_selected_clip(engine, base_url, tmp_path_factory):  # noqa: F811
    sid = _project(base_url, tmp_path_factory, f"dl chroma {engine.engine_name}")
    page = _open(engine, base_url, sid)
    _select_v1_clip(page)
    _open_tool(page, "chroma_key")
    before = json.dumps(_edl(base_url, sid)["tracks"][0]["clips"][0], sort_keys=True)
    _run_card(page, "chroma_key")
    clip = _edl(base_url, sid)["tracks"][0]["clips"][0]
    assert json.dumps(clip, sort_keys=True) != before
    assert "chroma" in json.dumps(clip).lower(), clip
    page.context.close()


def test_import_then_export_subtitles_from_the_catalogue(engine, base_url, tmp_path_factory):  # noqa: F811
    sid = _project(base_url, tmp_path_factory, f"dl srt {engine.engine_name}")
    srt = tmp_path_factory.mktemp("dl-srt") / "deep_link.srt"
    line = f"Hello from the deep link in {engine.engine_name}"
    srt.write_text(f"1\n00:00:00,500 --> 00:00:02,500\n{line}\n", encoding="utf-8")
    page = _open(engine, base_url, sid)

    body = _open_tool(page, "import_srt")
    body.locator("input[type=file]").set_input_files(str(srt))
    _run_card(page, "import_srt")

    _open_tool(page, "export_srt")
    _run_card(page, "export_srt")
    # The card's result view carries the handler's answer, path included.
    state = page.locator("#ai-body-export_srt .ai-state-done").text_content()
    wrote = re.search(r'"path":\s*"([^"]+\.srt)"', state)
    assert wrote, state
    out = Path(wrote.group(1))
    assert out.exists(), out
    assert line in out.read_text(encoding="utf-8")
    page.context.close()


def test_lower_third_and_brand_kit_from_the_catalogue_do_what_they_say(engine, base_url, tmp_path_factory):  # noqa: F811
    sid = _project(base_url, tmp_path_factory, f"dl lower third {engine.engine_name}")
    page = _open(engine, base_url, sid)
    who = f"Ada Deeplink {engine.engine_name}"
    body = _open_tool(page, "add_lower_third")
    body.get_by_label(re.compile("^Name")).fill(who)
    _run_card(page, "add_lower_third")
    lts = [c for t in _edl(base_url, sid)["tracks"] for c in t["clips"] if c.get("role") == "lower_third"]
    assert [c["text"] for c in lts] == [who], lts

    handle = f"@deeplink_{engine.engine_name}"
    body = _open_tool(page, "apply_brand_kit")
    body.get_by_label(re.compile("^Handle")).fill(handle)
    _run_card(page, "apply_brand_kit")
    kit = _edl(base_url, sid).get("brand_kit") or {}
    assert kit.get("handle") == handle, kit
    page.context.close()


def test_reduce_noise_from_the_catalogue_denoises_the_selected_clip(engine, base_url, tmp_path_factory):  # noqa: F811
    import subprocess
    sid = _project(base_url, tmp_path_factory, f"dl denoise {engine.engine_name}")
    page = _open(engine, base_url, sid)
    before = _edl(base_url, sid)["tracks"][0]["clips"][0]["src"]
    _select_v1_clip(page)
    _open_tool(page, "noise_reduce")
    _run_card(page, "noise_reduce", timeout=120000)
    after = _edl(base_url, sid)["tracks"][0]["clips"][0]["src"]
    assert after != before and "denoise" in after, (before, after)
    # A real file with the clip's picture and a (cleaned) audio stream.
    streams = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", after],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", check=True).stdout.split()
    assert sorted(streams) == ["audio", "video"], streams
    page.context.close()


def test_find_broll_from_the_catalogue_finds_the_matching_file(engine, base_url, sessions, tmp_path_factory):  # noqa: F811
    bin_dir = tmp_path_factory.mktemp("dl-broll")
    _clip(bin_dir / "sunset_beach_waves.mp4")
    _clip(bin_dir / "office_keyboard.mp4")
    page = _open(engine, base_url, sessions["full"])
    body = _open_tool(page, "find_broll")
    body.get_by_label(re.compile("^Query")).fill("sunset")
    body.get_by_label(re.compile("^B-roll folder")).fill(str(bin_dir))
    _run_card(page, "find_broll")
    state = page.locator("#ai-body-find_broll .ai-state-done").text_content()
    assert "sunset_beach_waves.mp4" in state and "office_keyboard" not in state, state
    page.context.close()


# ---------------------------------------------------------- screenshots ----

@pytest.mark.parametrize("size", [(1024, 768), (1280, 800), (1440, 900), (1920, 1080)])
def test_r5_reference_screenshots(engine, base_url, sessions, size):  # noqa: F811
    page = _open(engine, base_url, sessions["full"], *size)
    _ai_media(page)
    # Nothing in a card head spills out of the panel, at any width.
    spill = page.evaluate("""() => [...document.querySelectorAll('.ai-card-head')]
      .filter(r => r.scrollWidth > r.clientWidth + 1).map(r => r.getAttribute('aria-controls'))""")
    assert spill == [], (size, spill)
    page.screenshot(path=str(SHOTS / f"rail-r5-{engine.engine_name}-ai-{size[0]}x{size[1]}.png"))
    assert page.evaluate("document.documentElement.scrollWidth") <= size[0]
    page.context.close()
