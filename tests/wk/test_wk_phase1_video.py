"""Phase 1b WK acceptance: the ENGINE's picture path in real WKWebView
(INSTANT_PREVIEW_SPEC §3.2, §3.4, §3.5, §4.3, §11.2, §13 Phase 1).

The page (tests/wk/pages/engine.html → frontend/src/lib/preview/testkit/
wkEnginePage.ts, bundled by esbuild from the real modules) builds the
PreviewEngine — laneA over MSE, the WebGL2 compositor, the presented clock,
the ProxyStore over the product's proxy route shape — from a fixture EDL, and
reads what the ENGINE CANVAS shows: the bar burned into every source frame,
mapped through geometry.ts to the canvas.

* P1-F1 paused exactness: 200 seeks, 200/200 exact.
* P1-F2 playback exactness: 0 bar mismatches; missing frames measured.
* P1-F5 last good frame: span route delayed 1 s.
* P1-E1 element budget: ≤ 2 media elements over 100 edits.
* external pause (a pause WebKit makes on its own), WebGL context loss.
* geometry parity: the engine canvas vs the SERVER's render of the same EDL,
  Y-PSNR per geometry feature (EXACT needs ≥ 35 dB).
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from .conftest import FRONTEND, PAGES
from .engine_server import EngineServer
from .harness import WKHarness
from .playback import max_drops, timing_budget
from .proxy_fixture import SourceSpec, write_proxy_dir

pytestmark = pytest.mark.wk

ENTRY = FRONTEND / "src" / "lib" / "preview" / "testkit" / "wkEnginePage.ts"
CANVAS = (640, 360)
FPS = 30
#: WebKit's own measurement budgets are timing; frame identity is exact.
PAUSED_P95_MS = 250

#: 3 sources of different sizes; Q is a 25 fps source in the 30 fps project.
SOURCES = (
    SourceSpec("P", 4, 1280, 720, 300, "testsrc2", Fraction(30)),
    SourceSpec("Q", 5, 960, 540, 250, "testsrc", Fraction(25)),
    SourceSpec("S", 6, 720, 1280, 150, "testsrc2", Fraction(30)),
)

# start, src, in, out, speed, reverse — 17 clips, 2 gaps, 0.5x, 2x, reverse
CLIPS = (
    (0.0, "P", 0.0, 1.5, None, False),
    (1.5, "Q", 2.0, 3.0, None, False),
    (2.5, "S", 0.5, 1.5, None, False),
    (3.5, "P", 3.0, 4.0, 2.0, False),
    (4.0, "Q", 5.0, 5.5, 0.5, False),
    # gap 5.0 .. 5.5
    (5.5, "P", 6.0, 7.0, None, True),
    (6.5, "S", 2.0, 3.0, None, False),
    (7.5, "Q", 0.0, 1.0, None, False),
    (8.5, "P", 8.0, 9.0, None, False),
    (9.5, "S", 3.5, 4.5, 2.0, False),
    (10.0, "Q", 7.0, 8.0, None, False),
    (11.0, "P", 1.0, 2.0, None, False),
    # gap 12.0 .. 12.5
    (12.5, "S", 0.0, 1.0, None, True),
    (13.5, "P", 4.0, 7.5, None, False),
    (17.0, "Q", 3.0, 6.0, None, False),
)
DURATION = 20.0


def _tb(rate: Fraction) -> list[int]:
    scale = rate.numerator
    while scale < 10000:
        scale *= 2
    return [1, scale]


def timeline_fixture() -> dict:
    clips = [{"id": f"c{i:02d}", "src": src, "in": a, "out": b, "start": st, "speed": sp, "reverse": rev,
              "transform": {"x": 0, "y": 0, "scale": 1, "rotation": 0, "opacity": 1}, "fit": "contain",
              "effects": []}
             for i, (st, src, a, b, sp, rev) in enumerate(CLIPS)]
    edl = {"duration": DURATION, "canvas": {"w": CANVAS[0], "h": CANVAS[1], "fps": FPS},
           "tracks": [{"id": "v1", "type": "video", "clips": clips, "transitions": []}]}
    sources = {s.name: {"key": s.name, "srcId": s.src_id,
                        "info": {"rate": [s.rate.numerator, s.rate.denominator], "tb": _tb(s.rate),
                                 "frames": s.frames, "start_ticks": 0, "w": s.w, "h": s.h}}
               for s in SOURCES}
    # P1-F5: Q's span 3 (frames 150..199) is served 1 s late
    return {"edl": edl, "sources": sources, "canvas": list(CANVAS), "delayed": {"src": "Q", "first": 150, "last": 199}}


def _esbuild(entry: Path, out: Path) -> None:
    exe = FRONTEND / "node_modules" / ".bin" / "esbuild"
    if not exe.exists():
        pytest.skip("frontend/node_modules not installed (npm install)")
    subprocess.run([str(exe), str(entry), "--bundle", "--format=esm", "--target=safari16", "--sourcemap=inline",
                    f"--outfile={out}", "--log-level=warning"],
                   check=True, cwd=FRONTEND, capture_output=True, text=True, encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def engine_bundle(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("wk-engine-kit")
    _esbuild(ENTRY, out / "wkEnginePage.js")
    return out


@pytest.fixture(scope="module")
def engine_media(tmp_path_factory) -> dict[str, Path]:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH")
    cache = os.environ.get("VAI_WK_FIXTURE_CACHE")
    root = Path(cache) / "engine-v1" if cache else tmp_path_factory.mktemp("wk-engine-media")
    out = {}
    for spec in SOURCES:
        d = root / spec.name
        if not (d / "index.json").is_file():
            write_proxy_dir(spec, root)
        out[spec.name] = d
    return out


@pytest.fixture(scope="module")
def engine_server(engine_bundle, engine_media, tmp_path_factory):
    fx = tmp_path_factory.mktemp("wk-engine-fixture")
    (fx / "timeline.json").write_text(json.dumps(timeline_fixture()))
    srv = EngineServer({"pages": PAGES, "testkit": engine_bundle, "fixture": fx}, proxies=engine_media)
    yield srv
    srv.close()


@pytest.fixture(scope="module")
def wk_engine(engine_server, tmp_path_factory):
    h = WKHarness(engine_server, tmp_path_factory.mktemp("wk-engine-runs"))
    yield h
    h.close()


def run(harness, scenario: str, timeout: float = 120, **query) -> dict:
    r = harness.run("pages/engine.html", {"scenario": scenario, **query}, timeout=timeout).result
    assert "AppleWebKit" in r["ua"] and "Chrome" not in r["ua"], r["ua"]
    return r


def _load() -> str:
    try:
        return "load %.1f %.1f %.1f" % os.getloadavg()
    except OSError:
        return "load ?"


# ------------------------------------------------------------------- P1-F1

def test_p1_f1_paused_exactness(wk_engine):
    """200 paused seeks over 17 clips / 16 cuts on 3 sources of 3 sizes, a
    25 fps source in a 30 fps project, 0.5x, 2x, reverse and two gaps: the
    engine canvas shows the program's (source, frame) every time."""
    r = run(wk_engine, "paused_exact")
    print(json.dumps({"F1": {k: r[k] for k in ("seeks", "cuts", "gaps", "p50", "p95", "max")}, "engine": r["engineStats"], "lane": r["lane"],
                      "store": r["store"], "load": _load()}))
    assert r["cuts"] >= 12
    assert r["gaps"] > 0
    assert r["seeks"] == 200
    assert r["bad"] == [], json.dumps(r["bad"][:12])
    assert r["p95"] < PAUSED_P95_MS * (3 if os.getloadavg()[0] > 8 else 1)


# ------------------------------------------------------------------- P1-F2

def test_p1_f2_playback_exactness(wk_engine):
    """The whole 20 s program played: every presented frame's bar is the
    program's frame (0 mismatches); missing frames are measured."""
    r = run(wk_engine, "playback", timeout=150)
    missing = len(r["missing"])
    print(json.dumps({"F2": {"frames": r["frames"], "missing": missing, "missing_pct": round(100 * missing / max(1, r["frames"] + missing), 3),
                             "held": r["held"], "waiting": r["waiting"], "maxUploadMs": r["maxUploadMs"],
                             "maxDrawMs": r["maxDrawMs"], "elapsedMs": r["elapsedMs"]}, "load": _load()}))
    assert r["mismatches"] == [], r["mismatches"][:10]
    assert r["monotonic"] is True
    assert r["firstK"] <= 2 and r["lastK"] >= r["total"] - 2
    assert r["held"] == 0
    assert r["sizeMismatches"] <= 3  # rVFC metadata lags a frame at a class switch (D1)
    assert missing <= max_drops(r["frames"])


def test_paused_edits_show_the_new_program_exactly(wk_engine):
    """40 committed edits (move, trim, delete, split, swap source) with the
    playhead parked on a random frame: after each, the canvas shows the NEW
    program's frame there, exactly (§4.1: re-append at the playhead, re-seek,
    upload after 'seeked'). Edit → pixel time is reported (spec §11.1 ≤ 80 ms
    p95 for the engine part)."""
    r = run(wk_engine, "paused_edits")
    print(json.dumps({"paused_edits": {k: r[k] for k in ("edits", "p50", "p95", "max")}, "load": _load()}))
    assert r["edits"] == 40
    assert r["bad"] == [], json.dumps(r["bad"][:8])


def test_rvfc_handler_budget_at_1080p(wk_engine):
    """§11.2: the rVFC handler (texture upload + uniforms + draw) at a
    1920x1080 canvas; p99 is reported against the 4 ms budget and bounded
    loosely here (timing under load)."""
    for _attempt in range(3):
        r = run(wk_engine, "playback", timeout=150, canvas="1920x1080", perf="1")
        print(json.dumps({"handler_1080p_ms": r["handler"], "frames": r["frames"], "missing": len(r["missing"]),
                          "load": _load(), "stopLog": r.get("stopLog")}))
        # a pause WebKit made (the window hidden or covered by another run on
        # this machine) ends the playback early: that run measures nothing
        if not r.get("externalPauses"):
            break
    assert r["handler"]["n"] >= 300
    assert r["handler"]["p50"] < 4
    # §11.2 says p99 <= 4 ms, and WK's performance.now() is whole ms: a run
    # that meets the spec reads 4 (review RD3). Load-scaled by the shared
    # helper, not a hard switch at a 1-minute load of 6.
    assert r["handler"]["p99"] <= timing_budget(4), r["handler"]


# ------------------------------------------------------------------- P1-F5

def test_p1_f5_last_good_frame(wk_engine, engine_server):
    """Q's span 3 is served 1 s late: seeking into it leaves the canvas
    untouched (no draw, same pixels) until the right frame arrives, the
    spinner shows from 80 ms, and the frame lands exact."""
    engine_server.set_delay("/api/proxies/Q/v/0003.bin", 1.0)
    try:
        r = run(wk_engine, "last_good_frame")
    finally:
        engine_server.set_delay("/api/proxies/Q/v/0003.bin", 0.0)
    print(json.dumps({"F5": {k: r[k] for k in ("target", "spinnerAt", "drawnAt", "traceLen")}, "store": r["store"]}))
    assert r["changedEarly"] == []
    assert r["spinnerAt"] is not None and 70 <= r["spinnerAt"] <= 400, r["spinnerAt"]
    assert r["drawnAt"] is not None and r["drawnAt"] >= 400, r["drawnAt"]
    assert r["drawnAt"] < 3000
    assert r["spinnerOffAfter"] is True
    assert r["bar"] == r["exp"]


def test_buffering_stops_sound_with_the_picture_and_resumes_both(wk_engine, engine_server):
    """§3.5: playing into P's span 3 (frames 180..239), served 2 s late: laneA's
    window ends there, WebKit stalls ('waiting'), the engine stops the sound
    at the frame the picture froze on, and when the span lands both restart
    together (a fresh anchor), every presented frame still exact."""
    engine_server.set_delay("/api/proxies/P/v/0003.bin", 2.0)
    try:
        r = run(wk_engine, "buffering", timeout=90)
    finally:
        engine_server.set_delay("/api/proxies/P/v/0003.bin", 0.0)
    print(json.dumps({"buffering": r["log"], "calls": r["calls"][:8], "reached": r["reached"]}))
    log = r["log"]
    # the stall at the delayed span (P frame 180 is output frame 465); a
    # short 'waiting' right at play start (window still filling) may precede it
    i = next(n for n, e in enumerate(log) if e["buffering"] and e["k"] >= 455)
    stall, resume = log[i], log[i + 1]
    assert resume["buffering"] is False and resume["at"] - stall["at"] >= 800, log
    stall_k = stall["k"]
    # (the page stamps its log to 1 ms, the sink to 0.01 ms: the stop can read 1 ms "early")
    stops = [c for c in r["calls"] if c["op"] == "stop" and c["at"] >= stall["at"] - 1]
    assert stops and stops[0]["k"] == stall_k, (stops, stall_k)
    # nothing restarts the sound while the picture is frozen
    starts_after = [c for c in r["calls"] if c["op"] == "start" and c["at"] > stall["at"]]
    assert starts_after and all(c["at"] >= resume["at"] for c in starts_after), (starts_after, log)
    R = r["R"]
    assert starts_after[0]["k"] >= stall_k
    assert abs(starts_after[0]["sample"] - (starts_after[0]["k"] + 0.5) * R["den"] / R["num"] * 48000) <= 48000 * 0.15
    assert r["mismatches"] == [], r["mismatches"][:8]
    assert r["reached"] > stall_k + 10


# ------------------------------------------------------------------- P1-E1

def test_p1_e1_element_budget(wk_engine):
    """100 structural edits, seeks and a play: the engine never has more
    than 2 media elements (it creates exactly one: laneA)."""
    r = run(wk_engine, "element_budget")
    assert r["created"] <= 2 and r["engineCreated"] == 1, r
    assert r["maxLive"] <= 2, r


# --------------------------------------------------- pauses the engine did not issue

def test_external_pause_stops_sound_with_the_picture_and_resumes_both(wk_engine):
    r = run(wk_engine, "external_pause")
    ev = r["events"]
    assert [e["cause"] for e in ev] == ["element", "hidden"], ev
    # (1) WebKit paused the element: sound stops at the same k, clock stops
    a = r["afterElement"]
    assert a["playing"] is False and a["status"] is False
    assert ev[0]["willResume"] is False
    assert a["stops"] and a["stops"][0] == ev[0]["k"] == a["presentedK"]
    assert a["starts"] == 0
    assert r["clockStill"] is True
    # (2) hidden: stopped in lockstep, nothing restarts while hidden
    h = r["whileHidden"]
    assert h["playing"] is False and h["videoPaused"] is True
    assert ev[1]["willResume"] is True
    assert h["stops"] and h["stops"][0]["k"] == h["kHidden"] == ev[1]["k"]
    assert h["startsWhileHidden"] == 0
    # visible again, the user never paused: picture and sound resume together
    s = r["resumed"]
    assert s["playing"] is True and s["videoPaused"] is False
    assert s["starts"], s
    R = r["R"]
    sample0 = round((s["kHidden"] + 0.5) * R["den"] / R["num"] * 48000)
    assert abs(s["starts"][0]["sample"] - sample0) <= 48000 * 0.1, (s["starts"][0], sample0)


def test_webgl_context_loss_shows_the_snapshot_then_restores(wk_engine):
    r = run(wk_engine, "context_loss")
    if r.get("skipped"):
        pytest.skip(r["skipped"])
    assert r["shown"] is True
    lost = r["lostState"]
    assert lost["lost"] is True and lost["snapshotVisible"] == "visible" and lost["glVisible"] == "hidden"
    assert lost["mode"] == "client"
    assert r["snapshotLitFraction"] > 0.2  # the last frame, not black
    assert r["redrawn"] is True and r["restored"] == 1
    assert r["bar"] == r["exp"]
    assert r["glVisibleAfter"] == "visible" and r["mode"] == "client"


# ------------------------------------------------------------ geometry parity

BENCH = Path(os.environ.get("VAI_BENCH_DIR", str(Path.home() / "Library/Caches/Video AI Editor/bench/8515fa4411c9")))
#: masters cut from real footage: a landscape, a portrait and an odd-size 25 fps
#: source, all square-pixel (the export ignores SAR in its fit — see notes)
GEO_MASTERS = {
    "land": ("0", "scale=1280:720", 30),
    "port": ("5", "crop=405:720:437:0,scale=720:1280", 30),
    "odd25": ("10", "scale=962:540", 25),
}
#: EXACT needs Y-PSNR ≥ 35 dB against the export (task brief; §8 idle check 36);
#: APPROX ≥ 28 dB (§8).
EXACT_DB = 35.0
APPROX_DB = 28.0


def _kf(*pts, interp="linear"):
    return {"keyframes": [list(p) for p in pts], "interp": interp}


#: group → [(feature, clip fields)]: 1 s clips back to back from 0
GEO_GROUPS: dict[str, list[tuple[str, dict]]] = {
    "static": [
        ("identity", {"src": "land"}),
        ("contain_portrait", {"src": "port"}),
        ("cover_portrait", {"src": "port", "fit": "cover"}),
        ("cover_pan", {"src": "port", "fit": "cover", "transform": {"x": 20.0, "y": -150.0}}),
        ("rotate", {"src": "land", "transform": {"rotation": 17.0}}),
        ("zoom_pan", {"src": "land", "transform": {"scale": 1.4, "x": 60.0, "y": -25.0}}),
        ("shrink_pan", {"src": "land", "transform": {"scale": 0.6, "x": -80.0, "y": 30.0}}),
        ("hflip", {"src": "land", "effects": [{"type": "hflip", "params": {}}]}),
        ("vflip_pan", {"src": "land", "transform": {"x": 40.0}, "effects": [{"type": "vflip", "params": {}}]}),
        ("opacity", {"src": "land", "transform": {"opacity": 0.6}}),
        ("fades", {"src": "land", "in": 1.0, "out": 2.0, "video_fade_in": 0.4, "video_fade_out": 0.4}),
        ("contain_odd_25fps", {"src": "odd25"}),
        ("combined", {"src": "port", "fit": "cover", "transform": {"scale": 1.2, "rotation": -8.0, "opacity": 0.85},
                      "effects": [{"type": "hflip", "params": {}}]}),
        # wave E gate (X2): fades at in > 0 off the source grid and retimed —
        # the retimed source frame's time on the in-anchored clock
        ("fades_offgrid", {"src": "land", "in": 0.52, "out": 1.52, "video_fade_in": 0.4,
                           "video_fade_out": 0.4}),
        ("fades_half_speed", {"src": "land", "in": 1.01, "out": 1.51, "speed": 0.5, "video_fade_in": 0.3,
                              "video_fade_out": 0.3}),
    ],
    "kf_transform": [("kf_transform", {"src": "land", "transform": {"scale": _kf((0, 1), (1, 1.6)), "x": _kf((0, 0), (1, 120))}})],
    "kf_rotation": [("kf_rotation", {"src": "land", "transform": {"rotation": _kf((0, 0), (1, 45))}})],
    "kf_opacity": [("kf_opacity", {"src": "land", "transform": {"opacity": _kf((0, 1), (1, 0.2))}})],
}
#: clip-local frames measured per feature
GEO_KS = {"fades": (5, 27), "fades_offgrid": (0, 5, 27), "fades_half_speed": (1, 5, 25),
          "kf_transform": (0, 12, 24), "kf_rotation": (6, 18, 28), "kf_opacity": (6, 20)}
DEFAULT_KS = (15, 25)


def _geo_edl(group: str, paths: dict[str, str]):
    from video_ai_editor.edl.schema import Canvas, Clip, Effect, Keyframe, empty_edl

    e = empty_edl(Canvas(w=CANVAS[0], h=CANVAS[1], fps=FPS))
    e.canvas.loudness_lufs = None
    v1 = e.get_track("v1")
    for i, (_feat, f) in enumerate(GEO_GROUPS[group]):
        c = Clip(src=paths[f["src"]], start=float(i), speed=f.get("speed"), id=f"g{i:02d}")
        c.in_ = f.get("in", 0.0)
        c.out = f.get("out", c.in_ + 1.0)
        c.fit = f.get("fit", "contain")
        for k, v in f.get("transform", {}).items():
            setattr(c.transform, k, Keyframe(**v) if isinstance(v, dict) else v)
        c.effects = [Effect(**x) for x in f.get("effects", [])]
        c.video_fade_in = f.get("video_fade_in", 0.0)
        c.video_fade_out = f.get("video_fade_out", 0.0)
        v1.clips.append(c)
    e.recompute_duration()
    return e


def _geo_ks(group: str) -> list[tuple[str, int]]:
    out = []
    for i, (feat, _f) in enumerate(GEO_GROUPS[group]):
        for j in GEO_KS.get(feat, DEFAULT_KS):
            out.append((feat, i * FPS + j))
    return out


def _decode_y(path: Path, w: int, h: int) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"],
                         check=True, capture_output=True).stdout
    per = w * h * 3 // 2
    return np.stack([np.frombuffer(raw[i * per:i * per + w * h], np.uint8).reshape(h, w) for i in range(len(raw) // per)])


def _psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = float(np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2))
    return 99.0 if mse == 0 else 10 * np.log10(255.0 ** 2 / mse)


@pytest.fixture(scope="module")
def geo_env(engine_bundle, tmp_path_factory):
    """Masters cut from real footage, REAL proxies (ingest ProxyManager), the
    export's frames of every geometry group, and a server for the page."""
    src = BENCH / "broll_20s.mp4"
    if not src.is_file() or shutil.which("ffmpeg") is None:
        pytest.skip(f"no bench footage at {src}")
    proxy_queue = pytest.importorskip("video_ai_editor.ingest.proxy_queue")
    P = pytest.importorskip("video_ai_editor.ingest.proxy")
    from video_ai_editor import storage as _storage
    from video_ai_editor.render import compositor
    from video_ai_editor.render.frame_map import SourceInfo as FmSource

    root = tmp_path_factory.mktemp("wk-geo")
    paths = {}
    for name, (ss, vf, rate) in GEO_MASTERS.items():
        out = root / "masters" / f"{name}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", ss, "-i", str(src), "-t", "3",
                        "-vf", f"{vf},setsar=1,fps={rate},format=yuv420p", "-an", "-c:v", "libx264", "-crf", "16",
                        "-preset", "veryfast", str(out)], check=True, capture_output=True)
        paths[name] = str(out)
    old = _storage.WORKDIR
    _storage.WORKDIR = root / "wd"
    _storage.WORKDIR.mkdir()
    manager = proxy_queue.ProxyManager()
    try:
        keys = {n: manager.ensure(p) for n, p in paths.items()}
        assert manager.wait_idle(240), "proxy builds did not finish"
        dirs = {keys[n]: P.proxy_dir(keys[n]) for n in paths}
        sources = {paths[n]: {"key": keys[n], "info": FmSource.from_proxy(P.load_source(keys[n])).to_json()} for n in paths}
        groups, server_y = [], {}
        for g in GEO_GROUPS:
            edl = _geo_edl(g, paths)
            sess = root / "render" / g
            sess.mkdir(parents=True)
            out = compositor._render(edl, sess / "out.mp4", height=CANVAS[1], fps=FPS, preview=False,
                                     cache_dir=sess / "cache", chunked=False)
            ys = _decode_y(out, *CANVAS)
            ks = _geo_ks(g)
            for feat, k in ks:
                server_y[(g, k)] = (feat, ys[k])
            d = edl.model_dump(by_alias=True, mode="json")
            d["tracks"] = [t for t in d["tracks"] if t["id"] == "v1"]
            groups.append({"name": g, "edl": d, "ks": [k for _f, k in ks]})
    finally:
        manager.shutdown()
        manager.wait_idle(10)
        _storage.WORKDIR = old
    fx = root / "fixture"
    fx.mkdir()
    (fx / "timeline.json").write_text(json.dumps({"edl": groups[0]["edl"], "sources": sources, "canvas": list(CANVAS),
                                                  "groups": groups}))
    srv = EngineServer({"pages": PAGES, "testkit": engine_bundle, "fixture": fx}, proxies=dirs)
    try:
        yield srv, server_y
    finally:
        srv.close()


def geometry_psnr(result: dict, server_y: dict) -> dict[str, list[float]]:
    """Y-PSNR of each posted engine frame against the export's frame."""
    per: dict[str, list[float]] = {}
    for f in result["frames"]:
        assert f["ok"] and f["y"], f"{f['group']} k={f['k']} was not drawn"
        y = np.frombuffer(base64.b64decode(f["y"]), np.uint8).reshape(CANVAS[1], CANVAS[0])
        feat, ref = server_y[(f["group"], f["k"])]
        per.setdefault(feat, []).append(round(_psnr(y, ref), 2))
    return per


#: the frame the page shows last (a screenshot of every pass at once)
SHOWCASE = ("static", 12 * FPS + 15)


@pytest.fixture(scope="module")
def geometry_parity(geo_env, tmp_path_factory):
    srv, server_y = geo_env
    harness = WKHarness(srv, tmp_path_factory.mktemp("wk-geo-runs"))
    try:
        results = {}
        for mip in ("1", "0"):
            r = run(harness, "geometry", timeout=180, mipmaps=mip)
            results[mip] = geometry_psnr(r, server_y)
        yield results
    finally:
        harness.close()


#: support.ts's classification of each geometry feature (P1): features that
#: measure below EXACT_DB are APPROX there ('geometry:<feature>').
GEO_APPROX: set[str] = set()


def test_geometry_parity_with_the_server_render(geometry_parity):
    """The engine canvas vs the export's own frames of the same EDL, per
    geometry feature (Y-PSNR, BT.709 limited luma, full 640x360 frame)."""
    report = {mip: {f: min(v) for f, v in per.items()} for mip, per in geometry_parity.items()}
    print(json.dumps({"geometry_psnr_db": report, "load": _load()}))
    per = report["1"]
    assert set(per) == {f for g in GEO_GROUPS.values() for f, _ in g}
    for feat, db in per.items():
        floor = APPROX_DB if feat in GEO_APPROX else EXACT_DB
        assert db >= floor, f"{feat}: {db} dB < {floor} dB ({'APPROX' if feat in GEO_APPROX else 'EXACT'})"



def test_texture_upload_is_the_visible_frame_size(wk_engine):
    """texImage2D(video) in WebKit uploads each proxy at its own size (the
    index's w×h), including a 540-line one (not a macroblock multiple): the
    compositor maps uv 0..1 onto the picture with no coded-size padding."""
    probes = run(wk_engine, "texprobe")["probes"]
    sizes = {s.name: [s.w, s.h] for s in SOURCES}
    assert {p["src"] for p in probes} == set(sizes)
    for p in probes:
        assert p["texture"] == sizes[p["src"]] == p["element"], p


@pytest.mark.parametrize("self_pause", ["1", "0"], ids=["engine-pauses-on-hidden", "webkit-pauses-the-element"])
def test_real_window_hide_pauses_both_and_resumes_both(wk_engine, self_pause):
    """The harness orders the WK window OUT mid-playback (what a Space switch
    or minimise does to the app): picture and sound stop together at one k,
    nothing plays while hidden, and when the window is back both resume from
    a fresh anchor (the user never pressed pause). With the engine's own
    hidden-pause disabled, WebKit's pause of the muted element is what the
    engine must notice."""
    # Other WK runs on this machine can land a window on the same 4 px slot
    # (slot = pid % 256) and occlude ours, which hides the page: BEFORE our
    # own hide request (the engine then was not playing when we hid it, or
    # already paused on its own) or as an extra hide/show cycle after it.
    # Either is the environment, not the engine: such a run is retried (up
    # to 4 runs); the assertions below judge only a clean run.
    for attempt in range(4):
        r = run(wk_engine, "real_hide", timeout=60, selfPause=self_pause)
        clean = not r["envBeforeHide"] and sum(1 for e in r["log"] if e["ev"] == "visibility") == 2
        print(json.dumps({"real_hide": r["log"], "attempt": attempt, "clean": clean, "envBeforeHide": r["envBeforeHide"],
                          "whileHidden": r["whileHidden"], "afterShow": r["afterShow"]}))
        if clean:
            break
    assert r["playingAtHide"] is True, ("never a clean run: the environment kept hiding the window", r["log"])
    h = r["whileHidden"]
    assert h["visibility"] == "hidden", r["log"]
    assert h["playing"] is False and h["videoPaused"] is True
    stops = [c for c in h["calls"] if c["op"] == "stop"]
    assert stops and all(c["op"] != "start" for c in h["calls"]), h["calls"]
    ext = [e for e in r["log"] if e["ev"] == "pause-external"]
    assert len(ext) == 1 and ext[0]["willResume"] is True, r["log"]
    assert stops[0]["k"] == ext[0]["k"] == h["presentedK"]  # sound stopped on the frame the picture froze on
    s = r["afterShow"]
    assert s["visibility"] == "visible"
    assert s["playing"] is True and s["videoPaused"] is False, r["log"]
    starts = [c for c in s["calls"] if c["op"] == "start"]
    assert starts, s["calls"]
    R = r["R"]
    assert abs(starts[0]["sample"] - round((h["presentedK"] + 0.5) * R["den"] / R["num"] * 48000)) <= 4800
    assert s["presentedK"] > h["presentedK"]  # the picture moves again
