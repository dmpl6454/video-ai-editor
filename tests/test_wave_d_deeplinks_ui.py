"""Wave D, left tool rail, phase R5 — the tool panels' AI deep links, measured
in a real browser, in Chromium AND WebKit (docs/design/LEFT_RAIL_SPEC.md §2.5
M5, §8.3 case 8).

A deep-link row in Media, Audio, Text, Effects or Captions names ONE catalogue
tool. A click selects the AI panel, clears its search, expands THAT tool's
existing card through its own toggle, scrolls the card to just under the
panel's sticky search head and focuses the toggle; a back chip ("‹ Captions")
returns to the origin panel, restores its scroll and focuses the row, and goes
away on the next tab change. "All ‹group› tools (n)" lands on the group's
heading. Each row mirrors its card's status from the same sources.

What each test pins:
- case 8 exactly as the spec words it (Captions → Detect speakers and back);
- EVERY row and group link lands on the card (or heading) its label names,
  and the back chip returns to it (21 jumps per engine);
- the landing controls do what their labels say, at least one per panel:
  Find b-roll (Media) finds the matching file in a folder; Reduce noise
  (Audio) replaces the selected clip with a denoised file; Hook overlay,
  Lower third and Brand kit (Text) add that text, that name and that handle;
  Chroma key (Effects) keys the selected clip; Import subtitles then Export
  .srt (Captions) round-trips a subtitle file — each through the card a deep
  link opened, against the real backend. The rest need a model download, an
  API key or a transcript this harness does not have (no downloads);
- the keyboard path (Enter on a row, Enter on the back chip);
- the back chip's lifetime, and the row status mirroring the card's badge.

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

# Panel id → (rail label, [(tool, catalogue label)], All-link group or None).
LINKS = {
    "media": ("Media", [("search_media", "Search footage"), ("find_broll", "Find b-roll")], "Find & search"),
    "audio": ("Audio", [("noise_reduce", "Reduce noise"), ("vocal_isolate", "Isolate vocals"),
                        ("instrumental_isolate", "Isolate instrumental"), ("tts_voiceover", "AI voiceover")], None),
    "text": ("Text", [("add_hook_overlay", "Hook overlay"), ("generate_hook", "Suggest hooks"),
                      ("add_lower_third", "Lower third"), ("apply_brand_kit", "Brand kit")], "Text & brand"),
    "effects": ("Effects", [("remove_background", "Remove background"), ("chroma_key", "Chroma key")],
                "Cutout & effects"),
    "captions": ("Captions", [("translate_captions", "Translate captions"), ("diarize", "Detect speakers"),
                              ("name_speakers", "Name speakers"), ("import_srt", "Import subtitles"),
                              ("export_srt", "Export .srt")], "Captions & speech"),
}
GROUP_COUNTS = {"Find & search": 5, "Text & brand": 7, "Cutout & effects": 4, "Captions & speech": 10}


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
    items = {"vai.sessionId": sid, "vai.rightTab": "inspect", "vai.leftTab": "media", "vai.leftOpen": "true"}
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
    page.get_by_role("tab", name="Media", exact=True).wait_for()
    page.locator(".timeline-canvas-wrap canvas").first.wait_for()
    page.wait_for_timeout(1200)
    return page


def _tab(page, label):
    return page.get_by_role("tab", name=label, exact=True)


def _show(page, label):
    """Show a panel without moving focus (what a chord does). A click on the
    tab already shown would collapse the panel instead, so that one is left."""
    tab = _tab(page, label)
    if tab.get_attribute("aria-selected") == "true" and page.locator("#tool-panel").is_visible():
        return
    tab.evaluate("el => el.click()")
    page.wait_for_timeout(150)


def _row(page, panel, key):
    return page.locator(f'#tool-panel-{panel} [data-deep-link="{key}"]')


def _back(page):
    return page.locator("#tool-panel-ai").get_by_role("button", name=re.compile(r"^Back to "))


# Where the jump landed, measured in the page: the focused element, its card,
# the AI panel's search and back chip, and where the target sits against the
# sticky search head of the scrolled tool panel.
LANDED_JS = """() => {
  const a = document.activeElement
  const scroller = document.getElementById('tool-panel-ai')
  const head = scroller.querySelector('.ai-panel-head')
  const card = a && a.closest('.ai-card')
  const target = card || a
  const t = target.getBoundingClientRect(), h = head.getBoundingClientRect(), s = scroller.getBoundingClientRect()
  const chip = document.querySelector('.ai-back-chip')
  return {
    tag: a.tagName, id: a.id, controls: a.getAttribute('aria-controls'), expanded: a.getAttribute('aria-expanded'),
    text: (a.textContent || '').trim(),
    title: card ? card.querySelector('.ai-card-title').textContent : null,
    hasBody: !!(card && card.querySelector('.ai-card-body')),
    hasRun: !!(card && card.querySelector('.ai-card-body button.ai-run')),
    aiSelected: document.getElementById('rail-tab-ai').getAttribute('aria-selected'),
    aiHidden: scroller.hidden,
    search: document.querySelector('#tool-panel-ai .ai-search').value,
    chip: chip ? chip.textContent : null,
    gap: t.top - h.bottom,
    visible: t.top >= h.bottom - 1 && t.top < s.bottom,
    atEnd: Math.abs(scroller.scrollTop - (scroller.scrollHeight - scroller.clientHeight)) <= 1,
  }
}"""


def _assert_near_top(landed, what):
    # Just under the sticky head — or, for a target so close to the end of the
    # list that it cannot scroll up that far, fully scrolled and on screen.
    assert landed["visible"], (what, landed)
    assert 0 <= landed["gap"] <= 12 or landed["atEnd"], (what, landed)


def _returned(page, panel, key):
    return page.evaluate("""([panel, key]) => ({
      focused: document.activeElement && document.activeElement.dataset.deepLink,
      inPanel: !!document.activeElement.closest('#tool-panel-' + panel),
      selected: document.getElementById('rail-tab-' + panel).getAttribute('aria-selected'),
      chip: !!document.querySelector('.ai-back-chip'),
      scroll: document.getElementById('tool-panel-' + panel).scrollTop,
    })""", [panel, key])


# ---------------------------------------------------------------- case 8 ----

def test_case8_detect_speakers_lands_on_its_card_and_the_chip_returns(engine, base_url, sessions):  # noqa: F811
    page = _open(engine, base_url, sessions["full"], 1280, 800)
    # A leftover search that would hide the card: the jump must clear it.
    _show(page, "AI")
    page.locator("#tool-panel-ai .ai-search").fill("zzz no such tool")
    _show(page, "Captions")
    # Scroll the Captions panel so the row sits low: the chip must restore it.
    scroll = page.evaluate("""() => { const p = document.getElementById('tool-panel-captions')
      p.scrollTop = p.scrollHeight; return p.scrollTop }""")
    row = page.locator("#tool-panel-captions").get_by_role("button", name="Detect speakers", exact=True)
    assert row.count() == 1
    row.click()
    page.wait_for_timeout(300)

    landed = page.evaluate(LANDED_JS)
    assert landed["aiSelected"] == "true" and landed["aiHidden"] is False, landed
    assert landed["search"] == "", landed
    assert landed["controls"] == "ai-body-diarize" and landed["expanded"] == "true", landed
    assert landed["title"] == "Detect speakers" and landed["hasRun"], landed
    _assert_near_top(landed, "diarize")
    assert page.locator(".ai-card:has(> .ai-card-head[aria-controls='ai-body-diarize'])").count() == 1
    chip = _back(page)
    assert chip.count() == 1 and chip.is_visible()
    # "‹ Captions" on screen, named "Back to Captions" (the words are sr-only).
    assert page.get_by_role("button", name="Back to Captions", exact=True).count() == 1
    assert page.evaluate("getComputedStyle(document.querySelector('.ai-back-chip .deep-sr-only')).clipPath") == "inset(50%)"
    page.screenshot(path=str(SHOTS / f"rail-r5-{engine.engine_name}-case8-landed.png"))

    chip.click()
    page.wait_for_timeout(300)
    back = _returned(page, "captions", "tool:diarize")
    assert back["selected"] == "true" and back["focused"] == "tool:diarize" and back["inPanel"], back
    assert back["chip"] is False, back
    assert abs(back["scroll"] - scroll) <= 1, (back, scroll)
    assert page.errors == []
    page.context.close()


# ------------------------------------------------------ every deep link ----

def test_every_deep_link_lands_on_the_card_its_label_names(engine, base_url, sessions):  # noqa: F811
    page = _open(engine, base_url, sessions["full"], 1440, 900)
    for panel, (label, tools, group) in LINKS.items():
        for tool, name in tools:
            _show(page, label)
            # Named by its label alone: the status is a description, not the name.
            assert page.locator(f"#tool-panel-{panel}").get_by_role("button", name=name, exact=True).count() == 1, name
            row = _row(page, panel, f"tool:{tool}")
            row.scroll_into_view_if_needed()     # what the click would do; then the origin scroll is fixed
            origin = page.evaluate(f"document.getElementById('tool-panel-{panel}').scrollTop")
            row.click()
            page.wait_for_timeout(250)
            landed = page.evaluate(LANDED_JS)
            assert landed["aiSelected"] == "true" and landed["search"] == "", (tool, landed)
            assert landed["controls"] == f"ai-body-{tool}" and landed["expanded"] == "true", (tool, landed)
            assert landed["title"] == name and landed["hasBody"], (tool, landed)
            assert landed["chip"] == f"Back to {label}", (tool, landed)
            _assert_near_top(landed, tool)
            _back(page).click()
            page.wait_for_timeout(200)
            back = _returned(page, panel, f"tool:{tool}")
            assert back["selected"] == "true" and back["focused"] == f"tool:{tool}" and not back["chip"], (tool, back)
            assert abs(back["scroll"] - origin) <= 1, (tool, back, origin)
        if group:
            _show(page, label)
            link = page.locator(f"#tool-panel-{panel}").get_by_role(
                "button", name=f"All {group} tools ({GROUP_COUNTS[group]})", exact=True)
            assert link.count() == 1, group
            link.click()
            page.wait_for_timeout(250)
            landed = page.evaluate(LANDED_JS)
            assert landed["tag"] == "H3" and landed["text"] == group, (group, landed)
            assert landed["id"] == "ai-group-" + re.sub(r"\W+", "-", group).lower(), landed
            assert landed["search"] == "" and landed["chip"] == f"Back to {label}", landed
            _assert_near_top(landed, group)
            # The group holds as many cards as the link says.
            n = page.evaluate("id => document.getElementById(id).closest('section').querySelectorAll('.ai-card').length",
                              landed["id"])
            assert n == GROUP_COUNTS[group], (group, n)
            _back(page).click()
            page.wait_for_timeout(200)
            back = _returned(page, panel, f"group:{group}")
            assert back["focused"] == f"group:{group}", (group, back)
    assert page.errors == []
    page.context.close()


def test_rows_and_back_chip_work_from_the_keyboard(engine, base_url, sessions):  # noqa: F811
    page = _open(engine, base_url, sessions["full"])
    _show(page, "Text")
    _row(page, "text", "tool:add_lower_third").focus()
    page.keyboard.press("Enter")
    page.wait_for_timeout(250)
    landed = page.evaluate(LANDED_JS)
    assert landed["controls"] == "ai-body-add_lower_third" and landed["expanded"] == "true", landed
    # The card's form is live under focus: Tab moves into it, not out of the panel.
    _back(page).focus()
    page.keyboard.press("Enter")
    page.wait_for_timeout(250)
    back = _returned(page, "text", "tool:add_lower_third")
    assert back["focused"] == "tool:add_lower_third" and back["selected"] == "true", back
    page.context.close()


def test_the_back_chip_goes_away_on_the_next_tab_change(engine, base_url, sessions):  # noqa: F811
    page = _open(engine, base_url, sessions["full"])
    _show(page, "Audio")
    _row(page, "audio", "tool:tts_voiceover").click()
    page.wait_for_timeout(250)
    assert _back(page).count() == 1
    _tab(page, "Media").click()
    _tab(page, "AI").click()
    page.wait_for_timeout(200)
    assert _back(page).count() == 0
    # A jump that is not followed back leaves the card as the user left it.
    assert page.locator("button.ai-card-head[aria-controls='ai-body-tts_voiceover']").get_attribute("aria-expanded") == "true"
    page.context.close()


def test_a_row_mirrors_its_cards_status(engine, base_url, sessions):  # noqa: F811
    """Stubbed reports: stems missing, the TTS voice not downloaded yet. The
    rows say what the cards say, from the same sources."""
    def features(route):
        route.fulfill(json={
            "packaged_app": False, "python": "3.13", "anthropic_key_set": False, "summary": "", "available": [],
            "unavailable": [{"key": "stems", "feature": "Voice separation", "tools": ["vocal_isolate"],
                             "fix": "uv sync --extra stems"}]})

    def downloads(route):
        route.fulfill(json={"downloads": {"tts": {"what": "the voiceover voice", "bytes": 63_000_000, "cached": False}}})

    page = _open(engine, base_url, sessions["full"], routes={"**/api/features*": features, "**/api/downloads": downloads})
    _show(page, "Audio")      # an Audio panel on screen loads the catalogue
    status = page.locator("#tool-panel-audio .deep-link-status")
    status.first.wait_for(timeout=15000)
    got = {page.evaluate("el => el.closest('[data-deep-link]').dataset.deepLink", s.element_handle()): s.inner_text()
           for s in status.all()}
    assert got == {"tool:vocal_isolate": "Not installed", "tool:instrumental_isolate": "Not installed",
                   "tool:tts_voiceover": "Downloads 63 MB first"}, got
    # The status is the row's description; the name stays the label.
    row = page.locator("#tool-panel-audio").get_by_role("button", name="Isolate vocals", exact=True)
    desc = row.get_attribute("aria-describedby")
    assert desc and page.evaluate("id => document.getElementById(id).textContent", desc) == "Not installed"
    # ...and it is the card's own badge.
    row.click()
    page.wait_for_timeout(250)
    badges = page.locator(".ai-card:has(> .ai-card-head[aria-controls='ai-body-vocal_isolate']) .ai-badge").all_text_contents()
    assert "Not installed" in [b.strip() for b in badges], badges
    _show(page, "Audio")
    _row(page, "audio", "tool:tts_voiceover").click()
    page.wait_for_timeout(250)
    badges = page.locator(".ai-card:has(> .ai-card-head[aria-controls='ai-body-tts_voiceover']) .ai-badge").all_text_contents()
    assert "Downloads 63 MB first" in [b.strip() for b in badges], badges
    page.context.close()


# ------------------------------------------- the landing does what it says ----

def _run_card(page, tool, timeout=60000):
    body = page.locator(f"#ai-body-{tool}")
    body.locator("button.ai-run").click()
    page.locator(f".ai-card:has(> .ai-card-head[aria-controls='ai-body-{tool}']) .ai-state-done").wait_for(timeout=timeout)


def test_hook_overlay_from_the_text_panel_adds_that_text(engine, base_url, tmp_path_factory):  # noqa: F811
    sid = _project(base_url, tmp_path_factory, f"dl hook {engine.engine_name}")
    page = _open(engine, base_url, sid)
    _show(page, "Text")
    page.locator("#tool-panel-text").get_by_role("button", name="Hook overlay", exact=True).click()
    page.wait_for_timeout(250)
    assert page.evaluate(LANDED_JS)["controls"] == "ai-body-add_hook_overlay"
    words = f"Deep link hook {engine.engine_name}"
    field = page.locator("#ai-body-add_hook_overlay").get_by_label(re.compile("^Text"))
    field.fill(words)
    _run_card(page, "add_hook_overlay")
    texts = [c.get("text") for t in _edl(base_url, sid)["tracks"] for c in t["clips"]]
    assert words in texts, texts
    page.context.close()


def test_chroma_key_from_the_effects_panel_keys_the_selected_clip(engine, base_url, tmp_path_factory):  # noqa: F811
    sid = _project(base_url, tmp_path_factory, f"dl chroma {engine.engine_name}")
    page = _open(engine, base_url, sid)
    # Select the v1 clip the way a user does: click it on the timeline.
    box = page.locator(".timeline-canvas-wrap canvas").first.bounding_box()
    page.mouse.click(box["x"] + 130, box["y"] + 42)
    page.wait_for_timeout(300)
    _show(page, "Effects")
    page.locator("#tool-panel-effects").get_by_role("button", name="Chroma key", exact=True).click()
    page.wait_for_timeout(250)
    assert page.evaluate(LANDED_JS)["controls"] == "ai-body-chroma_key"
    before = json.dumps(_edl(base_url, sid)["tracks"][0]["clips"][0], sort_keys=True)
    _run_card(page, "chroma_key")
    clip = _edl(base_url, sid)["tracks"][0]["clips"][0]
    assert json.dumps(clip, sort_keys=True) != before
    assert "chroma" in json.dumps(clip).lower(), clip
    page.context.close()


def test_import_then_export_subtitles_from_the_captions_panel(engine, base_url, tmp_path_factory):  # noqa: F811
    sid = _project(base_url, tmp_path_factory, f"dl srt {engine.engine_name}")
    srt = tmp_path_factory.mktemp("dl-srt") / "deep_link.srt"
    line = f"Hello from the deep link in {engine.engine_name}"
    srt.write_text(f"1\n00:00:00,500 --> 00:00:02,500\n{line}\n", encoding="utf-8")
    page = _open(engine, base_url, sid)

    _show(page, "Captions")
    page.locator("#tool-panel-captions").get_by_role("button", name="Import subtitles", exact=True).click()
    page.wait_for_timeout(250)
    page.locator("#ai-body-import_srt input[type=file]").set_input_files(str(srt))
    _run_card(page, "import_srt")

    _back(page).click()          # back to Captions, then its next row
    page.wait_for_timeout(200)
    page.locator("#tool-panel-captions").get_by_role("button", name="Export .srt", exact=True).click()
    page.wait_for_timeout(250)
    _run_card(page, "export_srt")
    # The card's result view carries the handler's answer, path included.
    state = page.locator("#ai-body-export_srt .ai-state-done").text_content()
    wrote = re.search(r'"path":\s*"([^"]+\.srt)"', state)
    assert wrote, state
    out = Path(wrote.group(1))
    assert out.exists(), out
    assert line in out.read_text(encoding="utf-8")
    page.context.close()


def _select_v1_clip(page):
    """Select the v1 clip the way a user does: click it on the timeline."""
    box = page.locator(".timeline-canvas-wrap canvas").first.bounding_box()
    page.mouse.click(box["x"] + 130, box["y"] + 42)
    page.wait_for_timeout(300)


def _jump(page, panel_label, panel, name, tool):
    _show(page, panel_label)
    page.locator(f"#tool-panel-{panel}").get_by_role("button", name=name, exact=True).click()
    page.wait_for_timeout(250)
    landed = page.evaluate(LANDED_JS)
    assert landed["controls"] == f"ai-body-{tool}" and landed["title"] == name, landed


def test_lower_third_and_brand_kit_from_the_text_panel_do_what_they_say(engine, base_url, tmp_path_factory):  # noqa: F811
    sid = _project(base_url, tmp_path_factory, f"dl lower third {engine.engine_name}")
    page = _open(engine, base_url, sid)
    who = f"Ada Deeplink {engine.engine_name}"
    _jump(page, "Text", "text", "Lower third", "add_lower_third")
    page.locator("#ai-body-add_lower_third").get_by_label(re.compile("^Name")).fill(who)
    _run_card(page, "add_lower_third")
    lts = [c for t in _edl(base_url, sid)["tracks"] for c in t["clips"] if c.get("role") == "lower_third"]
    assert [c["text"] for c in lts] == [who], lts

    _back(page).click()
    page.wait_for_timeout(200)
    handle = f"@deeplink_{engine.engine_name}"
    _jump(page, "Text", "text", "Brand kit", "apply_brand_kit")
    page.locator("#ai-body-apply_brand_kit").get_by_label(re.compile("^Handle")).fill(handle)
    _run_card(page, "apply_brand_kit")
    kit = _edl(base_url, sid).get("brand_kit") or {}
    assert kit.get("handle") == handle, kit
    page.context.close()


def test_reduce_noise_from_the_audio_panel_denoises_the_selected_clip(engine, base_url, tmp_path_factory):  # noqa: F811
    import subprocess
    sid = _project(base_url, tmp_path_factory, f"dl denoise {engine.engine_name}")
    page = _open(engine, base_url, sid)
    before = _edl(base_url, sid)["tracks"][0]["clips"][0]["src"]
    _select_v1_clip(page)
    _jump(page, "Audio", "audio", "Reduce noise", "noise_reduce")
    _run_card(page, "noise_reduce", timeout=120000)
    after = _edl(base_url, sid)["tracks"][0]["clips"][0]["src"]
    assert after != before and "denoise" in after, (before, after)
    # A real file with the clip's picture and a (cleaned) audio stream.
    streams = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", after],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", check=True).stdout.split()
    assert sorted(streams) == ["audio", "video"], streams
    page.context.close()


def test_find_broll_from_the_media_panel_finds_the_matching_file(engine, base_url, sessions, tmp_path_factory):  # noqa: F811
    bin_dir = tmp_path_factory.mktemp("dl-broll")
    _clip(bin_dir / "sunset_beach_waves.mp4")
    _clip(bin_dir / "office_keyboard.mp4")
    page = _open(engine, base_url, sessions["full"])
    _jump(page, "Media", "media", "Find b-roll", "find_broll")
    body = page.locator("#ai-body-find_broll")
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
    for panel, (label, _tools, _group) in LINKS.items():
        _show(page, label)
        page.locator(f"#tool-panel-{panel} .deep-links").scroll_into_view_if_needed()
        # Nothing in a row spills out of the panel, at any width.
        spill = page.evaluate("""p => [...document.querySelectorAll('#tool-panel-' + p + ' .deep-link')]
          .filter(r => r.scrollWidth > r.clientWidth + 1).map(r => r.dataset.deepLink)""", panel)
        assert spill == [], (panel, size, spill)
        page.screenshot(path=str(SHOTS / f"rail-r5-{engine.engine_name}-{panel}-{size[0]}x{size[1]}.png"))
    assert page.evaluate("document.documentElement.scrollWidth") <= size[0]
    page.context.close()
