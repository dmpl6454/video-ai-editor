"""`brain/schema.py` — the frozen Content Graph / EDP shapes (EB1 lane C).

Pins: the hand-built graph and EDP validate; an extra key anywhere is
refused (`extra="forbid"` is the wire rule); `reason.facts` must resolve to
ids in the graph the EDP names; times are monotone and 4-decimal; ids
reference what exists; the canonical digest is stable across key order.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent))
import brain_contract_fixtures as BF  # noqa: E402

from video_ai_editor.brain import schema as S  # noqa: E402

SRC = "/tmp/talking_head.mp4"


def _edp() -> dict:
    return BF.edp(SRC)


# ---------------------------------------------------------------- shapes

def test_edp_and_graph_examples_validate_and_reject_extra_keys():
    edp = S.EDP.model_validate(_edp())
    assert edp.id == BF.DID and len(edp.decisions) == 9 and edp.decisions[4].kind == "open_on"
    graph = S.Graph.model_validate(BF.graph_header(SRC))
    assert graph.id == BF.GID and graph.sources[0].key == BF.SRC_KEY
    S.SpeechLayer.model_validate(BF.speech_layer())
    S.AudioLayer.model_validate(BF.audio_layer())
    S.SpeakersLayer.model_validate(BF.speakers_layer())
    S.SemanticLayer.model_validate(BF.semantic_layer())
    S.Scenes.model_validate(BF.scenes())
    S.Angles.model_validate(BF.angles(SRC))
    # extra keys: top level, a decision, a reason, a layer word, the graph header
    with pytest.raises(ValidationError):
        S.EDP.model_validate({**_edp(), "mood": "x"})
    bad = _edp()
    bad["decisions"][0]["colour"] = "red"
    with pytest.raises(ValidationError):
        S.EDP.model_validate(bad)
    bad = _edp()
    bad["decisions"][0]["reason"]["why"] = "because"
    with pytest.raises(ValidationError):
        S.EDP.model_validate(bad)
    sp = BF.speech_layer()
    sp["words"][0]["confidence"] = 0.5
    with pytest.raises(ValidationError):
        S.SpeechLayer.model_validate(sp)
    with pytest.raises(ValidationError):
        S.Graph.model_validate({**BF.graph_header(SRC), "notes": []})


def test_closed_enums_and_id_patterns():
    bad = _edp()
    bad["decisions"][0]["kind"] = "delete_everything"
    with pytest.raises(ValidationError):
        S.EDP.model_validate(bad)
    bad = _edp()
    bad["decisions"][0]["reason"]["code"] = "vibes"
    with pytest.raises(ValidationError):
        S.EDP.model_validate(bad)
    ok = _edp()
    ok["decisions"][0]["reason"]["code"] = "pause_kept:laughter"
    S.EDP.model_validate(ok)
    for field, value in (("id", "p_deadbeef"), ("id", "d_XYZ"), ("brain", "gpt"), ("seed", 1.5)):
        with pytest.raises(ValidationError):
            S.EDP.model_validate({**_edp(), field: value})
    bad = _edp()
    bad["decisions"][0]["id"] = "k_1"
    with pytest.raises(ValidationError):
        S.EDP.model_validate(bad)
    assert set(S.REASON_CODES) >= {"silence", "filler", "filler_acoustic", "false_start", "dead_air", "best_window",
                                   "duration_fit", "hook_strongest_opening", "speaker_turn", "at_cut",
                                   "jump_cut_hide", "emphasis_peak", "hook_emphasis", "caption_mode",
                                   "music_mood", "dialogue_lane", "control"}
    assert set(S.DECISION_KINDS) == {"keep_window", "cut_range", "keep_pause", "open_on", "switch_angle", "punch_in",
                                     "jump_cut_hide", "captions", "music", "reframe", "export_preset", "dialogue"}


# ---------------------------------------------------------------- times + references

def test_monotone_times_and_four_decimals():
    bad = _edp()
    bad["decisions"][0]["ref"] = {"src": BF.SRC_KEY, "t0": 0.9, "t1": 0.6}
    with pytest.raises(ValidationError):
        S.EDP.model_validate(bad)
    fine = _edp()
    fine["decisions"][0]["ref"] = {"src": BF.SRC_KEY, "t0": 0.123456789, "t1": 0.98765432}
    e = S.EDP.model_validate(fine)
    assert (e.decisions[0].ref.t0, e.decisions[0].ref.t1) == (0.1235, 0.9877)
    sp = BF.speech_layer()
    sp["sentences"][0]["t1"] = sp["sentences"][0]["t0"]
    with pytest.raises(ValidationError):
        S.SpeechLayer.model_validate(sp)
    au = BF.audio_layer()
    au["silences"][0]["t0"], au["silences"][0]["t1"] = 5.5, 2.8
    with pytest.raises(ValidationError):
        S.AudioLayer.model_validate(au)


def test_layer_id_references_must_exist():
    sp = BF.speech_layer()
    sp["words"][0]["sent"] = "s_9999"
    with pytest.raises(ValidationError):
        S.SpeechLayer.model_validate(sp)
    sp = BF.speech_layer()
    sp["flags"]["repeats"] = [{"id": "r_0001", "dup": "s_0003", "of": "s_4242", "similarity": 0.9}]
    with pytest.raises(ValidationError):
        S.SpeechLayer.model_validate(sp)
    sp = BF.speech_layer()
    sp["flags"]["repeats"] = [{"id": "r_0001", "dup": "s_0003", "of": "s_0002", "similarity": 0.9}]
    S.SpeechLayer.model_validate(sp)


def test_reason_facts_must_resolve_in_graph():
    ids = S.graph_ids(S.Graph.model_validate(BF.graph_header(SRC)),
                      {"speech": S.SpeechLayer.model_validate(BF.speech_layer()),
                       "audio": S.AudioLayer.model_validate(BF.audio_layer()),
                       "speakers": S.SpeakersLayer.model_validate(BF.speakers_layer()),
                       "semantic": S.SemanticLayer.model_validate(BF.semantic_layer())},
                      scenes=S.Scenes.model_validate(BF.scenes()))
    assert {"w_0002", "s_0002", "sil_0001", "u_0001", "S1", "t_001", "sc_0003", BF.SRC_KEY, "music_hint"} <= ids
    edp = S.EDP.model_validate(_edp())
    assert S.unresolved_facts(edp, ids) == []
    bad = _edp()
    bad["decisions"][1]["reason"]["facts"] = ["w_0006", "w_4242"]
    missing = S.unresolved_facts(S.EDP.model_validate(bad), ids)
    assert missing == [("k_0002", "w_4242")]
    with pytest.raises(ValueError, match="k_0002.*w_4242"):
        S.check_facts(S.EDP.model_validate(bad), ids)


# ---------------------------------------------------------------- digest + caps

def test_canonical_digest_is_key_order_independent_and_stable():
    a = S.EDP.model_validate(_edp())
    shuffled = json.loads(json.dumps(_edp(), sort_keys=True))
    shuffled["decisions"] = [dict(reversed(list(d.items()))) for d in shuffled["decisions"]]
    b = S.EDP.model_validate(shuffled)
    assert S.canonical_json(a) == S.canonical_json(b)
    assert S.digest(a) == S.digest(b) and S.digest(a).startswith("sha256:") and len(S.digest(a)) == 71
    c = S.EDP.model_validate({**_edp(), "seed": 1})
    assert S.digest(c) != S.digest(a)
    text = S.canonical_json(a)
    assert "\n" not in text and '"t0": ' not in text          # compact separators
    assert '"t0":0.6' in text and "0.60000" not in text        # 4-decimal times, no float noise


def test_size_caps_are_the_spec_numbers():
    assert S.MAX_LAYER_BYTES == 32 * 1024 * 1024
    assert S.MAX_GRAPH_BYTES == 64 * 1024 * 1024


def test_golden_graphs_from_lane_a_validate_when_present():
    root = Path(__file__).parent / "goldens" / "brain" / "graphs"
    files = sorted(root.glob("*.json")) if root.exists() else []
    if not files:
        pytest.skip("lane A's golden graphs are not on disk yet")
    for p in files:
        data = json.loads(p.read_text(encoding="utf-8"))
        S.Graph.model_validate(data if "sources" in data else data["graph"])


def test_the_wave_leaves_the_frozen_constants_where_they_were():
    """EB1 finalize: the flag defaults OFF and the render version is the committed 32 (the settled rules: this wave
    changes no renderable field, so no cached render is invalidated)."""
    from video_ai_editor import brain, brain_setting
    from video_ai_editor.edl import schema as edl_schema
    assert brain_setting.DEFAULT_ENABLED is False
    assert edl_schema.RENDER_BEHAVIOR_VERSION == 32
    assert brain.PLANNER_VERSION == 2 and brain.EDP_VERSION == 1 and brain.ANALYSIS_VERSION == 1
