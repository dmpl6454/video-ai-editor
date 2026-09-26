"""P1-B1 bake splice in real WKWebView (INSTANT_PREVIEW_SPEC §13, §5.3, §7, R13).

A clip with a colour grade is BAKED in Phase 1. After the preview render of
that hash lands, the bake spans of exactly that range are appended over the
RAW client frames in the same SourceBuffer. Asserted per output frame, by
paused (k+0.5)/R seeks read through texImage2D(video):

* before the splice every k shows its RAW client frame (the bar = the
  program map's source frame, at the ungraded level);
* after it, the BAKED range shows the bake — the same source frame (bar) at
  the GRADED level — and every other k is untouched;
* the bake is its own init class (the project rate, 30, vs a 25 fps source),
  switched in by one init segment;
* no <video> src swap, no `emptied`, no `loadstart`.

Nothing is simulated: a real backend (uvicorn + the app + a scratch
WORKDIR) serves the page, the proxy, /frame_map, the preview render and the
bake routes.
"""
from __future__ import annotations

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
ENTRY = FRONTEND / "src" / "lib" / "preview" / "testkit" / "bakeSplicePage.ts"
SRC_ID = 5


@pytest.fixture(scope="module")
def splice_run(tmp_path_factory):
    exe = FRONTEND / "node_modules" / ".bin" / "esbuild"
    if not exe.exists():
        pytest.skip("frontend/node_modules not installed (no esbuild)")
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH")
    import json

    from video_ai_editor.ingest.proxy_queue import MANAGER

    root = tmp_path_factory.mktemp("wk-bake")
    static = root / "static"
    (static / "bundle").mkdir(parents=True)
    subprocess.run([str(exe), str(ENTRY), "--bundle", "--format=esm", "--target=safari16",
                    f"--outfile={static / 'bundle' / 'bakeSplicePage.js'}", "--log-level=warning"],
                   check=True, cwd=FRONTEND, capture_output=True, text=True)
    (static / "splice.html").write_text(module_page("bake splice", "bakeSplicePage.js", "runBakeSplice"))
    (root / "pages").mkdir()
    (root / "pages" / "go.html").write_text(GO_PAGE)

    backend = LiveBackend(root / "wd", static)
    pages = PageServer({"pages": root / "pages"})
    harness = WKHarness(pages, root / "runs")
    try:
        sid = backend.call("POST", "/api/sessions")["id"]
        master = encode_master(SourceSpec("S", SRC_ID, 960, 540, 150, "testsrc2", Fraction(25)),
                               root / "wd" / sid / "uploads" / "S" / "S.normalized.mp4")
        backend.call("POST", f"/api/sessions/{sid}/dispatch",
                     {"tool": "set_canvas", "args": {"w": 960, "h": 540, "fps": 30}})
        ids = []
        for start, a, b in ((0.0, 0.0, 1.5), (1.5, 2.0, 3.5), (3.0, 4.0, 5.0)):
            ans = backend.call("POST", f"/api/sessions/{sid}/dispatch?include=edl",
                               {"tool": "add_clip", "args": {"src": str(master), "track": "v1",
                                                             "in": a, "out": b, "start": start}})
            ids.append(ans["result"]["clip_id"])
        last = backend.call("POST", f"/api/sessions/{sid}/dispatch?include=edl",
                            {"tool": "color_grade", "args": {"clip_id": ids[1], "brightness": 0.2}})
        h = last["render_hash"]
        src = next(c["src"] for t in last["edl"]["tracks"] for c in t["clips"] if c["id"] == ids[1])
        fm = backend.call("GET", f"/api/sessions/{sid}/frame_map?h={h}")
        run_b = [r for r in fm["runs"] if r.get("clip_id") == ids[1]]
        k0, k1 = run_b[0]["k0"], run_b[-1]["k0"] + run_b[-1]["n"]
        # The proxy is built as imports build it; then the preview render lands.
        MANAGER.ensure(Path(src), sid=sid, eager=True)
        assert MANAGER.wait_idle(180)
        backend.call("POST", f"/api/sessions/{sid}/preview")
        assert (root / "wd" / sid / "previews" / f"{h}.mp4").is_file()
        (static / "config.json").write_text(json.dumps(
            {"sid": sid, "h": h, "src": src, "srcId": SRC_ID, "baked": [k0, k1]}))
        yield harness.run("pages/go.html", {"to": f"{backend.base}/wk/splice.html"}, timeout=300), (k0, k1)
    finally:
        harness.close()
        pages.close()
        backend.close()


def test_bake_frames_replace_exactly_the_baked_range(splice_run):
    run, (k0, k1) = splice_run
    r = run.result
    assert "AppleWebKit" in r["ua"] and "Chrome" not in r["ua"], r["ua"]
    assert (r["k0"], r["k1"]) == (k0, k1) and 0 < k0 < k1 < r["total"]
    exp = r["expected"]
    assert None not in exp
    # Before the render lands: RAW client frames everywhere, frame-exact.
    assert [x["code"] for x in r["before"]] == exp
    # After: still the same source frame at every k (R13: bake frame k is
    # output frame k) ...
    assert [x["code"] for x in r["after"]] == exp
    # ... but inside the BAKED range the picture is the server's graded one,
    # and outside it nothing moved.
    lift = [a["level"] - b["level"] for a, b in zip(r["after"], r["before"])]
    inside = lift[k0:k1]
    outside = lift[:k0] + lift[k1:]
    assert min(inside) > 20, inside
    assert max(abs(x) for x in outside) <= 2, outside
    print(f"graded lift inside the bake: min {min(inside):.1f}, outside max |Δ| "
          f"{max(abs(x) for x in outside):.2f} (green levels)")


def test_the_bake_is_its_own_init_class_and_no_src_swap_happens(splice_run):
    run, (k0, k1) = splice_run
    r = run.result
    assert r["bakeInitKey"] != r["proxyInitKey"]          # 30 fps bake vs 25 fps source
    assert r["bakeIndexInitKey"] == r["bakeInitKey"]      # index.json names the same class
    assert r["initsAppendedForSplice"] == 1
    assert r["bakeSize"] == [960, 540] and r["bakeFrames"] == r["total"]
    assert r["spansFetched"] == list(range(k0 // 60, (k1 - 1) // 60 + 1))
    assert r["srcUnchanged"] is True and r["emptied"] == 0 and r["loadstarts"] == 0
    assert run.errors == []
