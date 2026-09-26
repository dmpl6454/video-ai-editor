"""The top-bar activity chip, the Text/Captions panels' run and the rail dots
in REAL WKWebView (LEFT_RAIL_SPEC §2.8, §8.3 case 4; phase R2).

``frontend/src/components/topbar/wkActivityPage.tsx`` mounts the real
ActivityChip, ToolRail and ToolPanel (so the real VoRecorder, CaptionsPanel
and lib/captionRun) over a stubbed ``fetch`` and a stand-in for desktop.py's
native voiceover bridge — the record path the packaged app takes — and drives
the flows in the product's engine. What is engine-dependent and pinned here:
a take survives its panel being hidden (VoRecorder stays mounted in a
``hidden`` tabpanel inside a ``hidden`` section) and stays stoppable from the
chip; WebKit's focus return from the consent dialog; the chip's Stop / Cancel
being the panel's own stop / cancel; the live region speaking state changes
only.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from .conftest import FRONTEND, _esbuild
from .harness import PageServer, WKHarness

pytestmark = pytest.mark.wk

ENTRY = FRONTEND / "src" / "components" / "topbar" / "wkActivityPage.tsx"


@pytest.fixture(scope="module")
def activity_page(tmp_path_factory):
    exe = _esbuild()
    if exe is None:
        pytest.skip("frontend/node_modules not installed (npm install), so no esbuild")
    out = tmp_path_factory.mktemp("wk-activity")
    subprocess.run(
        [str(exe), str(ENTRY), "--bundle", "--format=esm", "--target=safari16", "--jsx=automatic",
         "--loader:.css=empty", "--define:process.env.NODE_ENV=\"production\"",
         f"--outfile={out / 'wkActivityPage.js'}", "--log-level=warning"],
        check=True, cwd=FRONTEND, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    (out / "activity.html").write_text(
        '<!doctype html><meta charset="utf-8"><body></body>'
        '<script type="module" src="wkActivityPage.js"></script>', encoding="utf-8")
    server = PageServer({"activity": out})
    harness = WKHarness(server, out / "runs")
    yield harness
    harness.close()
    server.close()


def test_activity_chip_in_wkwebview(activity_page):
    r = activity_page.run("activity/activity.html", timeout=60).result
    assert "AppleWebKit" in r["ua"]
    # The polite region exists from the first paint, empty.
    assert r["regionAtBoot"] == {"exists": True, "live": "polite", "text": ""}

    rec = r["recording"]
    assert rec["panelHidden"] is True, rec                 # Media selected and the panel collapsed
    assert rec["recorderStillMounted"] is True, rec        # the take survives its panel hiding
    assert rec["stopShown"] is True, rec                   # …and is stoppable from the chip
    assert rec["audioDot"] == "rail-desc-audio", rec
    assert rec["bodyLabel"].startswith("Recording voiceover, 0:0") and rec["bodyLabel"].endswith("Open the Audio panel")
    assert r["recordingStopped"] == {"vo": ["start", "stop"], "audioDot": None}

    consent = r["consent"]
    assert consent["badge"] == "Downloads 1.6 GB first"
    assert consent["text"] == ("The first run downloads the fast caption model (1.6 GB, once). "
                               "It stays on this Mac for next time. Captions start when it finishes.")
    assert consent["focusInside"] is True
    assert consent["focusBack"] == "Generate captions"     # WebKit returns focus to the trigger
    assert consent["dispatched"] == []                      # Cancel sends nothing

    cc = r["captions"]
    assert cc["cancelShown"] is True and cc["captionsDot"] == "rail-desc-captions", cc
    assert "42%" in cc["chipText"]
    s = cc["stopping"]
    assert "Stopping…" in s["chipText"] and s["cancelDisabled"] is True and s["cancels"] == 1, s
    assert "Stopping…" in s["panelText"], s                # one run: the panel agrees
    assert cc["after"] == {"captionsDot": None, "generateEnabled": True}
    assert r["dispatched"] == ["auto_caption"]
    assert r["live"] == ["Recording a voiceover", "Recording stopped", "Captions started", "Captions 25%",
                         "Stopping captions", "Captions cancelled"], r["live"]
