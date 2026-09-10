"""The Plan contract behaves the way its consumers rely on (spec §1.1).

`test_prompt_contracts.py` pins the cross-owner SHAPES (wire schema ⊆
Pydantic, stage table vs DISPATCH). This file pins BEHAVIOUR of the schema
module itself: what the models refuse, what `Plan.new`/`with_` guarantee,
how `bind_postconditions` treats unbound references, what the stage order
promises the composer, and that the `IntentDraft` shape tolerates exactly
the noise a 7B model adds and nothing structural.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from video_ai_editor.agent.prompt import schema as Sc
from video_ai_editor.agent.prompt import validate as V


def _step(tool="add_music", **args):
    return Sc.Step(tool=tool, args=args, why=f"test {tool}")


# --- Step / NeedsInput / Postcondition ------------------------------------

@pytest.mark.parametrize("tool", ["add_music", "recipe:tighten", "ab", "a" * 48])
def test_step_tool_pattern_accepts_tools_and_recipe_macros(tool):
    assert Sc.Step(tool=tool, args={}, why="x").tool == tool


@pytest.mark.parametrize("tool", ["Recipe:tighten", "add-music", "a", "recipe:", "a" * 49, "add music", "../x"])
def test_step_tool_pattern_refuses_everything_else(tool):
    with pytest.raises(ValidationError):
        Sc.Step(tool=tool, args={}, why="x")


def test_step_refuses_extra_keys_and_bad_stage_and_long_why():
    with pytest.raises(ValidationError):
        Sc.Step(tool="add_music", args={}, why="x", path="/etc/passwd")
    with pytest.raises(ValidationError):
        Sc.Step(tool="add_music", args={}, why="x", stage=-1)
    with pytest.raises(ValidationError):
        Sc.Step(tool="add_music", args={}, why="w" * 161)
    assert Sc.Step(tool="add_music", args={}, why="x").stage is None
    assert Sc.Step(tool="add_music", args={}, why="x").optional is False


@pytest.mark.parametrize("required,default,pauses", [
    (True, None, True), (True, "hi", False), (False, None, False), (False, "x", False), (True, 0, False),
])
def test_needs_input_pause_rule(required, default, pauses):
    q = Sc.NeedsInput(key="target_lang", question="Which?", required=required, default=default)
    assert q.pauses is pauses


@pytest.mark.parametrize("key", ["A", "target-lang", "x", "k" * 33, "go2"])
def test_needs_input_key_pattern(key):
    with pytest.raises(ValidationError):
        Sc.NeedsInput(key=key, question="?", required=True)


def test_needs_input_kind_enum_and_option_shape():
    with pytest.raises(ValidationError):
        Sc.NeedsInput(key="go", question="?", required=True, kind="radio")
    opt = Sc.NeedsInputOption(value="hi", label="Hindi", synonyms=["hindi"], extra="tolerated")
    q = Sc.NeedsInput(key="lang", question="?", required=True, options=[opt])
    assert q.options and q.options[0].synonyms == ["hindi"]
    with pytest.raises(ValidationError):
        Sc.NeedsInput(key="lang", question="?", required=True, options=[opt] * 13)


def test_postcondition_pattern_and_defaults():
    pc = Sc.Postcondition(check="captions_cover", args={"min_ratio": 0.9}, human="covers")
    assert pc.needs_render is False and pc.headline is True
    with pytest.raises(ValidationError):
        Sc.Postcondition(check="Captions", args={}, human="x")


# --- Plan ------------------------------------------------------------------

def test_plan_new_assigns_a_fresh_id_each_time_and_is_immutable():
    a = Sc.Plan.new(intent="x", brain="recipes")
    b = Sc.Plan.new(intent="x", brain="recipes")
    assert a.id != b.id and a.version == 1 and a.is_read_only and a.blocking_questions == []
    c = a.with_(intent="y")
    assert c.intent == "y" and a.intent == "x" and c.id == a.id
    with pytest.raises(ValidationError):
        a.with_(steps=[_step()] * 25)
    with pytest.raises(ValidationError):
        a.with_(needs_input=[Sc.NeedsInput(key=f"k{c}", question="?", required=True) for c in "abcde"])
    with pytest.raises(ValidationError):
        a.with_(confidence=1.5)


def test_plan_blocking_questions_only_count_the_ones_without_defaults():
    qs = [Sc.NeedsInput(key="downloads", question="?", required=True, kind="confirm"),
          Sc.NeedsInput(key="hook_text", question="?", required=False, default="X"),
          Sc.NeedsInput(key="range", question="?", required=True, default="first 5s")]
    p = Sc.Plan.new(intent="x", brain="recipes", needs_input=qs)
    assert [q.key for q in p.blocking_questions] == ["downloads"]


def test_cloud_schema_strips_router_owned_fields_without_mutating_the_constant():
    before = repr(Sc.PLAN_JSON_SCHEMA)
    cloud = Sc.cloud_plan_input_schema()
    assert set(cloud["properties"]) == set(Sc.PLAN_JSON_SCHEMA["properties"]) - set(Sc.CLOUD_PLAN_STRIPPED_FIELDS)
    assert "brain" not in cloud["required"] and cloud["required"] and repr(Sc.PLAN_JSON_SCHEMA) == before


# --- IntentDraft -----------------------------------------------------------

def test_intent_draft_tolerates_7b_noise_but_not_structure():
    raw = {"intents": [{"recipe": "captions", "slots": {"style": "chunky", "n": 3, "flag": True, "x": None}, "why": "…"}],
           "confidence": 0.8, "reply": "ok", "extra": 1}
    d = Sc.IntentDraft.model_validate(raw)
    assert d.intents[0].slots["n"] == 3 and d.exclusions == [] and d.needs_input == []
    with pytest.raises(ValidationError):
        Sc.IntentDraft.model_validate({"intents": [{"recipe": "Captions!"}], "confidence": 0.5})
    with pytest.raises(ValidationError):
        Sc.IntentDraft.model_validate({"intents": [], "confidence": 1.2})
    with pytest.raises(ValidationError):
        Sc.IntentDraft.model_validate({"intents": [{"recipe": "captions", "slots": {"s": [1, 2]}}], "confidence": 0.5})


# --- stages ----------------------------------------------------------------

def test_stage_order_is_the_composition_order():
    s = Sc.TOOL_STAGE
    assert s["transcribe"] < s["remove_silences"] < s["make_shorts"] < s["apply_lut"] < s["add_transition"]
    assert s["add_transition"] < s["auto_reframe"] < s["add_caption_track"] < s["apply_hook_stack"]
    assert s["apply_hook_stack"] < s["add_music"] < s["set_loudness_target"] < s["apply_export_preset"] < s["audit_aesthetic"]
    assert s["set_clip_fit"] == s["auto_reframe"] and s["get_timeline"] == 0
    assert Sc.STAGE_NAMES[Sc.STAGE_TRANSITIONS] == "transitions"


def test_every_plan_tool_has_a_stage_and_no_denied_tool_is_staged():
    assert V.PLAN_TOOLS <= set(Sc.TOOL_STAGE), sorted(V.PLAN_TOOLS - set(Sc.TOOL_STAGE))
    assert not (set(Sc.TOOL_STAGE) & Sc.PLAN_DENY)
    assert not (V.PLAN_TOOLS & Sc.PLAN_DENY)
    assert {"transcribe", "apply_hook_stack", "add_transition", "list_transitions"} <= V.PLAN_TOOLS


# --- postconditions --------------------------------------------------------

def test_default_postconditions_fall_back_to_tool_ok():
    assert [p.check for p in Sc.default_postconditions("nonexistent_tool")] == ["tool_ok"]
    assert [p.check for p in Sc.default_postconditions("add_marker")] == ["tool_ok"]
    assert Sc.default_postconditions("add_music") is not Sc.DEFAULT_POSTCONDITIONS["add_music"]   # a copy


def test_bind_postconditions_never_leaks_a_template():
    for tool in Sc.DEFAULT_POSTCONDITIONS:
        for pc in Sc.bind_postconditions(tool, {}):
            assert not any(isinstance(v, str) and v.startswith(Sc.ARG_REF) for v in pc.args.values()), (tool, pc)
    bound = Sc.bind_postconditions("make_shorts", {"target_count": 3, "max_dur": 30.0, "min_dur": 12.0})
    assert bound[0].args == {"count": 3, "max_dur": 30.0, "min_dur": 12.0}
    bound = Sc.bind_postconditions("translate_captions", {"target_lang": "hi"})
    assert bound[0].args == {"target": "hi"}


def test_check_spec_args_are_closed():
    with pytest.raises(KeyError):
        Sc._pc("captions_cover", "x", ratio=0.9)          # the arg is min_ratio
    assert Sc.CHECK_SPECS["silence_total_leq"].needs_render and Sc.CHECK_SPECS["loudness_within"].needs_render
    assert all(isinstance(spec.args, dict) for spec in Sc.CHECK_SPECS.values())
    assert Sc.CHECK_SPECS["duration_between"].args["tol"] == 0.1
