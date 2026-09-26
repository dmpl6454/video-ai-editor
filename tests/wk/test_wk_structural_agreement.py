"""P1-S1 structural agreement in real WKWebView (INSTANT_PREVIEW_SPEC §13, §8.2,
R14): for each of 200 committed edits, the client program map equals the
server's `/frame_map` of the same render hash — 0 mismatches.

Nothing is simulated. A REAL backend (the FastAPI app under uvicorn, a
scratch WORKDIR, ffmpeg-made long-GOP masters of three sizes and two rates)
serves the page, so the page's fetches are same-origin exactly as in the
app. The page (frontend/src/lib/preview/testkit/structuralPage.ts, bundled
from the real modules) makes each edit with `POST /dispatch?include=edl`,
builds its own map from the EDL that answer carried, and runs
`verify/divergence.ts`, which fetches `/frame_map?h=` and compares.

The harness's own page server only holds the result mailbox and a one-line
redirect onto the backend's origin.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from .harness import PageServer, WKHarness
from .live_backend import GO_PAGE, LiveBackend, module_page

pytestmark = pytest.mark.wk

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
ENTRY = FRONTEND / "src" / "lib" / "preview" / "testkit" / "structuralPage.ts"
EDITS = 200

MASTERS = (  # name, rate, w, h, frames
    ("A", "30", 1280, 720, 240),
    ("B", "25", 640, 360, 150),
    ("C", "30000/1001", 720, 1280, 120),
)


def _master(path: Path, rate: str, w: int, h: int, frames: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=size={w}x{h}:rate={rate}", "-frames:v", str(frames),
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)],
                   check=True)
    return path


@pytest.fixture(scope="module")
def structural_run(tmp_path_factory):
    exe = FRONTEND / "node_modules" / ".bin" / "esbuild"
    if not exe.exists():
        pytest.skip("frontend/node_modules not installed (no esbuild)")
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH")
    from video_ai_editor.ingest import proxy as P
    from video_ai_editor.render import frame_map as FM

    root = tmp_path_factory.mktemp("wk-structural")
    static = root / "static"
    (static / "bundle").mkdir(parents=True)
    subprocess.run([str(exe), str(ENTRY), "--bundle", "--format=esm", "--target=safari16",
                    f"--outfile={static / 'bundle' / 'structuralPage.js'}", "--log-level=warning"],
                   check=True, cwd=FRONTEND, capture_output=True, text=True)
    (static / "structural.html").write_text(
        module_page("structural agreement", "structuralPage.js", "runStructural"))
    (root / "pages").mkdir()
    (root / "pages" / "go.html").write_text(GO_PAGE)

    backend = LiveBackend(root / "wd", static)
    pages = PageServer({"pages": root / "pages"})
    harness = WKHarness(pages, root / "runs")
    try:
        sid = backend.call("POST", "/api/sessions")["id"]
        backend.call("POST", f"/api/sessions/{sid}/dispatch",
              {"tool": "set_canvas", "args": {"w": 1280, "h": 720, "fps": 30}})
        sources: dict[str, dict] = {}
        start = 0.0
        for name, rate, w, h, frames in MASTERS:
            m = _master(root / "wd" / sid / "uploads" / name / f"{name}.normalized.mp4", rate, w, h, frames)
            ans = backend.call("POST", f"/api/sessions/{sid}/dispatch?include=edl",
                        {"tool": "add_clip", "args": {"src": str(m), "track": "v1", "in": 0.2,
                                                      "out": 1.6, "start": start}})
            start += 1.4
            stored = [c["src"] for t in ans["edl"]["tracks"] for c in t["clips"]
                      if Path(c["src"]).resolve() == m.resolve()][0]
            sources[stored] = FM.SourceInfo.from_proxy(P.probe_source(m)).to_json()
        (static / "config.json").write_text(json.dumps(
            {"sid": sid, "edits": EDITS, "seed": 20260926, "sources": sources}))
        run = harness.run("pages/go.html", {"to": f"{backend.base}/wk/structural.html"}, timeout=600)
        yield run
    finally:
        harness.close()
        pages.close()
        backend.close()


def test_client_map_equals_frame_map_for_200_edits(structural_run):
    r = structural_run.result
    assert "AppleWebKit" in r["ua"] and "Chrome" not in r["ua"], r["ua"]
    assert r["applied"] == EDITS, r
    # Every edit reached a verdict, and every verdict was agreement.
    assert r["mismatches"] == [], json.dumps(r["mismatches"], indent=1)[:4000]
    assert r["others"] == [], r["others"]
    assert r["outcomes"] == {"match": EDITS}, r["outcomes"]
    # The corpus really exercised the structural edits of Phase 1.
    for tool in ("add_clip", "split_at", "trim_clip", "move_clip", "ripple_delete", "undo", "redo",
                 "set_speed", "set_clip_reverse"):
        assert r["byTool"].get(tool, 0) > 0, (tool, r["byTool"], r["rejected"])
    assert r["clipsMax"] >= 6 and r["framesMax"] >= 200, r
    # Negative control: one altered frame in the last map is caught and
    # demoted, against the same live route, so "0 mismatches" means something.
    assert r["control"]["outcome"] == "mismatch", r["control"]
    assert len(r["control"]["demote"]) == 1 and r["control"]["demote"][0][1] - r["control"]["demote"][0][0] == 1
    assert structural_run.errors == []


def test_divergence_check_cost_on_loopback(structural_run):
    """§8.2: the check never blocks display; its whole round trip (fetch of
    /frame_map + compare) is reported here, bounded loosely for a loaded
    machine."""
    ms = structural_run.result["checkMs"]
    print(f"divergence check round trip: p50 {ms['p50']:.1f} ms, p95 {ms['p95']:.1f} ms, "
          f"max {ms['max']:.1f} ms")
    assert ms["p95"] < 1000, ms
