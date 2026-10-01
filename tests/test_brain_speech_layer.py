"""EB1-D: the speech layer (sentences, lexical fillers, false starts, repeats,
number class) measured against lane A's concat-offset truth."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain_analysis_fixtures as F  # noqa: E402

#: whisper.cpp ends words at plosive closures and starts them late; the
#: word-timing repairs (analysis/word_timing.py) bring sentence edges to the
#: voiced run's edges, measured worst case on TH: −0.22 s start, +0.21 s end.
START_TOL_S = 0.25
END_TOL_S = 0.30


def _layers(video: str, transcript_of: str | None = None, *, speakers: bool = False):
    from video_ai_editor.brain.analysis import audio as A
    from video_ai_editor.brain.analysis import pcm as P
    from video_ai_editor.brain.analysis import speakers as SK
    from video_ai_editor.brain.analysis import speech as S
    transcript = F.ensure_transcript(Path(transcript_of or video))
    pcm = P.read_pcm(video)
    audio = A.build_audio_layer(video, pcm=pcm)
    spk = SK.build_speakers_layer(pcm, audio, transcript=transcript) if speakers else None
    return S.build_speech_layer(transcript, audio_layer=audio, pcm=pcm, speakers_layer=spk), audio


def _norm(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z]+", text.lower()) if t not in ("um", "uh", "umm")}


@pytest.fixture(scope="module")
def th():
    return F.th_or_skip()


@pytest.fixture(scope="module")
def th_speech(th):
    return _layers(th.th.video_16x9)


def test_th_sentences_fillers_false_start_retake_ids_exact(th, th_speech):
    layer, truth = th_speech[0], th.th.truth
    sents = layer["sentences"]
    # ids exact: s_0001.. in order, one per planted sentence
    assert [s["id"] for s in sents] == [t.id for t in truth.sentences], [s["text"] for s in sents]
    for ts, s in zip(truth.sentences, sents):
        assert -START_TOL_S <= s["t0"] - ts.t0 <= START_TOL_S and -END_TOL_S <= s["t1"] - ts.t1 <= END_TOL_S, \
            (ts.id, s["t0"], ts.t0, s["t1"], ts.t1)
        a, b = _norm(s["text"]), _norm(ts.text)
        assert len(a & b) / max(1, len(a | b)) >= 0.7, (ts.id, s["text"], ts.text)   # digits/number words differ
    assert all(w["sent"] in {s["id"] for s in sents} and re.fullmatch(r"w_\d{4}_\d{2,}", w["id"]) for w in layer["words"])
    # every planted lexical filler is a `filler` word found by TIME; nothing else is
    fillers = [w for w in layer["words"] if w.get("filler")]
    for uid in truth.fillers_lexical:
        a, b = truth.utt(uid).voiced
        assert any(a - 0.1 <= (w["t0"] + w["t1"]) / 2 <= b + 0.1 for w in fillers), (uid, a, b)
    assert len(fillers) == len(truth.fillers_lexical), [(w["text"], w["t0"]) for w in fillers]
    # a leading filler is a word of its sentence but not part of its span
    for w in fillers:
        s = next(x for x in sents if x["id"] == w["sent"])
        assert w["t1"] <= s["t0"] + 0.05 or w["t0"] >= s["t1"] - 0.05, (w, s["id"])
    # no false start planted in TH; exactly one retake and the LATER rendering is the dup
    assert layer["flags"]["false_starts"] == []
    reps = layer["flags"]["repeats"]
    assert [(r["dup"], r["of"]) for r in reps] == [(truth.retake.dup, truth.retake.of)]
    assert reps[0]["similarity"] >= 0.8
    q = next(s for s in sents if s["id"] == truth.question)
    assert q["is_question"] and q["kind"] == "question"
    assert sum(1 for s in sents if s["is_question"]) == 1


def test_th_sentence_features_are_only_the_frozen_ones(th, th_speech):
    """C's `Features` forbids more; the layer validates through the store's model."""
    from video_ai_editor.brain import schema as S
    S.SpeechLayer.model_validate(th_speech[0])
    truth = th.th.truth
    quotable = next(s for s in th_speech[0]["sentences"] if s["id"] == truth.quotable)
    throwaway = next(s for s in th_speech[0]["sentences"] if s["id"] == truth.throwaway)
    assert quotable["features"]["strong_number"] and not throwaway["features"]["strong_number"]
    assert throwaway["features"]["weak_number"]


def test_p2_false_start_found_zero_false_positives():
    fx = F.p2_or_skip()
    layer, _ = _layers(fx.p2.recorder_wav, speakers=True)
    fs = layer["flags"]["false_starts"]
    truth = fx.p2.truth.false_start
    assert len(fs) == 1, fs
    assert abs(fs[0]["t0"] - truth.t0) <= 0.35 and abs(fs[0]["t1"] - truth.t1) <= 0.5, (fs, truth)
    kept = next(s for s in layer["sentences"] if s["id"] == fs[0]["kept"])
    assert kept["text"].lower().startswith("so the second")


def test_p2_camera_mic_transcript_gives_the_same_flags():
    """A real session's transcript of record is the v1 upload's — whisper over
    cam A's OWN microphone (−12 dB, a 40 ms reflection, room noise), moved to
    reference seconds. Measured: whisper then squeezes "second," (30 ms) and
    "money." (40 ms) onto the NEXT phrase's onset WITHOUT overlapping it; the
    repairs must still find the one false start and exactly the four "um"s."""
    from video_ai_editor.brain.analysis import audio as A
    from video_ai_editor.brain.analysis import pcm as P
    from video_ai_editor.brain.analysis import speakers as SK
    from video_ai_editor.brain.analysis import speech as S
    from video_ai_editor.brain.analysis.transcripts import to_reference
    fx = F.p2_or_skip()
    t = fx.p2.truth
    pcm = P.read_pcm(fx.p2.recorder_wav)
    audio = A.build_audio_layer(fx.p2.recorder_wav, pcm=pcm)
    tr = to_reference(F.ensure_transcript(fx.p2.cam_a), t.offsets["cam_a"], t.duration)
    spk = SK.build_speakers_layer(pcm, audio, transcript=tr)
    layer = S.build_speech_layer(tr, audio_layer=audio, pcm=pcm, speakers_layer=spk)
    fs = layer["flags"]["false_starts"]
    assert len(fs) == 1, fs
    assert abs(fs[0]["t0"] - t.false_start.t0) <= 0.35 and abs(fs[0]["t1"] - t.false_start.t1) <= 0.5, (fs, t.false_start)
    fillers = [w for w in layer["words"] if w.get("filler")]
    planted = [t.utt(u).voiced for u in t.fillers_lexical]
    assert len(fillers) == len(planted) == 4, [(w["text"], w["t0"]) for w in fillers]
    for (a, b), w in zip(planted, fillers):
        assert a - 0.1 <= (w["t0"] + w["t1"]) / 2 <= b + 0.1, (a, b, w)


def test_a_tiny_word_abutting_the_next_onset_after_a_gap_goes_back_to_its_own_run():
    """The measured shapes: "second," 76.81–76.84 before "so" 76.85 with its
    own run ending at 76.38; "money." 129.50–129.54 before "Money" 129.54."""
    from video_ai_editor.brain.analysis.word_timing import repair_word_times
    words = [{"text": "So", "t0": 75.3, "t1": 75.671}, {"text": "the", "t0": 75.671, "t1": 75.85},
             {"text": "second,", "t0": 76.81, "t1": 76.84}, {"text": "so", "t0": 76.85, "t1": 77.016},
             {"text": "the", "t0": 77.016, "t1": 77.276}, {"text": "second", "t0": 77.33, "t1": 77.794}]
    repair_word_times(words, [[75.3, 76.38], [76.76, 80.91]], {i: 0 for i in range(len(words))})
    assert words[2]["t1"] == 76.38 and 75.85 <= words[2]["t0"] < 76.38
    assert words[3]["t0"] == 76.76 and all(a["t1"] <= b["t0"] + 1e-9 for a, b in zip(words, words[1:]))
    # a short word that simply precedes its neighbour inside ONE run is left where whisper put it
    inside = [{"text": "on", "t0": 1.0, "t1": 1.3}, {"text": "a", "t0": 1.6, "t1": 1.63}, {"text": "boat.", "t0": 1.64, "t1": 2.0}]
    repair_word_times(inside, [[1.0, 2.0]], {0: 0, 1: 0, 2: 0})
    assert inside[1]["t0"] == 1.6


def test_leading_words_whisper_put_on_sound_are_not_moved_to_a_later_onset():
    """Measured on P2's recorder: whisper timed "all?" 136.65–136.81 at the
    START of the answer's run and the answer's own words 137.19–139.68 inside
    it; the leading-word repair then crammed the whole answer onto the next
    run's onset (141.5), leaving 2.8 s of speech with no word under it (which
    a planner reads as a silence). Words that already lie on sound stay."""
    from video_ai_editor.brain.analysis.word_timing import repair_word_times
    raw = [("at", 136.18, 136.32), ("all?", 136.65, 136.81), ("until", 137.19, 137.68), ("the", 137.68, 138.05),
           ("boat", 138.05, 138.73), ("test", 138.73, 139.32), ("proved", 139.32, 139.68), ("the", 140.21, 140.65),
           ("design.", 141.5, 141.52), ("What", 141.5, 141.9), ("is", 141.9, 142.03)]
    words = [{"text": t, "t0": a, "t1": b} for t, a, b in raw]
    sent_of = {0: 0, 1: 0, **{i: 1 for i in range(2, 9)}, 9: 2, 10: 2}
    repair_word_times(words, [[134.85, 136.14], [136.65, 139.65], [141.5, 143.06]], sent_of)
    by = {w["text"]: w for w in words[2:7]}
    assert by["until"]["t0"] <= 137.2 and by["proved"]["t1"] <= 139.65 + 1e-9, words
    assert all(136.65 <= w["t0"] and w["t1"] <= 139.65 + 1e-9 for w in words[2:7]), words
    assert words[9]["t0"] == 141.5
    # the case the repair exists for: leading words spread over the PAUSE before the onset are moved onto it
    lead = [{"text": "done.", "t0": 9.0, "t1": 9.4}, {"text": "The", "t0": 9.9, "t1": 10.3}, {"text": "point", "t0": 10.3, "t1": 10.9},
            {"text": "is", "t0": 11.2, "t1": 11.6}]
    repair_word_times(lead, [[8.0, 9.4], [11.0, 13.0]], {0: 0, 1: 1, 2: 1, 3: 1})
    assert lead[1]["t0"] >= 11.0 and lead[2]["t0"] >= 11.0, lead


def test_words_tile_the_sound_inside_a_voiced_run():
    """Measured on cam A's own microphone: whisper ends "iron." 0.79 s before
    the voice stops and leaves 0.46 s between "Not" and "until" inside ONE
    voiced run. The planner reads a word gap as a pause, so inside a run the
    words must cover the sound — except around a filler, whose own edges decide
    what a cut removes."""
    from video_ai_editor.brain.analysis.word_timing import repair_word_times
    runs = [[7.6, 12.28], [12.46, 14.0], [20.0, 23.0], [30.0, 32.0]]
    words = [{"text": "soldering", "t0": 10.6, "t1": 11.2}, {"text": "iron.", "t0": 11.2, "t1": 11.495},
             {"text": "Nobody", "t0": 12.46, "t1": 12.9},
             {"text": "Not", "t0": 20.0, "t1": 20.4}, {"text": "until", "t0": 20.86, "t1": 21.07},
             {"text": "Um,", "t0": 30.0, "t1": 30.3}, {"text": "I", "t0": 30.9, "t1": 31.0}]
    repair_word_times(words, runs, {0: 0, 1: 0, 2: 1, 3: 2, 4: 2, 5: 3, 6: 3})
    by = {w["text"]: w for w in words}
    assert by["iron."]["t1"] == 12.28 and by["Nobody"]["t0"] == 12.46
    assert by["Not"]["t1"] == by["until"]["t0"] == 20.86
    assert (by["Um,"]["t0"], by["Um,"]["t1"], by["I"]["t0"]) == (30.0, 30.3, 30.9)
    # more than TILE_MAX_S of unclaimed sound is not one word's: it stays unclaimed (and is flagged)
    far = [{"text": "first?", "t0": 1.0, "t1": 1.3}, {"text": "Because", "t0": 5.0, "t1": 5.4}]
    repair_word_times(far, [[1.0, 3.2], [5.0, 6.0]], {0: 0, 1: 1})
    assert far[0]["t1"] == 1.3


def test_sound_no_word_names_is_flagged_technical(th, th_speech):
    """A voiced stretch ≥ 0.3 s under no word and no acoustic filler is
    `unheard_voice` — speech whisper dropped (measured on cam A: the whole
    "Why the sensor first?") must never read as a silence. TH has none."""
    from video_ai_editor.brain.analysis.speech import unheard_voice
    assert th_speech[0]["flags"]["technical"] == []
    words = [{"t0": 33.0, "t1": 33.18}, {"t0": 35.6, "t1": 36.0}]
    got = unheard_voice(words, [{"t0": 36.2, "t1": 36.5}], [[33.0, 33.18], [33.7, 35.1], [35.6, 36.0], [36.2, 36.5]])
    assert got == [{"id": "x_0001", "t0": 33.7, "t1": 35.1, "why": "unheard_voice"}]


def test_a_gap_with_unheard_voice_in_it_is_not_dead_air():
    """Measured on P2: the guest's "…size of the body." | "Once the sensor…"
    are 1.4 s apart with the host's "Right." (whisper timed it 0.9 s late) in
    between — somebody is talking, so it is not dead air."""
    from video_ai_editor.brain.analysis.speech import _dead_air
    sents = [{"id": "s_0001", "spk": "S2", "t0": 38.1, "t1": 40.87}, {"id": "s_0002", "spk": "S2", "t0": 42.25, "t1": 46.0}]
    unheard = [{"id": "x_0001", "t0": 41.35, "t1": 41.72, "why": "unheard_voice"}]
    assert _dead_air(sents, [], True, unheard) == []
    assert [(d["t0"], d["t1"]) for d in _dead_air(sents, [], True, [])] == [(40.87, 42.25)]
    fx = F.p2_or_skip()
    layer, _ = _layers(fx.p2.recorder_wav, speakers=True)
    for d in layer["flags"]["dead_air"]:
        assert not any(min(d["t1"], x["t1"]) - max(d["t0"], x["t0"]) > 0 for x in layer["flags"]["technical"]), d


def test_a_repeat_across_a_sentence_end_is_not_a_stumble():
    from video_ai_editor.brain.analysis.speech import _mark_fillers
    words = [{"text": "money.", "t0": 0.0, "t1": 0.3, "spk": "S1"}, {"text": "Money", "t0": 0.35, "t1": 0.6, "spk": "S1"}]
    _mark_fillers(words, [])
    assert not any(w.get("filler") for w in words)


def test_dead_air_and_words_to_check(th, th_speech):
    layer = th_speech[0]
    # every planted 1.5 s pause is a silence between sentences, not dead air INSIDE a turn
    assert layer["flags"]["dead_air"] == []
    why = {w["why"] for w in layer["words_to_check"]}
    assert "digit" in why           # "2019", "40%"
    assert "low_prob" not in why    # prob is 1.0 on whisper.cpp this wave
    ids = {w["id"] for w in layer["words"]}
    assert all(c["word"] in ids for c in layer["words_to_check"])


def test_p2_in_turn_pause_is_dead_air_and_boundaries_are_not():
    """The 2.4 s pause inside the guest's turn is dead air; the 1.8 s
    turn-boundary silences and the 1.0 s emotional pause are not."""
    fx = F.p2_or_skip()
    layer, _ = _layers(fx.p2.recorder_wav, speakers=True)
    dead = layer["flags"]["dead_air"]
    by_kind: dict[str, list] = {}
    for p in fx.p2.truth.pauses:
        by_kind.setdefault(p.protection or p.kind, []).append(p)
    for p in by_kind["in_turn"]:
        if p.t1 - p.t0 >= 2.0:
            assert any(abs(d["t0"] - p.t0) <= 0.4 and abs(d["t1"] - p.t1) <= 0.4 for d in dead), (dead, p)
    for p in by_kind.get("turn_boundary", []) + by_kind.get("emotion", []):
        assert not any(d["t0"] < p.t1 - 0.2 and d["t1"] > p.t0 + 0.2 for d in dead), (p, dead)


def test_a_capital_after_a_leading_filler_is_not_a_name():
    from video_ai_editor.brain.analysis.speech import _assign_word_ids, _words_to_check
    words = [{"text": "Um,", "t0": 0.0, "t1": 0.3, "prob": 1.0, "sent": "s_0001", "filler": True, "id": "x"},
             {"text": "The", "t0": 0.4, "t1": 0.6, "prob": 1.0, "sent": "s_0001", "id": "y"},
             {"text": "Chicago", "t0": 0.6, "t1": 1.0, "prob": 1.0, "sent": "s_0001", "id": "z"}]
    _assign_word_ids([{"id": "s_0001"}], words)
    assert [w["id"] for w in words] == ["w_0001_00", "w_0001_01", "w_0001_02"]
    assert _words_to_check(words) == [{"word": "w_0001_02", "why": "capitalised_unknown"}]     # not "Um," and not "The"


def test_verbatim_repeat_is_a_stumble_only_within_one_speaker():
    from video_ai_editor.brain.analysis.speech import _mark_fillers
    same = [{"text": "the", "t0": 0.0, "t1": 0.2, "spk": "S1"}, {"text": "the", "t0": 0.25, "t1": 0.4, "spk": "S1"}]
    other = [{"text": "money.", "t0": 0.0, "t1": 0.3, "spk": "S1"}, {"text": "Money", "t0": 0.35, "t1": 0.6, "spk": "S2"}]
    _mark_fillers(same, [])
    _mark_fillers(other, [])
    assert same[0].get("filler") and not same[1].get("filler")
    assert not any(w.get("filler") for w in other)


@pytest.mark.parametrize("text,expected", [
    ("eight million", "strong"),
    ("we started in 2019", "weak"),
    ("with three people", "weak"),
    ("three times faster", "strong"),
    ("it locks focus in 80 milliseconds, the fastest we have ever tested", "strong"),
    ("cost me $30", "strong"),
    ("forty three lenses", "strong"),
    ("no numbers here", None),
])
def test_strong_vs_weak_number(text, expected):
    from video_ai_editor.brain.analysis.speech import number_class
    assert number_class(text) == expected


def test_a_restart_is_found_when_the_opener_is_on_one_side_only():
    """EB1 integration, measured in a real session: on cam A's normalised
    upload whisper wrote "the second … so the second test was a week" — the
    fragment lost its "So", the restart kept it. The first two tokens that
    carry the phrase are compared, openers aside."""
    from video_ai_editor.brain.analysis import speech as SP

    def w(text, t0, t1, sent="s_0001"):
        return {"id": f"w_{int(t0 * 100):05d}", "text": text, "t0": t0, "t1": t1, "spk": "S2", "sent": sent}
    said = [w("the", 75.30, 75.46), w("second", 75.46, 76.38),
            w("so", 76.76, 76.91), w("the", 76.91, 77.21), w("second", 77.21, 77.70), w("test", 77.70, 78.0),
            w("was", 78.0, 78.2), w("a", 78.2, 78.3), w("week", 78.3, 78.7)]
    got = SP._false_starts(said)
    assert len(got) == 1 and (got[0]["t0"], got[0]["t1"]) == (75.30, 76.38) and got[0]["text"] == "the second"
    # the opener on the fragment instead, and on both: the same one restart
    other = [w("so", 75.10, 75.30), *said[:2], *said[3:]]
    assert [f["text"] for f in SP._false_starts(other)] == ["so the second"]
    assert len(SP._false_starts([w("so", 75.10, 75.30), *said])) == 1
    # a phrase that merely starts with an opener and goes on differently is not a restart
    plain = [w("so", 1.0, 1.2), w("we", 1.2, 1.4), w("so", 1.8, 2.0), w("the", 2.0, 2.2), w("lens", 2.2, 2.6)]
    assert SP._false_starts(plain) == []
    assert SP._head(["so", "and", "the", "second", "test"]) == ["the", "second"] and SP._head(["so", "we"]) == ["so", "we"]
