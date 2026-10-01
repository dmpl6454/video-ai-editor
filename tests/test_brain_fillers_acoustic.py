"""EB1-D: the acoustic filler detector finds the planted `uh` islands by TIME.

Lane A's `uh` is a formant-synthesised schwa (0.24 s, −10 dB, 0.4 s of
silence before and 0.5 s after), which whisper never writes down; the six
`um`/`umm` are Piper renderings whisper does write ("Um,"). The speech layer
therefore lists exactly the three schwas as `acoustic_fillers` and marks the
six `um`s as `filler` WORDS — an island a filler word already covers is that
word's cut, never a second one (the planner emits one decision per word and
one per island)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain_analysis_fixtures as F  # noqa: E402

TOL_S = 0.06


@pytest.fixture(scope="module")
def th():
    return F.th_or_skip()


@pytest.fixture(scope="module")
def th_speech(th):
    from video_ai_editor.brain.analysis import audio as A
    from video_ai_editor.brain.analysis import pcm as P
    from video_ai_editor.brain.analysis import speech as S
    path = th.th.video_16x9
    pcm = P.read_pcm(path)
    audio = A.build_audio_layer(path, pcm=pcm)
    return S.build_speech_layer(F.ensure_transcript(path), audio_layer=audio, pcm=pcm), audio, pcm


def test_th_three_uh_found_by_time_within_60ms_zero_false_positives(th, th_speech):
    layer, _audio, _pcm = th_speech
    found = layer["acoustic_fillers"]
    truth = th.th.truth
    planted = [truth.utt(u).voiced for u in truth.fillers_acoustic]
    assert len(planted) == 3
    for a, b in planted:
        hit = [f for f in found if abs(f["t0"] - a) <= TOL_S and abs(f["t1"] - b) <= TOL_S]
        assert len(hit) == 1, (a, b, [(f["t0"], f["t1"]) for f in found])
        assert hit[0]["confidence"] >= 0.7, hit[0]
        assert set(hit[0]["evidence"]) >= {"pitch_range_st", "flux", "word_overlap"}
    assert len(found) == 3, [(f["t0"], f["t1"]) for f in found]      # zero false positives
    assert [f["id"] for f in found] == ["af_0001", "af_0002", "af_0003"]


def test_lexical_fillers_are_words_not_second_decisions(th, th_speech):
    layer, _audio, _pcm = th_speech
    truth = th.th.truth
    words = [w for w in layer["words"] if w.get("filler")]
    assert len(words) == len(truth.fillers_lexical) == 6
    for f in layer["acoustic_fillers"]:
        assert not any(min(w["t1"], f["t1"]) - max(w["t0"], f["t0"]) > 0.5 * (f["t1"] - f["t0"]) for w in words), f


def test_raw_detector_confidence_is_1_for_a_heard_filler(th, th_speech):
    """Straight from the detector: a planted `um` whisper heard AND the
    islands test accepts is reported at confidence 1.0 (`evidence.lexical`);
    the schwas, which whisper did not hear, are scored by the three tests."""
    from video_ai_editor.brain.analysis import fillers as FL
    from video_ai_editor.brain.analysis import pcm as P
    layer, audio, pcm = th_speech
    words = [{"t0": w["t0"], "t1": w["t1"], "text": w["text"]} for w in layer["words"]]
    found = FL.acoustic_fillers(pcm, P.SR, vad=audio["vad"], words=words)
    truth = th.th.truth
    lexical = [truth.utt(u).voiced for u in truth.fillers_lexical]
    heard = [f for f in found if f["evidence"]["lexical"] == 1.0]
    assert heard and all(f["confidence"] == 1.0 for f in heard)
    for f in heard:
        mid = (f["t0"] + f["t1"]) / 2
        assert any(a - 0.1 <= mid <= b + 0.1 for a, b in lexical), f
    schwa = [f for f in found if f["evidence"]["lexical"] == 0.0]
    assert len(schwa) == 3 and all(0.7 <= f["confidence"] < 1.0 for f in schwa)
