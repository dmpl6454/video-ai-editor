"""Instant preview Phase 1d ROBUSTNESS in real WKWebView (INSTANT_PREVIEW_SPEC
§7 fallback ladder and degraded tier, §3.5 visibility, §13 P1-R1).

The page (tests/wk/pages/robustness.html → frontend/src/lib/preview/testkit/
wkRobustnessPage.ts) builds the real PreviewEngine over the same fixture
proxies as test_wk_phase1_video.py, plus each source's MASTER: a long-GOP
x264 mp4 with B-frames and an edit list, the shape ingest's normalised
masters have, carrying the same bar (source id + frame index) in every frame.

* P1-R1 proxy failed → the degraded <video> tier: every paused frame of the
  failed source exact to the bar (+1 ms bias, rVFC-confirmed), within the
  2-media-element budget; the other sources stay on laneA, exact.
* §3.5 hidden: no laneA append or remove and no new span fetch while the
  window is ordered out; everything resumes when it is back.
* P1-R1 decode errors: 3 within 60 s → server mode; 1 → laneA rebuilt and
  the frame exact again.
* P1-R1 webglcontextlost not restored in 2 s → server mode (the snapshot and
  restore half is test_wk_phase1_video / test_wk_engine_faults).
* P1-R1 MediaSource deleted → server mode (the app's decision and the
  engine's own refusal); that preview.mp4 then PLAYS is
  test_wk_fallback_preview.py, against the real server render.

Frame identity is exact everywhere; only waits are timing.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from fractions import Fraction

from .conftest import FRONTEND, PAGES
from .engine_server import EngineServer
from .harness import WKHarness
from .playback import timing_budget
from .proxy_fixture import SourceSpec, encode_master, write_proxy_dir
from .test_wk_phase1_video import SOURCES, _esbuild, _tb, engine_media, timeline_fixture  # noqa: F401  (engine_media: fixture)

#: A one-minute source (30 spans of 60 frames) for the span-fault cases: a
#: paused seek to k=1500 lands in span 25, outside the initial window.
LONG = SourceSpec("L", 7, 960, 540, 1800, "testsrc2", Fraction(30))
LONG_SPAN = "/api/proxies/L/v/{:04d}.bin"


def long_fixture() -> dict:
    clip = {"id": "c000", "src": "L", "in": 0.0, "out": 60.0, "start": 0.0, "speed": None, "reverse": False,
            "transform": {"x": 0, "y": 0, "scale": 1, "rotation": 0, "opacity": 1}, "fit": "contain",
            "effects": []}
    edl = {"duration": 60.0, "canvas": {"w": 640, "h": 360, "fps": 30},
           "tracks": [{"id": "v1", "type": "video", "clips": [clip], "transitions": []}]}
    src = {"key": "L", "srcId": LONG.src_id,
           "info": {"rate": [30, 1], "tb": _tb(LONG.rate), "frames": LONG.frames, "start_ticks": 0,
                    "w": LONG.w, "h": LONG.h}}
    return {"edl": edl, "sources": {"L": src}, "canvas": [640, 360]}

ENTRY = FRONTEND / "src" / "lib" / "preview" / "testkit" / "wkRobustnessPage.ts"


@pytest.fixture(scope="module")
def masters(engine_media, tmp_path_factory):  # noqa: F811
    cache = os.environ.get("VAI_WK_FIXTURE_CACHE")
    root = Path(cache) / "engine-v1-masters" if cache else tmp_path_factory.mktemp("wk-masters")
    root.mkdir(parents=True, exist_ok=True)
    for spec in SOURCES:
        out = root / f"{spec.name}.mp4"
        if not out.is_file():
            encode_master(spec, out)
    return root


@pytest.fixture(scope="module")
def long_proxy(tmp_path_factory) -> Path:
    cache = os.environ.get("VAI_WK_FIXTURE_CACHE")
    root = Path(cache) / "robust-long" if cache else tmp_path_factory.mktemp("wk-robust-long")
    if not (root / "L" / "index.json").is_file():
        write_proxy_dir(LONG, root)
    return root / "L"


@pytest.fixture(scope="module")
def robust_server(engine_media, masters, long_proxy, tmp_path_factory):  # noqa: F811
    kit = tmp_path_factory.mktemp("wk-robust-kit")
    _esbuild(ENTRY, kit / "wkRobustnessPage.js")
    fx = tmp_path_factory.mktemp("wk-robust-fixture")
    tl = timeline_fixture()
    for name, s in tl["sources"].items():
        s["media"] = f"/masters/{name}.mp4"
    (fx / "timeline.json").write_text(json.dumps(tl))
    (fx / "long.json").write_text(json.dumps(long_fixture()))
    # /api/health for the reload test (the real api.ts asks it)
    api = tmp_path_factory.mktemp("wk-robust-api")
    (api / "health").write_text(json.dumps({"ok": True}))
    srv = EngineServer({"pages": PAGES, "testkit": kit, "fixture": fx, "masters": masters, "api": api},
                       proxies={**engine_media, "L": long_proxy})
    yield srv
    srv.close()


@pytest.fixture(scope="module")
def wk_robust(robust_server, tmp_path_factory):
    h = WKHarness(robust_server, tmp_path_factory.mktemp("wk-robust-runs"))
    yield h
    h.close()


def run(harness, scenario: str, timeout: float = 240, **query) -> dict:
    r = harness.run("pages/robustness.html", {"scenario": scenario, **query}, timeout=timeout).result
    assert "AppleWebKit" in r["ua"] and "Chrome" not in r["ua"], r["ua"]
    return r


def _load() -> str:
    try:
        return "load %.1f %.1f %.1f" % os.getloadavg()
    except OSError:
        return "load ?"


@pytest.mark.wk
def test_p1_r1_failed_proxy_paused_frames_come_exact_from_the_degraded_tier(wk_robust, robust_server):
    """Source P's proxy is gone (410): every paused frame of P (normal, 2x and
    reverse clips) is shown from P's master through the degraded <video>,
    exact to the bar; Q and S frames in between stay exact on laneA."""
    robust_server.fail("/api/proxies/P/index.json", 410, times=1000)
    try:
        r = run(wk_robust, "degraded", timeout=400)
    finally:
        robust_server.clear()
    print(json.dumps({"degraded": {k: r[k] for k in ("n", "nBad", "degradedFrames", "p50", "p95", "max", "degradedStats",
                                                     "elements", "liveVideos", "bakedFrames")}, "load": _load()}))
    assert r["became"] is True and r["mode"] == "client"
    assert r["failure"] and "410" in r["failure"]
    assert r["degradedFrames"] > 250 and r["n"] == r["degradedFrames"] + r["others"]
    assert r["nBad"] == 0, json.dumps(r["bad"])
    # classified: exactly P's frames are BAKED 'proxy:degraded' (BAKED while playing)
    assert r["flaggedEqual"] is True
    # the budget (§3.2, P1-E1): laneA + the one degraded element
    assert r["elements"] == 2 and r["liveVideos"] == 2
    st = r["degradedStats"]
    assert st["loads"] == 1
    # every frame was handed over confirmed (presented-frame stamp or rVFC),
    # never on a timeout, and none failed
    assert st["unconfirmed"] == 0 and st["failures"] == 0, st
    # (a show a newer one replaced before hand-over counts as superseded: the
    # failure now shows the paused frame at once, and a seek can replace it)
    assert st["stamped"] + st["confirmed"] + st["reused"] + st["superseded"] + st["cancelled"] == st["shows"], st
    assert st["superseded"] + st["cancelled"] <= 2, st


@pytest.mark.wk
@pytest.mark.parametrize("status,times", [(410, 1000), (500, 5)], ids=["gone", "five_transient"])
def test_p1_r1_a_paused_frame_whose_proxy_fails_after_the_seek_comes_from_the_degraded_tier(
        wk_robust, robust_server, status, times):
    """Review RD3: the engine is paused on a frame of P when P's index fails
    (410, or 500 for all five open retries): the degraded tier must show
    THAT frame, without another seek. It stayed stale under a spinner (the
    410 case for good; the 500x5 case until the 10 s reopen succeeded)."""
    robust_server.fail("/api/proxies/P/index.json", status, times=times)
    try:
        r = run(wk_robust, "failed_first_frame", k=0, wait=20000)
    finally:
        robust_server.clear()
    print(json.dumps({"failed_first_frame": {k: r[k] for k in ("shown", "ms", "degraded", "got", "exp", "next")},
                      "load": _load()}))
    assert r["degraded"], r
    assert r["shown"] and r["got"] == r["exp"], r
    assert r["ms"] < timing_budget(8000), r["ms"]      # 410: ~0.1 s; five retries: ~4 s
    assert r["next"]["ok"], r["next"]


@pytest.mark.wk
@pytest.mark.parametrize("times", [1, 8])
def test_a_span_that_fails_while_paused_is_retried_by_itself(wk_robust, robust_server, times):
    """Review RD3: span 25 answers 500 `times` times while the engine is
    paused on a frame in it and nothing else is loading. Eight failures used
    to stop every request (4 tried, then silence for 12 s, the spinner up)."""
    robust_server.fail(LONG_SPAN.format(25), 500, times=times)
    try:
        r = run(wk_robust, "span_retry", fixture="/fixture/long.json", k=1500, wait=12000)
        log = [(round(e["t"], 2), e["status"], e.get("range"))
               for e in robust_server.request_log if e["path"] == LONG_SPAN.format(25)]
    finally:
        robust_server.clear()
    print(json.dumps({"span_retry": {"times": times, **r["r"]}, "diag": r["diag"],
                      "requests": log,
                      "load": _load()}))
    assert r["r"]["shown"] and r["r"]["ok"], r
    assert r["r"]["ms"] < timing_budget(8000), r["r"]          # 8 failures: ~6 s of back-off


@pytest.mark.wk
def test_spans_still_encoding_do_not_hold_every_fetch_slot(wk_robust, robust_server):
    """Review RD3: spans 22-29 answer 202 (an on-demand encode, Retry-After
    0.05 s) for a long time; a paused seek into them, then one to a READY
    span (16): the ready frame shows at once. Each 202 used to keep its slot
    for up to 30 s, and all three slots were taken."""
    with robust_server._lock:
        for n in range(22, 30):
            robust_server.pending[LONG_SPAN.format(n)] = 300
    try:
        r = run(wk_robust, "span_pending", fixture="/fixture/long.json", k1=1560, k2=1000, dwell=800, wait=6000)
    finally:
        robust_server.clear()
    print(json.dumps({"span_pending": {"stuck": r["stuck"]["store"], "r": r["r"]}, "load": _load()}))
    assert r["r"]["shown"] and r["r"]["ok"], r
    assert r["r"]["ms"] < timing_budget(1500), r["r"]


@pytest.mark.wk
def test_a_silently_stuck_playback_is_restarted_by_the_watchdog(wk_robust):
    """Review RD3: in client mode Space set playing while the picture stayed
    on its first frames for seconds, with no error and no 'waiting'. Induced
    here by losing the rVFC chain at play start; the watchdog restarts the
    run after STALL_MS (engineLoop.ts) and every frame drawn is exact."""
    r = run(wk_robust, "stall_watchdog", wait=6000)
    print(json.dumps({"stall_watchdog": r, "load": _load()}))
    assert r["swallowed"] >= 1, r
    assert r["moved"] and r["restarts"] >= 1, r
    assert r["ms"] < timing_budget(4000), r
    assert r["drawnPlaying"] > 20 and r["nWrong"] == 0, r


#: spans served late in the hidden test, so fetches are still queued when the
#: window is ordered out (the store runs 3 at a time)
_SLOW_SPANS = [f"/api/proxies/{s.name}/v/{n:04d}.bin" for s in SOURCES for n in range(1, 5)]


@pytest.mark.wk
def test_hidden_page_suspends_appends_and_span_fetches_then_resumes(wk_robust, robust_server):
    """The window is ordered out right after the first frame, with span
    fetches still queued (some spans are served 1.5 s late): no append, no
    remove and no NEW span requested while hidden; a seek made while hidden
    shows exactly after the window is back, and the window fills again."""
    for attempt in range(3):
        for path in _SLOW_SPANS:
            robust_server.set_delay(path, 1.5)
        try:
            r = run(wk_robust, "hidden")
        finally:
            robust_server.clear()
        print(json.dumps({"hidden": {k: r[k] for k in ("hid", "back", "stillHidden", "vis", "atHideIdle", "whileHidden",
                                                       "after", "shown", "newWhileHidden", "sawHiddenAt", "suspendedAtSeek",
                                                       "eventsAtSeek")}, "attempt": attempt,
                          "load": _load()}))
        # another WK run on this machine can cover or reveal our 4 px window
        # (a hide/show we did not ask for): that run says nothing; try again
        if r["hid"] and r["stillHidden"] and len(r["vis"]) == 2:
            break
    assert r["hid"] and r["back"] and r["stillHidden"], r["vis"]
    a, w, after = r["atHideIdle"], r["whileHidden"], r["after"]
    assert a["queued"] > 0, a                     # span fetches were waiting when the page hid
    assert w["appends"] == a["appends"] and w["removes"] == a["removes"], (a, w)
    assert w["buffered"] == a["buffered"], (a, w)
    assert w["queued"] >= a["queued"], (a, w)     # nothing drained while hidden
    assert r["newWhileHidden"] == [], r["newWhileHidden"]
    s = r["shown"]
    assert s["shown"] and s["ok"], s
    # back: the queued fetches ran and laneA appended again
    assert after["spanFetches"] > w["spanFetches"] and after["appends"] > w["appends"], (w, after)


@pytest.mark.wk
def test_p1_r1_three_decode_errors_in_60s_fall_back_to_server_mode(wk_robust):
    """A frame whose bytes never decode: each MediaError is counted once, the
    lane is rebuilt after the 1st and 2nd, and the 3rd switches the engine to
    server mode."""
    r = run(wk_robust, "decode_errors", variant="noise", times=1000)
    print(json.dumps({"decode_errors": {k: r[k] for k in ("errors", "statuses", "mode", "reason", "fellBackMs", "recovery",
                                                          "served", "elements")}, "load": _load()}))
    assert r["mode"] == "server" and r["reason"] == "decode-errors", r
    assert r["recovery"]["decodeErrors"] == 3 and r["recovery"]["laneRebuilds"] == 2, r["recovery"]
    assert len(r["errors"]) >= 3
    assert r["elements"] == 1  # a rebuilt lane reuses laneA's element


@pytest.mark.wk
def test_p1_r1_one_decode_error_rebuilds_the_lane_and_shows_the_exact_frame(wk_robust):
    r = run(wk_robust, "decode_errors", variant="noise", times=1)
    print(json.dumps({"decode_error_once": {k: r[k] for k in ("errors", "mode", "recovery", "served", "caused", "after")},
                      "load": _load()}))
    assert r["caused"] == 1
    assert r["mode"] == "client", r
    assert r["recovery"]["decodeErrors"] == 1 and r["recovery"]["laneRebuilds"] == 1, r["recovery"]
    a = r["after"]
    assert a["shown"] and a["ok"], a
    assert a["other"]["shown"] and a["other"]["ok"], a


@pytest.mark.wk
def test_p1_r1_context_lost_and_never_restored_falls_back_in_2s(wk_robust):
    r = run(wk_robust, "context_no_restore")
    if r.get("skipped"):
        pytest.skip(r["skipped"])
    assert r["shown"]["ok"], r["shown"]
    assert r["lost"]["mode"] == "client" and r["lost"]["snapshotK"] == 75 and r["lost"]["snapVisible"] == "visible", r
    assert r["fell"] is True and r["mode"] == "server" and r["reason"] == "webgl-lost", r
    assert 1900 <= r["fellMs"], r["fellMs"]


@pytest.mark.wk
def test_p1_r1_media_source_deleted_is_server_mode(wk_robust):
    r = run(wk_robust, "no_mse")
    assert r["had"]["mse"] is True and r["gone"] is True
    assert r["caps"]["mse"] is False
    assert all(d["mode"] == "server" and d["reason"] == "no-mse" for d in r["decisions"]), r["decisions"]
    assert r["status"] == {"mode": "server", "reason": "no-mse", "playing": False}, r["status"]
    assert r["engineVideos"] == 0


@pytest.mark.wk
def test_a_reload_with_a_request_in_flight_is_not_an_engine_outage(wk_robust, robust_server, tmp_path):
    """⌘R / location.reload with an app request in flight (the real api.ts
    and lib/connection.ts): WKWebView fails the request with "Load failed"
    just BEFORE pagehide. The dying page must not flip the engine offline
    (the "editor engine disconnected / not responding" flash, server mode
    too) and must not hand the caller an engine-offline error."""
    robust_server.set_delay("/api/health", 3.0)
    try:
        r = run(wk_robust, "reload_in_flight", timeout=60)
    finally:
        robust_server.clear()
    print(json.dumps(r))
    assert r.get("reloaded") is True, r
    log = r["log"]
    evs = [e["ev"] for e in log]
    assert "pagehide" in evs, log                            # the page really left
    assert not any(e["ev"] == "engine" for e in log), log    # never 'offline'
    for e in log:
        if e["ev"] == "rejected":
            assert e["offline"] is False and e["abort"] is True, e



# ------------------------------------------- Playwright Chromium and WebKit
#
# The same pages for the engine LOGIC in both Playwright engines (spec §13),
# frame identity exact; a screenshot of the canvas per browser when
# VAI_ENGINE_SHOTS is set. WKWebView above stays the acceptance engine.

SHOTS = Path(os.environ["VAI_ENGINE_SHOTS"]) if os.environ.get("VAI_ENGINE_SHOTS") else None


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def pw_browser(request):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as pw:
        try:
            b = getattr(pw, request.param).launch()
        except Exception as e:  # noqa: BLE001 — a missing browser is a skip
            pytest.skip(f"no Playwright {request.param}: {e}")
        b.engine_name = request.param
        yield b
        b.close()


def _pw(browser, server, scenario: str, timeout: float = 240, **query) -> dict:
    import secrets
    import time
    token = secrets.token_hex(8)
    page = browser.new_page(viewport={"width": 700, "height": 420})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(server.url("pages/robustness.html", {"scenario": scenario, "token": token, **query}))
        deadline = time.monotonic() + timeout
        with server.box.cond:
            while token not in server.box.results and time.monotonic() < deadline:
                server.box.cond.wait(0.2)
            body = server.box.results.pop(token, None)
        assert body is not None, f"{scenario} posted nothing in {browser.engine_name}; page errors {errors}"
        r = json.loads(body)
        assert not r.get("fatal"), r.get("fatal")
        if SHOTS:
            SHOTS.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(SHOTS / f"robust-{scenario}-{browser.engine_name}.png"))
        return r
    finally:
        page.close()


def test_playwright_degraded_tier_is_exact(pw_browser, robust_server):
    robust_server.fail("/api/proxies/P/index.json", 410, times=100000)
    try:
        r = _pw(pw_browser, robust_server, "degraded", timeout=400)
    finally:
        robust_server.clear()
    print(json.dumps({pw_browser.engine_name: {k: r[k] for k in ("n", "nBad", "p50", "p95", "degradedStats", "elements")}}))
    assert r["became"] is True and r["mode"] == "client"
    assert r["nBad"] == 0, json.dumps(r["bad"])
    assert r["flaggedEqual"] is True and r["elements"] == 2
    st = r["degradedStats"]
    assert st["failures"] == 0 and st["unconfirmed"] == 0, st


def test_playwright_decode_errors(pw_browser, robust_server):
    r = _pw(pw_browser, robust_server, "decode_errors", variant="noise", times=1000)
    print(json.dumps({pw_browser.engine_name: {k: r[k] for k in ("errors", "mode", "reason", "recovery", "served")}}))
    if not r["errors"]:
        # this engine decodes the noise without an error (a concealed
        # picture): nothing to fall back from, and the engine must stay up
        assert r["mode"] == "client", r
        return
    assert r["mode"] == "server" and r["reason"] == "decode-errors", r
    assert r["recovery"]["decodeErrors"] == 3 and r["recovery"]["laneRebuilds"] == 2, r["recovery"]


def test_playwright_media_source_deleted_is_server_mode(pw_browser, robust_server):
    r = _pw(pw_browser, robust_server, "no_mse")
    assert r["gone"] is True and r["caps"]["mse"] is False
    assert r["status"] == {"mode": "server", "reason": "no-mse", "playing": False}, r["status"]
