"""Speed curves and Freeze through the UI, in Chromium AND WebKit, proven by
the EXPORT (wave D, lane S2).

The Inspector's Speed section (Normal | Curve, CapCut's six presets, Custom
and the editable curve) and the timeline toolbar's Freeze button are driven
like a person drives them: a clip is selected by clicking it on the timeline
canvas, a preset by clicking its radio, a curve point is moved with the arrow
keys, the playhead is typed into the toolbar clock and Freeze is pressed.
The project is then exported through the Export dialog, and the DOWNLOADED
file is decoded frame by frame: each frame of the bar-coded source carries
its own index (`frame_map_golden_lib`), so the file must equal, frame for
frame, the program map (`render/frame_map.build_program_map`) of the EDL the
UI produced — not merely "the UI changed" — and the freeze must hold the
frame the export showed at the playhead before it was pressed.

Harness: `test_frontend_a11y`'s server fixture (VAE_A11Y_BASE_URL = a Vite
dev server proxying /api to a backend, else frontend/dist). Screenshots at
1280x800 and 1440x900 go to VAE_SPEED_SHOTS (default /tmp).
"""
from __future__ import annotations

import json
import math
import os
import shutil
import sys
import time
from fractions import Fraction
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import frame_map_golden_lib as G  # noqa: E402
from test_frontend_a11y import base_url  # noqa: E402,F401  (fixture)
from test_frontend_design_system import GLYPHS_JS, ICONS_JS  # noqa: E402

from video_ai_editor.edl import timebase as tb  # noqa: E402
from video_ai_editor.edl.schema import EDL  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
SHOTS = Path(os.environ.get("VAE_SPEED_SHOTS", "/tmp"))
SID_CODE = 3                     # the bar-code source id of the fixture
SPLITS = (2.0, 4.0, 6.0, 8.0, 10.0, 12.0)   # → seven 2 s clips (the last 3 s)
LABEL_W = 80                     # Timeline.tsx labelWidth
FIT_MARGIN = 24                  # lib/timelineZoom.fitZoom marginPx


try:
    from playwright.sync_api import expect
except ImportError:  # the fixture below skips
    expect = None


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


@pytest.fixture(scope="module")
def bars(tmp_path_factory) -> Path:
    return G.make_bar_source(tmp_path_factory.mktemp("bars") / "bars15.mp4",
                             G.SourceSpec(key="bars", sid=SID_CODE, rate=Fraction(30), seconds=15.0))


def _client(base_url):  # noqa: F811
    import httpx
    return httpx.Client(base_url=base_url, timeout=180)


def _edl(base_url, sid) -> dict:  # noqa: F811
    with _client(base_url) as c:
        return c.get(f"/api/sessions/{sid}/edl").json()


def _v1(edl: dict) -> list[dict]:
    return next(t for t in edl["tracks"] if t["id"] == "v1")["clips"]


def _project(base_url, bars: Path, name: str) -> str:  # noqa: F811
    """A session whose Main video holds seven clips of the bar-coded source."""
    with _client(base_url) as c:
        sid = c.post("/api/sessions", json={"name": name}).json()["id"]
        with bars.open("rb") as fh:
            r = c.post(f"/api/sessions/{sid}/upload", files={"file": (bars.name, fh, "video/mp4")},
                       data={"add_to_timeline": "true", "transcribe": "false"})
        assert r.status_code in (200, 202), r.text
        deadline = time.time() + 120
        while not _v1(c.get(f"/api/sessions/{sid}/edl").json()):
            assert time.time() < deadline, "the import never reached the timeline"
            time.sleep(0.3)
        for t in SPLITS:
            r = c.post(f"/api/sessions/{sid}/dispatch", json={"tool": "split_at", "args": {"time": t}})
            assert r.status_code == 200, r.text
    return sid


def _wait_edl(base_url, sid, pred, what: str, timeout=20.0) -> dict:  # noqa: F811
    deadline = time.time() + timeout
    while True:
        edl = _edl(base_url, sid)
        if pred(edl):
            return edl
        assert time.time() < deadline, f"timed out waiting for {what}"
        time.sleep(0.2)


def _open(browser, base_url, sid, width, height):  # noqa: F811
    ctx = browser.new_context(viewport={"width": width, "height": height}, accept_downloads=True)
    ctx.add_init_script("try { " + " ".join(
        f"localStorage.setItem({json.dumps(k)}, {json.dumps(v)});"
        for k, v in {"vai.sessionId": sid, "vai.rightTab": "inspect"}.items()) + " } catch (e) {}")
    page = ctx.new_page()
    page.goto(base_url + "/")
    page.locator(".timeline-canvas-wrap canvas").first.wait_for()
    page.wait_for_timeout(1200)
    return page


class TimelineGeometry:
    """Where a clip is drawn after "Zoom to fit": x(t) = labelWidth + t·zoom
    with zoom = (laneW − margin) / duration (lib/timelineZoom.fitZoom), and
    the Main video row found once by clicking down the canvas."""

    def __init__(self, page):
        self.page = page
        self.row_dy: float | None = None
        self.last_point: tuple[float, float] = (0.0, 0.0)

    def select(self, base_url, sid, clip_id: str) -> None:  # noqa: F811
        page = self.page
        page.get_by_role("button", name="Zoom to fit").click()
        page.wait_for_timeout(250)
        edl = _edl(base_url, sid)
        clip = next(c for c in _v1(edl) if c["id"] == clip_id)
        box = page.locator(".timeline-canvas-wrap canvas").first.bounding_box()
        zoom = max(40.0, box["width"] - LABEL_W - FIT_MARGIN) / edl["duration"]
        dur = clip["freeze"] if clip.get("freeze") else _footprint(clip)
        x = box["x"] + LABEL_W + (clip["start"] + dur / 2) * zoom
        # Below the ruler (a ruler click parks the playhead under the
        # pointer, and a click within 5 px of the playhead grabs it — so each
        # try also steps sideways, inside the clip).
        rows = [self.row_dy] if self.row_dy is not None else range(30, int(box["height"]) - 4, 5)
        for i, dy in enumerate(rows):
            page.mouse.click(x + ((i % 3) - 1) * 9, box["y"] + dy)
            page.wait_for_timeout(150)
            got = page.locator(".props[data-clip-id]")
            if got.count() and got.get_attribute("data-clip-id") == clip_id:
                self.row_dy = dy
                self.last_point = (x, box["y"] + dy)          # the clip's middle
                return
        page.screenshot(path=str(SHOTS / "speed_select_failed.png"))
        raise AssertionError(f"could not select {clip_id} at x={x:.0f} (canvas {box})")


def _footprint(clip: dict) -> float:
    sp = clip.get("speed")
    s = clip["out"] - clip["in"]
    if isinstance(sp, dict):
        pts = sp["curve"]
        mean = sum((x1 - x0) * (r0 + r1) / 2 for (x0, r0), (x1, r1) in zip(pts, pts[1:]))
        return s / mean
    return s / (sp if isinstance(sp, (int, float)) and sp > 0 else 1.0)


def _timecode(t: float, fps: int = 30) -> str:
    f = tb.frame_of(t, fps)
    return f"{f // (3600 * fps):02d}:{f // (60 * fps) % 60:02d}:{f // fps % 60:02d}:{f % fps:02d}"


def _expected(edl_json: dict) -> dict:
    edl = EDL.model_validate(edl_json)
    src = _v1(edl_json)[0]["src"]
    return G.expected_frames(edl, {src: G.probe_source(Path(src))}, {src: SID_CODE})


def test_presets_custom_and_freeze_through_the_ui_export_as_the_program_map(
        engine, base_url, bars, tmp_path):  # noqa: F811
    name = engine.engine_name
    sid = _project(base_url, bars, f"speed ui {name}")
    with _client(base_url) as c:
        catalog = c.get("/api/speed/presets").json()
    menu = [p for p in catalog["presets"] if p["menu"]]
    assert [p["label"] for p in menu] == ["Montage", "Hero", "Bullet", "Jump Cut", "Flash In", "Flash Out"]
    clips = [c["id"] for c in _v1(_edl(base_url, sid))]
    assert len(clips) == 7

    page = _open(engine, base_url, sid, 1280, 800)
    geo = TimelineGeometry(page)
    props = page.locator(".props")

    # 1. Every CapCut preset, each on its own clip, through its radio.
    for cid, preset in zip(clips, menu):
        geo.select(base_url, sid, cid)
        page.get_by_role("radio", name="Curve", exact=True).click()
        props.get_by_role("radiogroup", name="Speed curve").wait_for()
        props.get_by_role("radio", name=preset["label"], exact=True).click()
        _wait_edl(base_url, sid, lambda e, cid=cid, p=preset: (next(c for c in _v1(e) if c["id"] == cid).get("speed") or {})
                  .get("name") == p["id"], f"{preset['label']} on {cid}")
        # The radio shows it (once the EDL is back), and the graph draws the
        # preset's points.
        expect(props.get_by_role("radio", name=preset["label"], exact=True)).to_have_attribute("aria-checked", "true")
        expect(page.locator(".speed-point")).to_have_count(len(preset["points"]))
        if preset["id"] == "hero":
            page.screenshot(path=str(SHOTS / f"speed_{name}_hero_1280x800.png"))

    # Every preset's clip now fills its curve's footprint, not the 2 s of
    # source it holds (the timeline rippled to it).
    placed = _v1(_edl(base_url, sid))
    for cid, preset in zip(clips, menu):
        c = next(x for x in placed if x["id"] == cid)
        mean = sum((x1 - x0) * (r0 + r1) / 2 for (x0, r0), (x1, r1) in zip(preset["points"], preset["points"][1:]))
        assert _footprint(c) == pytest.approx(2.0 / mean, abs=1e-6), preset["id"]
        assert not math.isclose(mean, 1.0, abs_tol=0.02), preset["id"]

    # 2. Custom on the last clip, edited from the keyboard: point 3 (50 %)
    #    three steps up the log axis: 1 → 1.12 → 1.26 → 1.41.
    last = clips[6]
    geo.select(base_url, sid, last)
    page.get_by_role("radio", name="Curve", exact=True).click()
    props.get_by_role("radio", name="Custom", exact=True).click()
    _wait_edl(base_url, sid, lambda e: (next(c for c in _v1(e) if c["id"] == last).get("speed") or {})
              .get("name") == "custom", "a custom curve")
    point = page.get_by_role("button", name="Speed point 3 of 5, 50% through the clip, 1×", exact=True)
    expect(point).to_be_visible()
    point.focus()
    for _ in range(3):
        page.keyboard.press("ArrowUp")
    e = _wait_edl(base_url, sid, lambda e: next(c for c in _v1(e) if c["id"] == last)["speed"]["curve"][2] == [0.5, 1.41],
                  "the keyboard edit")
    assert next(c for c in _v1(e) if c["id"] == last)["speed"]["curve"] == [
        [0.0, 1.0], [0.25, 1.0], [0.5, 1.41], [0.75, 1.0], [1.0, 1.0]]
    # The focused point kept focus and announces its new value.
    expect(page.locator(":focus")).to_have_attribute("aria-label", "Speed point 3 of 5, 50% through the clip, 1.41×")

    # The Speed section keeps the app's rules with the editor open: every
    # control icon a 16 px lucide glyph (the preset shapes are masks, not
    # svgs), no glyph stand-ins, every button named, motion only when allowed.
    assert page.evaluate(ICONS_JS) == []
    assert page.evaluate(GLYPHS_JS) == []
    assert page.evaluate("""() => [...document.querySelectorAll('.props .speed-section button')]
        .filter(b => !(b.getAttribute('aria-label') || b.textContent.trim())).length""") == 0
    motion = "() => getComputedStyle(document.querySelector('.speed-preset')).transitionDuration"
    page.emulate_media(reduced_motion="no-preference")
    assert float(page.evaluate(motion).split(",")[0].rstrip("s")) >= 0.05
    page.emulate_media(reduced_motion="reduce")      # styles.css QA-127: ≤ 0.01 ms
    assert float(page.evaluate(motion).split(",")[0].rstrip("s")) <= 0.001

    # 3a. Freeze from the clip's context menu, inside the Bullet clip.
    pre = _edl(base_url, sid)
    bullet = next(c for c in _v1(pre) if (c.get("speed") or {}).get("name") == "bullet")
    t2 = tb.quantize(bullet["start"] + 0.4, 30)
    k2 = tb.frame_of(t2, 30)
    exp_pre = _expected(pre)
    geo.select(base_url, sid, bullet["id"])
    clock = page.get_by_role("textbox", name="Playhead timecode")
    clock.click()
    clock.fill(_timecode(t2))
    clock.press("Enter")
    page.wait_for_timeout(300)
    x, y = geo.last_point
    page.mouse.click(x + 8, y, button="right")         # on the clip, clear of the playhead line
    menu = page.get_by_role("menu", name="Clip actions")
    expect(menu).to_be_visible()
    menu.get_by_role("menuitem", name="Freeze frame").click()
    mid = _wait_edl(base_url, sid, lambda e: any(c.get("freeze") for c in _v1(e)), "the context-menu freeze")
    n_hold = tb.frame_of(catalog["freeze_default"], 30)
    exp_mid = _expected(mid)
    assert exp_mid["top"][k2:k2 + n_hold] == [exp_pre["top"][k2]] * n_hold
    assert exp_mid["top"][:k2] == exp_pre["top"][:k2]

    # 3b. Freeze inside the Hero clip: type the playhead, press the toolbar's
    #    Freeze. The frame the export shows there BEFORE the edit is noted.
    before = _edl(base_url, sid)
    hero = next(c for c in _v1(before) if (c.get("speed") or {}).get("name") == "hero")
    t = tb.quantize(hero["start"] + 0.5, 30)
    k = tb.frame_of(t, 30)
    exp_before = _expected(before)
    # CapCut's rule: a selected clip must be the one under the playhead.
    geo.select(base_url, sid, hero["id"])
    clock = page.get_by_role("textbox", name="Playhead timecode")
    clock.click()
    clock.fill(_timecode(t))
    clock.press("Enter")
    page.wait_for_timeout(300)
    freeze = page.get_by_role("button", name="Freeze frame", exact=True)
    expect(freeze).to_be_enabled()
    freeze.click()
    after = _wait_edl(base_url, sid, lambda e: sum(1 for c in _v1(e) if c.get("freeze")) == 2, "the freeze")
    still = next(c for c in _v1(after) if c.get("freeze") and c["start"] < t2)
    assert still["freeze"] == catalog["freeze_default"] and still["start"] == pytest.approx(t, abs=1e-6)
    # The new still is selected and the Inspector says what it is.
    expect(props.get_by_text("Freeze frame — one frame held for 3.00s")).to_be_visible()
    page.set_viewport_size({"width": 1440, "height": 900})
    page.wait_for_timeout(400)
    page.screenshot(path=str(SHOTS / f"speed_{name}_freeze_1440x900.png"))
    geo.select(base_url, sid, last)
    page.get_by_role("radio", name="Curve", exact=True).click()
    page.wait_for_timeout(300)
    page.screenshot(path=str(SHOTS / f"speed_{name}_custom_1440x900.png"))

    # 4. Export through the dialog; decode the downloaded file.
    page.locator(".topbar-pinned button.primary").click()
    dialog = page.get_by_role("dialog")
    dialog.wait_for()
    with page.expect_download(timeout=600_000) as dl:
        dialog.get_by_role("button", name="Export", exact=True).click()
    out = tmp_path / f"export_{name}.mp4"
    dl.value.save_as(str(out))
    final = _edl(base_url, sid)
    model = _expected(final)
    measured = G.measure(out)
    errs = G.compare(measured, model, check_p=False)
    assert errs == [], "export vs program map:\n" + "\n".join(errs[:12])
    n = tb.frame_of(catalog["freeze_default"], 30)
    assert measured["top"][:k] == exp_before["top"][:k]
    assert measured["top"][k:k + n] == [exp_before["top"][k]] * n
    assert len(measured["top"]) == len(exp_before["top"]) + n
    page.context.close()


def test_timeline_toolbar_with_freeze_fits_every_width(engine, base_url, bars):  # noqa: F811
    """Freeze made the timeline toolbar 34 px wider (519 px with the zoom
    steps). Across 900-1300 px windows nothing may be pushed out of the bar:
    the steps drop below a 522 px pane and the gaps tighten below 460 px
    (styles.css), and Freeze itself stays on screen."""
    sid = _project(base_url, bars, f"speed toolbar {engine.engine_name}")
    page = _open(engine, base_url, sid, 1024, 768)
    bad = []
    for w in range(900, 1301, 10):
        page.set_viewport_size({"width": w, "height": 768})
        page.wait_for_timeout(80)
        r = page.evaluate("""() => { const t = document.querySelector('.timeline-toolbar'), tb = t.getBoundingClientRect()
          const right = (n) => document.querySelector(`button[aria-label="${n}"]`).getBoundingClientRect().right
          return [t.scrollWidth - t.clientWidth, right('Zoom to fit') - tb.right, right('Freeze frame') - tb.right] }""")
        if r[0] > 0 or r[1] > 0.5 or r[2] > 0.5:
            bad.append((w, r))
    page.context.close()
    assert bad == [], bad
