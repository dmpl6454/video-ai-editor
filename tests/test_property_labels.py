"""set_property's History / MCP / chat summary is editor language (QA-101-SWEEP).

It was `f"Set {cid}.{path} = {value!r}"`, so History read "Edit — Set.reverse
= True", "Set.style.background = '#000000B3'": a property path and a Python
repr. The table is shared with lib/opLabels.propertyLabel through
frontend/src/lib/__fixtures__/property_labels.json — both sides run every case.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import dispatch, property_label
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Sticker, TextClip, empty_edl  # noqa: F401

FIXTURE = Path(__file__).parent.parent / "frontend/src/lib/__fixtures__/property_labels.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=[f"{c['path']}={c['value']!r}" for c in CASES])
def test_every_path_reads_as_editor_language(case):
    assert property_label(case["path"], case["value"]) == (case["group"], case["phrase"])


def test_a_real_set_property_op_summary_has_no_path_or_repr(tmp_path):
    sd = tmp_path / "s"
    sd.mkdir()
    (sd / "edl.json").write_text(empty_edl().model_dump_json())
    s = EDLStore(sd)
    r = dispatch(s, "add_text", {"text": "HELLO", "start": 0, "end": 2})
    tid = next(c.id for t in s.edl.tracks for c in t.clips if isinstance(c, TextClip))
    edits = [("style.size", 120), ("style.background", "#000000B3"), ("style.shadow_on", True),
             ("style.align", "left"), ("style.upper", True), ("style.letter_spacing", 20)]
    for path, value in edits:
        r = dispatch(s, "set_property", {"clip_id": tid, "path": path, "value": value})
        assert "=" not in r["summary"] and "style." not in r["summary"] and tid not in r["summary"], r
    ops = [o for o in s.ops.ops if o.tool == "set_property"]
    assert [o.summary for o in ops] == [
        "Text style: Text size 120 px", "Text style: Background box on", "Text style: Shadow on",
        "Text style: Alignment: left", "Text style: Letter case: ALL CAPS", "Text style: Tracking 20 px"]
    assert not any(re.search(r"'[^']*'|\bTrue\b|\bNone\b", o.summary) for o in ops)
