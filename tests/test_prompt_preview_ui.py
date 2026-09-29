"""Preview, then apply (0.8.0) in a real browser, Chromium AND WebKit.

The key-free Prompt bar dry-runs a plan and shows a PromptPreviewCard; the
timeline changes only on Apply. Driven from the KEYBOARD:

  * `/` focuses the bar, a sentence and Enter plan it; the card appears with
    the change list and "Nothing has changed yet", focus lands on Apply and
    a screen reader hears "Preview ready: …" — the EDL and History (ops) are
    untouched;
  * Escape is Change: the card goes, focus is back in the prompt with the
    sentence kept for editing, nothing committed;
  * Enter plans it again and Enter on Apply commits ONE prompt op, announced
    "Applied";
  * typing a different sentence while a card is open drops the old card;
  * Settings › Prompt bar › "Ask before applying Prompt bar edits" (a switch,
    toggled with Space) turned OFF makes the next prompt apply at once;
  * with reduced motion the card does not animate in.

Harness: VAE_A11Y_BASE_URL = a Vite dev server proxying /api to a backend
started WITHOUT an Anthropic key and without VAI_PROMPT_CONFIRM (the switch
must be what decides). Screenshots go to VAE_PREVIEW_SHOTS.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_frontend_a11y import base_url  # noqa: E402,F401  (fixture)
from test_speed_ui_e2e import _client, _edl, _open, engine, pw  # noqa: E402,F401

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
SHOTS = Path(os.environ.get("VAE_PREVIEW_SHOTS", "/tmp"))
CARD = "[data-testid=prompt-preview]"


@pytest.fixture(scope="module", autouse=True)
def _backend_without_confirm_env():
    """tests/conftest.py sets VAI_PROMPT_CONFIRM=0 for the plan-semantics
    suites; the backend `base_url` starts here must NOT inherit it (the
    Settings switch is what decides). Autouse, so it runs before base_url."""
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("VAI_PROMPT_CONFIRM", raising=False)
        yield


@pytest.fixture(scope="module")
def clip(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("pv_media") / "pv_clip.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:d=8:r=30",
                    "-f", "lavfi", "-i", "sine=frequency=330:duration=8", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(p)], check=True, capture_output=True)
    return p


def _project(base_url, clip: Path, name: str) -> str:  # noqa: F811
    with _client(base_url) as c:
        sid = c.post("/api/sessions", json={"name": f"{name} {uuid.uuid4().hex[:6]}"}).json()["id"]
        with clip.open("rb") as fh:
            r = c.post(f"/api/sessions/{sid}/upload", files={"file": (clip.name, fh, "video/mp4")},
                       data={"add_to_timeline": "true", "transcribe": "false"})
        assert r.status_code in (200, 202), r.text
        deadline = time.time() + 120
        while not c.get(f"/api/sessions/{sid}/edl").json().get("duration"):
            assert time.time() < deadline, "upload never reached the timeline"
            time.sleep(0.3)
        # two clips, so "the second clip" means something
        v1 = next(t for t in c.get(f"/api/sessions/{sid}/edl").json()["tracks"] if t["id"] == "v1")
        assert c.post(f"/api/sessions/{sid}/dispatch", json={"tool": "split_at", "args": {
            "track": "v1", "time": 4.0}}).status_code == 200, v1
    return sid


def _setting(base_url, on: bool) -> None:  # noqa: F811
    with _client(base_url) as c:
        r = c.put("/api/settings/prompt", json={"confirm_before_apply": on})
        assert r.status_code == 200 and r.json()["confirm_before_apply"] is on, r.text
        assert r.json()["source"] != "env", "the backend was started with VAI_PROMPT_CONFIRM set"


def _ops(base_url, sid) -> list[str]:  # noqa: F811
    with _client(base_url) as c:
        body = c.get(f"/api/sessions/{sid}").json()
    return [o.get("tool") for o in body.get("ops", [])]


def _v1_mutes(base_url, sid) -> list[bool]:  # noqa: F811
    v1 = next(t for t in _edl(base_url, sid)["tracks"] if t["id"] == "v1")
    return [bool((c.get("audio") or {}).get("mute")) for c in sorted(v1["clips"], key=lambda c: c["start"])]


def _announced(page) -> str:
    return page.locator(".prompt-bar .prompt-sr-only").inner_text()


def _type_prompt(page, text: str) -> None:
    page.keyboard.press("Slash")
    page.wait_for_function("() => document.activeElement && document.activeElement.classList.contains('prompt-input')")
    page.keyboard.press("ControlOrMeta+a")
    page.keyboard.type(text)
    page.keyboard.press("Enter")


def test_card_flow_keyboard_only(engine, base_url, clip):  # noqa: F811
    _setting(base_url, True)
    sid = _project(base_url, clip, f"PV keys {engine.engine_name}")
    ops0 = _ops(base_url, sid)
    edl0 = _edl(base_url, sid)
    page = _open(engine, base_url, sid, 1440, 900)
    try:
        history_rows = page.locator(".ops-log .op").count()
        _type_prompt(page, "mute the first clip")
        card = page.locator(CARD)
        card.wait_for(timeout=20000)
        assert "Nothing has changed yet." in card.inner_text()
        lines = card.locator(".prompt-preview-list li").all_inner_texts()
        assert any(line.endswith("': muted") and line.startswith("Clip 1 ") for line in lines), lines
        page.wait_for_function("() => document.activeElement && document.activeElement.textContent.trim().startsWith('Apply')")
        page.wait_for_function("() => document.querySelector('.prompt-bar .prompt-sr-only').textContent"
                               ".startsWith('Preview ready: ')")
        assert _edl(base_url, sid) == edl0 and _ops(base_url, sid) == ops0     # nothing committed
        assert page.locator(".ops-log .op").count() == history_rows            # History: applied runs only
        card.screenshot(path=str(SHOTS / f"prompt-preview-card-{engine.engine_name}.png"))

        # Escape = Change: back to the prompt, the sentence kept, nothing committed
        page.keyboard.press("Escape")
        card.wait_for(state="detached")
        page.wait_for_function("() => document.activeElement && document.activeElement.classList.contains('prompt-input')")
        assert page.locator("textarea.prompt-input").input_value() == "mute the first clip"
        assert "nothing was changed" in _announced(page)
        assert _edl(base_url, sid) == edl0 and _ops(base_url, sid) == ops0

        # Enter plans it again; Enter on Apply commits ONE prompt op
        page.keyboard.press("Enter")
        card.wait_for(timeout=20000)
        page.wait_for_function("() => document.activeElement && document.activeElement.textContent.trim().startsWith('Apply')")
        page.keyboard.press("Enter")
        card.wait_for(state="detached", timeout=20000)
        deadline = time.time() + 30
        while _ops(base_url, sid) == ops0:
            assert time.time() < deadline, "Apply never committed"
            time.sleep(0.3)
        assert _ops(base_url, sid) == ops0 + ["prompt"]
        assert _v1_mutes(base_url, sid) == [True, False]
        page.wait_for_function("() => document.querySelector('.prompt-bar .prompt-sr-only').textContent"
                               ".startsWith('Applied')", timeout=30000)
        page.wait_for_function(f"() => document.querySelectorAll('.ops-log .op').length === {history_rows + 1}",
                               timeout=10000)
    finally:
        page.context.close()


def test_a_new_sentence_drops_the_open_card(engine, base_url, clip):  # noqa: F811
    _setting(base_url, True)
    sid = _project(base_url, clip, f"PV new {engine.engine_name}")
    ops0 = _ops(base_url, sid)
    page = _open(engine, base_url, sid, 1440, 900)
    try:
        _type_prompt(page, "mute the first clip")
        page.locator(CARD).wait_for(timeout=20000)
        with _client(base_url) as c:
            first = c.get(f"/api/sessions/{sid}/prompt/pending").json()["pending"]["token"]
        # a different sentence typed while the card is open, then Enter
        page.focus("textarea.prompt-input")
        page.fill("textarea.prompt-input", "mute the second clip")
        page.keyboard.press("Enter")
        page.wait_for_function(
            "() => [...document.querySelectorAll('.prompt-preview-list li')].some((li) => li.textContent.startsWith('Clip 2 '))",
            timeout=20000)
        with _client(base_url) as c:
            second = c.get(f"/api/sessions/{sid}/prompt/pending").json()["pending"]["token"]
        assert second != first
        assert page.locator(CARD).count() == 1
        assert _ops(base_url, sid) == ops0 and _v1_mutes(base_url, sid) == [False, False]
    finally:
        page.context.close()


def test_switch_off_applies_at_once(engine, base_url, clip):  # noqa: F811
    _setting(base_url, True)
    sid = _project(base_url, clip, f"PV off {engine.engine_name}")
    ops0 = _ops(base_url, sid)
    page = _open(engine, base_url, sid, 1440, 900)
    try:
        page.keyboard.press("ControlOrMeta+Comma")
        switch = page.get_by_role("switch", name="Ask before applying Prompt bar edits")
        switch.wait_for()
        page.wait_for_function("() => { const s = document.querySelector('[data-testid=prompt-confirm-switch]');"
                               " return s && !s.disabled }")
        assert switch.is_checked()
        switch.focus()
        page.keyboard.press("Space")
        page.wait_for_function("() => !document.querySelector('[data-testid=prompt-confirm-switch]').checked")
        with _client(base_url) as c:
            assert c.get("/api/settings/prompt").json()["confirm_before_apply"] is False
        page.locator(".settings-dialog").screenshot(path=str(SHOTS / f"prompt-setting-{engine.engine_name}.png"))
        page.keyboard.press("Escape")
        page.locator(".settings-dialog").wait_for(state="detached")
        _type_prompt(page, "mute the second clip")
        deadline = time.time() + 30
        while _ops(base_url, sid) == ops0:
            assert time.time() < deadline, "the prompt never applied"
            time.sleep(0.3)
        assert page.locator(CARD).count() == 0
        assert _ops(base_url, sid) == ops0 + ["prompt"] and _v1_mutes(base_url, sid) == [False, True]
    finally:
        page.context.close()
        _setting(base_url, True)


def test_reduced_motion_card_does_not_animate(engine, base_url, clip):  # noqa: F811
    _setting(base_url, True)
    sid = _project(base_url, clip, f"PV rm {engine.engine_name}")
    ctx = engine.new_context(viewport={"width": 1280, "height": 800}, reduced_motion="reduce")
    ctx.add_init_script(f"try {{ localStorage.setItem('vai.sessionId', {sid!r}) }} catch (e) {{}}")
    page = ctx.new_page()
    try:
        page.goto(base_url + "/?vae-test")
        page.locator(".timeline-canvas-wrap canvas").first.wait_for()
        page.wait_for_timeout(800)
        _type_prompt(page, "mute the first clip")
        page.locator(CARD).wait_for(timeout=20000)
        n = page.evaluate("() => document.querySelector('[data-testid=prompt-preview]').getAnimations().length")
        assert n == 0
        # a long list scrolls inside the card instead of pushing the picture away
        box = page.evaluate("() => { const s = getComputedStyle(document.querySelector('.prompt-preview-list'));"
                            " return [s.overflowY, s.maxHeight] }")
        assert box == ["auto", "168px"], box
        # the card's own tokens: no hard-coded colours on it
        style = page.evaluate("() => getComputedStyle(document.querySelector('.prompt-preview-kicker')).color")
        assert style and style != "rgb(0, 0, 0)"
    finally:
        ctx.close()


def _card_open_on_apply(page) -> None:
    page.locator(CARD).wait_for(timeout=20000)
    page.wait_for_function("() => document.activeElement && document.activeElement.textContent.trim().startsWith('Apply')")


def _unchanged_for(base_url, sid, edl0, ops0, seconds: float = 1.5) -> None:  # noqa: F811
    deadline = time.time() + seconds
    while time.time() < deadline:
        assert _edl(base_url, sid) == edl0 and _ops(base_url, sid) == ops0, "the timeline changed without Apply"
        time.sleep(0.25)


def test_typing_over_the_card_never_applies(engine, base_url, clip):  # noqa: F811
    """CRITICAL (final sweep 3): the card focuses Apply a frame after it
    appears; typing a new sentence ("no wait") pressed Apply at the first
    SPACE and committed the old plan. Typing now goes to the prompt, and the
    card stays until Enter plans the new sentence."""
    _setting(base_url, True)
    sid = _project(base_url, clip, f"PV type {engine.engine_name}")
    ops0, edl0 = _ops(base_url, sid), _edl(base_url, sid)
    page = _open(engine, base_url, sid, 1440, 900)
    try:
        _type_prompt(page, "mute the first clip")
        _card_open_on_apply(page)
        page.keyboard.type("no wait")
        _unchanged_for(base_url, sid, edl0, ops0)
        assert page.locator(CARD).count() == 1
        assert page.evaluate("() => document.activeElement.classList.contains('prompt-input')")
        assert page.locator("textarea.prompt-input").input_value() == "no wait"
        page.keyboard.press("Space")                    # a space typed in the prompt is a space
        _unchanged_for(base_url, sid, edl0, ops0, 0.5)
        assert page.locator("textarea.prompt-input").input_value() == "no wait "
    finally:
        page.context.close()


def test_slash_then_a_new_sentence_plans_it(engine, base_url, clip):  # noqa: F811
    """'/' on the card goes to the prompt with its text selected (it used to
    leave focus on Apply, so the space in 'mute clip 3' applied the card)."""
    _setting(base_url, True)
    sid = _project(base_url, clip, f"PV slash {engine.engine_name}")
    ops0, edl0 = _ops(base_url, sid), _edl(base_url, sid)
    page = _open(engine, base_url, sid, 1440, 900)
    try:
        _type_prompt(page, "mute the first clip")
        _card_open_on_apply(page)
        page.keyboard.press("Slash")
        page.wait_for_function("() => document.activeElement.classList.contains('prompt-input')")
        page.keyboard.type("mute the second clip")
        _unchanged_for(base_url, sid, edl0, ops0)
        assert page.locator("textarea.prompt-input").input_value() == "mute the second clip"
        page.keyboard.press("Enter")
        page.wait_for_function(
            "() => [...document.querySelectorAll('.prompt-preview-list li')].some((li) => li.textContent.startsWith('Clip 2 '))",
            timeout=20000)
        _unchanged_for(base_url, sid, edl0, ops0, 0.5)
    finally:
        page.context.close()


def test_a_capped_card_opens_to_show_every_line(engine, base_url, clip):  # noqa: F811
    """HIGH (final sweep 3): 'and 2 more changes' was plain text — the two
    hidden changes (one trimmed the music) could not be seen before Apply.
    The row is a keyboard-operable disclosure now."""
    _setting(base_url, True)
    sid = _project(base_url, clip, f"PV more {engine.engine_name}")
    page = _open(engine, base_url, sid, 1440, 900)
    lines = [f"Change number {i}" for i in range(11)]
    hidden = ["Music 'bed.wav': length 12.0 s -> 6.9 s (ends at 00:00:06:28)",
              "2 later clips move to keep the video continuous"]
    frames = [
        {"type": "brain", "status": "answered", "brain": "recipes", "label": "Recipes"},
        {"type": "clarify", "token": "q_capped", "plan_id": "p_1", "expires_in_s": 600,
         "questions": [{"key": "apply", "question": "Apply these changes?", "kind": "confirm", "required": True,
                        "options": [{"value": "yes", "label": "Apply"}, {"value": "no", "label": "Change"}]}],
         "preview": {"summary": "Reel: 13 changes", "lines": lines, "more": 2, "total": 13, "hidden": hidden,
                     "note": None, "nothing_changed": "Nothing has changed yet."}},
        {"type": "done"}]
    import json as _json
    body = "".join(f"data: {_json.dumps(f)}\n\n" for f in frames)
    page.route(f"**/api/sessions/{sid}/prompt", lambda route: route.fulfill(
        status=200, headers={"content-type": "text/event-stream"}, body=body)
        if route.request.method == "POST" else route.continue_())
    try:
        _type_prompt(page, "make it a 10 second reel")
        _card_open_on_apply(page)
        more = page.get_by_role("button", name="and 2 more changes")
        assert more.get_attribute("aria-expanded") == "false"
        more.focus()
        page.keyboard.press("Enter")
        page.wait_for_function("() => document.querySelector('.prompt-preview-more').getAttribute('aria-expanded') === 'true'")
        shown = page.locator(".prompt-preview-list li").all_inner_texts()
        assert all(h in shown for h in hidden), shown
        page.locator(CARD).screenshot(path=str(SHOTS / f"prompt-preview-capped-{engine.engine_name}.png"))
    finally:
        page.context.close()


# ---------------------------------------------------------------- final sweep 3 r2


def _hold_prompt(page, sid: str) -> list:
    """Hold the next POST …/prompt (a slow dry run: captions, silences,
    stabilise) until the test releases it."""
    held: list = []

    def handler(route):
        if route.request.method == "POST":
            held.append(route)
        else:
            route.continue_()
    page.route(f"**/api/sessions/{sid}/prompt", handler)
    return held


def test_a_card_never_takes_focus_from_the_timecode_field(engine, base_url, clip):  # noqa: F811
    """CRITICAL: the card focused Apply when it appeared, from ANY field — the
    Enter a person pressed to seek the Playhead timecode applied the card."""
    _setting(base_url, True)
    sid = _project(base_url, clip, f"PV focus {engine.engine_name}")
    ops0, edl0 = _ops(base_url, sid), _edl(base_url, sid)
    page = _open(engine, base_url, sid, 1440, 900)
    try:
        held = _hold_prompt(page, sid)
        _type_prompt(page, "mute the first clip")
        deadline = time.time() + 10
        while not held:
            assert time.time() < deadline, "the prompt was never sent"
            page.wait_for_timeout(50)
        tc = page.get_by_label("Playhead timecode")
        tc.click()
        page.keyboard.press("ControlOrMeta+a")
        page.keyboard.type("00:00:05:00")
        held[0].continue_()
        page.locator(CARD).wait_for(timeout=20000)
        page.wait_for_timeout(400)                       # the card's rAF focus has run
        assert page.evaluate("() => document.activeElement && document.activeElement.getAttribute('aria-label')") \
            == "Playhead timecode"
        page.keyboard.press("Enter")                      # meant for the seek
        _unchanged_for(base_url, sid, edl0, ops0)
        assert page.locator(CARD).count() == 1            # the card waits for a deliberate Apply
    finally:
        page.context.close()


def test_typing_yes_over_the_card_applies_it(engine, base_url, clip):  # noqa: F811
    """MEDIUM: the card says "Reply yes to apply"; typing yes in the bar
    dropped the card and asked 'Which of these did you mean?'."""
    _setting(base_url, True)
    sid = _project(base_url, clip, f"PV yes {engine.engine_name}")
    ops0 = _ops(base_url, sid)
    page = _open(engine, base_url, sid, 1440, 900)
    try:
        _type_prompt(page, "mute the first clip")
        _card_open_on_apply(page)
        page.focus("textarea.prompt-input")
        page.fill("textarea.prompt-input", "yes")
        page.keyboard.press("Enter")
        page.locator(CARD).wait_for(state="detached", timeout=20000)
        deadline = time.time() + 30
        while _ops(base_url, sid) == ops0:
            assert time.time() < deadline, "yes never applied the card"
            time.sleep(0.3)
        assert _ops(base_url, sid) == ops0 + ["prompt"] and _v1_mutes(base_url, sid) == [True, False]
        assert "Which of these did you mean" not in page.locator(".prompt-bar").inner_text()
    finally:
        page.context.close()


def test_a_card_dropped_elsewhere_does_not_stick_the_bar(engine, base_url, clip):  # noqa: F811
    """MEDIUM: a card that expired (or was answered 'no' in another window)
    left the bar in 'clarify' with no card after a reload — Enter and Run on
    the same sentence did nothing at all."""
    _setting(base_url, True)
    sid = _project(base_url, clip, f"PV stuck {engine.engine_name}")
    with _client(base_url) as c:
        body = c.post(f"/api/sessions/{sid}/prompt", json={"message": "make it black and white"}).text
        assert '"preview"' in body, body[:400]
        # "no" typed as the next message (the Chat pane, the phone): the card
        # is dropped, the run record still says "clarify"
        assert "Dropped the preview" in c.post(f"/api/sessions/{sid}/prompt", json={"message": "no"}).text
        assert c.get(f"/api/sessions/{sid}/prompt/pending").json()["pending"] is None
        assert c.get(f"/api/sessions/{sid}/prompt/run").json()["run"]["status"] == "clarify"
    page = _open(engine, base_url, sid, 1440, 900)
    try:
        posts: list[str] = []
        page.on("request", lambda r: posts.append(r.url) if r.method == "POST" and r.url.endswith("/prompt") else None)
        _type_prompt(page, "make it black and white")
        page.locator(CARD).wait_for(timeout=20000)
        assert posts, "Enter sent nothing"
    finally:
        page.context.close()


def test_a_preview_that_changes_nothing_is_not_done(engine, base_url, clip):  # noqa: F811
    """MEDIUM: 'unmute everything' with nothing muted showed '✓ Mute 2 steps
    done' and announced 'Done'."""
    _setting(base_url, True)
    sid = _project(base_url, clip, f"PV noop {engine.engine_name}")
    page = _open(engine, base_url, sid, 1440, 900)
    try:
        _type_prompt(page, "unmute everything")
        live = ".prompt-bar .prompt-sr-only[aria-live]"
        page.wait_for_function(f"() => document.querySelector({live!r}).textContent.length > 0", timeout=20000)
        said = page.locator(live).inner_text()
        assert said.startswith("Nothing to change"), said
        bar = page.locator(".prompt-bar").inner_text()
        assert "steps done" not in bar and "step done" not in bar, bar
        assert "is-done" not in (page.locator(".prompt-bar").get_attribute("class") or "")      # no green tick
    finally:
        page.context.close()
