"""Transform.flip_h / flip_v drawn by the browser = the export (wave E, lane
F4a), in Chromium AND WebKit, with screenshots.

Two projects, each flipped through `flip_clip` (the op; its Inspector buttons
and prompt are lane F4b's):

* A: a marker source on the Main video, mirrored and turned 20° — the client
  engine draws it (geometry.ts `flipStage`, EXACT);
* B: a grey Main video under a mirrored, turned overlay of the marker source
  and a mirrored two-colour sticker — StickerLayer draws both, in the client
  engine and in the server preview alike (`pipDraw.flipScale`).

The preview box is screenshotted and every marker / sticker half is located;
mapped to canvas pixels, each must sit where the export (the compositor,
decoded) puts it, within 2.5 % of the canvas width (display resampling).

Harness: `test_frontend_a11y`'s server fixture (VAE_A11Y_BASE_URL = a Vite
dev server proxying /api to a backend). Screenshots go to VAE_FLIP_SHOTS.
"""
from __future__ import annotations

import io
import os
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
import geometry_golden_lib as GL  # noqa: E402
from overlay_render_helpers import centroid, frame_rgb, gray_clip, mask_of  # noqa: E402
from test_frontend_a11y import base_url  # noqa: E402,F401  (fixture)
from test_speed_ui_e2e import _client, _edl, _open, _timecode, engine, pw  # noqa: E402,F401

from video_ai_editor.edl.schema import EDL  # noqa: E402
from video_ai_editor.render import compositor  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
SHOTS = Path(os.environ.get("VAE_FLIP_SHOTS", "/tmp"))
W, H = 640, 360
TOL = 0.025 * W
MAGENTA, CYAN = (255, 0, 255), (0, 255, 255)


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("flipui")
    png = d / "mc.png"
    img = Image.new("RGBA", (200, 100), (0, 0, 0, 0))
    img.paste((*MAGENTA, 255), (0, 0, 100, 100))
    img.paste((*CYAN, 255), (100, 0, 200, 100))
    img.save(png)
    return {"land": GL.make_source(d / "land.mp4", GL.SOURCES["land"]),
            "grey": gray_clip(d / "grey.mp4", W, H, 3.0, 30, "gray"), "png": png}


def _dispatch(c, sid: str, tool: str, args: dict) -> dict:
    r = c.post(f"/api/sessions/{sid}/dispatch", json={"tool": tool, "args": args})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _upload(c, sid: str, path: Path, add: bool, mime: str) -> str:
    with path.open("rb") as fh:
        r = c.post(f"/api/sessions/{sid}/upload", files={"file": (path.name, fh, mime)},
                   data={"add_to_timeline": "true" if add else "false", "transcribe": "false"})
    assert r.status_code in (200, 202), r.text
    return r.json()["src"]


def _project(base_url, media, kind: str, name: str) -> str:  # noqa: F811
    with _client(base_url) as c:
        sid = c.post("/api/sessions", json={"name": name}).json()["id"]
        main = media["land"] if kind == "A" else media["grey"]
        _upload(c, sid, main, True, "video/mp4")
        _dispatch(c, sid, "set_canvas", {"w": W, "h": H, "fps": 30})
        edl = c.get(f"/api/sessions/{sid}/edl").json()
        v1 = next(t for t in edl["tracks"] if t["id"] == "v1")["clips"][0]["id"]
        if kind == "A":
            _dispatch(c, sid, "flip_clip", {"clip_id": v1, "axis": "horizontal"})
            _dispatch(c, sid, "set_clip_transform", {"clip_id": v1, "rotation": 20.0})
        else:
            src = _upload(c, sid, media["land"], False, "video/mp4")
            pip = _dispatch(c, sid, "add_clip", {"track": "v2", "src": src, "in": 0.0, "out": 2.0,
                                                 "start": 0.0})["clip_id"]
            _dispatch(c, sid, "set_clip_transform", {"clip_id": pip, "x": 200.0, "y": 180.0,
                                                     "scale": 1.3, "rotation": 25.0})
            _dispatch(c, sid, "flip_clip", {"clip_id": pip, "axis": "horizontal"})
            png = _upload(c, sid, media["png"], False, "image/png")
            st = _dispatch(c, sid, "add_sticker", {"src": png, "start": 0.0, "end": 2.0})["sticker_id"]
            _dispatch(c, sid, "set_clip_transform", {"clip_id": st, "x": 500.0, "y": 180.0, "rotation": 30.0})
            _dispatch(c, sid, "flip_clip", {"clip_id": st, "axis": "horizontal"})
    return sid


def _points(rgb: np.ndarray, kind: str) -> dict[str, tuple[float, float] | None]:
    """Marker centres (and, for B, the sticker's two halves) in `rgb`'s px."""
    out: dict[str, tuple[float, float] | None] = {}
    for name, m in GL.measure_frame(rgb, gain_only=False)["markers"].items():
        out[name] = tuple(m) if m else None
    if kind == "B":
        out["magenta"] = centroid(mask_of(rgb, MAGENTA, tol=90))
        out["cyan"] = centroid(mask_of(rgb, CYAN, tol=90))
    return out


def _export_points(base_url, sid: str, kind: str, tmp: Path) -> dict:  # noqa: F811
    edl = EDL.model_validate(_edl(base_url, sid))
    out = compositor._render(edl, tmp / f"{kind}.mp4", height=H, fps=30, preview=False,
                             cache_dir=tmp / "cache", chunked=False)
    return _points(frame_rgb(out, 15), kind)


def _preview_points(page, kind: str, shot: Path) -> dict:
    # the preview box, either mode: StickerLayer's canvas is laid over it
    box = page.locator('canvas[data-layer="stickers"]').first.locator("xpath=..")
    png = box.screenshot(path=str(shot))
    img = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
    h, w = img.shape[:2]
    # the screenshot is the box at device pixels: bring it to canvas pixels
    canvas = np.asarray(Image.fromarray(img).resize((W, H), Image.BILINEAR))
    assert abs(w / h - W / H) < 0.02, (w, h)
    return _points(canvas, kind)


def _compare(got: dict, want: dict, what: str) -> None:
    seen = 0
    for name, p in want.items():
        if p is None:
            continue
        q = got.get(name)
        assert q is not None, f"{what}: {name} not drawn (export at {p})"
        assert np.hypot(q[0] - p[0], q[1] - p[1]) <= TOL, f"{what}: {name} at {q}, export {p}"
        seen += 1
    assert seen >= 4, f"{what}: only {seen} points measured"


def _set_playhead(page, t: float) -> None:
    clock = page.get_by_role("textbox", name="Playhead timecode")
    clock.click()
    clock.fill(_timecode(t))
    clock.press("Enter")
    page.wait_for_timeout(600)


def _preview_mode(page, mode: str) -> bool:
    page.evaluate("""(m) => fetch('/api/settings/preview', {method: 'PUT',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({engine: m})})""", mode)
    page.reload()
    page.locator(".timeline-canvas-wrap canvas").first.wait_for()
    page.wait_for_timeout(1800)
    return page.locator('[data-preview-engine="client"]').count() > 0


@pytest.mark.parametrize("kind", ["A", "B"])
def test_the_browser_draws_a_flip_where_the_export_does(engine, base_url, media, kind, tmp_path):  # noqa: F811
    name = engine.engine_name
    sid = _project(base_url, media, kind, f"flip {kind} {name}")
    want = _export_points(base_url, sid, kind, tmp_path)
    assert want["white"] is not None
    page = _open(engine, base_url, sid, 1280, 800)
    try:
        client = _preview_mode(page, "client")
        if name == "chromium":
            assert client, "headless Chromium runs the client preview"
        # The server preview draws the Main video itself (A: the export's own
        # pixels) and leaves overlays and stickers to StickerLayer (B).
        modes = (["client"] if client else []) + ["server"]
        print(f"flip {kind} {name}: preview modes {modes}")
        for mode in modes:
            if mode == "server":
                _preview_mode(page, "server")
            _set_playhead(page, 0.5)
            page.wait_for_timeout(2500 if mode == "server" else 800)
            shot = SHOTS / f"f4a_flip_{kind}_{name}_{mode}_1280x800.png"
            _compare(_preview_points(page, kind, shot), want, f"{kind} {name} {mode}")
    finally:
        page.evaluate("""() => fetch('/api/settings/preview', {method: 'PUT',
            headers: {'Content-Type': 'application/json'}, body: JSON.stringify({engine: 'server'})})""")
        page.context.close()
