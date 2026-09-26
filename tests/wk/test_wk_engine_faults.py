"""Engine races and faults in real WKWebView (INSTANT_PREVIEW_SPEC §3.2, §3.5,
§4.1 step 5, §7; the RD2 adversarial review's scenarios, promoted).

The page (tests/wk/pages/engine_faults.html → frontend/src/lib/preview/
testkit/wkEngineFaultPage.ts) builds the real PreviewEngine over the same
fixture proxies as test_wk_phase1_video.py and reads the bar burned into
every source frame back off the engine canvas.

* paused seek races: seek(K) → seek(K+1) → seek(K) in one task, and a seek
  followed by setTimeline in the same task. WebKit fires ONE 'seeking' for two
  assignments of the same time in one task; the engine's stale-'seeked' guard
  then drifted one behind for good and no paused seek ever showed again.
* a seek while playing: the sound restarts at the new position, never at a
  stale frame WebKit still presents from the old one.
* webglcontextlost while playing: picture and sound stop together; the
  snapshot never shows another frame; both resume on restore.
* one 5xx on a proxy's index.json: retried, not a proxy degraded for life.

Frame identity and sample positions are exact assertions; only the waits are
timing (each paused seek must show inside 1.5 s, below the 2 s seek timeout
that would otherwise mask a wedge).
"""
from __future__ import annotations

import json
import os

import pytest

from .engine_server import EngineServer
from .conftest import FRONTEND, PAGES
from .harness import WKHarness
from .test_wk_phase1_video import _esbuild, engine_media, timeline_fixture  # noqa: F401  (engine_media: fixture)

pytestmark = pytest.mark.wk

ENTRY = FRONTEND / "src" / "lib" / "preview" / "testkit" / "wkEngineFaultPage.ts"


@pytest.fixture(scope="module")
def fault_server(engine_media, tmp_path_factory):  # noqa: F811
    kit = tmp_path_factory.mktemp("wk-engine-fault-kit")
    _esbuild(ENTRY, kit / "wkEngineFaultPage.js")
    fx = tmp_path_factory.mktemp("wk-engine-fault-fixture")
    (fx / "timeline.json").write_text(json.dumps(timeline_fixture()))
    srv = EngineServer({"pages": PAGES, "testkit": kit, "fixture": fx}, proxies=engine_media)
    yield srv
    srv.close()


@pytest.fixture(scope="module")
def wk_faults(fault_server, tmp_path_factory):
    h = WKHarness(fault_server, tmp_path_factory.mktemp("wk-engine-fault-runs"))
    yield h
    h.close()


def run(harness, scenario: str, timeout: float = 240, **query) -> dict:
    r = harness.run("pages/engine_faults.html", {"scenario": scenario, **query}, timeout=timeout).result
    assert "AppleWebKit" in r["ua"] and "Chrome" not in r["ua"], r["ua"]
    assert "fatal" not in r, r.get("fatal")
    return r


def _load() -> str:
    try:
        return "load %.1f %.1f %.1f" % os.getloadavg()
    except OSError:
        return "load ?"


def test_triple_paused_seek_in_one_task_never_wedges(wk_faults):
    """seek(K) → seek(K+1) → seek(K) in one task (a scrub that lands, then →
    and ←), then a plain seek elsewhere, 80 times: every target shows its
    exact frame and the seek counters stay in step."""
    r = run(wk_faults, "flipflop", **({"trace": "1"} if os.environ.get("FIX2_TRACE") else {}))
    print(json.dumps({"flipflop": {k: r[k] for k in ("n", "nBad", "seekTimeouts", "finalLag")}, "load": _load()}))
    assert r["n"] == 160
    assert r["nBad"] == 0, json.dumps(r["bad"])
    assert r["finalLag"] == 0
    assert r["seekTimeouts"] == 0


def test_paused_seek_then_same_task_set_timeline_never_wedges(wk_faults):
    """A paused seek and the same timeline pushed again in the same task (what
    the controller does when /frame_map or a source lookup lands), 60 times."""
    r = run(wk_faults, "seek_then_push")
    print(json.dumps({"seek_then_push": {k: r[k] for k in ("n", "nBad", "seekTimeouts", "finalLag")}, "load": _load()}))
    assert r["n"] == 60
    assert r["nBad"] == 0, json.dumps(r["bad"])
    assert r["finalLag"] == 0
    assert r["seekTimeouts"] == 0


def test_seek_while_playing_restarts_sound_at_the_new_position(wk_faults):
    """24 seeks while playing, each ≥ 40 frames away: every sound (re)start
    after a seek lies within 12 frames of the seek's target (the restart
    anchors a frame of the NEW position plus the sink's lead), never at a
    frame WebKit still presented from the old position; the picture stays
    exact."""
    r = run(wk_faults, "seek_while_playing")
    print(json.dumps({"seek_while_playing": {k: r[k] for k in ("seeks", "restarted", "nStale", "drawn", "wrong")}, "load": _load()}))
    assert r["seeks"] == 24
    assert r["restarted"] >= 12, r  # most seeks restart before the next one
    assert r["nStale"] == 0, r["stale"]
    assert r["wrong"] == 0


def test_context_loss_while_playing_stops_both_and_resumes_both(wk_faults):
    r = run(wk_faults, "context_loss_playing")
    if r.get("skipped"):
        pytest.skip(r["skipped"])
    print(json.dumps({"context_loss_playing": {k: r[k] for k in ("events", "during", "later", "after", "drawnAfter")}}))
    d = r["during"]
    # picture and sound stopped together, at the frame on screen
    assert d["playing"] is False and d["videoPaused"] is True, d
    assert d["stops"] and d["stops"][0] == d["kLost"], d
    assert d["starts"] == 0
    assert r["events"] and r["events"][0]["cause"] == "context" and r["events"][0]["willResume"] is True
    # the snapshot never shows ANOTHER frame: it holds kLost, or it is black
    # (with the spinner up)
    if d["snapshotK"] != d["kLost"]:
        assert d["snapshotK"] == -1 and d["level"] < 2 and d["spinner"] is True, d
    later = r["later"]
    assert later["playing"] is False and later["presentedK"] == d["kLost"] and later["starts"] == 0, later
    # restored: both resume from a fresh anchor at the frame they stopped on
    a = r["after"]
    assert a["mode"] == "client" and a["glVisible"] == "visible"
    assert a["playing"] is True and a["presentedK"] > d["kLost"], a
    R = r["R"]
    assert a["starts"], a
    first = a["starts"][0]["sample"] / 48000 * R["num"] / R["den"]
    assert abs(first - d["kLost"]) <= 12, (first, d["kLost"])
    assert r["drawnAfter"] > 5
    assert r["wrong"] == [], r["wrong"][:6]


def test_context_loss_while_paused_keeps_the_paused_frame_and_stays_paused(wk_faults):
    r = run(wk_faults, "context_loss_paused")
    if r.get("skipped"):
        pytest.skip(r["skipped"])
    assert r["lost"]["snapshotK"] == 75 and r["lost"]["visible"] == "visible" and r["lost"]["level"] > 5, r["lost"]
    assert r["shown"] is True and r["bar"] == r["exp"], r
    assert r["playing"] is False


def test_one_index_json_500_is_retried_not_degraded_for_life(wk_faults, fault_server):
    """A transient 500 on source P's index.json at open (the RD2 index_500
    scenario): the proxy opens on a retry, the first frame shows exact, and
    none of P's ranges is demoted to BAKED 'proxy:degraded'."""
    fault_server.fail("/api/proxies/P/index.json", 500, times=1)
    try:
        r = run(wk_faults, "index_500")
    finally:
        fault_server.clear()
    print(json.dumps({"index_500": r}))
    assert r["shown"] is True and r["bar"] == r["exp"], r
    assert r["degraded"] == []
    assert r["retries"] >= 1 and r["failure"] is None
