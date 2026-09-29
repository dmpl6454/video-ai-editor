"""Final QA round 2: Prompt-bar / Chat phrasings that made the WRONG edit.

Same harness as test_final_qa_prompt_phrases: the real key-free service on
the capcut-sweep session (three 4 s clips A/B/C on the main track, a 12 s
music bed at -14 dB, clip B selected, playhead 5.5 s). Before these fixes:

  * "bring the music down 4db" / "music down 10 db" set an ABSOLUTE level
    (the music got LOUDER), "turn the music up 3 db" set +3 dB;
  * "50% faster" / "speed up … by 50%" HALVED the speed, "25% slower" played
    at 0.25x;
  * "make the title red" / "title font size 80" deleted the user's title and
    added one saying "red" / "font size 80";
  * a request typed after a question was taken as its ANSWER ("add a title"
    then "mute the music" retitled the video "mute the music");
  * "flash in speed curve on the second clip" added Fade-to-White transitions;
    "jump cut speed ramp on the first clip" asked which part to cut;
  * "make this clip black and white" applied the teal-orange LUT; "make this
    black and white" graded every clip;
  * "change the glitch transition to a dissolve" also replaced the crossfade;
    "make the transitions longer" made them SHORTER cross dissolves;
  * "put the captions in the middle" / "make the captions bold" re-laid the
    captions in a different style;
  * a pick from the "I did not catch that" menu ended on "Nothing to change";
    "cut clip 2 at 7 seconds" asked for a range forever;
  * "remove the speed curve" said to press Delete (which deletes the clip).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from prompt_fixtures import no_downloads  # noqa: E402,F401

from test_prompt_capcut_sweep import (  # noqa: E402  (helpers + the module-scoped session fixture)
    ASKS, NOOP, UI, Case, _eq, _pre_captions, _pre_hero_c, _question_text, _session, _turn,
    media, music, texts, transitions, v1,
)
from test_final_qa_prompt_phrases import _pre_summer  # noqa: E402
from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.edl.snapshot import EDLStore  # noqa: E402


def _music_db(e) -> set[float]:
    return {round(c.audio.gain_db, 1) for c in music(e).clips}


def _speed_b(e) -> float:
    return round(v1(e)[1].speed_factor, 3)


def _pre_glitch_crossfade(st: EDLStore, ids) -> None:
    dispatch(st, "add_transition", {"at": 4.0, "type": "glitch", "duration": 0.5})
    dispatch(st, "add_transition", {"at": 8.0, "type": "fade", "duration": 0.5})


def _pre_default_captions(st: EDLStore, ids) -> None:
    dispatch(st, "add_caption_track", {"style": "default", "position": "bottom"})


def _cap_state(e):
    cap = e.get_track("captions")
    return (cap.config.style, cap.config.position, [(c.text, c.start) for c in cap.clips])


def _restyled(**want):
    """The user's own 'Summer Trip' title, same words and time, with the look asked."""
    def chk(e, ids):
        ts = texts(e)
        _eq([(t.text, round(t.start, 2), round(t.end, 2)) for t in ts], [("Summer Trip", 0.0, 3.0)])
        _eq({k: getattr(ts[0].style, k) for k in want}, want)
    return chk


def _summer_kept(e, ids) -> None:
    _eq([t.text for t in texts(e)], ["Summer Trip"])


def _lut_names(e) -> list[list[str]]:
    return [[Path(str(x.params.get("src", ""))).name for x in c.effects if x.type == "lut"] for c in v1(e)]


CASES: list[Case] = [
    # ---- a level change said without "by" -----------------------------------
    Case("bring the music down 4db", lambda e, ids: _eq(_music_db(e), {-18.0})),
    Case("music down 10 db", lambda e, ids: _eq(_music_db(e), {-24.0})),
    Case("turn the music up 3 db", lambda e, ids: _eq(_music_db(e), {-11.0})),
    Case("turn the music down by 10 db", lambda e, ids: _eq(_music_db(e), {-24.0})),
    Case("set the music to -20 db", lambda e, ids: _eq(_music_db(e), {-20.0})),
    Case("music -6db", lambda e, ids: _eq(_music_db(e), {-6.0})),
    Case("turn down clip 2 by 6 decibels",
         lambda e, ids: (_eq([c.audio.gain_db for c in v1(e)], [0.0, -6.0, 0.0]), _eq(_music_db(e), {-14.0}))),
    Case("turn down clip 2 by 6 db",
         lambda e, ids: (_eq([c.audio.gain_db for c in v1(e)], [0.0, -6.0, 0.0]), _eq(_music_db(e), {-14.0}))),
    # ---- speed percentages keep their direction ------------------------------
    Case("make the second clip 50% faster", lambda e, ids: _eq(_speed_b(e), 1.5)),
    Case("speed up the second clip by 50%", lambda e, ids: _eq(_speed_b(e), 1.5)),
    Case("make clip 2 25% slower", lambda e, ids: _eq(_speed_b(e), 0.75)),
    Case("slow down clip 2 by 20%", lambda e, ids: _eq(_speed_b(e), 0.8)),
    Case("clip 2 at 150% speed", lambda e, ids: _eq(_speed_b(e), 1.5)),
    # ---- restyling a title never replaces it ---------------------------------
    # (K3: the Prompt bar restyles it now — set_text_style — instead of
    # pointing at the Inspector; the words and timing stay the user's own)
    Case("make the title red", _restyled(color="#FF3B30"), pre=_pre_summer),
    Case("make the Summer Trip text bigger", _restyled(size=120.0), pre=_pre_summer),
    Case("title font size 80", _restyled(size=80.0), pre=_pre_summer),
    Case("make the text white with a black outline", _restyled(color="#FFFFFF", stroke_w=6.0), pre=_pre_summer),
    # ---- a speed curve by name is a speed curve --------------------------------
    Case("flash in speed curve on the second clip",
         lambda e, ids: (_eq(transitions(e), []), _eq(v1(e)[1].speed.get("name") if isinstance(v1(e)[1].speed, dict)
                                                      else None, "flash_in"))),
    Case("flash out speed curve on clip 3",
         lambda e, ids: (_eq(transitions(e), []), _eq(v1(e)[2].speed.get("name") if isinstance(v1(e)[2].speed, dict)
                                                      else None, "flash_out"))),
    Case("jump cut speed ramp on the first clip",
         lambda e, ids: _eq(v1(e)[0].speed.get("name") if isinstance(v1(e)[0].speed, dict) else None, "jump_cut")),
    # ---- a colour look is the one asked for, on the clip meant ---------------
    Case("make this clip black and white", lambda e, ids: _eq(_lut_names(e), [[], ["mono.cube"], []])),
    Case("make this clip warm", lambda e, ids: _eq(_lut_names(e), [[], ["warm.cube"], []])),
    Case("make this black and white", lambda e, ids: _eq(_lut_names(e), [[], ["mono.cube"], []])),
    Case("make the second clip black & white", lambda e, ids: _eq(_lut_names(e), [[], ["mono.cube"], []]),
         ui={"playhead": 5.5}),
    Case("trim the last clip to 3 seconds then make it black and white",
         lambda e, ids: _eq(_lut_names(e), [[], [], ["mono.cube"]]), ui={"playhead": 5.5}),
    Case("give the second clip a filter", ASKS, ui={"playhead": 5.5}, question="Which look"),
    # ---- existing transitions: change one type / change their length ---------
    Case("change the glitch transition to a dissolve",
         lambda e, ids: _eq(sorted((t.at, t.type) for t in transitions(e)), [(4.0, "dissolve"), (8.0, "fade")]),
         pre=_pre_glitch_crossfade),
    # 1.5x / ÷1.5 of 0.5 s, on the 30 fps frame grid (within a frame)
    Case("make the transitions longer",
         lambda e, ids: _eq(sorted((t.at, t.type, abs(t.duration - 0.75) <= 1 / 30) for t in transitions(e)),
                            [(4.0, "glitch", True), (8.0, "fade", True)]),
         pre=_pre_glitch_crossfade),
    Case("make the transitions shorter",
         lambda e, ids: _eq(sorted((t.at, t.type, abs(t.duration - 1 / 3) <= 1 / 30) for t in transitions(e)),
                            [(4.0, "glitch", True), (8.0, "fade", True)]),
         pre=_pre_glitch_crossfade),
    # ---- the captions' place / weight is never a re-lay ----------------------
    Case("put the captions in the middle",
         lambda e, ids: _eq(_cap_state(e)[:2], ("default", "center")), pre=_pre_default_captions),
    Case("captions at the top please",
         lambda e, ids: _eq(_cap_state(e)[:2], ("default", "top")), pre=_pre_default_captions),
    Case("make the captions bold", NOOP, pre=_pre_default_captions, question="Inspector"),
    # ---- "cut <clip> at <time>" is a split -----------------------------------
    Case("cut clip 2 at 7 seconds",
         lambda e, ids: _eq([round(c.start, 2) for c in v1(e)], [0.0, 4.0, 7.0, 8.0])),
    # ---- clip properties are removed, never "press Delete" -------------------
    Case("remove the speed curve from clip 3",
         lambda e, ids: _eq((v1(e)[2].speed_curve, v1(e)[2].speed_factor), (None, 1.0)), pre=_pre_hero_c),
    Case("remove the speed ramp from the last clip",
         lambda e, ids: _eq((v1(e)[2].speed_curve, v1(e)[2].speed_factor), (None, 1.0)), pre=_pre_hero_c),
]


@pytest.mark.usefixtures("no_downloads")
@pytest.mark.parametrize("case", CASES, ids=[f"{i:02d}-{c.phrase}" for i, c in enumerate(CASES)])
def test_r2_phrase_does_the_right_edit_or_says_why(media, case, request):
    st, ids = _session(media, f"s_r2{request.node.callspec.id[:2]}")
    ui = {k: (ids[v] if k == "selection" else v) for k, v in case.ui.items()}
    if case.pre:
        case.pre(st, ids)
    before = st.edl.hash()
    events = _turn(st, case.phrase, ui)
    e = st.edl
    errors = [x for x in events if x["type"] == "error"]
    assert not errors, errors
    said = _question_text(events)
    if case.expect == ASKS:
        assert e.hash() == before, "the timeline changed on a phrase that must ask"
        assert "?" in said and (case.question or "") in said, said
        return
    if case.expect == NOOP:
        assert e.hash() == before, f"the timeline changed on a phrase with nothing to do: {said}"
        assert (case.question or "") in said, said
        return
    assert e.hash() != before, f"nothing changed: {said}"
    ver = next((x for x in events if x["type"] == "verify"), None)
    assert ver is not None and ver["passed"] == ver["total"], (ver, said)
    assert "done with issues" not in said, said
    case.expect(e, ids)


def test_restyling_a_title_never_removes_it(media):
    st, ids = _session(media, "s_r2_style")
    _pre_summer(st, ids)
    for phrase in ("make the title red", "title font size 80"):
        _turn(st, phrase, dict(UI, selection=ids["B"]))
        _summer_kept(st.edl, ids)


# ------------------------------------------------ a request after a question

def _clarify(events) -> bool:
    return any(x["type"] == "clarify" for x in events)


@pytest.mark.usefixtures("no_downloads")
def test_a_new_request_after_a_question_is_planned_fresh(media):
    st, ids = _session(media, "s_r2_pend")
    _pre_summer(st, ids)
    ui = dict(UI, selection=ids["B"])
    assert _clarify(_turn(st, "add a title", ui))          # "What should the title say?"
    _turn(st, "mute the music", ui)
    assert music(st.edl).muted or all(c.audio.mute for c in music(st.edl).clips)
    _summer_kept(st.edl, ids)


@pytest.mark.usefixtures("no_downloads")
def test_a_request_after_a_range_question_does_not_loop(media):
    st, ids = _session(media, "s_r2_pend2")
    ui = dict(UI, selection=ids["B"])
    assert _clarify(_turn(st, "trim it", ui))
    _turn(st, "delete clip 3", ui)
    assert [c.id for c in v1(st.edl)] == [ids["A"], ids["B"]]


@pytest.mark.usefixtures("no_downloads")
def test_a_short_answer_is_still_an_answer(media):
    st, ids = _session(media, "s_r2_pend3")
    ui = dict(UI, selection=ids["B"])
    assert _clarify(_turn(st, "add a title", ui))
    _turn(st, "Road Trip", ui)
    assert "Road Trip" in [t.text for t in texts(st.edl)]


# ------------------------------------------------ the intent menu answers

@pytest.mark.usefixtures("no_downloads")
@pytest.mark.parametrize("prompt,answer", [
    ("its too long", "trim"),
    ("spead up teh secnd clip", "speed"),
    ("change Summer Trip to Autumn Days", "title"),
])
def test_a_pick_from_the_intent_menu_never_ends_on_nothing_to_change(media, prompt, answer):
    st, ids = _session(media, f"s_r2_menu{len(prompt)}")
    _pre_summer(st, ids)
    ui = dict(UI, selection=ids["B"])
    first = _turn(st, prompt, ui)
    if not _clarify(first):
        pytest.skip(f"{prompt!r} is no longer a menu: {_question_text(first)}")
    before = st.edl.hash()
    events = _turn(st, answer, ui)
    said = _question_text(events)
    assert "Nothing to change" not in said, said
    assert _clarify(events) or st.edl.hash() != before, said
    if answer == "speed":        # the typo'd words still name the SECOND clip
        assert [round(c.speed_factor, 2) != 1.0 for c in v1(st.edl)] == [False, True, False], said
    if answer == "title":
        assert [t.text for t in texts(st.edl)] == ["Autumn Days"], said


@pytest.mark.usefixtures("no_downloads")
def test_a_removal_menu_offers_a_removal(media):
    st, ids = _session(media, "s_r2_menu_rm")
    events = _turn(st, "delete the overlay", dict(UI, selection=ids["B"]))
    said = _question_text(events)
    if _clarify(events):
        opts = [o["label"] for x in events if x["type"] == "clarify" for q in x["questions"]
                for o in (q.get("options") or [])]
        assert any("Delete" in o or "Remove" in o for o in opts), opts


@pytest.mark.usefixtures("no_downloads")
def test_a_single_time_answer_to_the_range_question_offers_a_split(media):
    st, ids = _session(media, "s_r2_range")
    ui = dict(UI, selection=ids["B"])
    assert _clarify(_turn(st, "trim it", ui))
    said = _question_text(_turn(st, "at 7 seconds", ui))
    assert "split" in said.lower(), said


@pytest.mark.usefixtures("no_downloads")
def test_clear_all_effects_from_a_clip_removes_them(media):
    st, ids = _session(media, "s_r2_fx")
    dispatch(st, "add_effect", {"clip_id": ids["B"], "type": "vignette", "params": {}})
    dispatch(st, "apply_lut", {"clip_id": ids["B"], "src": "warm.cube"})
    events = _turn(st, "clear all effects from clip 2", dict(UI, selection=ids["B"]))
    said = _question_text(events)
    assert "press Delete" not in said, said
    assert v1(st.edl)[1].effects == [], said


def test_no_removal_reply_tells_the_user_to_press_delete_on_a_clip_property():
    from video_ai_editor.agent.prompt.planner import unsupported_removal_reply
    for clause in ("remove the speed curve", "remove the filter", "remove the zoom keyframes"):
        assert "press Delete" not in unsupported_removal_reply(clause), clause
    assert "press Delete" in unsupported_removal_reply("remove the sticker")


# ------------------------------------------------ Apple Intelligence drafts

def _fm_facts():
    from video_ai_editor.agent.prompt.facts import TimelineFacts
    return TimelineFacts.minimal(v1_clip_ids=["c_a", "c_b", "c_c"], clip_ids=["c_a", "c_b", "c_c", "m_1"],
                                 v1_boundaries=[4.0, 8.0], duration=12.0, track_ids=["v1", "music"],
                                 has_music=True, music_clip_ids=["m_1"], music_gain_db=-12.0)


def _fm_plan(prompt: str, draft: list[dict]):
    from test_c6_prompt_paraphrase import _fm_brain
    from video_ai_editor.agent.prompt import recipes
    from video_ai_editor.agent.prompt.brains.base import BrainRequest
    res = _fm_brain([draft]).plan(BrainRequest(prompt=prompt, facts=_fm_facts(), recipes=recipes.cards()),
                                  timeout_s=8)
    assert res.ok, res.reason
    return res.plan


def test_an_ai_one_clip_look_is_checked_on_that_clip_only():
    plan = _fm_plan("make the second clip grayscale",
                    [{"recipe": "color_look", "look": "mono", "clip_ref": "the second clip"}])
    assert [(s.tool, s.args.get("clip_id")) for s in plan.steps] == [("apply_lut", "c_b")]
    from video_ai_editor.agent.prompt.schema import bind_postconditions
    from video_ai_editor.agent.prompt.validate import validate_plan
    plan = validate_plan(plan, _fm_facts())
    # every check the step brings, the tool's default one included
    assert [p.args.get("clip_id") for p in bind_postconditions("apply_lut", dict(plan.steps[0].args))] == ["c_b"]
    eff = [p for p in plan.postconditions if p.check == "effect_present"]
    assert eff and all(p.args.get("clip_id") == "c_b" or p.args.get("all") is False for p in eff), eff


def test_effect_present_honours_its_clip():
    from video_ai_editor.agent.prompt import verify as V
    from video_ai_editor.agent.prompt.schema import Postcondition
    from video_ai_editor.edl.schema import EDL, Canvas, Clip, Effect, Track
    edl = EDL(canvas=Canvas(w=160, h=90, fps=30), tracks=[Track(id="v1", type="video", clips=[
        Clip(id="c_a", src="/a.mp4", in_=0, out=4, start=0),
        Clip(id="c_b", src="/a.mp4", in_=4, out=8, start=4, effects=[Effect(type="lut", params={"src": "m"})])])])

    class Ctx:
        pass
    ctx = Ctx()
    ctx.edl = edl
    one = Postcondition(check="effect_present", human="x", args={"type": "lut", "track": "v1", "clip_id": "c_b"})
    every = Postcondition(check="effect_present", human="x", args={"type": "lut", "track": "v1"})
    assert V.c_effect_present(ctx, one).passed is True
    assert V.c_effect_present(ctx, every).passed is False


def test_an_ai_music_plan_for_a_named_clip_edits_that_clip():
    plan = _fm_plan("turn down clip 2 by 6 decibels", [{"recipe": "volume", "target": "music", "db": -6}])
    assert [(s.tool, s.args) for s in plan.steps] == [("set_volume", {"target": "c_b", "db": -6.0})]
