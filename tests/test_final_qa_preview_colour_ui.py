"""Final QA (export-truth): the DEFAULT (server) preview shows the main picture
and a picture-in-picture of the SAME footage as the same grey — in Chromium
and in WebKit — and that grey is the export's.

WebKit presents an untagged <video> colour-managed (~1.96 gamma), while PiPs
are drawn colour-exact through a WebGL copy (lib/videoColour). Measured before
the fix on a flat 100-grey clip (BT.709 matrix, transfer unknown), v1 corner /
PiP centre: export 98/98, Chromium 98/98, WebKit server preview 109/98 — two
greys for one clip. In WebKit the server <video>'s frames are now drawn onto a
colour-exact canvas laid over it (Preview.tsx `exactRef`).

Harness: `test_frontend_a11y`'s server fixture (VAE_A11Y_BASE_URL = a Vite dev
server proxying /api to a backend). Screenshots go to VAE_COLOUR_SHOTS.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
from test_frontend_a11y import base_url  # noqa: E402,F401  (fixture)
from test_speed_ui_e2e import _client, _open, engine, pw  # noqa: E402,F401

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
SHOTS = Path(os.environ.get("VAE_COLOUR_SHOTS", "/tmp"))
W, H = 640, 360
#: Display resampling and 8-bit rounding; the defect was 11 levels.
TOL = 2


@pytest.fixture(scope="module")
def grey(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("colour") / "grey100.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=0x646464:s={W}x{H}:r=30:d=4",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-c:a", "aac", str(out)],
                   check=True)
    return out


def _decoded_grey(src: Path) -> float:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-frames:v", "1", "-vf", "format=rgb24",
                          "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    return float(np.frombuffer(raw, np.uint8).reshape(H, W, 3).mean())


def _project(base_url, grey: Path) -> str:  # noqa: F811
    with _client(base_url) as c:
        sid = c.post("/api/sessions", json={"name": "Grey PiP"}).json()["id"]
        with grey.open("rb") as fh:
            r = c.post(f"/api/sessions/{sid}/upload", files={"file": (grey.name, fh, "video/mp4")},
                       data={"add_to_timeline": "true", "transcribe": "false"})
        assert r.status_code in (200, 202), r.text
        deadline = time.time() + 120
        while True:
            edl = c.get(f"/api/sessions/{sid}/edl").json()
            v1 = next(t for t in edl["tracks"] if t["id"] == "v1")["clips"]
            if v1:
                break
            assert time.time() < deadline, "the import never reached the timeline"
            time.sleep(0.3)

        def dispatch(tool, args):
            r = c.post(f"/api/sessions/{sid}/dispatch", json={"tool": tool, "args": args})
            assert r.status_code == 200, r.text
            return r.json()["result"]
        dispatch("set_canvas", {"w": W, "h": H, "fps": 30})
        ov = dispatch("add_clip", {"track": "v2", "src": v1[0]["src"], "in": 0.0, "out": 4.0, "start": 0.0})
        dispatch("set_clip_transform", {"clip_id": ov["clip_id"], "scale": 0.8})
    return sid


def test_server_preview_main_and_pip_show_the_exports_grey(engine, base_url, grey):  # noqa: F811
    ref = _decoded_grey(grey)
    sid = _project(base_url, grey)
    page = _open(engine, base_url, sid, 1440, 900)
    try:
        # The default preview is the server one (Settings > Instant preview Off).
        assert page.evaluate("window.__vaeTest.useStore.getState().previewEngine") != "client"
        page.evaluate("(t) => window.__vaeTest.useStore.getState().setPlayhead(t)", 1.0 + 0.25 / 30)
        page.wait_for_timeout(5000)
        box = page.locator('canvas[data-layer="stickers"]').first.locator("xpath=..")
        img = np.asarray(Image.open(io.BytesIO(box.screenshot())).convert("RGB")
                         .resize((W, H), Image.BILINEAR)).astype(float)
        Image.fromarray(img.astype(np.uint8)).save(SHOTS / f"final_qa_grey_{engine.engine_name}.png")
    finally:
        page.context.close()
    main = img[10:30, 10:30].mean()        # outside the 0.8 PiP: the main picture
    pip = img[170:190, 310:330].mean()     # the PiP's centre
    assert abs(main - pip) <= TOL, f"{engine.engine_name}: main {main:.1f} vs PiP {pip:.1f} (export {ref:.1f})"
    assert abs(main - ref) <= TOL, f"{engine.engine_name}: main {main:.1f} vs export {ref:.1f}"
    assert abs(pip - ref) <= TOL, f"{engine.engine_name}: PiP {pip:.1f} vs export {ref:.1f}"
