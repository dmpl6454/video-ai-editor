"""P1-R1 first bullet, end to end in real WKWebView (INSTANT_PREVIEW_SPEC §7,
§13): with MediaSource deleted the app decides SERVER mode, and the server's
preview.mp4 then PLAYS (review RD3 — test_wk_robustness pointed at this file,
which did not exist, so only the decision half was tested).

A real backend (uvicorn + the app + a scratch WORKDIR) renders a real preview
of a real session; the page (frontend/src/lib/preview/testkit/
wkFallbackPage.ts) deletes MediaSource and ManagedMediaSource, resolves the
mode with the app's own modules, and plays /api/sessions/{sid}/preview.mp4 in
a <video>: currentTime advances and frames are presented.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from .harness import PageServer, WKHarness
from .live_backend import GO_PAGE, LiveBackend, module_page
from .proxy_fixture import SourceSpec, encode_master

pytestmark = pytest.mark.wk

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
ENTRY = FRONTEND / "src" / "lib" / "preview" / "testkit" / "wkFallbackPage.ts"


@pytest.fixture(scope="module")
def fallback_run(tmp_path_factory):
    exe = FRONTEND / "node_modules" / ".bin" / "esbuild"
    if not exe.exists():
        pytest.skip("frontend/node_modules not installed (no esbuild)")
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH")
    root = tmp_path_factory.mktemp("wk-fallback")
    static = root / "static"
    (static / "bundle").mkdir(parents=True)
    subprocess.run([str(exe), str(ENTRY), "--bundle", "--format=esm", "--target=safari16",
                    f"--outfile={static / 'bundle' / 'wkFallbackPage.js'}", "--log-level=warning"],
                   check=True, cwd=FRONTEND, capture_output=True, text=True)
    (static / "fallback.html").write_text(module_page("fallback preview", "wkFallbackPage.js", "runFallbackPreview"))
    (root / "pages").mkdir()
    (root / "pages" / "go.html").write_text(GO_PAGE)
    backend = LiveBackend(root / "wd", static)
    pages = PageServer({"pages": root / "pages"})
    harness = WKHarness(pages, root / "runs")
    try:
        sid = backend.call("POST", "/api/sessions")["id"]
        master = encode_master(SourceSpec("S", 5, 960, 540, 150, "testsrc2", Fraction(30)),
                               root / "wd" / sid / "uploads" / "S" / "S.normalized.mp4")
        backend.call("POST", f"/api/sessions/{sid}/dispatch",
                     {"tool": "set_canvas", "args": {"w": 640, "h": 360, "fps": 30}})
        backend.call("POST", f"/api/sessions/{sid}/dispatch",
                     {"tool": "add_clip", "args": {"src": str(master), "track": "v1", "in": 0.0, "out": 4.0,
                                                   "start": 0.0}})
        prev = backend.call("POST", f"/api/sessions/{sid}/preview")
        url = prev.get("url") or f"/api/sessions/{sid}/preview.mp4"
        (static / "config.json").write_text(json.dumps({"url": url}))
        yield harness.run("pages/go.html", {"to": f"{backend.base}/wk/fallback.html"}, timeout=120).result
    finally:
        harness.close()
        pages.close()
        backend.close()


def test_media_source_deleted_is_server_mode_and_the_server_preview_plays(fallback_run):
    r = fallback_run
    assert not r.get("fatal"), r.get("fatal")
    assert "AppleWebKit" in r["ua"] and "Chrome" not in r["ua"], r["ua"]
    assert r["had"]["mse"] and r["gone"] and r["caps"]["mse"] is False, r
    assert all(d["mode"] == "server" for d in r["decisions"]), r["decisions"]
    assert {d["engine"]: d["reason"] for d in r["decisions"]} == {"auto": "no-mse", "client": "no-mse",
                                                                   "server": "setting"}, r["decisions"]
    # the server's render plays
    assert r["errors"] == [], r
    assert (r["w"], r["h"]) == (640, 360), r
    assert r["currentTime"] >= 1.0 and not r["paused"], r
    if r["hasRvfc"]:
        assert r["presented"] >= 10, r
