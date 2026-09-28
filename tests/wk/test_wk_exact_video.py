"""Final QA r2 (export-truth), real WKWebView: the server preview's picture
through the colour-exact presenter (lib/videoColour `createExactPresenter`,
which Preview.tsx lays over the <video> in WebKit) is the export's grey.

WebKit presents an untagged video colour-managed (~1.96 gamma): measured in
Playwright WebKit, a flat 100-grey main picture read 109 on screen while the
same grey as a PiP — drawn through the WebGL copy — read 98, the export's
value. Here the served preview.mp4 of that grey is read back in the product's
engine both ways: through the presenter (must be the ffmpeg decode, ±2) and
through a plain 2D drawImage (reported for the record).
"""
from __future__ import annotations

import json
import shutil
import subprocess

import numpy as np
import pytest
from pathlib import Path

from .harness import PageServer, WKHarness
from .live_backend import GO_PAGE, LiveBackend, module_page

pytestmark = pytest.mark.wk

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
ENTRY = FRONTEND / "src" / "lib" / "preview" / "testkit" / "wkExactVideoPage.ts"
W, H = 640, 360


def _decoded_grey(path: Path) -> float:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", "1.0", "-i", str(path), "-frames:v", "1",
                          "-vf", "format=rgb24", "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    a = np.frombuffer(raw, np.uint8)
    return float(a.mean())


@pytest.fixture(scope="module")
def exact_run(tmp_path_factory):
    exe = FRONTEND / "node_modules" / ".bin" / "esbuild"
    if not exe.exists():
        pytest.skip("frontend/node_modules not installed (no esbuild)")
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH")
    root = tmp_path_factory.mktemp("wk-exact")
    static = root / "static"
    (static / "bundle").mkdir(parents=True)
    subprocess.run([str(exe), str(ENTRY), "--bundle", "--format=esm", "--target=safari16",
                    f"--outfile={static / 'bundle' / 'wkExactVideoPage.js'}", "--log-level=warning"],
                   check=True, cwd=FRONTEND, capture_output=True, text=True)
    (static / "exact.html").write_text(module_page("exact video", "wkExactVideoPage.js", "runExactVideo"))
    (root / "pages").mkdir()
    (root / "pages" / "go.html").write_text(GO_PAGE)
    backend = LiveBackend(root / "wd", static)
    pages = PageServer({"pages": root / "pages"})
    harness = WKHarness(pages, root / "runs")
    try:
        sid = backend.call("POST", "/api/sessions")["id"]
        grey = root / "wd" / sid / "uploads" / "G" / "grey.normalized.mp4"
        grey.parent.mkdir(parents=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=0x646464:s={W}x{H}:r=30:d=3",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-colorspace", "bt709", str(grey)], check=True)
        backend.call("POST", f"/api/sessions/{sid}/dispatch",
                     {"tool": "set_canvas", "args": {"w": W, "h": H, "fps": 30}})
        backend.call("POST", f"/api/sessions/{sid}/dispatch",
                     {"tool": "add_clip", "args": {"src": str(grey), "track": "v1", "in": 0.0, "out": 3.0,
                                                   "start": 0.0}})
        prev = backend.call("POST", f"/api/sessions/{sid}/preview")
        url = prev.get("url") or f"/api/sessions/{sid}/preview.mp4"
        (static / "config.json").write_text(json.dumps({"url": url}))
        preview_file = next((root / "wd" / sid).rglob("*.mp4"), None)
        ref = _decoded_grey(grey)
        result = harness.run("pages/go.html", {"to": f"{backend.base}/wk/exact.html"}, timeout=120).result
        yield result, ref, preview_file
    finally:
        harness.close()
        pages.close()
        backend.close()


def test_the_exact_presenter_shows_the_exports_grey_in_wkwebview(exact_run):
    r, ref, _ = exact_run
    print(json.dumps({"exact_video": r, "ffmpeg": ref}))
    assert not r.get("fatal"), r.get("fatal")
    assert "AppleWebKit" in r["ua"] and "Chrome" not in r["ua"], r["ua"]
    assert r["errors"] == [] and r["readyState"] >= 2, r
    assert r["presenter"] and r["drew"], r
    assert all(abs(v - ref) <= 2 for v in r["exact"]), (r["exact"], ref)
