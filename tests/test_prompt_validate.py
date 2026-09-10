"""`validate_plan` — the security boundary (spec §1.3, §2.9).

Every rule has a test that constructs the offending plan and asserts it
never comes back runnable: denied tools, unknown tools and args, missing
required args, wrong types, out-of-range numbers, every free string that
could reach the filesystem or the network, every path shape a model might
author, and the seam/clip/track references. The accepting side is tested
too: sentinels, macros, canonicalised enums, the platform rules, stage
sorting, postcondition fill, and that the input plan is never mutated.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from video_ai_editor import config
from video_ai_editor.agent.prompt import recipes as R
from video_ai_editor.agent.prompt import schema as Sc
from video_ai_editor.agent.prompt import validate as V
from video_ai_editor.agent.prompt.facts import TimelineFacts
from video_ai_editor.agent.prompt.validate import PlanRejected, validate_plan


def step(tool: str, **args) -> Sc.Step:
    return Sc.Step(tool=tool, args=args, why=f"test {tool}")


def plan(*steps: Sc.Step, brain: str = "claude", **fields) -> Sc.Plan:
    return Sc.Plan.new(intent="test", brain=brain, steps=list(steps), **fields)


@pytest.fixture
def offered(tmp_path):
    """A session with one uploaded bed, and a second session whose upload is
    NOT offered to the first."""
    mine = tmp_path / "s_mine" / "uploads" / "audio" / "bed.wav"
    other = tmp_path / "s_other" / "uploads" / "audio" / "other.wav"
    for p in (mine, other):
        p.parent.mkdir(parents=True)
        p.write_bytes(b"RIFF")
    return mine, other


@pytest.fixture
def facts(offered):
    mine, _ = offered
    return TimelineFacts.minimal(v1_clip_ids=["c_v1", "c_v2"], clip_ids=["c_v1", "c_v2"], v1_boundaries=[12.0],
                                 selection="c_v2", uploads_audio=[str(mine.resolve())],
                                 allowed_paths={str(mine.resolve())})


def rejected(p, f, *needles: str) -> list[str]:
    with pytest.raises(PlanRejected) as ei:
        validate_plan(p, f)
    text = "; ".join(ei.value.reasons)
    for n in needles:
        assert n in text, (n, text)
    return ei.value.reasons


# --- rule 3: the allowlist -------------------------------------------------------

@pytest.mark.parametrize("tool", sorted(Sc.PLAN_DENY))
def test_every_denied_tool_is_refused(tool, facts):
    rejected(plan(step(tool)), facts, "denied")


def test_unknown_tool_and_the_hook_stack_are_where_the_spec_says(facts):
    rejected(plan(step("frobnicate")), facts, "unknown tool")
    assert "apply_hook_stack" in V.PLAN_TOOLS and "transcribe" in V.PLAN_TOOLS
    assert not (V.PLAN_TOOLS & Sc.PLAN_DENY)
    rejected(plan(step("apply_hook_stack", duration=3.0)), facts, "missing required arg 'text'")
    out = validate_plan(plan(step("apply_hook_stack", text="Watch this")), facts)
    assert out.steps[0].stage == Sc.STAGE_TEXT and {c.check for c in out.postconditions} == {"hook_text_starts_leq", "hook_axes_geq"}


# --- rule 5: unknown args, required, types, enums ------------------------------

def test_unknown_arg_required_arg_type_and_enum(facts):
    rejected(plan(step("add_music", src="bed.wav", gain_db=3)), facts, "unknown args ['gain_db']")
    rejected(plan(step("set_speed", clip_id="$v1_all")), facts, "missing required arg 'factor'")
    rejected(plan(step("set_speed", clip_id="$v1_all", factor="fast")), facts, "must be number")
    rejected(plan(step("set_speed", clip_id="$v1_all", factor=True)), facts, "must be number")
    rejected(plan(step("set_clip_fit", clip_id="$v1_all", fit="stretch")), facts, "must be one of")
    out = validate_plan(plan(step("set_clip_fit", clip_id="$v1_all", fit="Cover")), facts)
    assert out.steps[0].args["fit"] == "cover"                 # canonicalised on the copy
    rejected(plan(step("apply_text_template", name="end_card_handle", fields={"handle": {"nested": 1}})), facts, "scalar")
    rejected(plan(step("remove_fillers", words=["um", {"w": 1}])), facts, "items must be string")
    out = validate_plan(plan(step("set_loudness_target", lufs=None)), facts)
    assert out.steps[0].args == {}                              # None means unset, dropped


# --- rule 8: bounds and whitelists ---------------------------------------------

@pytest.mark.parametrize("s,needle", [
    (step("set_speed", clip_id="$v1_all", factor=9), "outside [0.25, 4.0]"),
    (step("add_music", src="bed.wav", volume_db=3), "outside [-40.0, 0.0]"),
    (step("set_loudness_target", lufs=-3), "outside [-24.0, -9.0]"),
    (step("auto_caption", max_chars=200), "outside [16, 60]"),
    (step("add_transition", at=12.0, type="fade", duration=5), "outside [0.1, 2.0]"),
    (step("make_shorts", target_count=50), "outside [1, 10]"),
    (step("make_shorts", max_dur=1000), "outside [5.0, 180.0]"),
    (step("apply_hook_stack", text="x", duration=30), "outside [1.0, 6.0]"),
    (step("auto_cut_to_beats", subdivision=64), "outside [1, 16]"),
    (step("tts_voiceover", text="w" * 601), "over the 600 limit"),
    (step("add_text", text="w" * 121, start=0, end=1), "over the 120 limit"),
    (step("apply_hook_stack", text="w" * 61), "over the 60 limit"),
])
def test_numeric_and_text_bounds(s, needle, facts):
    rejected(plan(s), facts, needle)


@pytest.mark.parametrize("s,needle", [
    (step("tts_voiceover", text="hi", voice="xx_XX-evil"), "not one of"),
    (step("auto_caption", model="someone/huge-repo"), "not one of"),
    (step("transcribe", model="../../model"), "not one of"),
    (step("add_text", text="hi", start=0, end=1, font="../x.ttf"), "not a bundled font"),
    (step("apply_brand_kit", handle="@a", font="/System/Library/Fonts/x.ttf"), "not a bundled font"),
    (step("translate_captions", target_lang="fr"), "not one of"),
    (step("auto_caption", target="de"), "not one of"),
    (step("auto_caption", language="en; rm -rf"), "not a language code"),
    (step("apply_show_template", name="../../etc"), "not a saved show"),
    (step("add_transition", at=12.0, type="matrix"), "must be one of"),
])
def test_free_strings_are_closed_sets(s, needle, facts):
    rejected(plan(s), facts, needle)


def test_downloads_are_allowed_only_when_the_plan_asks(facts):
    f = facts.with_(first_use={"whisper:large-v3": 3_100_000_000, "madlad": 3_000_000_000,
                               "piper:en_GB-alan-medium": 60_000_000})
    rejected(plan(step("transcribe", model="large-v3")), f, "not downloaded")
    rejected(plan(step("auto_caption", target="hi")), f, "MADLAD")
    rejected(plan(step("translate_captions", target_lang="es")), f, "MADLAD")
    rejected(plan(step("tts_voiceover", text="hi", voice="en_GB-alan-medium")), f, "not downloaded")
    dl = [Sc.DownloadNeeded(what="large-v3", bytes=3_100_000_000, tool="transcribe"),
          Sc.DownloadNeeded(what="madlad", bytes=3_000_000_000, tool="auto_caption")]
    out = validate_plan(plan(step("transcribe", model="large-v3"), step("auto_caption", target="hi"),
                             downloads_needed=dl), f)
    assert [s.tool for s in out.steps] == ["transcribe", "auto_caption"]
    assert validate_plan(plan(step("transcribe", model="small")), f).steps      # cached model, no question


# --- rule 6: paths ---------------------------------------------------------------

def test_write_paths_and_denied_path_tools(facts):
    rejected(plan(step("export_srt", path="/tmp/x.srt")), facts, "denied")
    rejected(plan(step("set_property", path="src", value="/etc/hosts")), facts, "denied")
    rejected(plan(step("add_effect", type="lut", params={"src": "/x.cube"})), facts, "denied")
    rejected(plan(step("add_sticker", emoji="🔥")), facts, "denied")


def test_luts_are_bundled_names_never_paths(facts):
    rejected(plan(step("apply_lut", clip_id="$v1_all", lut_path="/x.cube")), facts, "bundled names")
    rejected(plan(step("apply_lut", clip_id="$v1_all", src="../luts/teal_orange.cube")), facts, "bundled names")
    rejected(plan(step("apply_lut", clip_id="$v1_all", src="nope.cube")), facts, "bundled names")
    out = validate_plan(plan(step("apply_lut", clip_id="$v1_all", src="teal_orange")), facts)
    assert out.steps[0].args["src"] == "teal_orange.cube"
    out = validate_plan(plan(step("apply_lut", clip_id="$v1_all", lut_path="warm.cube", intensity=0.5)), facts)
    assert out.steps[0].args == {"clip_id": "$v1_all", "src": "warm.cube", "intensity": 0.5}


def test_real_files_the_plan_was_not_offered_are_refused(facts, offered, tmp_path):
    _, other = offered
    home_file = next((p for p in Path.home().iterdir() if p.is_file()), None)
    cases = ["/etc/passwd", "~", "../../etc/passwd", str(other), str(tmp_path), str(Path.home())]
    if home_file is not None:
        cases.append(str(home_file))
    for bad in cases:
        rejected(plan(step("add_music", src=bad)), facts, "path not offered")


def test_a_fabricated_path_becomes_a_question_never_an_arg(facts, offered):
    mine, _ = offered
    out = validate_plan(plan(step("add_music", src="/path/to/upbeat_music.mp3")), facts)
    s = out.steps[0]
    assert s.args["src"] == R.placeholder("music_src")
    q = out.blocking_questions[0]
    assert q.key == "music_src" and q.kind == "choice" and [o.value for o in q.options] == [str(mine.resolve())]
    assert "model-authored path" in (out.reply or "")
    assert "/path/to" not in repr(out.model_dump())
    # answering with the offered file passes; answering with another made-up path asks again
    ok = validate_plan(out.with_(steps=[s.model_copy(update={"args": {**s.args, "src": str(mine)}})], needs_input=[]), facts)
    assert ok.steps[0].args["src"] == str(mine.resolve()) and not ok.needs_input


def test_basename_of_an_offered_file_is_the_library_picker(facts, offered):
    mine, _ = offered
    out = validate_plan(plan(step("add_music", src="bed.wav")), facts)
    assert out.steps[0].args["src"] == str(mine.resolve()) and "resolved to the offered file" in (out.reply or "")
    out = validate_plan(plan(step("add_music", src="/somewhere/else/bed.wav")), facts)
    assert out.steps[0].args["src"] == str(mine.resolve())


def test_an_optional_path_arg_is_dropped_with_a_note(facts):
    out = validate_plan(plan(step("apply_brand_kit", handle="@a", end_card="/no/such/card.png")), facts)
    assert "end_card" not in out.steps[0].args and "dropped end_card" in (out.reply or "")


def test_no_room_to_ask_is_a_rejection(facts):
    qs = [Sc.NeedsInput(key=k, question="?", required=True) for k in ("aa", "bb", "cc", "dd")]
    rejected(plan(step("add_music", src="/path/to/x.mp3"), needs_input=qs), facts, "no room to ask")


def test_offered_paths_still_obey_the_lan_allowlist(facts):
    before = config._FORCED_RESTRICT
    config.enable_path_restriction(True)
    try:
        assert config.restrict_paths_active()
        rejected(plan(step("add_music", src=next(iter(facts.allowed_paths)))), facts, "outside the allowed roots")
    finally:
        config.enable_path_restriction(before)


# --- rule 7: references -----------------------------------------------------------

def test_clip_track_and_seam_references(facts):
    for ref in Sc.CLIP_SENTINELS:
        assert validate_plan(plan(step("noise_reduce", clip_id=ref)), facts).steps
    assert validate_plan(plan(step("noise_reduce", clip_id="c_v2")), facts).steps
    rejected(plan(step("noise_reduce", clip_id="c_nope")), facts, "not a clip on this timeline")
    rejected(plan(step("bulk_delete", clip_ids=["c_v1", "c_zzz"])), facts, "not a clip")
    rejected(plan(step("remove_silences", track="nope")), facts, "not a track")
    assert validate_plan(plan(step("set_duck", track="music", enabled=True, to_db=-18)), facts).steps
    rejected(plan(step("add_transition", at=7.0, type="fade")), facts, "not a seam")
    assert validate_plan(plan(step("add_transition", at=12.0, type="fade")), facts).steps
    assert validate_plan(plan(step("cut_range", track="v1", start=0, end=2), step("add_transition", at=7.0, type="fade")), facts).steps
    rejected(plan(step("cut_range", track="v1", start=10, end=5)), facts, "end must be after start")
    rejected(plan(step("cut_range", track="v1", start=99, end=100)), facts, "past the end")


# --- macros, defaults, platform rules, postconditions, ordering --------------------

def test_recipe_macros_expand_through_the_recipe_table(facts):
    f = facts.with_(has_transcript=True, words=10)
    out = validate_plan(plan(step("recipe:tighten"), step("apply_lut", clip_id="$v1_all", src="warm.cube")), f)
    assert [s.tool for s in out.steps] == ["remove_silences", "remove_fillers", "apply_lut"]
    assert {c.check for c in out.postconditions} >= {"speech_preserved", "fillers_remaining_leq", "effect_present"}
    rejected(plan(step("recipe:frobnicate")), f, "unknown recipe macro")
    rejected(plan(step("recipe:ask")), f, "unknown recipe macro")
    out = validate_plan(plan(step("recipe:transitions", type="glitch", at="first")), f)
    assert out.steps[0].args == {"at": 12.0, "type": "glitch", "duration": 0.3}


def test_default_filler_list_is_the_floor(facts):
    out = validate_plan(plan(step("remove_fillers", words=["like"])), facts)
    assert out.steps[0].args["words"] == [*R.FILLERS_STRICT, "like"]
    out = validate_plan(plan(step("remove_fillers")), facts)
    assert out.steps[0].args["words"] == list(R.FILLERS_STRICT)


def test_platform_to_aspect_rules(facts):
    out = validate_plan(plan(step("apply_export_preset", name="tiktok")), facts)
    assert [s.tool for s in out.steps] == ["auto_reframe", "set_clip_fit", "apply_export_preset"]
    assert out.steps[0].args == {"ratio": "9:16", "subject_track": False} and "reframe to 9:16 inserted" in (out.reply or "")
    out = validate_plan(plan(step("auto_reframe", ratio="9:16")), facts)
    assert [s.tool for s in out.steps] == ["auto_reframe", "set_clip_fit"] and out.steps[1].args["fit"] == "cover"
    out = validate_plan(plan(step("apply_export_preset", name="reels"), step("auto_reframe", ratio="16:9")), facts)
    assert next(s for s in out.steps if s.tool == "apply_export_preset").args["name"] == "youtube_16x9"
    out = validate_plan(plan(step("apply_export_preset", name="youtube_16x9")), facts)   # already 16:9
    assert [s.tool for s in out.steps] == ["apply_export_preset"]


def test_raw_plan_from_a_fake_llm_brain_gets_stages_and_real_postconditions(facts):
    raw = plan(step("add_caption_track", style="ig_chunky"), step("apply_lut", clip_id="$v1_all", src="warm.cube"),
               step("set_speed", clip_id="$v1_all", factor=1.5), brain="claude")
    assert all(s.stage is None for s in raw.steps) and raw.postconditions == []
    out = validate_plan(raw, facts)
    assert [s.tool for s in out.steps] == ["set_speed", "apply_lut", "add_caption_track"]      # stage order
    assert [s.stage for s in out.steps] == [Sc.STAGE_CUTS, Sc.STAGE_LOOK, Sc.STAGE_CAPTIONS]
    checks = [c.check for c in out.postconditions]
    assert {"speed_equals", "effect_present", "captions_nonempty"} <= set(checks) and "tool_ok" not in checks
    assert next(c for c in out.postconditions if c.check == "speed_equals").args == {"clip_id": "$v1_all", "factor": 1.5}
    assert out.brain == "claude" and out.id and out.estimated_seconds is not None and out.estimated_seconds > 0
    assert raw.model_dump(exclude={"id"}) == plan(*raw.steps, brain="claude").model_dump(exclude={"id"})   # input untouched


def test_tool_ok_only_for_tools_without_a_natural_check(facts):
    out = validate_plan(plan(step("add_marker", time=1.0, label="x")), facts)
    assert [c.check for c in out.postconditions] == ["tool_ok"]


def test_existing_postconditions_are_kept_and_not_duplicated(facts):
    pcs = [Sc.Postcondition(check="speed_equals", args={"clip_id": "$v1_all", "factor": 1.5}, human="mine")]
    out = validate_plan(plan(step("set_speed", clip_id="$v1_all", factor=1.5), postconditions=pcs), facts)
    assert [c.human for c in out.postconditions] == ["mine"]


def test_accepts_dict_input_and_keeps_ids(facts):
    p = plan(step("set_speed", clip_id="$v1_all", factor=2))
    out = validate_plan(p.model_dump(), facts)
    assert out.id == p.id and out.version == 1
    with pytest.raises(ValidationError):
        validate_plan({**p.model_dump(), "surprise": 1}, facts)
    with pytest.raises(ValidationError):
        Sc.Plan.new(intent="x", brain="claude", steps=[step("add_marker", time=1.0)] * 25)


def test_all_reasons_are_reported_together(facts):
    reasons = rejected(plan(step("frobnicate"), step("set_speed", clip_id="c_zzz", factor=99)), facts)
    assert len(reasons) == 3 and reasons[0].startswith("step 1") and all("step 2" in r for r in reasons[1:])


def test_plan_schema_for_merges_extra_args_and_extra_tools():
    assert "loop" in V.plan_schema_for("add_music")["properties"]
    assert "subject_track" in V.plan_schema_for("auto_reframe")["properties"]
    assert V.plan_schema_for("apply_hook_stack")["required"] == ["text"]
    assert V.plan_schema_for("nonexistent") is None
    assert os.path.sep not in "".join(V.EXTRA_TOOL_SCHEMAS)


# --- §3.7: the hook line is always the plan's, never generate_hook's -----------------

def test_blank_hook_text_and_a_templates_hook_stack_never_reach_generate_hook(facts):
    """`apply_hook_stack(text="")` and `apply_template(with_hook_stack=True)`
    with no `inputs.hook` both made dispatch call `generate_hook` — an
    Anthropic request outside the brain ladder whenever a key is set."""
    rejected(plan(step("apply_hook_stack", text="")), facts, "non-blank")
    rejected(plan(step("apply_hook_stack", text="   ")), facts, "non-blank")
    rejected(plan(step("apply_template", name="tech_tip", with_hook_stack=True)), facts, "needs inputs.hook")
    # The handler default (with_hook_stack absent → True) is switched OFF with a note.
    out = validate_plan(plan(step("apply_template", name="tech_tip")), facts)
    assert next(s for s in out.steps if s.tool == "apply_template").args["with_hook_stack"] is False
    assert "hook stack left off" in (out.reply or "")
    out = validate_plan(plan(step("apply_template", name="tech_tip", inputs={"hook": "Stop scrolling"})), facts)
    assert next(s for s in out.steps if s.tool == "apply_template").args.get("with_hook_stack", True) is True
    out = validate_plan(plan(step("apply_hook_stack", text="WE LOOK AT THE NEW CAMERA")), facts)
    assert out.steps[0].args["text"] == "WE LOOK AT THE NEW CAMERA"


def test_seam_sentinel_is_accepted_and_bound_refs_resolve(facts):
    out = validate_plan(plan(step("add_transition", at=Sc.SEAM_SENTINEL, type="whip", duration=0.25)), facts)
    assert out.steps[0].args["at"] == Sc.SEAM_SENTINEL
    rejected(plan(step("add_transition", at="$nope", type="whip")), facts, "must be number")
    pcs = [Sc.Postcondition(check="text_present", args={"contains": f"{Sc.ARG_REF}handle"}, human="end card")]
    out = validate_plan(plan(step("apply_brand_kit", handle="@x"), postconditions=pcs), facts)
    assert next(c for c in out.postconditions if c.check == "text_present").args["contains"] == "@x"
    # an unbound ref (arg still a placeholder) is left for the next validation, never dropped
    out = validate_plan(plan(step("apply_brand_kit", handle=R.placeholder("handle")), postconditions=pcs,
                             needs_input=[R.ask("handle", "Which handle?", kind="text")]), facts)
    assert next(c for c in out.postconditions if c.check == "text_present").args["contains"] == f"{Sc.ARG_REF}handle"


@pytest.mark.parametrize("tool", ["vocal_isolate", "instrumental_isolate", "diarize", "assign_caption_speakers", "search_media"])
def test_first_use_downloaders_without_a_probe_are_denied(tool, facts):
    """demucs / pyannote / CLIP fetch weights on first use and have no
    `facts.first_use` probe: a plan may not name them (§1.4)."""
    assert tool in Sc.PLAN_DENY and tool not in Sc.TOOL_STAGE and tool not in V.PLAN_TOOLS
    rejected(plan(step(tool, clip_id="$v1_first")), facts, "denied")
