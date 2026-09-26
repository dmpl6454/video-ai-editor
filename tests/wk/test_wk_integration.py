"""Instant preview 1c INTEGRATION, end to end (INSTANT_PREVIEW_SPEC §3.5,
§4.1-4.4, §7, §13: P1-F3, P1-F4, P1-S1, P1-A3, P1-B1, and the external-pause
requirement found in milestone 1).

Real WKWebView (marker ``wk``), a REAL backend (the FastAPI app under uvicorn
in a thread, a scratch WORKDIR, bar-coded ffmpeg masters, the app's proxy
manager, /frame_map, preview renders and bakes), and the app's client-mode
code: every edit is ``POST /dispatch?include=edl`` followed by
``PreviewController.applyTimeline`` — exactly what the store does — and the
picture is read back from the ENGINE CANVAS, the sound from the real
AudioEngine's output (an AudioWorklet tap). The page is
``frontend/src/lib/preview/testkit/wkIntegrationPage.ts``, bundled per run.

The same page runs under Playwright Chromium and Playwright WebKit for the
engine LOGIC (identity assertions only; the timing and the sound are
WK-normative).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from .harness import PageServer, WKHarness, wk_unavailable_reason
from .integration_fixture import FLASH_EVERY, Session, build_masters, make_session
from .live_backend import GO_PAGE, LiveBackend
from .playback import load_per_core, machine_is_quiet, timing_budget

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
ENTRY = FRONTEND / "src" / "lib" / "preview" / "testkit" / "wkIntegrationPage.ts"
TAP = Path(__file__).resolve().parent / "pages" / "audio_tap.js"
CHROMIUM = Path(os.environ.get(
    "VAI_CHROMIUM", str(Path.home() / "Library/Caches/ms-playwright/chromium_headless_shell-1243/"
                        "chrome-headless-shell-mac-arm64/chrome-headless-shell")))
ENGINE_CANVAS = [640, 360]
PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>integration</title></head>
<body style="margin:0;background:#000"><script type="module" src="/wk/bundle/integ.js"></script></body></html>"""

# §11.1 paused edit-to-visible budgets (p95, ms)
BUDGET = {"split": 60, "trim": 80, "move": 80, "delete": 80, "ripple": 80, "undo": 80}


def _edits_layout() -> list[tuple[str, float, float]]:
    out = []
    for i in range(24):
        name = "ABC"[i % 3]
        a = round((i * 0.37) % 5.5, 3)
        out.append((name, a, round(a + 0.8 + (i % 4) * 0.1, 3)))
    return out


def _sync_layout() -> list[tuple[str, float, float]]:
    # 21 clips of the flash master, each starting ON a flash frame: 20 cuts
    # land on flashes (and clicks); durations 15/20/25/30 frames
    out = []
    for i in range(21):
        m = (i * 7) % 36
        a = m * FLASH_EVERY / 30
        n = (15, 20, 25, 30)[i % 4]
        out.append(("F", round(a, 6), round(a + n / 30, 6)))
    return out


LAYOUTS = {
    "edits": ((1280, 720, 30), _edits_layout()),
    # P1-F4 ripples clips away while playing: its own copy of the layout
    "playing": ((1280, 720, 30), _edits_layout()),
    "structural": ((1280, 720, 30), [("A", 0.2, 1.6), ("B", 0.5, 2.0), ("C", 0.3, 1.5)]),
    "sync": ((1280, 720, 30), _sync_layout()),
    "bake": ((960, 540, 30), [("S", 0.0, 1.5), ("S", 2.0, 3.5), ("S", 4.0, 5.0)]),
}


@dataclass
class Env:
    root: Path
    static: Path
    backend: LiveBackend
    pages: PageServer
    masters: dict[str, Path]
    sessions: dict[str, Session] = field(default_factory=dict)
    _wk: WKHarness | None = None

    def session(self, name: str) -> Session:
        if name not in self.sessions:
            canvas, clips = LAYOUTS[name]
            s = make_session(self.backend, self.masters, self.root / "wd", canvas, clips)
            if name == "bake":
                ans = self.backend.call("POST", f"/api/sessions/{s.sid}/dispatch",
                                        {"tool": "color_grade", "args": {"clip_id": s.clip_ids[1], "brightness": 0.2}})
                assert ans["result"], ans
            self.sessions[name] = s
        return self.sessions[name]

    def config(self, name: str, **extra) -> str:
        s = self.session(name)
        cfg = {"sid": s.sid, "srcIds": s.src_ids, "canvas": ENGINE_CANVAS, "flashEvery": FLASH_EVERY, **extra}
        tag = f"cfg-{name}-{abs(hash(json.dumps(extra, sort_keys=True))) % 10**8}"
        (self.static / f"{tag}.json").write_text(json.dumps(cfg))
        return tag

    def url(self, scenario: str, cfg: str) -> str:
        return f"{self.backend.base}/wk/integ.html?scenario={scenario}&cfg={cfg}"

    def wk(self, scenario: str, cfg: str, timeout: float = 300) -> dict:
        if wk_unavailable_reason():
            pytest.skip(f"wk: {wk_unavailable_reason()}")
        if self._wk is None:
            self._wk = WKHarness(self.pages, self.root / "runs")
        run = self._wk.run("pages/go.html", {"to": self.url(scenario, cfg)}, timeout=timeout)
        r = run.result
        dump = os.environ.get("VAI_INTEG_DUMP")
        if dump:
            Path(dump).mkdir(parents=True, exist_ok=True)
            (Path(dump) / f"{scenario}.json").write_text(json.dumps(r))
        assert "AppleWebKit" in r["ua"] and "Chrome" not in r["ua"], r["ua"]
        return r


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    exe = FRONTEND / "node_modules" / ".bin" / "esbuild"
    if not exe.exists():
        pytest.skip("frontend/node_modules not installed (no esbuild)")
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH")
    from video_ai_editor.api import pairing

    root = tmp_path_factory.mktemp("wk-integ")
    static = root / "static"
    (static / "bundle").mkdir(parents=True)
    subprocess.run([str(exe), str(ENTRY), "--bundle", "--format=esm", "--target=safari16",
                    f"--outfile={static / 'bundle' / 'integ.js'}", "--log-level=warning"],
                   check=True, cwd=FRONTEND, capture_output=True, text=True)
    (static / "integ.html").write_text(PAGE)
    shutil.copyfile(TAP, static / "audio_tap.js")
    (root / "pages").mkdir()
    (root / "pages" / "go.html").write_text(GO_PAGE)
    cache = Path(os.environ["VAI_WK_FIXTURE_CACHE"]) / "integration" if os.environ.get("VAI_WK_FIXTURE_CACHE") \
        else root / "masters"
    masters = build_masters(cache)
    with pytest.MonkeyPatch.context() as mp:
        # never the user's settings.json; the server behaves as in client mode
        mp.setattr(pairing, "settings_path", lambda: root / "settings.json")
        mp.setattr(pairing, "_cache", None, raising=False)
        mp.setenv("VAI_PREVIEW_ENGINE", "client")
        backend = LiveBackend(root / "wd", static)
        pages = PageServer({"pages": root / "pages"})
        e = Env(root, static, backend, pages, masters)
        try:
            yield e
        finally:
            if e._wk is not None:
                e._wk.close()
            pages.close()
            backend.close()


def _p95(xs: list[float]) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(0.95 * (len(s) - 1) + 0.5))] if s else 0.0


def _report(name: str, **kv) -> None:
    print(json.dumps({"test": name, "load_per_core": round(load_per_core(), 2), "quiet": machine_is_quiet(), **kv}))


# ------------------------------------------------------------------ P1-F3

@pytest.mark.wk
def test_p1_f3_paused_edit_latency(env):
    """50 each of split, trim, move, delete, ripple (+ an undo after each)
    while paused: dispatch start → the correct bar on the engine canvas."""
    r = env.wk("edit_latency", env.config("edits", edits=50, seed=11), timeout=900)
    by = r["by"]
    _report("P1-F3", by=by, refused=r["refused"], refusals=r["refusals"])
    assert r["bad"] == [], r["bad"]
    for kind, spec in BUDGET.items():
        b = by[kind]
        assert b["n"] >= 50 and b["failed"] == 0, (kind, b)
        assert b["p95"] <= timing_budget(spec), (kind, b["p95"], spec, load_per_core())
    # the edits really changed the frame on screen (a split does only where
    # the export's own fps=near phase restarts at the cut: mixed-rate sources)
    for kind in ("trim", "move", "delete", "ripple", "undo"):
        assert by[kind]["changed"] >= 25, (kind, by[kind])


# ------------------------------------------------------------------ P1-F4

@pytest.mark.wk
def test_p1_f4_edit_while_playing(env):
    """No frame at or after presentedK + 5 (at the edit's arrival) shows the
    old program, whether the edit lands 30 frames ahead (the spec's case) or
    under the playhead itself; no 'waiting' stall."""
    r = env.wk("edit_while_playing", env.config("playing", edits=10, seed=5), timeout=300)
    res = r["results"]
    _report("P1-F4", edits=[{k: x[k] for k in ("variant", "peAnswer", "region", "answerMs", "judged", "changedJudged")}
                            for x in res], waiting=r["waiting"])
    assert len(res) >= 6, res
    assert {x["variant"] for x in res} == {"ahead30", "underPlayhead"}
    for x in res:
        assert x["bad"] == [], x
        assert x["judged"] > 10 and x["changedJudged"] > 0, x
    assert r["waiting"] == 0, r


# ------------------------------------------------------------------ P1-S1

@pytest.mark.wk
def test_p1_s1_structural_agreement_through_the_engine(env):
    """200 committed edits through the app path; after each, the ENGINE's
    program map (built from the dispatch answer, with the controller's own
    source table) equals the server's /frame_map of that render hash."""
    r = env.wk("structural", env.config("structural", edits=200, seed=20260926), timeout=900)
    _report("P1-S1", outcomes=r["outcomes"], byTool=r["byTool"], checked=r["checked"])
    assert r["applied"] == 200, r
    assert r["noVerdictCount"] == 0, r["noVerdict"]
    assert r["mismatches"] == [], json.dumps(r["mismatches"])[:3000]
    assert r["outcomes"] == {"match": 200}, r["outcomes"]
    for tool in ("add_clip", "split_at", "trim_clip", "move_clip", "ripple_delete", "undo", "set_speed"):
        assert r["byTool"].get(tool, 0) > 0, (tool, r["byTool"])
    assert r["clipsMax"] >= 6 and r["framesMax"] >= 150, r


# ------------------------------------------------------------------ P1-A3

def _offsets(flashes: list[dict], after: float = 0.0) -> list[float]:
    return [f["offsetMs"] for f in flashes if f["offsetMs"] is not None and f["at"] >= after]


@pytest.mark.wk
def test_p1_a3_live_av_sync(env):
    """White flash frames against clicks, through the engine's picture and
    the real AudioEngine: |offset| p95 ≤ 10 ms, max ≤ 20 ms; ≤ 1 frame within
    100 ms of play start; every click sounds where a flash is on screen."""
    r = env.wk("av_sync", env.config("sync"), timeout=180)
    offs = _offsets(r["flashes"])
    ab = [abs(x) for x in offs]
    first = [f for f in r["flashes"] if f["offsetMs"] is not None and f["at"] - r["playStartAt"] <= 100]
    kinds = {}
    for c in r["clicks"]:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    _report("P1-A3", n=len(offs), p95=_p95(ab), max=max(ab, default=None), mean=sum(offs) / max(1, len(offs)),
            cuts=r["cutFlashes"], clicks=kinds, firstFrameErrorMs=r["audioSync"].get("firstFrameErrorMs"),
            outputLatency=r["outputLatency"])
    assert r["cutFlashes"] >= 18, r["cutFlashes"]
    assert len(offs) >= 30, offs
    # the picture: flashes drawn white, the rest dark
    assert min(f["level"] for f in r["flashes"] if f["level"] is not None) > 150
    assert r["maxNonFlashLevel"] < 100
    assert kinds.get("orphan", 0) == 0 and kinds.get("frozen", 0) == 0, r["clicks"]
    assert _p95(ab) <= timing_budget(10), (_p95(ab), load_per_core())
    assert max(ab) <= timing_budget(20), (max(ab), load_per_core())
    for f in first:
        assert abs(f["offsetMs"]) <= 1000 / 30, first


# ------------------------------------------------------------------ P1-B1

@pytest.mark.wk
def test_p1_b1_bake_splice_through_the_engine(env):
    r = env.wk("bake_splice", env.config("bake"), timeout=300)
    _check_bake(r)


def _check_bake(r: dict) -> None:
    (k0, k1), = r["baked"]
    _report("P1-B1", baked=[k0, k1], renderMs=round(r["renderMs"]), spliceMs=round(r["spliceMs"]),
            bakeState=r["bakeState"])
    assert 0 < k0 < k1 < r["total"]
    assert r["previewHash"] == r["renderHash"]
    exp = r["expected"]
    assert [x["bar"] for x in r["before"]] == exp        # RAW client frames, frame-exact
    assert [x["bar"] for x in r["after"]] == exp         # bake frame k = output frame k (R13)
    assert not any(x["baked"] for x in r["before"])
    assert [x["baked"] for x in r["after"]] == [k0 <= x["k"] < k1 for x in r["after"]]
    lift = [a["level"] - b["level"] for a, b in zip(r["after"], r["before"])]
    assert min(lift[k0:k1]) > 20, lift[k0:k1]           # the server's grade, inside only
    assert max(abs(x) for x in lift[:k0] + lift[k1:]) <= 2
    assert r["bakeState"]["waiting"] == 0
    assert r["srcUnchanged"] is True and r["emptied"] == 0 and r["loadstarts"] == 0


# ------------------------------------------------------- external pauses

@pytest.mark.wk
def test_webkit_pauses_on_hide_and_occlusion_stop_both_and_resume_both(env):
    """The harness orders the window out (Space switch, minimise) and covers
    it with another window (occlusion); WebKit pauses the muted <video> on
    its own. Picture and sound stop together, the app shows paused, nothing
    plays while away; on return (the user never paused) both resume from a
    fresh anchor with no drift; an interrupted AudioContext stops the
    picture with it."""
    r = env.wk("external_pause", env.config("sync"), timeout=240)
    kinds: dict[str, int] = {}
    for c in r["clicks"]:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    for ph in r["phases"]:
        after = _offsets(r["flashes"], ph["resumedAt"] + 150)
        ph["afterP95"] = _p95([abs(x) for x in after])
        ph["afterMax"] = max((abs(x) for x in after), default=None)
        ph["afterN"] = len(after)
    _report("external-pause", phases=[{k: p[k] for k in ("off", "stopMs", "hiddenState", "after", "afterP95", "afterMax",
                                                          "afterN")} for p in r["phases"]],
            clicks=kinds, interrupted=r["interrupted"], log=r["log"])
    assert len(r["phases"]) == 2
    for ph in r["phases"]:
        h = ph["hiddenState"]
        assert h["visibility"] == "hidden", (ph["off"], r["log"])
        assert h["playing"] is False and h["storePlaying"] is False, (ph["off"], h)
        assert h["videoPaused"] is True and h["audioRunning"] is False, (ph["off"], h)
        assert h["drawsWhileOff"] == 0, (ph["off"], h)
        a = ph["after"]
        assert a["visibility"] == "visible" and a["playing"] is True and a["storePlaying"] is True, (ph["off"], a)
        assert ph["afterN"] >= 3, ph
        assert ph["afterP95"] <= timing_budget(10), ph
        assert ph["afterMax"] <= timing_budget(20), ph
    ext = [e for e in r["log"] if e["ev"] == "pause-external"]
    hidden = [e for e in ext if e["cause"] == "hidden"]
    # ours (≥ 2: something else on a busy machine may hide the window too)
    assert len(hidden) >= 2 and all(e["willResume"] for e in hidden), r["log"]
    # the interrupted context: an 'element' pause that stays paused
    assert [e["willResume"] for e in ext if e["cause"] == "element"] == [False], r["log"]
    # sound never ran on without its picture
    assert kinds.get("frozen", 0) == 0 and kinds.get("orphan", 0) == 0, r["clicks"]
    i = r["interrupted"]
    assert i["playing"] is False and i["storePlaying"] is False and i["audioRunning"] is False, i
    assert abs(i["presentedK"] - i["kInt"]) <= 3, i


# ---------------------------------------------- Playwright: engine logic

def _pw_reason(name: str) -> str | None:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return "playwright not installed"
    if name == "chromium" and not CHROMIUM.exists():
        return f"no headless Chromium at {CHROMIUM}"
    return None


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def pw(request, env):
    reason = _pw_reason(request.param)
    if reason:
        pytest.skip(reason)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = (p.chromium.launch(executable_path=str(CHROMIUM), args=["--autoplay-policy=no-user-gesture-required"])
             if request.param == "chromium" else p.webkit.launch())
        b.engine_name = request.param
        try:
            yield b
        finally:
            b.close()


def _pw_run(browser, env: Env, scenario: str, cfg: str, timeout: float = 300) -> dict:
    page = browser.new_page(viewport={"width": 800, "height": 600})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(env.url(scenario, cfg) + f"&token=pw&mailbox={env.pages.url('').rstrip('/')}")
        page.wait_for_function("window.__result !== undefined", timeout=timeout * 1000)
        r = page.evaluate("window.__result")
        shots = os.environ.get("VAI_ENGINE_SHOTS")
        if shots:
            Path(shots).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(shots) / f"integ-{scenario}-{browser.engine_name}.png"))
    finally:
        page.close()
    assert not r.get("fatal"), r.get("fatal")
    assert errors == [], errors
    return r


def test_playwright_paused_edits_show_the_right_frame(pw, env):
    r = _pw_run(pw, env, "edit_latency", env.config("edits", edits=6, seed=3))
    _report(f"pw-{pw.engine_name}-edits", by=r["by"])
    assert r["bad"] == [], r["bad"]
    assert all(b["failed"] == 0 and b["n"] >= 6 for b in r["by"].values()), r["by"]


def test_playwright_structural_agreement(pw, env):
    r = _pw_run(pw, env, "structural", env.config("structural", edits=40, seed=99), timeout=400)
    assert r["noVerdictCount"] == 0, r["noVerdict"]
    assert r["outcomes"] == {"match": r["applied"]} and r["applied"] == 40, r


def test_playwright_bake_splice(pw, env):
    _check_bake(_pw_run(pw, env, "bake_splice", env.config("bake")))


# Chromium keeps showing frames it already decoded when they are overwritten
# ahead of the playhead (3 stale frames, spec §2/§10: "wk-only"); WebKit
# re-enqueues them. There the logic check starts once the decoder queue has
# turned over (400 ms after the answer); WebKit is judged from presentedK + 5.
PW_STALE_MS = {"chromium": 400, "webkit": 0}


def test_playwright_edit_while_playing_logic(pw, env):
    r = _pw_run(pw, env, "edit_while_playing", env.config("playing", edits=4, seed=8))
    assert r["results"], r
    for x in r["results"]:
        bad = [b for b in x["bad"] if b["afterMs"] >= PW_STALE_MS[pw.engine_name]]
        assert bad == [], (pw.engine_name, x)
        assert x["changedJudged"] > 0, (pw.engine_name, x)


@pytest.mark.wk
def test_every_frame_by_paused_seek_over_real_proxies(env):
    """Every output frame of a 24-cut timeline over REAL proxies (two size
    classes, a 25 fps source in the 30 fps project), in order and shuffled:
    the engine canvas shows exactly the frame the program names."""
    for shuffle in ("0", "1"):
        r = env.wk("sweep", env.config("edits") + f"&shuffle={shuffle}", timeout=900)
        _report("sweep", shuffle=shuffle, total=r["total"], nBad=r["nBad"], lane=r["lane"])
        assert r["total"] > 600
        assert r["nBad"] == 0, r["bad"][:4]
