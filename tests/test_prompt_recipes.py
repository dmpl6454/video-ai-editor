"""Recipe table + composer (spec §2.4–2.7, §2.9).

Three fact fixtures drive every test: a bare 16:9 timeline with no
transcript (`F16`), a finished-looking 9:16 one with a transcript, four
clips, captions and music (`F916`), and a 12-minute 16:9 lecture (`FLONG`).
The plans they produce use only allow-listed tools, stay stage-monotonic,
and — the one that matters — `tighten` executed on the smoke clip removes
every planted filler and no kept word, measured through `timemap`, not
assumed from the plan.
"""
from __future__ import annotations

import importlib

import pytest

from video_ai_editor.agent import timemap
from video_ai_editor.agent.prompt import planner as P
from video_ai_editor.agent.prompt import recipes as R
from video_ai_editor.agent.prompt import schema as Sc
from video_ai_editor.agent.prompt import validate as V
from video_ai_editor.agent.prompt.facts import SpeechSpan, TimelineFacts, build_facts
from video_ai_editor.agent.prompt.presets import music_beds

D = importlib.import_module("video_ai_editor.agent.dispatch")

F16 = TimelineFacts.minimal()
F916 = TimelineFacts.minimal(
    session_id="s_916", duration=30.0, canvas_w=1080, canvas_h=1920, aspect="9:16", source_aspect="9:16",
    v1_clip_ids=["c_a", "c_b", "c_c", "c_d"], clip_ids=["c_a", "c_b", "c_c", "c_d", "c_cap", "c_bed"],
    track_ids=["v1", "music", "captions"], v1_boundaries=[5.0, 12.0, 20.0], music_clip_ids=["c_bed"],
    has_transcript=True, language="en", words=60,
    speech_spans=[SpeechSpan(start=0.5, end=9.0), SpeechSpan(start=12.5, end=28.0)], speech_seconds=24.0,
    word_spans=[SpeechSpan(start=0.5 + i * 0.45, end=0.8 + i * 0.45) for i in range(60)],
    filler_count=3, has_music=True, music_ducked=True, has_captions=True, caption_style="ig_chunky",
    transcript_head="Today we look at the new camera and why it beats the old one.",
    uploads_audio=["/uploads/audio/bed.wav"], allowed_paths={"/uploads/audio/bed.wav"}, loudness_lufs=-16.0)
FLONG = TimelineFacts.minimal(session_id="s_long", duration=720.0, has_transcript=True, words=1800,
                              speech_spans=[SpeechSpan(start=1.0, end=700.0)], speech_seconds=680.0,
                              filler_count=40, transcript_head="Welcome to the lecture on graphs.")
FIXTURES = {"F16": F16, "F916": F916, "FLONG": FLONG}

BENCHMARK_PROMPTS = [
    "add captions", "add chunky captions in hinglish", "remove the silences", "cut out the ums",
    "tighten it up and add captions", "make 3 shorts under 30 seconds for tiktok", "make it vertical for reels",
    "turn this into a tiktok", "add chill background music and duck it under my voice", "cut to the beat of the music",
    "add a hook in the first 3 seconds", "give it a cinematic look", "clean up the audio and normalize to -14 LUFS",
    "speed it up 1.5x", "cut the first 5 seconds", "add a lower third for Priya Sharma @priya.codes at the start",
    "apply my brand kit @quicksolutions.in with #techtips and add an end card", "add smooth transitions between the clips",
    "make it good for youtube", "complete the video for instagram reels with hindi captions and upbeat music",
    "add a voiceover saying 'Thanks for watching' at the end", "undo that", "make it pop", "remove the ums",
]


def _tools(p: Sc.Plan) -> list[str]:
    return [s.tool for s in p.steps]


def _step(p: Sc.Plan, tool: str) -> Sc.Step:
    return next(s for s in p.steps if s.tool == tool)


# --- invariants over every benchmark prompt × fixture -------------------------

@pytest.mark.parametrize("fixture", list(FIXTURES))
@pytest.mark.parametrize("prompt", BENCHMARK_PROMPTS)
def test_plans_use_allow_listed_tools_with_monotonic_stages_and_validate(prompt, fixture):
    f = FIXTURES[fixture]
    p = P.plan(prompt, f)
    assert p.brain == "recipes" and 0.0 <= p.confidence <= 1.0
    for s in p.steps:
        assert s.tool in V.PLAN_TOOLS, s.tool
        assert s.stage is not None and 0 <= s.stage <= 12, (s.tool, s.stage)
        # The recipe table may move a tool later than its default stage (beat
        # splits after the music, auto_caption as the transcription pass) but
        # never earlier than the composition order allows.
        assert s.stage >= min(Sc.TOOL_STAGE[s.tool], Sc.STAGE_PREREQ), (s.tool, s.stage)
    stages = [s.stage for s in p.steps]
    assert stages == sorted(stages), _tools(p)
    for pc in p.postconditions:
        assert pc.check in Sc.CHECK_SPECS and set(pc.args) <= set(Sc.CHECK_SPECS[pc.check].args)
    if p.blocking_questions or p.intent in ("undo", "redo", "ask", "clarify", "noop"):
        return
    out = V.validate_plan(p, f)                      # the recipes brain never produces a rejected plan
    assert _tools(out) == _tools(p)


def test_the_cards_are_the_frozen_contract():
    names = [c.name for c in R.cards()]
    assert "transcribe" not in names and "ask" not in names and len(names) == len(R.RECIPE_NAMES) - 2
    for c in R.RECIPE_CARDS:
        for slot, kind in c.slots.items():
            assert kind in ("enum", "number", "text")
            if kind == "enum":
                assert c.slot_values[slot]
    assert set(R.RECIPE_SLOTS["transitions"]) == {"look", "type", "at", "duration"}
    assert "fadeblack" in R.RECIPE_BY_NAME["transitions"].slot_values["type"]


# --- the executed oracle: tighten cuts no kept word ---------------------------

def test_tighten_on_the_smoke_clip_removes_fillers_and_keeps_every_other_word(tmp_path):
    from prompt_fixtures import WORDS, make_store, speech_clip

    store = make_store(tmp_path, src=speech_clip(tmp_path / "sess"), name="sess")
    facts = build_facts(store, None, feature_report={"unavailable": []})
    assert facts.has_transcript and facts.filler_count == 3
    plan = V.validate_plan(P.plan("tighten it up", facts), facts)
    assert _tools(plan) == ["remove_silences", "remove_fillers"]      # transcript exists → no transcribe
    assert set(plan.steps[1].args["words"]) >= set(R.FILLERS_STRICT)
    with store.batch():
        for s in plan.steps:
            D.dispatch(store, s.tool, dict(s.args))
    edl = store.edl
    kept = [(w, s, e) for w, s, e in WORDS if w not in R.FILLERS_STRICT]
    for word, s, e in kept:
        spans = timemap.source_range_to_timeline(edl, "v1", s, e)
        assert spans, f"kept word {word!r} ({s}-{e}) was cut"
        assert sum(b - a for a, b in spans) >= (e - s) - 0.06, f"{word!r} was clipped: {spans}"
    for word, s, e in WORDS:
        if word in R.FILLERS_STRICT:
            assert not timemap.source_range_to_timeline(edl, "v1", s + 0.02, e - 0.02), f"filler {word!r} survived"
    assert edl.duration < facts.duration - 0.5          # the two silences and three fillers are gone


# --- composition rules ---------------------------------------------------------

def test_captions_default_to_the_persisted_transcript_and_never_two_transcriptions():
    p = P.plan("add captions", F916)
    assert _tools(p) == ["add_caption_track"] and p.steps[0].args["style"] == "ig_chunky"
    assert "replacing the existing captions" in (p.reply or "")
    p = P.plan("add captions", F16)                       # no transcript → transcribe first
    assert _tools(p) == ["transcribe", "add_caption_track"] and p.steps[0].stage == Sc.STAGE_PREREQ
    p = P.plan("add hindi captions", F16)                 # language change → auto_caption transcribes itself
    assert _tools(p) == ["auto_caption"] and p.steps[0].args["target"] == "hi" and p.steps[0].stage == Sc.STAGE_PREREQ
    assert any(c.check == "captions_language" and c.args["target"] == "hi" for c in p.postconditions)


def test_downloads_are_a_question_and_skip_degrades_honestly():
    f = F16.with_(first_use={"madlad": 3_000_000_000, "whisper:large-v3": 3_100_000_000})
    p = P.plan("add hindi captions", f)
    assert p.downloads_needed and p.downloads_needed[0].tool == "auto_caption"
    assert p.needs_input[0].key == "downloads" and p.needs_input[0].kind == "confirm" and p.needs_input[0].pauses
    skipped = P.apply_answers(p, {"downloads": "skip"}, f)
    assert skipped.downloads_needed == [] and skipped.steps[0].args.get("target") is None
    assert "spoken language" in (skipped.reply or "")
    assert not any(c.check == "captions_language" for c in skipped.postconditions)
    yes = P.apply_answers(p, {"downloads": "yes"}, f)
    assert yes.steps[0].args["target"] == "hi" and yes.downloads_needed


def test_transitions_precede_captions_and_relay_existing_ones():
    p = P.plan("add smooth transitions and captions", F916)
    tools = _tools(p)
    assert tools.index("add_transition") < tools.index("add_caption_track")
    assert [s.args["type"] for s in p.steps if s.tool == "add_transition"] == ["crossdissolve"] * 3
    p = P.plan("add smooth transitions", F916)            # captions exist, not requested → re-laid
    assert _tools(p)[-1] == "add_caption_track" and "re-laid" in (p.reply or "")
    assert any(c.check == "captions_sync" for c in p.postconditions)
    p = P.plan("add smooth transitions", F16)             # one clip → nothing to do, says so
    assert p.steps == [] and "no seam" in (p.reply or "")


def test_transition_catalog_intents():
    p = P.plan("smooth zoom between every clip", F916)
    assert [(s.args["at"], s.args["type"], s.args["duration"]) for s in p.steps if s.tool == "add_transition"] == [
        (5.0, "zoomin", 0.3), (12.0, "zoomin", 0.3), (20.0, "zoomin", 0.3)]     # the renderer's own default
    p = P.plan("add a barn doors open transition between every clip lasting 0.5 seconds", F916)
    assert [(s.args["type"], s.args["duration"]) for s in p.steps if s.tool == "add_transition"] == [("vertopen", 0.5)] * 3
    p = P.plan("add a glitch transition at the hook", F916)
    assert [(s.args["at"], s.args["type"], s.args["duration"]) for s in p.steps if s.tool == "add_transition"] == [(5.0, "glitch", 0.3)]
    p = P.plan("fade to black at the end", F916)
    assert _step(p, "add_transition").args == {"at": 20.0, "type": "fadeblack", "duration": 0.6}
    p = P.plan("add a 1 second dissolve at 0:12", F916)
    assert _step(p, "add_transition").args == {"at": 12.0, "type": "dissolve", "duration": 1.0}
    p = P.plan("add a wipe at 0:29", F916)               # no seam near 29 s → honest note, no step
    assert p.steps == [] and "no clip change" in (p.reply or "")
    assert any(c.check == "transitions_count_geq" and c.args.get("type") == "glitch"
               for c in P.plan("glitch between every clip", F916).postconditions)


def test_export_preset_on_a_16x9_timeline_inserts_reframe_and_fit_first():
    p = P.plan("export for reels", F16)
    assert _tools(p) == ["auto_reframe", "set_clip_fit", "apply_export_preset"]
    assert _step(p, "auto_reframe").args["ratio"] == "9:16" and _step(p, "set_clip_fit").args == {"clip_id": "$v1_all", "fit": "cover"}
    assert _step(p, "apply_export_preset").args["name"] == "reels"
    p = P.plan("export for reels", F916)                  # already 9:16 → no reframe
    assert _tools(p) == ["apply_export_preset"]
    p = P.plan("make it landscape and export for reels", F16)   # conflict: reframe wins, preset re-derived
    assert _step(p, "apply_export_preset").args["name"] == "youtube_16x9"


def test_end_card_noops_with_brand_and_brand_asks_for_an_unknown_handle():
    p = P.plan("apply my brand kit @acme and add an end card", F916)
    assert _tools(p) == ["apply_brand_kit"] and "second one" in (p.reply or "")
    assert _step(p, "apply_brand_kit").args["handle"] == "@acme"
    p = P.plan("apply my brand kit", F16)
    assert p.blocking_questions[0].key == "handle" and _step(p, "apply_brand_kit").args["handle"] == R.placeholder("handle")
    filled = P.apply_answers(p, {"handle": "@me"}, F16)
    assert _step(filled, "apply_brand_kit").args["handle"] == "@me" and not filled.blocking_questions


def test_music_loops_ducks_and_asks_when_nothing_is_offered():
    p = P.plan("add chill music", F16)                    # no beds installed, nothing uploaded → ask
    s = _step(p, "add_music")
    assert s.args["loop"] is True and s.args["duck"] is True and s.args["volume_db"] == -14.0
    assert s.args["src"] == R.placeholder("music_src") and p.blocking_questions[0].key == "music_src"
    p = P.plan("add music", F916)                         # music exists → skip with note
    assert p.steps == [] and "already on the timeline" in (p.reply or "")
    p = P.plan("add another track", F916.with_(has_music=True))
    assert _step(p, "add_music").args["src"] == "/uploads/audio/bed.wav"
    assert _step(p, "bulk_delete").args["clip_ids"] == ["c_bed"]          # the old bed goes first


def test_music_picks_the_mood_bed_when_beds_are_installed(tmp_path, monkeypatch):
    from video_ai_editor.agent.prompt import presets as Pre
    bed = tmp_path / "chill_90bpm.wav"
    bed.write_bytes(b"RIFF")
    (tmp_path / "chill_90bpm.json").write_text('{"bpm": 90, "mood": "chill", "beat_grid_offset": 0.0}')
    monkeypatch.setattr(Pre, "music_dir", lambda: tmp_path)
    beds = music_beds()
    assert beds and beds[0].mood == "chill"
    f = F16.with_(allowed_paths={str(beds[0].path)})
    p = P.plan("add chill music", f)
    assert _step(p, "add_music").args["src"] == str(beds[0].path) and not p.blocking_questions
    assert "90 BPM" in (p.reply or "")


def test_beat_sync_run_time_cuts_carry_the_min_shot_the_plan_promises():
    """With the bed already on the timeline the grid is unknown, so the plan
    falls back to `auto_cut_to_beats` — and that step must carry the same
    `min_shot` the plan's `min_shot_geq` postcondition asserts, or the plan
    fails itself (benchmark case 10 measured a 0.697 s shot)."""
    p = P.plan("cut to the beat of the music", F916)      # has_music → no precomputed grid
    s = _step(p, "auto_cut_to_beats")
    assert s.args["subdivision"] == 4 and s.args["min_shot"] == R.MIN_SHOT_S
    pc = next(c for c in p.postconditions if c.check == "min_shot_geq")
    assert pc.args["seconds"] == s.args["min_shot"]
    assert V.validate_plan(p, F916).steps                  # the schema advertises min_shot


def test_hook_always_carries_text_and_says_where_it_came_from():
    p = P.plan("add a hook", F916)
    # The strongest whole clause, openers ("today") stripped — the same line
    # brains/content.heuristic_hook_candidates would offer; never a fragment.
    assert _step(p, "apply_hook_stack").args["text"] == "WE LOOK AT THE NEW CAMERA"
    assert "heuristic" in (p.reply or "")
    p = P.plan("add a hook", F16)                         # no transcript → generic + optional question
    assert _step(p, "apply_hook_stack").args["text"] and "generic" in (p.reply or "")
    assert p.needs_input and not p.blocking_questions
    p = P.plan('add a hook saying "Stop scrolling"', F16)
    assert _step(p, "apply_hook_stack").args["text"] == "Stop scrolling" and "yours" in (p.reply or "")
    p = P.plan("add a hook", F16, hook_text=("Why cameras lie", "local_model"))
    assert _step(p, "apply_hook_stack").args["text"] == "Why cameras lie" and p.content_brain == "local_model"
    assert _step(p, "apply_hook_stack").args["duration"] == 3.0


def test_shorts_finish_only_when_a_platform_was_named():
    p = P.plan("make 3 shorts under 30 seconds for tiktok", F16)
    s = _step(p, "make_shorts")
    assert s.args == {"target_count": 3, "max_dur": 30.0, "min_dur": 12.0, "save_as_sessions": True}
    assert any(c.check == "shorts_finished" for c in p.postconditions)
    p = P.plan("make 3 shorts", F16)
    assert _step(p, "make_shorts").args["max_dur"] == 60.0
    assert not any(c.check == "shorts_finished" for c in p.postconditions)
    assert _tools(p) == ["make_shorts"]                     # no transcript prerequisite


def test_reframe_skips_when_already_the_aspect_and_asks_on_a_bare_vertical():
    assert P.plan("make it vertical", F916).steps == []
    p = P.plan("reframe it", F916)
    assert p.blocking_questions[0].key == "ratio"
    p = P.plan("make it vertical", F16)
    assert _tools(p) == ["auto_reframe", "set_clip_fit"] and _step(p, "auto_reframe").args["subject_track"] is False
    p = P.plan("make it vertical", F16.with_(tools_available={"motion_track", "auto_reframe", "set_clip_fit"}))
    assert _step(p, "auto_reframe").args["subject_track"] is True


def test_speed_trim_and_loudness_details():
    p = P.plan("speed it up 1.5x", F16)
    assert _step(p, "set_speed").args == {"clip_id": "$v1_all", "factor": 1.5}
    assert any(c.check == "duration_between" and c.args["factor"] == 1.5 for c in p.postconditions)
    p = P.plan("smooth slow motion on the first clip", F16)
    assert _tools(p) == ["set_speed", "smooth_slow_motion"] and p.steps[1].optional
    p = P.plan("cut the first 5 seconds", F16)
    assert _step(p, "cut_range").args == {"track": "v1", "start": 0.0, "end": 5.0}
    p = P.plan("clean up the audio and normalize to -14 LUFS", F16)
    assert _step(p, "noise_reduce").optional and _step(p, "set_loudness_target").args["lufs"] == -14.0
    p = P.plan("normalize it for shorts", F16)
    assert _step(p, "set_loudness_target").args["lufs"] == -14.0        # single source: the export preset table
    p = P.plan("clean up the audio and export for tiktok", F16)
    assert "set_loudness_target" not in _tools(p)                         # the preset sets it


def test_auto_edit_order_and_long_run_gate():
    p = P.plan("make it pop", FLONG)
    tools = _tools(p)
    assert tools[:2] == ["remove_silences", "remove_fillers"] and tools[-1] == "audit_aesthetic"
    assert tools.index("add_caption_track") < tools.index("apply_hook_stack") < tools.index("add_music")
    assert p.estimated_seconds and p.estimated_seconds > Sc.LONG_RUN_SECONDS
    assert p.needs_input[0].key == "go" and p.needs_input[0].pauses
    cancelled = P.apply_answers(p, {"go": "no"}, FLONG)
    assert cancelled.steps == [] and "unchanged" in (cancelled.reply or "")
    assert P.apply_answers(p, {"go": "yes"}, FLONG).steps
    p = P.plan("make it a reel without music", F16)
    assert "add_music" not in _tools(p) and "apply_export_preset" in _tools(p)
    assert any(c.check == "audit_ok" for c in p.postconditions)


def test_templates_expand_with_their_own_slots_and_exclusions():
    p = P.plan("clean up the lecture", FLONG)
    tools = _tools(p)
    assert "apply_hook_stack" not in tools and "add_music" not in tools
    assert _step(p, "add_caption_track").args["style"] == "default" and _step(p, "set_loudness_target").args["lufs"] == -14.0


def test_clarify_ask_undo_and_noop_plans():
    p = P.plan("blorp the wibble", F16)
    assert p.intent == "clarify" and p.blocking_questions[0].key == "intent" and p.steps == []
    p = P.plan("how long is the video?", F16)
    assert p.intent == "ask" and "30.0s" in (p.reply or "") and p.is_read_only
    assert P.plan("undo that", F16).intent == "undo"
    p = P.plan("no captions", F16)
    assert p.intent == "noop" and p.steps == []


# --- IntentDraft → Plan --------------------------------------------------------

def test_from_intents_normalises_the_7b_models_slot_values():
    draft = Sc.IntentDraft(intents=[
        Sc.IntentItem(recipe="shorts", slots={"count": "three", "max_dur": "30 seconds", "platform": "TikTok"}),
        Sc.IntentItem(recipe="transitions", slots={"type": "zoom", "at": "every cut"}),
        Sc.IntentItem(recipe="remove_fillers", slots={"words": "um, like"}),
        Sc.IntentItem(recipe="reframe", slots={"ratio": "vertical", "extra": "ignored"}),
    ], exclusions=["music"], confidence=0.9, reply="Here is the plan.")
    p = R.from_intents(draft, F916)
    assert _step(p, "make_shorts").args["target_count"] == 3 and _step(p, "make_shorts").args["max_dur"] == 30.0
    assert (p.reply or "").startswith("Here is the plan.")
    assert R.normalize_slots("transitions", {"type": "zoom", "at": "every cut"}) == {"type": "zoomin", "at": "all"}
    assert R.normalize_slots("remove_fillers", {"words": "um, like"}) == {"words": ("um", "like")}
    assert R.normalize_slots("reframe", {"ratio": "vertical", "bogus": 1}) == {"ratio": "9:16"}
    assert R.normalize_slots("captions", {"style": "chunky", "model_upgrade": "yes"}) == {"style": "ig_chunky", "model_upgrade": True}
    with pytest.raises(KeyError):
        R.from_intents(draft.model_copy(update={"intents": [Sc.IntentItem(recipe="frobnicate")]}), F916)
    child = R.from_intents(R.SHORTS_FINISH_DRAFT, F16.with_(has_transcript=True, transcript_head="Hi there friends."))
    assert _tools(child) == ["auto_reframe", "set_clip_fit", "add_caption_track", "apply_hook_stack"]


# --- pure helpers ----------------------------------------------------------------

def test_beat_split_times_respects_words_min_shot_and_cap():
    beats = [i * 0.5 for i in range(80)]                  # 120 BPM, 40 s
    words = [(1.0, 2.0), (7.9, 8.3), (30.0, 33.0)]
    splits = R.beat_split_times(beats, 40.0, words, subdivision=4)
    assert splits == sorted(splits) and 2 <= len(splits) <= R.MAX_BEAT_SPLITS
    assert all(R.MIN_SHOT_S <= t <= 40.0 - R.MIN_SHOT_S for t in splits)
    assert all(not (s + R.WORD_EDGE_TOLERANCE_S < t < e - R.WORD_EDGE_TOLERANCE_S) for t in splits for s, e in words)
    assert 8.0 not in splits and 32.0 not in splits
    assert R.beat_split_times(beats, 1.0, [], subdivision=4) == []
    assert R.beat_split_times([], 40.0, [], subdivision=4) == []


def test_heuristic_hook_strips_fillers_and_caps_words():
    assert R.heuristic_hook("um so today we, uh, look at the new camera. It is great") == "WE LOOK AT THE NEW CAMERA"
    assert R.heuristic_hook("Hi. Second sentence") == "HI" or R.heuristic_hook("Hi. Second sentence") is None
    assert R.heuristic_hook("um uh") is None and R.heuristic_hook("") is None


_DANGLING = {"a", "an", "the", "to", "of", "in", "on", "at", "for", "and", "or", "but", "with", "is", "are", "am"}


@pytest.mark.parametrize("head", [
    "Hey everyone, today I am taking a close look at the new camera. The first thing is the size.",
    "So basically the trick is to shoot in the morning when the light is soft and the wind is down.",
    "Welcome back. In this video I test three lenses and one of them is a mistake most people make.",
    "Um, okay so the battery lasts about four hours which is more than the old one.",
])
def test_heuristic_hook_is_a_whole_clause_never_a_fragment(head):
    """The no-model hook must not end on an article/preposition or run past
    a sentence — 'HEY EVERYONE TODAY I AM TAKING A' shipped on the benchmark."""
    hook = R.heuristic_hook(head)
    assert hook and 2 <= len(hook.split()) <= 7
    assert hook.split()[-1].lower() not in _DANGLING, hook
    assert "." not in hook.strip(".")
    sentences = [x.strip().upper() for x in head.replace("!", ".").replace("?", ".").split(".") if x.strip()]
    assert any(all(w in sent.split() for w in hook.split()) for sent in sentences), (hook, sentences)


def test_auto_edit_honours_a_target_length():
    """'make this a 30s reel': the length used to be extracted and dropped.
    Now a trim to the first 30 s runs after the tightening cuts (optional,
    on the live timeline) and `duration_leq` verifies the real length."""
    p = P.plan("make this a 20s reel with hinglish captions", F916, allow_downloads=False)
    trim = _step(p, "cut_range")
    assert trim.args["start"] == 20.0 and trim.optional and trim.stage == Sc.STAGE_STRUCTURE
    assert _tools(p).index("cut_range") > _tools(p).index("remove_fillers")     # after the cuts
    assert _tools(p).index("cut_range") < _tools(p).index("auto_caption")       # before the captions are laid
    assert any(c.check == "duration_leq" and c.args["max"] == 20.5 for c in p.postconditions)
    assert "20s" in (p.reply or "")
    V.validate_plan(p, F916)
    p = P.plan("make this a 30s reel", F16)                # 30 s timeline → nothing to trim, no note
    assert "cut_range" not in _tools(p)


def test_auto_edit_with_cuts_adds_transitions_at_the_live_seams():
    """A cutting plan moves every seam, so per-seam times cannot be planned;
    one `add_transition(at=$v1_seams)` step fans out at dispatch time. The
    old plan skipped transitions on every multi-clip auto edit."""
    p = P.plan("turn this into a tiktok", F916)
    tr = [s for s in p.steps if s.tool == "add_transition"]
    assert len(tr) == 1 and tr[0].args == {"at": Sc.SEAM_SENTINEL, "type": "crossdissolve", "duration": 0.4}
    assert tr[0].stage == Sc.STAGE_TRANSITIONS and _tools(p).index("add_transition") > _tools(p).index("remove_fillers")
    assert any(c.check == "transitions_count_geq" and c.args.get("type") == "crossdissolve" for c in p.postconditions)
    assert "transitions skipped" not in (p.reply or "")
    p = P.plan("tighten it up and add whip pans between the clips", F916)
    assert _step(p, "add_transition").args["type"] == "whip" and _step(p, "add_transition").args["at"] == Sc.SEAM_SENTINEL
    V.validate_plan(p, F916)                                # the boundary accepts the sentinel


def test_replacing_music_removes_the_old_bed_first():
    """'replace the music' used to lay the new bed ON TOP of the old one (two
    beds mixed at 0 s, `music_present clips: 2`)."""
    f = F916.with_(music_clip_ids=["c_bed1"], clip_ids=[*F916.clip_ids, "c_bed1"])
    p = P.plan("replace the music with another track", f)
    tools = _tools(p)
    assert tools.index("bulk_delete") < tools.index("add_music")
    assert _step(p, "bulk_delete").args == {"clip_ids": ["c_bed1"]}
    present = next(c for c in p.postconditions if c.check == "music_present")
    assert present.args.get("count") == 1
    assert "replacing the current music" in (p.reply or "")
    V.validate_plan(p, f)
    p = P.plan("add background music", F916)               # music exists, no "replace" → skip with note
    assert "add_music" not in _tools(p) and "already on the timeline" in (p.reply or "")


def test_brand_end_card_check_binds_to_the_answered_handle():
    """A plan that ASKED for the handle must be verified with the same three
    checks as one that had it inline — `$arg:handle` resolves on resume."""
    asked = P.plan("add my brand kit", F16)
    assert asked.blocking_questions and asked.blocking_questions[0].key == "handle"
    pc = next(c for c in asked.postconditions if c.check == "text_present")
    assert pc.args["contains"] == f"{Sc.ARG_REF}handle"
    resumed = P.apply_answers(asked, {"handle": "@acme.studio"}, F16)
    assert _step(resumed, "apply_brand_kit").args["handle"] == "@acme.studio"
    assert next(c for c in resumed.postconditions if c.check == "text_present").args["contains"] == "@acme.studio"
    inline = P.plan("apply my brand kit @acme.studio", F16)
    assert {c.check for c in inline.postconditions} == {c.check for c in resumed.postconditions}


def test_estimate_seconds_counts_models_and_tracking():
    fast = R.step_cost(Sc.Step(tool="auto_reframe", args={"ratio": "9:16", "subject_track": False}, why="x"), F16)
    slow = R.step_cost(Sc.Step(tool="auto_reframe", args={"ratio": "9:16", "subject_track": True}, why="x"), F16)
    assert fast == 1.0 < slow
    large = R.step_cost(Sc.Step(tool="transcribe", args={"model": "large-v3"}, why="x"), F16)
    small = R.step_cost(Sc.Step(tool="transcribe", args={"model": "small"}, why="x"), F16)
    assert large > small > 0 and R.estimate_seconds([], F16) == 0.0


@pytest.mark.parametrize("fixture", ["F16", "F916"])
def test_lower_third_carries_the_name_and_handle_at_the_requested_position(fixture):
    """Benchmark case 16: name + handle → one `add_lower_third` in the first
    second, verified by `text_present(role=lower_third)` and the safe zone —
    no question, because the prompt already said whose card it is."""
    p = P.plan("add a lower third for Priya Sharma @priya.codes at the start", FIXTURES[fixture])
    assert _tools(p) == ["add_lower_third"] and not p.blocking_questions
    args = _step(p, "add_lower_third").args
    assert args["name"] == "Priya Sharma" and args["handle"] == "@priya.codes"
    assert args["start"] == 0.0 and 3.0 <= args["end"] - args["start"] <= 4.0
    checks = {c.check: c.args for c in p.postconditions}
    assert checks["text_present"] == {"contains": "Priya Sharma", "role": "lower_third"}
    assert "overlays_inside_safe_zone" in checks


def test_lower_third_asks_for_the_name_only_when_neither_name_nor_handle_is_given():
    handle_only = P.plan("add a lower third @priya.codes", F16)
    assert _step(handle_only, "add_lower_third").args["name"] == "@priya.codes" and not handle_only.blocking_questions
    bare = P.plan("add a lower third", F16)
    assert [q.key for q in bare.blocking_questions] == ["name"] and bare.blocking_questions[0].kind == "text"
    assert _step(bare, "add_lower_third").args["name"] == R.placeholder("name")
    filled = P.apply_answers(bare, {"name": "Priya Sharma"}, F16)
    assert _step(filled, "add_lower_third").args["name"] == "Priya Sharma" and not filled.blocking_questions
    assert next(c for c in filled.postconditions if c.check == "text_present").args["contains"] == "Priya Sharma"
    # headline text is a title, not a person — even though NAME_RE reads "says Big Launch"
    title = P.plan("add a title that says Big Launch", F16)
    assert _tools(title) == ["add_text"] and _step(title, "add_text").args["text"] == "big launch"


def test_an_empty_answer_is_refused_once_then_the_step_is_dropped_never_asked_forever():
    """The benchmark runner answers a text question with "" — the planner
    used to keep the question unchanged and the run looped to the round cap."""
    asked = P.plan("add a title", F16)
    assert [q.key for q in asked.blocking_questions] == ["text"]
    once = P.apply_answers(asked, {"text": ""}, F16)
    assert [q.key for q in once.blocking_questions] == ["text"]
    assert once.blocking_questions[0].question.startswith("I still need the text")
    assert _tools(once) == ["add_text"]                     # nothing ran, nothing dropped yet
    twice = P.apply_answers(once, {"text": "   "}, F16)
    assert twice.steps == [] and not twice.blocking_questions
    assert "skipped title text" in twice.reply and "no text was given" in twice.reply
    # a real answer on the re-asked plan binds and clears the refusal
    answered = P.apply_answers(once, {"text": "Big Launch"}, F16)
    assert _step(answered, "add_text").args["text"] == "Big Launch" and not answered.blocking_questions
    assert not (answered.reply or "").startswith("I still need")
    # a key simply absent from the answers is asked again unchanged — not a refusal
    unanswered = P.apply_answers(asked, {}, F16)
    assert unanswered.blocking_questions[0].question == "What should the title say?"
    # and the round count is bounded whatever the client sends
    plan, rounds = asked, 0
    while plan.blocking_questions:
        plan, rounds = P.apply_answers(plan, {"text": ""}, F16), rounds + 1
    assert rounds == 2
