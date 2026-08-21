"""Cue building — the properties that make captions match the speech.

These are the measurable claims. Numbers in the comments come from 25s of real
Hinglish speech (a dense podcast clip whose largest internal pause is 0.08s),
measured before and after the phrase-first rewrite:

    cues 11 -> 7 | shortest 0.40s -> 2.12s | longest 5.86s -> 4.80s
    under 1s 5 -> 0 | over 5s 2 -> 0 | peak reading speed 35 -> 21.8 CPS
    boundaries that missed an available pause: 0
"""
import pytest

from video_ai_editor.ingest.caption_format import (
    Cue, build_cues, cues_from_segments, resegment_at_pauses,
)


def words(spec: list[tuple[str, float, float]]) -> list[dict]:
    return [{"word": w, "start": s, "end": e} for w, s, e in spec]


def speech(text: str, *, start: float = 0.0, wdur: float = 0.3, gap: float = 0.0):
    """Evenly-timed words, optionally separated by a fixed pause."""
    out, t = [], start
    for tok in text.split(" "):
        out.append({"word": tok, "start": t, "end": t + wdur})
        t += wdur + gap
    return out


def chars(c: Cue) -> int:
    return len(c.text.replace("\n", " "))


def cps(c: Cue) -> float:
    return chars(c) / max(0.01, c.end - c.start)


# --- the bug that made reading speed WORSE ---------------------------------

def test_reading_speed_is_fixed_by_holding_the_cue_not_by_splitting_it():
    """The old builder split a cue whose CPS was too high. Splitting cannot
    reduce chars-per-second — both halves stay just as dense and now flicker —
    and it measurably produced a 0.40s cue at 35 CPS while enforcing a 17 CPS
    limit. Dense speech followed by silence must yield ONE cue held longer."""
    ws = speech("this is a dense burst of speech", start=0.0, wdur=0.12)
    # ...then a long silence before the next utterance.
    ws += [{"word": "later", "start": 12.0, "end": 12.4}]
    cues = build_cues(ws, max_cps=17.0)
    burst = cues[0]
    assert "this is a dense burst of speech" == burst.text.replace("\n", " ")
    assert cps(burst) <= 17.0 + 0.01, f"{cps(burst):.1f} CPS — was not held long enough"
    # Held into the silence, but not over the next cue's first word.
    assert burst.end <= 12.0


def test_a_cue_is_never_extended_over_the_next_cues_speech():
    ws = speech("one two three four five six seven eight", wdur=0.1)
    cues = build_cues(ws, max_chars=12, max_lines=1, min_dur=2.0)
    for a, b in zip(cues, cues[1:]):
        assert a.end <= b.start, f"{a.text!r} runs into {b.text!r}"


# --- boundaries follow the speech -----------------------------------------

def test_boundaries_land_on_pauses_when_the_speaker_pauses():
    a = speech("first phrase here", start=0.0, wdur=0.3)
    b = speech("second phrase here", start=2.0, wdur=0.3)   # 0.8s pause before
    cues = build_cues(a + b, max_chars=42)
    assert len(cues) == 2
    assert cues[0].text.replace("\n", " ") == "first phrase here"
    assert cues[1].text.replace("\n", " ") == "second phrase here"
    assert cues[0].start == 0.0 and cues[1].start == 2.0


def test_sentence_punctuation_ends_a_cue():
    ws = words([("Done.", 0.0, 0.5), ("Next", 0.55, 0.9), ("one", 0.95, 1.3)])
    cues = build_cues(ws)
    assert cues[0].text.startswith("Done.")
    assert len(cues) == 2


def test_a_long_phrase_with_no_pause_is_split_at_its_largest_internal_gap():
    """Continuous speech offers no clean boundary, so pick the best one there
    is rather than wherever the character budget ran out."""
    first = speech("alpha bravo charlie delta", start=0.0, wdur=0.3)
    # A 0.25s gap — under gap_break, so this is ONE phrase, but it is still the
    # most natural place to break it. max_chars must be wide enough to ALLOW
    # that cut (the first half is 25 chars); a tighter budget legitimately
    # forces an earlier break, which is the budget's fault, not the chooser's.
    second = speech("echo foxtrot golf hotel", start=1.45, wdur=0.3)
    cues = build_cues(first + second, max_chars=26, max_lines=1)
    assert len(cues) == 2
    assert cues[0].text == "alpha bravo charlie delta"
    assert cues[1].text == "echo foxtrot golf hotel"


# --- hard bounds -----------------------------------------------------------

def test_no_cue_exceeds_max_dur():
    """A chunk that fits the character budget can still be too SLOW. Checking
    only characters let an 82-char/5.86s cue through a 5.0s cap, and the packer
    never revisits an emitted chunk."""
    ws = speech(" ".join(f"w{i}" for i in range(40)), wdur=0.35)
    cues = build_cues(ws, max_dur=5.0)
    assert cues, "no cues produced"
    for c in cues:
        assert c.end - c.start <= 5.0 + 0.01, f"{c.end - c.start:.2f}s cue: {c.text!r}"


def test_no_flicker_cues_when_there_is_room_to_hold_them():
    ws = speech("hi", wdur=0.2)
    ws += [{"word": "much", "start": 9.0, "end": 9.3}]
    cues = build_cues(ws, min_dur=1.0)
    assert cues[0].end - cues[0].start >= 1.0


def test_a_cue_starts_and_ends_on_its_own_words():
    ws = speech("exactly these words", start=3.25, wdur=0.4, gap=0.05)
    cues = build_cues(ws)
    assert cues[0].start == pytest.approx(3.25)
    # The end may be extended into silence, never pulled in before the audio.
    assert cues[0].end >= ws[-1]["end"]


def test_char_budget_is_respected_across_both_lines():
    ws = speech(" ".join(f"word{i}" for i in range(30)), wdur=0.25)
    for c in build_cues(ws, max_chars=20, max_lines=2):
        for line in c.text.split("\n"):
            assert len(line) <= 20, f"line too long: {line!r}"
        assert c.text.count("\n") <= 1


# --- line wrapping ---------------------------------------------------------

def test_the_line_break_prefers_a_pause_over_the_middle_of_the_string():
    """A line break is a reading cue, so it belongs where the speaker paused —
    the old middle-of-string split cut phrases like `STD / hi hua tha` apart."""
    # 0.25s pause: under gap_break (0.35), so this stays ONE phrase and the
    # pause is available as a LINE break rather than a cue break. At 0.85 the
    # gap would be 0.45 and the phrase would split into two cues instead.
    ws = (speech("aa bb", start=0.0, wdur=0.2)
          + speech("cccccccc dddddddd", start=0.65, wdur=0.2))
    cue = build_cues(ws, max_chars=20, max_lines=2)[0]
    assert cue.text == "aa bb\ncccccccc dddddddd"


# --- degenerate input ------------------------------------------------------

def test_empty_and_blank_input():
    assert build_cues([]) == []
    assert build_cues([{"word": "  ", "start": 0.0, "end": 1.0}]) == []


def test_a_single_word_longer_than_the_budget_terminates():
    """Must not loop forever trying to fit it."""
    ws = [{"word": "x" * 200, "start": 0.0, "end": 1.0},
          {"word": "y", "start": 1.1, "end": 1.4}]
    cues = build_cues(ws, max_chars=10, max_lines=1)
    assert cues and any("x" * 10 in c.text for c in cues)


def test_words_without_timing_from_an_imported_srt_still_build_cues():
    segs = [{"start": 0.0, "end": 3.0, "text": "one two three four"}]
    cues = cues_from_segments(segs)
    assert cues
    assert "one" in cues[0].text


def test_segments_with_word_timing_are_preferred_over_synthesis():
    segs = [{"start": 0.0, "end": 2.0, "text": "alpha beta",
             "words": [{"word": "alpha", "start": 0.0, "end": 0.4},
                       {"word": "beta", "start": 1.6, "end": 2.0}]}]
    cues = cues_from_segments(segs)
    assert cues[0].start == 0.0
    assert cues[-1].end >= 2.0


# --- the shape of the real measurement ------------------------------------

def test_dense_real_world_speech_produces_readable_cues():
    """Reproduces the measured clip's shape: ~20s of continuous speech whose
    largest internal pause is 0.08s, in two bursts separated by 3.3s."""
    ws = speech(" ".join(f"shabd{i}" for i in range(46)), start=0.0, wdur=0.34, gap=0.08)
    ws += speech(" ".join(f"baat{i}" for i in range(12)), start=22.0, wdur=0.3, gap=0.05)
    cues = build_cues(ws)
    assert cues
    durs = [c.end - c.start for c in cues]
    assert min(durs) >= 1.0, f"flicker cue: {min(durs):.2f}s"
    assert max(durs) <= 5.01, f"over-long cue: {max(durs):.2f}s"
    for a, b in zip(cues, cues[1:]):
        assert a.end <= b.start + 1e-6
    # Every word survives, in order — captions must not drop speech.
    joined = " ".join(c.text.replace("\n", " ") for c in cues)
    assert joined.split() == [w["word"] for w in ws]


# --- resegment_at_pauses: the translate-pivot sync bug ---------------------
#
# Reproduces a real measured clip: one Whisper/batched-decode segment spanning
# 131.34s-176.16s (44.8s) with a 14.6s silent gap inside it (a scene cut
# mid-dialogue). Translation strips word timing (see
# dispatch._translate_segments_to), and cues_from_segments' fallback then
# spreads pseudo-words EVENLY across a segment's full span — including 14.6s
# of dead air — which is what "captions don't match the speech" measured as.
# resegment_at_pauses must run BEFORE translation discards the real timing.

def _bb_segment() -> dict:
    ws = (
        words([("Say", 131.34, 131.76), ("my", 131.76, 131.9), ("name.", 131.9, 132.16),
               ("I", 132.36, 132.8)])
        # 3.04s real gap
        + words([("don't", 135.84, 135.98), ("have", 135.98, 136.04), ("a", 136.04, 136.1),
                  ("clue", 136.1, 136.56)])
        # 14.59s real gap — the scene cut
        + words([("Say", 163.61, 164.05), ("my", 164.05, 164.41), ("name.", 164.41, 164.99)])
        # 10.39s real gap
        + words([("Goddamn", 175.38, 175.82), ("right.", 175.82, 176.16)])
    )
    text = " ".join(w["word"] for w in ws)
    return {"start": 131.34, "end": 176.16, "text": text, "words": ws}


def test_resegment_splits_a_long_segment_at_its_real_silent_gaps():
    out = resegment_at_pauses([_bb_segment()], gap_break=0.35)
    # 4, not 5: splitting is gap-only now (see test below) — "Say my name. I"
    # stays ONE chunk because the 0.2s gap after "name." is ordinary speech,
    # not a pause. The split lands after "I" (3.04s real gap), after "clue"
    # (14.59s — the scene cut), and after the second "name." (10.39s).
    assert len(out) == 4
    assert [round(s["start"], 2) for s in out] == \
        [131.34, 135.84, 163.61, 175.38]
    assert [round(s["end"], 2) for s in out] == \
        [132.8, 136.56, 164.99, 176.16]
    # No piece's span reaches into the gap it was split away from.
    for a, b in zip(out, out[1:]):
        assert a["end"] <= b["start"]


def test_resegment_does_not_split_on_sentence_punctuation_alone():
    """The actual regression in the first version of this fix: reusing the
    caption-cue splitter (which ends a phrase on '.' regardless of gap size)
    cut "I" away from "don't have a clue" over a mere 0.2s gap — ordinary
    speech, not a pause — producing a context-free one-word translation unit.
    Real translator behaviour measured directly: "I don't have a clue"
    translates to a coherent 'मैं नहीं हूँ'; "I" alone comes back as literally
    'I', untranslated. Splitting on silence only keeps "Say my name. I"
    together as one chunk instead of orphaning "I" on its own."""
    out = resegment_at_pauses([_bb_segment()], gap_break=0.35)
    assert out[0]["text"] == "Say my name. I"


def test_resegment_never_synthesizes_time_across_a_silence_after_translation():
    """The actual failure mode: pretend-translate (swap text, keep timing
    untouched — exactly what ai.translate.translate_segments does), strip
    words the way _translate_segments_to does, then build cues. Without
    resegmenting first, a single 44.8s cue set would spread text across the
    14.6s gap; with it, no cue may straddle that gap."""
    pieces = resegment_at_pauses([_bb_segment()], gap_break=0.35)
    translated = [{**p, "text": f"tx:{i}"} for i, p in enumerate(pieces)]
    stripped = [{k: v for k, v in s.items() if k != "words"} for s in translated]
    cues = cues_from_segments(stripped)
    assert cues
    for c in cues:
        # A cue may not span the 14.59s silent gap between 136.56 and 163.61.
        assert not (c.start < 136.56 and c.end > 163.61), \
            f"cue spans the silence: {c.start}-{c.end}"


def test_resegment_leaves_a_gapless_segment_untouched():
    ws = words([("hello", 0.0, 0.3), ("world", 0.3, 0.6)])
    seg = {"start": 0.0, "end": 0.6, "text": "hello world", "words": ws}
    out = resegment_at_pauses([seg], gap_break=0.35)
    assert len(out) == 1
    assert out[0]["start"] == 0.0
    assert out[0]["end"] == 0.6


def test_resegment_passes_through_a_segment_with_no_words():
    """Already-translated (words stripped) or an imported .srt: nothing to
    recover finer timing from, so the segment must pass through unchanged."""
    seg = {"start": 1.0, "end": 5.0, "text": "no word timing here"}
    out = resegment_at_pauses([seg])
    assert out == [seg]
