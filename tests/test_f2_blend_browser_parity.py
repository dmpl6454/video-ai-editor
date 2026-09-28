"""Wave E, F2: the LIVE blend of an overlay (CSS `mix-blend-mode`, what
lib/pipBlendLayers asks the browser for) against the EXPORT's blend
(render/pip.py's lut2 graph), per mode, in Playwright Chromium and WebKit.

Both sides get the SAME inputs: the export's own decoded frame of the base
(overlay lane muted) and of the overlay drawn Normal. The browser composites
them exactly as the app does — the base under a transparent canvas holding
the overlay's pixels at its opacity, the canvas carrying the mode — and the
screenshot is compared with the decoded export frame of that mode, inside
the overlay (Y and RGB PSNR). Outside it the base must be untouched.

A mode the browser cannot draw (CSS.supports false: `plus-darker`, Linear
Burn, in Chromium) is FLAGGED in the table, never hidden: the Inspector says
so for that browser (components/canvas/BlendSection).

    VAI_BLEND_PARITY_OUT=table.json .venv/bin/python -m pytest -q tests/test_f2_blend_browser_parity.py
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
from test_f2_canvas_blend import (  # noqa: E402,F401 — the render fixtures and helpers
    ASPECTS, BLEND_MIN_PSNR, _blend_edl, _export, _pip_box, decode, media, psnr,
)
from video_ai_editor.edl import canvas_blend as CB  # noqa: E402

playwright = pytest.importorskip("playwright.sync_api")

#: Live (browser) vs export, inside the overlay, per supported mode. The
#: export side carries 4:2:0 and a lossy encode; the browser blends 8-bit
#: sRGB. Measured (wave E F2): every unflagged mode 36.4-45.1 dB in both
#: browsers (Difference at full opacity the floor, as on the export side).
LIVE_MIN_PSNR = 35.0
#: APPROX (INSTANT_PREVIEW_SPEC §8.3) — a flagged mode's floor.
APPROX_DB = 28.0


def _flags() -> dict[str, str]:
    """lib/canvasBlend/catalog.ts LIVE_BLEND_FLAGS.webkit, read from the
    source (the Inspector's note is driven by the same table)."""
    import re
    src = (Path(__file__).resolve().parents[1] / "frontend/src/lib/canvasBlend/catalog.ts").read_text()
    body = src.split("export const LIVE_BLEND_FLAGS", 1)[1].split("webkit: {", 1)[1].split("}", 1)[0]
    return dict(re.findall(r"(\w+): '([\w-]+)'", body))


def _png(a: np.ndarray) -> str:
    buf = io.BytesIO()
    Image.fromarray(np.clip(a + 0.5, 0, 255).astype(np.uint8)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


PAGE = """<!doctype html><html><body style="margin:0;background:#000">
<div id="stage" style="position:relative;width:{w}px;height:{h}px;overflow:hidden">
  <img id="base" src="data:image/png;base64,{base}" style="position:absolute;left:0;top:0;width:{w}px;height:{h}px">
  <div style="position:absolute;inset:0;pointer-events:none">
    <canvas id="c" width="{w}" height="{h}" style="position:absolute;left:0;top:0;mix-blend-mode:{css}"></canvas>
  </div>
</div>
<script>
const img = new Image();
img.onload = () => {{
  const ctx = document.getElementById('c').getContext('2d');
  ctx.globalAlpha = {alpha};
  ctx.drawImage(img, {x}, {y});
  document.body.dataset.ready = '1';
}};
img.src = 'data:image/png;base64,{elem}';
window.supported = CSS.supports('mix-blend-mode', '{css}');
</script></body></html>"""


@pytest.fixture(scope="module")
def renders(media, tmp_path_factory):
    root = tmp_path_factory.mktemp("f2_live")
    wh = ASPECTS["9x16"]
    cb = decode(_export(root / "cb", _blend_edl(media, wh, "normal", muted=True)), 1.0, *wh)
    cs = decode(_export(root / "cs", _blend_edl(media, wh, "normal")), 1.0, *wh)
    out = {}
    for mode in CB.BLEND_IDS[1:]:
        for op in (1.0, 0.5):
            out[(mode, op)] = decode(_export(root / f"{mode}{op}", _blend_edl(media, wh, mode, op)), 1.0, *wh)
    return wh, cb, cs, out


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


def _live(browser, wh, cb, cs, mode: str, alpha: float) -> tuple[np.ndarray, bool]:
    x0, y0, x1, y1 = _pip_box(wh)
    ctx = browser.new_context(viewport={"width": wh[0], "height": wh[1]}, device_scale_factor=1)
    page = ctx.new_page()
    try:
        page.set_content(PAGE.format(w=wh[0], h=wh[1], base=_png(cb), elem=_png(cs[y0:y1, x0:x1]),
                                     css=CB.blend_of(mode).css, alpha=alpha, x=x0, y=y0))
        page.wait_for_selector("body[data-ready='1']", timeout=10000)
        page.wait_for_timeout(50)
        shot = page.locator("#stage").screenshot()
        supported = bool(page.evaluate("window.supported"))
    finally:
        ctx.close()
    return np.asarray(Image.open(io.BytesIO(shot)).convert("RGB"), dtype=np.float64), supported


def _y(a: np.ndarray) -> np.ndarray:
    return 16 + (219 / 255) * (0.2126 * a[..., 0] + 0.7152 * a[..., 1] + 0.0722 * a[..., 2])


def test_live_blend_matches_the_export_per_mode(browser, renders):
    wh, cb, cs, out = renders
    x0, y0, x1, y1 = _pip_box(wh)
    inner = (slice(y0 + 3, y1 - 3), slice(x0 + 3, x1 - 3))
    outside = np.ones((wh[1], wh[0]), bool)
    outside[max(0, y0 - 2):y1 + 2, max(0, x0 - 2):x1 + 2] = False
    flags = _flags() if browser.engine_name == "webkit" else {}
    rows, bad = [], []
    for mode in CB.BLEND_IDS[1:]:
        for op in (1.0, 0.5):
            live, supported = _live(browser, wh, cb, cs, mode, op)
            exp = out[(mode, op)]
            row = {"browser": browser.engine_name, "mode": mode, "opacity": op, "supported": supported,
                   "flag": flags.get(mode), "rgb_db": round(psnr(live[inner], exp[inner]), 2),
                   "y_db": round(psnr(_y(live[inner]), _y(exp[inner])), 2),
                   "outside_max": float(np.abs(live[outside] - cb[outside]).max())}
            rows.append(row)
            if not supported:
                continue
            assert row["outside_max"] <= 1, row          # the base is untouched outside the overlay
            flag = flags.get(mode)
            if flag == "approx":
                if row["rgb_db"] < APPROX_DB:
                    bad.append(row)
            elif flag == "partial-opacity" and op < 1:
                # flagged, reported; the flag must still be TRUE (else drop it)
                assert row["rgb_db"] < LIVE_MIN_PSNR, f"{mode} at {op} now matches: remove its flag"
            elif row["rgb_db"] < LIVE_MIN_PSNR:
                bad.append(row)
    print(json.dumps(rows))
    if os.environ.get("VAI_BLEND_PARITY_OUT"):
        with open(os.environ["VAI_BLEND_PARITY_OUT"], "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rows) + "\n")
    assert not bad, f"{browser.engine_name}: live blend below its class: {bad}"
    unsupported = {r["mode"] for r in rows if not r["supported"]}
    # only the Porter-Duff darker operator may be missing (Chromium); it is
    # flagged in the UI (BlendSection) rather than silently drawn Normal
    assert unsupported <= {"linear_burn"}, unsupported
    if browser.engine_name == "webkit":
        assert not unsupported, "WebKit (the app's engine) draws every mode live"
