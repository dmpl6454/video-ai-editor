"""Final sweep 2 (prompt assistant): Prompt-bar phrasings that COMMITTED a
wrong edit (or lost half of one) on the key-free ladder.

Every phrase runs through the real service (`brain="recipes"`, the no-key
path) on the capcut-sweep session: three 4 s clips A/B/C on the main track,
a 1920x1080 canvas, a 12 s music bed at -14 dB, clip B selected, playhead
5.5 s. Before these fixes:

  * answering the rollback card with the rolled-back plan's own intent
    replayed the same misread ("make the title twice as big" → a new title
    reading "twice as big"), and the pick licensed it;
  * "remove 15 seconds from the end" on a 12 s video deleted every clip;
  * "the clip under the playhead" was every clip;
  * "take the black and white off" put black and white on every clip, and
    "take the fade off the last clip" ADDED fades;
  * "-45 degrees" rotated +45;
  * "make all transitions 1 second long" replaced them with 0.4 s dissolves;
  * "music fade in 1s and fade out 3s" faded the last clip's picture;
  * "cut the first 2 seconds of clip 3" cut the video's first 2 seconds;
  * "delete clips 1-2" deleted clip 1 only;
  * "turn it up 3db" was dropped (the glued unit hid the level clause);
  * "turn down all the clips except the music" lowered only the music;
  * "clip 2 volume back to normal" went further down;
  * "slow clip 1 down 25%" set 0.25x;
  * "put clip 1 between clip 2 and clip 3" added a transition;
  * "… and move it to the top" was dropped;
  * "duplicate … twice" made one copy; "for half a second" held 3 s;
  * "remove the music and the captions" re-laid the captions;
  * "remove the middle of clip 2" deleted all of clip 2;
  * "a bit slower" from 2x went to 0.8x; "music +3db" set +3 dB.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import no_downloads  # noqa: E402,F401

from test_final_qa_prompt_phrases import _pre_summer, _pre_summer_and_sale  # noqa: E402
from test_prompt_capcut_sweep import (  # noqa: E402  (helpers + the module-scoped session fixture)
    ASKS, NO_SEL, UI, Case, _eq, _question_text, _session, _turn, gains, ins, looks, media, music, texts,  # noqa: F401
    transitions, v1,
)
from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.agent.prompt import service  # noqa: E402
from video_ai_editor.edl.snapshot import EDLStore  # noqa: E402

# --------------------------------------------------------------------------- setups / reads


def _pre_mono_b(st: EDLStore, ids) -> None:
    dispatch(st, "apply_lut", {"clip_id": ids["B"], "src": "mono.cube"})


def _pre_fade_c(st: EDLStore, ids) -> None:
    dispatch(st, "set_video_fade", {"clip_id": ids["C"], "out_s": 1.0})


def _pre_two_transitions(st: EDLStore, ids) -> None:
    dispatch(st, "add_transition", {"at": 4.0, "type": "dissolve", "duration": 0.5})
    dispatch(st, "add_transition", {"at": 8.0, "type": "fade", "duration": 0.5})


def _pre_gain(key: str, db: float):
    def pre(st: EDLStore, ids) -> None:
        dispatch(st, "set_volume", {"target": ids[key], "db": db})
    return pre


def _pre_speed_b2(st: EDLStore, ids) -> None:
    dispatch(st, "set_speed", {"clip_id": ids["B"], "factor": 2.0})


def _pre_mute_b(st: EDLStore, ids) -> None:
    dispatch(st, "set_clip_muted", {"clip_id": ids["B"], "muted": True})


def _pre_zoom_b(st: EDLStore, ids) -> None:
    dispatch(st, "set_clip_transform", {"clip_id": ids["B"], "scale": 1.5})


def _pre_summer_red(st: EDLStore, ids) -> None:
    dispatch(st, "add_text", {"text": "Summer Trip", "start": 0.0, "end": 3.0, "y": 810, "size": 96,
                              "color": "#FF0000"})


def _scale(c) -> float:
    v = c.transform.scale
    return round(float(v.keyframes[-1][1]) if hasattr(v, "keyframes") else float(v), 3)


def _pre_captions(st: EDLStore, ids) -> None:
    dispatch(st, "add_caption_track", {"style": "default", "position": "bottom"})


def _speeds(e) -> list[float]:
    return [round(c.speed_factor, 3) for c in v1(e)]


def _rot(c) -> float:
    v = c.transform.rotation
    return round(float(v.keyframes[-1][1]) if hasattr(v, "keyframes") else float(v), 2)


def _tr(e) -> list[tuple[float, str, float]]:
    return sorted((round(t.at, 2), t.type, round(float(t.duration), 2)) for t in transitions(e))


def _fades(c) -> tuple[float, float, float, float]:
    return (c.video_fade_in, c.video_fade_out, c.audio.fade_in, c.audio.fade_out)


def _no_v1_fades(e) -> None:
    _eq([_fades(c) for c in v1(e)], [(0.0, 0.0, 0.0, 0.0)] * 3)


def _music_state(e) -> list[tuple[float, float, float]]:
    return [(round(c.audio.gain_db, 2), round(c.audio.fade_in, 2), round(c.audio.fade_out, 2))
            for c in music(e).clips]


def _cov(e) -> list[tuple[float, float]]:
    spans = sorted((round(c.in_, 2), round(c.out, 2)) for c in v1(e))
    out: list[list[float]] = []
    for a, b in spans:
        if out and a <= out[-1][1] + 1e-3:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _only(key: str, pred):
    """Only clip `key` changed, and it passes `pred`."""
    def chk(e, ids):
        by = {c.id: c for c in v1(e)}
        for k in ("A", "B", "C"):
            c = by[ids[k]]
            if k == key:
                assert pred(c), c.model_dump(exclude_defaults=True)
            else:
                assert (c.speed_factor == 1.0 and not c.audio.mute and not c.effects and _rot(c) == 0.0
                        and c.audio.gain_db == 0.0), (k, c.model_dump(exclude_defaults=True))
    return chk


def _summer(pred):
    def chk(e, ids):
        ts = texts(e)
        _eq([t.text for t in ts], ["Summer Trip"])
        assert pred(ts[0]), ts[0].model_dump(exclude_defaults=True)
    return chk


# --------------------------------------------------------------------------- the phrases

CASES: list[Case] = [
    # ---- a first/last N longer than the video asks ------------------------
    Case("remove 15 seconds from the end", ASKS, question="only 12"),
    Case("remove 20 seconds from the end", ASKS, question="only 12"),
    Case("trim 13 seconds off the end", ASKS, question="only 12"),
    # ---- the clip under the playhead --------------------------------------
    Case("slow down the clip under the playhead", _only("B", lambda c: c.speed_factor == 0.8), ui=NO_SEL),
    Case("mute the clip under the playhead", _only("B", lambda c: c.audio.mute), ui=NO_SEL),
    Case("make the clip under the cursor black and white",
         _only("B", lambda c: [Path(str(x.params.get("src"))).name for x in c.effects] == ["mono.cube"]), ui=NO_SEL),
    # ---- "take / turn the X off" removes X --------------------------------
    Case("take the black and white off", lambda e, ids: _eq(looks(e), [[], [], []]), pre=_pre_mono_b),
    Case("turn the black and white off", lambda e, ids: _eq(looks(e), [[], [], []]), pre=_pre_mono_b),
    Case("take the fade off the last clip", lambda e, ids: _no_v1_fades(e), pre=_pre_fade_c),
    # ---- a negative rotation keeps its sign -------------------------------
    Case("rotate clip 3 -45 degrees", lambda e, ids: _eq([_rot(c) for c in v1(e)], [0.0, 0.0, -45.0])),
    Case("rotate clip 1 minus 30 degrees", lambda e, ids: _eq([_rot(c) for c in v1(e)], [-30.0, 0.0, 0.0])),
    Case("rotate the second clip -90°", lambda e, ids: _eq([_rot(c) for c in v1(e)], [0.0, -90.0, 0.0])),
    # ---- a transition LENGTH keeps the transitions' types -----------------
    Case("make all transitions 1 second long",
         lambda e, ids: _eq(_tr(e), [(4.0, "dissolve", 1.0), (8.0, "fade", 1.0)]), pre=_pre_two_transitions),
    Case("set the transitions to 2 seconds",
         lambda e, ids: _eq(_tr(e), [(4.0, "dissolve", 2.0), (8.0, "fade", 2.0)]), pre=_pre_two_transitions),
    Case("transitions 1s each",
         lambda e, ids: _eq(_tr(e), [(4.0, "dissolve", 1.0), (8.0, "fade", 1.0)]), pre=_pre_two_transitions),
    # ---- a music fade is the MUSIC's fade ---------------------------------
    Case("music fade in 1s and fade out 3s",
         lambda e, ids: (_no_v1_fades(e), _eq(_music_state(e), [(-14.0, 1.0, 3.0)]))),
    Case("fade in the music over 1 second and fade it out over 4 seconds",
         lambda e, ids: (_no_v1_fades(e), _eq(_music_state(e), [(-14.0, 1.0, 4.0)]))),
    Case("turn the music down 5 db and fade it out at the end",
         lambda e, ids: (_no_v1_fades(e), _eq([g for g, _i, o in _music_state(e) if o > 0], [-19.0]))),
    Case("lower the song 3 db and fade it out",
         lambda e, ids: (_no_v1_fades(e), _eq([g for g, _i, o in _music_state(e) if o > 0], [-17.0]))),
    # ---- a time range inside a named clip ---------------------------------
    Case("cut the first 2 seconds of clip 3", lambda e, ids: _eq(_cov(e), [(0.0, 8.0), (10.0, 12.0)])),
    Case("trim the last second off clip 1", lambda e, ids: _eq(_cov(e), [(0.0, 3.0), (4.0, 12.0)])),
    # ---- clip lists written as ranges / with an elided noun ---------------
    Case("delete clips 1-2", lambda e, ids: _eq(ins(e), [8.0])),
    Case("mute clips 1 through 3", lambda e, ids: _eq([c.audio.mute for c in v1(e)], [True, True, True])),
    Case("give the first clip a warm look and the last a cool look",
         lambda e, ids: _eq(looks(e), [["warm.cube"], [], ["cool.cube"]])),
    # ---- a glued dB unit is a level ----------------------------------------
    Case("reverse the selected clip and turn it up 3db",
         _only("B", lambda c: c.reverse and abs(c.audio.gain_db - 3.0) < 0.01)),
    Case("raise clip 3 by 2db", lambda e, ids: _eq(gains(e), [0.0, 0.0, -4.0]), pre=_pre_gain("C", -6.0)),
    Case("boost clip 2 by 3db", lambda e, ids: _eq(gains(e), [0.0, 3.0, 0.0])),
    Case("turn up clip 2 and turn down clip 1", lambda e, ids: _eq(gains(e), [-6.0, 6.0, 0.0])),
    # ---- except the music --------------------------------------------------
    Case("turn down all the clips except the music by 3dB",
         lambda e, ids: (_eq(gains(e), [-3.0, -3.0, -3.0]), _eq(_music_state(e)[0][0], -14.0))),
    # ---- back to normal ----------------------------------------------------
    Case("clip 2 volume back to normal", lambda e, ids: _eq(gains(e), [0.0, 0.0, 0.0]), pre=_pre_gain("B", -6.0)),
    # ---- "slow … down N%" is relative -------------------------------------
    Case("slow clip 1 down 25%", lambda e, ids: _eq(_speeds(e), [0.75, 1.0, 1.0])),
    # ---- "put clip 1 between clip 2 and clip 3" moves it -------------------
    Case("put clip 1 between clip 2 and clip 3", lambda e, ids: (_eq(ins(e), [4.0, 0.0, 8.0]), _eq(_tr(e), []))),
    # ---- a title's look AND place ------------------------------------------
    Case("make the title text bigger and move it to the top",
         _summer(lambda t: t.style.size > 96 and t.transform.y < 810), pre=_pre_summer),
    Case("make the title smaller and put it at the top",
         _summer(lambda t: t.style.size < 96 and t.transform.y < 810), pre=_pre_summer),
    # ---- counts and lengths ------------------------------------------------
    Case("duplicate the selected clip twice", lambda e, ids: _eq(ins(e), [0.0, 4.0, 4.0, 4.0, 8.0])),
    Case("duplicate clip 1 three times", lambda e, ids: _eq(ins(e), [0.0, 0.0, 0.0, 0.0, 4.0, 8.0])),
    Case("hold a still frame at 1s for half a second",
         lambda e, ids: _eq([round(float(c.freeze), 2) for c in v1(e) if c.freeze is not None], [0.5])),
    # below the shortest hold (0.5 s): say so, never the 3 s default
    Case("freeze at 6 seconds for a quarter second", ASKS, question="0.5"),
    Case("freeze at 6 seconds for a second and a half",
         lambda e, ids: _eq([round(float(c.freeze), 2) for c in v1(e) if c.freeze is not None], [1.5])),
    # ---- removal distributes over "X and the Y" ---------------------------
    Case("remove the music and the captions",
         lambda e, ids: _eq((len(music(e).clips), len(e.get_track("captions").clips if e.get_track("captions") else [])),
                            (0, 0)), pre=_pre_captions),
    # ---- part of a clip is not the clip ------------------------------------
    Case("remove the middle of clip 2", ASKS),
    # ---- relative words on a changed value ---------------------------------
    Case("a bit slower on the second clip", lambda e, ids: _eq(_speeds(e), [1.0, 1.6, 1.0]), pre=_pre_speed_b2),
    Case("music +3db", lambda e, ids: _eq(_music_state(e)[0][0], -11.0)),
    # ---- fades taken off never ADD fades -----------------------------------
    Case("remove the fades", lambda e, ids: _no_v1_fades(e), pre=_pre_fade_c),
    Case("remove the fade from the last clip", lambda e, ids: _no_v1_fades(e), pre=_pre_fade_c),
    # ---- clear requests that were lost (MEDIUM) ----------------------------
    Case("i want clip 2 at like 1.5 speed", lambda e, ids: _eq(_speeds(e), [1.0, 1.5, 1.0])),
    Case("clip 2 speed = 2", lambda e, ids: _eq(_speeds(e), [1.0, 2.0, 1.0])),
    Case("set speed 0.8 for all clips", lambda e, ids: _eq(_speeds(e), [0.8, 0.8, 0.8])),
    Case("turn the audio back on for clip 2", lambda e, ids: _eq([c.audio.mute for c in v1(e)], [False] * 3),
         pre=_pre_mute_b),
    Case("no audio on the last clip pls", lambda e, ids: _eq([c.audio.mute for c in v1(e)], [False, False, True])),
    Case("shut off the sound on clip two", lambda e, ids: _eq([c.audio.mute for c in v1(e)], [False, True, False])),
    Case("make clip 1 silent", lambda e, ids: _eq([c.audio.mute for c in v1(e)], [True, False, False])),
    Case("mute all clips but keep music",
         lambda e, ids: (_eq([c.audio.mute for c in v1(e)], [True] * 3), _eq(music(e).muted, False))),
    Case("keep 4s-8s only", lambda e, ids: _eq(_cov(e), [(4.0, 8.0)])),
    Case("remove seconds 10 through 12", lambda e, ids: _eq(_cov(e), [(0.0, 10.0)])),
    Case("take 2 seconds off the end of the video", lambda e, ids: _eq(_cov(e), [(0.0, 10.0)])),
    Case("switch clips 2 and 3", lambda e, ids: _eq(ins(e), [0.0, 8.0, 4.0])),
    Case("divide the first clip at 2s", lambda e, ids: _eq(ins(e), [0.0, 2.0, 4.0, 8.0])),
    Case("zoom abit on clip one", _only("A", lambda c: True)),
    Case("reset the zoom on clip 2", lambda e, ids: _eq([_scale(c) for c in v1(e)], [1.0, 1.0, 1.0]),
         pre=_pre_zoom_b),
    Case("font size 72 for the title", _summer(lambda t: t.style.size == 72), pre=_pre_summer),
    Case("change the title colour to white", _summer(lambda t: t.style.color.upper() == "#FFFFFF"),
         pre=_pre_summer_red),
    Case("extend the SALE text by 2 seconds",
         lambda e, ids: _eq([(t.text, t.start, t.end) for t in texts(e)], [("Summer Trip", 0.0, 3.0), ("SALE", 5.0, 9.0)]),
         pre=_pre_summer_and_sale),
    Case("make all the text red", lambda e, ids: _eq([t.style.color.upper() for t in texts(e)], ["#FF3B30"] * 2),
         pre=_pre_summer_and_sale),
    Case("make the title twice as big", _summer(lambda t: t.style.size == 192), pre=_pre_summer),
    Case("make the titel bigr", _summer(lambda t: t.style.size > 96), pre=_pre_summer),
    # ---- the recipes read what Apple Intelligence answered off-topic -------
    Case("set clip 3 to -6 db", lambda e, ids: _eq(gains(e), [0.0, 0.0, -6.0])),
    Case("tilt clip one by 10 degrees", lambda e, ids: _eq([_rot(c) for c in v1(e)], [10.0, 0.0, 0.0])),
    Case("make the subtitles blue and move them up top",
         lambda e, ids: _eq((e.get_track("captions").config.position,
                             e.get_track("captions").config.look.color.upper()), ("top", "#0A84FF")),
         pre=_pre_captions),
]


@pytest.mark.usefixtures("no_downloads")
@pytest.mark.parametrize("case", CASES, ids=[f"{i:02d}-{c.phrase}" for i, c in enumerate(CASES)])
def test_s2_phrase_does_the_right_edit_or_asks(media, case, request):
    st, ids = _session(media, f"s_s2{request.node.callspec.id[:2]}")
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
        assert e.hash() == before, f"the timeline changed on a phrase that must ask: {said}"
        assert "?" in said and (case.question or "") in said, said
        return
    assert e.hash() != before, f"nothing changed: {said}"
    ver = next((x for x in events if x["type"] == "verify"), None)
    assert ver is not None and ver["passed"] == ver["total"], (ver, said)
    assert "done with issues" not in said, said
    case.expect(e, ids)


# --------------------------------------------------------------------------- the rollback card

def _answer(st: EDLStore, events: list[dict], pick: str) -> list[dict]:
    card = [x for x in events if x["type"] == "clarify"]
    assert card, _question_text(events)
    q = card[-1]["questions"][0]
    return F.collect(service.resume(st, card[-1]["token"], {q["key"]: pick}, history=[], user_message=pick))


def _options(events: list[dict]) -> list[str]:
    card = [x for x in events if x["type"] == "clarify"]
    return [o.get("value") for q in (card[-1]["questions"] if card else []) for o in (q.get("options") or [])]


@pytest.mark.usefixtures("no_downloads")
@pytest.mark.parametrize("phrase,pick", [("make the titel bigr", "title"), ("make the title twice as big", "title")])
def test_answering_the_rollback_card_never_adds_the_refused_title(media, phrase, pick, request):
    st, ids = _session(media, f"s_s2rb{abs(hash(phrase)) % 10_000}")
    _pre_summer(st, ids)
    events = _turn(st, phrase, dict(UI, selection=ids["B"]))
    if pick in _options(events):
        events = _answer(st, events, pick)
    ts = texts(st.edl)
    assert [t.text for t in ts] == ["Summer Trip"], (_question_text(events), [t.text for t in ts])
    assert ts[0].style.size >= 96, ts[0].style.size


@pytest.mark.usefixtures("no_downloads")
def test_the_rollback_card_does_not_offer_a_pick_that_replays_the_refused_plan(media):
    """"split at 6s then mute the second half" muted the MUSIC and dropped the
    split; picking 'mute' from the card replayed exactly that."""
    st, ids = _session(media, "s_s2rb_mute")
    events = _turn(st, "split at 6s then mute the second half", dict(UI, selection=ids["B"]))
    for _ in range(2):
        if "mute" not in _options(events):
            break
        events = _answer(st, events, "mute")
    e = st.edl
    assert not e.get_track("music").muted and not any(c.audio.mute for c in music(e).clips), _question_text(events)


@pytest.mark.usefixtures("no_downloads")
def test_a_pick_that_replans_the_undone_edit_does_not_run_it_again(media):
    """The resume guard: whatever the menu offered, a pick whose re-plan is
    the rolled-back plan's own steps never runs (the pick licensed it)."""
    from video_ai_editor.agent.prompt import executor, pending, planner
    from video_ai_editor.agent.prompt.recipes import ask
    from video_ai_editor.agent.prompt.schema import Plan
    st, ids = _session(media, "s_s2rb_guard")
    dispatch(st, "add_text", {"text": "Summer Trip", "start": 0.0, "end": 3.0, "y": 810, "size": 96})
    facts = service.build_facts_for(st, dict(UI, selection=ids["B"]))
    prompt = "make the title say bigr"
    undone = planner.plan_as(prompt, "title", facts)
    assert undone.steps, undone
    card = Plan.new(intent="clarify", brain="recipes", confidence=0.3, title="Which edit?",
                    needs_input=[ask("intent", "I undid that. Which did you mean?", options=[("title", "Title")])])
    rec = pending.save_pending(Path(st.dir), plan=card, prompt=prompt, facts=facts, ui_state=None,
                               rollback={"steps": executor.step_signature(undone),
                                         "reasons": [{"kind": "unasked", "message": "it added a new title"}]})
    before = st.edl.hash()
    events = F.collect(service.resume(st, rec["token"], {"intent": "title"}, history=[], user_message="title"))
    said = _question_text(events)
    assert st.edl.hash() == before, said
    assert "same change I just undid" in said, said
    assert pending.load_pending(Path(st.dir)) is None


def test_the_net_keeps_a_refusal_a_pick_would_license(media):
    """`safety_net` with a pick: the refusal of the first run still stands."""
    from video_ai_editor.agent.prompt import executor
    from video_ai_editor.agent.prompt.contract import Contract
    st, ids = _session(media, "s_s2rb_net")
    before = st.edl.model_copy(deep=True)
    dispatch(st, "set_track_muted", {"track": "music", "muted": True})
    prompt = "split at 6s then mute the second half"
    plain = [v.as_dict() for v in Contract.read(prompt).judge(before, st.edl)]
    assert any(v["kind"] == "unasked" for v in plain), plain
    picked = [v.as_dict() for v in Contract.read(prompt, picked=("mute",)).judge(before, st.edl)]
    assert not any(v["kind"] == "unasked" for v in picked), picked      # the pick alone licenses it

    class _R:
        edl_before = before
        steps: list = []
    facts = service.build_facts_for(st, dict(UI, selection=ids["B"]))
    from video_ai_editor.agent.prompt.schema import Plan
    reasons = executor.safety_net(st, Plan.new(intent="mute", brain="recipes", confidence=0.9), _R(), facts,
                                  prompt, {"picked": ["mute"], "refused": plain})
    assert any(r["kind"] == "unasked" for r in reasons), reasons


# --------------------------------------------------------------------------- the net, on its own
#
# The planner now reads each phrase right, so the net rarely sees the wrong
# edit any more. These feed the contract the WRONG edit each phrase used to
# commit and require it to be refused.

def _d(tool: str, args):
    return lambda st, ids: dispatch(st, tool, {k: (ids[v] if isinstance(v, str) and v in ids else v)
                                              for k, v in args.items()})


def _all3(tool: str, args):
    def go(st, ids):
        for k in ("A", "B", "C"):
            dispatch(st, tool, {**args, "clip_id": ids[k]})
    return go


NET_CASES = [
    ("remove 15 seconds from the end", None, _d("cut_range", {"track": "v1", "start": 0.0, "end": 12.0}), {}),
    ("slow down the clip under the playhead", None, _all3("set_speed", {"factor": 0.8}), {"playhead": 5.5}),
    ("take the black and white off", _pre_mono_b, _d("apply_lut", {"clip_id": "A", "src": "mono.cube"}), {}),
    ("take the fade off the last clip", _pre_fade_c, _d("set_video_fade", {"clip_id": "C", "in_s": 1.0}), {}),
    ("rotate clip 3 -45 degrees", None, _d("set_clip_transform", {"clip_id": "C", "rotation": 45.0}), {}),
    ("make all transitions 1 second long", _pre_two_transitions,
     lambda st, ids: [dispatch(st, "add_transition", {"at": a, "type": "crossdissolve", "duration": 0.4})
                      for a in (4.0, 8.0)], {}),
    ("music fade in 1s and fade out 3s", None, _d("set_video_fade", {"clip_id": "C", "out_s": 3.0}), {}),
    ("cut the first 2 seconds of clip 3", None, _d("cut_range", {"track": "v1", "start": 0.0, "end": 2.0}), {}),
    ("delete clips 1-2", None, _d("ripple_delete", {"clip_id": "A"}), {}),
    ("reverse the selected clip and turn it up 3db", None, _d("set_clip_reverse", {"clip_id": "B", "reverse": True}),
     {"selection": "B"}),
    ("turn down all the clips except the music by 3dB", None, _d("set_volume", {"target": "music", "db": -17.0}), {}),
    ("clip 2 volume back to normal", _pre_gain("B", -6.0), _d("set_volume", {"target": "B", "db": -12.0}), {}),
    ("slow clip 1 down 25%", None, _d("set_speed", {"clip_id": "A", "factor": 0.25}), {}),
    ("put clip 1 between clip 2 and clip 3", None,
     _d("add_transition", {"at": 8.0, "type": "crossdissolve", "duration": 0.4}), {}),
    ("duplicate clip 1 three times", None, _d("duplicate_clip", {"clip_id": "A"}), {}),
    ("hold a still frame at 1s for half a second", None, _d("freeze_frame", {"time": 1.0, "duration": 3.0}), {}),
    ("remove the music and the captions", _pre_captions,
     lambda st, ids: (dispatch(st, "add_caption_track", {"style": "default", "position": "bottom"}),
                      dispatch(st, "bulk_delete", {"clip_ids": [c.id for c in music(st.edl).clips]})), {}),
    ("remove the middle of clip 2", None, _d("ripple_delete", {"clip_id": "B"}), {}),
    ("a bit slower on the second clip", _pre_speed_b2, _d("set_speed", {"clip_id": "B", "factor": 0.8}), {}),
    ("music +3db", None, _d("set_volume", {"target": "music", "db": 3.0}), {}),
    ("make the title text bigger and move it to the top", _pre_summer,
     lambda st, ids: dispatch(st, "set_text_style", {"clip_id": texts(st.edl)[0].id, "size_scale": 1.25}), {}),
]


@pytest.mark.parametrize("phrase,pre,wrong,ui", NET_CASES, ids=[c[0] for c in NET_CASES])
def test_the_contract_refuses_the_wrong_edit_each_phrase_used_to_commit(media, phrase, pre, wrong, ui, request):
    from video_ai_editor.agent.prompt.contract import Contract
    st, ids = _session(media, f"s_s2net{NET_CASES.index(next(c for c in NET_CASES if c[0] == phrase)):02d}")
    if pre:
        pre(st, ids)
    before = st.edl.model_copy(deep=True)
    wrong(st, ids)
    sel = ids.get(ui.get("selection")) if ui.get("selection") else None
    found = Contract.read(phrase, selection=sel, playhead=ui.get("playhead")).judge(before, st.edl)
    assert found, f"the net let {phrase!r} commit the wrong edit"


def test_an_off_topic_on_device_answer_falls_through():
    """Apple Intelligence answered "set clip 3 to 0 db" with a voice-over and a
    60 MB download offer, and "tilt clip one by 10 degrees" with "already
    16:9": the router must not end the turn on either."""
    from video_ai_editor.agent.prompt.brains import router
    from video_ai_editor.agent.prompt.recipes import ask
    from video_ai_editor.agent.prompt.schema import Plan
    vo = Plan.new(intent="voiceover", brain="apple_intelligence", confidence=0.8,
                  needs_input=[ask("text", "What should the voiceover say?", kind="text")])
    assert router.off_topic(vo, "set clip 3 to 0 db")
    reply = Plan.new(intent="reframe", brain="apple_intelligence", confidence=0.8,
                     reply="The video is already 16:9 — no reframe needed.")
    assert router.off_topic(reply, "tilt clip one by 10 degrees")
    music = Plan.new(intent="music", brain="apple_intelligence", confidence=0.8,
                     reply="music is already on the timeline — say turn the music down…")
    assert router.off_topic(music, "music at minus 10 decibels")
    # an on-topic plan still passes
    ok = Plan.new(intent="rotate", brain="apple_intelligence", confidence=0.8)
    assert router.off_topic(ok, "tilt clip one by 10 degrees") is None


def test_the_rollback_menu_offers_the_edit_the_words_name():
    """"turn the audio back on for clip 2" was offered only rotate and trim."""
    from video_ai_editor.agent.prompt.planner import safe_options
    assert safe_options("turn the audio back on for clip 2")[0][0] == "mute"
    assert "volume" in [v for v, _l in safe_options("clip 1 a bit louder please")]


def test_a_lut_option_is_shown_by_its_name_not_its_file():
    """"change the title colour to white" once listed teal_orange.cube …"""
    from video_ai_editor.agent.prompt.recipes import ask
    q = ask("look", "Which look?", options=[("teal_orange.cube", "Cinematic"), ("mono.cube", "Black and white")])
    text = service.question_text(q)
    assert ".cube" not in text and "Cinematic" in text, text
