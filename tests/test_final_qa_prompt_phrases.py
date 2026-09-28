"""Final QA (0.8.0 sweep): Prompt-bar phrasings that made the WRONG edit.

Every phrase runs through the real key-free service on the capcut-sweep
session (three 4 s clips A/B/C on the main track, a 12 s music bed, clip B
selected, playhead 5.5 s) — grammar, planner, validate, executor, dispatch,
verifier — and the EDL is checked for the right edit, or proved unchanged
with the reply saying why. Before these fixes:

  * split particles did the opposite: "turn the robot voice off" turned Robot
    ON, "take the transitions out" ADDED cross-dissolves ("done");
  * "reset the speed" / "normal speed" set 1.25x (the default factor);
  * "set it to 4:5 for instagram" ran a whole automatic edit (hook, captions,
    cuts, crossfades);
  * "change the Summer Trip text to Winter Trip" ADDED a new title reading
    "to Winter Trip" and deleted the user's own; "delete the Summer Trip
    title" asked what a new title should say;
  * "add a lower third saying Jane Doe, Producer" asked for the name it was
    given (recipes) or made a big SUPER title (Apple Intelligence);
  * "make the captions yellow / bigger" re-laid every cue unchanged ("4/4");
  * with a 40 s bed under 12 s of video, correct cuts reported "done with
    issues" and "deleted the last clip (32.0s)";
  * "mute everything except the voiceover" left the music playing, and
    "… except the music" muted the MUSIC;
  * "remove the glitch effect" (a release-notes phrase) refused, and "remove
    the voiceover" offered a NEW voice-over;
  * "make the overlay 50% transparent" / "move the overlay to the top right
    corner" got off-topic questions from Apple Intelligence.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import no_downloads  # noqa: E402,F401

from test_prompt_capcut_sweep import (  # noqa: E402  (helpers + the module-scoped session fixture)
    ASKS, NOOP, UI, Case, _eq, _pre_captions, _pre_echo_b_robot_c, _pre_hero_c, _pre_overlay,
    _pre_robot, _pre_transitions, _pre_vo, _pre_vo_on_music, _question_text, _session, _turn,
    media, music, texts, transitions, v1, vo_clips, voices,
)
from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.edl.schema import TextClip  # noqa: E402
from video_ai_editor.edl.snapshot import EDLStore  # noqa: E402


# --------------------------------------------------------------------------- setups

def _pre_speed_a(st: EDLStore, ids) -> None:
    dispatch(st, "set_speed", {"clip_id": ids["A"], "factor": 2.0})


def _pre_glitch(st: EDLStore, ids) -> None:
    dispatch(st, "add_transition", {"at": 4.0, "type": "fade"})
    dispatch(st, "add_transition", {"at": 8.0, "type": "glitch"})


def _pre_rgb_c(st: EDLStore, ids) -> None:
    dispatch(st, "add_effect", {"clip_id": ids["C"], "type": "rgb_split", "params": {}})


def _pre_summer(st: EDLStore, ids) -> None:
    dispatch(st, "add_text", {"text": "Summer Trip", "start": 0.0, "end": 3.0, "y": 810, "size": 96})


def _pre_summer_and_sale(st: EDLStore, ids) -> None:
    _pre_summer(st, ids)
    dispatch(st, "add_text", {"text": "SALE", "start": 5.0, "end": 7.0})


def _pre_long_bed(st: EDLStore, ids) -> None:
    """QA-083's import: the WHOLE 40 s bed under 12 s of video."""
    for c in list(music(st.edl).clips):
        dispatch(st, "ripple_delete", {"clip_id": c.id})
    bed = F.music_bed(st.dir, name="bed40.wav", dur=40.0)
    dispatch(st, "add_clip", {"track": "music", "src": str(bed), "in": 0.0, "out": 40.0, "start": 0.0})
    assert st.edl.duration == pytest.approx(40.0) and st.edl.video_extent() == pytest.approx(12.0)


# --------------------------------------------------------------------------- checks

def _speeds(e) -> list[float]:
    return [round(c.speed_factor, 3) for c in v1(e)]


def _summer(e) -> TextClip:
    return next(t for t in texts(e) if t.role != "lower_third")


def _retexted(words: str):
    def chk(e, ids):
        ts = [t for t in texts(e) if t.role != "caption"]
        _eq([t.text for t in ts], [words])
        t = ts[0]
        # the user's own style and place survive (it used to be replaced by
        # the Anton 140 px pop preset at y=324)
        _eq((round(float(t.transform.y)), round(float(t.style.size)), t.start, t.end), (810, 96, 0.0, 3.0))
    return chk


def _canvas_ratio(w_over_h: float):
    def chk(e, ids):
        assert abs(e.canvas.w / e.canvas.h - w_over_h) < 0.02, (e.canvas.w, e.canvas.h)
        # a reframe only: no captions, hook, transitions or cuts
        assert not [t for t in texts(e)], [t.text for t in texts(e)]
        _eq(transitions(e), [])
        _eq([round(c.in_, 2) for c in v1(e)], [0.0, 4.0, 8.0])
    return chk


def _cue_looks(e) -> list[tuple[str, float]]:
    return [(c.style.color, c.style.size) for c in e.get_track("captions").clips if isinstance(c, TextClip)]


def _mute_state(e) -> tuple[bool, bool, bool]:
    vo = e.get_track("vo")
    return (all(c.audio.mute for c in v1(e)), bool(music(e).muted), bool(vo.muted) if vo else False)


CASES: list[Case] = [
    # ---- split particles ("turn X off", "take X out") ------------------------
    Case("turn the robot voice off", lambda e, ids: _eq(voices(e), [None, None, None]), pre=_pre_robot),
    Case("switch the robot voice off", NOOP, question="nothing to take off"),
    Case("turn the echo voice off on clip 2", lambda e, ids: _eq(voices(e), [None, None, "robot"]),
         pre=_pre_echo_b_robot_c),
    Case("take the transitions out", lambda e, ids: _eq(transitions(e), []), pre=_pre_transitions),
    Case("take the glitch transition out",
         lambda e, ids: _eq([(t.at, t.type) for t in transitions(e)], [(4.0, "fade")]), pre=_pre_glitch),
    Case("take the glitch transition out", NOOP, pre=_pre_transitions, question="no Glitch transition"),
    # ---- "remove the glitch effect" (release notes) / "remove the voiceover" -----
    Case("remove the glitch effect",
         lambda e, ids: _eq([(t.at, t.type) for t in transitions(e)], [(4.0, "fade")]), pre=_pre_glitch),
    Case("remove the glitch effect", lambda e, ids: _eq([[x.type for x in c.effects] for c in v1(e)], [[], [], []]),
         pre=_pre_rgb_c),
    Case("remove the glitch effect", NOOP, question="no Glitch transition or RGB Split"),
    Case("remove the voiceover", lambda e, ids: _eq(vo_clips(e), []), pre=_pre_vo),
    Case("delete the narration", lambda e, ids: _eq(vo_clips(e), []), pre=_pre_vo),
    Case("remove the voiceover", NOOP, question="no voice-over"),
    # ---- speed back to normal -----------------------------------------------
    Case("reset the speed of the first clip", lambda e, ids: _eq(_speeds(e), [1.0, 1.0, 1.0]), pre=_pre_speed_a),
    Case("make clip 1 normal speed again", lambda e, ids: _eq(_speeds(e), [1.0, 1.0, 1.0]), pre=_pre_speed_a),
    Case("make the first clip play at normal speed", lambda e, ids: _eq(_speeds(e), [1.0, 1.0, 1.0]),
         pre=_pre_speed_a),
    Case("remove the speed change on clip 1", lambda e, ids: _eq(_speeds(e), [1.0, 1.0, 1.0]), pre=_pre_speed_a),
    Case("reset the speed of the last clip",
         lambda e, ids: _eq((v1(e)[-1].speed_curve, v1(e)[-1].speed_factor), (None, 1.0)), pre=_pre_hero_c),
    Case("put clip 1 back to normal speed", NOOP, question="already plays at normal speed"),
    # ---- an explicit ratio is a reframe, never an auto edit -------------------
    Case("set it to 4:5 for instagram", _canvas_ratio(4 / 5)),
    Case("change the ratio to 9:16 for youtube shorts", _canvas_ratio(9 / 16)),
    # ---- a text's new wording -------------------------------------------------
    Case("change the Summer Trip text to Winter Trip", _retexted("Winter Trip"), pre=_pre_summer),
    Case("rename the title to Winter Trip", _retexted("Winter Trip"), pre=_pre_summer),
    Case("edit the text so it says Hello World", _retexted("Hello World"), pre=_pre_summer),
    Case("change the title to say Winter Trip", _retexted("Winter Trip"), pre=_pre_summer),
    Case("fix the typo in the title, it should say Summer Trips", _retexted("Summer Trips"), pre=_pre_summer),
    Case("replace 'Summer Trip' with 'Winter Trip'", _retexted("Winter Trip"), pre=_pre_summer),
    Case("rename the title to Winter Trip", ASKS, pre=_pre_summer_and_sale, question="Which text"),
    Case("delete the Summer Trip title", lambda e, ids: _eq([t.text for t in texts(e)], ["SALE"]),
         pre=_pre_summer_and_sale),
    # ---- lower thirds keep the name they were given -------------------------
    Case("add a lower third saying Jane Doe, Producer",
         lambda e, ids: _eq(sorted((t.role or "", t.text) for t in texts(e)),
                            [("", "Summer Trip"), ("lower_third", "Jane Doe\nProducer")]), pre=_pre_summer),
    Case("lower third: Jane Doe",
         lambda e, ids: _eq([t.text for t in texts(e) if t.role == "lower_third"], ["Jane Doe"]), pre=_pre_summer),
    Case("add a lower third: Jane Doe",
         lambda e, ids: _eq([t.text for t in texts(e) if t.role == "lower_third"], ["Jane Doe"])),
    # ---- the captions' LOOK (never a re-lay) --------------------------------
    Case("make the captions yellow",
         lambda e, ids: _eq({c for c, _ in _cue_looks(e)}, {"#FFD400"}), pre=_pre_captions),
    Case("change the caption font colour to yellow",
         lambda e, ids: _eq({c for c, _ in _cue_looks(e)}, {"#FFD400"}), pre=_pre_captions),
    Case("make the captions yellow", NOOP, question="no captions yet"),
    # ---- mute everything except … -------------------------------------------
    Case("mute everything except the voiceover", lambda e, ids: _eq(_mute_state(e), (True, True, False)),
         pre=_pre_vo),
    Case("mute everything but the voiceover", lambda e, ids: _eq(_mute_state(e), (True, True, False)),
         pre=_pre_vo),
    Case("mute everything except the music", lambda e, ids: _eq(_mute_state(e), (True, False, True)),
         pre=_pre_vo),
    # ---- an overlay's opacity / position: honest, never an off-topic question --
    Case("make the overlay 50% transparent", NOOP, pre=_pre_overlay, question="Opacity"),
    Case("move the overlay to the top right corner", NOOP, pre=_pre_overlay, question="Position"),
    # ---- a music bed longer than the video: correct edits verify ------------
    Case("delete clip 3", lambda e, ids: _eq([c.id for c in v1(e)], [ids["A"], ids["B"]]), pre=_pre_long_bed),
    Case("remove the part from 1s to 2.5s", lambda e, ids: _eq(round(e.video_extent(), 2), 10.5),
         pre=_pre_long_bed),
    Case("slow down the whole video to half speed", lambda e, ids: _eq(round(e.video_extent(), 2), 24.0),
         pre=_pre_long_bed),
    Case("duplicate the second clip", lambda e, ids: _eq(round(e.video_extent(), 2), 16.0), pre=_pre_long_bed),
    Case("delete this clip", lambda e, ids: _eq([c.id for c in v1(e)], [ids["A"], ids["C"]]), pre=_pre_long_bed),
]


@pytest.mark.usefixtures("no_downloads")
@pytest.mark.parametrize("case", CASES, ids=[f"{i:02d}-{c.phrase}" for i, c in enumerate(CASES)])
def test_final_qa_phrase_does_the_right_edit_or_says_why(media, case, request):
    st, ids = _session(media, f"s_fq{request.node.callspec.id[:2]}")
    ui = {k: (ids[v] if k == "selection" else v) for k, v in case.ui.items()}
    if case.pre:
        case.pre(st, ids)
    before = st.edl.hash()
    looks_before = _cue_looks(st.edl) if st.edl.get_track("captions") else []
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
    if looks_before and "caption" in case.phrase:
        # a look change keeps every cue (same count, same words)
        assert len(_cue_looks(e)) == len(looks_before)


def test_the_bigger_captions_are_a_quarter_bigger_and_keep_their_words(media, request):
    st, ids = _session(media, "s_fq_big")
    _pre_captions(st, ids)
    cues = [(c.text, c.start) for c in st.edl.get_track("captions").clips]
    size = st.edl.get_track("captions").clips[0].style.size
    _turn(st, "make the subtitles bigger", dict(UI, selection=ids["B"]))
    after = st.edl.get_track("captions").clips
    assert [(c.text, c.start) for c in after] == cues
    assert {round(c.style.size) for c in after} == {round(size * 1.25)}


def test_a_long_bed_does_not_change_the_stated_clip_length(media):
    st, ids = _session(media, "s_fq_len")
    _pre_long_bed(st, ids)
    events = _turn(st, "delete clip 3", dict(UI, selection=ids["B"]))
    said = _question_text(events)
    assert "(4.0s)" in said and "32.0s" not in said, said
