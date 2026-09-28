"""Clip animations (wave E, F1): the ENGINE's picture of every In, Out and
Combo preset on the main track against the EXPORT's decoded frames of the
same EDL — Y-PSNR per preset at three frames of its window, over REAL
proxies of real footage (the product's PreviewEngine, geometry.ts and the
WebGL2 compositor, bundled by esbuild from frontend/src).

EXACT (lib/preview/timeline/support.ts) needs ≥ 35 dB. Blur In / Blur Out
have no client pass: support.ts classifies their windows BAKED (the server's
frames are spliced in), so they are measured and reported here, not held to
EXACT.

Runs in Playwright Chromium and Playwright WebKit on every machine, and in
the real WKWebView (marker wk) where a GUI session is unlocked:

    VAI_WK=1 .venv/bin/python -m pytest -q tests/wk/test_clip_anim_parity.py -k wkwebview
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from .conftest import PAGES
from .engine_server import EngineServer
from .test_wk_phase1_video import (  # noqa: F401 — module fixtures reused
    APPROX_DB, BENCH, CANVAS, EXACT_DB, FPS, _decode_y, engine_bundle, geometry_psnr, run,
)

playwright = pytest.importorskip("playwright.sync_api")

CLIP_S = 1.6
DUR = 0.6
N = int(round(CLIP_S * FPS))
D = int(round(DUR * FPS))
SHOTS = Path(os.environ["VAI_ENGINE_SHOTS"]) if os.environ.get("VAI_ENGINE_SHOTS") else None


def _presets() -> dict[str, list[tuple[str, dict]]]:
    from video_ai_editor.edl import clip_animations as A
    groups: dict[str, list[tuple[str, dict]]] = {
        "anim_in": [(f"in:{p.id}", {"anim_in": p.id, "anim_dur": DUR}) for p in A.IN_PRESETS],
        "anim_out": [(f"out:{p.id}", {"anim_out": p.id, "anim_out_dur": DUR}) for p in A.OUT_PRESETS],
        "anim_combo": [(f"combo:{p.id}", {"anim_combo": p.id}) for p in A.COMBO_PRESETS],
        # on top of keyed and static poses (the keyframe clock is shared)
        "anim_keyed": [
            ("keyed:slide_up+kf", {"anim_in": "slide_up", "anim_dur": DUR,
                                   "transform": {"x": {"keyframes": [[0, -40], [CLIP_S, 40]], "interp": "linear"},
                                                 "scale": {"keyframes": [[0, 0.9], [CLIP_S, 1.2]], "interp": "ease-in"}}}),
            ("keyed:rock+rotation", {"anim_combo": "rock",
                                     "transform": {"rotation": {"keyframes": [[0, 0], [CLIP_S, 20]], "interp": "linear"}}}),
            ("keyed:spin+static", {"anim_out": "spin", "anim_out_dur": DUR,
                                   "transform": {"scale": 1.3, "x": 30.0, "rotation": 10.0, "opacity": 0.8}}),
            ("keyed:zoom_in_out+cover", {"anim_combo": "zoom_in_out", "fit": "cover"}),
        ],
    }
    return groups


def _ks(feat: str) -> tuple[int, ...]:
    if feat.startswith("in:") or feat.startswith("keyed:slide"):
        return (D // 6, D // 2, (5 * D) // 6)
    if feat.startswith("out:") or feat.startswith("keyed:spin"):
        return (N - D + D // 6, N - D + D // 2, N - D + (5 * D) // 6)
    return (7, 20, 36)


def _edl(group: str, master: str):
    from video_ai_editor.edl.schema import Canvas, Clip, Keyframe, empty_edl
    e = empty_edl(Canvas(w=CANVAS[0], h=CANVAS[1], fps=FPS))
    e.canvas.loudness_lufs = None
    v1 = e.get_track("v1")
    for i, (_feat, f) in enumerate(_presets()[group]):
        c = Clip(src=master, start=CLIP_S * i, id=f"a{i:02d}")
        c.in_, c.out = 0.2, 0.2 + CLIP_S
        c.fit = f.get("fit", "contain")
        for k, v in f.get("transform", {}).items():
            setattr(c.transform, k, Keyframe(**v) if isinstance(v, dict) else v)
        for k in ("anim_in", "anim_out", "anim_combo", "anim_dur", "anim_out_dur"):
            if k in f:
                setattr(c, k, f[k])
        v1.clips.append(c)
    e.recompute_duration()
    return e


@pytest.fixture(scope="module")
def anim_env(engine_bundle, tmp_path_factory):  # noqa: F811
    src = BENCH / "broll_20s.mp4"
    if not src.is_file() or shutil.which("ffmpeg") is None:
        pytest.skip(f"no bench footage at {src}")
    proxy_queue = pytest.importorskip("video_ai_editor.ingest.proxy_queue")
    P = pytest.importorskip("video_ai_editor.ingest.proxy")
    from video_ai_editor import storage as _storage
    from video_ai_editor.render import compositor
    from video_ai_editor.render.frame_map import SourceInfo as FmSource

    root = tmp_path_factory.mktemp("anim-parity")
    master = root / "masters" / "land.mp4"
    master.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", "2", "-i", str(src), "-t", "2.2",
                    "-vf", "scale=1280:720,setsar=1,fps=30,format=yuv420p", "-an", "-c:v", "libx264", "-crf", "16",
                    "-preset", "veryfast", str(master)], check=True, capture_output=True)
    old = _storage.WORKDIR
    _storage.WORKDIR = root / "wd"
    _storage.WORKDIR.mkdir()
    manager = proxy_queue.ProxyManager()
    try:
        key = manager.ensure(str(master))
        assert manager.wait_idle(240), "proxy build did not finish"
        dirs = {key: P.proxy_dir(key)}
        sources = {str(master): {"key": key, "info": FmSource.from_proxy(P.load_source(key)).to_json()}}
        groups, server_y = [], {}
        for g in _presets():
            edl = _edl(g, str(master))
            sess = root / "render" / g
            sess.mkdir(parents=True)
            out = compositor._render(edl, sess / "out.mp4", height=CANVAS[1], fps=FPS, preview=False,
                                     cache_dir=sess / "cache", chunked=False)
            ys = _decode_y(out, *CANVAS)
            ks = []
            for i, (feat, _f) in enumerate(_presets()[g]):
                for j in _ks(feat):
                    k = i * N + j
                    server_y[(g, k)] = (feat, ys[k])
                    ks.append(k)
            d = edl.model_dump(by_alias=True, mode="json")
            d["tracks"] = [t for t in d["tracks"] if t["id"] == "v1"]
            groups.append({"name": g, "edl": d, "ks": ks})
    finally:
        manager.shutdown()
        manager.wait_idle(10)
        _storage.WORKDIR = old
    fx = root / "fixture"
    fx.mkdir()
    (fx / "timeline.json").write_text(json.dumps({"edl": groups[0]["edl"], "sources": sources,
                                                  "canvas": list(CANVAS), "groups": groups}))
    srv = EngineServer({"pages": PAGES, "testkit": engine_bundle, "fixture": fx}, proxies=dirs)
    try:
        yield srv, server_y
    finally:
        srv.close()


#: support.ts `ANIM_APPROX` (the presets measured under EXACT_DB) — kept
#: equal by frontend/src/lib/anim/animSupport.test.ts; a case whose name
#: carries one of them is APPROX too.
ANIM_APPROX = {"in:zoom_out", "in:spin", "out:spin"}


def _is_approx(feat: str) -> bool:
    return feat in ANIM_APPROX or feat.startswith("keyed:spin")


def _classify(per: dict[str, list[float]]) -> dict[str, float]:
    return {f: min(v) for f, v in per.items()}


def _assert_parity(per: dict[str, float], engine: str) -> None:
    """EXACT presets ≥ 35 dB, the measured APPROX ones ≥ 28 dB; Blur (BAKED)
    is only reported."""
    blur = {f: db for f, db in per.items() if "blur" in f}
    exact = {f: db for f, db in per.items() if "blur" not in f and not _is_approx(f)}
    approx = {f: db for f, db in per.items() if _is_approx(f)}
    print(json.dumps({"engine": engine, "exact_min_db": min(exact.values()), "approx_db": approx,
                      "per_preset_db": per, "blur_baked_db": blur}))
    assert len(per) == sum(len(v) for v in _presets().values())
    bad = {f: db for f, db in exact.items() if db < EXACT_DB}
    assert not bad, f"{engine}: below EXACT ({EXACT_DB} dB): {bad}"
    bad = {f: db for f, db in approx.items() if db < APPROX_DB}
    assert not bad, f"{engine}: below APPROX ({APPROX_DB} dB): {bad}"


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


def _run_page(browser, server, scenario: str, timeout: float = 240, shot: str | None = None, **query) -> dict:
    token = secrets.token_hex(8)
    ctx = browser.new_context(viewport={"width": CANVAS[0] + 40, "height": CANVAS[1] + 40})
    page = ctx.new_page()
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
        ctx.close()


def test_clip_animation_parity_in_playwright_browsers(browser, anim_env):
    srv, server_y = anim_env
    hold = f"anim_in:{2 * N + D // 2}"   # Zoom Out, mid-window, left on screen
    r = _run_page(browser, srv, "geometry", shot="clip-anim", hold=hold)
    per = geometry_psnr(r, server_y)
    print(json.dumps({"engine": browser.engine_name, "per_frame_db": per}))
    _assert_parity(_classify(per), browser.engine_name)


@pytest.mark.wk
def test_clip_animation_parity_in_wkwebview(anim_env, tmp_path_factory):
    from .harness import WKHarness
    srv, server_y = anim_env
    harness = WKHarness(srv, tmp_path_factory.mktemp("wk-anim-runs"))
    try:
        r = run(harness, "geometry", timeout=240)
    finally:
        harness.close()
    _assert_parity(_classify(geometry_psnr(r, server_y)), "wkwebview")
