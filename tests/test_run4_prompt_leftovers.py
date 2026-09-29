"""Run 4 (Q3) — the Prompt-bar leftovers from the run-3 fixers, at the unit
level: the shared readings in `semantics.py` (a percentage is OF THE CURRENT
level; "twice as fast" / "half speed" scale the clip's own speed; "shorten
the video by 3 s" is its tail; the strength of a look; ranges anchored on
the playhead or a marker), the planner's questions for a missing anchor,
`set_text_style`'s In / Out animation, and the verifier's reading of the
`$playhead` sentinel. The service-level proof of each phrase is the K3
corpus (tests/test_k3_prompt_corpus.py, "run 4 (Q3)" block).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.agent.prompt import planner as P  # noqa: E402
from video_ai_editor.agent.prompt import semantics as M  # noqa: E402
from video_ai_editor.agent.prompt.contract import Contract  # noqa: E402
from video_ai_editor.agent.prompt.facts import ClipFact, TimelineFacts  # noqa: E402
from video_ai_editor.edl.snapshot import EDLStore  # noqa: E402


# --------------------------------------------------------------------------- semantics

@pytest.mark.parametrize("phrase,direction,delta,db,ambiguous", [
    ("music volume 50%", "down", -6.02, None, False),
    ("set the music to 50%", "down", -6.02, None, False),
    ("music volume 200%", "up", 6.02, None, False),
    ("turn the music down to 20%", "down", -13.98, None, False),
    ("set clip 2 volume to 0%", "down", -60.0, None, False),
    ("lower the volume of the last clip by 50%", "down", -6.02, None, False),
    ("turn the music up by 50%", "up", 3.52, None, False),
    ("turn the music up to 50%", "up", None, None, True),      # 50 % of now is quieter: ask
    ("music at 100%", None, None, None, True),                 # the level it already plays at
    ("set the music to -25 dB", None, None, -25.0, False),     # a dB number is still a level
])
def test_percent_is_relative_to_the_current_level(phrase, direction, delta, db, ambiguous):
    la = M.level_ask(phrase)
    assert (la.direction, la.delta_db, la.db, bool(la.ambiguous)) == (direction, delta, db, ambiguous), la


@pytest.mark.parametrize("phrase,factor,scale", [
    ("twice as fast", 2.0, True),
    ("double the speed of clip 2", 2.0, True),
    ("3 times faster", 3.0, True),
    ("2 times slower", 0.5, True),
    ("half speed", 0.5, True),
    ("drop the speed of the last clip by half", 0.5, True),
    ("make clip 2 twice as long", 0.5, True),
    ("quarter speed", 0.25, True),
    ("slow motion", 0.5, False),                # a named speed on the dial
    ("make it 2x", 2.0, False),
    ("set it to 1.5x speed", 1.5, False),
    ("50% faster", 1.5, False),                 # unchanged: relative to 1x
])
def test_multiples_scale_the_clips_own_speed(phrase, factor, scale):
    sa = M.speed_ask(phrase)
    assert (sa.factor, sa.scale, bool(sa.ambiguous)) == (factor, scale, False), sa


@pytest.mark.parametrize("phrase,seconds", [
    ("shorten the video by 3 seconds", 3.0),
    ("make the video 3 seconds shorter", 3.0),
    ("cut 3 seconds from the video", 3.0),
    ("take 2 seconds off the video", 2.0),
    ("make it 2 seconds shorter", 2.0),
    ("cut the video down by 4 seconds", 4.0),
    ("shorten it by half a second", 0.5),
    ("trim the whole thing by 1 minute", 60.0),
    ("shorten clip 2 by 1 s", None),           # a clip's own length
    ("shorten the title by 1 second", None),   # a text's own length
    ("make the last clip 2 seconds shorter", None),
    ("shorten the video to 8 seconds", None),  # a target length, not an amount
])
def test_shorten_the_video_by_is_its_tail(phrase, seconds):
    assert M.whole_video_by(phrase) == seconds
    if seconds is not None:
        assert [(t.kind, t.a) for t in M.time_refs(phrase)] == [("last", seconds)]


@pytest.mark.parametrize("phrase,way", [
    ("make the warm look stronger", "up"),
    ("stronger warm look on clip 2", "up"),
    ("crank the filter up", "up"),
    ("make the warm look weaker", "down"),
    ("tone the filter down", "down"),
    ("dial the cinematic look back", "down"),
    ("the look is too strong", "down"),
    ("apply the warm look", None),
    ("make it brighter still", None),          # brightness, not a look's strength
])
def test_look_strength_direction(phrase, way):
    assert M.direction(phrase, "look") == way


@pytest.mark.parametrize("phrase,anchor,side,label", [
    ("from here to the end make it black and white", "playhead", "from", None),
    ("delete everything after the playhead", "playhead", "from", None),
    ("speed up everything after the playhead", "playhead", "from", None),
    ("from the playhead onwards", "playhead", "from", None),
    ("the rest of the video from here", "playhead", "from", None),
    ("trim from the start to the playhead", "playhead", "to", None),
    ("mute everything up to here", "playhead", "to", None),
    ("cut from the marker to the end", "marker", "from", None),
    ("everything before the marker", "marker", "to", None),
    ("after the intro marker", "marker", "from", "intro"),
    ("from the marker called outro to the end", "marker", "from", "outro"),
])
def test_ui_ranges_are_read(phrase, anchor, side, label):
    ur = M.ui_range(phrase)
    assert ur is not None and (ur.anchor, ur.side, ur.label) == (anchor, side, label), ur


@pytest.mark.parametrize("phrase", [
    "mute from here",                       # the clip under the playhead (unchanged reading)
    "cut from 0:02 to 0:05",                # a numeric range wins
    "add a marker at 3 seconds",
    "split at the marker",
    "remove the markers",
    "from here to the marker",              # anchored on both ends: not one range
])
def test_ui_ranges_not_read(phrase):
    assert M.ui_range(phrase) is None


# --------------------------------------------------------------------------- planner

def _facts(**kw) -> TimelineFacts:
    base = dict(session_id="s", duration=12.0, video_end=12.0, canvas_w=1920, canvas_h=1080, fps=30, aspect="16:9",
                v1_clip_ids=["a", "b", "c"], clip_ids=["a", "b", "c"], track_ids=["v1"], selection="b", playhead=5.5,
                v1_boundaries=[4.0, 8.0],
                clips=[ClipFact(id="a", track="v1", start=0, duration=4, src_in=0, src_out=4),
                       ClipFact(id="b", track="v1", start=4, duration=4, src_in=4, src_out=8),
                       ClipFact(id="c", track="v1", start=8, duration=4, src_in=8, src_out=12)])
    base.update(kw)
    return TimelineFacts(**base)


def test_marker_range_without_a_marker_is_one_question():
    p = P.plan("cut from the marker to the end", _facts())
    assert p.intent == "ask" and not p.steps and "no marker" in p.reply, (p.intent, p.reply)


def test_marker_range_with_two_markers_asks_which():
    p = P.plan("cut from the marker to the end", _facts(markers=[(4.0, "intro"), (8.0, "outro")]))
    assert p.intent == "ask" and not p.steps and "Which marker" in p.reply and "intro" in p.reply, p.reply


def test_named_marker_range_cuts_from_it():
    p = P.plan("cut from the outro marker to the end", _facts(markers=[(4.0, "intro"), (8.0, "outro")]))
    assert [(s.tool, s.args["start"], s.args["end"]) for s in p.steps] == [("cut_range", 8.0, 12.0)], p.steps


def test_playhead_range_at_the_end_asks():
    p = P.plan("delete everything after the playhead", _facts(playhead=12.0))
    assert p.intent == "ask" and not p.steps and "end of the video" in p.reply, p.reply


def test_playhead_range_unknown_playhead_asks():
    p = P.plan("mute from here to the end", _facts(playhead=None))
    assert p.intent == "ask" and not p.steps and "playhead" in p.reply.lower(), p.reply


def test_from_here_to_the_end_splits_at_the_playhead_then_edits_the_rest():
    p = P.plan("from here to the end make it black and white", _facts())
    assert [(s.tool, s.args.get("time"), s.args.get("clip_id")) for s in p.steps] == [
        ("split_at", 5.5, None), ("apply_lut", None, "$playhead"), ("apply_lut", None, "c")], p.steps


def test_look_strength_steps_the_clips_that_carry_it():
    f = _facts(clips=[ClipFact(id="a", track="v1", start=0, duration=4, src_in=0, src_out=4),
                      ClipFact(id="b", track="v1", start=4, duration=4, src_in=4, src_out=8,
                               looks=["warm.cube"], look_intensity={"warm.cube": 0.6}),
                      ClipFact(id="c", track="v1", start=8, duration=4, src_in=8, src_out=12)])
    p = P.plan("make the warm look stronger", f)
    assert [(s.tool, s.args["clip_id"], s.args["intensity"]) for s in p.steps] == [("apply_lut", "b", 0.8)], p.steps
    p = P.plan("make the warm look weaker", f)
    assert [(s.tool, s.args["clip_id"], s.args["intensity"]) for s in p.steps] == [("apply_lut", "b", 0.4)], p.steps
    p = P.plan("make the warm look stronger", _facts())
    assert not p.steps and "no warm look" in p.reply, p.reply


def test_scope_only_lead_clause_is_not_reported_undone():
    f = _facts()
    p = P.plan("take clip 3, make it 2x and black and white", f)
    assert "Not done" not in (p.reply or ""), p.reply
    assert sorted(s.tool for s in p.steps) == ["apply_lut", "set_speed"] and all(s.args["clip_id"] == "c" for s in p.steps)


def test_two_restyles_naming_different_texts_stay_two_edits():
    from video_ai_editor.agent.prompt.recipes import Intent
    a = Intent("title", {"_restyle": True, "_look": {"color": "#FF3B30"}}, 1.0, "make the Day One title red")
    b = Intent("title", {"_restyle": True, "_look": {"color": "#0A84FF"}}, 1.0, "the SALE text blue")
    assert P._another_title(a, b) is True
    c = Intent("title", {"_restyle": True, "_look": {"size_scale": 1.25}}, 1.0, "the title bigger")
    assert P._another_title(a, c) is False              # "the title" names no other text: one restyle


def test_split_at_the_marker_is_its_moment():
    p = P.plan("split at the marker", _facts(markers=[(6.0, "cut here")]))
    assert [(s.tool, s.args["time"]) for s in p.steps] == [("split_at", 6.0)], p.steps
    p = P.plan("split at the intro marker", _facts(markers=[(4.5, "intro"), (8.5, "outro")]))
    assert [(s.tool, s.args["time"]) for s in p.steps] == [("split_at", 4.5)], (p.steps, p.reply)
    p = P.plan("split at the marker", _facts(markers=[(4.5, "intro"), (8.5, "outro")]))
    assert p.intent == "ask" and not p.steps and "Which marker" in p.reply, p.reply
    p = P.plan("split at the marker", _facts())
    assert p.intent == "ask" and not p.steps and "no marker" in p.reply, p.reply


def test_the_intro_marker_is_not_the_intro_clip():
    from video_ai_editor.agent.prompt import grammar as G
    assert G.clip_ref_of("split at the intro marker") is None
    assert G.clip_ref_of("speed up the intro") == "$v1_first"


def test_title_retime_reads_both_clauses():
    assert P._title_retime("make the title appear at 3s and disappear at 7s") == {"start": 3.0, "end": 7.0}
    assert P._title_retime("make the title appear at 3s") == {"start": 3.0}


@pytest.mark.parametrize("clause,look", [
    ("fade the title in", {"anim_in": "fade"}),
    ("make the title fade in", {"anim_in": "fade"}),
    ("the title should fade out", {"anim_out": "fade"}),
    ("slide the title up", {"anim_in": "slide_up"}),
    ("pop the text in", {"anim_in": "pop"}),
    ("remove the title's fade in", {"anim_in": ""}),
    ("fade the video in", {}),
])
def test_text_animation_words(clause, look):
    assert {k: v for k, v in P.text_look_of(clause).items() if k.startswith("anim")} == look


# --------------------------------------------------------------------------- contract

def test_contract_binds_ui_ranges_to_the_playhead_and_markers():
    c = Contract.read("from here to the end make it black and white", playhead=5.5, video_end=12.0)
    assert c.reads[0].ui_span == (5.5, 12.0)
    c = Contract.read("cut from the marker to the end", playhead=5.5, video_end=12.0, markers=[(6.0, "cut here")])
    assert c.reads[0].ui_span == (6.0, 12.0)
    c = Contract.read("cut from the marker to the end", playhead=5.5, video_end=12.0,
                      markers=[(4.0, "intro"), (8.0, "outro")])
    assert c.reads[0].ui_span is None                       # which marker? — nothing bound
    c = Contract.read("everything before the intro marker", video_end=12.0, markers=[(4.0, "intro"), (8.0, "outro")])
    assert c.reads[0].ui_span == (0.0, 4.0)


# --------------------------------------------------------------------------- dispatch

def test_set_text_style_sets_and_clears_the_animation(tmp_path):
    st = EDLStore(tmp_path / "s")
    src = F.speech_clip(tmp_path)
    dispatch(st, "add_clip", {"track": "v1", "src": str(src), "in": 0, "out": 4, "start": 0})
    dispatch(st, "add_text", {"text": "Summer Trip", "start": 0.0, "end": 3.0})
    tid = next(c.id for t in st.edl.tracks for c in t.clips if getattr(c, "text", None) == "Summer Trip")
    out = dispatch(st, "set_text_style", {"clip_id": tid, "anim_in": "fade"})
    t = st.edl.get_clip(tid)[1]
    assert (t.anim_in, t.anim_out, t.text) == ("fade", None, "Summer Trip") and "anim in fade" in out["changed"]
    dispatch(st, "set_text_style", {"clip_id": tid, "anim_out": "slide_down"})
    assert (st.edl.get_clip(tid)[1].anim_in, st.edl.get_clip(tid)[1].anim_out) == ("fade", "slide_down")
    dispatch(st, "set_text_style", {"clip_id": tid, "anim_in": ""})
    assert st.edl.get_clip(tid)[1].anim_in is None
    with pytest.raises(ValueError):
        dispatch(st, "set_text_style", {"clip_id": tid, "anim_in": "spin"})
