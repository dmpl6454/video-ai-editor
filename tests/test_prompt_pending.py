"""Pending clarification: pause, parse a typed answer, resume, invalidate
(agent/prompt/pending.py, spec §4.3).

The rule under test is the WHOLE-MESSAGE rule: the next chat message resumes
a paused plan only when the entire message, normalised, is an answer to its
first blocking question. "hi" resumes a language question; "make it hi-res"
is a new request and drops the question — a paused plan must never hijack
what the user actually asked for next.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from video_ai_editor.agent.prompt import pending
from video_ai_editor.agent.prompt.facts import TimelineFacts
from video_ai_editor.agent.prompt.schema import DownloadNeeded, NeedsInput, NeedsInputOption, Plan, Step


def _lang_question(required: bool = True, default=None) -> NeedsInput:
    return NeedsInput(
        key="target_lang", question="Which language for the captions?", kind="choice",
        options=[NeedsInputOption(value="hi", label="Hindi", synonyms=["hindi"]),
                 NeedsInputOption(value="en", label="English"),
                 NeedsInputOption(value="hinglish", label="Hinglish", synonyms=["roman hindi"]),
                 NeedsInputOption(value="es", label="Spanish")],
        required=required, default=default)


def _plan(*questions: NeedsInput, steps: list[Step] | None = None, **fields) -> Plan:
    steps = steps if steps is not None else [
        Step(tool="translate_captions", args={"target_lang": None}, why="translate")]
    return Plan.new(intent="translate_captions", brain="recipes", steps=steps,
                    needs_input=list(questions), **fields)


def _facts(**over) -> TimelineFacts:
    return TimelineFacts.minimal(duration=24.2, **over)


# ---------------------------------------------------------------- parsing

@pytest.mark.parametrize("message, expected", [
    ("hi", "hi"), ("Hindi", "hi"), ("hindi.", "hi"), ("  HI  ", "hi"), ('"hi"', "hi"),
    ("the first one", "hi"), ("2nd", "en"), ("the second option", "en"), ("third", "hinglish"),
    ("roman hindi", "hinglish"), ("Spanish please", "es"), ("go with es", "es"),
    ("make it hi-res", None), ("hi there, also add music", None), ("english captions please", None),
    ("", None), ("fifth", None),
])
def test_whole_message_rule_for_a_choice(message, expected):
    assert pending.parse_answer_for(_lang_question(), message) == expected


@pytest.mark.parametrize("message, expected", [
    ("yes", "yes"), ("Yes!", "yes"), ("download", "yes"), ("go", "yes"), ("haan", "yes"), ("ok", "yes"),
    ("no", "no"), ("skip", "no"), ("nahi", "no"), ("nope", "no"), ("never mind", "no"),
    ("yes but only the captions", None), ("download the small one", None),
])
def test_confirm_kind_accepts_yes_no_go_skip_download_and_hinglish(message, expected):
    q = NeedsInput(key="downloads", question="Download MADLAD (3 GB)?", kind="confirm", required=True)
    assert pending.parse_answer_for(q, message) == expected


@pytest.mark.parametrize("kind, message, expected", [
    ("number", "3", 3.0), ("number", "3 clips", None), ("number", "0", None), ("number", "11", None),
    ("duration", "45s", 45.0), ("duration", "45 seconds", 45.0), ("duration", "2 min", 120.0),
    ("duration", "1.5m", 90.0), ("duration", "under 30", None), ("duration", "30", 30.0),
])
def test_number_and_duration_kinds_use_the_slot_extractors_and_bounds(kind, message, expected):
    q = NeedsInput(key="count", question="How many?", kind=kind, required=True, min=1, max=10 if kind == "number" else 600)
    assert pending.parse_answer_for(q, message) == expected


def test_text_kind_takes_the_whole_message_but_options_still_win():
    q = NeedsInput(key="handle", question="Which handle?", kind="text", required=True,
                   options=[NeedsInputOption(value="skip", label="skip")])
    assert pending.parse_answer_for(q, "@priya.codes") == "@priya.codes"
    assert pending.parse_answer_for(q, "skip") == "skip"
    assert pending.parse_answer_for(q, "   ") is None


def test_try_parse_answer_binds_the_whole_message_to_the_question_it_answers():
    """The pause text lists EVERY open question, so a whole-message "yes"
    answers the confirm even while the language is still open; the resumed
    plan then pauses again for the language (one question per turn is the
    phone's reality). Anything that answers none of them drops the plan."""
    plan = _plan(_lang_question(), NeedsInput(key="go", question="Start?", kind="confirm", required=True))
    record = {"plan": plan.model_dump(), "token": "q_x"}
    assert pending.try_parse_answer("hi", record) == {"target_lang": "hi"}
    assert pending.try_parse_answer("yes", record) == {"go": "yes"}
    after_go, _ = pending.apply_answers(plan, {"go": "yes"})
    assert [q.key for q in after_go.blocking_questions] == ["target_lang"]
    assert pending.try_parse_answer("make it hi-res", record) is None
    assert pending.try_parse_answer("hi please and also add music", record) is None


# ---------------------------------------------------------------- save / load / validity

def test_save_load_and_expiry(tmp_path: Path, monkeypatch):
    plan = _plan(_lang_question())
    facts = _facts()
    record = pending.save_pending(tmp_path, plan=plan, prompt="add hindi captions", facts=facts,
                                  ui_state={"playhead": 1.0})
    assert record["token"].startswith("q_") and record["brain"] == "recipes"
    assert record["expires"] - record["created"] == pytest.approx(600.0)
    on_disk = json.loads((tmp_path / "prompt_pending.json").read_text(encoding="utf-8"))
    assert on_disk["token"] == record["token"] and on_disk["prompt"] == "add hindi captions"
    loaded = pending.load_pending(tmp_path)
    assert loaded == on_disk
    assert pending.pending_plan(loaded) == plan
    assert [q.key for q in pending.blocking_questions(loaded)] == ["target_lang"]
    assert pending.pending_is_valid(loaded, facts) == (True, None)
    assert pending.pending_is_valid(loaded, facts, now=time.time() + 601)[0] is False
    assert "expired" in pending.pending_is_valid(loaded, facts, now=time.time() + 601)[1]
    pending.clear_pending(tmp_path)
    assert pending.load_pending(tmp_path) is None
    pending.clear_pending(tmp_path)                            # idempotent


def test_a_foreign_op_invalidates_the_question_but_the_playhead_does_not():
    """§4.3 rule 3: any `op` committed by another route changes the facts hash.
    Moving the playhead or selecting a clip is not an op."""
    facts = _facts(clip_ids=["c1"], v1_clip_ids=["c1"])
    record = {"facts_hash": pending.facts_hash(facts), "expires": time.time() + 100, "plan": {}}
    assert pending.pending_is_valid(record, facts)[0]
    assert pending.pending_is_valid(record, facts.model_copy(update={"playhead": 9.0, "selection": "c1"}))[0]
    cut = facts.model_copy(update={"duration": 20.0})
    ok, why = pending.pending_is_valid(record, cut)
    assert not ok and "timeline changed" in why
    assert not pending.pending_is_valid(record, facts.model_copy(update={"has_captions": True}))[0]
    assert not pending.pending_is_valid(record, facts.model_copy(update={"clip_ids": ["c1", "c2"]}))[0]
    assert pending.pending_is_valid(record, None)[0]           # no facts to compare: only expiry counts


def test_corrupt_pending_file_reads_as_none(tmp_path: Path):
    (tmp_path / "prompt_pending.json").write_text("{", encoding="utf-8")
    assert pending.load_pending(tmp_path) is None
    (tmp_path / "prompt_pending.json").write_text(json.dumps({"token": "q_1"}), encoding="utf-8")
    assert pending.load_pending(tmp_path) is None              # no plan → not a pending record


# ---------------------------------------------------------------- binding answers

def test_apply_answers_fills_the_unset_step_arg_and_removes_the_question():
    plan = _plan(_lang_question())
    new_plan, notes = pending.apply_answers(plan, {"target_lang": "hi"})
    assert new_plan.steps[0].args == {"target_lang": "hi"}
    assert new_plan.needs_input == [] and not new_plan.blocking_questions
    assert notes == ["target_lang: hi"]
    assert plan.steps[0].args == {"target_lang": None}         # the original plan is untouched


def test_unanswered_questions_with_a_default_take_the_default_and_required_ones_stay():
    plan = _plan(_lang_question(default="en"),
                 NeedsInput(key="handle", question="Handle?", kind="text", required=True),
                 steps=[Step(tool="translate_captions", args={"target_lang": None}, why="t"),
                        Step(tool="apply_brand_kit", args={"handle": None}, why="b")])
    new_plan, notes = pending.apply_answers(plan, {})
    assert new_plan.steps[0].args["target_lang"] == "en"
    assert [q.key for q in new_plan.blocking_questions] == ["handle"]
    assert new_plan.steps[1].args["handle"] is None


def test_downloads_answered_no_drops_the_dependent_steps():
    plan = _plan(NeedsInput(key="downloads", question="Download MADLAD (3 GB)?", kind="confirm", required=True),
                 steps=[Step(tool="add_caption_track", args={"style": "ig_chunky"}, why="c"),
                        Step(tool="translate_captions", args={"target_lang": "hi"}, why="t")],
                 downloads_needed=[DownloadNeeded(what="MADLAD", bytes=3_000_000_000, tool="translate_captions")])
    kept, notes = pending.apply_answers(plan, {"downloads": "skip"})
    assert [s.tool for s in kept.steps] == ["add_caption_track"]
    assert any("skipped the steps that needed a download: translate_captions" in n for n in notes)
    both, notes = pending.apply_answers(plan, {"downloads": "yes"})
    assert [s.tool for s in both.steps] == ["add_caption_track", "translate_captions"]


def test_go_answered_no_and_abort_empty_the_plan_skip_drops_the_consuming_step():
    plan = _plan(NeedsInput(key="go", question="This will take 4 minutes. Start?", kind="confirm", required=True))
    stopped, notes = pending.apply_answers(plan, {"go": "no"})
    assert stopped.is_read_only and "not started" in " ".join(notes)
    q = NeedsInput(key="target_lang", question="?", kind="choice", required=True,
                   options=[NeedsInputOption(value="skip", label="skip"), NeedsInputOption(value="abort", label="abort")])
    aborted, _ = pending.apply_answers(_plan(q), {"target_lang": "abort"})
    assert aborted.is_read_only
    skipped, notes = pending.apply_answers(
        _plan(q, steps=[Step(tool="add_caption_track", args={}, why="c"),
                        Step(tool="translate_captions", args={"target_lang": None}, why="t")]),
        {"target_lang": "skip"})
    assert [s.tool for s in skipped.steps] == ["add_caption_track"]
    assert "skipped the target_lang step" in notes


def test_normalise_answer_strips_wrappers_and_punctuation():
    assert pending.normalise_answer("  The Second one, please! ") == "second"
    assert pending.normalise_answer("Option 'hi'") == "hi"
    assert pending.normalise_answer("go with English") == "english"
    assert pending.normalise_answer("") == ""


# ---------------------------------------------------------------- `$ask:` placeholders (the real planner shapes)

def test_apply_answers_fills_ask_placeholders_wherever_they_sit():
    """The planner writes `$ask:<key>` into the arg the answer fills — the
    arg NAME differs from the key (music_src → src, platform → name, range →
    start/end). Binding by name alone left the literal in the step and the
    executor refused it after the user had answered."""
    from video_ai_editor.agent.prompt.recipes import ask, placeholder
    plan = Plan.new(intent="t", brain="recipes", steps=[
        Step(tool="add_music", args={"src": placeholder("music_src"), "start": 0.0, "loop": True}, why="m"),
        Step(tool="apply_export_preset", args={"name": placeholder("platform")}, why="p"),
        Step(tool="cut_range", args={"track": "v1", "start": placeholder("range"), "end": placeholder("range")}, why="r"),
    ], needs_input=[ask("music_src", "Which track?", kind="choice", options=[("/u/a.wav", "a.wav"), ("/u/b.wav", "b.wav")]),
                    ask("platform", "Which platform?", options=[("reels", "reels"), ("tiktok", "tiktok")]),
                    ask("range", "Which part?", kind="text")])
    out, notes = pending.apply_answers(plan, {"music_src": "/u/b.wav", "platform": "tiktok", "range": "the first 5 seconds"},
                                       duration=24.2)
    args = {s.tool: s.args for s in out.steps}
    assert args["add_music"]["src"] == "/u/b.wav" and args["apply_export_preset"]["name"] == "tiktok"
    assert (args["cut_range"]["start"], args["cut_range"]["end"]) == (0.0, 5.0)
    assert not any(isinstance(v, str) and v.startswith("$ask:") for a in args.values() for v in a.values())
    assert out.needs_input == [] and "music_src: /u/b.wav" in notes
    # an unparsable range keeps the question (the literal never reaches cut_range)
    again, notes = pending.apply_answers(plan, {"music_src": "/u/a.wav", "platform": "reels", "range": "the good bit"}, duration=24.2)
    assert [q.key for q in again.needs_input] == ["range"] and any("could not read" in n for n in notes)
    assert next(s for s in again.steps if s.tool == "cut_range").args["start"] == "$ask:range"


def test_an_empty_answer_to_a_required_text_question_is_refused_once_then_dropped():
    handle = NeedsInput(key="handle", question="Which handle?", kind="text", required=True)
    plan = _plan(handle, steps=[Step(tool="apply_brand_kit", args={"handle": None}, why="brand kit"),
                                Step(tool="add_music", args={"src": "/u/a.wav"}, why="bed")])
    once, notes = pending.apply_answers(plan, {"handle": ""})
    assert notes == ["I still need the handle"]
    assert [q.question for q in once.blocking_questions] == ["I still need the handle. Which handle?"]
    assert [s.tool for s in once.steps] == ["apply_brand_kit", "add_music"]
    twice, notes = pending.apply_answers(once, {"handle": "  "})
    assert notes == ["skipped brand kit — no handle was given"]
    assert [s.tool for s in twice.steps] == ["add_music"] and not twice.blocking_questions
    answered, notes = pending.apply_answers(once, {"handle": "@me"})
    assert answered.steps[0].args["handle"] == "@me" and not answered.blocking_questions and notes == ["handle: @me"]
    # a question with a default is never refused — the blank falls back to the default
    plan = _plan(_lang_question(default="en"))
    filled, _ = pending.apply_answers(plan, {"target_lang": ""})
    assert filled.steps[0].args["target_lang"] == "en" and not filled.blocking_questions
