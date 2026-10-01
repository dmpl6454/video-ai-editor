"""Editor Brain surfaces (EB1-F) in a real browser, Chromium AND WebKit.

Behind `brain.enabled` the preview card gains a Plan / Changes tablist (APG
tabs, the card's existing focus rules) and the right panel a Versions strip
above History:

  * the Plan tab (first for a brain run) lists the decisions by kind with
    their reasons, the hook quote with a seek button on the ruler's SMPTE,
    and "Not done this time"; the Changes tab is the diff with a why per
    line; Apply commits the preview's fingerprint as ONE prompt op;
  * the tabs keep the card's focus rules: Apply takes focus when the card
    appears, arrows move between tabs, a typed character goes to the prompt,
    Escape is Change;
  * Restore on the Versions strip is one `restore_version` op in History;
  * with the flag off there is no tablist, no strip and no versions route.

Harness (the test_prompt_preview_ui.py pattern): VAE_A11Y_BASE_URL = a Vite
dev server proxying /api to a backend started with the lane's WORKDIR, which
VAE_A11Y_WORKDIR names — the suite seeds the pending record's `brain`
payload and a `versions.json` there because lane E's `edit` card and lane
C's versions helper land in parallel with this lane. Without those two
variables the suite starts its own backend over frontend/dist (skipped when
dist is not built). Screenshots go to VAE_PREVIEW_SHOTS.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import brain_card_fixtures as BF  # noqa: E402
from test_frontend_a11y import DIST, ROOT, _free_port  # noqa: E402
from test_speed_ui_e2e import _client, _edl, _open, engine, pw  # noqa: E402,F401

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
SHOTS = Path(os.environ.get("VAE_PREVIEW_SHOTS", "/tmp"))
CARD = "[data-testid=prompt-preview]"
STRIP = "[data-testid=versions-strip]"


@pytest.fixture(scope="module", autouse=True)
def _env():
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("VAI_PROMPT_CONFIRM", raising=False)
        mp.delenv("VAI_BRAIN_ENABLED", raising=False)
        yield


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    """(base_url, workdir): an external harness, or a backend of our own."""
    url, wd = os.environ.get("VAE_A11Y_BASE_URL"), os.environ.get("VAE_A11Y_WORKDIR")
    if url and wd:
        yield url.rstrip("/"), Path(wd)
        return
    if not (DIST / "index.html").exists():
        pytest.skip("frontend/dist is not built and no VAE_A11Y_BASE_URL/VAE_A11Y_WORKDIR harness is set")
    tmp = tmp_path_factory.mktemp("brain_ui")
    home, workdir = tmp / "home", tmp / "wd"
    home.mkdir()
    port = _free_port()
    env = {**os.environ, "HOME": str(home), "WORKDIR": str(workdir), "ANTHROPIC_API_KEY": "",
           "HUGGINGFACE_TOKEN": "", "HF_HUB_OFFLINE": "1", "PYTHONPATH": str(ROOT / "src"),
           "VAI_KEYCHAIN_SERVICE": "vai-brain-ui-test-scratch", "VAI_LOG_DIR": str(tmp / "logs")}
    env.pop("VAI_PROMPT_CONFIRM", None)
    env.pop("VAI_BRAIN_ENABLED", None)
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "video_ai_editor.main:app", "--host", "127.0.0.1",
                             "--port", str(port)], cwd=str(ROOT), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import httpx
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/api/health", timeout=1).status_code == 200:
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
        yield base, workdir
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def clip(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("brain_media") / "talk.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:d=8:r=30",
                    "-f", "lavfi", "-i", "sine=frequency=330:duration=8", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(p)], check=True, capture_output=True)
    return p


def _project(base_url, clip: Path, name: str) -> str:
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
        assert c.post(f"/api/sessions/{sid}/dispatch", json={"tool": "split_at", "args": {
            "track": "v1", "time": 4.0}}).status_code == 200
    return sid


def _settings(base_url, *, confirm: bool = True, brain: bool) -> None:
    with _client(base_url) as c:
        r = c.put("/api/settings/prompt", json={"confirm_before_apply": confirm})
        assert r.status_code == 200 and r.json()["source"] != "env", r.text
        r = c.put("/api/settings/brain", json={"enabled": brain})
        assert r.status_code == 200 and r.json()["enabled"] is brain and r.json()["source"] != "env", r.text


def _ops(base_url, sid) -> list[dict]:
    with _client(base_url) as c:
        return c.get(f"/api/sessions/{sid}").json()["ops"]


def _tools(base_url, sid) -> list[str]:
    return [o.get("tool") for o in _ops(base_url, sid)]


def _seed_brain_card(base_url, workdir: Path, sid: str, *, big: bool = False) -> dict:
    """A REAL pending preview (a real dry run of 'mute the first clip'), then
    the `brain` payload lane E's card would have carried, seeded onto it —
    the card, the tabs, Apply and its fingerprint check are all real."""
    with _client(base_url) as c:
        body = c.post(f"/api/sessions/{sid}/prompt", json={"message": "mute the first clip"}).text
        assert '"preview"' in body, body[:400]
        deadline = time.time() + 20
        while not (c.get(f"/api/sessions/{sid}/prompt/pending").json().get("pending") or {}).get("preview"):
            assert time.time() < deadline, "no pending preview"
            time.sleep(0.2)
    path = workdir / sid / "prompt_pending.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    lines = list(record["preview"]["lines"]) + list(record["preview"].get("hidden") or [])
    src = next(cl["src"] for t in _edl(base_url, sid)["tracks"] if t["id"] == "v1" for cl in t["clips"])
    whys = ["filler “um” at 00:00:05:00"] + [None] * (len(lines) - 1)
    record["preview"]["brain"] = (BF.big_seed(src) if big else BF.card_seed(src, whys=whys))
    path.write_text(json.dumps(record, indent=1), encoding="utf-8")
    return record


def _seed_version(base_url, workdir: Path, sid: str, label: str) -> dict:
    """A "V1 <label>" row on the live tree, in C's `brain/versions.json`
    (brain/versions.py: `{"versions": [{id, label, op_seq, edl_hash,
    decisions_id, kind, created, pinned}]}`, the snapshot being
    `{op_seq + 1:05d}_{edl_hash}.json`), which the service writes after an
    applied brain run — lane E's `edit` card is not on this tree yet."""
    ops = _ops(base_url, sid)
    edl = _edl(base_url, sid)
    with _client(base_url) as c:
        h = c.get(f"/api/sessions/{sid}").json()["summary"]["edl_hash"]
    row = {"id": "v_1", "label": label, "op_seq": ops[-1]["seq"], "edl_hash": h, "decisions_id": BF.DID,
           "kind": "brain", "created": time.time(), "pinned": True, "undone": False}
    assert (workdir / sid / "snapshots" / f"{row['op_seq'] + 1:05d}_{h}.json").is_file(), "no snapshot for the live tree"
    path = workdir / sid / "brain" / "versions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"versions": [row]}), encoding="utf-8")
    return {"row": row, "edl": edl}


def _card_open_on_apply(page) -> None:
    page.locator(CARD).wait_for(timeout=20000)
    page.wait_for_function(
        "() => document.activeElement && document.activeElement.textContent.trim().startsWith('Apply')")


def _tab(page, name: str):
    return page.locator(CARD).get_by_role("tab", name=name)


def test_plan_tab_lists_reasons_and_apply_commits_fingerprint(engine, harness, clip):  # noqa: F811
    from video_ai_editor.agent.prompt import changes as C
    from video_ai_editor.edl.schema import EDL
    base_url, workdir = harness
    _settings(base_url, brain=True)
    sid = _project(base_url, clip, f"Brain plan {engine.engine_name}")
    record = _seed_brain_card(base_url, workdir, sid)
    ops0, edl0 = _tools(base_url, sid), _edl(base_url, sid)
    page = _open(engine, base_url, sid, 1440, 900)
    try:
        _card_open_on_apply(page)
        card = page.locator(CARD)
        tabs = card.get_by_role("tablist", name="Preview details")
        tabs.wait_for()
        assert _tab(page, "Plan").get_attribute("aria-selected") == "true"        # Plan first for a brain run
        assert _tab(page, "Changes").get_attribute("aria-selected") == "false"
        plan = card.get_by_role("tabpanel")
        text = plan.inner_text()
        for must in ("Talking head → Reel", "Ninety percent of first cuts are thrown away.", "Cuts",
                     "silence of 0.8 s at 00:00:03:00", "filler “um” at 00:00:05:00",
                     "false start “so the—” before “so the cut” at 00:00:09:00", "Pauses kept",
                     "after an emotional line", "Captions", "Music", "Not done this time",
                     "per-word caption highlight — next wave", "moments by Apple Intelligence"):
            assert must in text, (must, text)
        assert "Nothing has changed yet." in card.inner_text()
        # the punch-in the resolver dropped is said, never listed as done
        assert "1 not applied" in text
        card.screenshot(path=str(SHOTS / f"brain-plan-tab-{engine.engine_name}.png"))
        # the hook's seek button moves the playhead to the ruler time it names
        seek = plan.get_by_role("button", name=re.compile(r"^Seek to 00:00:02:00"))
        seek.click()
        page.wait_for_function("() => document.querySelector('[aria-label=\"Playhead timecode\"]').value"
                               " === '00:00:02:00'")
        assert _edl(base_url, sid) == edl0 and _tools(base_url, sid) == ops0     # nothing committed
        # the Changes tab is the diff, each line with its why
        _tab(page, "Changes").click()
        page.wait_for_function("() => document.querySelector('.prompt-preview-list')")
        lines = card.locator(".prompt-preview-list li").all_inner_texts()
        assert lines and lines[0].startswith("Clip 1 ") and "': muted" in lines[0], lines
        assert card.locator(".prompt-preview-list li .prompt-preview-why").first.inner_text().strip() \
            == "— filler “um” at 00:00:05:00"
        card.screenshot(path=str(SHOTS / f"brain-changes-tab-{engine.engine_name}.png"))
        # Apply: one prompt op, the committed tree IS the previewed one
        apply = card.get_by_role("button", name=re.compile(r"^Apply"))
        apply.focus()
        page.keyboard.press("Enter")
        card.wait_for(state="detached", timeout=20000)
        deadline = time.time() + 30
        while _tools(base_url, sid) == ops0:
            assert time.time() < deadline, "Apply never committed"
            time.sleep(0.3)
        assert _tools(base_url, sid) == ops0 + ["prompt"]
        after = _edl(base_url, sid)
        assert C.canonical(EDL.model_validate(after), EDL.model_validate(edl0)) == record["preview"]["fingerprint"]
        page.wait_for_function("() => document.querySelector('.prompt-bar .prompt-sr-only').textContent"
                               ".startsWith('Applied')", timeout=30000)
    finally:
        page.context.close()


def test_tabs_keep_focus_rules(engine, harness, clip):  # noqa: F811
    base_url, workdir = harness
    _settings(base_url, brain=True)
    sid = _project(base_url, clip, f"Brain focus {engine.engine_name}")
    _seed_brain_card(base_url, workdir, sid)
    ops0, edl0 = _tools(base_url, sid), _edl(base_url, sid)
    page = _open(engine, base_url, sid, 1440, 900)
    try:
        _card_open_on_apply(page)                       # Apply, not a tab, takes focus
        card = page.locator(CARD)
        _tab(page, "Changes").click()
        page.wait_for_function("() => document.activeElement.getAttribute('role') === 'tab'")
        assert _tab(page, "Changes").get_attribute("aria-selected") == "true"
        assert _tab(page, "Plan").get_attribute("tabindex") == "-1"          # roving tabindex
        page.keyboard.press("ArrowLeft")
        page.wait_for_function("() => document.activeElement.textContent.trim() === 'Plan'")
        assert _tab(page, "Plan").get_attribute("aria-selected") == "true"    # arrows select
        page.keyboard.press("End")
        page.wait_for_function("() => document.activeElement.textContent.trim() === 'Changes'")
        page.keyboard.press("Home")
        page.wait_for_function("() => document.activeElement.textContent.trim() === 'Plan'")
        # a printable key on a tab goes to the PROMPT — never applies
        page.keyboard.type("no")
        page.wait_for_function("() => document.activeElement.classList.contains('prompt-input')")
        assert page.locator("textarea.prompt-input").input_value() == "no"
        time.sleep(1.0)
        assert _edl(base_url, sid) == edl0 and _tools(base_url, sid) == ops0
        assert card.count() == 1
        # Escape from a tab is Change: card gone, focus in the prompt, nothing committed
        _tab(page, "Plan").click()
        page.wait_for_function("() => document.activeElement.getAttribute('role') === 'tab'")
        page.keyboard.press("Escape")
        card.wait_for(state="detached")
        page.wait_for_function("() => document.activeElement.classList.contains('prompt-input')")
        assert _edl(base_url, sid) == edl0 and _tools(base_url, sid) == ops0
    finally:
        page.context.close()


def test_versions_strip_restore_is_an_op(engine, harness, clip):  # noqa: F811
    base_url, workdir = harness
    _settings(base_url, brain=True)
    sid = _project(base_url, clip, f"Brain versions {engine.engine_name}")
    seeded = _seed_version(base_url, workdir, sid, "V1 Reel")
    with _client(base_url) as c:
        assert c.post(f"/api/sessions/{sid}/dispatch", json={"tool": "add_text", "args": {
            "text": "later", "start": 0, "end": 1}}).status_code == 200
    ops0 = _tools(base_url, sid)
    page = _open(engine, base_url, sid, 1440, 900)
    try:
        strip = page.locator(STRIP)
        strip.wait_for(timeout=20000)
        assert strip.get_attribute("role") == "radiogroup"
        radio = strip.get_by_role("radio", name="V1 Reel")
        assert radio.get_attribute("aria-checked") == "false"          # the live tree moved on
        # above History
        top_strip = strip.bounding_box()["y"]
        top_hist = page.locator(".ops-log .section-label").bounding_box()["y"]
        assert top_strip < top_hist, (top_strip, top_hist)
        strip.screenshot(path=str(SHOTS / f"brain-versions-strip-{engine.engine_name}.png"))
        restore = strip.get_by_role("button", name="Restore V1 Reel")
        restore.focus()
        page.keyboard.press("Enter")
        deadline = time.time() + 20
        while _tools(base_url, sid) == ops0:
            assert time.time() < deadline, "Restore never committed"
            time.sleep(0.2)
        assert _tools(base_url, sid) == ops0 + ["restore_version"]
        assert _edl(base_url, sid) == seeded["edl"]
        page.wait_for_function("() => [...document.querySelectorAll('.ops-log .op')]"
                               ".some((el) => el.textContent.includes('Restore'))", timeout=10000)
        page.wait_for_function("() => document.querySelector('[data-testid=versions-strip] [role=radio]')"
                               ".getAttribute('aria-checked') === 'true'", timeout=10000)
    finally:
        page.context.close()


def test_hidden_when_flag_off(engine, harness, clip):  # noqa: F811
    base_url, workdir = harness
    _settings(base_url, brain=False)
    sid = _project(base_url, clip, f"Brain off {engine.engine_name}")
    _seed_version(base_url, workdir, sid, "V1 Reel")
    _seed_brain_card(base_url, workdir, sid)
    page = _open(engine, base_url, sid, 1440, 900)
    try:
        _card_open_on_apply(page)
        card = page.locator(CARD)
        assert card.get_by_role("tab").count() == 0 and card.get_by_role("tablist").count() == 0
        assert "brain" not in (page.evaluate("() => document.querySelector('[data-testid=prompt-preview]').outerHTML"))
        assert card.locator(".prompt-preview-why").count() == 0
        assert card.get_attribute("class") == "prompt-preview"          # not even the brain card's layout class
        assert card.locator(".prompt-preview-panel").count() == 0 and page.locator("[data-testid=analysis-progress]").count() == 0
        assert page.locator(STRIP).count() == 0
        with _client(base_url) as c:
            r = c.get(f"/api/sessions/{sid}/brain/versions")
            assert r.status_code == 404 and r.json()["error"]["code"] == "brain_disabled"
        card.screenshot(path=str(SHOTS / f"brain-flag-off-card-{engine.engine_name}.png"))
        page.keyboard.press("Escape")
        card.wait_for(state="detached")
    finally:
        page.context.close()
        _settings(base_url, brain=True)


SIZES = [(900, 640), (1280, 800), (1440, 900), (1920, 1080)]


def _hit(page, name: str) -> tuple[str | None, dict]:
    """What `document.elementFromPoint` finds at the centre of the card's
    `name` button: the button's own text, or the tag that covers it."""
    btn = page.locator(CARD).get_by_role("button", name=re.compile(rf"^{name}"))
    box = btn.bounding_box()
    assert box is not None, name
    got = page.evaluate("""([x, y]) => { const e = document.elementFromPoint(x, y);
        const b = e && e.closest('button'); return b ? b.textContent.trim() : (e ? e.tagName + '.' + e.className : null) }""",
                        [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2])
    return got, box


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
@pytest.mark.parametrize("tab", ["Plan", "Changes"])
def test_apply_and_change_are_hittable_at_every_size(engine, harness, clip, size, tab):  # noqa: F811
    """UX-02: the card's body scrolls, its action row stays pinned and
    hittable by MOUSE — element-at-point and a real click — at 900x640 up."""
    base_url, workdir = harness
    _settings(base_url, brain=True)
    w, h = size
    sid = _project(base_url, clip, f"Brain hit {engine.engine_name} {w}")
    _seed_brain_card(base_url, workdir, sid, big=True)
    ops0 = _tools(base_url, sid)
    page = _open(engine, base_url, sid, w, h)
    try:
        _card_open_on_apply(page)
        if tab == "Changes":
            _tab(page, "Changes").click()
        bar = page.locator(".prompt-bar").bounding_box()
        for name in ("Apply", "Change"):
            got, box = _hit(page, name)
            assert got is not None and got.startswith(name), (name, got, box, bar)
            assert box["y"] + box["height"] <= bar["y"] + bar["height"] + 1, (name, box, bar)
        page.screenshot(path=str(SHOTS / f"brain-hit-{tab.lower()}-{w}x{h}-{engine.engine_name}.png"))
        page.locator(CARD).get_by_role("button", name=re.compile(r"^Apply")).click(timeout=5000)
        deadline = time.time() + 30
        while _tools(base_url, sid) == ops0:
            assert time.time() < deadline, "Apply (a mouse click) never committed"
            time.sleep(0.3)
        assert _tools(base_url, sid) == ops0 + ["prompt"]
    finally:
        page.context.close()


def test_restore_by_keyboard_keeps_focus_and_says_so(engine, harness, clip):  # noqa: F811
    """UX-08: after Enter on Restore focus was lost to <body> (Chromium) and
    nothing was announced anywhere: it now lands on the restored version's
    radio and the polite live region says "Restored V1 Reel"."""
    base_url, workdir = harness
    _settings(base_url, brain=True)
    sid = _project(base_url, clip, f"Brain restore focus {engine.engine_name}")
    _seed_version(base_url, workdir, sid, "V1 Reel")
    with _client(base_url) as c:
        assert c.post(f"/api/sessions/{sid}/dispatch", json={"tool": "add_text", "args": {
            "text": "later", "start": 0, "end": 1}}).status_code == 200
    ops0 = _tools(base_url, sid)
    page = _open(engine, base_url, sid, 1440, 900)
    try:
        strip = page.locator(STRIP)
        strip.wait_for(timeout=20000)
        strip.get_by_role("button", name="Restore V1 Reel").focus()
        page.keyboard.press("Enter")
        deadline = time.time() + 20
        while _tools(base_url, sid) == ops0:
            assert time.time() < deadline, "Restore never committed"
            time.sleep(0.2)
        page.wait_for_function("""() => { const a = document.activeElement;
            return !!a && a.getAttribute('role') === 'radio' && a.textContent.includes('V1 Reel')
              && a.getAttribute('aria-checked') === 'true' }""", timeout=10000)
        page.wait_for_function("""() => [...document.querySelectorAll('[data-announcer]')]
            .some((el) => el.textContent === 'Restored V1 Reel')""", timeout=10000)
        page.screenshot(path=str(SHOTS / f"brain-restore-focus-{engine.engine_name}.png"))
    finally:
        page.context.close()


def test_changes_tabpanel_keeps_its_list(engine, harness, clip):  # noqa: F811
    """UX-08: the Changes tabpanel was a <ul role=tabpanel>, so its items were
    orphan listitems in the accessibility tree; the panel is a div now and the
    list inside it is a list. The tablist keeps its APG keys."""
    base_url, workdir = harness
    _settings(base_url, brain=True)
    sid = _project(base_url, clip, f"Brain panel {engine.engine_name}")
    _seed_brain_card(base_url, workdir, sid)
    page = _open(engine, base_url, sid, 1440, 900)
    try:
        _card_open_on_apply(page)
        _tab(page, "Changes").click()
        panel = page.locator(CARD).get_by_role("tabpanel")
        assert panel.evaluate("(el) => el.tagName") == "DIV"
        assert panel.locator("ul li").count() >= 1
        snap = panel.aria_snapshot()
        assert '- list "Changes it would make"' in snap and "- listitem" in snap, snap
        assert panel.get_attribute("aria-labelledby") == _tab(page, "Changes").get_attribute("id")
        # the panel of the other tab is still exactly one tabpanel
        _tab(page, "Plan").click()
        assert page.locator(CARD).get_by_role("tabpanel").count() == 1
    finally:
        page.context.close()


# ---------------------------------------------------------------------------
# UX-07 (UI half): the read of the footage shows real progress and can be cancelled
# ---------------------------------------------------------------------------

_STREAM_STUB = """() => {
  const real = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : input.url;
    if (/\\/prompt\\/answer$/.test(url) && ((init && init.method) || 'GET') === 'POST') {
      const enc = new TextEncoder();
      const stream = new ReadableStream({ start(c) {
        window.__push = (o) => c.enqueue(enc.encode('data: ' + JSON.stringify(o) + '\\n\\n'));
        window.__end = () => { try { c.close(); } catch (e) {} };
      } });
      return Promise.resolve(new Response(stream, { status: 200, headers: { 'content-type': 'text/event-stream' } }));
    }
    return real(input, init);
  };
}"""

STRAY_CARD = {"type": "clarify", "token": "tok_stray", "plan_id": "p_stray", "expires_in_s": 600,
              "questions": [{"key": "apply_preview", "question": "Apply these changes?", "kind": "confirm",
                             "required": True}],
              "preview": {"summary": "Edit: 1 change", "lines": ["Removed 1 stretch"], "more": 0, "total": 1,
                          "hidden": [], "note": None, "nothing_changed": "Nothing has changed yet."}}


def _to_the_gate(page, prompt: str = "make a 45-second reel"):
    page.locator("textarea.prompt-input").fill(prompt)
    page.keyboard.press("Enter")
    gate = page.get_by_role("button", name=re.compile(r"^Read the footage first"))
    gate.wait_for(timeout=30000)
    return gate


@pytest.mark.parametrize("with_job", [True, False], ids=["job-id-on-the-frame", "bare-frame"])
def test_the_read_shows_progress_and_cancel_stops_it(engine, harness, clip, with_job):  # noqa: F811
    """UX-07: 'planning · a moment estimated' for the whole read, and a Cancel
    that did nothing (the card popped up after it). The bar shows the layer,
    the fraction and the time left from the `analysis` frames — whichever
    optional fields the backend sends — Cancel names the job (when the frame
    does) and asks the run to stop, and a card that still arrives is dropped."""
    base_url, workdir = harness
    _settings(base_url, brain=True)
    sid = _project(base_url, clip, f"Brain read {engine.engine_name}")
    ops0 = _tools(base_url, sid)
    page = _open(engine, base_url, sid, 1440, 900)
    seen: list[str] = []
    page.on("request", lambda r: seen.append(f"{r.method} {r.url.split('/api', 1)[-1]}"))
    try:
        gate = _to_the_gate(page)
        page.evaluate(_STREAM_STUB)
        gate.click()
        page.wait_for_function("() => document.querySelector('.prompt-bar').classList.contains('is-busy')")
        job = {"job_id": "job_test1"} if with_job else {}
        page.evaluate("(f) => window.__push(f)", {"type": "analysis", "layer": "speech", "pct": 20.0, "eta_s": 30.0, **job})
        bar = page.locator("[data-testid=analysis-progress]")
        bar.wait_for(timeout=5000)
        assert bar.get_attribute("role") == "progressbar" and bar.get_attribute("aria-valuenow") == "20"
        assert bar.inner_text().strip() == "Reading the footage — speech · 20% · about 30 s left"
        assert float(page.evaluate("() => getComputedStyle(document.querySelector('.prompt-bar'))"
                                   ".getPropertyValue('--prompt-progress')")) == pytest.approx(0.2)
        # a later backend names every layer: each with its fraction
        page.evaluate("(f) => window.__push(f)", {"type": "analysis", "layer": "semantic", "pct": 55.0, "eta_s": 12.0,
                                                  "layers": [{"name": "speech", "fraction": 1.0},
                                                             {"name": "semantic", "fraction": 0.4}, "scenes"], **job})
        page.wait_for_function("() => document.querySelector('[data-testid=analysis-progress]')"
                               ".textContent.includes('meaning')", timeout=5000)
        text = bar.inner_text()
        assert text.startswith("Reading the footage — meaning · 55% · about 12 s left"), text
        assert "speech ✓" in text and "meaning 40%" in text and "scenes" in text, text
        page.screenshot(path=str(SHOTS / f"brain-read-progress-{engine.engine_name}.png"))
        # Esc → "Cancel the run?" → Cancel run
        page.keyboard.press("Escape")
        page.get_by_role("alertdialog", name="Cancel the run?").get_by_role("button", name="Cancel run").click()
        deadline = time.time() + 5
        while not any(s.startswith("POST /sessions/") and s.endswith("/prompt/cancel") for s in seen):
            assert time.time() < deadline, seen
            time.sleep(0.1)
        assert any(s == "POST /jobs/job_test1/cancel" for s in seen) is with_job, seen
        # the server ignored it and its plan reached a card anyway: nothing pops up
        page.evaluate("(f) => { window.__push(f); window.__end(); }", STRAY_CARD)
        page.wait_for_function("() => [...document.querySelectorAll('.prompt-sr-only')]"
                               ".some((el) => el.textContent === 'Cancelled — nothing was changed.')", timeout=5000)
        time.sleep(0.6)
        assert page.locator(CARD).count() == 0
        assert page.locator(".prompt-bar.is-busy, .prompt-bar.is-done").count() == 0
        assert page.locator("[data-testid=analysis-progress]").count() == 0
        page.screenshot(path=str(SHOTS / f"brain-read-cancelled-{engine.engine_name}.png"))
        assert _tools(base_url, sid) == ops0
    finally:
        page.context.close()


def test_plan_tab_reads_like_an_editors_plan(engine, harness, clip):  # noqa: F811
    """UX-05 (presentation): the length the run leaves, a seek button for EVERY
    decision that has a source range (not only the hook), no graph key, id or
    .normalized name anywhere, and "Not done this time" in one place."""
    base_url, workdir = harness
    _settings(base_url, brain=True)
    sid = _project(base_url, clip, f"Brain plan words {engine.engine_name}")
    _seed_brain_card(base_url, workdir, sid)
    edl0, ops0 = _edl(base_url, sid), _tools(base_url, sid)
    page = _open(engine, base_url, sid, 1440, 900)
    try:
        _card_open_on_apply(page)
        card = page.locator(CARD)
        plan = card.get_by_role("tabpanel")
        text = plan.inner_text()
        assert "Talking head → Reel, 44.7 s:" in text, text
        assert not re.search(r"src_[0-9a-f]{6,}|\b[kdcw]_[0-9a-f]{4,}\b|\.normalized", card.inner_text()), card.inner_text()
        assert "dialogue from the footage (th_16x9.mp4) on lane a1" in text, text
        assert card.inner_text().count("Not done this time") == 1
        # a seek button per decision that has a source range: the three cuts, the kept pause, the punch-in, the hook
        seeks = plan.get_by_role("button", name=re.compile(r"^Seek to "))
        names = [seeks.nth(i).evaluate("(el) => el.getAttribute('aria-label') || el.textContent.trim()")
                 for i in range(seeks.count())]
        assert len(names) == 6, names
        for tc, what in (("00:00:03:00", "silence of 0.8 s"), ("00:00:05:00", "filler “um”"), ("00:00:09:00", "false start"),
                         ("00:00:07:00", "kept 0.6 s of the pause"), ("00:00:02:06", "punch in on the hook")):
            assert any(n.startswith(f"Seek to {tc}: ") and what in n for n in names), (tc, names)
        assert any(n.startswith("Seek to 00:00:02:00") for n in names), names            # the hook's own
        plan.get_by_role("button", name=re.compile(r"^Seek to 00:00:05:00: filler")).click()
        page.wait_for_function("() => document.querySelector('[aria-label=\"Playhead timecode\"]').value === '00:00:05:00'")
        card.screenshot(path=str(SHOTS / f"brain-plan-words-{engine.engine_name}.png"))
        assert _edl(base_url, sid) == edl0 and _tools(base_url, sid) == ops0
    finally:
        page.context.close()


def test_the_run_log_calls_an_advisory_audit_noted_not_failed(engine, harness, clip):  # noqa: F811
    """Closer N-17: after a run whose advisory check did not hold, Details read '… advisory · the edit was kept — failed'.
    The row says it was kept and NOTED; 'failed' stays for a check that really failed."""
    base_url, _wd = harness
    _settings(base_url, confirm=False, brain=True)
    sid = _project(base_url, clip, f"Brain advisory {engine.engine_name}")
    page = _open(engine, base_url, sid, 1440, 900)
    checks = [{"check": "no_cut_mid_word", "human": "no cut lands inside a word", "pass": True, "measured": 0, "expected": 0,
               "headline": True, "blocking": True},
              {"check": "audit_ok", "human": "the aesthetic audit passes", "pass": False, "measured": {"score": 75, "hook_score": 2},
               "expected": {"hook_score": 3}, "headline": False, "blocking": False},
              {"check": "captions_cover", "human": "captions cover the speech", "pass": False, "measured": 0.5, "expected": 0.9,
               "headline": True, "blocking": True}]
    frames = [
        {"type": "brain", "status": "answered", "brain": "recipes", "label": "Recipes"},
        {"type": "step", "index": 0, "total": 1, "tool": "add_caption_track", "status": "ok", "summary": "laid captions"},
        {"type": "verify", "plan_id": "p_adv", "checks": checks, "passed": 1, "total": 2, "rendered": False},
        {"type": "text_delta", "text": "Done with issues."}, {"type": "done"}]
    body = "".join(f"data: {json.dumps(f)}\n\n" for f in frames)
    page.route(f"**/api/sessions/{sid}/prompt", lambda route: route.fulfill(
        status=200, headers={"content-type": "text/event-stream"}, body=body)
        if route.request.method == "POST" else route.continue_())
    try:
        page.fill("textarea.prompt-input", "add captions")
        page.keyboard.press("Enter")
        details = page.get_by_role("button", name="Details")
        details.wait_for(timeout=20000)
        details.click()
        rows = page.locator(".prompt-check")
        rows.first.wait_for()
        audit = page.locator(".prompt-check", has_text="the aesthetic audit passes")
        real_miss = page.locator(".prompt-check", has_text="captions cover the speech")
        audit_text, miss_text = audit.inner_text(), real_miss.inner_text()
        assert "advisory" in audit_text.lower() and "noted" in audit.locator(".prompt-sr-only").text_content(), audit_text
        assert "failed" not in audit.locator(".prompt-sr-only").text_content()
        assert "failed" in real_miss.locator(".prompt-sr-only").text_content(), miss_text
        page.locator("section.prompt-log").screenshot(path=str(SHOTS / f"brain-advisory-noted-{engine.engine_name}.png"))
    finally:
        page.context.close()
