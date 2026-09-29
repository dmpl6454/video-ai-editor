"""Instant preview SOAK and memory budgets (INSTANT_PREVIEW_SPEC §11.3, §13
P1-M1 and P1-E1), in real WKWebView against a REAL backend.

P1-M1: a 12-minute timeline of 1080p bar-coded click-track sources, looped
in client mode with an edit every 10 s (split, trim, move, delete, undo in
rotation) through the store's path (``/dispatch?include=edl`` then
``PreviewController.applyTimeline``), the real AudioEngine sounding. The
harness samples the WebContent process footprint from outside
(``proc_pid_rusage`` phys_footprint of the WKWebView's own WebContent pid,
:class:`harness.FootprintSampler`); the page samples laneA's SourceBuffer,
the span and decoded-audio LRUs and the media elements, reads the bar of
every frame drawn while playing, and checks a random frame paused every
check period. Asserted:

* footprint slope after warm-up < 1 MB/min (least squares);
* SourceBuffer buffered span ≤ 45 s; span LRU ≤ its cap (128 MB);
  decoded-audio LRU ≤ its cap (96 MB); no unhandled QuotaExceededError;
* no wrong frame (every judged playing frame and every paused check);
* ≤ 2 media elements at all times; WebContent never relaunched.

Two forms, both ``wk`` and ``slow`` and opt-in (a plain ``pytest tests/wk``
skips them): select them with ``-m slow`` (or ``-m "wk and slow"``), or name
them in ``VAI_SOAK`` (``short``, ``full`` or ``all``):

* ``full`` — the spec's 30 minutes, the engine's own caps; warm-up 5 min.
* ``short`` — 5 minutes for the gate. It starts 1 minute before the end of
  the program so the loop (and the refill after it) falls in its 2.5-minute
  warm-up, and runs the LRUs below their caps (64 MB spans: above laneA's
  ~30 MB working window, so spans are not refetched in a loop; 24 MB audio)
  so they are full within the warm-up. 2.5 steady minutes cannot resolve a
  1 MB/min slope, so it fails a gross leak (``Form.noise_mb``); every other
  assertion is the full form's.

The slope is judged on the footprint that SURVIVES a full JavaScript
collection: the harness forces one every 30 s (15 s in the short form,
``WKHarness.run(gc_every=…)``, WebKit's own test SPI) and
:func:`post_gc_points` takes the lowest sample after each. Without it the
footprint is mostly garbage JavaScriptCore has not collected yet (measured:
128 MB of dropped fetch buffers stayed in the footprint until a forced
collection took it to 21 MB), so its level moves by 100-200 MB with the
collector's heuristics and a least-squares line through it measures those,
not a leak. The raw slope and the per-window floor (:func:`floor_points`)
are reported beside it.

``VAI_SOAK_MINUTES`` overrides a form's length. ``VAI_SOAK_ROOT`` puts the
backend's WORKDIR (and the result dump) in a directory of your choice;
``VAI_WK_FIXTURE_CACHE`` keeps the masters between runs.

P1-E1 (``test_p1_e1_element_budget_over_100_dispatch_edits``, ``wk`` only):
100 dispatch edits through the controller, paused and playing, with seeks.
"""
from __future__ import annotations

import json
import os
import statistics
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from .harness import PageServer, WKHarness, process_footprint, wk_unavailable_reason
from .live_backend import GO_PAGE, LiveBackend
from .playback import load_per_core, machine_is_quiet
from .soak_fixture import FPS, SPECS, Session, build_soak_masters, make_session, restart_layout, short_layout, twelve_minute_layout

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
ENTRY = FRONTEND / "src" / "lib" / "preview" / "testkit" / "wkSoakPage.ts"
PAGES = Path(__file__).resolve().parent / "pages"
ENGINE_CANVAS = [640, 360]
PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>soak</title></head>
<body style="margin:0;background:#000"><script type="module" src="/wk/bundle/soak.js"></script></body></html>"""

MB = 1 << 20
SLOPE_BUDGET_MB_PER_MIN = 1.0
BUFFERED_BUDGET_S = 45.0
SPAN_CAP = 128 * MB
AUDIO_CAP = 96 * MB
MAX_MEDIA_ELEMENTS = 2
PROGRAM_S = 720.0
PROCESS_BUDGET_MB = 1024


@dataclass(frozen=True)
class Form:
    minutes: float
    warmup_min: float
    #: the floor window (≥ one collection cycle; floor_points)
    floor_min: float
    #: the harness forces a full JS collection this often (s); the slope is
    #: judged on the footprint that survives it (post_gc_points)
    gc_every_s: float
    #: None: the least-squares slope of the retained footprint must be under
    #: the budget (the full form: 25 steady minutes resolve it to ±0.4
    #: MB/min). A number (MB): the short form's test instead — the retained
    #: footprint wanders by tens of MB with the caches' fill level (measured
    #: 249-340 MB over one steady 30-minute run), so 2.5 minutes cannot
    #: resolve a 1 MB/min slope (±5-12 MB/min measured); it fails a GROSS
    #: leak: the lowest of the last third of the steady points may exceed the
    #: lowest of the first third by at most budget x their distance + this
    #: (thirds_growth).
    noise_mb: float | None
    start_at_s: float
    check_every_s: float
    span_cap: int | None
    audio_cap: int | None


FORMS = {
    "full": Form(minutes=30, warmup_min=5, floor_min=2, gc_every_s=30, noise_mb=None, start_at_s=0,
                 check_every_s=60, span_cap=None, audio_cap=None),
    # starts 60 s before the end: the loop and the refill after it (measured:
    # the retained footprint settles about a minute after the loop) fall in
    # the warm-up, and the steady window is the looped program
    "short": Form(minutes=5, warmup_min=2.5, floor_min=1, gc_every_s=15, noise_mb=32,
                  start_at_s=PROGRAM_S - 60, check_every_s=30,
                  span_cap=SPAN_CAP // 2, audio_cap=AUDIO_CAP // 4),
}

LAYOUTS = {"twelve": twelve_minute_layout, "script": short_layout, "restart": restart_layout}


# ----------------------------------------------------------- slope maths

def slope_mb_per_min(points: list[tuple[float, float]]) -> float:
    """Least-squares slope of (minutes, MB) points."""
    n = len(points)
    if n < 2:
        raise ValueError("a slope needs at least two points")
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mx) ** 2 for p in points)
    if sxx == 0:
        raise ValueError("all points at one time")
    return sum((p[0] - mx) * (p[1] - my) for p in points) / sxx


def slope_standard_error(points: list[tuple[float, float]]) -> float:
    """Standard error of the least-squares slope of (minutes, MB) points."""
    n = len(points)
    if n < 3:
        raise ValueError("a slope's standard error needs at least three points")
    b = slope_mb_per_min(points)
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mx) ** 2 for p in points)
    rss = sum((p[1] - (my + b * (p[0] - mx))) ** 2 for p in points)
    return (rss / (n - 2) / sxx) ** 0.5


def thirds_growth(points: list[tuple[float, float]]) -> tuple[float, float]:
    """(lowest of the last third − lowest of the first third, minutes between
    the thirds' middles) of (minutes, MB) points.

    The lowest, because the post-collection footprint's wander is one-sided:
    what rides on top of the retained set is memory not yet collected or not
    yet returned by the allocator (measured in the short form: 87-105 MB
    between spikes to 150-162 MB). A leak raises the lowest point too."""
    n = len(points) // 3
    if n < 2:
        raise ValueError("thirds need at least six points")
    first, last = points[:n], points[-n:]
    return (min(v for _, v in last) - min(v for _, v in first),
            statistics.median(t for t, _ in last) - statistics.median(t for t, _ in first))


def floor_points(points: list[tuple[float, float]], window_min: float) -> list[tuple[float, float]]:
    """The lower envelope: the lowest sample of each `window_min` window.

    WebKit's footprint is a sawtooth — span fetches and FLAC decodes make
    garbage (tens of MB between collections, measured: 64 → 123 → 65 MB
    within a minute at a steady cache size) and the collector and the
    scavenger give it back in steps. A leak is memory that SURVIVES
    collection, so the budget is judged on the post-collection floor; the
    raw least-squares slope is reported next to it."""
    if not points:
        return []
    t0 = points[0][0]
    out: dict[int, tuple[float, float]] = {}
    for t, v in points:
        w = int((t - t0) // window_min)
        if w not in out or v < out[w][1]:
            out[w] = (t, v)
    # a trailing partial window (under half a window) has no floor yet
    last = max(out)
    if points[-1][0] - (t0 + last * window_min) < window_min / 2:
        out.pop(last)
    return [out[w] for w in sorted(out)]


def post_gc_points(rows: list[dict], gcs: list[float], started_wall: float, settle_s: float = 2.0,
                   key: str = "web") -> list[tuple[float, float]]:
    """(minutes since the soak started, MB): the lowest footprint between
    ``settle_s`` after each forced collection and the next one.

    Measured (WK, a page that fetched 128 MB then dropped every reference):
    the footprint stayed at 149 MB for as long as it was watched without a
    collection and fell to 21 MB within seconds of a forced one. So the raw
    footprint is mostly garbage JavaScriptCore has not collected yet; the
    post-collection footprint is what the page RETAINS — a leak is its slope."""
    out = []
    marks = sorted(g for g in gcs if g >= started_wall)
    for i, g in enumerate(marks):
        end = marks[i + 1] if i + 1 < len(marks) else float("inf")
        vals = [r[key] for r in rows if r.get(key) and g + settle_s <= r["wall"] < end]
        if vals:
            out.append(((g - started_wall) / 60.0, min(vals) / MB))
    return out


def footprint_series(rows: list[dict], started_wall: float, key: str = "web") -> list[tuple[float, float]]:
    """(minutes since the soak started, MB) for rows with a reading."""
    return [((r["wall"] - started_wall) / 60.0, r[key] / MB) for r in rows if r.get(key) and r["wall"] >= started_wall]


def test_slope_is_least_squares():
    assert slope_mb_per_min([(0, 100), (1, 101), (2, 102)]) == pytest.approx(1.0)
    # the floor of a GC sawtooth: flat when the retained set is flat, the
    # leak's slope when it is not
    saw = [(t / 30, 70 + (t % 30) * 2.0) for t in range(300)]            # 10 min, 1-min GC cycle
    assert slope_mb_per_min(floor_points(saw, 2.0)) == pytest.approx(0, abs=1e-9)
    leaky = [(t / 30, 70 + (t % 30) * 2.0 + 1.5 * t / 30) for t in range(300)]
    assert slope_mb_per_min(floor_points(leaky, 2.0)) == pytest.approx(1.5, abs=0.1)
    assert len(floor_points(saw, 2.0)) == 5
    assert slope_mb_per_min([(0, 50), (1, 50), (2, 50), (3, 50)]) == 0
    # noise around a flat line stays near zero; a leak of 2 MB/min shows
    flat = [(t / 6, 300 + (7 if t % 2 else -7)) for t in range(60)]
    assert abs(slope_mb_per_min(flat)) < 0.2
    leak = [(t / 6, 300 + 2 * t / 6 + (7 if t % 2 else -7)) for t in range(60)]
    assert slope_mb_per_min(leak) == pytest.approx(2.0, abs=0.2)
    with pytest.raises(ValueError):
        slope_mb_per_min([(1, 1)])
    # the standard error: 0 on a line, ~σ/sqrt(Σ(t-t̄)²) on noise
    assert slope_standard_error([(0, 1), (1, 2), (2, 3), (3, 4)]) == pytest.approx(0, abs=1e-12)
    se = slope_standard_error(flat)
    assert 0.05 < se < 1.0, se
    assert abs(slope_mb_per_min(leak) - 2.0) < 3 * slope_standard_error(leak)
    # thirds: one-sided spikes on a flat floor grow 0; a 20 MB/min leak over
    # 3 minutes shows 40 MB under the same spikes
    spiky = [(t / 4, 300 + (60 if t % 3 == 1 else 0)) for t in range(12)]
    g, apart = thirds_growth(spiky)
    assert g == 0 and apart == pytest.approx(2.0)
    g, _ = thirds_growth([(t / 4, v + 20 * t / 4) for t, (_, v) in enumerate(spiky)])
    assert g == pytest.approx(40.0)
    with pytest.raises(ValueError):
        thirds_growth([(0, 1), (1, 1), (2, 1), (3, 1), (4, 1)])


def test_post_gc_points_takes_the_lowest_sample_after_each_collection():
    MBf = float(MB)
    # a sample 1 s after each collection is low (still settling: ignored);
    # 2..9 s after it the level climbs from 200 + t
    rows = [{"wall": 100 + t, "web": (10 if t % 10 == 1 else 200 + t) * MBf} for t in range(0, 40)]
    # collections at 100, 110, 120, 130 (one before the start is ignored)
    pts = post_gc_points(rows, [100.0, 110.0, 120.0, 130.0, 50.0], started_wall=100.0)
    assert [round(t * 60) for t, _ in pts] == [0, 10, 20, 30]
    assert [v for _, v in pts] == [202.0, 212.0, 222.0, 232.0]
    assert slope_mb_per_min(pts) == pytest.approx(60.0)
    rows2 = [{"wall": 100 + t, "web": (1000 - t) * MBf} for t in range(0, 40)]
    pts2 = post_gc_points(rows2, [100.0, 120.0], started_wall=100.0)
    assert pts2 == [(0.0, 1000 - 19), (20 / 60, 1000 - 39)]
    assert post_gc_points(rows2, [], started_wall=100.0) == []


def test_process_footprint_reads_this_process_and_sees_it_grow():
    """proc_pid_rusage phys_footprint: positive, and 128 MB of written pages
    show up in it (the measurement the soak's slope rests on)."""
    if process_footprint(os.getpid()) is None:
        pytest.skip("proc_pid_rusage needs macOS")
    import mmap
    before = process_footprint(os.getpid())["footprint"]
    # An anonymous mmap, not a bytearray: in a long pytest process malloc can
    # hand a 128 MB bytearray back out of large regions it freed earlier but
    # still owns, and those pages are ALREADY in phys_footprint, so before ==
    # after exactly (the 0.8.0 release gate saw this in a shard that had run
    # 600 tests). A fresh mapping is new memory by construction.
    block = mmap.mmap(-1, 128 * MB)
    try:
        for i in range(0, 128 * MB, 4096):
            block[i] = 1
        after = process_footprint(os.getpid())
    finally:
        block.close()
    assert after["footprint"] - before >= 100 * MB, (before, after)
    assert after["peak"] >= after["footprint"]


@pytest.fixture(scope="module")
def bare_wk(tmp_path_factory):
    srv = PageServer({"pages": PAGES})
    harness = WKHarness(srv, tmp_path_factory.mktemp("wk-footprint"))
    yield harness
    harness.close()
    srv.close()


@pytest.mark.wk
def test_footprint_sampler_measures_the_webviews_own_webcontent_process(bare_wk):
    """The sampled pid is this WKWebView's WebContent process (not the
    harness child, not another page's), and 256 MB the page writes show up
    in its footprint."""
    run = bare_wk.run("pages/footprint_probe.html", {"mb": 256, "idle": 1500, "hold": 2500}, timeout=60,
                      sample_every=0.2)
    assert run.result["mb"] == 256
    rows = [r for r in run.footprints if r.get("web")]
    assert len(rows) >= 10, run.footprints
    pids = {r["web_pid"] for r in rows}
    assert len(pids) == 1 and run.pid not in pids, (pids, run.pid)
    # the executables, read by the sampler while the helpers were alive
    paths = run.helper_pids[0]["paths"]
    assert "com.apple.WebKit.WebContent" in (paths["web"] or ""), paths
    assert "com.apple.WebKit.GPU" in (paths["gpu"] or ""), paths
    assert f"helpers web={next(iter(pids))}" in run.stdout
    base = min(r["web"] for r in rows[:5])
    top = max(r["web"] for r in rows)
    assert top - base >= 200 * MB, (base / MB, top / MB)


@pytest.mark.wk
def test_forced_collection_takes_dropped_buffers_out_of_the_footprint(bare_wk):
    """The soak's slope rests on this: a page drops 256 MB of buffers and the
    footprint read after the harness's forced collection no longer holds
    them (without the collection it stays up: measured 149 MB of dropped
    fetch buffers for as long as it was watched)."""
    run = bare_wk.run("pages/footprint_probe.html", {"mb": 256, "idle": 1000, "hold": 2500, "after": 12000},
                      timeout=90, sample_every=0.2, gc_every=4)
    r = run.result
    assert len(run.gcs) >= 3, run.stdout
    rows = [x for x in run.footprints if x.get("web")]
    held = [x["web"] for x in rows if r["heldAt"] / 1000 - 1.5 <= x["wall"] <= r["droppedAt"] / 1000]
    after = post_gc_points(rows, [g for g in run.gcs if g > r["droppedAt"] / 1000 + 0.5], r["droppedAt"] / 1000)
    assert held and after, (r, [(round(x["wall"], 1), x["web"] // MB) for x in rows], run.gcs)
    assert max(held) / MB - min(v for _, v in after) >= 200, (max(held) / MB, after)


# ------------------------------------------------------------ environment

@dataclass
class Env:
    root: Path
    static: Path
    backend: LiveBackend
    pages: PageServer
    cache: Path
    sessions: dict[str, Session] = field(default_factory=dict)
    _wk: WKHarness | None = None

    def session(self, name: str) -> Session:
        if name not in self.sessions:
            clips = LAYOUTS[name]()
            masters = build_soak_masters(self.cache, sorted({c[0] for c in clips}))
            self.sessions[name] = make_session(self.backend, masters, self.root / "wd", clips)
        return self.sessions[name]

    def config(self, name: str, tag: str, **extra) -> str:
        s = self.session(name)
        cfg = {"sid": s.sid, "srcIds": s.src_ids, "canvas": ENGINE_CANVAS, **extra}
        (self.static / f"cfg-{tag}.json").write_text(json.dumps(cfg))
        return f"cfg-{tag}"

    def wk(self, scenario: str, cfg: str, timeout: float, sample_every: float | None = None,
           gc_every: float | None = None):
        if wk_unavailable_reason():
            pytest.skip(f"wk: {wk_unavailable_reason()}")
        if self._wk is None:
            self._wk = WKHarness(self.pages, self.root / "runs")
        url = f"{self.backend.base}/wk/soak.html?scenario={scenario}&cfg={cfg}"
        run = self._wk.run("pages/go.html", {"to": url}, timeout=timeout, sample_every=sample_every, gc_every=gc_every)
        assert "AppleWebKit" in run.result["ua"] and "Chrome" not in run.result["ua"], run.result["ua"]
        return run


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    exe = FRONTEND / "node_modules" / ".bin" / "esbuild"
    if not exe.exists():
        pytest.skip("frontend/node_modules not installed (no esbuild)")
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH")
    from video_ai_editor.api import pairing

    base = os.environ.get("VAI_SOAK_ROOT")
    root = Path(base) / f"run-{os.getpid()}" if base else tmp_path_factory.mktemp("wk-soak")
    static = root / "static"
    (static / "bundle").mkdir(parents=True, exist_ok=True)
    subprocess.run([str(exe), str(ENTRY), "--bundle", "--format=esm", "--target=safari16",
                    f"--outfile={static / 'bundle' / 'soak.js'}", "--log-level=warning"],
                   check=True, cwd=FRONTEND, capture_output=True, text=True)
    (static / "soak.html").write_text(PAGE)
    (root / "pages").mkdir(exist_ok=True)
    (root / "pages" / "go.html").write_text(GO_PAGE)
    fixture_cache = os.environ.get("VAI_WK_FIXTURE_CACHE")
    cache = Path(fixture_cache) / "soak" if fixture_cache else root / "masters"
    with pytest.MonkeyPatch.context() as mp:
        # never the user's settings.json; the server behaves as in client mode
        mp.setattr(pairing, "settings_path", lambda: root / "settings.json")
        mp.setattr(pairing, "_cache", None, raising=False)
        mp.setenv("VAI_PREVIEW_ENGINE", "client")
        backend = LiveBackend(root / "wd", static)
        pages = PageServer({"pages": root / "pages"})
        e = Env(root, static, backend, pages, cache)
        try:
            yield e
        finally:
            if e._wk is not None:
                e._wk.close()
            pages.close()
            backend.close()
            if not os.environ.get("VAI_SOAK_KEEP"):
                shutil.rmtree(root / "wd", ignore_errors=True)   # proxies: GBs


def _report(name: str, **kv) -> dict:
    row = {"test": name, "load_per_core": round(load_per_core(), 2), "quiet": machine_is_quiet(), **kv}
    print(json.dumps(row))
    return row


def _dump(env: Env, name: str, payload: dict) -> None:
    out = Path(os.environ["VAI_SOAK_DUMP"]) if os.environ.get("VAI_SOAK_DUMP") else env.root
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}.json").write_text(json.dumps(payload, indent=1))


# ----------------------------------------------------------------- P1-E1

@pytest.mark.wk
def test_p1_e1_element_budget_over_100_dispatch_edits(env):
    """100 edits through the app path (dispatch include=edl → controller),
    paused and playing, with seeks: at most 2 media elements exist at any
    time, and the engine creates exactly one (laneA)."""
    run = env.wk("edit_script", env.config("script", "script", edits=100, seed=7), timeout=600)
    r = run.result
    _report("P1-E1", created=r["created"], maxLive=r["maxLive"], engineCreated=r["engineCreated"], applied=r["applied"],
            byKind=r["byKind"], playingEdits=r["playingEdits"], refusals=r["refusals"])
    assert r["applied"] >= 90, r
    assert set(r["byKind"]) == {"split", "trim", "move", "delete", "undo"}, r["byKind"]
    assert r["playingEdits"] >= 20, r
    assert r["engineCreated"] == 1 and r["created"] <= MAX_MEDIA_ELEMENTS, r
    assert r["maxLive"] <= MAX_MEDIA_ELEMENTS, r
    assert r["judge"]["bad"] == 0, r["judge"]
    assert r["unhandled"]["quota"] == 0, r["unhandled"]


# ------------------------------------------- the loop: play again at the end

@pytest.mark.wk
def test_play_again_at_the_end_restarts_from_the_start(env):
    """Found by the soak: after playback stopped at the end, play (the
    store's Space with the playhead on the last frame, or play from 0) must
    run from frame 0. WebKit still presents frames of the OLD position
    after the element is re-seeked (and fires 'waiting' while the start is
    not buffered); those must not end the new run as 'end of program'.
    The span LRU is 16 MB here (≈ 25 s of 720p) so, as in the 12-minute
    soak, the start's spans are no longer cached when the end is reached.
    A round the environment touched (the page hidden, a pause WebKit made:
    another window over the 4 px harness window) is reported and replaced,
    up to 6 times; every other round must reach the end and restart.

    Final QA r4 (1 round in 10-40): the run's seek was issued while the start
    was not buffered, and WebKit completed it at the END of the append that
    followed (measured 'seeked' at t = 3.0 s with [0, 3] s just appended),
    then moved the element to every later append's end; frame 0 was drawn
    once and the run stood still, `buffering` up. The engine now issues the
    run's seek only once the start frame is appended (engineSeek.RunSeek), so
    every round defers it here (``deferred``), and a round the stall watchdog
    had to rescue (a stop after play, a restart) counts as failed too."""
    run = env.wk("end_restart", env.config("restart", "end-restart", edits=10, spanCacheBytes=16 * MB), timeout=900)
    r = run.result
    _report("end-restart", rounds=r["rounds"], clean=r["clean"], envRounds=r["envRounds"], failed=r["failed"])
    assert r["clean"] >= 8, ("the environment kept hiding the window", r["rounds"])
    clean = [x for x in r["rounds"] if x["env"] == 0]
    assert r["failed"] == 0, [x for x in clean if not x["ok"]]
    assert all(x["stallRestarts"] == 0 and x["stopsAfterPlay"] == 0 for x in clean), [x for x in clean if x["why"]]
    assert all(x["deferred"] >= 1 for x in clean), [x for x in clean if x["deferred"] < 1]
    assert r["judge"]["bad"] == 0, r["judge"]


#: The user's pause at the end before Space (past the engine's own-pause window).
PAUSE_AT_END_MS = 1500
#: The app's main-thread work right after play() (the store update, React's
#: render of the transport and the timeline), measured 10-30 ms.
BUSY_AFTER_PLAY_MS = 40


def _assert_restart_rounds(r: dict, want_clean: int) -> None:
    assert not r.get("fatal"), r.get("fatal")
    assert r["clean"] >= want_clean, ("the environment kept hiding the window", r["rounds"])
    clean = [x for x in r["rounds"] if x["env"] == 0]
    assert r["failed"] == 0, [x for x in clean if not x["ok"]]
    # the element played from the last frame ran off the media end ('ended',
    # then an 'element' pause): the round is a stop, never an env round
    assert all(x["endedPauses"] == 0 for x in clean), [x for x in clean if x["endedPauses"]]
    assert all(x["stallRestarts"] == 0 and x["stopsAfterPlay"] == 0 for x in clean), [x for x in clean if x["stopsAfterPlay"] or x["stallRestarts"]]
    assert all(x["deferred"] >= 1 for x in clean), [x for x in clean if x["deferred"] < 1]
    assert r["judge"]["bad"] == 0, r["judge"]


@pytest.mark.wk
def test_play_again_after_a_pause_at_the_end_restarts_from_the_start(env):
    """Final sweep 4: the same loop, but the round waits PAUSE_AT_END_MS at
    the end before playing again — a user's Space, not the soak's 20 ms —
    and the main thread is busy BUSY_AFTER_PLAY_MS right after play(), as
    the app's click handler is (the store update, React's render).
    Measured in WebKit (the app in Playwright WebKit, 1 round in 3 to 9 in
    10): with the start not buffered the run's seek is deferred (RunSeek)
    but the element was played at once from where it stood, the last frame,
    16.7 ms before the media duration; laneA's remove of the old window came
    after that (the main thread was busy), so the element reached the
    duration, fired 'ended' and paused itself; the deferred seek then landed
    on a paused element, and the pause — past the engine's own-pause window
    — was taken for an external one ('element'): playback stopped at once
    (or, inside the window, 1 s later at frame 0). The element must stay
    parked until its run seek is issued, and play then."""
    run = env.wk("end_restart", env.config("restart", "end-restart-paused", edits=10, spanCacheBytes=16 * MB,
                                           pauseBeforePlayMs=PAUSE_AT_END_MS, busyAfterPlayMs=BUSY_AFTER_PLAY_MS),
                 timeout=900)
    r = run.result
    _report("end-restart-paused", rounds=[{k: v for k, v in x.items() if k != "why"} for x in r["rounds"]],
            clean=r["clean"], envRounds=r["envRounds"], failed=r["failed"])
    _assert_restart_rounds(r, 8)


@pytest.mark.wk
@pytest.mark.slow
def test_play_start_first_frames_are_the_right_ones(env, request):
    """The soak's one open wrong frame (a full run drew k = 2 with source
    frame 1 in the first play of the page): 40 runs started from a paused
    frame (every other one from 0, the first the moment frame 0 is up, as
    the soak does), every frame drawn while playing judged by its bar. Not
    reproduced in 40 dedicated page loads, so opt-in (``-m slow`` or
    ``VAI_SOAK=play_start|all``): a repro tool and a guard, not a gate."""
    if not _opted_in(request, "play_start"):
        pytest.skip("opt-in: -m slow, or VAI_SOAK=play_start|all")
    run = env.wk("play_start", env.config("script", "play-start", edits=40, seed=11), timeout=600)
    r = run.result
    _report("play-start", bad=r["bad"], judged=r["judge"]["judged"], badSamples=r["judge"]["badSamples"],
            rounds=[x for x in r["rounds"] if x["bad"]])
    # (round 0 plays as the page comes up: its paused frame is not waited for)
    assert len(r["rounds"]) == 40 and all(x["shownMs"] >= 0 for x in r["rounds"][1:]), r["rounds"]
    assert r["judge"]["judged"] >= 40 * FPS // 2, r["judge"]
    assert r["bad"] == 0 and r["judge"]["bad"] == 0, r["judge"]["badSamples"]


# ---------------------------------------------- Playwright: the same logic

CHROMIUM = Path(os.environ.get(
    "VAI_CHROMIUM", str(Path.home() / "Library/Caches/ms-playwright/chromium_headless_shell-1243/"
                        "chrome-headless-shell-mac-arm64/chrome-headless-shell")))


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def pw(request, env):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    if request.param == "chromium" and not CHROMIUM.exists():
        pytest.skip(f"no headless Chromium at {CHROMIUM}")
    with sync_playwright() as p:
        b = (p.chromium.launch(executable_path=str(CHROMIUM), args=["--autoplay-policy=no-user-gesture-required"])
             if request.param == "chromium" else p.webkit.launch())
        b.engine_name = request.param
        try:
            yield b
        finally:
            b.close()


def test_playwright_edit_script_element_budget(pw, env):
    """The P1-E1 script's logic in Playwright Chromium and WebKit (the
    element count and the frames judged while playing; timing is WK's)."""
    cfg = env.config("script", f"script-pw-{pw.engine_name}", edits=100, seed=7,
                     staleMs=400 if pw.engine_name == "chromium" else 0)
    page = pw.new_page(viewport={"width": 800, "height": 600})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(f"{env.backend.base}/wk/soak.html?scenario=edit_script&cfg={cfg}&token=pw"
                  f"&mailbox={env.pages.url('').rstrip('/')}")
        page.wait_for_function("window.__result !== undefined", timeout=600_000)
        r = page.evaluate("window.__result")
        shots = os.environ.get("VAI_ENGINE_SHOTS")
        if shots:
            Path(shots).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(shots) / f"soak-edit-script-{pw.engine_name}.png"))
    finally:
        page.close()
    assert not r.get("fatal"), r.get("fatal")
    assert errors == [], errors
    _report(f"pw-{pw.engine_name}-P1-E1", created=r["created"], maxLive=r["maxLive"], applied=r["applied"])
    assert r["applied"] >= 90 and r["engineCreated"] == 1, r
    assert r["created"] <= MAX_MEDIA_ELEMENTS and r["maxLive"] <= MAX_MEDIA_ELEMENTS, r
    assert r["judge"]["bad"] == 0, r["judge"]


@pytest.mark.parametrize("pause_ms", [0, PAUSE_AT_END_MS], ids=["at_once", "after_a_pause"])
def test_playwright_play_again_at_the_end(pw, env, pause_ms):
    """The end-restart loop's logic in Playwright Chromium and WebKit, played
    again at once and after the user's pause with the app's busy main thread
    (final sweep 4: Playwright WebKit fires 'ended' on the element played
    from the last frame like WKWebView; Chromium stalls at the end of the
    buffered data instead — both must restart)."""
    cfg = env.config("restart", f"end-restart-pw-{pw.engine_name}-{pause_ms}", edits=6, spanCacheBytes=16 * MB,
                     pauseBeforePlayMs=pause_ms, busyAfterPlayMs=BUSY_AFTER_PLAY_MS if pause_ms else 0,
                     staleMs=400 if pw.engine_name == "chromium" else 0)
    page = pw.new_page(viewport={"width": 800, "height": 600})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(f"{env.backend.base}/wk/soak.html?scenario=end_restart&cfg={cfg}&token=pw"
                  f"&mailbox={env.pages.url('').rstrip('/')}")
        page.wait_for_function("window.__result !== undefined", timeout=600_000)
        r = page.evaluate("window.__result")
    finally:
        page.close()
    assert errors == [], errors
    _report(f"pw-{pw.engine_name}-end-restart-{pause_ms}", rounds=[{k: v for k, v in x.items() if k != "why"} for x in r["rounds"]],
            clean=r["clean"], envRounds=r["envRounds"], failed=r["failed"])
    _assert_restart_rounds(r, 5)


# ----------------------------------------------------------------- P1-M1

def _opted_in(request, form: str) -> bool:
    want = os.environ.get("VAI_SOAK", "").strip().lower()
    if want in (form, "all"):
        return True
    return "slow" in (request.config.getoption("markexpr") or "") and not want


@pytest.mark.wk
@pytest.mark.slow
@pytest.mark.parametrize("form", ["short", "full"])
def test_p1_m1_soak(env, form, request):
    if not _opted_in(request, form):
        pytest.skip(f"the {form} soak is opt-in: -m slow, or VAI_SOAK={form}|all")
    f = FORMS[form]
    minutes = float(os.environ.get("VAI_SOAK_MINUTES") or f.minutes)
    extra = {"minutes": minutes, "editEveryS": 10, "checkEveryS": f.check_every_s, "startAtS": f.start_at_s,
             "rowEveryS": 5, "seed": 20260926}
    if f.span_cap:
        extra["spanCacheBytes"] = f.span_cap
    if f.audio_cap:
        extra["audioCapBytes"] = f.audio_cap
    run = env.wk("soak", env.config("twelve", f"soak-{form}", **extra), timeout=minutes * 60 + 900, sample_every=1,
                 gc_every=f.gc_every_s)
    r = run.result
    started = r["startedAtEpochMs"] / 1000.0
    series = footprint_series(run.footprints, started)
    gpu = footprint_series(run.footprints, started, "gpu")
    steady = [p for p in series if p[0] >= f.warmup_min]
    raw_slope = slope_mb_per_min(steady)
    floor = floor_points(steady, f.floor_min)
    floor_slope = slope_mb_per_min(floor) if len(floor) >= 2 else None
    retained = post_gc_points(run.footprints, run.gcs, started)
    retained_steady = [p for p in retained if p[0] >= f.warmup_min]
    slope = slope_mb_per_min(retained_steady)
    slope_se = slope_standard_error(retained_steady)
    growth, growth_min = thirds_growth(retained_steady)
    net = footprint_series(run.footprints, started, "net")
    gpu_steady = [p for p in gpu if p[0] >= f.warmup_min]
    caps = r["caps"]
    peaks = r["peaks"]
    report = _report(
        f"P1-M1-{form}", minutes=minutes, slopeMBperMin=round(slope, 3), slopeSE=round(slope_se, 3),
        thirdsGrowthMB=round(growth, 1), thirdsApartMin=round(growth_min, 2),
        rawSlopeMBperMin=round(raw_slope, 3),
        floorSlopeMBperMin=round(floor_slope, 3) if floor_slope is not None else None, gcs=len(run.gcs),
        retainedMB={"warm": round(retained_steady[0][1], 1), "end": round(retained_steady[-1][1], 1),
                    "min": round(min(p[1] for p in retained_steady), 1),
                    "max": round(max(p[1] for p in retained_steady), 1)},
        retainedByMin=[[round(t, 1), round(v, 1)] for t, v in retained[::max(1, len(retained) // 40)]],
        floorMB=[[round(t, 2), round(v, 1)] for t, v in floor],
        gpuSlopeMBperMin=round(slope_mb_per_min(gpu_steady), 3) if len(gpu_steady) >= 2 else None,
        webMB={"start": round(series[0][1], 1), "warm": round(steady[0][1], 1), "end": round(series[-1][1], 1),
               "peak": round(max(p[1] for p in series), 1),
               "lifetimePeak": round(max(x.get("web_peak") or 0 for x in run.footprints) / MB, 1)},
        gpuMB={"end": round(gpu[-1][1], 1), "peak": round(max(p[1] for p in gpu), 1)} if gpu else None,
        netMB={"end": round(net[-1][1], 1), "peak": round(max(p[1] for p in net), 1)} if net else None,
        peaks={**peaks, "spanMB": round(peaks["spanBytes"] / MB, 1), "audioMB": round(peaks["audioBytes"] / MB, 1)},
        caps=caps, loops=r["loops"], resumes=r["resumes"], buffering=r["buffering"], extPauses=r["extPauses"],
        edits={k: r["edits"][k] for k in ("applied", "refused", "skipped", "byKind", "ms")}, refusals=r["refusals"],
        checks={k: r["checks"][k] for k in ("n", "ok", "p95", "max")},
        judge={k: r["judge"][k] for k in ("drawn", "judged", "skipped", "bad")},
        unhandled=r["unhandled"], mediaCreated=r["mediaCreated"], helperPids=run.helper_pids,
        laneStats=r["engine"]["lane"], store=r["engine"]["store"], audio=r["engine"]["audio"],
        handlerMs=r["engine"]["handlerMs"], divergence=r["engine"]["divergence"])
    _dump(env, f"soak-{form}", {"report": report, "footprints": run.footprints, "gcs": run.gcs, "rows": r["rows"],
                                  "badSamples": r["judge"]["badSamples"], "checkBad": r["checks"]["bad"]})

    # the run itself: it ran its length, looped, and edited in rotation
    assert r["elapsedS"] >= minutes * 60 - 5, r["elapsedS"]
    assert r["loops"] >= 1, r
    assert r["edits"]["applied"] >= 0.8 * minutes * 6, r["edits"]
    assert set(r["edits"]["byKind"]) == {"split", "trim", "move", "delete", "undo"}, r["edits"]
    # the WebContent process was never relaunched (a crash would reset memory)
    assert len({p["web"] for p in run.helper_pids}) == 1, run.helper_pids
    # §11.3: flat memory after warm-up
    # §11.3: flat memory after warm-up — the footprint that survives a full
    # collection (a forced one every gc_every_s), least squares
    assert len(run.gcs) >= minutes * 60 / f.gc_every_s * 0.8, len(run.gcs)
    assert len(retained_steady) >= 5, retained_steady
    if f.noise_mb is None:
        assert slope < SLOPE_BUDGET_MB_PER_MIN, (slope, slope_se, retained_steady)
    else:
        assert growth < SLOPE_BUDGET_MB_PER_MIN * growth_min + f.noise_mb, (growth, growth_min, retained_steady)
    # and the engine stays inside its process budget (§11.3: ≤ 1 GB)
    assert max(v for _, v in retained) <= PROCESS_BUDGET_MB, retained
    # laneA's window, the span LRU, the decoded-audio LRU
    assert peaks["bufSec"] <= BUFFERED_BUDGET_S, peaks
    assert caps["spanBytes"] == (f.span_cap or SPAN_CAP) and peaks["spanBytes"] <= caps["spanBytes"], (peaks, caps)
    assert caps["audioBytes"] == (f.audio_cap or AUDIO_CAP) and peaks["audioBytes"] <= caps["audioBytes"], (peaks, caps)
    # the LRUs really filled (so "within the cap" was tested at the cap)
    assert peaks["spanBytes"] >= 0.75 * caps["spanBytes"], (peaks, caps)
    assert peaks["audioBytes"] >= 0.75 * caps["audioBytes"], (peaks, caps)
    assert r["unhandled"]["quota"] == 0, r["unhandled"]
    # no wrong frame: every judged playing frame, every paused check
    assert r["judge"]["bad"] == 0, r["judge"]["badSamples"]
    assert r["judge"]["judged"] >= 0.5 * minutes * 60 * FPS, r["judge"]
    assert r["checks"]["n"] >= int(minutes * 60 / f.check_every_s) - 1, r["checks"]
    assert r["checks"]["ok"] == r["checks"]["n"], r["checks"]["bad"]
    assert r["engine"]["divergence"]["mismatched"] == 0, r["engine"]["divergence"]
    # P1-E1 over the whole soak
    assert peaks["media"] <= MAX_MEDIA_ELEMENTS and r["mediaCreated"] <= MAX_MEDIA_ELEMENTS, (peaks, r["mediaCreated"])


def test_sources_fit_the_bar():
    """Every soak source fits the 11-bit frame / 4-bit source bar."""
    assert len({s.src_id for s in SPECS}) == len(SPECS) == 12
    assert all(1 <= s.src_id <= 15 and s.frames <= 2048 for s in SPECS)
    total = sum(b - a for _, a, b in twelve_minute_layout())
    assert total == pytest.approx(PROGRAM_S)
