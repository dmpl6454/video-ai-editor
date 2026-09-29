"""Final sweep 2, round 2 (prompt assistant): Prompt-bar phrasings that
COMMITTED a wrong edit (and passed their own checks) on the key-free ladder.

Every phrase runs through the real service (`brain="recipes"`) on the
capcut-sweep session: three 4 s clips A/B/C on the main track, 1920x1080, a
12 s music bed at -14 dB, clip B selected, playhead 5.5 s. Before these fixes:

  * "lower the clip audio to -18 dB under the voiceover" lowered the
    VOICEOVER (the one sound the user wanted clear), and so did "turn the
    video sound down under my voiceover" / "make the clips quieter than the
    voiceover";
  * "clip 2 volume +2" took -6 dB to -12 dB, "bring the music down to -25"
    set -20 (a bare number without "dB" was ignored and a 6 dB step used);
  * "three quarter speed" was 0.25x, "increase clip 1 speed by 30%" 0.3x,
    "by a third" 0.8x / 1.25x;
  * "make the title last 2 seconds longer" SHORTENED it, "make the title say
    X" added a second title reading "say X";
  * "add a title 'Intro' and make it red" was white, "add a caption
    'Welcome' at 2 seconds" captioned the whole transcript;
  * "fade transitions everywhere, 1 sec" were 0.5 s, "make the second
    transition 1 second" changed both;
  * "remove the fade in" removed the fade OUT as well;
  * "zoom clip 1 to 2x" set the clip's SPEED to 2x;
  * "trim the start by 1s and the end by 1s" trimmed one end;
  * "replace the music with silence" added a new bed, "remove the last 3
    seconds of the music" cut the video, "remove the music ducking" turned
    ducking ON;
  * "make the first 4 seconds black and white" put the look on every clip,
    "the second half … in slow motion" slowed every clip;
  * "remove the gap" (no gap) removed silences, "remove clip 2 but keep the
    gap" closed the gap;
  * "cut the first 2 seconds and then speed up the rest by 1.5x" was right
    but reported "done with issues" (two false duration failures).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from prompt_fixtures import no_downloads  # noqa: E402,F401

from test_final_qa_prompt_phrases import _pre_summer, _pre_summer_and_sale  # noqa: E402
from test_prompt_capcut_sweep import (  # noqa: E402  (helpers + the module-scoped session fixture)
    ASKS, NOOP, Case, _eq, _question_text, _session, _turn, gains, ins, media, music, texts,  # noqa: F401
    transitions, v1,
)
from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.edl.snapshot import EDLStore  # noqa: E402

# --------------------------------------------------------------------------- setups / reads


def _pre_vo(clips_db: float = -12.0, music_db: float = -12.0):
    """A voiceover (0 dB) on the vo lane; the clips and the music at a level."""
    def pre(st: EDLStore, ids) -> None:
        bed = next(c for c in music(st.edl).clips).src
        dispatch(st, "add_clip", {"track": "vo", "src": str(bed), "in": 0, "out": 4.0, "start": 0})
        for k in ("A", "B", "C"):
            if clips_db:
                dispatch(st, "set_volume", {"target": ids[k], "db": clips_db})
        dispatch(st, "set_volume", {"target": "music", "db": music_db})
    return pre


def _vo(e) -> list[float]:
    t = e.get_track("vo")
    return [round(c.audio.gain_db, 2) for c in (t.clips if t else [])]


def _music_db(e) -> list[float]:
    return [round(c.audio.gain_db, 2) for c in music(e).clips]


def _pre_gain(key: str, db: float):
    def pre(st: EDLStore, ids) -> None:
        dispatch(st, "set_volume", {"target": ids[key], "db": db})
    return pre


def _speeds(e) -> list[float]:
    return [round(c.speed_factor, 3) for c in v1(e)]


def _tr(e) -> list[tuple[float, str, float]]:
    return sorted((round(t.at, 2), t.type, round(float(t.duration), 2)) for t in transitions(e))


def _pre_two_transitions(st: EDLStore, ids) -> None:
    dispatch(st, "add_transition", {"at": 4.0, "type": "dissolve", "duration": 0.5})
    dispatch(st, "add_transition", {"at": 8.0, "type": "fade", "duration": 0.5})


def _pre_fades(st: EDLStore, ids) -> None:
    dispatch(st, "set_video_fade", {"clip_id": ids["A"], "in_s": 1.0})
    dispatch(st, "set_video_fade", {"clip_id": ids["C"], "out_s": 1.0})


def _vfades(e) -> list[tuple[float, float]]:
    return [(round(c.video_fade_in, 2), round(c.video_fade_out, 2)) for c in v1(e)]


def _scale(c) -> float:
    v = c.transform.scale
    return round(float(v.keyframes[-1][1]) if hasattr(v, "keyframes") else float(v), 3)


def _cov(e) -> list[tuple[float, float]]:
    spans = sorted((round(c.in_, 2), round(c.out, 2)) for c in v1(e))
    out: list[list[float]] = []
    for a, b in spans:
        if out and a <= out[-1][1] + 1e-3:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _looks(e) -> list[list[str]]:
    return [[Path(str(x.params.get("src", ""))).name if x.type == "lut" else x.type for x in c.effects] for c in v1(e)]


def _pre_duck(st: EDLStore, ids) -> None:
    dispatch(st, "set_duck", {"track": "music", "enabled": True, "to_db": -18.0})


def _texts(e) -> list[tuple[str, float, float]]:
    return [(t.text, round(t.start, 2), round(t.end, 2)) for t in texts(e)]


def _pre_speed_b2(st: EDLStore, ids) -> None:
    dispatch(st, "set_speed", {"clip_id": ids["B"], "factor": 2.0})


def _pre_mono_b(st: EDLStore, ids) -> None:
    dispatch(st, "apply_lut", {"clip_id": ids["B"], "src": "mono.cube"})


def _pre_fade_c(st: EDLStore, ids) -> None:
    dispatch(st, "set_video_fade", {"clip_id": ids["C"], "out_s": 1.0})


def _pre_day_one(st: EDLStore, ids) -> None:
    dispatch(st, "add_text", {"text": "Day One", "start": 0.0, "end": 3.0})


def _pre_overlay(st: EDLStore, ids) -> None:
    src = next(c for c in v1(st.edl)).src
    dispatch(st, "add_clip", {"track": "v2", "src": str(src), "in": 0, "out": 2.0, "start": 1.0})


def _rot(c) -> float:
    v = c.transform.rotation
    return round(float(v.keyframes[-1][1]) if hasattr(v, "keyframes") else float(v), 2)


def _approx_all(got: list[float], want: list[float], tol: float = 0.01) -> None:
    assert len(got) == len(want) and all(abs(a - b) <= tol for a, b in zip(got, want)), f"{got} != {want}"


def _untouched_levels(e, ids, clips: float, mus: float, vo: float) -> None:
    _eq(gains(e), [clips] * 3)
    _eq(_music_db(e), [mus])
    _eq(_vo(e), [vo])


# --------------------------------------------------------------------------- the phrases

CASES: list[Case] = [
    # ---- a voiceover named only as the reference is never the target ------
    Case("lower the clip audio to -18 dB under the voiceover",
         lambda e, ids: (_eq(gains(e), [-18.0] * 3), _eq(_vo(e), [0.0]), _eq(_music_db(e), [-12.0])), pre=_pre_vo()),
    Case("turn the video sound down under my voiceover",
         lambda e, ids: (_eq(gains(e), [-18.0] * 3), _eq(_vo(e), [0.0]), _eq(_music_db(e), [-12.0])), pre=_pre_vo()),
    Case("make the clips quieter than the voiceover",
         lambda e, ids: (_eq(gains(e), [-18.0] * 3), _eq(_vo(e), [0.0]), _eq(_music_db(e), [-12.0])), pre=_pre_vo()),
    Case("put the music under the voiceover at -20 dB",
         lambda e, ids: (_eq(_music_db(e), [-20.0]), _eq(_vo(e), [0.0]), _eq(gains(e), [-12.0] * 3)), pre=_pre_vo()),
    Case("duck the video sound under the voiceover", NOOP, pre=_pre_vo(), question="music"),
    Case("turn the original video sound down to -12 dB so my voiceover is clear",
         lambda e, ids: (_eq(gains(e), [-12.0] * 3), _eq(_vo(e), [0.0]), _eq(_music_db(e), [-14.0])),
         pre=_pre_vo(clips_db=0.0, music_db=-14.0)),
    Case("lower the voiceover by 6 db",                                     # control: the vo IS the target
         lambda e, ids: (_eq(_vo(e), [-6.0]), _eq(gains(e), [-12.0] * 3)), pre=_pre_vo()),
    # ---- a level number without "dB" --------------------------------------
    Case("clip 2 volume +2", lambda e, ids: _eq(gains(e), [0.0, -4.0, 0.0]), pre=_pre_gain("B", -6.0)),
    Case("clip 2 volume +2 ", lambda e, ids: _eq(gains(e), [0.0, 2.0, 0.0])),
    Case("music volume +2", lambda e, ids: _eq(_music_db(e), [-12.0])),
    Case("clip 2 volume -2", lambda e, ids: _eq(gains(e), [0.0, -2.0, 0.0])),
    Case("clip 2 volume -2 ", lambda e, ids: _eq(gains(e), [0.0, -2.0, 0.0]), pre=_pre_gain("B", -6.0)),
    Case("bring the music down to -25", lambda e, ids: _eq(_music_db(e), [-25.0])),
    Case("set the music at -18", lambda e, ids: _eq(_music_db(e), [-18.0])),
    # ---- speed fractions and "increase … speed by N%" ----------------------
    Case("clip 1 at three quarter speed", lambda e, ids: _eq(_speeds(e), [0.75, 1.0, 1.0])),
    Case("increase clip 1 speed by 30%", lambda e, ids: _eq(_speeds(e), [1.3, 1.0, 1.0])),
    Case("slow clip 1 down by a third", lambda e, ids: _approx_all(_speeds(e), [0.667, 1.0, 1.0])),
    Case("make clip 2 a third faster", lambda e, ids: _approx_all(_speeds(e), [1.0, 1.333, 1.0])),
    Case("slow the whole video down by a third", lambda e, ids: _approx_all(_speeds(e), [0.667] * 3)),
    Case("decrease clip 3 speed by 40%", lambda e, ids: _eq(_speeds(e), [1.0, 1.0, 0.6])),
    Case("make clip 2 two thirds speed", lambda e, ids: _approx_all(_speeds(e), [1.0, 0.667, 1.0])),
    # ---- retiming / retexting the existing title --------------------------
    Case("make the title last 2 seconds longer", lambda e, ids: _eq(_texts(e), [("Summer Trip", 0.0, 5.0)]),
         pre=_pre_summer),
    Case("title 1 second shorter", lambda e, ids: _eq(_texts(e), [("Summer Trip", 0.0, 2.0)]), pre=_pre_summer),
    Case("make the title say Road Trip 2026", lambda e, ids: _eq([t.text for t in texts(e)], ["Road Trip 2026"]),
         pre=_pre_summer),
    # ---- adding text keeps the whole request -------------------------------
    Case("add a title 'Intro' and make it red",
         lambda e, ids: _eq([(t.text, t.style.color.upper()) for t in texts(e)], [("Intro", "#FF3B30")])),
    Case("add a caption 'Welcome' at 2 seconds",
         lambda e, ids: (_eq([(t.text, round(t.start, 2)) for t in texts(e)], [("Welcome", 2.0)]),
                         _eq(bool(e.get_track("captions") and e.get_track("captions").clips), False))),
    # ---- transition length and scope ---------------------------------------
    Case("fade transitions everywhere, 1 sec", lambda e, ids: _eq(_tr(e), [(4.0, "fade", 1.0), (8.0, "fade", 1.0)])),
    Case("make the second transition 1 second",
         lambda e, ids: _eq(_tr(e), [(4.0, "dissolve", 0.5), (8.0, "fade", 1.0)]), pre=_pre_two_transitions),
    Case("change the first transition to a wipe",
         lambda e, ids: (_eq([x[1:] for x in _tr(e)][1], ("fade", 0.5)), _eq(_tr(e)[0][2], 0.5),
                         _eq("wipe" in _tr(e)[0][1], True)), pre=_pre_two_transitions),
    # ---- a fade removal names its side ------------------------------------
    Case("remove the fade in", lambda e, ids: _eq(_vfades(e), [(0.0, 0.0), (0.0, 0.0), (0.0, 1.0)]), pre=_pre_fades),
    Case("remove the fade out", lambda e, ids: _eq(_vfades(e), [(1.0, 0.0), (0.0, 0.0), (0.0, 0.0)]), pre=_pre_fades),
    # ---- zoom is not speed --------------------------------------------------
    Case("zoom clip 1 to 2x", lambda e, ids: (_eq(_speeds(e), [1.0] * 3), _eq([_scale(c) for c in v1(e)], [2.0, 1.0, 1.0]))),
    Case("scale clip 3 to 1.5x",
         lambda e, ids: (_eq(_speeds(e), [1.0] * 3), _eq([_scale(c) for c in v1(e)], [1.0, 1.0, 1.5]))),
    # ---- both ends in one sentence -----------------------------------------
    Case("trim the start by 1s and the end by 1s", lambda e, ids: _eq(_cov(e), [(1.0, 11.0)])),
    Case("remove the first and last second", lambda e, ids: _eq(_cov(e), [(1.0, 11.0)])),
    # ---- music requests edit the music -------------------------------------
    Case("replace the music with silence", lambda e, ids: (_eq(len(music(e).clips), 0), _eq(_cov(e), [(0.0, 12.0)]))),
    Case("remove the last 3 seconds of the music",
         lambda e, ids: (_eq(_cov(e), [(0.0, 12.0)]), _eq([round(c.start + c.effective_duration, 2) for c in music(e).clips], [9.0]))),
    Case("remove the music ducking", NOOP, question="duck"),
    Case("remove the music ducking ", lambda e, ids: _eq(music(e).duck, None), pre=_pre_duck),
    # ---- a time range scopes a look / speed --------------------------------
    Case("make the first 4 seconds black and white", lambda e, ids: _eq(_looks(e), [["mono.cube"], [], []])),
    Case("first 6 seconds black and white", ASKS),
    Case("i want the second half of the video in slow motion", ASKS),
    Case("make the last 8 seconds slow motion", lambda e, ids: _eq(_speeds(e), [1.0, 0.5, 0.5])),
    # ---- gaps are gaps -----------------------------------------------------
    Case("remove the gap", NOOP, question="no gap"),
    Case("remove clip 2 but keep the gap", NOOP, question="gap"),
    # ---- a trim then a speed verifies against the timeline after the trim --
    Case("cut the first 2 seconds and then speed up the rest by 1.5x",
         lambda e, ids: _approx_all([round(e.video_extent(), 2)], [6.67])),
    # ---- MEDIUM: clear requests that rolled back or dead-ended -------------
    Case("a bit slower", lambda e, ids: _eq(_speeds(e), [0.8, 1.6, 0.8]), pre=_pre_speed_b2),
    Case("lower all clips by 2db but leave the music",
         lambda e, ids: (_eq(gains(e), [-2.0] * 3), _eq(_music_db(e), [-14.0]))),
    Case("mute everything except clip 1",
         lambda e, ids: (_eq([c.audio.mute for c in v1(e)], [False, True, True]), _eq(music(e).muted, True))),
    Case("slow everything down 10 percent", lambda e, ids: _eq(_speeds(e), [0.9] * 3)),
    Case("lower clip 2 a lot", lambda e, ids: (_eq(_speeds(e), [1.0] * 3), _eq(gains(e), [0.0, -12.0, 0.0]))),
    Case("make clip 2 two thirds speed", lambda e, ids: _approx_all(_speeds(e), [1.0, 0.667, 1.0])),
    Case("rotate clip 2 90 degrees counterclockwise", lambda e, ids: _eq([_rot(c) for c in v1(e)], [0.0, -90.0, 0.0])),
    Case("start the title at 0:02", lambda e, ids: _eq(_texts(e), [("Summer Trip", 2.0, 5.0)]), pre=_pre_summer),
    Case("title size up by 20", lambda e, ids: (_eq([t.text for t in texts(e)], ["Summer Trip"]),
                                                _eq(texts(e)[0].style.size > 96, True)), pre=_pre_summer),
    Case("text 50% bigger", lambda e, ids: (_eq([t.text for t in texts(e)], ["Summer Trip"]),
                                            _eq(texts(e)[0].style.size, 144.0)), pre=_pre_summer),
    Case("title font 120", lambda e, ids: _eq([(t.text, t.style.size) for t in texts(e)], [("Summer Trip", 120.0)]),
         pre=_pre_summer),
    Case("move the second clip after the third", lambda e, ids: _eq(ins(e), [0.0, 8.0, 4.0])),
    Case("swap clip 1 and clip 3", lambda e, ids: _eq(ins(e), [8.0, 4.0, 0.0])),
    Case("mute clip 1, make clip 2 b&w, and fade out clip 3",
         lambda e, ids: (_eq([c.audio.mute for c in v1(e)], [True, False, False]),
                         _eq(_looks(e), [[], ["mono.cube"], []]), _eq(_vfades(e)[2][1] > 0, True))),
    Case("mute the voice over", lambda e, ids: (_eq(e.get_track("vo").muted, True),
                                                _eq([c.audio.mute for c in v1(e)], [False] * 3)), pre=_pre_vo()),
    Case("set clip 2 volume to 0%", lambda e, ids: _eq(gains(e)[1] <= -40.0, True)),
    Case("shorten clip 2 by a second", lambda e, ids: _eq([round(c.start, 2) for c in v1(e)], [0.0, 4.0, 7.0])),
    Case("make the video 9 seconds long", ASKS, question="9s"),
    Case("make clip 2 twice as long", lambda e, ids: _eq(_speeds(e), [1.0, 0.5, 1.0])),
    Case("make clip 2 take twice as long", lambda e, ids: _eq(_speeds(e), [1.0, 0.5, 1.0])),
    Case("fad out the end", lambda e, ids: _eq(_vfades(e)[2][1] > 0, True)),
    Case("saturate clip 1 more", lambda e, ids: _eq([bool(c.effects) for c in v1(e)], [True, False, False])),
    Case("make Day One purple", lambda e, ids: _eq([(t.text, t.style.color.upper()) for t in texts(e)],
                                                   [("Day One", "#AF52DE")]), pre=_pre_day_one),
    Case("SALE should show until the end",
         lambda e, ids: _eq(_texts(e), [("Summer Trip", 0.0, 3.0), ("SALE", 5.0, 12.0)]), pre=_pre_summer_and_sale),
    Case("reset clip 2", ASKS, question="speed"),
    Case("delete the overlay", lambda e, ids: _eq(bool(e.get_track("v2") and e.get_track("v2").clips), False),
         pre=_pre_overlay),
    Case("remove every other clip", lambda e, ids: _eq(ins(e), [0.0, 8.0])),
    Case("cut the last half second of clip 2", lambda e, ids: _eq(_cov(e), [(0.0, 7.5), (8.0, 12.0)])),
    Case("remove the black and white from clip 2", lambda e, ids: _eq(_looks(e), [[], [], []]), pre=_pre_mono_b),
    Case("cut the music at 8 seconds",
         lambda e, ids: (_eq(_cov(e), [(0.0, 12.0)]),
                         _eq([round(c.start + c.effective_duration, 2) for c in music(e).clips], [8.0]))),
    Case("trim the music to 6 seconds",
         lambda e, ids: _eq([round(c.start + c.effective_duration, 2) for c in music(e).clips], [6.0])),
    Case("make the music twice as loud", lambda e, ids: _eq(_music_db(e), [-8.0])),
    Case("loop the music", NOOP, question="nothing to loop"),
    Case("start the music at 3 seconds", lambda e, ids: _eq([round(c.start, 2) for c in music(e).clips], [3.0])),
    Case("music only in the first half",
         lambda e, ids: _eq([round(c.start + c.effective_duration, 2) for c in music(e).clips], [6.0])),
    Case("make the fade out longer", NOOP, pre=_pre_fade_c, question="How long"),
]


@pytest.mark.usefixtures("no_downloads")
@pytest.mark.parametrize("case", CASES, ids=[f"{i:03d}-{c.phrase}" for i, c in enumerate(CASES)])
def test_s2r2_phrase_does_the_right_edit_or_asks(media, case, request):
    st, ids = _session(media, f"s_s2r2_{request.node.callspec.id.split('-')[0]}")
    ui = {"selection": ids["B"], "playhead": 5.5}
    if case.pre:
        case.pre(st, ids)
    before = st.edl.hash()
    events = _turn(st, case.phrase.strip(), ui)
    e = st.edl
    errors = [x for x in events if x["type"] == "error"]
    assert not errors, errors
    said = _question_text(events)
    if case.expect == ASKS:
        assert e.hash() == before, f"the timeline changed on a phrase that must ask: {said}"
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


# --------------------------------------------------------------------------- the net, on its own
#
# The planner reads each phrase right now, so the net rarely sees the wrong
# edit. These feed the contract the WRONG edit each phrase used to commit and
# require it to be refused (the contract runs here without the executor's
# never-raise guard, so a broken rule fails the test instead of passing).

def _d(tool: str, args):
    return lambda st, ids: dispatch(st, tool, {k: (ids[v] if isinstance(v, str) and v in ids else v)
                                              for k, v in args.items()})


def _each(*calls):
    def go(st, ids):
        for c in calls:
            c(st, ids)
    return go


def _all3(tool: str, args):
    return _each(*[_d(tool, {**args, "clip_id": k}) for k in ("A", "B", "C")])


def _wrong_music_swap(st: EDLStore, ids) -> None:
    bed = music(st.edl).clips[0]
    src = str(bed.src)
    dispatch(st, "bulk_delete", {"clip_ids": [bed.id]})
    dispatch(st, "add_music", {"src": src, "start": 0.0, "volume_db": -14.0, "duck": True})


def _wrong_title_at(text: str, start: float):
    return _d("add_text", {"text": text, "start": start, "end": start + 3.0})


NET_CASES = [
    ("lower the clip audio to -18 dB under the voiceover", _pre_vo(), _d("set_volume", {"target": "vo", "db": -18.0}), {}),
    ("turn the video sound down under my voiceover", _pre_vo(), _d("set_volume", {"target": "vo", "db": -6.0}), {}),
    ("make the clips quieter than the voiceover", _pre_vo(), _d("set_volume", {"target": "vo", "db": -6.0}), {}),
    ("clip 2 volume +2", _pre_gain("B", -6.0), _d("set_volume", {"target": "B", "db": -12.0}), {}),
    ("music volume +2", None, _d("set_volume", {"target": "music", "db": -20.0}), {}),
    ("clip 2 volume -2", None, _d("set_volume", {"target": "B", "db": -6.0}), {}),
    ("bring the music down to -25", None, _d("set_volume", {"target": "music", "db": -20.0}), {}),
    ("clip 1 at three quarter speed", None, _d("set_speed", {"clip_id": "A", "factor": 0.25}), {}),
    ("increase clip 1 speed by 30%", None, _d("set_speed", {"clip_id": "A", "factor": 0.3}), {}),
    ("slow clip 1 down by a third", None, _d("set_speed", {"clip_id": "A", "factor": 0.8}), {}),
    ("make clip 2 a third faster", None, _d("set_speed", {"clip_id": "B", "factor": 1.25}), {}),
    ("decrease clip 3 speed by 40%", None, _d("set_speed", {"clip_id": "C", "factor": 0.4}), {}),
    ("make the title last 2 seconds longer", _pre_summer,
     lambda st, ids: dispatch(st, "set_clip_timing", {"clip_id": texts(st.edl)[0].id, "start": 0.0, "end": 2.0}), {}),
    ("make the title say Road Trip 2026", _pre_summer, _wrong_title_at("say Road Trip 2026", 0.0), {}),
    ("start the title at 0:02", _pre_summer, _wrong_title_at("at 0:02", 2.0), {}),
    ("add a title 'Intro' and make it red", None, _d("add_text", {"text": "Intro", "start": 0.0, "end": 3.0,
                                                                 "color": "#FFFFFF"}), {}),
    ("add a caption 'Welcome' at 2 seconds", None, _d("add_caption_track", {"style": "default", "position": "bottom"}),
     {}),
    ("fade transitions everywhere, 1 sec", None,
     _each(_d("add_transition", {"at": 4.0, "type": "fade", "duration": 0.5}),
           _d("add_transition", {"at": 8.0, "type": "fade", "duration": 0.5})), {}),
    ("make the second transition 1 second", _pre_two_transitions,
     _each(_d("add_transition", {"at": 4.0, "type": "dissolve", "duration": 1.0}),
           _d("add_transition", {"at": 8.0, "type": "fade", "duration": 1.0})), {}),
    ("change the first transition to a wipe", _pre_two_transitions,
     _d("add_transition", {"at": 4.0, "type": "wiperight", "duration": 0.4}), {}),
    ("remove the fade in", _pre_fades,
     _each(_all3("set_video_fade", {"in_s": 0.0, "out_s": 0.0}), _all3("add_fade", {"in_s": 0.0, "out_s": 0.0})), {}),
    ("zoom clip 1 to 2x", None, _d("set_speed", {"clip_id": "A", "factor": 2.0}), {}),
    ("trim the start by 1s and the end by 1s", None, _d("cut_range", {"track": "v1", "start": 0.0, "end": 1.0}), {}),
    ("remove the first and last second", None, _d("cut_range", {"track": "v1", "start": 11.0, "end": 12.0}), {}),
    ("replace the music with silence", None, _wrong_music_swap, {}),
    ("remove the last 3 seconds of the music", None, _d("cut_range", {"track": "v1", "start": 9.0, "end": 12.0}), {}),
    ("remove the music ducking", None, _d("set_duck", {"track": "music", "enabled": True, "to_db": -18.0}), {}),
    ("make the first 4 seconds black and white", None, _all3("apply_lut", {"src": "mono.cube"}), {}),
    ("remove the gap", None, _d("cut_range", {"track": "v1", "start": 5.0, "end": 6.0}), {}),
    ("remove clip 2 but keep the gap", None, _d("ripple_delete", {"clip_id": "B"}), {}),
]


@pytest.mark.parametrize("phrase,pre,wrong,ui", NET_CASES, ids=[c[0] for c in NET_CASES])
def test_the_contract_refuses_the_wrong_edit_each_phrase_used_to_commit(media, phrase, pre, wrong, ui):
    from video_ai_editor.agent.prompt.contract import Contract
    st, ids = _session(media, f"s_s2r2net{[c[0] for c in NET_CASES].index(phrase):02d}")
    if pre:
        pre(st, ids)
    before = st.edl.model_copy(deep=True)
    wrong(st, ids)
    found = Contract.read(phrase, selection=ids["B"], playhead=5.5).judge(before, st.edl)
    assert found, f"the net let {phrase!r} commit the wrong edit"


RIGHT_CASES = [
    ("rotate clip 2 90 degrees counterclockwise", None, _d("set_clip_transform", {"clip_id": "B", "rotation": -90.0})),
    ("lower the voiceover by 6 db", _pre_vo(), _d("set_volume", {"target": "vo", "db": -6.0})),
    ("make the second transition 1 second", _pre_two_transitions,
     _d("add_transition", {"at": 8.0, "type": "fade", "duration": 1.0})),
    ("remove the fade in", _pre_fades, _each(_d("set_video_fade", {"clip_id": "A", "in_s": 0.0}),
                                            _d("add_fade", {"clip_id": "A", "in_s": 0.0}))),
]


@pytest.mark.parametrize("phrase,pre,right", RIGHT_CASES, ids=[c[0] for c in RIGHT_CASES])
def test_the_contract_accepts_the_right_edit(media, phrase, pre, right):
    """The fixes to the net must not refuse the edit the words DO ask for
    ("90 degrees counterclockwise" was refused as "did not rotate 90°")."""
    from video_ai_editor.agent.prompt.contract import Contract
    st, ids = _session(media, f"s_s2r2ok{[c[0] for c in RIGHT_CASES].index(phrase):02d}")
    if pre:
        pre(st, ids)
    before = st.edl.model_copy(deep=True)
    right(st, ids)
    found = [v.as_dict() for v in Contract.read(phrase, selection=ids["B"], playhead=5.5).judge(before, st.edl)]
    assert not found, found


# --------------------------------------------------------------------------- the ladder

def test_a_voiceover_named_as_the_reference_never_takes_the_on_device_answer():
    """"turn the original video sound down to -12 dB so my voiceover is clear"
    came back from Apple Intelligence as a NEW voice-over with a 60 MB voice
    download: naming the voiceover that is there is not asking for one."""
    from video_ai_editor.agent.prompt.brains import router
    from video_ai_editor.agent.prompt.recipes import ask
    from video_ai_editor.agent.prompt.schema import Plan
    vo = Plan.new(intent="voiceover", brain="apple_intelligence", confidence=0.9,
                  needs_input=[ask("text", "What should the voiceover say?", kind="text")])
    phrase = "turn the original video sound down to -12 dB so my voiceover is clear"
    assert router.off_topic(vo, phrase)
    assert router.off_topic(vo, "lower the clip audio under the voiceover")
    assert router.off_topic(vo, "make the voiceover louder")
    # asking for one is still on topic
    assert router.off_topic(vo, "add a voiceover saying welcome to the channel") is None
    assert router.off_topic(vo, "narrate this") is None


def test_the_ladder_falls_through_the_off_topic_voiceover(monkeypatch):
    from test_prompt_brains_router import Fake, req, run
    from video_ai_editor.agent.prompt.recipes import ask
    from video_ai_editor.agent.prompt.schema import Plan
    vo = Plan.new(intent="voiceover", brain="apple_intelligence", confidence=0.9,
                  needs_input=[ask("text", "What should the voiceover say?", kind="text")])
    fm = Fake("apple_intelligence", result=vo)
    out, _events = run({"recipes": Fake("recipes", result="rejected:low confidence 0.50"), "apple_intelligence": fm},
                       request=req("turn the original video sound down to -12 dB so my voiceover is clear"))
    assert fm.plans == 1
    assert not (out.answered and out.brain == "apple_intelligence"), out
    assert any(a.brain == "apple_intelligence" and a.status == "failed" and "off-topic" in (a.reason or "")
               for a in out.attempts), out.attempts


@pytest.mark.parametrize("phrase,check", [
    ("add a title 'Intro' and make it red", lambda st: st.args.get("color") == "#FF3B30" and st.args["text"] == "Intro"),
    ("add a caption 'Welcome' at 2 seconds", lambda st: st.args["text"] == "Welcome" and st.args["start"] == 2.0),
])
def test_the_recipes_answer_text_adds_before_the_on_device_rung(phrase, check):
    """Both went to Apple Intelligence (recipes rejected at 0.50) and came
    back white / as transcript captions: the recipes now read the whole line
    confidently, so the ladder never reaches the model."""
    from test_prompt_brains_router import Fake, run
    from video_ai_editor.agent.prompt import recipes as R
    from video_ai_editor.agent.prompt.brains.base import BrainRequest
    from video_ai_editor.agent.prompt.brains.recipes_brain import RecipesBrain
    from video_ai_editor.agent.prompt.facts import TimelineFacts
    from video_ai_editor.agent.prompt.schema import Plan, Step
    white = Plan.new(intent="title", brain="apple_intelligence", confidence=0.9,
                     steps=[Step(tool="add_text", args={"text": "Intro\nIntro", "start": 0, "end": 3}, why="t")])
    fm = Fake("apple_intelligence", result=white)
    request = BrainRequest(prompt=phrase, facts=TimelineFacts.minimal(duration=12.0), recipes=R.cards())
    out, _events = run({"recipes": RecipesBrain(), "apple_intelligence": fm}, request=request)
    assert out.brain == "recipes" and fm.plans == 0, out
    adds = [s for s in out.plan.steps if s.tool == "add_text"]
    assert len(adds) == 1 and check(adds[0]), out.plan.steps
    assert not any(s.tool == "add_caption_track" for s in out.plan.steps)


# --------------------------------------------------------------------------- the rollback card

@pytest.mark.parametrize("phrase,avoid,never", [
    ("a bit slower", {"speed"}, {"trim", "title"}),
    ("lower all clips by 2db but leave the music", set(), {"trim", "speed", "title"}),
    ("make the title twice as big", {"title"}, {"trim", "speed"}),
])
def test_the_rollback_card_offers_only_edits_the_words_name(phrase, avoid, never):
    from video_ai_editor.agent.prompt.planner import safe_options
    got = [v for v, _l in safe_options(phrase, None, avoid)]
    assert not set(got) & never, got
    assert not set(got) & avoid, got


def test_a_prompt_that_names_no_edit_still_gets_the_generic_menu():
    from video_ai_editor.agent.prompt.planner import safe_options
    assert [v for v, _l in safe_options("banana zebra please")] == ["trim", "speed", "title"]


# --------------------------------------------------------------------------- one duration expectation

def test_several_duration_changes_are_one_expectation():
    """"cut the first 2 seconds and then speed up the rest by 1.5x": each
    step's duration check was measured from the ORIGINAL 12 s."""
    from video_ai_editor.agent.prompt.facts import TimelineFacts
    from video_ai_editor.agent.prompt.planner import _compose_durations
    from video_ai_editor.agent.prompt.recipes import pc
    facts = TimelineFacts.minimal(duration=12.0)
    pcs = [pc("duration_between", "cut", start=0.0, end=2.0, tol=0.1),
           pc("speed_equals", "sp", clip_id="$v1_all", factor=1.5),
           pc("duration_between", "sp", factor=1.5, tol_ratio=0.05)]
    out = _compose_durations(pcs, facts)
    durs = [p for p in out if p.check == "duration_between"]
    assert len(durs) == 1 and abs(durs[0].args["target"] - 6.667) < 0.01, durs
    assert [p.check for p in out].count("speed_equals") == 1
    # one duration change is left as it was
    assert _compose_durations(pcs[:2], facts) == pcs[:2]
