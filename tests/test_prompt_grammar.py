"""Intent grammar (spec §2.3, §2.9): every benchmark prompt resolves at
≥ 0.75 to the intents the benchmark expects, 15 adversarial phrasings
resolve at ≥ 0.6, the `cut` precedence table holds row by row, negations
become exclusions, templates join as `auto_edit` variants, and undo/redo
are whole-prompt intents.

The benchmark prompts are the §6.2 table verbatim (K's `prompts.py::CASES`
carries the same strings; if the two drift the benchmark run says so).
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

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
    for prompt in ("smooth zoom between every clip", "add a glitch at the hook", "fade to black at the last cut"):
        assert G.detect(prompt).intents == ["transitions"], prompt
    # QA-018: "at the END" is the closing fade of the video, not the last seam.
    assert G.detect("fade to black at the end").intents == ["fade"]
    assert G.detect("add a hook").intents == ["hook"]        # no seam vocabulary → still a hook
    # Wave D3 (E3): a zoom on the picture is its own recipe now (it asks
    # which clip when none is named or selected) — never a transition.
    assert G.detect("zoom in on the product").intents == ["zoom"]
    assert G.detect("smooth zoom between every clip").intents == ["transitions"]


def test_every_intent_has_a_phrase_row_and_the_tie_breaks_are_real_intents():
    from video_ai_editor.agent.prompt import edit_grammar as EG
    # the `edit` rows live in edit_grammar (read only with brain.enabled on; review SC-04)
    assert set(G.PHRASES) | set(EG.EDIT_PHRASES) == set(G.INTENTS) and not set(G.PHRASES) & set(EG.EDIT_PHRASES)
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


# --------------------------------------------------------------------------
# Editor Brain (EB1, FX-D, review SC-04): brain.enabled OFF is 0.8.0.
#
# The differential runs against a FROZEN copy of the 0.8.0 grammar
# (tests/goldens/prompt_flag_off/grammar_0_8_0.py.txt = `git show f407287:…/grammar.py`),
# phrase by phrase over the whole K3 corpus, the `edit` block and the UX-12 phrasings:
# the reading (intents, scores, clauses, slots, exclusions, unmatched) and the PLAN
# (intent, title, steps, questions, reply) must be equal with the flag off.
# --------------------------------------------------------------------------

_FROZEN_0_8_0 = Path(__file__).parent / "goldens" / "prompt_flag_off" / "grammar_0_8_0.py.txt"


def frozen_grammar() -> types.ModuleType:
    """The 0.8.0 grammar module, loaded under the prompt package so its relative imports resolve."""
    name = "video_ai_editor.agent.prompt.grammar_0_8_0"
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    mod.__package__ = "video_ai_editor.agent.prompt"
    mod.__file__ = str(_FROZEN_0_8_0)
    sys.modules[name] = mod                       # dataclasses look their module up here
    exec(compile(_FROZEN_0_8_0.read_text(encoding="utf-8"), str(_FROZEN_0_8_0), "exec"), mod.__dict__)
    return mod


def _corpus_phrases() -> tuple[list[str], list[str]]:
    """(the K3 phrasings that are not `edit` phrasings, the `edit` block + passthrough)."""
    import test_k3_prompt_corpus as K
    edit = [p.phrase for p in K.EDIT_BLOCK + K.EDIT_PASSTHROUGH]
    return [p.phrase for p in K.CORPUS + K.HOLDOUT + K.HOLDOUT2], edit


def _reading(det) -> tuple:
    return (det.clauses, [(h.intent, h.score, h.clause, h.slots, h.template) for h in det.hits],
            det.exclusions, det.unmatched, det.slots)


def test_flag_off_every_phrase_reads_as_it_did_in_0_8_0(monkeypatch):
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "0")
    head = frozen_grammar()
    rest, edit = _corpus_phrases()
    phrases = rest + edit
    assert len(phrases) >= 500 and len(edit) >= 34
    bad = [p for p in phrases if _reading(G.detect(p)) != _reading(head.detect(p))]
    assert not bad, f"{len(bad)} phrases read differently from 0.8.0 with the brain off: {bad[:8]}"
    assert not any(h.intent == "edit" for p in phrases for h in G.detect(p).hits)
    # the tables everyone else reads are the 0.8.0 ones
    assert G.PHRASES == head.PHRASES and G.CUT_PRECEDENCE == head.CUT_PRECEDENCE


def test_flag_on_no_neighbour_moves(monkeypatch):
    """The `edit` rows take only the `edit` phrasings: with the brain ON every other corpus phrase still reads as 0.8.0."""
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    head = frozen_grammar()
    rest, edit = _corpus_phrases()
    bad = [p for p in rest if _reading(G.detect(p)) != _reading(head.detect(p))]
    assert not bad, f"an `edit` row took over {bad[:8]}"
    # …and every phrase of the edit block reads as ONE edit or (the passthrough) as the auto edit, never a question
    import test_k3_prompt_corpus as K
    for p in K.EDIT_BLOCK:
        det = G.detect(p.phrase)
        assert "edit" in det.intents and det.confidence >= G.RUN_THRESHOLD, (p.phrase, det.intents, det.confidence)
    for p in K.EDIT_PASSTHROUGH:                 # the sentences 0.8.0 already answered read as 0.8.0 read them
        assert G.detect(p.phrase).intents == head.detect(p.phrase).intents, p.phrase


def _plan_digest(plan) -> dict:
    return {"intent": plan.intent, "title": plan.title, "reply": plan.reply,
            "steps": [(s.tool, json.dumps(s.args, sort_keys=True, default=str), s.stage) for s in plan.steps],
            "questions": [(q.key, q.question, [(o.value, o.label) for o in (q.options or [])]) for q in plan.needs_input]}


def plan_digests(phrases: list[str], facts, grammar_module=None) -> dict[str, dict]:
    """`planner.plan` for every phrase, optionally with another grammar module swapped in for the run."""
    from video_ai_editor.agent.prompt import planner
    saved = planner.G
    if grammar_module is not None:
        planner.G = grammar_module
    try:
        return {p: _plan_digest(planner.plan(p, facts)) for p in phrases}
    finally:
        planner.G = saved


@pytest.fixture(scope="module")
def k3_facts(tmp_path_factory):
    """Facts of the K3 sweep session (three 4 s clips, music bed, clip B selected) — built with the flag off."""
    import prompt_fixtures as F
    from video_ai_editor import config, storage
    from video_ai_editor.agent.dispatch import dispatch
    from video_ai_editor.agent.prompt.facts import build_facts
    from video_ai_editor.edl.snapshot import EDLStore
    root = tmp_path_factory.mktemp("flag_off")
    mp = pytest.MonkeyPatch()
    mp.setenv("VAI_BRAIN_ENABLED", "0")
    before = config._FORCED_RESTRICT
    config.enable_path_restriction(False)
    mp.setattr(storage, "WORKDIR", root)
    mp.setattr(config, "WORKDIR", root)
    src = F.speech_clip(root)
    F.write_ingest(src)
    bed = F.music_bed(root, dur=12.0)
    st = EDLStore(root / "sess")
    dispatch(st, "add_clip", {"track": "v1", "src": str(src), "in": 0, "out": F.CLIP_DUR, "start": 0})
    dispatch(st, "set_canvas", {"w": 1920, "h": 1080})
    dispatch(st, "split_at", {"track": "v1", "time": 4.0})
    dispatch(st, "split_at", {"track": "v1", "time": 8.0})
    dispatch(st, "add_music", {"src": str(bed), "start": 0.0, "volume_db": -14.0, "duck": False})
    ids = [c.id for c in st.edl.get_track("v1").clips]
    facts = build_facts(st, {"selection": ids[1], "playhead": 5.5}, feature_report={"unavailable": []})
    yield facts
    mp.undo()
    config.enable_path_restriction(before)


def test_flag_off_every_phrase_plans_as_it_did_in_0_8_0(k3_facts, monkeypatch):
    """The plan/route chosen — intent, title, steps, the question (with its options) and the reply — equals
    what 0.8.0's grammar chooses, phrase by phrase: no replies naming the hidden feature, no new rows."""
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "0")
    rest, edit = _corpus_phrases()
    phrases = rest + edit
    now = plan_digests(phrases, k3_facts)
    then = plan_digests(phrases, k3_facts, frozen_grammar())
    bad = [p for p in phrases if now[p] != then[p]]
    assert not bad, f"{len(bad)} plans differ from 0.8.0 with the brain off, first: {bad[0]!r}: {now[bad[0]]} != {then[bad[0]]}"
    assert not any("Editor Brain" in (d["reply"] or "") for d in now.values())
    assert not any(d["intent"] == "edit" or "edit" in d["intent"].split("+") for d in now.values())


def test_flag_off_an_unread_phrase_never_offers_the_hidden_recipe(k3_facts, monkeypatch):
    """The 'which did you mean?' options come from the 0.8.0 tables, with the brain off and on alike."""
    for flag in ("0", "1"):
        monkeypatch.setenv("VAI_BRAIN_ENABLED", flag)
        for phrase in ("do something with the sound", "make it better", "fix this"):
            digest = plan_digests([phrase], k3_facts)[phrase]
            offered = {v for _k, _q, opts in digest["questions"] for v, _l in opts}
            assert "edit" not in offered, (flag, phrase, digest)


def test_the_ux12_phrasings_are_one_edit_with_the_brain_on(monkeypatch):
    """Review UX-12: each phrasing the re-testers found answered with a wrong or unclear question is ONE `edit` clause."""
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    for phrase in ("cut this down to a 60s vertical for tiktok", "clean this up", "edit this", "make it punchier",
                   "make a 2 minute reel", "podcast ko tight karo, premium feel",
                   "cut the silences and switch to whoever is speaking",
                   "edit this like a podcast, switch cameras when they talk", "give me a 45 sec short for youtube shorts",
                   "filler words hata do aur tight kar do", "sync the dialogue"):
        det = G.detect(phrase)
        assert det.intents == ["edit"] and det.confidence == 1.0 and not det.unmatched, (phrase, det.intents, det.confidence)
    # neighbours stay: a platform export, "clean this up FOR tiktok", a title look, the beat sync
    assert G.detect("export for tiktok").intents == ["export_preset"]
    assert G.detect("clean this up for tiktok").intents == ["auto_edit"]
    assert G.detect("make the title look premium").intents == ["color_look"]
    assert G.detect("sync to the beat").intents == ["beat_sync"]
