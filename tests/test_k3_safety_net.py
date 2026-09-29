"""K3 — the safety net under the key-free Prompt bar and the assistant's plans.

  * semantics.py — ONE reading of direction, amount, scope, pronouns, time,
    "keep only", typos and ambiguity, table-tested here;
  * contract_diff.py / contract.py — what a run CHANGED vs what the prompt
    SAID, judged independently of the plan;
  * executor.safety_net — a failed BLOCKING check or a contract violation
    rolls the whole run back inside its batch (nothing in history) and the
    reply is a clarify card; advisory checks keep the honest report;
  * set_text_style — a title's look is a plan edit (never a new title);
  * the planner's semantic fix-ups (multi-clip fan-out, typos, lane fixes)
    and the on-device adapter's grounding (`content.ground_semantics`).

Every test here failed before K3 (the modules / tool did not exist, or the
wrong edit was committed); the per-phrase proof is tests/test_k3_prompt_corpus.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture, no_downloads  # noqa: E402,F401

from test_prompt_capcut_sweep import _question_text, _session, _turn, media, v1  # noqa: E402,F401
from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.agent.prompt import executor, pending, semantics as M, service  # noqa: E402
from video_ai_editor.agent.prompt.contract import Contract, families_of, rollback_question  # noqa: E402
from video_ai_editor.agent.prompt.contract_diff import diff  # noqa: E402
from video_ai_editor.agent.prompt.schema import BLOCKING_CHECKS, CHECK_SPECS, TOOL_STAGE  # noqa: E402

UI = {"selection": "B", "playhead": 5.5}


# =========================================================================== semantics

@pytest.mark.parametrize("phrase,direction,factor,ambiguous", [
    ("reduce the speed of the 2nd clip", "down", None, False),
    ("slow the footage down", "down", None, False),
    ("make the whole video 2 times faster", "up", 2.0, False),
    ("make the last clip 30% faster", "up", 1.3, False),
    ("make the first clip 40% slower", "down", 0.6, False),
    ("slow everything down by 50%", "down", 0.5, False),
    ("half speed on the first clip", "down", 0.5, False),
    ("drop the speed of the last clip by half", "down", 0.5, False),
    ("set the speed back to normal", "reset", 1.0, False),
    ("clip 2 at 150% speed", "up", 1.5, False),
    ("slow it down to 2x", "down", 2.0, True),
    ("speed up clip 2 to 0.5x", "up", 0.5, True),
    ("faster and slower", "both", None, True),
])
def test_speed_reading(phrase, direction, factor, ambiguous):
    sa = M.speed_ask(phrase)
    assert (sa.direction, sa.factor, bool(sa.ambiguous)) == (direction, factor, ambiguous), sa


@pytest.mark.parametrize("phrase,direction,delta,db", [
    ("lower the music by 3 decibels", "down", -3.0, None),        # relative
    ("bring the music down 4db", "down", -4.0, None),             # a direction + N dB is a change
    ("music down 10 db", "down", -10.0, None),
    ("turn the music up 3 db", "up", 3.0, None),
    ("set clip 3 volume to -10 db", None, None, -10.0),           # absolute
    ("music -6db", None, None, -6.0),
    ("music volume 50%", "down", -6.02, None),                    # run 4: a percentage OF THE CURRENT level
    ("music volume 200%", "up", 6.02, None),
    ("set the music to 50%", "down", -6.02, None),
    ("the music's way too loud", "down", None, None),
    ("make the music not so loud", "down", None, None),
    ("music thoda kam karo", "down", None, None),                 # Hinglish
])
def test_level_reading(phrase, direction, delta, db):
    la = M.level_ask(phrase)
    assert (la.direction, la.delta_db, la.db) == (direction, delta, db), la


@pytest.mark.parametrize("prompt,want", [
    # pronouns carry across clauses of ONE prompt
    ("mute clip 2 and slow it down", [(2,), (2,)]),
    ("take the last clip, reverse it and mute it", [(-1,), (-1,), (-1,)]),
    ("slow down clip 1, then mute it, then flip it", [(1,), (1,), (1,)]),
    # several clips in one clause
    ("speed up clip 2 and clip 3", [(2, 3)]),
    ("delete clips 1 and 2", [(1, 2)]),
    ("delete the first and last clips", [(1, -1)]),
    ("slow down the clip i selected", [("sel",)]),
    # a number that is not a clip
    ("make the second clip 50% faster", [(2,)]),
    ("rotate the first clip 90 degrees", [(1,)]),
])
def test_scope_and_pronouns(prompt, want):
    assert [s.refs for s in M.resolve_scopes(prompt)] == want


def test_scope_all_except_and_media():
    s = M.scope_of("speed up all the clips except the second one")
    assert s.all and s.except_refs == (2,)
    assert M.media_of("lower the music under the voice") == ("music", "voice")   # the first noun is the lane


@pytest.mark.parametrize("phrase,want", [
    ("cut the first 3 seconds", [("first", 3.0, None)]),
    ("keep only the last 4 seconds", [("last", 4.0, None)]),
    ("delete 2 to 3 seconds", [("range", 2.0, 3.0)]),
    ("cut out 00:02-00:04", [("range", 2.0, 4.0)]),
    ("remove the first second", [("first", 1.0, None)]),
    ("get rid of everything after 9 seconds", [("after", 9.0, None)]),
    ("clip 2 at 150% speed", []),                                  # a percentage, not a moment
])
def test_time_phrases(phrase, want):
    assert [(t.kind, t.a, t.b) for t in M.time_refs(phrase)] == want


@pytest.mark.parametrize("phrase,keep", [
    ("keep only the first 10 seconds", True), ("just keep the first 6 seconds", True),
    ("keep the middle clip only", True), ("mute clip 2 but keep the music", False),
    ("cut the first 10 seconds", False),
])
def test_keep_only(phrase, keep):
    assert M.keeps_only(phrase) is keep


@pytest.mark.parametrize("prompt,has_question", [
    ("slow it down to 2x", True), ("change the speed", True), ("change the volume", True),
    ("chop the first 20 seconds", True), ("delete the 7th clip", True), ("slow down the music", True),
    ("speed up clip 2 2x", False), ("cut the first 3 seconds", False), ("split at 1:20", False),
])
def test_ambiguous_phrases_ask(prompt, has_question):
    assert bool(M.ambiguities(prompt, video_end=12.0, clip_count=3)) is has_question


def test_typos_are_read_the_same_everywhere():
    assert M.norm("spead up teh secnd clip") == "speed up the second clip"
    assert M.norm("tunr the musci down") == "turn the music down"
    assert M.clauses("make it 9:16") == ["make it 9:16"]          # a ratio's colon is not a clause break


# =========================================================================== the diff and the contract

def _judge(prompt: str, before, after, selection=None):
    return Contract.read(prompt, selection=selection).judge(before, after)


def _three(tmp_path):
    st = F.make_store(tmp_path)
    dispatch(st, "split_at", {"track": "v1", "time": 4.0})
    dispatch(st, "split_at", {"track": "v1", "time": 8.0})
    return st, [c.id for c in st.edl.get_track("v1").clips]


def _after(st, *calls):
    before = st.edl.model_copy(deep=True)
    for tool, args in calls:
        dispatch(st, tool, args)
    return before, st.edl


@pytest.mark.usefixtures("desktop_posture")
def test_diff_names_what_changed(tmp_path):
    st, ids = _three(tmp_path)
    before, after = _after(st, ("set_speed", {"clip_id": ids[1], "factor": 2.0}),
                           ("ripple_delete", {"clip_id": ids[0]}))
    d = diff(before, after)
    assert {"clip:speed", "v1:delete", "v1:cut"} <= d.categories
    assert [x.id for x in d.changed_clips("speed")] == [ids[1]]
    assert d.clip(ids[0]).gone


@pytest.mark.usefixtures("desktop_posture")
def test_a_split_is_not_a_cut_and_a_longer_clip_is_not_a_duplicate(tmp_path):
    st, ids = _three(tmp_path)
    before, after = _after(st, ("split_at", {"track": "v1", "time": 6.0}))
    d = diff(before, after)
    assert "v1:split" in d.categories and not d.categories & {"v1:cut", "v1:duplicate"}


@pytest.mark.usefixtures("desktop_posture")
@pytest.mark.parametrize("prompt,calls,kind", [
    # the round-3 wrong edits, judged from the result alone
    ("slow the footage down", [("set_speed", {"clip_id": "$ALL", "factor": 1.25})], "direction"),
    ("mute clip 2 and slow it down", [("set_clip_muted", {"clip_id": "B", "muted": True}),
                                      ("set_speed", {"clip_id": "$ALL", "factor": 0.8})], "scope"),
    ("keep only the first 10 seconds", [("cut_range", {"track": "v1", "start": 0.0, "end": 10.0})], "partial"),
    ("make the whole video black and white", [("set_volume", {"target": "A", "db": -3.0})], "unasked"),
    ("mute clip 2 but keep the music", [("set_track_muted", {"track": "music", "muted": True})], "unasked"),
    ("reduce the speed of the 2nd clip", [("set_speed", {"clip_id": "B", "factor": 1.25})], "direction"),
    ("chop the first 20 seconds", [("cut_range", {"track": "v1", "start": 0.0, "end": 12.0})], "range"),
    ("delete clips 1 and 2", [("ripple_delete", {"clip_id": "A"})], "partial"),
])
def test_the_contract_catches_a_wrong_edit(tmp_path, prompt, calls, kind):
    st, ids = _three(tmp_path)
    bed = F.music_bed(Path(st.dir), dur=12.0)
    dispatch(st, "add_music", {"src": str(bed), "start": 0.0, "volume_db": -14.0, "duck": False})
    names = dict(zip("ABC", ids))
    real = []
    for tool, args in calls:
        if args.get("clip_id") == "$ALL":
            real.extend((tool, {**args, "clip_id": cid}) for cid in ids)
        else:
            real.append((tool, {k: names.get(v, v) if isinstance(v, str) else v for k, v in args.items()}))
    before, after = _after(st, *real)
    got = _judge(prompt, before, after, selection=ids[1])
    assert kind in {v.kind for v in got}, [v.as_dict() for v in got]


@pytest.mark.usefixtures("desktop_posture")
@pytest.mark.parametrize("prompt,calls", [
    ("slow the footage down", [("set_speed", {"clip_id": "$ALL", "factor": 0.8})]),
    ("mute clip 2 and slow it down", [("set_clip_muted", {"clip_id": "B", "muted": True}),
                                      ("set_speed", {"clip_id": "B", "factor": 0.8})]),
    ("keep only the first 10 seconds", [("cut_range", {"track": "v1", "start": 10.0, "end": 12.0})]),
    ("make the whole video black and white", [("apply_lut", {"clip_id": "$ALL", "src": "mono.cube"})]),
    ("delete clips 1 and 2", [("ripple_delete", {"clip_id": "A"}), ("ripple_delete", {"clip_id": "B"})]),
    ("speed up clip 2 by 50%", [("set_speed", {"clip_id": "B", "factor": 1.5})]),
])
def test_the_contract_passes_the_right_edit(tmp_path, prompt, calls):
    st, ids = _three(tmp_path)
    names = dict(zip("ABC", ids))
    real = []
    for tool, args in calls:
        if args.get("clip_id") == "$ALL":
            real.extend((tool, {**args, "clip_id": cid}) for cid in ids)
        else:
            real.append((tool, {k: names.get(v, v) if isinstance(v, str) else v for k, v in args.items()}))
    before, after = _after(st, *real)
    assert _judge(prompt, before, after, selection=ids[1]) == []


def test_level_words_need_something_audible():
    """"lower the contrast", "turn clip two upside down", "speed up by 50%"
    are not volume changes (the first contract draft read them as one)."""
    for phrase in ("lower the contrast of clip 3", "turn clip two upside down", "speed up clip 2 by 50%",
                   "slow the last clip down a little"):
        assert "level" not in families_of(phrase), phrase
    assert "level" in families_of("turn the music down")


def test_the_rollback_question_fits_the_wire_schema():
    q = rollback_question(["it asked for slower, but clip 2 went from 1x to 1.25x" * 3, "x"])
    assert q.startswith("I undid that:") and len(q) <= 200 and q.endswith("Which did you mean?")


# =========================================================================== blocking vs advisory

def test_blocking_checks_are_edl_measured_and_known():
    assert BLOCKING_CHECKS <= set(CHECK_SPECS)
    assert not any(CHECK_SPECS[c].needs_render for c in BLOCKING_CHECKS)
    for advisory in ("captions_cover", "speech_preserved", "silence_total_leq", "loudness_within", "duration_between"):
        assert advisory not in BLOCKING_CHECKS


@pytest.mark.usefixtures("desktop_posture")
def test_a_failed_blocking_check_rolls_the_run_back(tmp_path, monkeypatch):
    """A plan whose own blocking postcondition does not hold is not kept
    "done with issues": the batch restores the tree, nothing is committed."""
    st, ids = _three(tmp_path)
    facts = F.facts_for(st)
    from video_ai_editor.agent.prompt.schema import Postcondition
    plan = F.plan_of(F.step("set_speed", clip_id=ids[1], factor=2.0),
                     postconditions=[Postcondition(check="speed_equals", args={"clip_id": ids[1], "factor": 3.0},
                                                   human="the speed matches")])
    before, n_ops = st.edl.hash(), len(st.ops.ops)
    res = executor.run_plan(st, plan, facts, emit=lambda e: None, cancel_event=None, prompt="speed up clip 2",
                            validator=F.identity_validator, wait_transcript=False, contract_hint={})
    assert res.rollback and res.rollback[0]["kind"] == "check" and not res.committed
    assert st.edl.hash() == before and len(st.ops.ops) == n_ops


@pytest.mark.usefixtures("desktop_posture")
def test_the_contract_only_judges_runs_started_from_a_prompt(tmp_path):
    """A hand-built plan (tools, the shorts finishing pass) carries no user
    words to judge; only its blocking checks apply."""
    st, ids = _three(tmp_path)
    facts = F.facts_for(st)
    plan = F.plan_of(F.step("apply_lut", clip_id=ids[0], src="warm.cube"))
    res = executor.run_plan(st, plan, facts, emit=lambda e: None, cancel_event=None, prompt="translate",
                            validator=F.identity_validator, wait_transcript=False)
    assert res.committed and not res.rollback
    res2 = executor.run_plan(st, F.plan_of(F.step("apply_lut", clip_id=ids[1], src="warm.cube")), facts,
                             emit=lambda e: None, cancel_event=None, prompt="translate",
                             validator=F.identity_validator, wait_transcript=False, contract_hint={})
    assert res2.rollback and not res2.committed


# =========================================================================== end to end: rollback → clarify

@pytest.mark.usefixtures("no_downloads")
def test_a_wrong_model_plan_is_rolled_back_to_a_clarify_card(media, monkeypatch):
    """The Apple Intelligence failure of round 3: "make the whole video black
    and white" planned as noise-reduction-like audio edits. Any brain's plan
    goes through the same net: the run is undone in its batch, History has
    no op, the reply is a clarify card that says why, and a pick re-plans."""
    st, ids = _session(media, "s_k3_rollback")
    ui = dict(UI, selection=ids["B"])
    wrong = F.plan_of(F.step("set_volume", target=ids["A"], db=-6.0), F.step("set_clip_muted", clip_id=ids["C"],
                                                                              muted=True),
                      brain="apple_intelligence", title="Clean audio")
    F.route_with(monkeypatch, F.FakeRouted(wrong))
    before, n_ops = st.edl.hash(), len(st.ops.ops)
    events = _turn(st, "make the whole video black and white", ui)
    assert st.edl.hash() == before and len(st.ops.ops) == n_ops
    types = [e["type"] for e in events]
    assert "step" in types and "clarify" in types and "op" not in types and "verify" not in types
    said = _question_text(events)
    assert "I undid that" in said and "did not ask for" in said, said
    rec = pending.load_pending(Path(st.dir))
    assert rec is not None
    q = pending.pending_plan(rec).needs_input[0]
    assert q.key == "intent"
    assert executor.get_run(Path(st.dir).name).log.record.status == "clarify"
    # the pick re-plans the SAME words as that edit (the recipes brain now)
    monkeypatch.undo()
    options = [o.value for o in q.options]
    assert "color_look" in options, options
    out = F.collect(service.resume(st, rec["token"], {"intent": "color_look"}, ui_state=ui, history=[]))
    assert out[-1]["type"] == "done"
    luts = [[Path(str(x.params.get("src", ""))).name for x in c.effects] for c in v1(st.edl)]
    assert luts == [["mono.cube"]] * 3, (luts, _question_text(out))


# =========================================================================== text restyle tool

@pytest.mark.usefixtures("desktop_posture")
def test_set_text_style_restyles_without_touching_the_words(tmp_path):
    st = F.make_store(tmp_path)
    dispatch(st, "add_text", {"text": "Summer Trip", "start": 0.0, "end": 3.0, "y": 810, "size": 96})
    tid = next(c.id for t in st.edl.tracks for c in t.clips if getattr(c, "text", None) == "Summer Trip")
    n = len(st.ops.ops)
    out = dispatch(st, "set_text_style", {"clip_id": tid, "color": "#ff3b30", "size_scale": 1.25, "bold": True,
                                          "position": "top"})
    t = st.edl.get_clip(tid)[1]
    assert (t.text, t.start, t.end) == ("Summer Trip", 0.0, 3.0)
    assert (t.style.color, t.style.size, t.style.font) == ("#FF3B30", 120.0, "Inter-Black")
    assert float(t.transform.y) < st.edl.canvas.h * 0.34
    assert len(st.ops.ops) == n + 1 and "bold" in out["changed"]
    with pytest.raises(ValueError):
        dispatch(st, "set_text_style", {"clip_id": tid, "color": "red"})
    assert TOOL_STAGE["set_text_style"] == 8


@pytest.mark.usefixtures("no_downloads")
@pytest.mark.parametrize("phrase,want", [
    ("make the title red", {"color": "#FF3B30"}),
    ("change the title font to Anton", {"font": "Anton-Regular"}),
    ("make teh title biger", {"size": 120.0}),
    ("put the title at the top", {}),
])
def test_a_title_restyle_keeps_the_users_title(media, phrase, want):
    st, ids = _session(media, f"s_k3_style{len(phrase)}")
    dispatch(st, "add_text", {"text": "Summer Trip", "start": 0.0, "end": 3.0, "y": 810, "size": 96})
    events = _turn(st, phrase, dict(UI, selection=ids["B"]))
    ts = [c for t in st.edl.tracks for c in t.clips if getattr(c, "text", None) is not None and t.id != "captions"]
    assert [(t.text, t.start, t.end) for t in ts] == [("Summer Trip", 0.0, 3.0)], _question_text(events)
    assert {k: getattr(ts[0].style, k) for k in want} == want, _question_text(events)
    if phrase.endswith("top"):
        assert float(ts[0].transform.y) < st.edl.canvas.h * 0.34


# =========================================================================== the planner's semantic fix-ups

@pytest.mark.usefixtures("no_downloads")
@pytest.mark.parametrize("phrase,check", [
    ("speed up clip 2 and clip 3", lambda cl: [round(c.speed_factor, 2) for c in cl] == [1.0, 1.25, 1.25]),
    ("slow down clip 2 and speed up clip 3", lambda cl: [round(c.speed_factor, 2) for c in cl] == [1.0, 0.8, 1.25]),
    ("mute clip 1 and clip 3", lambda cl: [c.audio.mute for c in cl] == [True, False, True]),
    ("delete clips 1 and 2", lambda cl: [round(c.in_, 1) for c in cl] == [8.0]),
    ("make every clip except the first one warm", lambda cl: [bool(c.effects) for c in cl] == [False, True, True]),
    ("remove the first second and the last second", lambda cl: (round(cl[0].in_, 2), round(cl[-1].out, 2)) == (1.0, 11.0)),
    ("mute clip 2 but keep the music", lambda cl: [c.audio.mute for c in cl] == [False, True, False]),
    ("spead up teh second clip", lambda cl: [round(c.speed_factor, 2) > 1 for c in cl] == [False, True, False]),
    ("reduce the speed of the 2nd clip", lambda cl: [round(c.speed_factor, 2) for c in cl] == [1.0, 0.8, 1.0]),
])
def test_the_planner_reads_scope_and_direction_the_shared_way(media, phrase, check):
    st, ids = _session(media, f"s_k3_plan{abs(hash(phrase)) % 10**6}")
    events = _turn(st, phrase, dict(UI, selection=ids["B"]))
    assert check(v1(st.edl)), (_question_text(events), [c.model_dump(exclude_defaults=True) for c in v1(st.edl)])
    assert st.edl.get_track("music").muted is False


def test_the_on_device_draft_is_grounded_by_the_same_semantics():
    from video_ai_editor.agent.prompt.brains.content import ground_semantics
    from video_ai_editor.agent.prompt.schema import IntentItem
    fixed = ground_semantics([IntentItem(recipe="speed", slots={"factor": 1.25, "clip_ref": "clip 2"})],
                             "reduce the speed of clip 2")
    assert fixed[0].slots["factor"] == 0.8
    fixed = ground_semantics([IntentItem(recipe="volume", slots={"target": "music", "change": "up"})],
                             "turn the music down")
    assert fixed[0].slots["change"] == "down"
    assert ground_semantics([IntentItem(recipe="speed", slots={"factor": 2})], "slow it down to 2x") == []
    assert ground_semantics([IntentItem(recipe="mute", slots={"target": "music"})],
                            "mute clip 2 but keep the music") == []
