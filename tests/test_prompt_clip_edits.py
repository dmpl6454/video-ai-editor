"""The key-free clip edits (wave D3, lane E3) — the pieces under the sweep.

`tests/test_prompt_capcut_sweep.py` runs 100+ phrases end to end; this file
pins the rules each phrase leans on, one at a time: which clip a clause
names, how an internal reference binds (or asks), how "the last N seconds"
becomes a split, that a model's `clip_ref` can never widen into EVERY clip,
the verifier grading `$v1_last` as one clip, `reorder_clips` really
reordering the main track, and the router refusing an unasked reorder.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture  # noqa: E402,F401

from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.agent.prompt import clip_expanders as CX  # noqa: E402
from video_ai_editor.agent.prompt import clip_slots as CS  # noqa: E402
from video_ai_editor.agent.prompt import grammar as G  # noqa: E402
from video_ai_editor.agent.prompt import planner as P  # noqa: E402
from video_ai_editor.agent.prompt import recipes as R  # noqa: E402
from video_ai_editor.agent.prompt import slots as S  # noqa: E402
from video_ai_editor.agent.prompt.facts import TimelineFacts  # noqa: E402

THREE = TimelineFacts.minimal(duration=12.0, v1_clip_ids=["c_a", "c_b", "c_c"], clip_ids=["c_a", "c_b", "c_c", "m_1"],
                              track_ids=["v1", "music"], v1_boundaries=[4.0, 8.0], selection="c_b", playhead=5.5,
                              has_music=True, music_clip_ids=["m_1"], music_gain_db=-14.0)
NO_SELECTION = THREE.with_(selection=None)


@pytest.mark.parametrize("text,ref", [
    ("delete the second clip", "$v1_nth:2"), ("clip 3", "$v1_nth:3"), ("the 2nd shot", "$v1_nth:2"),
    ("the first clip", "$v1_first"), ("clip 1", "$v1_first"), ("the opening shot", "$v1_first"),
    ("the last one", "$v1_last"), ("the final clip", "$v1_last"), ("the second to last clip", "$v1_nth:-2"),
    ("the middle clip", "$v1_nth:mid"), ("the clip at 0:05", "$v1_at:5"), ("the clip at 9s", "$v1_at:9"),
    ("this clip", "$selected"), ("this part", "$selected"), ("every clip", "$v1_all"),
    ("the first 5 seconds", None), ("zoom in on the product", None), ("speed up clip 2 by 2x", "$v1_nth:2"),
])
def test_which_clip_a_clause_names(text, ref):
    assert G.clip_ref_of(text) == ref


@pytest.mark.parametrize("ref,facts,bound,asks", [
    ("$v1_nth:2", THREE, "c_b", None), ("$v1_nth:-2", THREE, "c_b", None), ("$v1_nth:mid", THREE, "c_b", None),
    ("$v1_nth:5", THREE, None, "only 3 clips"), ("$v1_at:9", THREE, "c_c", None), ("$v1_at:4", THREE, "c_b", None),
    ("$v1_at:30", THREE, None, "no clip on the main track at 30s"), ("$selected", THREE, "c_b", None),
    ("$selected", NO_SELECTION, None, "Nothing is selected"), ("$v1_last", THREE, "$v1_last", None),
    ("$v1_nth:mid", THREE.with_(v1_clip_ids=["a", "b"], v1_boundaries=[6.0]), None, "middle one"),
])
def test_binding_a_reference_names_one_clip_or_asks(ref, facts, bound, asks):
    got, q = CX.bind_clip(ref, facts)
    assert got == bound
    assert (q is None) if asks is None else (asks in q and "?" in q), q


def test_a_models_clip_ref_never_widens_into_every_clip():
    """`normalize_slots` used to turn any clip_ref it could not read into
    `$v1_all` — a model's "delete_clip" with clip_ref "the blue one" would
    have ripple-deleted the whole timeline."""
    assert R.normalize_slots("delete_clip", {"clip_ref": "second"}) == {"clip_ref": "$v1_nth:2"}
    assert R.normalize_slots("delete_clip", {"clip_ref": "the blue one"}) == {}
    assert R.normalize_slots("speed", {"clip_ref": "the blue one"})["clip_ref"] == "$v1_all"   # unchanged rule
    p = P.compose([R.Intent("delete_clip", R.normalize_slots("delete_clip", {"clip_ref": "the blue one"}))],
                  NO_SELECTION)
    assert not p.steps and "Which clip" in (p.reply or "")


def test_a_plural_or_every_clip_delete_asks():
    for prompt in ("delete the clips", "delete every clip"):
        p = P.plan(prompt, THREE)
        assert not p.steps and "?" in (p.reply or ""), (prompt, p.reply)


@pytest.mark.parametrize("rng,steps,targets,asks", [
    (S.TimeRange(kind="last", end=3.0), [("split_at", 9.0)], ["$v1_last"], None),
    (S.TimeRange(kind="last", end=4.0), [], ["c_c"], None),                      # starts on the seam
    (S.TimeRange(kind="last", end=8.0), [], ["c_b", "c_c"], None),
    (S.TimeRange(kind="last", end=5.0), [], [], "just the last clip"),            # across a cut: ask
    (S.TimeRange(kind="first", end=2.0), [("split_at", 2.0)], ["c_a"], None),
    (S.TimeRange(kind="first", end=6.0), [("split_at", 6.0)], ["c_a", "c_b"], None),
    (S.TimeRange(kind="abs", start=4.0, end=6.0), [("split_at", 6.0)], ["c_b"], None),
    (S.TimeRange(kind="abs", start=5.0, end=6.0), [], [], "starts inside a clip"),
])
def test_an_edit_over_the_last_or_first_n_seconds(rng, steps, targets, asks):
    pre, clips, q = CX.range_targets(rng, THREE, "slow down")
    assert [(s.tool, s.args["time"]) for s in pre] == steps
    assert clips == targets
    assert (q is None) if asks is None else (asks in q and "?" in q), q


@pytest.mark.parametrize("clause,rng", [
    ("remove 3s to 5s", (3.0, 5.0)), ("delete 00:04-00:06", (4.0, 6.0)), ("cut 1:05 to 1:10", (65.0, 70.0)),
    ("trim 2 seconds off the end", (10.0, 12.0)), ("cut the start by 1.5 s", (0.0, 1.5)),
    ("delete everything after 10 seconds", (10.0, 12.0)), ("remove anything before 0:03", (0.0, 3.0)),
])
def test_bare_ranges(clause, rng):
    got = CS.bare_range(clause)
    assert got is not None and got.resolve(100.0 if rng[1] > 12 else 12.0) == rng
    assert CS.bare_range("swap 2 to 3 clips") is None


@pytest.mark.parametrize("prompt,seam", [
    ("add a transition between the first and second clip", 1), ("a dissolve between clips 2 and 3", 2),
    ("add a fade after the second clip", 2), ("add a transition between the first and third clip", None),
])
def test_the_seam_a_transition_names(prompt, seam):
    assert CS.seam_index(S.normalize(prompt)) == seam


def test_zoom_and_adjust_readings():
    z = CS.READERS["zoom"](G.detect("zoom this clip to 150%").hits[0], S.Slots())
    # "to 150%" is a level the user named (review RD3: `absolute`)
    assert z == {"clip_ref": "$selected", "direction": "in", "scale": 1.5, "style": "static", "absolute": True}
    z = CS.READERS["zoom"](G.detect("slowly zoom out on the first clip").hits[0], S.Slots())
    assert z["direction"] == "out" and z["style"] == "slow" and z["clip_ref"] == "$v1_first"
    a = CS.READERS["adjust"](G.detect("it looks too dark").hits[0], S.Slots())
    assert (a["property"], a["change"]) == ("brightness", "up")
    a = CS.READERS["adjust"](G.detect("lower the contrast on the second clip").hits[0], S.Slots())
    assert (a["property"], a["change"], a["clip_ref"]) == ("contrast", "down", "$v1_nth:2")


def test_removing_a_feature_never_adds_it():
    """"remove the captions" used to LAY captions, "remove the filter" to
    apply a LUT — both verified and committed."""
    for prompt in ("remove the captions", "remove the filter", "turn off the transitions", "delete the text"):
        det = G.detect(prompt)
        assert det.intents == ["remove_feature"], (prompt, det.intents)
        p = P.plan(prompt, THREE)
        assert not p.steps and "say 'undo'" in (p.reply or ""), (prompt, p.reply)
    # the removals that DO have a recipe keep it
    assert G.detect("remove the music").intents == ["remove_music"]
    assert G.detect("remove the silences").intents == ["remove_silences"]
    assert G.detect("remove the second clip").intents == ["delete_clip"]


def test_deleting_the_only_clip_asks():
    one = TimelineFacts.minimal(duration=12.0, v1_clip_ids=["c_a"], clip_ids=["c_a"], track_ids=["v1"])
    for prompt in ("remove the first part", "delete the last clip"):
        p = P.plan(prompt, one)
        assert not p.steps and "only clip" in (p.reply or ""), (prompt, p.reply)


def test_a_sticker_request_says_where_stickers_are():
    p = P.plan("add a sticker", THREE)
    assert p.intent == "sticker" and not p.steps and "Stickers panel" in p.reply and "?" in p.reply


def test_the_verifier_grades_the_last_clip_alone(tmp_path, desktop_posture):
    """`speed_equals(clip_id=$v1_last)` measured EVERY v1 clip, so "slow
    motion on the last clip" did the right edit and reported it failed."""
    from video_ai_editor.agent.prompt import verify as V
    store = F.make_store(tmp_path)
    dispatch(store, "split_at", {"track": "v1", "time": 6.0})
    last = store.edl.get_track("v1").clips[-1].id
    dispatch(store, "set_speed", {"clip_id": last, "factor": 0.5})
    ctx = V.VerifyCtx(store=store, plan=None, exec_result=None, facts_before=F.facts_for(store))  # type: ignore[arg-type]
    pc = R.pc("speed_equals", "the speed matches", clip_id="$v1_last", factor=0.5)
    assert V.c_speed_equals(ctx, pc).passed is True
    pc = R.pc("speed_equals", "the speed matches", clip_id="$v1_first", factor=0.5)
    assert V.c_speed_equals(ctx, pc).passed is False


def test_reorder_clips_really_reorders_the_main_track(tmp_path, desktop_posture):
    """`_ripple_close_gap` sorts the main lane by start, so `reorder_clips`
    on v1 put every clip back where it was and reported success."""
    store = F.make_store(tmp_path)
    dispatch(store, "split_at", {"track": "v1", "time": 4.0})
    dispatch(store, "split_at", {"track": "v1", "time": 8.0})
    a, b, c = (x.id for x in store.edl.get_track("v1").clips)
    dispatch(store, "reorder_clips", {"track": "v1", "order": [c, a, b]})
    clips = sorted(store.edl.get_track("v1").clips, key=lambda x: x.start)
    assert [x.id for x in clips] == [c, a, b]
    assert [round(x.start, 3) for x in clips] == [0.0, 4.0, 8.0]
    assert [round(x.in_, 3) for x in clips] == [8.0, 0.0, 4.0]


def test_the_router_refuses_an_unasked_reorder_or_duplicate():
    from video_ai_editor.agent.prompt.brains import router
    from video_ai_editor.agent.prompt.schema import Plan, Step
    plan = Plan.new(intent="x", brain="apple_intelligence",
                    steps=[Step(tool="reorder_clips", args={"track": "v1", "order": ["c_c", "c_a", "c_b"]}, why="x"),
                           Step(tool="duplicate_clip", args={"clip_id": "c_a"}, why="x")])
    assert router.ungrounded_restructure(plan, "add captions") == ["duplicate_clip", "reorder_clips"]
    assert router.ungrounded_restructure(plan, "move the last clip to the start and duplicate the first clip") == []


def _fm_steps(prompt: str, intents: list[dict], facts: TimelineFacts):
    from video_ai_editor.agent.prompt.brains import fm
    from video_ai_editor.agent.prompt.brains.base import BrainRequest
    probe = {"ok": True, "available": True, "state": "available", "fix": None, "os": "27.0.0", "languages": ["en"]}
    answer = {"ok": True, "model": "apple-fm", "latency_ms": 900,
              "draft": {"intents": intents, "exclusions": [], "needs_input": [], "confidence": 0.85, "reply": "ok"}}

    def runner(argv, stdin, timeout_s):
        return 0, json.dumps(probe if argv[-1] == "probe" else answer).encode(), b""

    brain = fm.FMBrain(runner=runner, helper=sys.executable, darwin=True, macos=(27, 0), frozen=False)
    res = brain.plan(BrainRequest(prompt=prompt, facts=facts, recipes=R.cards()), timeout_s=8)
    assert res.ok, res.reason
    return res.plan


def test_an_apple_intelligence_delete_without_a_clip_asks_instead_of_guessing():
    """The FM helper has no `clip_ref` field: its delete_clip draft names no
    clip, and with nothing selected that is a question, not a guess."""
    p = _fm_steps("get rid of that bit in the middle", [{"recipe": "delete_clip"}], NO_SELECTION)
    assert not p.steps and "Which clip" in (p.reply or ""), p.reply
    p = _fm_steps("get rid of that bit", [{"recipe": "delete_clip"}], THREE)          # the selection
    assert [(s.tool, s.args) for s in p.steps] == [("ripple_delete", {"clip_id": "c_b"})]
