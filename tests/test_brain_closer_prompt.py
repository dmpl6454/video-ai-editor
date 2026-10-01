"""EB1 closer, Prompt-bar side: the speech the footage has, 'clean this up', a rolled-back plan, the applied reply, the reply cap.

Each test failed on the tree that reached the closer (the failing output is in the closer's report)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from gen_brain_goldens import p2_graph  # noqa: E402

from video_ai_editor.brain.planner import plan  # noqa: E402

PODCAST = {"content_type": "podcast"}


# --------------------------------------------------------------------------
# speech_preserved on a brain plan: the words the plan removes on purpose are not lost speech
# --------------------------------------------------------------------------

@pytest.fixture
def reel_run(tmp_path, monkeypatch):
    """The 12 s clip, the hand graph and EDP, a 3-second reel planned by the expander and run by the executor."""
    import threading
    import brain_contract_fixtures as BF
    import prompt_fixtures as F
    from video_ai_editor import config
    from video_ai_editor.agent.prompt import brain_expanders as BX
    from video_ai_editor.agent.prompt import executor
    from video_ai_editor.agent.prompt.facts import build_facts
    from video_ai_editor.agent.prompt.recipes import Context, Intent
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    store = F.make_store(tmp_path, name="s_reel")
    src = next(c for c in store.edl.get_track("v1").clips).src
    BF.write_brain_files(Path(store.dir), src, workdir=tmp_path, bed=str(F.music_bed(tmp_path)))
    facts = build_facts(store, None)
    x = BX.x_edit(Intent("edit", {"_duration_s": 3}, clause="make a 3 second reel"), facts,
                  Context(recipes=frozenset({"edit"}), exclusions=frozenset(), has_cut_steps=True))
    plan = F.plan_of(*x.steps, intent="edit", title="Reel", postconditions=list(x.postconditions))
    res = executor.run_plan(store, plan, facts, emit=lambda e: None, cancel_event=threading.Event(), prompt="make a 3 second reel")
    return store, facts, plan, res


def _speech_preserved(store, facts, plan, res):
    from video_ai_editor.agent.prompt import verify
    from video_ai_editor.agent.prompt.schema import Postcondition
    ctx = verify.VerifyCtx(store=store, plan=plan, exec_result=res, facts_before=facts, render_allowed=False)
    return verify.run_check(ctx, Postcondition(check="speech_preserved", human="no kept word was cut", args={}))


def test_a_reel_that_drops_words_on_purpose_does_not_fail_speech_preserved(reel_run):
    store, facts, plan, res = reel_run
    assert res.committed, res.error or res.rollback
    got = _speech_preserved(store, facts, plan, res)
    assert got.passed is True, (got.measured, got.detail)
    assert "on purpose" in (got.detail or ""), got.detail


def test_speech_preserved_still_fails_for_a_word_lost_outside_the_plans_removals(reel_run, monkeypatch):
    from video_ai_editor.brain import checks as BC
    store, facts, plan, res = reel_run
    real = BC.named_removals
    # the plan names LESS than it removed (a cut its decisions never named): the words that went are lost speech
    monkeypatch.setattr(BC, "named_removals", lambda st, pl, before: [(f, a, min(b, a + 0.01)) for f, a, b in real(st, pl, before)])
    got = _speech_preserved(store, facts, plan, res)
    assert got.passed is False and got.measured >= 1, (got.measured, got.detail)


# --------------------------------------------------------------------------
# UX-01 residual: a clip with next to no speech is refused, never offered an edit
# --------------------------------------------------------------------------

def _seg(*words: str) -> dict:
    return {"language": "en", "duration": 12.0, "segments": [{"id": 0, "start": 0.0, "end": 1.0, "text": " ".join(words), "words": [
        {"word": w, "start": 0.1 * i, "end": 0.1 * i + 0.09, "prob": 0.9} for i, w in enumerate(words)]}]}


@pytest.mark.parametrize("words, kept", [
    (["(eerie", "music)"], []),
    (["[MUSIC]"], []),
    (["*applause*", "thank", "you"], ["thank", "you"]),
    (["♪"], []),
    (["so", "(laughs)", "we", "start"], ["so", "we", "start"]),
    (["in", "(1998)", "we", "began"], ["in", "(1998)", "we", "began"]),           # a year in brackets is speech
])
def test_the_speech_layer_drops_a_recognisers_sound_annotations(words, kept):
    from video_ai_editor.brain.analysis import speech as SP
    assert [w["text"] for w in SP._words(_seg(*words))] == kept


def test_a_layer_with_next_to_no_words_is_empty_not_ok():
    from video_ai_editor.brain import graph as G
    assert G._speech_status(None, 12.0) == "missing"
    assert G._speech_status({"words": [{"text": "you"}]}, 12.0) == "empty"
    assert G._speech_status({"words": [{"text": "um", "filler": True}] * 30}, 12.0) == "empty"
    assert G._speech_status({"words": [{"text": "w"}] * 60}, 12.0) == "ok"
    assert G._speech_status({"words": [{"text": "w"}] * 60}, 3 * 3600.0) == "empty"          # under 5 words a minute


def test_the_header_of_a_speechless_graph_blocks_with_one_honest_sentence():
    from video_ai_editor.brain import graph as G
    hdr = {"reference": "src_a", "sources": [{"key": "src_a", "layers": {"speech": "empty"}}]}
    found = G.blockers(hdr)
    assert found == [{"code": "silent", "message": "There is no speech in this footage to edit."}]
    hdr["sources"][0]["layers"]["speech"] = "ok"
    assert G.blockers(hdr) == []
    hdr["sources"][0]["layers"]["speech"] = "missing"
    assert G.blockers(hdr)[0]["code"] == "no_speech"


@pytest.mark.parametrize("phrase", ["tighten this podcast", "edit this", "make a 45-second reel"])
def test_a_silent_clip_gets_no_edit_only_the_sentence(tmp_path, monkeypatch, phrase):
    import json
    import brain_contract_fixtures as BF
    import prompt_fixtures as F
    from video_ai_editor import config, storage
    from video_ai_editor.agent.prompt import service
    from video_ai_editor.brain import store as ST
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    monkeypatch.delenv("VAI_PROMPT_CONFIRM", raising=False)
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    monkeypatch.setattr(storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(service, "_RESOLVE_STORE", None)
    store = F.make_store(tmp_path, name="s_silent")
    src = next(c for c in store.edl.get_track("v1").clips).src
    BF.write_brain_files(Path(store.dir), src, workdir=tmp_path, current=False)
    gp = ST.graph_path(Path(store.dir), BF.GID)
    hdr = json.loads(gp.read_text())
    for s in hdr["sources"]:
        s["layers"]["speech"] = "empty"
    gp.write_text(json.dumps(hdr))
    ST.set_current_graph(Path(store.dir), BF.GID)
    before, ops = store.edl.hash(), len(store.ops.ops)
    events = F.collect(service.prompt_turn(store, phrase, [], brain="recipes"))
    text = " ".join(str(e.get("text", "")) for e in events if e["type"] == "text_delta")
    assert "There is no speech in this footage to edit." in text, (text, [e["type"] for e in events])
    assert not [e for e in events if e["type"] == "clarify"]
    assert all(not e["plan"]["steps"] for e in events if e["type"] == "plan")
    assert store.edl.hash() == before and len(store.ops.ops) == ops


# --------------------------------------------------------------------------
# 'clean this up' is a clean-up: cuts, fillers and pauses, nothing the request did not ask for
# --------------------------------------------------------------------------

CLEANUP_CONTROLS = {"scope": "cleanup", "captions": "off", "music": "off"}


def test_a_cleanup_plan_holds_only_the_removals_on_a_single_camera():
    from gen_brain_goldens import th_graph
    kinds = {d["kind"] for d in plan(th_graph(), CLEANUP_CONTROLS)["decisions"]}
    assert "cut_range" in kinds, kinds
    assert kinds <= {"cut_range", "keep_pause"}, kinds


def test_the_whole_edit_still_has_all_of_it():
    from gen_brain_goldens import th_graph
    kinds = {d["kind"] for d in plan(th_graph(), {})["decisions"]}
    assert {"cut_range", "captions", "punch_in", "dialogue"} <= kinds, kinds


def test_a_cleanup_of_two_cameras_keeps_the_camera_plan_and_the_dialogue_lane():
    kinds = {d["kind"] for d in plan(p2_graph(), {**CLEANUP_CONTROLS, "content_type": "podcast"})["decisions"]}
    assert {"cut_range", "switch_angle", "dialogue"} <= kinds and not kinds & {"captions", "punch_in", "music"}, kinds


@pytest.mark.parametrize("phrase, cleanup", [
    ("clean this up", True), ("clean it up", True), ("remove the boring parts", True), ("edit this", False),
    ("make it punchier", False), ("clean this up and add captions", False), ("edit this like a premium podcast", False)])
def test_which_asks_are_a_cleanup(phrase, cleanup):
    from video_ai_editor.agent.prompt import edit_grammar as EG
    assert EG.is_cleanup(phrase) is cleanup


def test_the_prompt_bar_offers_a_cleanup_for_clean_this_up(tmp_path, monkeypatch):
    import brain_contract_fixtures as BF
    import prompt_fixtures as F
    from video_ai_editor import config
    from video_ai_editor.agent.prompt import brain_expanders as BX
    from video_ai_editor.agent.prompt.facts import build_facts
    from video_ai_editor.agent.prompt.recipes import Context, Intent
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    store = F.make_store(tmp_path, name="s_clean")
    src = next(c for c in store.edl.get_track("v1").clips).src
    BF.write_brain_files(Path(store.dir), src, workdir=tmp_path, bed=str(F.music_bed(tmp_path)))
    facts = build_facts(store, None)
    ctx = Context(recipes=frozenset({"edit"}), exclusions=frozenset(), has_cut_steps=True)
    clean = BX.x_edit(Intent("edit", {}, clause="clean this up"), facts, ctx)
    whole = BX.x_edit(Intent("edit", {}, clause="edit this"), facts, ctx)
    tools = {s.tool for s in clean.steps}
    assert "cut_source_ranges" in tools, tools
    assert not tools & {"add_caption_track", "set_caption_style", "add_keyframe", "sync_dialogue_lane", "add_music"}, tools
    assert {"add_caption_track", "sync_dialogue_lane"} <= {s.tool for s in whole.steps}


# --------------------------------------------------------------------------
# a brain plan the safety net rolls back: one honest sentence and a next step, never the intent picker
# --------------------------------------------------------------------------

MID_WORD = {"kind": "check", "clause": "no_cut_mid_word",
            "message": "No cut lands inside a word did not hold: “that.” cut at source 127.150s (seam 00:00:04:13 on a1)"}


@pytest.mark.parametrize("applied, reel, lead", [
    (False, True, "I could not cut a clean 45-second reel from this recording"),
    (True, True, "The reel was rolled back"),
    (False, False, "I could not make that edit cleanly"),
    (True, False, "The edit was rolled back"),
])
def test_a_rolled_back_brain_plan_is_said_in_plain_words(applied, reel, lead):
    from video_ai_editor.agent.prompt import brain_reply as BR
    text = BR.rollback_text([MID_WORD], applied=applied, reel=reel, asked_s=45.0)
    assert text.startswith(lead) and "inside a spoken word" in text and "Nothing was changed." in text, text
    for leak in ("source", "seam", "a1", "127.150", "did not hold", "Which did you mean", "Trim", "tighten"):
        assert leak not in text, (leak, text)
    assert ("another length" in text) is reel


@pytest.mark.parametrize("confirm, lead", [(None, "I could not cut a clean 3-second reel"), ("0", "The reel was rolled back")])
def test_the_prompt_bar_answers_a_rolled_back_reel_with_one_sentence_and_no_question(tmp_path, monkeypatch, confirm, lead):
    import brain_contract_fixtures as BF
    import prompt_fixtures as F
    from video_ai_editor import config, storage
    from video_ai_editor.agent.prompt import executor, pending, service
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    monkeypatch.delenv("VAI_PROMPT_CONFIRM", raising=False)
    if confirm is not None:
        monkeypatch.setenv("VAI_PROMPT_CONFIRM", confirm)
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    monkeypatch.setattr(storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(service, "_RESOLVE_STORE", None)
    store = F.make_store(tmp_path, name="s_rollback")
    src = next(c for c in store.edl.get_track("v1").clips).src
    BF.write_brain_files(Path(store.dir), src, workdir=tmp_path, bed=str(F.music_bed(tmp_path)))
    monkeypatch.setattr(executor, "safety_net", lambda *a, **k: [MID_WORD])
    before, ops = store.edl.hash(), len(store.ops.ops)
    events = F.collect(service.prompt_turn(store, "make a 3 second reel", [], brain="recipes"))
    text = " ".join(str(e.get("text", "")) for e in events if e["type"] == "text_delta")
    assert lead in text and "Nothing was changed." in text, (text, [e["type"] for e in events])
    assert not [e for e in events if e["type"] == "clarify"], "a rollback is not a question"
    assert "Which did you mean" not in text and "seam" not in text and "source" not in text
    assert store.edl.hash() == before and len(store.ops.ops) == ops
    assert pending.load_pending(Path(store.dir)) is None, "nothing is left to answer"


# --------------------------------------------------------------------------
# the applied reply: the length once, no idle bookkeeping (N-17)
# --------------------------------------------------------------------------

def test_the_planners_own_length_note_is_not_repeated_in_the_reply():
    from video_ai_editor.agent.prompt import brain_reply as BR
    title, notes = BR.planner_notes("talking head → 45 s reel: 8 cuts; the result runs 0:44; opens on “Hello there”; "
                                    "a note worth keeping", [])
    assert title == "talking head → 45 s reel: 8 cuts"
    assert notes == ["a note worth keeping"], notes


def test_an_idle_bookkeeping_step_is_not_a_line_of_the_reply():
    from types import SimpleNamespace as NS
    from video_ai_editor.agent.prompt import brain_reply as BR
    steps = [NS(tool="split_at", effect="none"), NS(tool="auto_reframe", effect="none"), NS(tool="reorder_clips", effect="none"),
             NS(tool="cut_source_ranges", effect="none"), NS(tool="split_at", effect=None)]
    assert [(s.tool, s.effect) for s in BR.worth_saying(steps)] == [("cut_source_ranges", "none"), ("split_at", None)]


def test_the_reply_of_a_brain_run_says_its_length_once_and_no_idle_line(tmp_path, monkeypatch):
    import brain_contract_fixtures as BF
    import prompt_fixtures as F
    from video_ai_editor import config, storage
    from video_ai_editor.agent.prompt import service
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    monkeypatch.setenv("VAI_PROMPT_CONFIRM", "0")
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    monkeypatch.setattr(storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(service, "_RESOLVE_STORE", None)
    store = F.make_store(tmp_path, name="s_reply")
    src = next(c for c in store.edl.get_track("v1").clips).src
    BF.write_brain_files(Path(store.dir), src, workdir=tmp_path, bed=str(F.music_bed(tmp_path)))
    events = F.collect(service.prompt_turn(store, "make a 3 second reel", [], brain="recipes"))
    text = " ".join(str(e.get("text", "")) for e in events if e["type"] == "text_delta")
    assert "Applied" in text or "Done" in text, text
    assert re.findall(r"\b\d+(?:\.\d+)? s\b", text.split("Done")[1]) == ["3.5 s"], text      # the measured length, once
    assert text.count("The video is now") == 1 and "result runs" not in text, text
    for noise in ("nothing to split", "reframed 0 clips", "premium_podcast"):
        assert noise not in text, (noise, text)


# --------------------------------------------------------------------------
# a turn's notes in front of a plan's own reply never overflow `Plan.reply` (a real session ended on a traceback)
# --------------------------------------------------------------------------

def test_the_notes_and_the_plans_reply_fit_the_reply_cap_together():
    from video_ai_editor.agent.prompt.reply_text import REPLY_MAX, joined_reply
    from video_ai_editor.agent.prompt.schema import Plan
    note = "Dropped the earlier question — nothing from it was applied."
    long_reply = "podcast → episode: " + ", ".join(f"{n} cuts" for n in range(60))
    assert len(long_reply) > REPLY_MAX
    joined = joined_reply([note], long_reply)
    assert joined.startswith(note) and len(joined) <= REPLY_MAX and joined.endswith("…"), joined
    Plan.new(intent="edit", brain="recipes", reply=joined)               # what the turn does next: it validates
    assert joined_reply([note], "short reply") == f"{note} short reply"
    assert joined_reply([note], None) == note and joined_reply([], "x") == "x"
    huge = joined_reply(["x" * 300, "y " * 200], "ignored")
    assert len(huge) <= REPLY_MAX and huge.startswith("x" * 300)
    assert joined_reply(["n" * 390], "tail words that do not fit") == "n" * 390          # no room: the note alone, never a stub


def test_a_second_prompt_over_an_unanswered_question_does_not_end_on_a_traceback(tmp_path, monkeypatch):
    """The first prompt asks the analysis gate; the next one drops that question (a note) and plans: with a brain plan's
    long reply the joined text used to fail `Plan.reply`'s 400-character cap inside the turn."""
    import brain_contract_fixtures as BF
    import prompt_fixtures as F
    from video_ai_editor import config, storage
    from video_ai_editor.agent.prompt import service
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    monkeypatch.delenv("VAI_PROMPT_CONFIRM", raising=False)
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    monkeypatch.setattr(storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(service, "_RESOLVE_STORE", None)
    store = F.make_store(tmp_path, name="s_two_prompts")
    first = F.collect(service.prompt_turn(store, "make a 3 second reel", [], brain="recipes"))
    assert [e for e in first if e["type"] == "clarify"], [e["type"] for e in first]               # no graph yet: the gate asks
    src = next(c for c in store.edl.get_track("v1").clips).src
    BF.write_brain_files(Path(store.dir), src, workdir=tmp_path, bed=str(F.music_bed(tmp_path)))
    real = service.joined_reply
    seen: list[str] = []
    monkeypatch.setattr(service, "joined_reply", lambda notes, reply: (seen.append(real(notes, "x" * 900)), real(notes, "x" * 900))[1])
    second = F.collect(service.prompt_turn(store, "tighten this like a premium podcast", [], brain="recipes"))
    assert not [e for e in second if e["type"] == "error"], [e for e in second if e["type"] == "error"]
    assert seen and all(len(t) <= 400 for t in seen), seen
