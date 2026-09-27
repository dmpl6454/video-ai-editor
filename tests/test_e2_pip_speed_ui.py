"""Speed, a curve preset and a freeze on OVERLAY (picture-in-picture) clips,
through the UI, in Chromium AND WebKit, proven by the EXPORT (wave D3, E2).

CapCut retimes overlays; this app refused to ("PIP clips render at native
speed") until render/pip.py learnt v1's retime. Here a person's gestures do
it: an overlay clip is selected by clicking it on the timeline canvas, the
Inspector's Speed slider is stepped from the keyboard to 2x, a second
overlay gets the Hero curve from the Curve presets, a third is frozen with
the timeline toolbar's Freeze at a typed playhead. While paused over the 2x
clip, the preview's hidden PIP <video> must sit on the source instant of
the export's frame (lib/pipTime) — in the server preview and, where the
window can run it, the client (instant) preview. The project is then
exported through the Export dialog and the DOWNLOADED file decoded frame by
frame: every frame must be the model's — v1's program map under, and each
overlay clip's own frame list (`frame_map.clip_frame_list`, v1's rule) over
its window — and the freeze must hold the frame the export showed at the
playhead before Freeze was pressed.

Harness: `test_frontend_a11y`'s server fixture (VAE_A11Y_BASE_URL = a Vite
dev server proxying /api to a backend). Screenshots go to VAE_SPEED_SHOTS.
"""
from __future__ import annotations

import os
import shutil
import sys
from fractions import Fraction
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import frame_map_golden_lib as G  # noqa: E402
from test_frontend_a11y import base_url  # noqa: E402,F401  (fixture)
from test_speed_ui_e2e import (  # noqa: E402,F401  (fixtures + helpers)
    FIT_MARGIN, LABEL_W, _client, _edl, _open, _timecode, _wait_edl, engine, pw,
)

from video_ai_editor.edl import timebase as tb  # noqa: E402
from video_ai_editor.edl.schema import EDL  # noqa: E402
from video_ai_editor.render import frame_map as FM  # noqa: E402
from video_ai_editor.render.pip import pip_frames, pip_layout_end  # noqa: E402

try:
    from playwright.sync_api import expect
except ImportError:  # the fixture skips
    expect = None

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
SHOTS = Path(os.environ.get("VAE_SPEED_SHOTS", "/tmp"))
V1_SID, PIP_SID = 3, 4
FPS = 30
#: Transform.scale at which a PIP fills a canvas of its own aspect
#: (pip.py: long edge = canvas long edge × 0.35 × scale).
FILL = 1 / 0.35 + 1e-4


@pytest.fixture(scope="module")
def sources(tmp_path_factory) -> tuple[Path, Path]:
    d = tmp_path_factory.mktemp("pip_ui_bars")
    return (G.make_bar_source(d / "main15.mp4", G.SourceSpec(key="m", sid=V1_SID, rate=Fraction(30), seconds=15.0)),
            G.make_bar_source(d / "over12.mp4", G.SourceSpec(key="o", sid=PIP_SID, rate=Fraction(30), seconds=12.0)))


def _track(edl: dict, tid: str) -> list[dict]:
    return next(t for t in edl["tracks"] if t["id"] == tid)["clips"]


def _dispatch(c, sid: str, tool: str, args: dict) -> dict:
    r = c.post(f"/api/sessions/{sid}/dispatch", json={"tool": tool, "args": args})
    assert r.status_code == 200, r.text
    return r.json()


def _project(base_url, main: Path, over: Path, name: str) -> tuple[str, list[str]]:  # noqa: F811
    """Main video: the 15 s bar source. v2: three clips of the second bar
    source, each filling the canvas (so the decoded code is the PIP's)."""
    with _client(base_url) as c:
        sid = c.post("/api/sessions", json={"name": name}).json()["id"]
        srcs = []
        for path, add in ((main, "true"), (over, "false")):
            with path.open("rb") as fh:
                r = c.post(f"/api/sessions/{sid}/upload", files={"file": (path.name, fh, "video/mp4")},
                           data={"add_to_timeline": add, "transcribe": "false"})
            assert r.status_code in (200, 202), r.text
            srcs.append(r.json()["src"])
        edl = c.get(f"/api/sessions/{sid}/edl").json()
        assert _track(edl, "v1"), "the import never reached the timeline"
        w, h = edl["canvas"]["w"], edl["canvas"]["h"]
        ids = []
        for i_, o, st in ((1.0, 4.0, 1.0), (5.0, 7.0, 4.0), (8.0, 10.0, 8.0)):
            cid = _dispatch(c, sid, "add_clip", {"track": "v2", "src": srcs[1], "in": i_, "out": o,
                                                 "start": st})["result"]["clip_id"]
            _dispatch(c, sid, "set_clip_transform", {"clip_id": cid, "x": w / 2, "y": h / 2, "scale": FILL})
            ids.append(cid)
    return sid, ids


def _select(page, base_url, sid, clip_id: str) -> tuple[float, float]:  # noqa: F811
    """Click a clip on the timeline canvas (any lane) until the Inspector
    shows it; returns the point clicked."""
    page.get_by_role("button", name="Zoom to fit").click()
    page.wait_for_timeout(250)
    edl = _edl(base_url, sid)
    clip = next(c for t in edl["tracks"] for c in t["clips"] if c.get("id") == clip_id)
    e = EDL.model_validate(edl)
    dur = e.get_clip(clip_id)[1].effective_duration
    box = page.locator(".timeline-canvas-wrap canvas").first.bounding_box()
    zoom = max(40.0, box["width"] - LABEL_W - FIT_MARGIN) / edl["duration"]
    x = box["x"] + LABEL_W + (clip["start"] + dur / 2) * zoom
    for i, dy in enumerate(range(30, int(box["height"]) - 4, 5)):
        page.mouse.click(x + ((i % 3) - 1) * 9, box["y"] + dy)
        page.wait_for_timeout(120)
        got = page.locator(".props[data-clip-id]")
        if got.count() and got.get_attribute("data-clip-id") == clip_id:
            return x, box["y"] + dy
    page.screenshot(path=str(SHOTS / "pip_speed_select_failed.png"))
    raise AssertionError(f"could not select {clip_id}")


def _set_playhead(page, t: float) -> None:
    clock = page.get_by_role("textbox", name="Playhead timecode")
    clock.click()
    clock.fill(_timecode(t))
    clock.press("Enter")
    page.wait_for_timeout(300)


def _expected(edl_json: dict) -> list[int]:
    """Per output frame: the PIP's code inside an overlay clip's window, v1's
    program-map code elsewhere (no transitions: layout = render time)."""
    edl = EDL.model_validate(edl_json)
    v1 = _track(edl_json, "v1")[0]["src"]
    pip = _track(edl_json, "v2")[0]["src"]
    info = {v1: G.probe_source(Path(v1)), pip: G.probe_source(Path(pip))}
    top = list(G.expected_frames(edl, {v1: info[v1]}, {v1: V1_SID})["top"])
    for c in edl.get_track("v2").clips:
        f0, n = pip_frames(c.start, pip_layout_end(c), FPS)
        fl = FM.clip_frame_list(c, info[c.src], FPS)
        while len(top) < f0 + n:
            top.append(0)
        for j in range(n):
            top[f0 + j] = G.code_of(PIP_SID, fl[j])
    return top


def _pip_video_time(page) -> float | None:
    return page.evaluate("""() => { const v = document.querySelector('[data-pip-video-host] video')
        return v && Number.isFinite(v.duration) ? v.currentTime : null }""")


def _wait_pip_time(page, want: float, what: str) -> float:
    got = None
    for _ in range(60):
        got = _pip_video_time(page)
        if got is not None and abs(got - want) < 0.002:
            return got
        page.wait_for_timeout(100)
    raise AssertionError(f"{what}: PIP <video> at {got}, the export's frame is at {want:.6f}")


def test_overlay_speed_curve_and_freeze_through_the_ui_export_as_the_model(
        engine, base_url, sources, tmp_path):  # noqa: F811
    name = engine.engine_name
    sid, (p_fast, p_hero, p_frz) = _project(base_url, *sources, f"pip speed ui {name}")
    page = _open(engine, base_url, sid, 1280, 800)
    props = page.locator(".props")

    # 1. 2x on the first overlay: the Inspector's Speed slider, from the keyboard.
    _select(page, base_url, sid, p_fast)
    slider = props.get_by_role("slider", name="Speed")
    slider.focus()
    for _ in range(20):                                       # 1.00 → 2.00 in 0.05 steps
        page.keyboard.press("ArrowRight")
    e = _wait_edl(base_url, sid, lambda e: _track(e, "v2")[0].get("speed") == 2.0, "2x on the overlay")
    fast = _track(e, "v2")[0]
    assert fast["start"] == pytest.approx(1.0), "the overlay keeps its placement"
    assert [c["start"] for c in _track(e, "v2")[1:]] == [pytest.approx(4.0), pytest.approx(8.0)]

    # 2. Hero on the second overlay: Curve, then the preset's radio.
    _select(page, base_url, sid, p_hero)
    page.get_by_role("radio", name="Curve", exact=True).click()
    props.get_by_role("radiogroup", name="Speed curve").wait_for()
    props.get_by_role("radio", name="Hero", exact=True).click()
    e = _wait_edl(base_url, sid, lambda e: (_track(e, "v2")[1].get("speed") or {}).get("name") == "hero",
                  "Hero on the overlay")
    expect(props.get_by_role("radio", name="Hero", exact=True)).to_have_attribute("aria-checked", "true")
    page.screenshot(path=str(SHOTS / f"pip_speed_{name}_hero_1280x800.png"))

    # 3. While paused inside the 2x overlay the preview's PIP element is on
    #    the source instant of the export's frame: slot 15 of the clip at 2x
    #    → in + 2·15.5/30 (lib/pipTime; a 1x map would sit at 1.5).
    _set_playhead(page, 1.5)
    want = 1.0 + 2 * 15.5 / 30 - 1e-6
    _wait_pip_time(page, want, f"{name} server preview")
    page.screenshot(path=str(SHOTS / f"pip_speed_{name}_2x_server_1280x800.png"))

    # 4. Freeze the third overlay at a typed playhead (toolbar Freeze).
    before = _edl(base_url, sid)
    t = 8.5
    k = tb.frame_of(t, FPS)
    exp_before = _expected(before)
    _select(page, base_url, sid, p_frz)
    _set_playhead(page, t)
    freeze = page.get_by_role("button", name="Freeze frame", exact=True)
    expect(freeze).to_be_enabled()
    freeze.click()
    after = _wait_edl(base_url, sid, lambda e: any(c.get("freeze") for c in _track(e, "v2")), "the overlay freeze")
    still = next(c for c in _track(after, "v2") if c.get("freeze"))
    assert still["start"] == pytest.approx(t, abs=1e-6) and still["freeze"] == 3.0
    assert _track(after, "v1") == _track(before, "v1"), "the Main video does not move for an overlay"
    expect(props.get_by_text("Freeze frame — one frame held for 3.00s")).to_be_visible()
    page.set_viewport_size({"width": 1440, "height": 900})
    page.wait_for_timeout(400)
    page.screenshot(path=str(SHOTS / f"pip_speed_{name}_freeze_1440x900.png"))

    # 5. Export through the dialog; decode the downloaded file.
    page.locator(".topbar-pinned button.primary").click()
    dialog = page.get_by_role("dialog")
    dialog.wait_for()
    with page.expect_download(timeout=600_000) as dl:
        dialog.get_by_role("button", name="Export", exact=True).click()
    out = tmp_path / f"pip_export_{name}.mp4"
    dl.value.save_as(str(out))
    final = _edl(base_url, sid)
    model = _expected(final)
    measured = G.measure(out)["top"]
    errs = [f"k={i}: {m} vs {x}" for i, (m, x) in enumerate(zip(measured, model)) if m != x]
    assert len(measured) == len(model) and errs == [], "export vs model:\n" + "\n".join(errs[:12])
    n = tb.frame_of(3.0, FPS)
    assert measured[k:k + n] == [exp_before[k]] * n, "the freeze holds the frame shown before"
    assert measured[:k] == exp_before[:k]

    # 6. The client (instant) preview draws the PIP through the same map.
    page.evaluate("""() => fetch('/api/settings/preview', {method: 'PUT',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({engine: 'client'})})""")
    page.reload()
    page.locator(".timeline-canvas-wrap canvas").first.wait_for()
    page.wait_for_timeout(1500)
    client = page.locator('[data-preview-engine="client"]').count() > 0
    if name == "chromium":
        assert client, "headless Chromium runs the client preview"
    if client:
        _set_playhead(page, 1.5)
        _wait_pip_time(page, want, f"{name} client preview")
        page.screenshot(path=str(SHOTS / f"pip_speed_{name}_2x_client_1440x900.png"))
    page.evaluate("""() => fetch('/api/settings/preview', {method: 'PUT',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({engine: 'server'})})""")
    page.context.close()
