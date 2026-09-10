"""Intent grammar (spec §2.3, §2.9): every benchmark prompt resolves at
≥ 0.75 to the intents the benchmark expects, 15 adversarial phrasings
resolve at ≥ 0.6, the `cut` precedence table holds row by row, negations
become exclusions, templates join as `auto_edit` variants, and undo/redo
are whole-prompt intents.

The benchmark prompts are the §6.2 table verbatim (K's `prompts.py::CASES`
carries the same strings; if the two drift the benchmark run says so).
"""
from __future__ import annotations

import pytest

from video_ai_editor.agent.prompt import grammar as G

BENCHMARK: list[tuple[str, list[str]]] = [
    ("add captions", ["captions"]),
    ("add chunky captions in hinglish", ["captions"]),
    ("remove the silences", ["remove_silences"]),
    ("cut out the ums", ["remove_fillers"]),
    ("tighten it up and add captions", ["tighten", "captions"]),
    ("make 3 shorts under 30 seconds for tiktok", ["shorts"]),
    ("make it vertical for reels", ["reframe"]),
    ("turn this into a tiktok", ["auto_edit"]),
    ("add chill background music and duck it under my voice", ["music", "duck"]),
    ("cut to the beat of the music", ["beat_sync"]),
    ("add a hook in the first 3 seconds", ["hook"]),
    ("give it a cinematic look", ["color_look"]),
    ("clean up the audio and normalize to -14 LUFS", ["clean_audio", "loudness"]),
    ("speed it up 1.5x", ["speed"]),
    ("cut the first 5 seconds", ["trim"]),
    ("add a lower third for Priya Sharma @priya.codes at the start", ["title"]),
    ("apply my brand kit @quicksolutions.in with #techtips and add an end card", ["brand", "end_card"]),
    ("add smooth transitions between the clips", ["transitions"]),
    ("make it good for youtube", ["auto_edit"]),
    ("complete the video for instagram reels with hindi captions and upbeat music", ["auto_edit", "music"]),
    ("add a voiceover saying 'Thanks for watching' at the end", ["voiceover"]),
    ("undo that", ["undo"]),
    ("make it pop", ["auto_edit"]),
    ("remove the ums", ["remove_fillers"]),
]

ADVERSARIAL: list[tuple[str, str]] = [
    ("captions please", "captions"),
    ("subtitles", "captions"),
    ("ums out", "remove_fillers"),
    ("vertical version please", "reframe"),
    ("can you add some music?", "music"),
    ("chill bed", "music"),
    ("1.5x", "speed"),
    ("make it snappier", "tighten"),
    ("hindi subs", "captions"),
    ("reframe for tiktok", "reframe"),
    ("quieter music when I talk", "duck"),
    ("trim the intro", "trim"),
    ("brand it @acme", "brand"),
    ("dip to black between the scenes", "transitions"),
    ("shorten it", "tighten"),
]

PRECEDENCE: list[tuple[str, str | None]] = [
    ("cut to the beat", "beat_sync"),
    ("cut it to the music", "beat_sync"),
    ("cut the first 5 seconds", "trim"),
    ("cut from 0:05 to 0:12", "trim"),
    ("cut out the ums", "remove_fillers"),
    ("cut the filler words", "remove_fillers"),
    ("cut the silences", "remove_silences"),
    ("cut out the dead air", "remove_silences"),
    ("cut it into 3 clips", "shorts"),
    ("cut it into shorts", "shorts"),
    ("cut it down", "tighten"),
    ("cut", None),
]


@pytest.mark.parametrize("prompt,intents", BENCHMARK, ids=[p for p, _ in BENCHMARK])
def test_benchmark_prompts_resolve_confidently(prompt, intents):
    det = G.detect(prompt)
    assert det.intents == intents, (det.intents, det.confidence, det.unmatched)
    assert det.confidence >= G.RUN_THRESHOLD and det.tier == "run"


@pytest.mark.parametrize("prompt,intent", ADVERSARIAL, ids=[p for p, _ in ADVERSARIAL])
def test_adversarial_phrasings_still_resolve(prompt, intent):
    det = G.detect(prompt)
    assert det.intents == [intent], det.intents
    assert det.confidence >= 0.6


@pytest.mark.parametrize("prompt,intent", PRECEDENCE, ids=[p for p, _ in PRECEDENCE])
def test_cut_precedence_table(prompt, intent):
    det = G.detect(prompt)
    assert det.intents == ([intent] if intent else []), det.intents
    if intent is None:
        assert det.tier == "clarify" and det.unmatched == ("cut",)


def test_confidence_is_mean_score_times_coverage():
    det = G.detect("add captions and frobnicate the wibble")
    assert det.intents == ["captions"] and det.unmatched == ("frobnicate the wibble",)
    assert det.confidence == pytest.approx(0.5) and det.tier == "normalise"
    assert G.detect("").confidence == 0.0 and G.detect("").tier == "clarify"


def test_negations_become_exclusions_not_hits():
    det = G.detect("add music but no captions")
    assert det.intents == ["music"] and det.exclusions == ("captions",)
    det = G.detect("make it a reel without music, no hook")
    assert det.intents == ["auto_edit"] and set(det.exclusions) == {"music", "hook"}
    det = G.detect("don't add a hook")
    assert det.intents == [] and det.exclusions == ("hook",) and det.confidence == 1.0
    det = G.detect("bina music reel banao")
    assert "music" in det.exclusions and "music" not in det.intents


def test_clause_split_keeps_protected_phrases_and_quotes():
    assert G.split_clauses("make it black and white, then add captions") == ["make it black and white", "add captions"]
    assert G.split_clauses("remove the silences and fillers") == ["remove the silences and fillers"]
    assert G.split_clauses('add a voiceover saying "rock and roll" and captions') == [
        'add a voiceover saying "rock and roll"', "captions"]
    assert G.split_clauses("captions lagao aur music daal do") == ["captions add", "music add"]


def test_templates_are_auto_edit_variants():
    det = G.detect("clean up the lecture")
    assert det.intents == ["auto_edit"] and det.hits[0].template == "lecture_cleanup"
    det = G.detect("make a talking head reel")
    assert det.hits[0].template == "talking_head_reel"
    assert G.detect("tiktok hype edit please").hits[0].template == "tiktok_hype"


def test_undo_and_redo_are_whole_prompt_intents():
    assert G.detect("undo that and add music").intents == ["undo"]
    assert G.detect("redo").intents == ["redo"]
    assert G.detect("add music and then undo the captions").intents == ["music", "captions"] or \
        "undo" not in G.detect("add music and then undo the captions").intents[:1]


def test_ask_only_wins_for_questions():
    assert G.detect("how long is the video?").intents == ["ask"]
    assert G.detect("what did I add?").intents == ["ask"]
    assert G.detect("add captions?").intents == ["captions"]


def test_each_clause_yields_one_intent_with_its_own_slots():
    det = G.detect("speed it up 2x, add captions in hindi and a chill bed")
    assert det.intents == ["speed", "captions", "music"]
    assert det.hits[0].slots.speed == 2.0 and det.hits[1].slots.language == "hi" and det.hits[2].slots.mood == "chill"
    assert det.slots.language == "hi"          # whole-prompt slots carry across clauses


def test_transition_requests_without_the_word_transition():
    for prompt in ("smooth zoom between every clip", "add a glitch at the hook", "fade to black at the end"):
        assert G.detect(prompt).intents == ["transitions"], prompt
    assert G.detect("add a hook").intents == ["hook"]        # no seam vocabulary → still a hook
    assert G.detect("zoom in on the product").intents == []


def test_every_intent_has_a_phrase_row_and_the_tie_breaks_are_real_intents():
    assert set(G.PHRASES) == set(G.INTENTS)
    for winner, loser in G._TIE_BREAKS:
        assert winner in G.INTENTS and loser in G.INTENTS


def test_clause_slots_keep_the_proper_noun_name_from_the_whole_prompt():
    """Clauses are lower-cased and `NAME_RE` reads capitals, so the name used
    to survive only in the whole-prompt slots — the title recipe then asked
    for a name it had been given (benchmark case 16)."""
    det = G.detect("add a lower third for Priya Sharma @priya.codes at the start")
    assert det.intents == ["title"]
    assert det.hits[0].slots.name == "Priya Sharma" and det.hits[0].slots.handle == "@priya.codes"
    assert det.hits[0].slots.at_start
    # a name crosses over only into the clause that contains it
    det = G.detect("apply my brand kit @acme and add a lower third for Priya Sharma")
    assert det.intents == ["brand", "title"]
    assert det.hits[0].slots.name is None and det.hits[0].slots.handle == "@acme"
    assert det.hits[1].slots.name == "Priya Sharma" and det.hits[1].slots.handle is None
