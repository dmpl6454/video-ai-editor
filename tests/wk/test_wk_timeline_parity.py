"""Timeline parity in real WKWebView (JavaScriptCore), spec §6 R2-R4, §13.

vitest proves ``frontend/src/lib/preview/timeline/`` against the goldens under
V8; the app runs JavaScriptCore. The exact arithmetic leans on engine details
(BigInt, typed-array bit access for a double's exact value, JSON float
parsing, stable sort), so the SAME comparisons run here, in a page built by
esbuild from the real modules and loaded in the WK harness:

* every timebase golden case;
* every output frame of every frame-map golden (decoded compositor renders);
* the RLE, seams and sound placement of every golden and of the 500-EDL
  frame-plan corpus;
* the §11.2 budget for a full ``programMap.build`` (12 min, 300 clips).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from .harness import PageServer, WKHarness

pytestmark = pytest.mark.wk

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
ENTRY = FRONTEND / "src" / "lib" / "preview" / "testkit" / "timelineParity.ts"
GOLDENS = REPO / "tests" / "goldens"

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>timeline parity</title></head>
<body style="margin:0"><script type="module">
import { runParity } from '/bundle/timelineParity.js'
const token = new URLSearchParams(location.search).get('token') || 'none'
const post = (body) => fetch('/__result/' + token, { method: 'POST', body: JSON.stringify(body) })
try { await post(await runParity('/goldens')) }
catch (e) { await post({ fatal: String((e && e.stack) || e) }) }
</script></body></html>
"""


@pytest.fixture(scope="module")
def parity_run(tmp_path_factory):
    exe = FRONTEND / "node_modules" / ".bin" / "esbuild"
    if not exe.exists():
        pytest.skip("frontend/node_modules not installed (no esbuild)")
    root = tmp_path_factory.mktemp("wk-parity")
    (root / "bundle").mkdir()
    (root / "pages").mkdir()
    subprocess.run([str(exe), str(ENTRY), "--bundle", "--format=esm", "--target=safari16",
                    f"--outfile={root / 'bundle' / 'timelineParity.js'}", "--log-level=warning"],
                   check=True, cwd=FRONTEND, capture_output=True, text=True)
    (root / "pages" / "parity.html").write_text(PAGE)
    server = PageServer({"pages": root / "pages", "bundle": root / "bundle", "goldens": GOLDENS})
    harness = WKHarness(server, root / "runs")
    try:
        yield harness.run("pages/parity.html", timeout=180)
    finally:
        harness.close()
        server.close()


def test_every_golden_matches_in_javascriptcore(parity_run):
    r = parity_run.result
    assert "AppleWebKit" in r["ua"] and "Chrome" not in r["ua"], r["ua"]
    assert r["mismatches"] == [], "\n".join(r["mismatches"])
    assert r["checks"]["timebase"] > 5000
    assert r["checks"]["frames"] > 20000
    assert r["checks"]["models"] >= 136
    # Wave D S1: speed curves (sqrt setpts) and freezes at five rates.
    assert r["checks"]["speedFrames"] > 2000
    assert r["checks"]["plans"] == 500
    assert parity_run.errors == []


def test_program_map_build_budget_in_javascriptcore(parity_run):
    """§11.2: programMap.build ≤ 4 ms full rebuild at 21,600 frames. Warm (the
    per-clip memo holds every clip) is the per-edit case; cold includes the
    first selection of all 300 clips and is reported, bounded loosely."""
    b = parity_run.result["bench"]
    assert b["frames"] == 21600
    assert b["warmMs"] <= 4.0, b
    assert b["coldMs"] < 250, b
