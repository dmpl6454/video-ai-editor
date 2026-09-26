"""The tool panels' AI deep links in REAL WKWebView (LEFT_RAIL_SPEC §2.5 M5,
§8.3 case 8; phase R5).

``frontend/src/components/rail/wkDeepLinkPage.tsx`` mounts the real ToolRail
and ToolPanel (so the real panels, AiPanel and AiToolCards, styled by the real
stylesheets) over a stubbed ``fetch`` and drives every deep link in the
product's engine. What is engine-dependent and pinned here:

- focus: a row pressed with focus on it lives in a panel that hides in the
  same commit; WebKit may leave ``activeElement`` on the hidden row, and the
  jump must still end on the card's toggle (and the back chip on the row);
- sticky metrics: the card lands just under the AI panel's sticky search head
  in the FIRST frame that paints, including the ones whose form only renders
  after the jump (cards near the end of the list);
- scroll: the back chip restores the origin panel's scroll after it was
  ``hidden`` (display: none) in between.
"""
from __future__ import annotations

import re
import subprocess

import pytest

from .conftest import FRONTEND, _esbuild
from .harness import PageServer, WKHarness

pytestmark = pytest.mark.wk

ENTRY = FRONTEND / "src" / "components" / "rail" / "wkDeepLinkPage.tsx"
GROUP_COUNTS = {"Find & search": 5, "Text & brand": 7, "Cutout & effects": 4, "Captions & speech": 10}


@pytest.fixture(scope="module")
def deeplink_page(tmp_path_factory):
    exe = _esbuild()
    if exe is None:
        pytest.skip("frontend/node_modules not installed (npm install), so no esbuild")
    out = tmp_path_factory.mktemp("wk-deeplinks")
    # CSS is bundled for real: the landing position IS layout (sticky head,
    # each tabpanel's own scroller).
    subprocess.run(
        [str(exe), str(ENTRY), "--bundle", "--format=esm", "--target=safari16", "--jsx=automatic",
         "--define:process.env.NODE_ENV=\"production\"",
         f"--outfile={out / 'wkDeepLinkPage.js'}", "--log-level=warning"],
        check=True, cwd=FRONTEND, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert (out / "wkDeepLinkPage.css").exists()
    (out / "deeplinks.html").write_text(
        '<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="wkDeepLinkPage.css"><body></body>'
        '<script type="module" src="wkDeepLinkPage.js"></script>', encoding="utf-8")
    server = PageServer({"deeplinks": out})
    harness = WKHarness(server, out / "runs")
    yield harness
    harness.close()
    server.close()


def _near_top(landed: dict) -> bool:
    return landed["gap"] is not None and (0 <= landed["gap"] <= 12 or (landed["atEnd"] and landed["gap"] >= 0))


def test_deep_links_in_wkwebview(deeplink_page):
    r = deeplink_page.run("deeplinks/deeplinks.html", timeout=90).result
    assert "AppleWebKit" in r["ua"]

    # Names are the catalogue labels; statuses are the cards' (a packaged app
    # without voice separation says "Not set up"; the TTS voice downloads).
    status = {row["key"]: row["status"] for row in r["rows"]}
    assert status["tool:vocal_isolate"] == "Not set up", r["rows"]
    assert status["tool:instrumental_isolate"] == "Not set up"
    assert status["tool:tts_voiceover"] == "Downloads 63 MB first"
    assert status["tool:diarize"] is None and status["tool:chroma_key"] is None
    assert len(r["rows"]) == 17

    # Case 8.
    c8 = r["case8"]
    assert r["searchBefore"] == "zzz no such tool"
    for when in ("firstFrame", "settled"):
        landed = c8[when]
        assert landed["aiSelected"] == "true" and landed["search"] == "", (when, landed)
        assert landed["controls"] == "ai-body-diarize" and landed["expanded"] == "true", (when, landed)
        assert landed["title"] == "Detect speakers", (when, landed)
        assert landed["chip"] == "Back to Captions", (when, landed)
        assert _near_top(landed), (when, landed)
    assert c8["back"]["focused"] == "tool:diarize" and c8["back"]["selected"] == "true", c8["back"]
    assert c8["back"]["chipAfter"] is False
    assert abs(c8["back"]["scroll"] - c8["ccScroll"]) <= 1 and c8["ccScroll"] > 0, c8
    assert abs(c8["originScroll"] - c8["ccScroll"]) <= 1, c8

    # Every row and group link, with and without focus on the row.
    assert len(r["all"]) == 17 + 4
    for j in r["all"]:
        assert not j.get("missing"), j
        first, settled = j["firstFrame"], j["settled"]
        for landed in (first, settled):
            assert landed["aiSelected"] == "true" and landed["search"] == "", j
            assert landed["chip"] == f"Back to {j['origin']}", j
            assert _near_top(landed), (j["key"], landed)
        if j["key"].startswith("tool:"):
            tool = j["key"][5:]
            assert settled["controls"] == f"ai-body-{tool}" and settled["expanded"] == "true", j
            assert settled["title"] == j["label"] and settled["hasBody"], j
            assert j["label"] in j["rowText"], j
        else:
            group = j["key"][6:]
            assert settled["tag"] == "H3" and settled["text"] == group, j
            assert settled["id"] == "ai-group-" + re.sub(r"\W+", "-", group).lower(), j
            assert j["rowText"] == f"All {group} tools ({GROUP_COUNTS[group]})", j
        assert j["back"]["focused"] == j["key"] and j["back"]["selected"] == "true", j
        assert j["back"]["chipAfter"] is False, j
        assert abs(j["back"]["scroll"] - j["originScroll"]) <= 1, j
    # The mirrored status is the card's own badge.
    vocal = next(j for j in r["all"] if j["key"] == "tool:vocal_isolate")
    assert "Not set up" in vocal["settled"]["badges"], vocal
