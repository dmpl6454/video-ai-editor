"""The AI panel's Cut to the beat default minimum shot IS the recipe's.

Final sweep 2: the form fell back to the tool's min_shot default (0) and left
2-frame flash shots at the old clip edges, while the Prompt-bar recipe passes
heuristics.MIN_SHOT_S. aiCatalog.ts now carries BEAT_MIN_SHOT_S; this pins the
two numbers together so neither drifts alone.
"""
from __future__ import annotations

import re
from pathlib import Path

from video_ai_editor.agent.prompt.heuristics import MIN_SHOT_S

CATALOG = Path(__file__).resolve().parents[1] / "frontend" / "src" / "lib" / "aiCatalog.ts"


def test_ai_panel_min_shot_matches_the_recipe():
    m = re.search(r"export const BEAT_MIN_SHOT_S = ([0-9.]+)", CATALOG.read_text(encoding="utf-8"))
    assert m, "aiCatalog.ts lost BEAT_MIN_SHOT_S"
    assert float(m.group(1)) == MIN_SHOT_S
    assert MIN_SHOT_S > 0
