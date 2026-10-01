"""`plan()` over the golden graphs equals the pinned EDPs byte for byte.

Regenerate with `python tests/gen_brain_goldens.py` only after bumping
`PLANNER_VERSION` — a golden that changes without a bump is a regression.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import gen_brain_goldens as G  # noqa: E402

from video_ai_editor.brain.planner import PLANNER_VERSION, plan  # noqa: E402

GRAPHS = G.golden_graphs()


@pytest.mark.parametrize("name", sorted(GRAPHS))
def test_plan_over_golden_graphs_is_byte_identical(name):
    golden = G.EDP_DIR / f"{name}.json"
    assert golden.exists(), f"missing {golden}: run tests/gen_brain_goldens.py"
    want = golden.read_bytes()
    got = G.canonical(plan(GRAPHS[name], G.CONTROLS.get(name, {}))).encode("utf-8")
    assert got == want, (f"{name}: plan() differs from the golden; if the change is intended bump PLANNER_VERSION "
                         f"and regenerate with tests/gen_brain_goldens.py")
    again = G.canonical(plan(GRAPHS[name], G.CONTROLS.get(name, {}))).encode("utf-8")
    assert again == got, "plan() is not pure"


def test_golden_carries_the_planner_version_and_reasons_with_facts():
    import json
    for name in GRAPHS:
        edp = json.loads((G.EDP_DIR / f"{name}.json").read_text(encoding="utf-8"))
        assert edp["planner_version"] == PLANNER_VERSION
        assert edp["graph"]["id"] == GRAPHS[name]["graph"]["id"]
        ids = _graph_ids(GRAPHS[name])
        for d in edp["decisions"]:
            r = d["reason"]
            assert r["code"] and r["text"], d
            assert all(f in ids for f in r["facts"]), (d["id"], r["facts"])
            if d["kind"] not in ("captions", "reframe", "export_preset", "music"):
                assert r["facts"], (d["id"], "a decision about the footage names the graph facts it read")


def _graph_ids(graph: dict) -> set[str]:
    L = graph["layers"]
    ids = {w["id"] for w in L["speech"]["words"]} | {s["id"] for s in L["speech"]["sentences"]}
    ids |= {t["id"] for t in L["speech"]["turns"]} | {a["id"] for a in L["speech"]["acoustic_fillers"]}
    for flag in L["speech"]["flags"].values():
        ids |= {f["id"] for f in flag}
    ids |= {s["id"] for s in L["audio"]["silences"]} | {sc["id"] for sc in graph["scenes"]}
    h = graph["graph"]
    ids |= {s["id"] for s in h["speakers"]} | {s["key"] for s in h["sources"]} | {t["id"] for t in h.get("topics", [])}
    ids |= {"music_hint", "content_type"}
    return ids
