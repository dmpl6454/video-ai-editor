"""Final QA round 3 (prompt-assistant-backend): Prompt-bar phrasings that
still made the WRONG edit (or none) after round 2.

Every phrase runs through the real key-free service on the capcut-sweep
session (three 4 s clips A/B/C on the main track, a 12 s music bed, clip B
selected, playhead 5.5 s) and the EDL is checked. Before these fixes:

  * "keep only the first 10 seconds" DELETED the first 10 seconds;
  * "slow the whole video down" / "slow the footage down" sped it up (1.25x);
  * "mute clip 3 and slow it down" slowed every clip ("it" was the video);
  * "make the title longer / move the title to 4 seconds …" deleted the
    user's title and added one saying "longer" / "to 4 seconds";
  * "make the whole video black and white" was unread (Apple Intelligence
    then denoised every clip);
  * with a 40 s bed under 12 s of video, "cut the last 2 seconds" cut the
    MUSIC and "text at the end" landed 28 s after the picture;
  * "change the crossfade to a wipe" rewrote EVERY transition;
  * "a wipe between the first two clips" went on every cut;
  * "split clip 2" was unread; "remove the screen blend" re-applied Screen;
  * "undo the last 3 edits" undid one; "duck the music more" changed nothing;
  * "flip the sticker" said "Want a sticker?"; "remove all animations"
    asked which clip to animate.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import no_downloads  # noqa: E402,F401

from test_final_qa_prompt_phrases import _pre_long_bed, _pre_summer  # noqa: E402
from test_prompt_capcut_sweep import (  # noqa: E402  (helpers + the module-scoped session fixture)
    ASKS, NO_SEL, NOOP, UI, Case, _eq, _portrait, _pre_anim_b, _pre_overlay, _pre_overlay_screen,
    _pre_sticker, _question_text, _session, _turn, anims, blends, dur, looks, media, music, stickers,
    texts, transitions, v1,
)
from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.edl.snapshot import EDLStore  # noqa: E402

# --------------------------------------------------------------------------- setups / reads


def _pre_gl_cf(st: EDLStore, ids) -> None:
    dispatch(st, "add_transition", {"at": 4.0, "type": "glitch", "duration": 0.5})
    dispatch(st, "add_transition", {"at": 8.0, "type": "fade", "duration": 1.0})


def _pre_duck18(st: EDLStore, ids) -> None:
    dispatch(st, "set_duck", {"track": "music", "enabled": True, "to_db": -18.0})


def _speeds(e) -> list[float]:
    return [round(c.speed_factor, 3) for c in v1(e)]


def _tr(e) -> list[tuple[float, str, float]]:
    return sorted((round(t.at, 2), t.type, round(float(t.duration), 2)) for t in transitions(e))


def _plain(c) -> bool:
    return (c.speed in (None, 1.0) and not c.reverse and not c.audio.mute and not c.effects
            and c.video_fade_in == 0 and not getattr(c.transform, "flip_h", False))


def _only_c(pred):
    def chk(e, ids):
        by = {c.id: c for c in v1(e)}
        assert _plain(by[ids["A"]]) and _plain(by[ids["B"]]), [c.model_dump(exclude_defaults=True) for c in v1(e)]
        assert pred(by[ids["C"]]), by[ids["C"]].model_dump(exclude_defaults=True)
    return chk


def _summer_at(a: float, b: float):
    def chk(e, ids):
        ts = [t for t in texts(e)]
        _eq([(t.text, round(t.start, 2), round(t.end, 2)) for t in ts], [("Summer Trip", a, b)])
        # the user's own look and place survive
        _eq((round(float(ts[0].transform.y)), round(float(ts[0].style.size))), (810, 96))
    return chk


def _mono_all(e, ids):
    _eq(looks(e), [["mono.cube"]] * 3)


# --------------------------------------------------------------------------- the phrases

CASES: list[Case] = [
    # ---- keep only … -------------------------------------------------------
    Case("keep only the first 10 seconds", lambda e, ids: _eq((dur(e), round(v1(e)[0].in_, 2)), (10.0, 0.0))),
    Case("keep the first 10 seconds", lambda e, ids: _eq((dur(e), round(v1(e)[0].in_, 2)), (10.0, 0.0))),
    Case("just keep the last 5 seconds", lambda e, ids: _eq((dur(e), round(v1(e)[0].in_, 2)), (5.0, 7.0))),
    Case("keep the last 5 seconds only", lambda e, ids: _eq((dur(e), round(v1(e)[0].in_, 2)), (5.0, 7.0))),
    Case("keep only the first 10 seconds", lambda e, ids: _eq((dur(e), round(v1(e)[0].in_, 2)), (10.0, 0.0)),
         pre=_pre_long_bed),
    # ---- slow … down -------------------------------------------------------
    Case("slow the whole video down", lambda e, ids: _eq(_speeds(e), [0.8, 0.8, 0.8])),
    Case("slow the footage down", lambda e, ids: _eq(_speeds(e), [0.8, 0.8, 0.8])),
    Case("slow clip 3 down", lambda e, ids: _eq(_speeds(e), [1.0, 1.0, 0.8])),
    Case("slow the last clip down a little", lambda e, ids: _eq(_speeds(e)[:2] + [_speeds(e)[2] < 1], [1.0, 1.0, True])),
    # ---- "it" is the clip named before it ---------------------------------
    Case("mute clip 3 and slow it down", _only_c(lambda c: c.audio.mute and c.speed_factor == 0.8)),
    Case("reverse clip 3 and flip it", _only_c(lambda c: c.reverse and c.transform.flip_h)),
    Case("take the last clip, reverse it and mute it", _only_c(lambda c: c.reverse and c.audio.mute)),
    Case("make the first clip black and white and slow it down",
         lambda e, ids: (_eq(looks(e), [["mono.cube"], [], []]), _eq(_speeds(e), [0.8, 1.0, 1.0]))),
    Case("cut clip 3 at 10 seconds and make the second half 2x speed",
         lambda e, ids: (_eq([round(c.in_, 2) for c in v1(e)], [0.0, 4.0, 8.0, 10.0]),
                         _eq(_speeds(e), [1.0, 1.0, 1.0, 2.0]))),
    Case("put the pip in multiply and make it fade in",
         lambda e, ids: (_eq([c.video_fade_in for c in v1(e)], [0, 0, 0]), _eq(blends(e), ["multiply"]),
                         _eq([c.anim_in for c in e.get_track("v2").clips], ["fade_in"])),
         pre=_pre_overlay),
    # ---- an existing title's TIMING ---------------------------------------
    Case("make the title longer", _summer_at(0.0, 4.5), pre=_pre_summer),
    Case("move the title to 4 seconds", _summer_at(4.0, 7.0), pre=_pre_summer),
    Case("extend the title to 5 seconds", _summer_at(0.0, 5.0), pre=_pre_summer),
    Case("shorten the title to 2 seconds", _summer_at(0.0, 2.0), pre=_pre_summer),
    Case("make the title stay on screen for 6 seconds", _summer_at(0.0, 6.0), pre=_pre_summer),
    Case("make the text appear at 2 seconds", _summer_at(2.0, 5.0), pre=_pre_summer),
    Case("title from 1s to 4s", _summer_at(1.0, 4.0), pre=_pre_summer),
    Case("make the title last 5 seconds", _summer_at(0.0, 5.0), pre=_pre_summer),
    # ---- the whole video black and white ----------------------------------
    Case("make the whole video black and white", _mono_all),
    Case("make the entire video black and white", _mono_all),
    # ---- "the end" is the PICTURE's end under a long bed ------------------
    Case("chop off the last 3 seconds", lambda e, ids: _eq(dur(e), 9.0), pre=_pre_long_bed),
    Case("cut the last 2 seconds", lambda e, ids: _eq(dur(e), 10.0), pre=_pre_long_bed),
    Case("trim the ending by 3 seconds", lambda e, ids: _eq(dur(e), 9.0), pre=_pre_long_bed),
    Case("add text 'Thanks for watching' at the end",
         lambda e, ids: _eq([(t.text, round(t.start, 2), round(t.end, 2)) for t in texts(e)],
                            [("Thanks for watching", 9.0, 12.0)]), pre=_pre_long_bed),
    # ---- ONE existing transition, by its name -----------------------------
    Case("change the crossfade to a wipe",
         lambda e, ids: _eq([(a, "wipe" in t) for a, t, _d in _tr(e)] + [_tr(e)[0][1]], [(4.0, False), (8.0, True), "glitch"]),
         pre=_pre_gl_cf),
    Case("change the crossfade transition to a wipe",
         lambda e, ids: _eq([(a, "wipe" in t) for a, t, _d in _tr(e)] + [_tr(e)[0][1]], [(4.0, False), (8.0, True), "glitch"]),
         pre=_pre_gl_cf),
    Case("change the crossfade to a zoom",
         lambda e, ids: _eq([_tr(e)[0][1], "zoom" in _tr(e)[1][1]], ["glitch", True]), pre=_pre_gl_cf),
    Case("replace the glitch with a crossfade",
         lambda e, ids: _eq([(a, t) for a, t, _d in _tr(e)] + [_tr(e)[1][2]], [(4.0, "fade"), (8.0, "fade"), 1.0]),
         pre=_pre_gl_cf),
    Case("swap the glitch for a dissolve",
         lambda e, ids: _eq([(a, t) for a, t, _d in _tr(e)], [(4.0, "dissolve"), (8.0, "fade")]), pre=_pre_gl_cf),
    Case("make the crossfade longer",
         lambda e, ids: _eq(_tr(e), [(4.0, "glitch", 0.5), (8.0, "fade", 1.5)]), pre=_pre_gl_cf),
    Case("make the crossfade transition longer",
         lambda e, ids: _eq(_tr(e), [(4.0, "glitch", 0.5), (8.0, "fade", 1.5)]), pre=_pre_gl_cf),
    Case("make the glitch transition 1 second",
         lambda e, ids: _eq(_tr(e), [(4.0, "glitch", 1.0), (8.0, "fade", 1.0)]), pre=_pre_gl_cf),
    Case("make the glitch transition 1 second long",
         lambda e, ids: _eq(_tr(e), [(4.0, "glitch", 1.0), (8.0, "fade", 1.0)]), pre=_pre_gl_cf),
    # ---- a transition on ONE cut ------------------------------------------
    Case("put a wipe between the first two clips", lambda e, ids: _eq([(a, t) for a, t, _d in _tr(e)], [(4.0, "wiperight")])),
    Case("add a wipe between clip 1 and clip 2", lambda e, ids: _eq([(a, t) for a, t, _d in _tr(e)], [(4.0, "wiperight")])),
    Case("add a wipe between clips 2 and 3", lambda e, ids: _eq([(a, t) for a, t, _d in _tr(e)], [(8.0, "wiperight")])),
    # ---- split a named clip -----------------------------------------------
    Case("split clip 2", lambda e, ids: _eq([round(c.in_, 2) for c in v1(e)], [0.0, 4.0, 5.5, 8.0])),
    Case("split clip 3 at 10 seconds", lambda e, ids: _eq([round(c.in_, 2) for c in v1(e)], [0.0, 4.0, 8.0, 10.0])),
    Case("cut clip one in half", lambda e, ids: _eq([round(c.in_, 2) for c in v1(e)], [0.0, 2.0, 4.0, 8.0])),
    Case("split clip 3", ASKS, question="Where in clip 3"),
    # ---- "use my photo": a sticker's artwork is not a photo ---------------
    Case("use my photo as the background", NOOP, pre=_portrait(_pre_sticker), question="no picture"),
    # ---- remove a blend ----------------------------------------------------
    Case("remove the screen blend", lambda e, ids: _eq(blends(e), ["normal"]), pre=_pre_overlay_screen),
    Case("remove the screen blend mode", lambda e, ids: _eq(blends(e), ["normal"]), pre=_pre_overlay_screen),
    Case("turn off the blend mode on the overlay", lambda e, ids: _eq(blends(e), ["normal"]), pre=_pre_overlay_screen),
    Case("remove the blend", lambda e, ids: _eq(blends(e), ["normal"]), pre=_pre_overlay_screen),
    # ---- duck deeper -------------------------------------------------------
    Case("duck the music more", lambda e, ids: _eq(music(e).duck.to_db, -24.0), pre=_pre_duck18),
    # ---- stickers and animations ------------------------------------------
    Case("flip the sticker horizontally", lambda e, ids: _eq([s.transform.flip_h for s in stickers(e)], [True]),
         pre=_pre_sticker),
    Case("make the sticker bigger", NOOP, pre=_pre_sticker, question="Inspector"),
    Case("remove all animations", lambda e, ids: _eq(anims(e), [(None, None, None)] * 3), pre=_pre_anim_b, ui=NO_SEL),
    Case("remove the zoom animation", lambda e, ids: _eq(anims(e)[1], (None, "fade_out", None)), pre=_pre_anim_b,
         ui=NO_SEL),
]


@pytest.mark.usefixtures("no_downloads")
@pytest.mark.parametrize("case", CASES, ids=[f"{i:02d}-{c.phrase}" for i, c in enumerate(CASES)])
def test_r3_phrase_does_the_right_edit_or_says_why(media, case, request):
    st, ids = _session(media, f"s_r3{request.node.callspec.id[:2]}")
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


# --------------------------------------------------------------------------- multi-turn / replies

def test_a_clause_that_made_no_step_is_not_reported_done(media):
    st, ids = _session(media, "s_r3_len")
    events = _turn(st, "trim clip 3 to 3 seconds and speed it up", dict(UI, selection=ids["B"]))
    said = _question_text(events)
    by = {c.id: c for c in v1(st.edl)}
    assert _plain(by[ids["A"]]) and _plain(by[ids["B"]]), said
    assert "Clip length · Speed: done" not in said, said


def test_split_a_named_clip_then_answer_the_time(media):
    st, ids = _session(media, "s_r3_split")
    ui = dict(UI, selection=ids["B"])
    first = _turn(st, "split clip 3", ui)
    assert "Where in clip 3" in _question_text(first)
    _turn(st, "at 10 seconds", ui)
    _eq([round(c.in_, 2) for c in v1(st.edl)], [0.0, 4.0, 8.0, 10.0])


def test_undo_honours_a_count(media):
    st, ids = _session(media, "s_r3_undo")
    for cid in (ids["A"], ids["B"], ids["C"]):
        dispatch(st, "set_clip_reverse", {"clip_id": cid, "reverse": True})
    events = _turn(st, "undo the last 3 edits", dict(UI, selection=ids["B"]))
    said = _question_text(events)
    assert [c.reverse for c in v1(st.edl)] == [False, False, False], said
    assert "3" in said, said


def test_duck_more_when_already_as_deep_as_asked_says_nothing_changed(media):
    st, ids = _session(media, "s_r3_duck")
    dispatch(st, "set_duck", {"track": "music", "enabled": True, "to_db": -18.0})
    before = st.edl.hash()
    events = _turn(st, "duck the music to -18 dB", dict(UI, selection=ids["B"]))
    said = _question_text(events)
    assert st.edl.hash() == before
    assert "done" not in said.split("—", 1)[-1][:12] and "already" in said, said
