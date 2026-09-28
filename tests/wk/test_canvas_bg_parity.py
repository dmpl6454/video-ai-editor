"""CapCut Canvas backgrounds (wave E, F2): the ENGINE's picture of every
canvas kind — colour, the four blur strengths, image — against the EXPORT's
decoded frames of the same EDL, on a 16:9 canvas (a portrait clip, bars left
and right) and a 9:16 canvas (a landscape clip, bars top and bottom), over
REAL proxies of real footage (the product's PreviewEngine, geometry.ts,
canvasBg.ts / canvasBlur.ts and the WebGL2 compositor, bundled by esbuild
from frontend/src).

Y-PSNR per case, whole frame AND the letterbox alone (the background's own
pixels, the stricter number); support.ts `CANVAS_BG_MODE` (and
`CANVAS_BG_ROTATED_MODE` for a rotated clip) is asserted here: EXACT ≥ 35 dB,
APPROX ≥ 28 dB, in both numbers.

Runs in Playwright Chromium and Playwright WebKit on every machine, and in
the real WKWebView (marker wk) where a GUI session is unlocked:

    VAI_WK=1 .venv/bin/python -m pytest -q tests/wk/test_canvas_bg_parity.py -k wkwebview
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

from .conftest import PAGES
from .engine_server import EngineServer
from .test_wk_phase1_video import (  # noqa: F401 — module fixtures reused
    APPROX_DB, BENCH, EXACT_DB, FPS, _decode_y, engine_bundle, run,
)

playwright = pytest.importorskip("playwright.sync_api")

CLIP_S = 0.5
N = int(round(CLIP_S * FPS))
K_IN_CLIP = 7
SHOTS = Path(os.environ["VAI_ENGINE_SHOTS"]) if os.environ.get("VAI_ENGINE_SHOTS") else None
#: canvas → the master that letterboxes on it
LAYOUTS = {"16x9": ((640, 360), "port"), "9x16": ((360, 640), "land")}
MASTERS = {"land": "scale=1280:720", "port": "crop=405:720:437:0,scale=720:1280"}


def _cases() -> list[tuple[str, dict]]:
    """(feature, clip fields): the kind is the feature's first word."""
    return [
        ("color:red", {"canvas_bg": {"type": "color", "color": "#E53935"}}),
        ("color:green+opacity", {"canvas_bg": {"type": "color", "color": "#40A060"},
                                 "transform": {"opacity": 0.7}}),
        ("color:white+rotate", {"canvas_bg": {"type": "color", "color": "#FFFFFF"},
                                "transform": {"rotation": 6.0, "scale": 1.1}}),
        *[(f"blur:{lv}", {"canvas_bg": {"type": "blur", "blur": lv}}) for lv in (1, 2, 3, 4)],
        ("blur:2+pan", {"canvas_bg": {"type": "blur", "blur": 2}, "transform": {"x": 40.0, "scale": 0.9}}),
        ("blur:3+rotate", {"canvas_bg": {"type": "blur", "blur": 3}, "transform": {"rotation": -8.0}}),
        ("image", {"canvas_bg": {"type": "image"}}),
        ("image+flip", {"canvas_bg": {"type": "image"}, "transform": {"flip_h": True}}),
        # review RE: the background is a still layer under the moving picture
        ("blur:3+scale07", {"canvas_bg": {"type": "blur", "blur": 3}, "transform": {"scale": 0.7}}),
        ("color:red+pan", {"canvas_bg": {"type": "color", "color": "#E53935"},
                           "transform": {"x": 60.0, "y": -30.0, "scale": 0.8}}),
        ("image+scale06", {"canvas_bg": {"type": "image"}, "transform": {"scale": 0.6}}),
        ("blur:2+flip", {"canvas_bg": {"type": "blur", "blur": 2}, "transform": {"flip_h": True}}),
    ]


def _kind(feat: str) -> str:
    return feat.split(":")[0].split("+")[0]


def _edl(master: str, pic: str, canvas: tuple[int, int]):
    from video_ai_editor.edl.schema import Canvas, CanvasBackground, Clip, empty_edl
    e = empty_edl(Canvas(w=canvas[0], h=canvas[1], fps=FPS))
    e.canvas.loudness_lufs = None
    v1 = e.get_track("v1")
    for i, (_feat, f) in enumerate(_cases()):
        c = Clip(src=master, start=CLIP_S * i, id=f"c{i:02d}")
        c.in_, c.out = 0.2 + CLIP_S * (i % 12), 0.2 + CLIP_S * (i % 12 + 1)   # the master is ~7 s
        bg = dict(f["canvas_bg"])
        if bg["type"] == "image":
            bg["image"] = pic
        c.canvas_bg = CanvasBackground(**bg)
        for k, v in f.get("transform", {}).items():
            setattr(c.transform, k, v)
        v1.clips.append(c)
    e.recompute_duration()
    return e


def _bars(canvas: tuple[int, int], master: str) -> np.ndarray:
    """The letterbox, less a 6 px band at the picture's edge (the export's
    4:2:0 chroma and the lossy encode meet the picture there)."""
    from video_ai_editor.render.sar import fit_dims
    W, H = canvas
    sw, sh = (1280, 720) if master == "land" else (720, 1280)
    fw, fh = fit_dims(sw, sh, W, H, "decrease")
    x0, y0 = ((W - fw) // 2) & ~1, ((H - fh) // 2) & ~1
    m = np.ones((H, W), bool)
    m[max(0, y0 - 6):y0 + fh + 6, max(0, x0 - 6):x0 + fw + 6] = False
    return m


@pytest.fixture(scope="module")
def canvas_env(engine_bundle, tmp_path_factory):  # noqa: F811
    src = BENCH / "broll_20s.mp4"
    if not src.is_file() or shutil.which("ffmpeg") is None:
        pytest.skip(f"no bench footage at {src}")
    proxy_queue = pytest.importorskip("video_ai_editor.ingest.proxy_queue")
    P = pytest.importorskip("video_ai_editor.ingest.proxy")
    from PIL import Image
    from video_ai_editor import storage as _storage
    from video_ai_editor.render import canvas_bg, compositor
    from video_ai_editor.render.frame_map import SourceInfo as FmSource

    root = tmp_path_factory.mktemp("canvas-parity")
    masters: dict[str, Path] = {}
    for name, vf in MASTERS.items():
        m = root / "masters" / f"{name}.mp4"
        m.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", "3", "-i", str(src), "-t", "6",
                        "-vf", f"{vf},setsar=1,fps=30,format=yuv420p", "-an", "-c:v", "libx264", "-crf", "16",
                        "-preset", "veryfast", str(m)], check=True, capture_output=True)
        masters[name] = m
    pic = root / "pic.png"
    grad = Image.linear_gradient("L").resize((300, 200))
    Image.merge("RGB", (grad, grad.transpose(Image.Transpose.FLIP_LEFT_RIGHT), grad.rotate(90).resize((300, 200)))
                ).save(pic)
    old = _storage.WORKDIR
    _storage.WORKDIR = root / "wd"
    _storage.WORKDIR.mkdir()
    manager = proxy_queue.ProxyManager()
    envs = {}
    try:
        keys = {n: manager.ensure(str(m)) for n, m in masters.items()}
        assert manager.wait_idle(300), "proxy build did not finish"
        dirs = {k: P.proxy_dir(k) for k in keys.values()}
        for layout, (canvas, mname) in LAYOUTS.items():
            master = str(masters[mname])
            edl = _edl(master, str(pic), canvas)
            sess = root / "render" / layout
            sess.mkdir(parents=True)
            out = compositor._render(edl, sess / "out.mp4", height=canvas[1], fps=FPS, preview=False,
                                     cache_dir=sess / "cache", chunked=False)
            ys = _decode_y(out, *canvas)
            server_y = {}
            ks = []
            for i, (feat, _f) in enumerate(_cases()):
                k = i * N + K_IN_CLIP
                server_y[k] = (feat, ys[k])
                ks.append(k)
            fx = root / f"fixture-{layout}"
            (fx / "canvas-bg").mkdir(parents=True)
            for c in edl.get_track("v1").clips:
                if c.canvas_bg.type == "image":
                    shutil.copy(canvas_bg.image_file(c.canvas_bg.image, *canvas), fx / "canvas-bg" / f"{c.id}.png")
            d = edl.model_dump(by_alias=True, mode="json")
            d["tracks"] = [t for t in d["tracks"] if t["id"] == "v1"]
            sources = {master: {"key": keys[mname],
                                "info": FmSource.from_proxy(P.load_source(keys[mname])).to_json()}}
            (fx / "timeline.json").write_text(json.dumps({
                "edl": d, "sources": sources, "canvas": list(canvas),
                "groups": [{"name": layout, "edl": d, "ks": ks}]}))
            envs[layout] = (fx, server_y, canvas, mname)
    finally:
        manager.shutdown()
        manager.wait_idle(10)
        _storage.WORKDIR = old
    servers = {layout: EngineServer({"pages": PAGES, "testkit": engine_bundle, "fixture": fx}, proxies=dirs)
               for layout, (fx, *_rest) in envs.items()}
    try:
        yield {layout: (servers[layout], *envs[layout][1:]) for layout in envs}
    finally:
        for s in servers.values():
            s.close()


def _psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = float(np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2))
    return 99.0 if mse == 0 else 10 * np.log10(255.0 ** 2 / mse)


def _measure(result: dict, server_y: dict, canvas, mname) -> dict[str, dict[str, float]]:
    bars = _bars(canvas, mname)
    out: dict[str, dict[str, float]] = {}
    for f in result["frames"]:
        assert f["ok"] and f["y"], f"k={f['k']} was not drawn"
        y = np.frombuffer(base64.b64decode(f["y"]), np.uint8).reshape(canvas[1], canvas[0])
        feat, ref = server_y[f["k"]]
        out[feat] = {"frame_db": round(_psnr(y, ref), 2), "bars_db": round(_psnr(y[bars], ref[bars]), 2)}
    return out


def _assert_classes(per: dict[str, dict[str, float]], engine: str, layout: str) -> None:
    """support.ts CANVAS_BG_MODE (read from the source, not restated)."""
    src = (Path(__file__).resolve().parents[2] / "frontend/src/lib/preview/timeline/support.ts").read_text()
    body = src.split("export const CANVAS_BG_MODE", 1)[1].split("}", 1)[0]
    modes = {k: ("EXACT" if f"{k}: MODE_EXACT" in body else "APPROX" if f"{k}: MODE_APPROX" in body else "BAKED")
             for k in ("color", "image", "blur")}
    rot = src.split("export const CANVAS_BG_ROTATED_MODE: Mode = ", 1)[1].split("\n", 1)[0].strip()
    modes["rotated"] = rot.replace("MODE_", "")
    moved = src.split("export const CANVAS_BG_MOVED_MODE: Mode = ", 1)[1].split("\n", 1)[0].strip()
    modes["moved"] = moved.replace("MODE_", "")
    row = json.dumps({"engine": engine, "layout": layout, "modes": modes, "per_case": per})
    print(row)
    if os.environ.get("VAI_CANVAS_PARITY_OUT"):
        with open(os.environ["VAI_CANVAS_PARITY_OUT"], "a", encoding="utf-8") as fh:
            fh.write(row + "\n")
    assert len(per) == len(_cases())
    for feat, v in per.items():
        tx = dict(_cases())[feat].get("transform") or {}
        moved = float(tx.get("scale", 1.0)) < 0.999      # support.ts canvasBgReason's "moved" rule
        mode = modes["rotated" if "rotate" in feat else "moved" if moved else _kind(feat)]
        worst = min(v["frame_db"], v["bars_db"])
        if mode == "EXACT":
            assert worst >= EXACT_DB, f"{engine} {layout} {feat}: {v} below EXACT ({EXACT_DB} dB)"
        elif mode == "APPROX":
            assert worst >= APPROX_DB, f"{engine} {layout} {feat}: {v} below APPROX ({APPROX_DB} dB)"


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


def _run_page(browser, server, canvas, timeout: float = 240, shot: str | None = None, **query) -> dict:
    token = secrets.token_hex(8)
    ctx = browser.new_context(viewport={"width": canvas[0] + 40, "height": canvas[1] + 40})
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(server.url("pages/engine.html", {"scenario": "geometry", "token": token,
                                                   "canvasBg": "/fixture/canvas-bg", **query}))
        deadline = time.monotonic() + timeout
        with server.box.cond:
            while token not in server.box.results and time.monotonic() < deadline:
                server.box.cond.wait(0.2)
            body = server.box.results.pop(token, None)
        assert body is not None, f"geometry posted nothing in {browser.engine_name}; page errors {errors}"
        r = json.loads(body)
        assert not r.get("fatal"), r.get("fatal")
        if shot and SHOTS:
            SHOTS.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(SHOTS / f"{shot}-{browser.engine_name}.png"))
        return r
    finally:
        ctx.close()


@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_canvas_background_parity_in_playwright_browsers(browser, canvas_env, layout):
    srv, server_y, canvas, mname = canvas_env[layout]
    hold = f"{layout}:{5 * N + K_IN_CLIP}"          # blur 3 left on screen for the screenshot
    r = _run_page(browser, srv, canvas, shot=f"canvas-bg-{layout}", hold=hold)
    _assert_classes(_measure(r, server_y, canvas, mname), browser.engine_name, layout)


@pytest.mark.wk
@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_canvas_background_parity_in_wkwebview(canvas_env, tmp_path_factory, layout):
    from .harness import WKHarness
    srv, server_y, canvas, mname = canvas_env[layout]
    harness = WKHarness(srv, tmp_path_factory.mktemp("wk-canvas-runs"))
    try:
        r = run(harness, "geometry", timeout=240, canvasBg="/fixture/canvas-bg")
    finally:
        harness.close()
    _assert_classes(_measure(r, server_y, canvas, mname), "wkwebview", layout)
