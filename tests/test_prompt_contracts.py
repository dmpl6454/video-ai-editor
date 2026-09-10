"""The Day-0 frozen contracts of the Prompt Editor compile and agree.

Five implementers build against `agent/prompt/{schema,facts,recipes,service}`,
`agent/prompt/brains/base` and `agent/path_args` in parallel (spec §0.2), so
this file pins the shapes they share — not behaviour that is still being
written. Every assertion here is one that would otherwise surface as a
cross-owner merge surprise:

  * the Pydantic `Plan` and the hand-written wire `PLAN_JSON_SCHEMA` (the
    cloud brain's `emit_plan` input_schema) describe the same thing;
  * every tool that has a composition stage is a real DISPATCH tool (or one
    of the §4.9 additions the stage table is allowed to lead), and every
    allow-listed tool HAS a stage and a postcondition;
  * every default postcondition names a check the verifier must implement;
  * the path-arg table equals the one `tests/test_path_guards.py` derives;
  * the SSE vocabulary is exactly the six the phone knows plus the five it
    deliberately drops.
"""
from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from video_ai_editor.agent import path_args
from video_ai_editor.agent.prompt import PLAN_VERSION, facts, recipes, schema, service
from video_ai_editor.agent.prompt.brains import base as brains_base

REPO = Path(__file__).resolve().parents[1]


def _dispatch_table() -> dict:
    # `video_ai_editor.agent.dispatch` is shadowed by the function re-exported
    # from the package __init__, so go through importlib.
    return importlib.import_module("video_ai_editor.agent.dispatch").DISPATCH


def _advertised_tools() -> set[str]:
    tools = importlib.import_module("video_ai_editor.agent.tools")
    return {t["name"] for t in tools.list_tools()}


# --- 1. Plan schema ⊇ PLAN_JSON_SCHEMA ----------------------------------------

def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    """Resolve `$ref`s so the Pydantic schema can be compared structurally."""
    if isinstance(node, dict):
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            merged = {**defs[name], **{k: v for k, v in node.items() if k != "$ref"}}
            return _inline_refs(merged, defs)
        return {k: _inline_refs(v, defs) for k, v in node.items() if k != "$defs"}
    if isinstance(node, list):
        return [_inline_refs(v, defs) for v in node]
    return node


def _assert_contains(expected: Any, actual: Any, path: str = "$") -> None:
    """`expected` ⊆ `actual`: every key/constraint the wire schema states must
    be present with the same value. Lists that are sets in JSON-Schema
    (`required`, `enum`) compare order-insensitively and must be EQUAL — an
    enum the Pydantic side widened would be a loosening, not a superset."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: expected object, got {type(actual).__name__}"
        for key, value in expected.items():
            assert key in actual, f"{path}.{key}: missing from Plan.model_json_schema()"
            _assert_contains(value, actual[key], f"{path}.{key}")
        return
    if isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: expected array"
        if all(isinstance(v, (str, int, float, type(None))) for v in expected):
            assert set(map(repr, expected)) == set(map(repr, actual)), f"{path}: {expected} != {actual}"
            return
        assert len(expected) == len(actual), path
        for i, (e, a) in enumerate(zip(expected, actual)):
            _assert_contains(e, a, f"{path}[{i}]")
        return
    assert expected == actual, f"{path}: wire says {expected!r}, Plan says {actual!r}"


def test_plan_model_schema_contains_the_wire_schema():
    generated = schema.Plan.model_json_schema()
    inlined = _inline_refs(generated, generated.get("$defs", {}))
    _assert_contains(schema.PLAN_JSON_SCHEMA, inlined)


def test_wire_schema_identity_and_cloud_strip():
    assert schema.PLAN_JSON_SCHEMA["$id"] == f"vai://plan/{PLAN_VERSION}"
    assert schema.PLAN_JSON_SCHEMA["properties"]["version"] == {"const": PLAN_VERSION}
    cloud = schema.cloud_plan_input_schema()
    for name in schema.CLOUD_PLAN_STRIPPED_FIELDS:
        assert name in schema.PLAN_JSON_SCHEMA["properties"], name
        assert name not in cloud["properties"] and name not in cloud["required"], name
    assert "brain" in schema.PLAN_JSON_SCHEMA["required"]          # the router sets it; the wire requires it
    assert "brain" in schema.PLAN_JSON_SCHEMA["properties"]          # cloud strip did not mutate the constant
    assert cloud["additionalProperties"] is False and "$id" not in cloud


def test_plan_new_round_trips_and_is_frozen():
    step = schema.Step(tool="add_music", args={"src": "upbeat_120bpm.wav", "loop": True}, why="bed")
    plan = schema.Plan.new(intent="music", brain="recipes", steps=[step],
                           postconditions=schema.bind_postconditions("add_music", step.args))
    assert re.fullmatch(r"p_[0-9a-f]{8}", plan.id)
    assert schema.Plan.model_validate(plan.model_dump()) == plan
    with pytest.raises(ValidationError):
        plan.model_copy().__setattr__("intent", "x")     # frozen
    with pytest.raises(ValidationError):
        schema.Plan.model_validate({**plan.model_dump(), "surprise": 1})   # extra="forbid"
    with pytest.raises(ValidationError):
        schema.Step(tool="add_music", args={}, why="bed", stage=13)        # stage ≤ 12
    with pytest.raises(ValidationError):
        schema.Plan.model_validate({**plan.model_dump(), "brain": "gpt"})  # enum


def test_needs_input_pause_rule():
    q = schema.NeedsInput(key="target_lang", question="Which language?", required=True)
    assert q.pauses
    assert not q.model_copy(update={"default": "hi"}).pauses
    assert not q.model_copy(update={"required": False}).pauses
    plan = schema.Plan.new(intent="translate", brain="recipes", needs_input=[q])
    assert plan.blocking_questions == [q] and plan.is_read_only


def test_intent_draft_accepts_the_7b_models_shape():
    # Baseline finding 13: the shape Qwen2.5-7B emitted with schemas in the
    # prompt, plus an extra key a small model might add (tolerated here; the
    # boundary is validate_plan on the expanded Plan).
    raw = {"intents": [{"recipe": "remove_fillers", "slots": {"words": "um"}},
                       {"recipe": "reframe", "slots": {"ratio": "9:16", "extra": None}}],
           "exclusions": [], "confidence": 0.9, "reply": "ok", "thoughts": "…",
           "needs_input": [{"key": "music_src", "question": "Which track?", "options": ["upbeat"], "default": None}]}
    draft = schema.IntentDraft.model_validate(raw)
    assert [i.recipe for i in draft.intents] == ["remove_fillers", "reframe"]
    assert draft.needs_input[0].options == ["upbeat"]
    with pytest.raises(KeyError):
        recipes.from_intents(draft.model_copy(update={"intents": [schema.IntentItem(recipe="nope")]}),
                             facts.TimelineFacts.minimal())


# --- 2. stages + postconditions ------------------------------------------------

def test_every_staged_tool_exists_in_dispatch():
    missing = set(schema.TOOL_STAGE) - set(_dispatch_table())
    assert missing <= schema.PENDING_DISPATCH_TOOLS, (
        f"TOOL_STAGE names tools DISPATCH does not have: {sorted(missing - schema.PENDING_DISPATCH_TOOLS)}")


def test_pending_tools_are_removed_once_they_land():
    landed = schema.PENDING_DISPATCH_TOOLS & set(_dispatch_table())
    assert not landed, f"{sorted(landed)} landed in DISPATCH — remove them from PENDING_DISPATCH_TOOLS"


def test_every_allow_listed_tool_has_a_stage_and_no_denied_tool_does():
    allow = (_advertised_tools() | {"apply_hook_stack"}) - schema.PLAN_DENY
    assert allow <= set(schema.TOOL_STAGE), f"no stage for {sorted(allow - set(schema.TOOL_STAGE))}"
    assert not (set(schema.TOOL_STAGE) & schema.PLAN_DENY)
    assert set(schema.TOOL_STAGE.values()) <= set(schema.STAGE_NAMES)
    assert schema.TOOL_STAGE["transcribe"] == 1 and schema.TOOL_STAGE["make_shorts"] == 3
    assert schema.TOOL_STAGE["add_transition"] < schema.TOOL_STAGE["auto_reframe"] < schema.TOOL_STAGE["auto_caption"]


def test_default_postconditions_name_real_checks_and_real_tools():
    for tool, pcs in schema.DEFAULT_POSTCONDITIONS.items():
        assert tool in schema.TOOL_STAGE, tool
        assert pcs, tool
        for pc in pcs:
            spec = schema.CHECK_SPECS[pc.check]
            assert set(pc.args) <= set(spec.args), (tool, pc.check, pc.args)
            assert pc.needs_render == spec.needs_render and pc.headline == spec.headline
    natural = {"auto_reframe": {"canvas_aspect", "reframe_effective"},
               "add_music": {"music_present", "music_covers"}, "set_speed": {"speed_equals"},
               "cut_range": {"duration_between"}, "add_transition": {"transitions_count_geq"},
               "apply_lut": {"effect_present"}}
    for tool, checks in natural.items():
        assert checks <= {pc.check for pc in schema.DEFAULT_POSTCONDITIONS[tool]}, tool
    assert [pc.check for pc in schema.default_postconditions("get_timeline")] == ["tool_ok"]


def test_bind_postconditions_substitutes_step_args():
    bound = schema.bind_postconditions("auto_reframe", {"ratio": "9:16"})
    assert bound[0].args == {"ratio": "9:16"}
    unbound = schema.bind_postconditions("set_speed", {"factor": 1.5})
    assert unbound[0].args == {"factor": 1.5}            # unbound $arg:clip_id dropped, never literal
    assert all(not str(v).startswith(schema.ARG_REF) for pc in unbound for v in pc.args.values())


def test_check_specs_cover_the_verifier_table():
    """Every check named in the FIRST column of the §4.4 table exists in
    CHECK_SPECS (the verifier implements all of them — spec §4.4 'test')."""
    spec_text = (REPO / "docs/design/PROMPT_EDITOR_SPEC.md").read_text(encoding="utf-8")
    table = spec_text.split("### 4.4 Verifier", 1)[1].split("**Verify render**", 1)[0]
    named: set[str] = set()
    for line in table.splitlines():
        if not line.startswith("| `"):
            continue
        first_cell = line.split("|")[1]
        named |= set(re.findall(r"`([a-z_]{2,40})(?:\(|`)", first_cell))
    named -= set(_dispatch_table())      # a row's parenthetical may cite the TOOL that satisfies it
    assert len(named) >= 30, named
    missing = named - set(schema.CHECK_SPECS)
    assert not missing, f"§4.4 names checks CHECK_SPECS lacks: {sorted(missing)}"
    assert {"brand_watermark_present", "brand_kit_set"} <= set(schema.CHECK_SPECS)   # the table's `brand_*`
    assert schema.CHECK_SPECS["hook_axes_geq"].headline is False
    assert {n for n, s in schema.CHECK_SPECS.items() if s.needs_render} == {"silence_total_leq", "loudness_within"}


# --- 3. path args --------------------------------------------------------------

def test_path_args_table_matches_the_guard_test():
    guards = importlib.import_module("test_path_guards")
    assert path_args.PATH_ARGS == guards.EXPECTED_GUARDS
    assert len(path_args.PATH_ARGS) == path_args.PATH_ARGS_COUNT == 22
    assert path_args.path_args_for("apply_lut") == {"src": "read", "lut_path": "read"}
    assert path_args.path_args_for("add_text") == {}
    assert path_args.guarded_args("write") == {("export_ass", "path"), ("export_srt", "path"), ("export_vtt", "path")}


# --- 4. SSE + routes -----------------------------------------------------------

def test_event_types_are_the_phones_six_plus_the_five_it_drops():
    sse_ts = (REPO / "mobile/lib/sse.ts").read_text(encoding="utf-8")
    known = sse_ts.split("const KNOWN_TYPES", 1)[1].split("]", 1)[0]
    assert set(re.findall(r'"([a-z_]+)"', known)) == set(service.LEGACY_EVENT_TYPES)
    assert set(service.PROMPT_EVENT_TYPES) == {"brain", "plan", "step", "verify", "clarify"}
    assert len(set(service.EVENT_TYPES)) == 11
    assert service.via(brains_base.BRAIN_LABELS["recipes"]) == "via Recipes — "
    assert service.unknown_event_types([{"type": "plan"}, {"type": "typing"}]) == {"typing"}


def test_route_table():
    assert service.route("prompt", "s_1") == "/api/sessions/s_1/prompt"
    assert service.route("brains") == "/api/prompt/brains"
    assert service.LOOPBACK_ONLY_ROUTES <= set(service.ROUTES)
    assert all(m in {"GET", "POST"} for m, _ in service.ROUTES.values())
    assert inspect.isasyncgenfunction(service.prompt_turn) and inspect.isasyncgenfunction(service.resume)


# --- 5. facts, recipes, brains -------------------------------------------------

def test_facts_stub_and_minimal_factory(tmp_path):
    """`build_facts` is real now (P): it refuses anything that is not a store
    and, on a store, reports speech in TIMELINE seconds through `timemap` —
    cut [5,10) on v1 and a word spoken at source 11 s sits at 6 s."""
    from prompt_fixtures import WORDS, make_store, speech_clip

    f = facts.TimelineFacts.minimal(duration=24.2, aspect="9:16")
    assert f.duration == 24.2 and f.aspect == "9:16" and not f.has_speech
    with pytest.raises(ValidationError):
        f.__setattr__("duration", 1.0)
    with pytest.raises(ValueError):
        facts.build_facts(None, None)

    # Uploads live INSIDE the session dir (main.py: `session_dir(sid) / "uploads"`).
    store = make_store(tmp_path, src=speech_clip(tmp_path / "sess"), name="sess")
    dispatch = importlib.import_module("video_ai_editor.agent.dispatch").dispatch
    dispatch(store, "cut_range", {"track": "v1", "start": 5.0, "end": 10.0})
    built = facts.build_facts(store, {"playhead": 1.5}, feature_report={"unavailable": []})
    assert isinstance(built, facts.TimelineFacts) and built.has_transcript and built.playhead == 1.5
    assert built.duration == pytest.approx(7.0, abs=0.01)
    assert built.aspect == "9:16" and built.source_aspect == "16:9"   # default canvas; a 320×180 clip
    goodbye = next(w for w in WORDS if w[0] == "goodbye")           # source 11.0 s
    assert any(abs(sp.start - (goodbye[1] - 5.0)) < 0.01 for sp in built.word_spans)
    assert all(sp.end <= built.duration + 0.01 for sp in built.word_spans)   # nothing past the cut timeline
    assert built.filler_count == 2                                    # "um"×2 survive; "uh" at 5.5 s was cut
    assert built.ingest_json_path and built.ingest_json_path.endswith("ingest.json")
    assert any(p.endswith("talk.normalized.mp4") for p in built.allowed_paths)


def test_recipe_table_matches_the_grammar_intents():
    spec_intents = {"auto_edit", "captions", "translate_captions", "remove_silences", "remove_fillers",
                    "tighten", "shorts", "reframe", "music", "duck", "beat_sync", "hook", "color_look",
                    "clean_audio", "loudness", "speed", "trim", "title", "brand", "end_card",
                    "transitions", "export_preset", "voiceover", "stabilize", "upscale", "ask"}
    assert spec_intents | {"transcribe"} == set(recipes.RECIPE_NAMES)   # undo/redo are intents, not recipes
    offered = {c.name for c in recipes.cards()}
    assert "transcribe" not in offered and "ask" not in offered and "auto_edit" in offered
    for card in recipes.RECIPE_CARDS:
        d = card.as_prompt_dict()
        assert set(d["slots"].values()) <= {"enum", "number", "text"}, card.name
        assert "/" not in card.description
    assert recipes.FILLERS_STRICT == ("um", "uh", "hmm", "erm", "uhh", "umm")


def test_brain_protocol_is_satisfiable_and_results_are_vetted():
    class Fake:
        id = "recipes"

        def availability(self):
            return brains_base.available("grammar")

        def plan(self, req, *, timeout_s):
            return brains_base.BrainResult(plan=None, brain=self.id, ok=False, reason="parse")

        def text(self, task, *, timeout_s):
            return None

    assert isinstance(Fake(), brains_base.Brain)
    assert brains_base.BRAIN_IDS == ("recipes", "apple_intelligence", "local_model", "claude")
    assert set(brains_base.BRAIN_LABELS) == set(brains_base.BRAIN_IDS)
    assert brains_base.BrainResult.failure("local_model", "rejected:unknown arg").reason.startswith("rejected")
    with pytest.raises(ValueError):
        brains_base.BrainResult.failure("local_model", "meh")
    err = brains_base.BrainUnavailable("appleIntelligenceNotEnabled", "Turn on Apple Intelligence in System Settings")
    assert err.fix and str(err) == "appleIntelligenceNotEnabled"
    assert brains_base.BrainAnswer is brains_base.BrainResult
