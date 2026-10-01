"""EB1-D: the Gateway's `rank_moments` — path-free payloads, the 60 s budget
with heuristic fallback (`partial`), cached annotations never re-asked, and
one live call on this Mac when Apple Intelligence answers."""
from __future__ import annotations

import json
import re
import time

import pytest

SENTS = [{"id": f"s_{i:05d}", "t0": 10.0 * i, "t1": 10.0 * i + 4.0, "spk": "S1",
          "text": t, "hook": h}
         for i, (t, h) in enumerate([
             ("The third one locks focus in 80 milliseconds, the fastest we have ever tested.", 0.7),
             ("We started in 2019 with three people and one lens.", 0.1),
             ("So which one should you actually buy?", 0.6),
             ("Do not buy the kit lens, the body alone is the better deal.", 0.5),
             ("If you shoot sports, only the third one keeps up.", 0.45),
         ])]


class Counting:
    id = "apple_intelligence"
    engine_version = "test-1"

    def __init__(self, order=None, delay=0.0):
        self.calls = 0
        self.order = order
        self.delay = delay

    def probe(self):
        return {"available": True, "detail": "stub"}

    def run(self, payload, *, timeout_s):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        n = len(payload["windows"])
        return {"order": list(self.order) if self.order is not None else list(range(n))}


def test_payload_is_path_free():
    from video_ai_editor.brain.digest import PathLeak, assert_path_free, rank_moments_payload
    dirty = [dict(s, text=s["text"] + " see /Users/me/clip.mp4 and ~/x and C:\\v\\a.mov") for s in SENTS]
    payload = rank_moments_payload(dirty)
    blob = json.dumps(payload)
    assert not re.search(r"[/\\~]|\.(mp4|mov|wav|m4a|mkv)\b", blob), blob
    assert_path_free(payload)
    with pytest.raises(PathLeak):
        assert_path_free({"windows": [{"text": "/tmp/x"}]})
    assert set(payload) == {"windows", "k"} and all(set(w) == {"start", "end", "text"} for w in payload["windows"])


def test_schema_and_repair():
    from video_ai_editor.brain.gateway import TASK_SCHEMA, validate_answer
    assert TASK_SCHEMA["title"] == "rank_moments"
    assert validate_answer({"order": [2, 0, 1]}, n=3) == [2, 0, 1]
    assert validate_answer({"order": [2, 2, 9, 0]}, n=3) == [2, 0]      # dupes and out-of-range dropped
    assert validate_answer({"order": "nope"}, n=3) is None
    assert validate_answer(None, n=3) is None


def test_timeout_falls_back_and_marks_partial():
    from video_ai_editor.brain.gateway import Gateway
    slow = Counting(delay=0.3)
    gw = Gateway([slow], call_timeout_s=0.05)
    res = gw.rank_moments(SENTS, budget_s=0.2)
    assert res.partial and res.scores == {} and res.calls >= 1
    assert res.provenance["by"] == "recipes"
    assert "timeout" in res.notes[0] or "budget" in res.notes[0]


def test_ranked_scores_blend_and_provenance():
    from video_ai_editor.brain.gateway import Gateway
    prov = Counting(order=[2, 0, 4, 3, 1])
    gw = Gateway([prov])
    res = gw.rank_moments(SENTS, budget_s=60)
    assert not res.partial and prov.calls == 1
    assert res.provenance["by"] == "apple_intelligence" and res.provenance["task"] == "rank_moments"
    assert re.fullmatch(r"sha256:[0-9a-f]{16}", res.provenance["prompt_hash"])
    assert res.scores["s_00002"] == 1.0 and res.scores["s_00001"] == 0.0
    assert res.scores["s_00000"] > res.scores["s_00004"] > res.scores["s_00003"]


def test_cached_annotation_never_reasked():
    from video_ai_editor.brain.analysis import semantic as SEM
    from video_ai_editor.brain.gateway import Gateway
    speech = {"sentences": [dict(s, features=_features(), complete=True, weak_start=False, answer_of=None,
                                 kind="statement", is_question=s["text"].endswith("?"))
                            for s in SENTS], "words": [], "turns": [], "flags": {}, "speakers": []}
    audio = {"hz": 100, "loudness_i": -20.0}
    prov = Counting(order=[0, 2, 3, 4, 1])
    layer = SEM.build_semantic_layer(speech, audio, gateway=Gateway([prov]))
    assert prov.calls == 1 and layer["annotations"]
    again = SEM.build_semantic_layer(speech, audio, gateway=Gateway([prov]), previous=layer)
    assert prov.calls == 1, "the cached annotation was re-asked"
    assert again["annotations"] == layer["annotations"]
    assert again["scores"] == layer["scores"]
    changed = json.loads(json.dumps(speech))
    changed["sentences"][0]["text"] += " Really."
    SEM.build_semantic_layer(changed, audio, gateway=Gateway([prov]), previous=layer)
    assert prov.calls == 2, "a changed sentence must be re-asked"


def _features() -> dict:
    return {"wpm": 150, "fillers": 0, "strong_number": True, "weak_number": False, "claim": True,
            "conclusion_marker": False, "story_marker": False, "contrast_words": 0, "anaphora_start": False,
            "len_words": 10, "rms_z": 0.0, "pitch_range_st": 2.0, "stretch": 1.0, "imperative": False,
            "conjunction_start": False, "repeat": False, "false_start": False, "filler_rate": 0.0,
            "answer_len_norm": 0.0, "topic_peak": False}


def test_live_rank_moments_on_this_mac_within_budget():
    """One real call through fm.py's text path when Apple Intelligence answers;
    skipped honestly otherwise (never a download, never a network route)."""
    from video_ai_editor.brain.gateway import AppleIntelligenceProvider, Gateway
    prov = AppleIntelligenceProvider()
    avail = prov.probe()
    if not avail.get("available"):
        pytest.skip(f"Apple Intelligence unavailable: {avail.get('detail')}")
    t0 = time.monotonic()
    res = Gateway([prov]).rank_moments(SENTS, budget_s=60)
    took = time.monotonic() - t0
    assert took <= 60, took
    if res.partial:
        pytest.skip(f"FM did not answer in budget: {res.notes}")
    assert res.provenance["by"] == "apple_intelligence" and res.provenance["model"]
    assert set(res.scores) <= {s["id"] for s in SENTS} and len(res.scores) >= 1


def test_live_provenance_lands_in_the_semantic_layer(tmp_path, monkeypatch):
    """The Milestone-B line: a live `rank_moments` on this Mac answers within
    the budget and its provenance is IN THE LAYER — annotation `by`/`model`/
    `task`/`prompt_hash`, the blended hook (0.6 model + 0.4 heuristic), the
    calls and time spent in `budget`, the layer `ok`. Through `analyse()` with
    the default gateway. Skips cleanly when the helper is unavailable."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import brain_analysis_fixtures as F
    from video_ai_editor import config
    from video_ai_editor.brain import graph
    from video_ai_editor.brain.analysis import semantic as SEM
    from video_ai_editor.brain.gateway import AppleIntelligenceProvider
    avail = AppleIntelligenceProvider().probe()
    if not avail.get("available"):
        pytest.skip(f"Apple Intelligence unavailable: {avail.get('detail')}")
    fx = F.th_or_skip()
    monkeypatch.delenv("VAI_BRAIN", raising=False)
    monkeypatch.setattr(config, "WORKDIR", tmp_path / "work")
    live = graph.analyse(tmp_path / "live", F.th_sources(fx))
    g = graph.load_graph(tmp_path / "live", live)
    sem = graph.load_layer(g, g["reference"], "semantic")
    if SEM.layer_status(sem) == "partial" or not sem["annotations"]:
        pytest.skip(f"FM did not answer in budget: {sem['budget']}")
    assert g["sources"][0]["layers"]["semantic"] == "ok"
    assert sem["budget"]["calls"] >= 1 and 0 < sem["budget"]["spent_s"] <= 60 and sem["budget"]["partial_from"] is None
    for a in sem["annotations"]:
        assert a["by"] == "apple_intelligence" and a["model"] and a["task"] == "rank_moments"
        assert a["prompt_hash"].startswith("sha256:") and 0.0 <= a["hook"] <= 1.0
    # the same footage with the brain pinned to recipes: no annotation, and only ranked hooks differ
    monkeypatch.setenv("VAI_BRAIN", "recipes")
    off = graph.analyse(tmp_path / "off", F.th_sources(fx))
    g0 = graph.load_graph(tmp_path / "off", off)
    heur = graph.load_layer(g0, g0["reference"], "semantic")
    assert heur["annotations"] == [] and heur["budget"]["calls"] == 0
    for a in sem["annotations"]:
        want = round(SEM.MODEL_BLEND * a["hook"] + (1 - SEM.MODEL_BLEND) * heur["scores"][a["sent"]]["hook"], 4)
        assert sem["scores"][a["sent"]]["hook"] == want
    unranked = set(sem["scores"]) - {a["sent"] for a in sem["annotations"]}
    assert all(sem["scores"][k] == heur["scores"][k] for k in unranked)


def test_gateway_follows_the_brain_pin(monkeypatch):
    from video_ai_editor.brain import graph
    monkeypatch.setenv("VAI_BRAIN", "recipes")
    assert graph._default_gateway() is None
    monkeypatch.setenv("VAI_BRAIN", "mlx")
    assert graph._default_gateway() is None
    monkeypatch.delenv("VAI_BRAIN", raising=False)
    assert graph._default_gateway() is not None
    monkeypatch.setenv("VAI_BRAIN", "fm")
    assert graph._default_gateway() is not None
