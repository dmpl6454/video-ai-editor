"""The top bar's width budget in REAL WKWebView (LEFT_RAIL_SPEC §1.4, §8.3
case 3; phase R3).

``frontend/src/components/topbar/wkTopBarPage.tsx`` mounts the real TopBar
(so the real ``useTopBarFit``, ActivityChip and RatioMenu, styled by the real
``styles.css``) over a stubbed ``fetch``, seeds the spec's worst case — a long
project name, "Applying", a recording and a captions run, the .vae and MP4
links outdated, a long export error — and hosts the bar at 1440, 1280, 1024
and 900 px, then back at 1440. Which density fits is decided by text metrics,
and text metrics are the engine's: this pins that in the packaged app's engine
nothing clips, Export is the right-most control at width − 12, the activity
buttons stay inside the left group, the settled step is the lowest that fits,
and a return to 1440 does not keep a narrower step.
"""
from __future__ import annotations

import subprocess

import pytest

from .conftest import FRONTEND, _esbuild
from .harness import PageServer, WKHarness

pytestmark = pytest.mark.wk

ENTRY = FRONTEND / "src" / "components" / "topbar" / "wkTopBarPage.tsx"


def _baseline(width: int) -> int:
    return 0 if width >= 1440 else 1 if width >= 1280 else 2 if width >= 1100 else 3


@pytest.fixture(scope="module")
def topbar_page(tmp_path_factory):
    exe = _esbuild()
    if exe is None:
        pytest.skip("frontend/node_modules not installed (npm install), so no esbuild")
    out = tmp_path_factory.mktemp("wk-topbar")
    # CSS is bundled for real (the fit IS layout); the app's UI font is the
    # system one, so no font files are needed.
    subprocess.run(
        [str(exe), str(ENTRY), "--bundle", "--format=esm", "--target=safari16", "--jsx=automatic",
         "--define:process.env.NODE_ENV=\"production\"",
         f"--outfile={out / 'wkTopBarPage.js'}", "--log-level=warning"],
        check=True, cwd=FRONTEND, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert (out / "wkTopBarPage.css").exists()
    (out / "topbar.html").write_text(
        '<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="wkTopBarPage.css"><body></body>'
        '<script type="module" src="wkTopBarPage.js"></script>', encoding="utf-8")
    server = PageServer({"topbar": out})
    harness = WKHarness(server, out / "runs")
    yield harness
    harness.close()
    server.close()


def test_worst_case_top_bar_fits_in_wkwebview(topbar_page):
    r = topbar_page.run("topbar/topbar.html", timeout=60).result
    assert "error" not in r, r.get("error")
    assert "AppleWebKit" in r["ua"]
    rows = r["rows"]
    assert [m["width"] for m in rows] == [1440, 1280, 1024, 900, 1440]
    for m in rows:
        w = m["width"]
        assert m["barSw"] == m["barCw"] == w, m                   # the bar never overflows
        assert m["leftSw"] <= m["leftCw"], m                      # nor its left group
        assert m["exportL"] >= 0 and abs(m["exportR"] - (w - 12)) <= 0.5, m
        assert m["stopIn"] is True and m["cancelIn"] is True, m
        assert m["clipped"] == [], m
        assert m["chipW"] >= 80, m                                # only the name truncates, to its floor
        assert m["h1Display"] != "none", m
        assert _baseline(w) <= m["density"] <= 4, m
        if m["density"] > _baseline(w):
            assert m["lessFits"] is False, m                      # the lowest step that fits
        assert (m["actWords"] == "none") == (m["density"] == 4), m
        if m["fitsHalf"]:
            assert abs(m["trigMid"] - w / 2) <= 1, m
    # The worst case needs more than the baseline somewhere, and a return to
    # 1440 re-fits from 1440's baseline rather than keeping 900's step.
    assert any(m["density"] > _baseline(m["width"]) for m in rows), rows
    assert rows[-1]["density"] == rows[0]["density"], rows
    # Every word a step hides stays in a name.
    names = r["names"]
    assert names["vae"] == "Download the saved .vae project (outdated)"
    assert names["mp4"] == ["Save exported MP4 (outdated)"]
    assert names["error"].startswith("Export failed: the encoder ran out of disk space") and "RuntimeError" not in names["error"]
    assert names["ratio"] == "Canvas ratio: 9:16, 1080 by 1920, 30 fps"
