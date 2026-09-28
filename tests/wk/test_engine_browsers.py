"""The engine pages under Playwright Chromium and Playwright WebKit (spec §13:
"Playwright headless Chromium runs the same pages on every PR for logic
regressions"). WKWebView stays the acceptance engine (test_wk_phase1_video);
these runs catch engine logic that only works by accident in one engine, and
leave a screenshot of the engine canvas per browser.

Frame identity is asserted exactly here too; timing only loosely.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import time
from pathlib import Path

import pytest

from .test_wk_phase1_video import (  # noqa: F401 — module fixtures reused
    APPROX_DB, CANVAS, EXACT_DB, GEO_APPROX, SHOWCASE, engine_bundle, engine_media, engine_server, geo_env,
    geometry_psnr,
)
from .test_wk_geometry_truth import truth_env  # noqa: F401,E402 — module fixture reused

playwright = pytest.importorskip("playwright.sync_api")

SHOTS = Path(os.environ.get("VAI_ENGINE_SHOTS", "")) if os.environ.get("VAI_ENGINE_SHOTS") else None


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def browser(request):
    with playwright.sync_playwright() as pw:
        try:
            b = getattr(pw, request.param).launch()
        except Exception as e:  # noqa: BLE001 — a missing browser is a skip, not a failure
            pytest.skip(f"no Playwright {request.param}: {e}")
        b.engine_name = request.param
        yield b
        b.close()


def _run(browser, server, scenario: str, timeout: float = 90, shot: str | None = None, **query) -> dict:
    token = secrets.token_hex(8)
    page = browser.new_page(viewport={"width": CANVAS[0] + 40, "height": CANVAS[1] + 40})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(server.url("pages/engine.html", {"scenario": scenario, "token": token, **query}))
        deadline = time.monotonic() + timeout
        with server.box.cond:
            while token not in server.box.results and time.monotonic() < deadline:
                server.box.cond.wait(0.2)
            body = server.box.results.pop(token, None)
        assert body is not None, f"{scenario} posted nothing in {browser.engine_name}; page errors {errors}"
        r = json.loads(body)
        assert not r.get("fatal"), r.get("fatal")
        if shot and SHOTS:
            SHOTS.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(SHOTS / f"{shot}-{browser.engine_name}.png"))
        return r
    finally:
        page.close()


def test_paused_exactness_in_playwright_browsers(browser, engine_server):  # noqa: F811
    r = _run(browser, engine_server, "paused_exact", shot="paused")
    assert r["seeks"] == 200
    assert r["bad"] == [], json.dumps(r["bad"][:8])


def test_playback_exactness_in_playwright_browsers(browser, engine_server):  # noqa: F811
    r = _run(browser, engine_server, "playback", timeout=120, shot="playback")
    print(json.dumps({browser.engine_name: {k: r[k] for k in ("frames", "held", "waiting", "elapsedMs")},
                      "missing": len(r["missing"])}))
    assert r["mismatches"] == [], r["mismatches"][:8]
    assert r["monotonic"] is True
    assert r["frames"] >= r["total"] // 2  # headless pacing varies; identity may not


def test_element_budget_in_playwright_browsers(browser, engine_server):  # noqa: F811
    r = _run(browser, engine_server, "element_budget")
    assert r["engineCreated"] == 1 and r["maxLive"] <= 2, r


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
def test_context_loss_in_playwright_browsers(browser, engine_server):  # noqa: F811
    r = _run(browser, engine_server, "context_loss")
    if r.get("skipped"):
        pytest.skip(r["skipped"])
    assert r["redrawn"] is True and r["bar"] == r["exp"], r


def test_geometry_parity_in_playwright_browsers(browser, geo_env):  # noqa: F811
    """The same geometry parity as in WKWebView, in Chromium and WebKit: every
    feature within its fidelity class against the export."""
    srv, server_y = geo_env
    r = _run(browser, srv, "geometry", timeout=180, shot="geometry", hold=f"{SHOWCASE[0]}:{SHOWCASE[1]}")
    per = {f: min(v) for f, v in geometry_psnr(r, server_y).items()}
    print(json.dumps({browser.engine_name: per}))
    for feat, db in per.items():
        assert db >= (APPROX_DB if feat in GEO_APPROX else EXACT_DB), (feat, db)


def test_geometry_truth_parity_in_playwright_browsers(browser, truth_env):  # noqa: F811
    """test_wk_geometry_truth's groups (anamorphic sources, every keyframe
    clock and — wave E, F4a — Transform.flip_h / flip_v) in Chromium and
    WebKit: every feature EXACT against the export."""
    from .test_wk_geometry_truth import _groups
    srv, server_y, _infos = truth_env
    r = _run(browser, srv, "geometry", timeout=300, shot="geometry-truth", mipmaps="1")
    per = {f: min(v) for f, v in geometry_psnr(r, server_y).items()}
    print(json.dumps({browser.engine_name: per}))
    assert set(per) == {f for g in _groups().values() for f, _ in g}
    low = {f: db for f, db in per.items() if db < EXACT_DB}
    assert not low, f"below EXACT ({EXACT_DB} dB): {low}"
