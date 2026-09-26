"""Phase 1 WK acceptance probes for laneA's MSE contract (INSTANT_PREVIEW_SPEC
§2, §3.2, §6 R1/R7/R8, §10, §13), driven by the REAL ``media/fmp4Writer.ts``
over proxy-shaped span packs, in real WKWebView.

These are the design-phase WebKit measurements the spec cites, re-run on the
code that ships: if a WebKit release changes any of them, the engine's
design assumptions changed and this suite says which one.
"""
from __future__ import annotations

import json

import pytest

from .playback import assert_drops_bounded

from .harness import WKRun
from .proxy_fixture import FRAME_BITS

pytestmark = pytest.mark.wk

STANDARD_RATES = [(24000, 1001), (24, 1), (25, 1), (30000, 1001), (30, 1), (48, 1), (50, 1), (60000, 1001), (60, 1)]
PAGE = "pages/mse.html"


def run(wk, scenario: str, rate: tuple[int, int] = (30, 1), timeout: float = 90) -> dict:
    res: WKRun = wk.run(PAGE, {"scenario": scenario, "num": rate[0], "den": rate[1]}, timeout=timeout)
    r = res.result
    # The engine under test is WebKit (the app's WKWebView), never Chromium.
    assert "AppleWebKit" in r["ua"] and "Chrome" not in r["ua"], r["ua"]
    assert r["api"]["MediaSource"] and r["api"]["rvfc"], r["api"]
    assert r["isTypeSupported"] is True, r["codec"]
    return r


def _fmt(code: int | None) -> str:
    if code is None or code < 0:
        return str(code)
    return f"src{code >> FRAME_BITS}:{code & ((1 << FRAME_BITS) - 1)}"


def _bad(rows: list[dict], got: str = "atSeeked") -> list[str]:
    return [f"k={r['k']} exp={_fmt(r['exp'])} got={_fmt(r[got])}" for r in rows if r[got] != r["exp"]]


@pytest.mark.parametrize("rate", STANDARD_RATES, ids=[f"{n}/{d}" for n, d in STANDARD_RATES])
def test_paused_mid_frame_seeks_are_exact_at_seeked(wk, rate):
    """§2: 42/42 paused seeks to (k+0.5)/R exact at `seeked` — here over cuts
    across 3 sources and 2 size classes, reverse, 2x, a gap and duplicates,
    at every standard rate the writer's tfdt = k·T grid supports."""
    r = run(wk, "seek_exact", rate)
    seeks = r["seeks"]
    assert len(seeks) >= 42
    assert r["buffered"] == [[0, pytest.approx(r["frames"] * rate[1] / rate[0], abs=2e-3)]]  # one contiguous range
    assert r["inits"] == 3  # A-class, C-class, back to A-class (B shares A's avcC)
    assert _bad(seeks) == [], f"{len(_bad(seeks))}/{len(seeks)} wrong at seeked"
    assert max(s["ms"] for s in seeks) < 250


@pytest.mark.parametrize("rate", [(30000, 1001), (25, 1), (60000, 1001)], ids=["29.97", "25", "59.94"])
def test_single_frame_steps_repaint_every_frame(wk, rate):
    """Frame stepping with (k+0.5)/R seeks: every step, forward and back,
    repaints the NEXT frame by `seeked` (R7). Exact-boundary k/R seeks are
    recorded only: they are the WebKit bug the half-frame rule avoids."""
    r = run(wk, "seek_step", rate)
    steps = r["steps"]
    assert len(steps) == 80
    assert _bad(steps) == []
    assert all(s["atSeeked"] != s["prev"] for s in steps), "a step did not repaint"
    boundary_ok = sum(b["got"] == b["exp"] for b in r["boundary"])
    print(json.dumps({"rate": rate, "exact_boundary_ok": f"{boundary_ok}/{len(r['boundary'])}"}))


def test_overwrites_are_re_enqueued_paused_and_ahead_of_the_playhead(wk):
    """§2: an overwrite at the paused playhead shows after a re-seek (edit to
    pixel ≈ 15 ms), and overwrites 4 and 15 frames ahead of the presented frame
    while PLAYING are shown with 0 stale frames (WebKit re-enqueues them)."""
    r = run(wk, "overwrite")
    p = r["paused"]
    print(json.dumps({"paused_overwrite": p}))
    assert p["before"] != p["exp"]
    assert p["reSeek"] == p["exp"], p
    assert p["editToPixelMs"] < 100, p
    play = r["playing"]
    assert [e["ahead"] for e in sorted(play["edits"], key=lambda e: e["firstK"])] == [4, 15]
    assert play["timedOut"] is False and play["waiting"] == 0
    stale = [f"k={k} exp={_fmt(e)} got={_fmt(g)}" for k, e, g in play["mismatches"]]
    assert stale == [], f"{len(stale)} stale frames: {stale[:10]}"
    assert_drops_bounded(play)


def test_init_switch_between_size_classes_is_seamless(wk):
    """§2: a new init segment before each size-class change plays through
    with no stall, no wrong frame, and the size flips on the exact frame."""
    r = run(wk, "init_switch")
    assert r["inits"] == 4  # A, C, A (B shares it), C
    assert sorted(r["sizes"]) == ["1280x720", "720x1280"]
    # rVFC metadata.width/height may lag ONE frame at a class switch (seen once
    # in 17 runs, at k=60, with the right picture): only a switch frame may
    # report a stale size, and only the previous class's.
    switches = {30: (720, 1280), 60: (1280, 720), 120: (720, 1280)}
    for k, w, h, *_ in r["wrongSize"]:
        assert k in switches and (w, h) != switches[k], r["wrongSize"]
    t = r["trace"]
    assert t["timedOut"] is False and t["waiting"] == 0
    assert t["mismatches"] == []
    assert_drops_bounded(t)
    assert len(r["buffered"]) == 1
    assert _bad(r["seeks"]) == []


def test_a_buffered_gap_stalls_so_timeline_gaps_need_filler_samples(wk):
    """§2/§3.2: a hole in the buffered ranges stalls WebKit playback at the
    hole; the same timeline gap written as the writer's filler samples plays
    straight through, showing the filler (the compositor paints black there)."""
    r = run(wk, "gap_stall")
    hole = r["hole"]
    assert len(hole["buffered"]) == 2, hole
    assert hole["currentTime"] <= hole["stallAt"] + 0.05, hole
    assert hole["ended"] is False
    filled = r["filled"]
    assert len(filled["buffered"]) == 1, filled
    assert filled["timedOut"] is False and filled["waiting"] == 0
    assert filled["lastK"] >= 149 - 1
    assert filled["mismatches"] == []
    assert filled["gapFrames"] >= 25
    assert filled["gapCodes"] == [(1 << FRAME_BITS) | 59]  # A frame 59, the previous appended sample


@pytest.mark.parametrize("rate", [(30, 1), (30000, 1001), (25, 1), (24000, 1001)], ids=["30", "29.97", "25", "23.976"])
def test_rvfc_presented_frame_clock(wk, rate):
    """§3.5/R8: on every rVFC, presentedK = round(mediaTime·R) is the frame on
    screen (its bar), mediaTime sits on the k/R grid tfdt = k·T defines, the
    clock is monotonic, and it advances at R."""
    r = run(wk, "rvfc_clock", rate)
    # Report the whole trace summary on failure: a bare "timedOut" hid whether
    # playback stalled (waiting events), stopped early (lastK) or skipped frames.
    assert r["timedOut"] is False and r["waiting"] == 0, " ".join(f"{k}={r.get(k)}" for k in (
        "timedOut", "ended", "waiting", "frames", "firstK", "lastK", "msPerFrame")) + f" missing={len(r.get('missingK') or [])} stall={r.get('stall')}"
    assert r["noPicture"] == 0
    assert r["mismatches"] == []
    assert r["frames"] >= 170
    assert_drops_bounded(r)
    # Real WKWebView lands on the grid to ~1e-14 of a frame; 1e-3 of a frame
    # would let a tfdt a few ticks off (8 ticks at 30 fps) pass unnoticed.
    assert r["maxOffGrid"] < 1e-9
    assert r["monotonic"] is True
    assert r["msPerFrameMedian"] == pytest.approx(r["expectedMsPerFrame"], rel=0.05)
