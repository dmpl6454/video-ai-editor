"""EB1-D: the heuristic scorers (§3.4) on the TH fixture and in isolation,
and model-off equivalence."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain_analysis_fixtures as F  # noqa: E402


@pytest.fixture(scope="module")
def th():
    return F.th_or_skip()


@pytest.fixture(scope="module")
def th_layers(th):
    from video_ai_editor.brain.analysis import audio as A
    from video_ai_editor.brain.analysis import pcm as P
    from video_ai_editor.brain.analysis import semantic as SEM
    from video_ai_editor.brain.analysis import speech as S
    path = th.th.video_16x9
    pcm = P.read_pcm(path)
    audio = A.build_audio_layer(path, pcm=pcm)
    speech = S.build_speech_layer(F.ensure_transcript(path), audio_layer=audio, pcm=pcm)
    semantic = SEM.build_semantic_layer(speech, audio, pcm=pcm, gateway=None)
    return speech, semantic, audio, pcm


def test_th_quotable_line_is_hook_top1_and_year_throwaway_below_by_0_2(th, th_layers):
    speech, semantic, _audio, _pcm = th_layers
    truth = th.th.truth
    scores = semantic["scores"]
    quotable, throwaway, closing = truth.quotable, truth.throwaway, truth.closing
    top = max(scores, key=lambda sid: scores[sid]["hook"])
    assert top == quotable, (scores[top], scores[quotable])
    assert scores[quotable]["hook"] - scores[throwaway]["hook"] >= 0.2
    assert scores[quotable]["quotable"] == 1.0
    assert scores[throwaway]["quotable"] < 0.5
    assert scores[quotable]["standalone"] >= 0.9
    assert "strong_number" in scores[quotable]["evidence"]["hook"]      # counted once, on the hook axis
    assert scores[closing]["hook"] == 0.0     # "thanks for watching" is not a candidate
    by_id = {s["id"]: s for s in speech["sentences"]}
    assert by_id[quotable]["features"]["strong_number"] and not by_id[throwaway]["features"]["strong_number"]
    assert by_id[throwaway]["features"]["weak_number"]
    # a greeting's vocative and a time adverbial are not claims
    assert not by_id["s_0001"]["features"]["claim"] and not by_id["s_0003"]["features"]["claim"]


def test_emphasis_sentence_has_rms_z_and_stretch(th, th_layers):
    """The +6 dB, 25 % slower sentence has rms_z >= 1.5 and stretch >= 1.1 and
    no other sentence has both (spec §3.4); its emotion clears 0.5."""
    from video_ai_editor.brain.analysis.delivery import sentence_inputs
    speech, semantic, audio, pcm = th_layers
    inputs = sentence_inputs(speech, audio, pcm)
    emph = th.th.truth.emphasis.sent
    assert inputs[emph]["rms_z"] >= 1.5 and inputs[emph]["stretch"] >= 1.1, inputs[emph]
    assert all(not (v["rms_z"] >= 1.5 and v["stretch"] >= 1.1) for k, v in inputs.items() if k != emph)
    assert semantic["scores"][emph]["emotion"] >= 0.5


def test_semantic_layer_is_the_frozen_shape(th, th_layers):
    from video_ai_editor.brain import schema as S
    _speech, semantic, _audio, _pcm = th_layers
    S.SemanticLayer.model_validate(semantic)
    assert set(semantic) == {"params", "scores", "annotations", "topics", "budget"}
    assert semantic["budget"] == {"calls": 0, "spent_s": 0.0, "partial_from": None}


def test_flat_delivery_ranks_below_emphatic_twin():
    from video_ai_editor.brain.analysis.semantic import score_sentence
    base = {"id": "s_0001", "text": "The best lens is the one you already own.", "is_question": False,
            "kind": "statement", "complete": True, "weak_start": False, "answer_of": None,
            "features": {"wpm": 150, "fillers": 0, "strong_number": False, "weak_number": False, "claim": True,
                         "conclusion_marker": False, "story_marker": False, "contrast_words": 0,
                         "anaphora_start": False, "len_words": 9, "rms_z": 0.0, "pitch_range_st": 1.0,
                         "stretch": 1.0, "imperative": False, "conjunction_start": False, "repeat": False,
                         "false_start": False, "filler_rate": 0.0, "answer_len_norm": 0.0, "topic_peak": False}}
    flat = score_sentence(base)
    emphatic = dict(base, features=dict(base["features"], rms_z=1.6, pitch_range_st=6.0))
    strong = score_sentence(emphatic)
    assert strong["hook"] > flat["hook"]
    assert strong["emotion"] > flat["emotion"]
    assert flat["hook"] <= 0.7 * (strong["hook"] + 1e-9) or strong["hook"] - flat["hook"] >= 0.1


def test_model_off_equals_heuristic_byte_for_byte(th, th_layers):
    from video_ai_editor.brain.analysis import semantic as SEM
    from video_ai_editor.brain.gateway import Gateway
    speech, heuristic, audio, pcm = th_layers

    class Dead:
        id = "apple_intelligence"
        engine_version = "test-dead"

        def probe(self):
            return {"available": True, "detail": "stub"}

        def run(self, payload, *, timeout_s):
            return None          # a stubbed / timed-out model

    stubbed = SEM.build_semantic_layer(speech, audio, pcm=pcm, gateway=Gateway([Dead()]))
    assert SEM.canonical_content(stubbed) == SEM.canonical_content(heuristic)
    assert stubbed["annotations"] == [] and SEM.scored_by(stubbed) == SEM.scored_by(heuristic)
    assert SEM.layer_status(stubbed) == "partial" and stubbed["budget"]["calls"] >= 1
    assert stubbed["budget"]["partial_from"] is not None
    assert SEM.layer_status(heuristic) == "ok" and heuristic["budget"]["calls"] == 0
    assert set(SEM.scored_by(stubbed).values()) == {"recipes"}
