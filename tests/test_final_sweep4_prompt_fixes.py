"""Final QA sweep 4 — the Prompt-bar readings the editor-ux and prompt-assistant
finders confirmed on 0.8.0 rc, at the planner level (`planner.plan` on a
synthetic timeline), each one failing before its fix:

  * a level clause about the voice-over / narration is a level, never a NEW
    spoken voice-over (it asked to download a Piper voice);
  * "clip 2 and clip 4" is one two-clip list; "silence <clips>" mutes them;
    "take the warm off clip 1" removes the look; "un-mute" unmutes;
  * "scale … down to 80%" is 80 %, not 180 %; "between clip 1 and clip 3"
    is clip 2; "at the very end" / "from the playhead" place a title;
  * "lower the music while the coach is talking" ducks; playhead-relative
    ranges keep their duration / start;
  * a stated amount is never replaced by the default step (dB, seconds,
    speed factor, grade, duck level);
  * per-clip scope holds for freeze / split every / mirror vertically /
    "between clips 1-2 only" / music in-and-out fades.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from prompt_fixtures import desktop_posture  # noqa: E402,F401
from video_ai_editor.agent.prompt import grammar as G  # noqa: E402
from video_ai_editor.agent.prompt import planner as P  # noqa: E402
from video_ai_editor.agent.prompt import semantics as M  # noqa: E402
from video_ai_editor.agent.prompt.contract import Contract, families_of  # noqa: E402
from video_ai_editor.agent.prompt.facts import ClipFact, TextFact, TimelineFacts  # noqa: E402


# --------------------------------------------------------------------------- timelines

def facts3(**kw) -> TimelineFacts:
    """The capcut-sweep session: three 4 s clips A/B/C, a 12 s bed at -14 dB,
    clip B selected, playhead 5.5 s."""
    base = dict(session_id="s", duration=12.0, video_end=12.0, canvas_w=1920, canvas_h=1080, fps=30, aspect="16:9",
                v1_clip_ids=["a", "b", "c"], clip_ids=["a", "b", "c", "m"], track_ids=["v1", "music"], selection="b",
                playhead=5.5, v1_boundaries=[4.0, 8.0], has_music=True, music_gain_db=-14.0, music_clip_ids=["m"],
                clips=[ClipFact(id="a", track="v1", start=0, duration=4, src_in=0, src_out=4, name="talk"),
                       ClipFact(id="b", track="v1", start=4, duration=4, src_in=4, src_out=8, name="talk"),
                       ClipFact(id="c", track="v1", start=8, duration=4, src_in=8, src_out=12, name="talk"),
                       ClipFact(id="m", track="music", start=0, duration=12, src_in=0, src_out=12, gain_db=-14.0,
                                name="bed")])
    base.update(kw)
    return TimelineFacts(**base)


RE_NAMES = ["re_agent", "re_kitchen_before", "re_kitchen_after", "re_living_before", "re_living_after"]


def facts5(**kw) -> TimelineFacts:
    """The real-estate project: five 6 s clips named after their footage."""
    ids = ["c1", "c2", "c3", "c4", "c5"]
    clips = [ClipFact(id=i, track="v1", start=6 * k, duration=6, src_in=0, src_out=6, name=n)
             for k, (i, n) in enumerate(zip(ids, RE_NAMES))]
    base = dict(session_id="s", duration=30.0, video_end=30.0, canvas_w=1920, canvas_h=1080, fps=30, aspect="16:9",
                v1_clip_ids=ids, clip_ids=ids, track_ids=["v1"], playhead=0.0, v1_boundaries=[6, 12, 18, 24],
                clips=clips)
    base.update(kw)
    return TimelineFacts(**base)


def with_vo(f: TimelineFacts) -> TimelineFacts:
    vo = ClipFact(id="vo1", track="vo", start=14, duration=6, src_in=0, src_out=6, name="re_narration")
    return f.with_(clip_ids=[*f.clip_ids, "vo1"], track_ids=[*f.track_ids, "vo"], clips=[*f.clips, vo])


def steps(p) -> list[tuple]:
    return [(s.tool, s.args) for s in p.steps]


def by_clip(p) -> list[tuple]:
    return sorted(steps(p), key=lambda s: (s[0], str(s[1].get("clip_id") or s[1].get("target") or "")))


# --------------------------------------------------------------------------- 1. voice-over levels

@pytest.mark.parametrize("phrase,db", [
    ("make the voiceover 3 dB quieter", -3.0),
    ("make the narration 3 dB quieter", -3.0),
    ("lower the narration by 3 dB", -3.0),
    ("make the narration louder", 6.0),
    ("turn the voice-over down 3 dB", -3.0),
])
def test_a_level_clause_about_the_voiceover_is_a_level_on_the_vo_lane(phrase, db):
    p = P.plan(phrase, with_vo(facts5()))
    assert steps(p) == [("set_volume", {"target": "vo", "db": db})], (p.intent, steps(p), p.reply)
    assert not p.downloads_needed and not p.needs_input


def test_narration_with_no_voiceover_lane_is_the_programmes_own_sound():
    p = P.plan("lower the narration by 3 dB", facts5())
    assert steps(p) == [("set_volume", {"target": "v1", "db": -3.0})], steps(p)


def test_a_new_voiceover_still_needs_a_creation_verb_or_words():
    assert G.detect("add a voiceover saying 'Thanks for watching'").intents == ["voiceover"]
    assert G.detect("add a narration").intents == ["voiceover"]
    assert G.detect("make the voiceover 3 dB quieter").intents == ["volume"]
    assert G.detect("make the narration louder").intents == ["volume"]


# --------------------------------------------------------------------------- 2. lists, mutes, removals

def test_a_repeated_noun_list_is_one_two_clip_list():
    p = P.plan("make clip 2 and clip 4 black and white", facts5())
    assert steps(p) == [("apply_lut", {"clip_id": "c2", "src": "mono.cube", "intensity": 0.8}),
                        ("apply_lut", {"clip_id": "c4", "src": "mono.cube", "intensity": 0.8})], (steps(p), p.reply)
    assert "Not done" not in (p.reply or "") and not p.needs_input
    assert G.split_clauses("make clip 2 and clip 4 black and white") == ["make clips 2 and 4 black and white"]
    assert G.split_clauses("mute clip 1, clip 3 and clip 5") == ["mute clips 1, 3 and 5"]
    assert G.split_clauses("mute the second clip and the fourth clip") == ["mute the second and fourth clips"]


def test_silence_named_clips_mutes_them():
    p = P.plan("silence the first two clips", facts3())
    assert by_clip(p) == [("set_clip_muted", {"clip_id": "a", "muted": True}),
                                ("set_clip_muted", {"clip_id": "b", "muted": True})], (p.intent, steps(p), p.reply)
    assert G.detect("silence the first two clips").intents == ["mute"]
    assert G.detect("silence clip 2").intents == ["mute"]
    assert G.detect("silence the silences").intents == ["remove_silences"]
    assert G.detect("remove the silences").intents == ["remove_silences"]


def test_take_the_look_off_a_clip_removes_it():
    f = facts3(clips=[ClipFact(id="a", track="v1", start=0, duration=4, src_in=0, src_out=4, looks=["warm.cube"],
                               look_intensity={"warm.cube": 1.0}, effects=["lut"]),
                      *facts3().clips[1:]])
    assert G.detect("take the warm off clip 1").intents == ["remove_feature"]
    p = P.plan("take the warm off clip 1", f)
    assert [s.tool for s in p.steps] == ["remove_effects"] and p.steps[0].args.get("clip_ids") == ["a"], (steps(p), p.reply)
    assert "lut" in p.steps[0].args.get("types", [])


def test_un_mute_with_a_hyphen_unmutes():
    p = P.plan("un-mute clip 3", facts3())
    assert steps(p) == [("set_clip_muted", {"clip_id": "c", "muted": False})], (steps(p), p.reply)
    p = P.plan("un mute clip 3", facts3())
    assert steps(p) == [("set_clip_muted", {"clip_id": "c", "muted": False})], (steps(p), p.reply)


# --------------------------------------------------------------------------- 3. zoom levels, between, title timing

@pytest.mark.parametrize("phrase,scale", [
    ("scale clip 1 down to 80%", 0.8),
    ("zoom clip 1 to 80%", 0.8),
    ("shrink clip 1 to 90%", 0.9),
    ("punch in 20% on clip 1", 1.2),
    ("zoom clip 1 to 150%", 1.5),
])
def test_a_zoom_level_named_with_to_is_that_level(phrase, scale):
    p = P.plan(phrase, facts3())
    assert steps(p) == [("set_clip_transform", {"clip_id": "a", "scale": scale})], (steps(p), p.reply)


def test_between_two_clips_is_the_clips_strictly_between():
    p = P.plan("cut out the part between clip 1 and clip 3", facts3())
    assert steps(p) == [("ripple_delete", {"clip_id": "b"})], (p.intent, steps(p), p.reply)
    p = P.plan("delete everything between the first and third clips", facts3())
    assert steps(p) == [("ripple_delete", {"clip_id": "b"})], (p.intent, steps(p), p.reply)
    p = P.plan("cut out the part between clip 1 and clip 2", facts3())
    assert not p.steps and "nothing between" in (p.reply or "").lower(), (steps(p), p.reply)


@pytest.mark.parametrize("phrase,start,end", [
    ("add the text Thanks at the very end for 2 seconds", 10.0, 12.0),
    ("put 'Chapter 1' on screen from the playhead for 3 seconds", 5.5, 8.5),
    ("add a title 'Go' starting at the playhead", 5.5, 8.5),
    ("add a title saying Go! at the playhead", 5.5, 8.5),
])
def test_title_timing_reads_the_very_end_and_from_the_playhead(phrase, start, end):
    p = P.plan(phrase, facts3())
    assert [s.tool for s in p.steps] == ["add_text"], (steps(p), p.reply)
    assert (p.steps[0].args["start"], p.steps[0].args["end"]) == (start, end), p.steps[0].args


# --------------------------------------------------------------------------- 4. ducking said in the third person

@pytest.mark.parametrize("phrase", [
    "lower the music while the coach is talking",
    "lower the music whenever someone is talking",
    "drop the music when she speaks",
    "keep the music under his voice",
])
def test_lower_the_music_while_someone_talks_is_ducking(phrase):
    p = P.plan(phrase, facts3())
    assert [s.tool for s in p.steps] == ["set_duck"], (p.intent, steps(p), p.reply)
    assert p.steps[0].args["enabled"] is True and p.steps[0].args["track"] == "music"
    assert "duck" in families_of(phrase), families_of(phrase)


def test_lower_while_talking_on_music_that_already_ducks_dips_it_further():
    p = P.plan("lower the music while the coach is talking", facts3(music_ducked=True, music_duck_db=-18.0))
    assert steps(p) == [("set_duck", {"track": "music", "enabled": True, "to_db": -24.0})], (steps(p), p.reply)


# --------------------------------------------------------------------------- 5. playhead-relative ranges

@pytest.mark.parametrize("phrase,side,span,from_s", [
    ("remove the 2 seconds after the playhead", "from", 2.0, None),
    ("delete the second before the playhead", "to", 1.0, None),
    ("cut the 3 seconds before the playhead", "to", 3.0, None),
    ("delete from 1s to the playhead", "to", None, 1.0),
    ("cut from 0:01 to the playhead", "to", None, 1.0),
    ("delete everything after the playhead", "from", None, None),
])
def test_ui_range_reads_a_duration_or_a_numeric_start(phrase, side, span, from_s):
    ur = M.ui_range(phrase)
    assert ur is not None and (ur.anchor, ur.side, ur.span_s, ur.from_s) == ("playhead", side, span, from_s), ur


@pytest.mark.parametrize("phrase,start,end", [
    ("remove the 2 seconds after the playhead", 5.5, 7.5),
    ("delete the second before the playhead", 4.5, 5.5),
    ("delete from 1s to the playhead", 1.0, 5.5),
    ("delete everything after the playhead", 5.5, 12.0),
])
def test_playhead_relative_cuts_keep_their_duration_and_start(phrase, start, end):
    p = P.plan(phrase, facts3())
    assert steps(p) == [("cut_range", {"track": "v1", "start": start, "end": end})], (p.intent, steps(p), p.reply)


def test_contract_binds_the_playhead_relative_span():
    c = Contract.read("remove the 2 seconds after the playhead", playhead=5.5, video_end=12.0)
    assert c.reads[0].ui_span == (5.5, 7.5)
    c = Contract.read("delete from 1s to the playhead", playhead=5.5, video_end=12.0)
    assert c.reads[0].ui_span == (1.0, 5.5)


# --------------------------------------------------------------------------- 6. a stated amount is the amount

def test_a_level_said_with_to_and_no_unit_is_that_level():
    p = P.plan("drop the bed to -30", facts3())
    assert steps(p) == [("set_volume", {"target": "music", "db": -30.0})], (steps(p), p.reply)


def test_bare_up_down_numbers_in_a_two_lane_level_clause_are_decibels():
    p = P.plan("voice up 3, music down 3", facts3())
    assert by_clip(p) == [("set_volume", {"target": "music", "db": -17.0}),
                                ("set_volume", {"target": "v1", "db": 3.0})], (steps(p), p.reply)


def test_half_a_second_fade_is_half_a_second():
    p = P.plan("fade the first clip in over half a second", facts3())
    assert steps(p) == [("set_video_fade", {"clip_id": "$v1_first", "in_s": 0.5}),
                        ("add_fade", {"clip_id": "$v1_first", "in_s": 0.5})], (steps(p), p.reply)


def test_slow_it_to_half_is_half_speed():
    p = P.plan("reverse clip 3 and slow it to half", facts3())
    assert by_clip(p) == [("set_clip_reverse", {"clip_id": "c", "reverse": True}),
                                ("set_speed", {"clip_id": "c", "factor": 0.5})], (steps(p), p.reply)


def test_speed_up_until_a_clip_is_n_seconds_long_is_that_factor():
    p = P.plan("speed clip 1 up until it's 2 seconds long", facts3())
    assert steps(p) == [("set_speed", {"clip_id": "$v1_first", "factor": 2.0})], (steps(p), p.reply)


def test_a_signed_grade_amount_and_by_half_are_read():
    p = P.plan("brightness +0.2 on the first clip", facts3())
    assert steps(p) == [("color_grade", {"clip_id": "$v1_first", "brightness": 0.2})], (steps(p), p.reply)
    p = P.plan("reduce saturation on clip 2 by half", facts3())
    assert steps(p) == [("color_grade", {"clip_id": "b", "saturation": 0.5})], (steps(p), p.reply)


def test_duck_to_a_bare_level_is_that_level():
    p = P.plan("duck the bed under the speech to -20", facts3())
    assert steps(p) == [("set_duck", {"track": "music", "enabled": True, "to_db": -20.0})], (steps(p), p.reply)


# --------------------------------------------------------------------------- 7. per-clip scope

def test_freeze_the_first_frame_of_a_named_clip_is_that_clips_start():
    p = P.plan("freeze the first frame of clip 2 for 1s", facts3())
    assert steps(p) == [("freeze_frame", {"time": 4.0, "duration": 1.0})], (steps(p), p.reply)
    p = P.plan("freeze the last frame of clip 2 for 1s", facts3())
    assert [s.tool for s in p.steps] == ["freeze_frame"] and abs(p.steps[0].args["time"] - (8.0 - 1 / 30)) < 1e-3


def test_split_every_clip_in_half_splits_each_at_its_midpoint():
    p = P.plan("split every clip in half", facts3())
    assert steps(p) == [("split_at", {"track": "v1", "time": 2.0}), ("split_at", {"track": "v1", "time": 6.0}),
                        ("split_at", {"track": "v1", "time": 10.0})], (steps(p), p.reply)


def test_mirror_vertically_flips_the_vertical_axis_only():
    p = P.plan("mirror the middle clip vertically", facts3())
    assert steps(p) == [("flip_clip", {"clip_id": "b", "axis": "vertical", "value": True})], (steps(p), p.reply)
    p = P.plan("mirror the middle clip", facts3())
    assert steps(p) == [("flip_clip", {"clip_id": "b", "axis": "horizontal", "value": True})], (steps(p), p.reply)


def test_transition_between_clips_1_2_only_is_one_seam():
    p = P.plan("transition between clips 1-2 only", facts3())
    assert [(s.tool, s.args["at"]) for s in p.steps] == [("add_transition", 4.0)], (steps(p), p.reply)
    p = P.plan("add a dissolve between clips 2 and 3", facts3())
    assert [(s.tool, s.args["at"]) for s in p.steps] == [("add_transition", 8.0)], (steps(p), p.reply)


def test_music_in_over_x_and_out_over_y_sets_both_fades():
    p = P.plan("fade the music in over 1 second and out over 3", facts3())
    assert steps(p) == [("fit_music_to_video", {"fade_in": 1.0, "fade_out": 3.0})], (steps(p), p.reply)
    assert "Not done" not in (p.reply or "")


# --------------------------------------------------------------------------- 8. clips named by their footage

@pytest.mark.parametrize("phrase,ids", [
    ("make the before shots black and white", ["c2", "c4"]),
    ("make the kitchen before shot black and white", ["c2"]),
    ("make the before clips black and white", ["c2", "c4"]),
    ("make the two before shots black and white", ["c2", "c4"]),
    ("make re_kitchen_before and re_living_before black and white", ["c2", "c4"]),
    ("make the living after shot black and white", ["c5"]),
])
def test_a_look_named_by_shot_name_lands_on_those_clips_only(phrase, ids):
    p = P.plan(phrase, facts5())
    assert [(s.tool, s.args["clip_id"], s.args["src"]) for s in p.steps] == [("apply_lut", i, "mono.cube") for i in ids], \
        (steps(p), p.reply)
    assert "Not done" not in (p.reply or "") and not p.needs_input


def test_a_shot_name_that_matches_nothing_asks_instead_of_widening_to_every_clip():
    p = P.plan("make the garage shot black and white", facts5())
    assert not p.steps and "which clip" in (p.reply or "").lower(), (steps(p), p.reply)


# --------------------------------------------------------------------------- 9. titles on clips

def test_a_second_title_on_a_clip_that_has_one_stacks_instead_of_replacing():
    f = facts3(texts=[TextFact(id="t1", text="Step 3: Plank", role="super", start=8.0, end=11.0)])
    p = P.plan("add a title 'Hold it' on clip 3", f)
    assert [s.tool for s in p.steps] == ["add_text"], (steps(p), p.reply)
    a = p.steps[0].args
    assert (a["start"], a["end"], a.get("allow_stack")) == (8.0, 11.0, True), a
    # a clip with no title keeps the plain add (no lane of its own needed)
    p = P.plan("add a title 'Warm up' on clip 1", f)
    assert p.steps[0].args.get("allow_stack") is None, p.steps[0].args


def test_two_titles_with_their_own_clip_placements_are_two_titles():
    p = P.plan("add a title 'BEFORE' on clip 2 and 'AFTER' on clip 3", facts5())
    got = sorted((s.args["text"], s.args["start"], s.args["end"]) for s in p.steps if s.tool == "add_text")
    assert got == [("AFTER", 12.0, 15.0), ("BEFORE", 6.0, 9.0)], (steps(p), p.reply)
    assert [s.tool for s in p.steps] == ["add_text", "add_text"]


# --------------------------------------------------------------------------- 10. a logo kept on screen

def _with_logo(f: TimelineFacts) -> TimelineFacts:
    return f.with_(sticker_ids=["st1"], track_ids=[*f.track_ids, "stickers"])


@pytest.mark.parametrize("phrase", [
    "keep the logo on screen for the whole video",
    "show the logo until the end",
    "put the logo in the top right corner and keep it on screen for the whole video",
])
def test_keeping_a_logo_on_screen_retimes_the_sticker(phrase):
    p = P.plan(phrase, _with_logo(facts5()))
    assert steps(p) == [("set_clip_timing", {"clip_id": "st1", "start": 0.0, "end": 30.0})], (p.intent, steps(p), p.reply)
    assert "handle" not in (p.reply or "").lower() and not p.needs_input
    if "corner" in phrase:
        assert "cannot set" in (p.reply or ""), p.reply       # the position limitation is still said


def test_keeping_a_logo_with_no_sticker_says_so():
    p = P.plan("keep the logo on screen for the whole video", facts5())
    assert not p.steps and "no sticker" in (p.reply or "").lower(), p.reply


# --------------------------------------------------------------------------- 11. captions with a look

def test_captions_added_with_a_colour_get_that_colour():
    p = P.plan("auto captions in yellow", facts3(has_transcript=True))
    assert [s.tool for s in p.steps] == ["add_caption_track", "set_caption_style"], (steps(p), p.reply)
    assert p.steps[1].args.get("color") == P._caption_look_of("auto captions in yellow")["color"]


def test_captions_added_bold_and_big_get_the_size_and_a_bold_note():
    p = P.plan("add subtitles and make them bold and big", facts3(has_transcript=True))
    assert [s.tool for s in p.steps] == ["add_caption_track", "set_caption_style"], (steps(p), p.reply)
    assert p.steps[1].args.get("size", 0) > 96 and "bold" in (p.reply or "").lower(), (p.steps[1].args, p.reply)


# --------------------------------------------------------------------------- 12. look strength on a named clip

@pytest.mark.parametrize("phrase", [
    "make the warm look on clip 1 weaker",
    "tone down the warm look on clip 1 a lot",
    "weaken the warm look on clip 1",
    "make the warm look on the first clip weaker",
])
def test_look_strength_on_a_named_clip_steps_that_clips_look(phrase):
    f = facts3(clips=[ClipFact(id="a", track="v1", start=0, duration=4, src_in=0, src_out=4, looks=["warm.cube"],
                               look_intensity={"warm.cube": 1.0}, effects=["lut"]), *facts3().clips[1:]])
    p = P.plan(phrase, f)
    assert steps(p) == [("apply_lut", {"clip_id": "a", "src": "warm.cube", "intensity": 0.8})], (steps(p), p.reply)


def test_remove_all_looks_reads_as_a_look_family():
    assert "look" in families_of("remove all looks")
    assert "look" in families_of("take off the filters")


# --------------------------------------------------------------------------- 13. countdown and picture-in-picture

@pytest.mark.parametrize("phrase,start", [
    ("add a 3 2 1 countdown at the start of clip 2", 4.0),
    ("add a countdown", 5.5),
    ("add a 3-2-1 countdown sticker at 0:08", 8.0),
    ("countdown 3 2 1 before the squat", 5.5),
])
def test_a_countdown_is_three_one_second_cards(phrase, start):
    p = P.plan(phrase, facts3())
    got = [(s.args["text"], s.args["start"], s.args["end"], s.args.get("allow_stack")) for s in p.steps if s.tool == "add_text"]
    assert got == [("3", start, start + 1, True), ("2", start + 1, start + 2, True), ("1", start + 2, start + 3, True)], \
        (steps(p), p.reply)
    assert not p.needs_input


def test_a_picture_in_picture_request_gets_the_overlay_lane_hint():
    p = P.plan("put the kitchen after shot over the kitchen before shot as a picture in picture", facts5())
    assert not p.steps and "PIP / overlay" in (p.reply or ""), (steps(p), p.reply)
    assert not p.needs_input


# --------------------------------------------------------------------------- 14. a full disk during a step

def test_a_step_that_hits_a_full_disk_says_the_disk_is_full(tmp_path, monkeypatch, desktop_posture):
    """Final sweep 4 (robustness): the per-step failure path stringified the
    OSError, so the card read "[Errno 28] No space left on device"."""
    import errno
    import importlib
    import threading
    import prompt_fixtures as F
    from video_ai_editor.agent.prompt import executor
    D = importlib.import_module("video_ai_editor.agent.dispatch")
    store = F.make_store(tmp_path)
    facts = F.facts_for(store)

    def _full(store, args):
        raise OSError(errno.ENOSPC, "No space left on device", str(tmp_path / "cache" / "x.wav"))

    monkeypatch.setitem(D.DISPATCH, "set_speed", _full)
    events: list[dict] = []
    res = executor.run_plan(store, F.plan_of(F.step("set_speed", clip_id="$v1_first", factor=1.5)), facts,
                            emit=events.append, cancel_event=threading.Event(), prompt="speed it up",
                            validator=F.identity_validator)
    assert res.disk_full and res.error.startswith(
        f"Step 1/1 set_speed failed: {executor.DISK_FULL_TEXT.rstrip('.')}. Timeline unchanged."), res.error
    said = " ".join(str(e) for e in events)
    assert executor.DISK_FULL_TEXT in said and "Errno" not in said and str(tmp_path) not in said, events
    assert [s.status for s in res.steps] == ["failed"] and res.steps[0].error == executor.DISK_FULL_TEXT


# --------------------------------------------------------------------------- 15. everyday phrasings that only got the menu

@pytest.mark.parametrize("phrase,expect", [
    ("nuke the last clip", [("ripple_delete", {"clip_id": "c"})]),
    ("bin clip 1", [("ripple_delete", {"clip_id": "a"})]),
    ("dupe clip 3", [("duplicate_clip", {"clip_id": "c"})]),
    ("make the second clip disappear", [("ripple_delete", {"clip_id": "b"})]),
    ("make clip 2 go away", [("ripple_delete", {"clip_id": "b"})]),
    ("throw away everything after 0:10", [("cut_range", {"track": "v1", "start": 10.0, "end": 12.0})]),
    ("reverse the order", [("reorder_clips", {"track": "v1", "order": ["c", "b", "a"]})]),
    ("razor at 3.25", [("split_at", {"track": "v1", "time": 3.25})]),
    ("lop off the last half second", [("cut_range", {"track": "v1", "start": 11.5, "end": 12.0})]),
    ("trm teh frist 2 secs", [("cut_range", {"track": "v1", "start": 0.0, "end": 2.0})]),
    ("mkae clp 3 balck adn wihte", [("apply_lut", {"clip_id": "c", "src": "mono.cube", "intensity": 0.8})]),
    ("split at 00:00:07:15", [("split_at", {"track": "v1", "time": 7.5})]),
    ("make the last clip 20% brighter", [("color_grade", {"clip_id": "$v1_last", "brightness": 0.2})]),
    ("remove 00:00:06:00 to 00:00:07:15", [("cut_range", {"track": "v1", "start": 6.0, "end": 7.5})]),
    ("delete 00:00:02 - 00:00:03", [("cut_range", {"track": "v1", "start": 2.0, "end": 3.0})]),
    ("keep 3s through 9s", [("cut_range", {"track": "v1", "start": 9.0, "end": 12.0}),
                            ("cut_range", {"track": "v1", "start": 0.0, "end": 3.0})]),
    ("keep everything from 4 seconds on", [("cut_range", {"track": "v1", "start": 0.0, "end": 4.0})]),
])
def test_everyday_phrasings_plan_the_edit(phrase, expect):
    p = P.plan(phrase, facts3())
    assert steps(p) == expect, (p.intent, steps(p), p.reply)
    assert not p.needs_input


def test_rename_a_title_by_its_words_changes_the_words():
    f = facts3(texts=[TextFact(id="t1", text="Day One", role="super", start=0.0, end=3.0),
                      TextFact(id="t2", text="SALE", role="super", start=5.0, end=7.0)])
    p = P.plan("rename Day One to Day Two", f)
    assert steps(p) == [("set_text", {"clip_id": "t1", "text": "Day Two"})], (p.intent, steps(p), p.reply)


def test_back_to_widescreen_reframes_a_portrait_canvas():
    p = P.plan("back to widescreen", facts3(canvas_w=1080, canvas_h=1920, aspect="9:16"))
    assert [(s.tool, s.args.get("ratio")) for s in p.steps if s.tool == "auto_reframe"] == [("auto_reframe", "16:9")], steps(p)


def test_a_title_shown_for_the_whole_video_is_a_licensed_retime():
    from video_ai_editor.agent.prompt import contract as C
    f = facts3(texts=[TextFact(id="t1", text="Summer Trip", role="super", start=0.0, end=3.0)])
    p = P.plan("the title should show for the whole video", f)
    assert steps(p) == [("set_clip_timing", {"clip_id": "t1", "start": 0.0, "end": 12.0})], (steps(p), p.reply)
    assert C._TEXT_RETIME_RE.search("the title should show for the whole video")
    assert C._TEXT_RETIME_RE.search("keep the title on screen throughout")
