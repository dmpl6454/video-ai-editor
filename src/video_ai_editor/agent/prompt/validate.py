"""`validate_plan` — THE security boundary of the Prompt Editor (spec §1.3).

Every plan, from every brain, passes through here before the executor
dispatches a single step; X calls it again inside the session lock. The
rules, in the order they run:

   1. `Plan.model_validate` (`extra="forbid"`) — an unknown key is refused.
   2. `recipe:<name>` macros expand through `planner.compose`, so a macro
      gets stages, prerequisites, postconditions and questions exactly as a
      grammar hit would; an unknown macro is refused.
   3. Tool allowlist — `PLAN_TOOLS` = advertised tools ∪ `EXTRA_TOOL_SCHEMAS`
      − `PLAN_DENY` (the verified reasons live on `schema.PLAN_DENY`).
   4. Schema — `tools.input_schema_for` + `EXTRA_TOOL_SCHEMAS` + `EXTRA_ARGS`.
   5. Unknown arg → refuse (finding 8: handlers silently ignored `gain_db`);
      required args present; types via `dispatch._arg_type_ok`; enums
      canonicalised case-insensitively. All on a COPY of the args.
   6. Path rule (`agent/path_args.PATH_ARGS`): a `write` path is refused
      outright; a `read` path must be an exact member of
      `facts.allowed_paths` after `Path.resolve()`. LUTs travel as bundled
      names, never as paths.
   7. `clip_id` / `clip_ids` / `track` must exist in the facts or be a
      sentinel; a transition must sit on a real seam.
   8. `ARG_BOUNDS` + enum whitelists for every free string that could reach
      the filesystem or the network: whisper models, Piper voices, fonts,
      caption targets, template and preset names, transition names, LUTs.
      A model, voice or translation model that is NOT on disk is accepted
      only when the plan already lists it in `downloads_needed` — the
      question the user must answer first (§1.4: no network without a yes).
   9. `len(steps) ≤ 24`; `estimated_seconds = Σ step_cost`.
  10. A NEW Plan, stage-sorted, with an id, every step staged and every
      tool covered by at least one natural postcondition.

WHY a model-authored path becomes a QUESTION and not always a rejection
(finding 13): the 7B model, asked for background music, wrote
`"/path/to/upbeat_music.mp3"` — a guess, not an attack — and the right
answer is the library picker, not a fall-through to a worse brain. But
`/etc/passwd`, `~/.ssh/id_rsa` and a WORKDIR file from another session are
real files the plan was not offered, and a plan that points at a real file
outside its allowlist is refused: the difference between the two is one
`Path.exists()`. Either way the string never reaches a tool.

WHY `remove_fillers.words` is unioned with `FILLERS_STRICT`: the 7B model
sent `["um"]` only (finding 13); the handler matches single tokens, so the
default list is the safe floor, and the user's quoted extras ride on top.

WHY platform → aspect rules live here and not only in the recipe table:
`apply_export_preset` changes the canvas without cropping clips or
rescaling overlays [verified in the handler], so a raw `claude` plan that
says "tiktok preset" on a 16:9 timeline would letterbox. The validator
inserts the same `auto_reframe` + `set_clip_fit(cover)` pair the recipe
uses, and gives every bare `auto_reframe` its `cover` fallback, so
`no_letterbox` is measured against the same shape whichever brain planned.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any

from ... import config
from .. import path_args as _path_args
from .. import tools as _tools
from . import slots as S
from .facts import TimelineFacts, VOICE_IDS, WHISPER_MODELS
from .presets import font_names, lut_names, music_beds, show_template_names
from .recipes import (ASK, FILLERS_STRICT, RECIPE_BY_NAME, Intent, ask, estimate_seconds,
                      normalize_slots, placeholder)
from .schema import (ARG_REF, CLIP_SENTINELS, PLAN_DENY, SEAM_SENTINEL, STAGE_AUDIT, STAGE_CUTS, STAGE_REFRAME,
                     TOOL_STAGE, NeedsInput, Plan, Postcondition, Step, bind_postconditions)

_D = importlib.import_module("video_ai_editor.agent.dispatch")


class PlanRejected(ValueError):
    """The plan may not run. `.reasons` lists every rule it broke — all of
    them, not the first, so a brain (or a reviewer) sees the whole picture."""

    def __init__(self, reasons: list[str] | str):
        self.reasons = [reasons] if isinstance(reasons, str) else list(reasons)
        super().__init__("; ".join(self.reasons))


# --------------------------------------------------------------------------
# 1. Tables (spec §1.3 rules 3, 4, 8)
# --------------------------------------------------------------------------

#: Tools a plan may use that `tools.list_tools()` does not advertise.
#: `apply_hook_stack` has NO schema anywhere in tools.py, so
#: `dispatch._validate_tool_args` passes it through unvalidated — this is the
#: only schema it has. `text` is required in plans (§3.7): without it the
#: handler calls `generate_hook`, which calls Anthropic whenever a key is set.
EXTRA_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "apply_hook_stack": {"type": "object", "properties": {
        "text": {"type": "string"}, "duration": {"type": "number"},
        "visual": {"type": "string", "enum": ["punch_in", "ken_burns", "none"]},
        "audio": {"type": "string", "enum": ["fade_boost", "none"]}}, "required": ["text"]},
}

#: Args beyond a tool's advertised schema that plans may carry (§1.3 / §4.9).
EXTRA_ARGS: dict[str, dict[str, dict[str, Any]]] = {
    "auto_reframe": {"subject_track": {"type": "boolean"}},
    "apply_template": {"with_hook_stack": {"type": "boolean"}},
    "add_caption_track": {"chunk_size": {"type": "integer"}},
    "add_music": {"loop": {"type": "boolean"}},
}

PLAN_TOOLS: frozenset[str] = frozenset(
    ({t["name"] for t in _tools.list_tools()} | set(EXTRA_TOOL_SCHEMAS)) - PLAN_DENY)

#: Track ids the EDL creates itself (defaults + the ones handlers add on
#: demand). A step may name one of these before an earlier step in the same
#: plan has created it — `set_duck(track="music")` after `add_music`.
KNOWN_TRACK_IDS: frozenset[str] = frozenset(
    {"v1", "v2", "a1", "music", "vo", "captions", "tx_hook", "tx_super", "tx_lt", "stickers"})

#: (tool, arg) → (min, max); None = unbounded on that side.
ARG_BOUNDS: dict[tuple[str, str], tuple[float | None, float | None]] = {
    ("set_speed", "factor"): (0.25, 4.0),
    ("smooth_slow_motion", "factor"): (2, 8),
    ("add_music", "volume_db"): (-40.0, 0.0),
    ("add_music", "start"): (0.0, None),
    ("set_loudness_target", "lufs"): (-24.0, -9.0),
    ("auto_caption", "max_chars"): (16, 60),
    ("auto_caption", "max_cps"): (5, 40),
    ("auto_caption", "chunk_size"): (1, 12),
    ("add_caption_track", "chunk_size"): (1, 12),
    ("add_transition", "duration"): (0.1, 2.0),
    ("add_transition", "at"): (0.0, None),
    ("make_shorts", "target_count"): (1, 10),
    ("make_shorts", "max_dur"): (5.0, 180.0),
    ("make_shorts", "min_dur"): (1.0, 180.0),
    ("apply_hook_stack", "duration"): (1.0, 6.0),
    ("add_hook_overlay", "duration"): (1.0, 6.0),
    ("auto_cut_to_beats", "subdivision"): (1, 16),
    ("apply_lut", "intensity"): (0.0, 1.0),
    ("noise_reduce", "strength"): (0.0, 1.0),
    ("set_duck", "to_db"): (-40.0, 0.0),
    ("remove_silences", "threshold_db"): (-60.0, -10.0),
    ("remove_silences", "min_dur"): (0.1, 5.0),
    ("remove_silences", "keep_pad"): (0.0, 1.0),
    ("remove_fillers", "pad"): (0.0, 0.5),
    ("cut_range", "start"): (0.0, None),
    ("cut_range", "end"): (0.0, None),
    ("split_at", "time"): (0.0, None),
    ("tts_voiceover", "volume_db"): (-40.0, 6.0),
    ("tts_voiceover", "start"): (0.0, None),
    ("add_text", "start"): (0.0, None),
    ("add_text", "end"): (0.0, None),
    ("add_text", "size"): (8, 400),
    ("add_lower_third", "start"): (0.0, None),
    ("add_keyframe", "time"): (0.0, None),
}

#: (tool, arg) → max characters for free text that lands on screen or in a
#: synthesiser. Longer is refused, not truncated — a silent cut would ship a
#: half sentence.
TEXT_LIMITS: dict[tuple[str, str], int] = {
    ("tts_voiceover", "text"): 600, ("add_text", "text"): 120, ("add_super_text", "text"): 120,
    ("apply_hook_stack", "text"): 60, ("add_hook_overlay", "text"): 60,
    ("add_lower_third", "name"): 60, ("add_lower_third", "handle"): 40,
    ("apply_brand_kit", "handle"): 40, ("add_marker", "label"): 80,
}

_TRANSLATED_TARGETS: tuple[str, ...] = tuple(_D._TRANSLATED_TARGETS)
_LANG_CODE_RE = r"^[a-z]{2,3}$"
_SEAM_TOL_S = 0.05
MAX_STEPS = 24
MAX_QUESTIONS = 4
MAX_POSTCONDITIONS = 20


# --------------------------------------------------------------------------
# 2. Schema lookup
# --------------------------------------------------------------------------

def plan_schema_for(tool: str) -> dict[str, Any] | None:
    """The schema a PLAN step is checked against: the advertised
    `input_schema` (or `EXTRA_TOOL_SCHEMAS`) with `EXTRA_ARGS` merged in."""
    base = _tools.input_schema_for(tool) or EXTRA_TOOL_SCHEMAS.get(tool)
    if base is None:
        return None
    props = {**(base.get("properties") or {}), **EXTRA_ARGS.get(tool, {})}
    return {**base, "properties": props, "required": list(base.get("required") or [])}


def _is_placeholder(v: Any) -> bool:
    return isinstance(v, str) and v.startswith(ASK)


def _is_seam_sentinel(tool: str, key: str, v: Any) -> bool:
    return tool == "add_transition" and key == "at" and v == SEAM_SENTINEL


def _blank(v: Any) -> bool:
    return not isinstance(v, str) or not v.strip()


# --------------------------------------------------------------------------
# 3. Per-step checks
# --------------------------------------------------------------------------

def _check_shape(tool: str, args: dict[str, Any], schema: dict[str, Any], reasons: list[str]) -> dict[str, Any]:
    """Rule 5: unknown / required / type / enum, on a copy. Returns the
    canonicalised args (enum spellings normalised, None values dropped)."""
    props: dict[str, Any] = schema["properties"]
    out = {k: v for k, v in args.items() if v is not None}
    if tool == "apply_lut" and "lut_path" in out and "src" not in out:
        out["src"] = out.pop("lut_path")          # documented alias; the handler reads `src or lut_path`
    unknown = sorted(k for k in out if k not in props)
    if unknown:
        reasons.append(f"{tool}: unknown args {unknown}")
    for req in schema["required"]:
        if req not in out:
            reasons.append(f"{tool}: missing required arg {req!r}")
    for key, value in list(out.items()):
        spec = props.get(key)
        if not isinstance(spec, dict) or _is_placeholder(value) or _is_seam_sentinel(tool, key, value):
            continue
        declared = spec.get("type")
        if declared is not None:
            variants = declared if isinstance(declared, list) else [declared]
            if not any(_D._arg_type_ok(value, d) for d in variants):
                reasons.append(f"{tool}.{key}: must be {declared}, got {type(value).__name__} {value!r}")
                continue
        allowed = spec.get("enum")
        if allowed:
            if all(isinstance(a, str) for a in allowed):
                canonical = {a.lower(): a for a in allowed}
                hit = canonical.get(str(value).lower())
                if hit is None:
                    reasons.append(f"{tool}.{key}: must be one of {allowed[:12]}{'…' if len(allowed) > 12 else ''}, got {value!r}")
                    continue
                out[key] = hit
            elif value not in allowed:
                reasons.append(f"{tool}.{key}: must be one of {allowed}, got {value!r}")
                continue
        items = spec.get("items")
        if isinstance(items, dict) and items.get("type") and isinstance(value, (list, tuple)):
            bad = [i for i, item in enumerate(value) if not _D._arg_type_ok(item, items["type"])]
            if bad:
                reasons.append(f"{tool}.{key}[{bad[0]}]: items must be {items['type']}")
        if spec.get("type") == "object" and isinstance(value, dict):
            # `apply_text_template.fields` / `apply_template.inputs`: plain
            # scalars only — an object is where a nested path would hide.
            for k2, v2 in value.items():
                if not isinstance(v2, (str, int, float, bool)) or (isinstance(v2, str) and len(v2) > 200):
                    reasons.append(f"{tool}.{key}.{k2}: only short scalar values are allowed")
    return out


def _check_hook_text(tool: str, args: dict[str, Any], reasons: list[str], notes: list[str]) -> dict[str, Any]:
    """§1.3 / §3.7: the hook line is always supplied BY THE PLAN. Both handlers
    fall back to `generate_hook` — an Anthropic call whenever a key is set,
    outside the brain ladder, ignoring `VAI_PROMPT_CLOUD=0`, attributed to no
    brain — when the text is empty: `apply_hook_stack(text="")` and
    `apply_template(with_hook_stack=True)` with no `inputs.hook`/`inputs.text`
    both reached it through validation (verified with a spy, 2026-09-10).
    So: a blank hook text is refused, and a template's hook stack is switched
    off unless the template inputs carry a non-blank line."""
    if tool == "apply_hook_stack" and "text" in args and not _is_placeholder(args["text"]) and _blank(args["text"]):
        reasons.append("apply_hook_stack.text: must be a non-blank line (the plan supplies the hook; the handler "
                       "must never fall back to generate_hook)")
    if tool == "apply_template" and args.get("with_hook_stack", True):
        inputs = args.get("inputs") if isinstance(args.get("inputs"), dict) else {}
        line = inputs.get("hook") or inputs.get("text")
        if _blank(line) and not _is_placeholder(line):
            if args.get("with_hook_stack") is True:
                reasons.append("apply_template.with_hook_stack: needs inputs.hook (or inputs.text) — without a line "
                               "the handler calls generate_hook; add an apply_hook_stack step with text instead")
            else:
                notes.append("apply_template: hook stack left off (no hook text given) — add a hook step for one")
                return {**args, "with_hook_stack": False}
    return args


def _check_bounds(tool: str, args: dict[str, Any], reasons: list[str]) -> None:
    for (t, key), (lo, hi) in ARG_BOUNDS.items():
        if t != tool or key not in args or _is_placeholder(args[key]):
            continue
        try:
            v = float(args[key])
        except (TypeError, ValueError):
            continue          # the type check already reported it
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            reasons.append(f"{tool}.{key}: {v:g} is outside [{lo}, {hi}]")
    for (t, key), limit in TEXT_LIMITS.items():
        if t == tool and isinstance(args.get(key), str) and not _is_placeholder(args[key]) and len(args[key]) > limit:
            reasons.append(f"{tool}.{key}: {len(args[key])} characters is over the {limit} limit")


def _check_whitelists(tool: str, args: dict[str, Any], facts: TimelineFacts, plan: Plan,
                      reasons: list[str]) -> None:
    """Rule 8: every free string that could reach the filesystem or the
    network is a member of a closed set, and anything that would DOWNLOAD is
    already a `downloads_needed` entry for this tool."""
    asked = {d.tool for d in plan.downloads_needed}
    if tool in ("transcribe", "auto_caption") and "model" in args and not _is_placeholder(args["model"]):
        model = str(args["model"])
        if model not in WHISPER_MODELS:
            reasons.append(f"{tool}.model: {model!r} is not one of {list(WHISPER_MODELS)}")
        elif not facts.is_cached(f"whisper:{model}") and tool not in asked:
            reasons.append(f"{tool}.model: {model!r} is not downloaded and the plan does not ask to download it")
    if tool == "tts_voiceover":
        voice = args.get("voice")
        if voice is not None and not _is_placeholder(voice):
            if voice not in VOICE_IDS:
                reasons.append(f"tts_voiceover.voice: {voice!r} is not one of {list(VOICE_IDS)}")
            elif not facts.is_cached(f"piper:{voice}") and tool not in asked:
                reasons.append(f"tts_voiceover.voice: {voice!r} is not downloaded and the plan does not ask to download it")
    targets = [args.get(k) for k in ("target", "target_lang", "caption_lang", "to") if args.get(k) is not None]
    if tool in ("auto_caption", "translate_captions"):
        for target in targets:
            if _is_placeholder(target):
                continue
            if str(target) not in _D.CAPTION_TARGETS:
                reasons.append(f"{tool}: caption target {target!r} is not one of {list(_D.CAPTION_TARGETS)}")
            elif str(target) in _TRANSLATED_TARGETS and not facts.is_cached("madlad") and tool not in asked:
                reasons.append(f"{tool}: translating to {target!r} needs the MADLAD model, which is not "
                               "downloaded and not asked for")
        for key in ("language", "source_lang"):
            v = args.get(key)
            if v is not None and not (isinstance(v, str) and re.fullmatch(_LANG_CODE_RE, v)):
                reasons.append(f"{tool}.{key}: {v!r} is not a language code")
    if tool in ("add_text", "apply_brand_kit") and "font" in args:
        fonts = font_names()
        if fonts and args["font"] not in fonts:
            reasons.append(f"{tool}.font: {args['font']!r} is not a bundled font")
    if tool == "apply_show_template" and "name" in args and args["name"] not in show_template_names():
        reasons.append(f"apply_show_template.name: {args['name']!r} is not a saved show")
    if tool == "apply_export_preset" and "name" in args and args["name"] not in _D._EXPORT_PRESETS:
        reasons.append(f"apply_export_preset.name: {args['name']!r} is not an export preset")
    if tool == "add_transition" and "type" in args:
        from ...render.transitions import is_valid
        if not is_valid(str(args["type"])):
            reasons.append(f"add_transition.type: {args['type']!r} is not in the transition catalog")
    if tool == "set_clip_fit" and args.get("fit") not in (None, "contain", "cover"):
        reasons.append(f"set_clip_fit.fit: {args.get('fit')!r} must be contain or cover")


def _lut_name(value: str) -> str | None:
    """A bundled LUT as a bare name (`teal_orange` or `teal_orange.cube`), or
    None when the value is a path or not bundled."""
    if "/" in value or "\\" in value or value.startswith(".") or value.startswith("~"):
        return None
    names = lut_names()
    for cand in (value, f"{value}.cube"):
        if cand in names:
            return cand
    return None


def _resolve(v: str) -> str | None:
    try:
        return str(Path(v).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        return None


def _library_options(tool: str, arg: str, facts: TimelineFacts) -> list[tuple[str, str]]:
    """What the user may pick for a path arg we refused to take from a model."""
    if tool == "add_music":
        paths = list(facts.uploads_audio) + [str(b.path) for b in music_beds() if str(b.path) in facts.allowed_paths]
    elif tool == "apply_brand_kit":
        paths = list(facts.uploads_images) + sorted(p for p in facts.allowed_paths if "end_cards" in p.replace("\\", "/"))
    else:
        paths = sorted(facts.allowed_paths)
    return [(p, p.replace("\\", "/").rsplit("/", 1)[-1]) for p in paths[:12]]


def _question_key(tool: str, arg: str) -> str:
    if tool == "add_music" and arg == "src":
        return "music_src"          # the key the music recipe and the resume parser already use
    return f"{arg}_{tool}"[:32]


def _check_paths(tool: str, args: dict[str, Any], facts: TimelineFacts, schema: dict[str, Any],
                 reasons: list[str], notes: list[str], questions: list[NeedsInput]) -> dict[str, Any]:
    """Rule 6. Returns args with read paths resolved (or replaced by a
    placeholder that a question fills in). See the module docstring for why
    a fabricated path asks and a real one refuses."""
    out = dict(args)
    for arg, guard in _path_args.path_args_for(tool).items():
        if arg not in out:
            continue
        value = out[arg]
        if guard == "write":
            reasons.append(f"{tool}.{arg}: plans may not write files")
            continue
        if _is_placeholder(value):
            continue
        if tool == "apply_lut":
            name = _lut_name(str(value))
            if name is None:
                reasons.append(f"{tool}.{arg}: LUTs are bundled names (one of {sorted(lut_names())}), not paths — got {value!r}")
            else:
                out.pop("lut_path", None)
                out["src"] = name
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        resolved_list: list[str] = []
        for v in values:
            if not isinstance(v, str) or not v.strip():
                reasons.append(f"{tool}.{arg}: path must be a non-empty string")
                continue
            resolved = _resolve(v)
            if resolved is not None and resolved in facts.allowed_paths:
                resolved_list.append(resolved)
                continue
            base = Path(v).name
            by_name = [p for p in facts.allowed_paths if Path(p).name == base] if base else []
            if len(by_name) == 1:
                resolved_list.append(by_name[0])
                notes.append(f"{tool}.{arg}: {base} resolved to the offered file")
                continue
            # Not offered. A real file is refused; a made-up one becomes a question.
            exists = False
            try:
                exists = Path(v).expanduser().exists()
            except (OSError, RuntimeError, ValueError):
                exists = False
            if exists or v.startswith("~") or ".." in Path(v).parts:
                reasons.append(f"{tool}.{arg}: path not offered ({base or v})")
                continue
            if arg not in schema["required"]:
                notes.append(f"{tool}: dropped {arg} — {base or v} is not a file this session offers")
                out.pop(arg, None)
                resolved_list = []
                break
            key = _question_key(tool, arg)
            if len(questions) >= MAX_QUESTIONS and key not in {q.key for q in questions}:
                reasons.append(f"{tool}.{arg}: path not offered ({base or v}) and no room to ask")
                break
            options = _library_options(tool, arg, facts)
            if key not in {q.key for q in questions}:
                questions.append(ask(key, f"Which file for {tool}? {base or v} is not one this session offers."
                                     + (" Reply with the file name." if options else " Upload it first, then reply with its name."),
                                     kind="choice" if options else "path", options=options or None))
            notes.append(f"{tool}.{arg}: asked for the file instead of using a model-authored path")
            out[arg] = placeholder(key)
            resolved_list = []
            break
        if resolved_list:
            out[arg] = resolved_list if isinstance(value, (list, tuple)) else resolved_list[0]
            if config.restrict_paths_active():
                for r in resolved_list:
                    try:
                        config.assert_path_allowed(r)
                    except ValueError as e:
                        reasons.append(f"{tool}.{arg}: {e}")
    return out


def _check_refs(tool: str, args: dict[str, Any], facts: TimelineFacts, has_cuts: bool, reasons: list[str]) -> None:
    """Rule 7: clip / track references exist or are sentinels; a transition
    sits on a seam (the compositor ignores one that does not, silently)."""
    known_clips = set(facts.clip_ids) | set(facts.v1_clip_ids)
    for key in ("clip_id", "clip_ids"):
        if key not in args or _is_placeholder(args[key]):
            continue
        refs = args[key] if isinstance(args[key], (list, tuple)) else [args[key]]
        for ref in refs:
            if ref in CLIP_SENTINELS or ref in known_clips:
                continue
            reasons.append(f"{tool}.{key}: {ref!r} is not a clip on this timeline (use a sentinel like $v1_all)")
    if "track" in args and not _is_placeholder(args["track"]):
        if args["track"] not in set(facts.track_ids) | KNOWN_TRACK_IDS:
            reasons.append(f"{tool}.track: {args['track']!r} is not a track")
    if tool == "cut_range" and all(isinstance(args.get(k), (int, float)) for k in ("start", "end")):
        if float(args["end"]) <= float(args["start"]):
            reasons.append("cut_range: end must be after start")
        elif facts.duration and float(args["start"]) >= facts.duration + _SEAM_TOL_S:
            reasons.append(f"cut_range: start {args['start']:g}s is past the end of the video ({facts.duration:g}s)")
    if tool == "add_transition" and isinstance(args.get("at"), (int, float)) and not has_cuts:
        if facts.v1_boundaries and not any(abs(float(args["at"]) - b) <= _SEAM_TOL_S for b in facts.v1_boundaries):
            reasons.append(f"add_transition.at: {float(args['at']):g}s is not a seam between two v1 clips")
        elif not facts.v1_boundaries and facts.v1_clip_ids:
            reasons.append("add_transition: there is no seam on v1 to put a transition on")


def _normalise_defaults(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Finding 13 floors: the default filler list is the safe minimum."""
    if tool == "remove_fillers":
        extra = args.get("words") or ()
        if isinstance(extra, str):
            extra = [extra]
        words = list(dict.fromkeys([*FILLERS_STRICT, *(str(w).strip().lower() for w in extra if str(w).strip())]))
        return {**args, "words": words}
    return args


# --------------------------------------------------------------------------
# 4. Plan-level passes
# --------------------------------------------------------------------------

def _expand_macros(plan: Plan, facts: TimelineFacts) -> Plan:
    """Rule 2: `recipe:<name>` steps → the recipe table's steps, questions,
    postconditions and downloads, via the same `compose` the grammar uses."""
    macros = [s for s in plan.steps if s.tool.startswith("recipe:")]
    if not macros:
        return plan
    intents: list[Intent] = []
    reasons: list[str] = []
    for s in macros:
        name = s.tool[len("recipe:"):]
        if name not in RECIPE_BY_NAME or name in ("ask", "transcribe"):
            reasons.append(f"unknown recipe macro {s.tool!r}")
            continue
        intents.append(Intent(name, normalize_slots(name, dict(s.args)), clause=s.why))
    if reasons:
        raise PlanRejected(reasons)
    from .planner import compose
    sub = compose(intents, facts, brain=plan.brain, confidence=plan.confidence)
    keys = {q.key for q in plan.needs_input}
    questions = list(plan.needs_input) + [q for q in sub.needs_input if q.key not in keys]
    if len(questions) > MAX_QUESTIONS:
        raise PlanRejected(f"recipe macros need {len(questions)} questions; the limit is {MAX_QUESTIONS}")
    steps = [s for s in plan.steps if not s.tool.startswith("recipe:")] + list(sub.steps)
    if len(steps) > MAX_STEPS:
        raise PlanRejected(f"{len(steps)} steps after expanding macros; the limit is {MAX_STEPS}")
    reply = "; ".join(dict.fromkeys(p for p in [plan.reply, sub.reply] if p))[:400] or None
    return plan.with_(steps=steps, needs_input=questions,
                      postconditions=(list(plan.postconditions) + list(sub.postconditions))[:MAX_POSTCONDITIONS],
                      downloads_needed=(list(plan.downloads_needed) + list(sub.downloads_needed))[:6],
                      reply=reply, content_brain=plan.content_brain or sub.content_brain)


def _platform_rules(steps: list[Step], facts: TimelineFacts, notes: list[str]) -> list[Step]:
    """Platform → aspect: an export preset whose aspect differs from the
    canvas gets the reframe pair in front of it; a reframe that conflicts
    with the preset wins and the preset is re-derived; every `auto_reframe`
    is followed by `set_clip_fit(cover)`."""
    from .expanders import _preset_for
    out = list(steps)
    reframes = [s for s in out if s.tool == "auto_reframe" and isinstance(s.args.get("ratio"), str)]
    reframe_ratio = reframes[-1].args["ratio"] if reframes else None
    subject = "motion_track" in facts.tools_available
    for i, s in enumerate(list(out)):
        if s.tool != "apply_export_preset" or _is_placeholder(s.args.get("name")):
            continue
        name = s.args.get("name")
        preset_ratio = S.PLATFORM_RATIO.get(str(name))
        if preset_ratio is None:
            continue
        if reframe_ratio and reframe_ratio != preset_ratio:
            new_name = _preset_for(str(name), reframe_ratio)
            if new_name and new_name != name:
                out[out.index(s)] = s.model_copy(update={"args": {**s.args, "name": new_name}})
                notes.append(f"export preset re-derived as {new_name} to match the {reframe_ratio} reframe")
            continue
        if not reframe_ratio and facts.aspect != preset_ratio and not any(
                t.tool in ("set_canvas", "set_aspect_ratio") for t in out):
            out.append(Step(tool="auto_reframe", args={"ratio": preset_ratio, "subject_track": subject},
                            why=f"the {name} preset is {preset_ratio}; reframe first so it does not letterbox",
                            stage=STAGE_REFRAME))
            reframe_ratio = preset_ratio
            notes.append(f"reframe to {preset_ratio} inserted ahead of the {name} export preset")
    for s in list(out):
        if s.tool != "auto_reframe":
            continue
        idx = out.index(s)
        if not any(t.tool == "set_clip_fit" and t.args.get("fit") == "cover" for t in out[idx + 1:]):
            out.insert(idx + 1, Step(tool="set_clip_fit", args={"clip_id": "$v1_all", "fit": "cover"},
                                     why="fill the frame — auto_reframe may skip a clip and Clip.fit defaults to contain",
                                     stage=STAGE_REFRAME))
    return out


def _pc_key(p: Postcondition) -> str:
    return p.check + repr(sorted(p.args.items(), key=lambda kv: kv[0]))


def _bind_plan_refs(steps: list[Step], pcs: list[Postcondition]) -> list[Postcondition]:
    """A recipe may express a check against a value it does not know at plan
    time as `"$arg:<name>"` (schema.ARG_REF) — `brand` asks for the handle
    and still wants `text_present(contains=<handle>)` on the end card. Once
    the answer is bound into the step (resume → planner.apply_answers →
    here), the reference resolves to the step arg of that name; while the
    arg is still an `$ask:` placeholder the reference is left for the next
    validation. WHY: computed at plan time, the resumed plan was verified
    with FEWER checks than the same request with the handle inline (2/2 vs
    3/3) — a stronger verdict from a weaker audit."""
    out: list[Postcondition] = []
    for pc in pcs:
        refs = {k: v for k, v in pc.args.items() if isinstance(v, str) and v.startswith(ARG_REF)}
        if not refs:
            out.append(pc)
            continue
        new_args = dict(pc.args)
        for key, ref in refs.items():
            name = ref[len(ARG_REF):]
            value = next((st.args[name] for st in steps
                          if name in st.args and st.args[name] is not None and not _is_placeholder(st.args[name])),
                         None)
            if value is not None:
                new_args[key] = value
        out.append(pc.model_copy(update={"args": new_args}))
    return out


def _fill_postconditions(steps: list[Step], existing: list[Postcondition]) -> list[Postcondition]:
    """Every step is covered by its natural checks: a default whose
    (check, bound args) the plan does not already carry is added, so a raw
    `claude` plan is verified exactly like a recipe plan (§1.1). `tool_ok`
    is only ever added for a tool with no natural check of its own."""
    out = list(existing)
    have = {_pc_key(p) for p in out}
    for s in steps:
        for p in bind_postconditions(s.tool, s.args):
            if _pc_key(p) in have:
                continue
            out.append(p)
            have.add(_pc_key(p))
    uniq: list[Postcondition] = []
    seen: set[str] = set()
    for p in out:
        if _pc_key(p) in seen:
            continue
        seen.add(_pc_key(p))
        uniq.append(p)
    if len(uniq) > MAX_POSTCONDITIONS:
        uniq = [p for p in uniq if p.check != "tool_ok"]
    if len(uniq) > MAX_POSTCONDITIONS:
        uniq = ([p for p in uniq if p.headline] + [p for p in uniq if not p.headline])[:MAX_POSTCONDITIONS]
    return uniq


# --------------------------------------------------------------------------
# 5. validate_plan
# --------------------------------------------------------------------------

def validate_plan(plan: Plan | dict[str, Any], facts: TimelineFacts) -> Plan:
    """The boundary (§1.3). Returns a NEW, stage-sorted Plan with an id, or
    raises `PlanRejected` carrying every reason. The input is never mutated."""
    p = Plan.model_validate(plan if isinstance(plan, dict) else plan.model_dump())
    p = _expand_macros(p, facts)
    reasons: list[str] = []
    notes: list[str] = []
    questions: list[NeedsInput] = list(p.needs_input)
    has_cuts = any((s.stage if s.stage is not None else TOOL_STAGE.get(s.tool, STAGE_AUDIT)) == STAGE_CUTS
                   for s in p.steps if not s.tool.startswith("recipe:"))
    checked: list[Step] = []
    for i, s in enumerate(p.steps):
        tag = f"step {i + 1} {s.tool}"
        if s.tool in PLAN_DENY:
            reasons.append(f"{tag}: tool is denied to plans")
            continue
        if s.tool not in PLAN_TOOLS:
            reasons.append(f"{tag}: unknown tool")
            continue
        schema = plan_schema_for(s.tool)
        if schema is None:
            reasons.append(f"{tag}: tool has no schema")
            continue
        step_reasons: list[str] = []
        args = _check_shape(s.tool, dict(s.args), schema, step_reasons)
        args = _normalise_defaults(s.tool, args)
        args = _check_hook_text(s.tool, args, step_reasons, notes)
        args = _check_paths(s.tool, args, facts, schema, step_reasons, notes, questions)
        _check_refs(s.tool, args, facts, has_cuts, step_reasons)
        _check_bounds(s.tool, args, step_reasons)
        _check_whitelists(s.tool, args, facts, p, step_reasons)
        reasons.extend(f"{tag}: {r}" if not r.startswith(s.tool) else f"step {i + 1} {r}" for r in step_reasons)
        stage = s.stage if s.stage is not None else TOOL_STAGE.get(s.tool, STAGE_AUDIT)
        checked.append(s.model_copy(update={"args": args, "stage": stage}))
    if reasons:
        raise PlanRejected(reasons)
    steps = _platform_rules(checked, facts, notes)
    if len(steps) > MAX_STEPS:
        raise PlanRejected(f"{len(steps)} steps; the limit is {MAX_STEPS}")
    steps = sorted(steps, key=lambda s: s.stage if s.stage is not None else STAGE_AUDIT)   # stable
    postconditions = _fill_postconditions(steps, _bind_plan_refs(steps, list(p.postconditions)))
    reply = "; ".join(dict.fromkeys(x for x in [p.reply, *notes] if x))[:400] or None
    return p.with_(id=p.id or Plan.new_id(), steps=steps, needs_input=questions[:MAX_QUESTIONS],
                   postconditions=postconditions, estimated_seconds=estimate_seconds(steps, facts), reply=reply)


__all__ = ["PlanRejected", "EXTRA_TOOL_SCHEMAS", "EXTRA_ARGS", "PLAN_TOOLS", "KNOWN_TRACK_IDS",
           "ARG_BOUNDS", "TEXT_LIMITS", "plan_schema_for", "validate_plan"]
