"""The Plan contract: what every brain emits and what the executor runs.

Frozen on Day-0 (spec §0.2, §1.1). Four owners compile against this file at
the same time, so nothing here may change shape without a spec revision:

  * ``PLAN_JSON_SCHEMA`` — the wire schema. It is also the `input_schema` of
    the cloud brain's forced ``emit_plan`` tool (minus the fields §1.2 strips),
    so it is written as plain JSON-Schema, not derived from Pydantic.
  * ``Plan`` / ``Step`` / ``NeedsInput`` / ``Postcondition`` — the Pydantic
    twins. ``tests/test_prompt_contracts.py`` asserts
    ``Plan.model_json_schema()`` ⊇ ``PLAN_JSON_SCHEMA`` so the two can never
    drift apart silently.
  * ``IntentDraft`` — what the on-device LLM brains emit instead of a Plan.
    ``recipes.from_intents`` expands it through the same recipe table the
    grammar uses, so an LLM plan gets stages, postconditions, prerequisites
    and idempotence rules for free (§1.1). Only the ``claude`` brain may emit
    raw tool steps.
  * ``TOOL_STAGE`` / ``DEFAULT_POSTCONDITIONS`` / ``CHECK_SPECS`` — every
    allow-listed tool has a composition stage and at least one natural
    postcondition, so "every executed plan is verified" holds for raw-step
    plans too: ``validate_plan`` fills them in for any step lacking them.

WHY ``extra="forbid"`` + ``frozen=True`` everywhere: a model-authored key the
schema does not know is exactly the thing the validator exists to refuse
(baseline finding 10: ``add_music(gain_db=…)`` was silently dropped by the
handler and shipped a −12 dB bed), and a Plan that mutates between validation
and dispatch is not a validated Plan. The executor dispatches ``dict(step.args)``
because ``dispatch._validate_tool_args`` mutates its argument in place (§1.3
rule 5); a test asserts the Plan object is unchanged after ``run_plan``.

WHY ``SkipJsonSchema[None]`` on optional string/number fields: the wire schema
says ``{"type": "string"}`` for ``title``/``hint``/``unit``; a plain
``str | None`` would emit ``anyOf [string, null]`` and break the containment
test, while a non-None default (``""``) would make "absent" and "empty" the
same value. The field still accepts and defaults to ``None`` in Python. Constraints on
such a field go on the ``str`` arm via ``Annotated[str, Field(...)]`` — put on
the whole union they neither reach the schema nor survive a ``None`` value.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import SkipJsonSchema

# --------------------------------------------------------------------------
# 1. Wire schema (spec §1.1, verbatim)
# --------------------------------------------------------------------------

BrainId = Literal["recipes", "apple_intelligence", "local_model", "claude"]
NeedsInputKind = Literal["choice", "number", "text", "duration", "path", "confirm"]

PLAN_JSON_SCHEMA: dict[str, Any] = {
    "$id": "vai://plan/1",
    "type": "object",
    "required": ["version", "intent", "steps", "needs_input", "postconditions", "confidence", "brain"],
    "additionalProperties": False,
    "properties": {
        "version": {"const": 1},
        "id": {"type": "string", "pattern": "^p_[0-9a-f]{8}$"},
        "intent": {"type": "string", "maxLength": 64},
        "title": {"type": "string", "maxLength": 80},
        "steps": {"type": "array", "maxItems": 24, "items": {
            "type": "object", "required": ["tool", "args", "why"], "additionalProperties": False,
            "properties": {
                "tool": {"type": "string", "pattern": "^(recipe:)?[a-z_]{2,48}$"},
                "args": {"type": "object"},
                "why": {"type": "string", "maxLength": 160},
                "optional": {"type": "boolean", "default": False},
                "stage": {"type": "integer", "minimum": 0, "maximum": 12}}}},
        "needs_input": {"type": "array", "maxItems": 4, "items": {
            "type": "object", "required": ["key", "question", "required"], "additionalProperties": False,
            "properties": {
                "key": {"type": "string", "pattern": "^[a-z_]{2,32}$"},
                "question": {"type": "string", "maxLength": 200},
                "kind": {"enum": ["choice", "number", "text", "duration", "path", "confirm"], "default": "choice"},
                "options": {"type": "array", "maxItems": 12, "items": {"type": "object", "required": ["value", "label"],
                            "properties": {"value": {}, "label": {"type": "string"}, "hint": {"type": "string"}, "synonyms": {"type": "array", "items": {"type": "string"}}}}},
                "default": {}, "required": {"type": "boolean"},
                "min": {"type": "number"}, "max": {"type": "number"}, "unit": {"type": "string"}}}},
        "postconditions": {"type": "array", "maxItems": 20, "items": {
            "type": "object", "required": ["check", "args", "human"], "additionalProperties": False,
            "properties": {"check": {"type": "string", "pattern": "^[a-z_]{2,40}$"}, "args": {"type": "object"},
                           "human": {"type": "string", "maxLength": 140}, "needs_render": {"type": "boolean", "default": False},
                           "headline": {"type": "boolean", "default": True}}}},
        "downloads_needed": {"type": "array", "maxItems": 6, "items": {"type": "object", "required": ["what", "bytes", "tool"],
                             "properties": {"what": {"type": "string"}, "bytes": {"type": "integer"}, "tool": {"type": "string"}}}},
        "estimated_seconds": {"type": "number"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "brain": {"enum": ["recipes", "apple_intelligence", "local_model", "claude"]},
        "content_brain": {"enum": ["recipes", "apple_intelligence", "local_model", "claude", None]},
        "reply": {"type": "string", "maxLength": 400},
    },
}

#: Fields the cloud brain must not author (§1.2): the router sets `brain`,
#: `Plan.new` sets `id`, the planner computes the last three from facts.
CLOUD_PLAN_STRIPPED_FIELDS = ("brain", "id", "content_brain", "downloads_needed", "estimated_seconds")


def cloud_plan_input_schema() -> dict[str, Any]:
    """`PLAN_JSON_SCHEMA` minus `CLOUD_PLAN_STRIPPED_FIELDS` — the `input_schema`
    of the `claude` brain's forced `emit_plan` tool (§1.2). A new dict; the
    module constant is never mutated."""
    props = {k: v for k, v in PLAN_JSON_SCHEMA["properties"].items() if k not in CLOUD_PLAN_STRIPPED_FIELDS}
    required = [k for k in PLAN_JSON_SCHEMA["required"] if k not in CLOUD_PLAN_STRIPPED_FIELDS]
    return {**{k: v for k, v in PLAN_JSON_SCHEMA.items() if k != "$id"},
            "properties": props, "required": required}

#: `args.clip_id` / `args.clip_ids` values the validator accepts and the
#: executor resolves against `store.edl` immediately before each dispatch
#: (§1.1) — resolving earlier would grade only the first fragment `cut_range`
#: leaves behind, because `_clone_clip` assigns fresh ids.
CLIP_SENTINELS = ("$v1_all", "$v1_first", "$v1_last", "$selected", "$playhead")

#: `add_transition(at=SEAM_SENTINEL)` fans out over EVERY seam of the LIVE v1
#: at dispatch time (the same rule `$v1_all` follows for clips). WHY a seam
#: sentinel and not per-seam steps: a plan that also cuts (`tighten`) moves
#: every seam before the transition stage runs, so a seam time computed at
#: plan time is wrong by construction — `auto_edit` used to skip transitions
#: on any cutting plan for exactly that reason, shipping hard cuts on every
#: multi-clip project. The executor reads `store.edl` after the cuts instead.
SEAM_SENTINEL = "$v1_seams"

#: Duration gate (§1.1): above this the planner appends the `go` confirm.
LONG_RUN_SECONDS = 90.0

_FROZEN = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------
# 2. Pydantic twins
# --------------------------------------------------------------------------

class Step(BaseModel):
    model_config = _FROZEN

    tool: str = Field(pattern=r"^(recipe:)?[a-z_]{2,48}$")
    args: dict[str, Any]
    why: str = Field(max_length=160)
    optional: bool = False
    stage: Annotated[int, Field(ge=0, le=12)] | SkipJsonSchema[None] = None


class NeedsInputOption(BaseModel):
    # No `extra="forbid"` here on purpose — the wire schema does not forbid
    # extra keys on options (it is the one object without
    # `additionalProperties: false`), and slot tables may grow hint fields.
    model_config = ConfigDict(frozen=True)

    value: Any
    label: str
    hint: str | SkipJsonSchema[None] = None
    synonyms: list[str] | SkipJsonSchema[None] = None


class NeedsInput(BaseModel):
    model_config = _FROZEN

    key: str = Field(pattern=r"^[a-z_]{2,32}$")
    question: str = Field(max_length=200)
    kind: NeedsInputKind = "choice"
    options: Annotated[list[NeedsInputOption], Field(max_length=12)] | SkipJsonSchema[None] = None
    default: Any = None
    required: bool
    min: float | SkipJsonSchema[None] = None
    max: float | SkipJsonSchema[None] = None
    unit: str | SkipJsonSchema[None] = None

    @property
    def pauses(self) -> bool:
        """§1.1: `required && default is None` pauses the run; anything with a
        default is pre-filled and executes immediately."""
        return self.required and self.default is None


class Postcondition(BaseModel):
    model_config = _FROZEN

    check: str = Field(pattern=r"^[a-z_]{2,40}$")
    args: dict[str, Any]
    human: str = Field(max_length=140)
    needs_render: bool = False
    headline: bool = True


class DownloadNeeded(BaseModel):
    model_config = ConfigDict(frozen=True)

    what: str
    bytes: int
    tool: str


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True,
                              json_schema_extra={"$id": "vai://plan/1"})

    version: Literal[1]
    id: Annotated[str, Field(pattern=r"^p_[0-9a-f]{8}$")] | SkipJsonSchema[None] = None
    intent: str = Field(max_length=64)
    title: Annotated[str, Field(max_length=80)] | SkipJsonSchema[None] = None
    steps: list[Step] = Field(max_length=24)
    needs_input: list[NeedsInput] = Field(max_length=4)
    postconditions: list[Postcondition] = Field(max_length=20)
    downloads_needed: list[DownloadNeeded] = Field(default_factory=list, max_length=6)
    estimated_seconds: float | SkipJsonSchema[None] = None
    confidence: float = Field(ge=0, le=1)
    brain: BrainId
    content_brain: Literal["recipes", "apple_intelligence", "local_model", "claude", None] = None
    reply: Annotated[str, Field(max_length=400)] | SkipJsonSchema[None] = None

    @staticmethod
    def new_id() -> str:
        return f"p_{uuid4().hex[:8]}"

    @classmethod
    def new(cls, *, intent: str, brain: str, steps: list[Step] | tuple[Step, ...] = (),
            needs_input: list[NeedsInput] | tuple[NeedsInput, ...] = (),
            postconditions: list[Postcondition] | tuple[Postcondition, ...] = (),
            confidence: float = 1.0, **fields: Any) -> "Plan":
        """Build a v1 Plan with a fresh id. The only constructor the planner,
        the recipe expander and tests should use — `Plan(...)` directly is for
        `model_validate` of wire data."""
        return cls(version=1, id=cls.new_id(), intent=intent, brain=brain,
                   steps=list(steps), needs_input=list(needs_input),
                   postconditions=list(postconditions), confidence=confidence, **fields)

    def with_(self, **changes: Any) -> "Plan":
        """Immutable update; validates the result (a `model_copy` would not)."""
        return Plan.model_validate({**self.model_dump(), **changes})

    @property
    def blocking_questions(self) -> list[NeedsInput]:
        return [q for q in self.needs_input if q.pauses]

    @property
    def is_read_only(self) -> bool:
        return not self.steps


# --------------------------------------------------------------------------
# 3. IntentDraft — what the on-device brains emit (§1.1, §3.1)
# --------------------------------------------------------------------------

SlotValue = str | float | int | bool | None


class IntentItem(BaseModel):
    # WHY tolerant (`extra="ignore"`) where Plan is strict: this is raw model
    # output on its way INTO the recipe table, not a plan on its way to a
    # tool. Unknown slots are dropped by `recipes.from_intents` (it only reads
    # the slot names a recipe declares) and unknown recipes make it raise —
    # the security boundary is `validate_plan` on the expanded Plan, so being
    # strict here would only turn a usable 7B answer into a fall-through.
    model_config = ConfigDict(extra="ignore", frozen=True)

    recipe: str = Field(pattern=r"^[a-z_]{2,32}$")
    slots: dict[str, SlotValue] = Field(default_factory=dict)


class DraftQuestion(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    key: str
    question: str
    options: list[str] = Field(default_factory=list)
    default: SlotValue = None


class IntentDraft(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    intents: list[IntentItem]
    exclusions: list[str] = Field(default_factory=list)
    needs_input: list[DraftQuestion] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    reply: str = ""


# --------------------------------------------------------------------------
# 4. Composition stages (§2.5)
# --------------------------------------------------------------------------

STAGE_INSPECT, STAGE_PREREQ, STAGE_CUTS, STAGE_STRUCTURE, STAGE_LOOK = 0, 1, 2, 3, 4
STAGE_TRANSITIONS, STAGE_REFRAME, STAGE_CAPTIONS, STAGE_TEXT = 5, 6, 7, 8
STAGE_MUSIC, STAGE_AUDIO, STAGE_EXPORT, STAGE_AUDIT = 9, 10, 11, 12

STAGE_NAMES: dict[int, str] = {
    0: "inspection", 1: "prerequisites", 2: "cuts", 3: "structure", 4: "look",
    5: "transitions", 6: "reframe", 7: "captions", 8: "text", 9: "music",
    10: "audio", 11: "export preset", 12: "audit",
}

#: Tools a plan may never contain (§1.3 rule 3), with the verified reasons:
#: `set_property(path="src")` bypasses `_safe_src` on the desktop; `add_clip`
#: sources media plans must not; `add_sticker` fetches from a CDN;
#: `add_effect(params.src)` carries a path; `undo/redo` are their own intent;
#: exports write outside the session; the rest are interactive or repair
#: tools. Lives here (not only in validate.py) so TOOL_STAGE coverage —
#: "every allow-listed tool has a stage" — is checkable from the contract.
#:
#: The second group DOWNLOADS ON FIRST USE with no cache probe in
#: `facts.first_use` (§1.4 "no network without a yes"): `vocal_isolate` /
#: `instrumental_isolate` fetch demucs `htdemucs` (~80 MB, torch hub) inside
#: `ai/separate.py`; `diarize` / `assign_caption_speakers` run
#: `Pipeline.from_pretrained` against the HF Hub (and need a token);
#: `search_media` pulls the CLIP weights once. Until each has a `Path.exists`
#: probe and a `downloads_needed` entry like whisper/Piper/MADLAD, a plan may
#: not name them — the chat tools still can, where the user is in the loop.
PLAN_DENY: frozenset[str] = frozenset({
    "undo", "redo", "repair_media_paths", "repair_chunks", "save_show_template",
    "record_voiceover", "import_srt", "export_srt", "export_vtt", "export_ass",
    "multicam", "find_broll", "object_erase", "motion_track", "remove_background",
    "set_property", "add_clip", "add_sticker", "add_effect", "pyannote_status",
    "list_shows",
    "vocal_isolate", "instrumental_isolate", "diarize", "assign_caption_speakers", "search_media",
})

#: §4.9 tools X adds to DISPATCH after BASE. They need a stage today because
#: P's `transcribe` recipe emits them; the contracts test lets TOOL_STAGE lead
#: DISPATCH by exactly this set. Delete entries as they land — `transcribe`
#: landed with `dispatch.transcribe_tool`, so the set is empty.
PENDING_DISPATCH_TOOLS: frozenset[str] = frozenset()

TOOL_STAGE: dict[str, int] = {
    # 0 — inspection / read-only
    "get_timeline": 0, "get_clip": 0, "get_transcript": 0, "check_features": 0,
    "list_filters": 0, "list_luts": 0, "list_templates": 0, "list_text_styles": 0,
    "list_transitions": 0, "find_moments": 0,
    "set_track_locked": 0, "add_marker": 0, "remove_marker": 0,
    # 1 — prerequisites (transcript-producing / transcript-annotating)
    "transcribe": 1, "name_speakers": 1,
    # 2 — cuts (change the v1 timeline; everything later measures the result)
    "cut_range": 2, "ripple_delete": 2, "remove_silences": 2, "remove_fillers": 2,
    "set_speed": 2, "smooth_slow_motion": 2, "stabilize": 2, "upscale": 2,
    "trim_clip": 2, "split_at": 2, "set_clip_timing": 2, "move_clip": 2,
    "reorder_clips": 2, "bulk_delete": 2, "bulk_duplicate": 2, "duplicate_clip": 2,
    # 3 — structure (terminal for the parent plan)
    "make_shorts": 3,
    # 4 — look (per-clip picture; templates are composites applied first)
    "apply_lut": 4, "color_grade": 4, "match_style": 4, "chroma_key": 4,
    "add_mask": 4, "remove_mask": 4, "remove_effect": 4, "set_clip_transform": 4,
    "add_keyframe": 4, "remove_keyframe": 4, "set_clip_z": 4,
    "apply_template": 4, "apply_show_template": 4,
    # 5 — transitions (BEFORE reframe/captions/text: they shorten the timeline)
    "add_transition": 5, "remove_transition": 5, "set_video_fade": 5,
    # 6 — reframe (+ fit fallback, canvas)
    "auto_reframe": 6, "set_clip_fit": 6, "set_canvas": 6, "set_aspect_ratio": 6,
    "set_pip_framing": 6,
    # 7 — captions / translate
    "auto_caption": 7, "add_caption_track": 7, "translate_captions": 7,
    # 8 — text (hook, title, brand, end card, voiceover)
    "apply_hook_stack": 8, "add_hook_overlay": 8, "generate_hook": 8,
    "add_text": 8, "add_super_text": 8, "add_lower_third": 8,
    "apply_text_template": 8, "apply_brand_kit": 8, "tts_voiceover": 8,
    # 9 — music (add, duck, beats)
    "add_music": 9, "set_duck": 9, "auto_cut_to_beats": 9,
    # 10 — audio (noise, loudness, levels)
    "noise_reduce": 10, "set_loudness_target": 10, "set_volume": 10,
    "set_clip_muted": 10, "set_track_muted": 10, "add_fade": 10,
    # 11 — export preset (+ explicit loudness after it)
    "apply_export_preset": 11,
    # 12 — audit / final inspection
    "audit_aesthetic": 12, "render_preview": 12,
}


# --------------------------------------------------------------------------
# 5. Check specs (§4.4) — the names the verifier must implement
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CheckSpec:
    """One verifier check: its argument names with defaults, whether it needs
    the shared verify render, and whether it counts toward the parity
    headline. `args` lists EVERY key the verifier may read so a recipe cannot
    invent one; `None` means "no default — required or derived from facts"."""
    name: str
    args: dict[str, Any]
    human: str
    needs_render: bool = False
    headline: bool = True


def _spec(name: str, human: str, /, needs_render: bool = False, headline: bool = True,
          **args: Any) -> CheckSpec:
    return CheckSpec(name=name, args=dict(args), human=human,
                     needs_render=needs_render, headline=headline)


CHECK_SPECS: dict[str, CheckSpec] = {s.name: s for s in (
    # transcript / captions (all TIMELINE seconds via timemap)
    _spec("transcript_present", "a transcript exists"),
    _spec("captions_nonempty", "captions were laid"),
    _spec("captions_cover", "captions cover the speech", min_ratio=0.9),
    _spec("captions_within_extent", "no caption runs past the video"),
    _spec("captions_sync", "captions stay in sync across cuts", tol=0.1),
    _spec("captions_language", "captions are in the requested language", target=None),
    _spec("captions_style", "captions use the requested style", style=None),
    _spec("speech_preserved", "no kept word was cut"),
    _spec("fillers_remaining_leq", "filler words are gone", words=None, max=0),
    # duration
    _spec("duration_shrank", "the video got shorter", min_ratio=None, min_seconds=None),
    # `target` (absolute seconds) OR `start`+`end` (a removed range: expected =
    # duration_before − (end − start)) OR `factor` (speed: before / factor).
    _spec("duration_between", "the duration matches", target=None, start=None, end=None,
          factor=None, tol=0.1, tol_ratio=None),
    _spec("duration_leq", "the video is short enough", max=None),
    # Long pauses only (≥ the plan's min_dur + 2×keep_pad + 0.15 s): the kept
    # air remove_silences leaves on purpose and natural breaths do not count.
    _spec("silence_total_leq", "no long pauses remain", max_total_s=1.0, needs_render=True),
    # framing
    _spec("canvas_aspect", "the canvas has the requested aspect", ratio=None),
    _spec("reframe_effective", "the reframe changed the picture"),
    _spec("no_letterbox", "no black bars"),
    _spec("overlays_inside_safe_zone", "text stays clear of the platform UI", ratio=None),
    # music / beats
    _spec("music_present", "music is on the timeline", ducked=None, count=None),
    _spec("music_ducked", "music ducks under speech", to_db=-12),
    _spec("music_within_video_extent", "music does not outlast the video"),
    _spec("music_covers", "music runs under the whole video", min_ratio=0.95),
    _spec("beat_splits_geq", "cuts were placed on beats", n=2),
    _spec("min_shot_geq", "no shot is too short", seconds=0.8),
    _spec("beat_pulse_present", "punch-ins land on beats", n=2),
    # hook / text / brand
    _spec("hook_text_starts_leq", "the hook starts immediately", t=0.5),
    _spec("hook_axes_geq", "the hook works on several axes", n=3, headline=False),
    _spec("text_present", "the text is on screen", contains=None, start_geq=None, role=None),
    _spec("brand_watermark_present", "the brand watermark is placed"),
    _spec("brand_kit_set", "the brand kit is recorded"),
    _spec("vo_present", "the voiceover is on the timeline"),
    # effects / clips
    _spec("effect_present", "the effect is applied", type=None, track="v1", all=True),
    _spec("clip_src_changed", "the clip was re-rendered", clip_id=None),
    _spec("speed_equals", "the speed matches", clip_id=None, factor=None),
    _spec("transitions_count_geq", "transitions were added", n=1, type=None),
    _spec("export_preset_applied", "the export preset is set", name=None),
    # audio
    _spec("loudness_target_set", "the loudness target is recorded", lufs=None),
    _spec("loudness_within", "the render hits the loudness target", tol=1.0, needs_render=True),
    # structure
    _spec("shorts_created", "the shorts were created", count=None, max_dur=None, min_dur=None),
    _spec("shorts_finished", "each short has captions, a hook and a 9:16 canvas"),
    _spec("audit_ok", "the aesthetic audit passes"),
    # the fallback: the step ran without raising
    _spec("tool_ok", "the step completed", tool=None),
)}


# --------------------------------------------------------------------------
# 6. Default postconditions per tool (§1.1)
# --------------------------------------------------------------------------

#: Prefix of a template value that `bind_postconditions` replaces with the
#: step's own argument of that name — `"$arg:ratio"` → `step.args["ratio"]`.
#: Unbound references are dropped so the check falls back to its own default.
ARG_REF = "$arg:"


def _pc(check: str, human: str, /, **args: Any) -> Postcondition:
    spec = CHECK_SPECS[check]
    unknown = set(args) - set(spec.args)
    if unknown:
        raise KeyError(f"{check}: unknown postcondition args {sorted(unknown)}")
    return Postcondition(check=check, args=args, human=human,
                         needs_render=spec.needs_render, headline=spec.headline)


_TOOL_OK = [_pc("tool_ok", "the step completed")]

DEFAULT_POSTCONDITIONS: dict[str, list[Postcondition]] = {
    "transcribe": [_pc("transcript_present", "a transcript exists")],
    "auto_caption": [_pc("captions_nonempty", "captions were laid"),
                     _pc("captions_within_extent", "no caption runs past the video"),
                     _pc("captions_cover", "captions cover the speech", min_ratio=0.9)],
    "add_caption_track": [_pc("captions_nonempty", "captions were laid"),
                          _pc("captions_within_extent", "no caption runs past the video"),
                          _pc("captions_cover", "captions cover the speech", min_ratio=0.9)],
    "translate_captions": [_pc("captions_language", "captions are in the requested language",
                               target=f"{ARG_REF}target_lang")],
    "remove_silences": [_pc("speech_preserved", "no kept word was cut")],
    "remove_fillers": [_pc("fillers_remaining_leq", "filler words are gone",
                           words=f"{ARG_REF}words", max=0),
                       _pc("speech_preserved", "no kept word was cut")],
    "cut_range": [_pc("duration_between", "the cut range is gone",
                      start=f"{ARG_REF}start", end=f"{ARG_REF}end", tol=0.1)],
    "set_speed": [_pc("speed_equals", "the speed matches",
                      clip_id=f"{ARG_REF}clip_id", factor=f"{ARG_REF}factor")],
    "add_transition": [_pc("transitions_count_geq", "transitions were added", n=1)],
    "apply_lut": [_pc("effect_present", "the look is applied", type="lut", track="v1")],
    "auto_reframe": [_pc("canvas_aspect", "the canvas has the requested aspect",
                         ratio=f"{ARG_REF}ratio"),
                     _pc("reframe_effective", "the reframe changed the picture")],
    "set_clip_fit": [_pc("no_letterbox", "no black bars")],
    "set_aspect_ratio": [_pc("canvas_aspect", "the canvas has the requested aspect",
                             ratio=f"{ARG_REF}ratio")],
    "apply_export_preset": [_pc("export_preset_applied", "the export preset is set",
                                name=f"{ARG_REF}name"),
                            _pc("no_letterbox", "no black bars")],
    "add_music": [_pc("music_present", "music is on the timeline"),
                  _pc("music_covers", "music runs under the whole video", min_ratio=0.95),
                  _pc("music_within_video_extent", "music does not outlast the video")],
    "set_duck": [_pc("music_ducked", "music ducks under speech", to_db=f"{ARG_REF}to_db")],
    "auto_cut_to_beats": [_pc("beat_splits_geq", "cuts were placed on beats", n=2),
                          _pc("min_shot_geq", "no shot is too short", seconds=0.8)],
    "apply_hook_stack": [_pc("hook_text_starts_leq", "the hook starts immediately", t=0.5),
                         _pc("hook_axes_geq", "the hook works on several axes", n=3)],
    "add_hook_overlay": [_pc("hook_text_starts_leq", "the hook starts immediately", t=0.5)],
    "add_text": [_pc("text_present", "the text is on screen", contains=f"{ARG_REF}text"),
                 _pc("overlays_inside_safe_zone", "text stays clear of the platform UI")],
    "add_super_text": [_pc("text_present", "the text is on screen"),
                       _pc("overlays_inside_safe_zone", "text stays clear of the platform UI")],
    "apply_text_template": [_pc("text_present", "the text is on screen"),
                            _pc("overlays_inside_safe_zone", "text stays clear of the platform UI")],
    "add_lower_third": [_pc("text_present", "the lower third is on screen",
                            contains=f"{ARG_REF}name"),
                        _pc("overlays_inside_safe_zone", "text stays clear of the platform UI")],
    "apply_brand_kit": [_pc("brand_watermark_present", "the brand watermark is placed"),
                        _pc("brand_kit_set", "the brand kit is recorded")],
    "tts_voiceover": [_pc("vo_present", "the voiceover is on the timeline")],
    "noise_reduce": [_pc("clip_src_changed", "the audio was cleaned", clip_id=f"{ARG_REF}clip_id")],
    "stabilize": [_pc("clip_src_changed", "the clip was stabilized", clip_id=f"{ARG_REF}clip_id")],
    "upscale": [_pc("clip_src_changed", "the clip was upscaled", clip_id=f"{ARG_REF}clip_id")],
    "smooth_slow_motion": [_pc("clip_src_changed", "the slow motion was interpolated",
                               clip_id=f"{ARG_REF}clip_id")],
    "set_loudness_target": [_pc("loudness_target_set", "the loudness target is recorded",
                                lufs=f"{ARG_REF}lufs")],
    "make_shorts": [_pc("shorts_created", "the shorts were created", count=f"{ARG_REF}target_count",
                        max_dur=f"{ARG_REF}max_dur", min_dur=f"{ARG_REF}min_dur")],
    "audit_aesthetic": [_pc("audit_ok", "the aesthetic audit passes")],
}


def default_postconditions(tool: str) -> list[Postcondition]:
    """Natural postconditions for `tool`; `tool_ok` for anything without one."""
    return list(DEFAULT_POSTCONDITIONS.get(tool) or _TOOL_OK)


def bind_postconditions(tool: str, args: dict[str, Any]) -> list[Postcondition]:
    """`default_postconditions(tool)` with every `$arg:<name>` template replaced
    by the step's own value. A reference the step does not supply is dropped
    so the check uses its CHECK_SPECS default — never a literal `"$arg:…"`."""
    bound: list[Postcondition] = []
    for pc in default_postconditions(tool):
        new_args: dict[str, Any] = {}
        for key, value in pc.args.items():
            if isinstance(value, str) and value.startswith(ARG_REF):
                name = value[len(ARG_REF):]
                if name in args:
                    new_args[key] = args[name]
                continue
            new_args[key] = value
        bound.append(pc.model_copy(update={"args": new_args}))
    return bound


__all__ = [
    "PLAN_JSON_SCHEMA", "CLOUD_PLAN_STRIPPED_FIELDS", "cloud_plan_input_schema", "CLIP_SENTINELS", "SEAM_SENTINEL", "LONG_RUN_SECONDS",
    "BrainId", "NeedsInputKind", "SlotValue",
    "Step", "NeedsInputOption", "NeedsInput", "Postcondition", "DownloadNeeded", "Plan",
    "IntentItem", "DraftQuestion", "IntentDraft",
    "STAGE_NAMES", "PLAN_DENY", "PENDING_DISPATCH_TOOLS", "TOOL_STAGE",
    "CheckSpec", "CHECK_SPECS", "ARG_REF", "DEFAULT_POSTCONDITIONS",
    "default_postconditions", "bind_postconditions",
]
