"""Recipe table: names, slot names, cards, and recipe → steps expansion (§2.4).

Frozen contract (§0.2): `RECIPE_NAMES`, `RECIPE_SLOTS`, `RecipeCard`,
`cards()` and the signature of `from_intents()`. B builds the on-device
brains' prompts from `cards()` — a recipe name or slot name that is not in
this table is one the 7B model cannot be asked for.

WHY the brains see recipes, not tools (baseline findings 12/13): given raw
tool names the 7B model invented every arg name; given recipe cards with
slot names it emitted zero unknown args. The model owns intent, order and
slot values; recipes own exact tool args; `validate_plan` owns the boundary.

WHY `FILLERS_STRICT` is short: `remove_fillers` matches SINGLE tokens only,
so multi-word entries ("you know", "so basically") never matched anyway, and
an unquoted "like" is a content word ("I like this part" must survive —
benchmark case 4). Users add words by quoting them.

Layout of this module:
  1. cards (the contract)            3. Expansion types + helpers
  2. slot normalisation              4. from_intents (→ planner.compose)

The per-recipe expanders and the cost table live in `expanders.py` (an
expander is a pure `(Intent, TimelineFacts, Context) -> Expansion`); they are
reachable here as `recipes.EXPANDERS`, `recipes.heuristic_hook`, … through
the lazy `__getattr__` at the bottom, so the recipe surface has one import
path for every consumer.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from . import slots as S
from .facts import FIRST_USE_BYTES, TimelineFacts, VOICE_IDS, WHISPER_MODELS
from .presets import transition_catalog
from .schema import (CHECK_SPECS, DownloadNeeded, IntentDraft, NeedsInput, NeedsInputOption, Plan,
                     Postcondition, Step)

#: Single-token fillers removed by default (§2.4 `remove_fillers`).
FILLERS_STRICT: tuple[str, ...] = ("um", "uh", "hmm", "erm", "uhh", "umm")

#: Slot value kinds as the FM helper's `recipes[].slots` expects them
#: (`"enum|number|text"`; §3.2) — `bool` is spelled as an enum of yes/no.
SlotKind = str  # "enum" | "number" | "text"


# --------------------------------------------------------------------------
# 1. Cards — the frozen contract
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class RecipeCard:
    """What an on-device brain is told about one recipe: the name, one line,
    and the slot names with their kind and (for enums) allowed values. Never
    a path, never a tool name."""
    name: str
    description: str
    slots: dict[str, SlotKind] = field(default_factory=dict)
    slot_values: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def as_prompt_dict(self) -> dict:
        """The JSON shape the FM helper and the MLX system prompt consume."""
        return {"name": self.name, "description": self.description, "slots": dict(self.slots)}


_PLATFORMS = ("reels", "tiktok", "shorts", "story", "youtube_16x9", "youtube_4k",
              "ig_feed_1x1", "ig_feed_4x5")
_RATIOS = ("9:16", "16:9", "1:1", "4:5")
_LANGS = ("hi", "en", "hinglish", "es")
_STYLES = ("ig_chunky", "word_emphasis", "default")
_POSITIONS = ("top", "center", "bottom")
_MOODS = ("chill", "upbeat", "cinematic", "lofi")
_LOOKS = ("teal_orange.cube", "warm.cube", "cool.cube", "punch.cube", "faded.cube", "mono.cube")
_TRANSITION_LOOKS = ("smooth", "cinematic", "punchy", "clean")


def _transition_type_values() -> tuple[str, ...]:
    """The 72 canonical looks from presets/transitions/catalog.json — the
    enum a brain may pick a `type` from. Aliases and display labels are
    normalised into these by `normalize_slots` (via `slots.transition_type_of`)."""
    try:
        return tuple(sorted(transition_catalog()))
    except ValueError:
        from ..render.transitions import CUSTOM_EXPRS, NATIVE   # pragma: no cover — catalog missing
        return tuple(sorted(set(NATIVE) | set(CUSTOM_EXPRS)))


_TRANSITION_TYPES = _transition_type_values()
_VOICES = VOICE_IDS
_YES_NO = ("yes", "no")


def _card(name: str, description: str, /, **slots: SlotKind | tuple[str, ...]) -> RecipeCard:
    # positional-only: the `title` recipe has a slot literally called `name`.
    kinds: dict[str, SlotKind] = {}
    values: dict[str, tuple[str, ...]] = {}
    for slot, kind in slots.items():
        if isinstance(kind, tuple):
            kinds[slot] = "enum"
            values[slot] = kind
        else:
            kinds[slot] = kind
    return RecipeCard(name=name, description=description, slots=kinds, slot_values=values)


#: The recipe table (§2.4), in stage order. Descriptions are one line each
#: because they are the entire vocabulary the on-device brains receive.
RECIPE_CARDS: tuple[RecipeCard, ...] = (
    _card("transcribe", "Transcribe the speech (prerequisite; inserted automatically).",
          model=WHISPER_MODELS),
    _card("captions", "Add captions from the speech.",
          style=_STYLES, position=_POSITIONS, target=_LANGS, max_chars="number",
          model_upgrade=_YES_NO),
    _card("translate_captions", "Translate the captions to another language.", target_lang=_LANGS),
    _card("remove_silences", "Cut the silent pauses.",
          threshold_db="number", min_dur="number", keep_pad="number"),
    _card("remove_fillers", "Cut filler words like um and uh.", words="text"),
    _card("tighten", "Remove silences and filler words."),
    _card("shorts", "Cut the video into several short clips.",
          count="number", max_dur="number", min_dur="number", platform=_PLATFORMS, finish=_YES_NO),
    _card("reframe", "Change the aspect ratio, keeping the subject in frame.",
          ratio=_RATIOS, platform=_PLATFORMS, subject_track=_YES_NO),
    _card("music", "Add background music, ducked under speech.",
          mood=_MOODS, src="text", volume_db="number", duck=_YES_NO),
    _card("duck", "Lower the music under speech.", to_db="number"),
    _card("beat_sync", "Cut and pulse the picture on the music's beats.",
          subdivision="number", pulse=_YES_NO),
    _card("hook", "Add an attention hook in the first seconds.", text="text", duration_s="number"),
    _card("color_look", "Apply a colour look.", look=_LOOKS, intensity="number"),
    _card("clean_audio", "Reduce noise and normalise loudness.", strength="number", lufs="number"),
    _card("loudness", "Set the loudness target.", lufs="number"),
    _card("speed", "Change playback speed.", factor="number", clip_ref="text"),
    _card("trim", "Cut a time range out.", range="text"),
    _card("title", "Add a title or lower third.",
          text="text", name="text", handle="text", at="number", dur="number"),
    _card("brand", "Apply the brand kit (watermark, colours, end card).",
          handle="text", hashtags="text", palette="text"),
    _card("end_card", "Add an end card with the handle.", handle="text"),
    _card("transitions", "Add transitions between the clips: a look for every seam, or one named type "
          "at the first, last or every seam, or at a time.",
          look=_TRANSITION_LOOKS, type=_TRANSITION_TYPES, at="text", duration="number"),
    _card("export_preset", "Set the export preset for a platform.", platform=_PLATFORMS),
    _card("voiceover", "Add a synthesised voiceover.", text="text", voice=_VOICES, start="number"),
    _card("stabilize", "Stabilise shaky footage.", clip_ref="text"),
    _card("upscale", "Upscale the resolution.", clip_ref="text", upscale_factor=("2", "4")),
    _card("auto_edit", "Do the whole edit for a platform.",
          platform=_PLATFORMS, language=_LANGS, mood=_MOODS, look=_LOOKS),
    _card("ask", "Answer a question about the timeline without editing."),
)

RECIPE_NAMES: tuple[str, ...] = tuple(c.name for c in RECIPE_CARDS)
RECIPE_SLOTS: dict[str, tuple[str, ...]] = {c.name: tuple(c.slots) for c in RECIPE_CARDS}
RECIPE_BY_NAME: dict[str, RecipeCard] = {c.name: c for c in RECIPE_CARDS}

#: Recipes that end the parent plan (§2.5): `finish` children run as their
#: own one-op commits in the sessions `make_shorts` creates.
TERMINAL_RECIPES: frozenset[str] = frozenset({"shorts"})

#: Recipes the grammar handles as their own intent, never expanded to steps.
NON_STEP_RECIPES: frozenset[str] = frozenset({"ask"})

#: Recipes that read the transcript and therefore need `transcribe` first
#: when the session has none (§2.4 prerequisite column).
TRANSCRIPT_RECIPES: frozenset[str] = frozenset({"captions", "translate_captions", "remove_fillers", "tighten"})

#: What a `shorts.finish` child session gets (§2.4): the executor expands
#: this per new session with `from_intents`.
SHORTS_FINISH_DRAFT = IntentDraft(
    intents=[{"recipe": "reframe", "slots": {"ratio": "9:16"}},
             {"recipe": "captions", "slots": {}},
             {"recipe": "hook", "slots": {}}],
    confidence=1.0, reply="finish each short")


def cards(*, exclude: frozenset[str] = frozenset({"transcribe", "ask"})) -> list[RecipeCard]:
    """Recipe cards for the on-device brains (§3.1). `transcribe` is a
    prerequisite the expander inserts itself and `ask` is routed by the
    grammar, so neither is offered to a model by default; the FM `context`
    retry (§3.2) passes a smaller table by trimming this list."""
    return [c for c in RECIPE_CARDS if c.name not in exclude]


# --------------------------------------------------------------------------
# 2. Slot normalisation (draft or grammar values → canonical)
# --------------------------------------------------------------------------

def _as_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("yes", "y", "true", "on", "1", "haan", "ha"):
            return True
        if s in ("no", "n", "false", "off", "0", "nahi"):
            return False
    return None


def _as_float(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(float(v)) else None
    if isinstance(v, str):
        d = S.parse_duration(v)
        if d is not None and any(u in v.lower() for u in ("s", "m")):
            return d[0]
        n = S.parse_number(v)
        if n is not None:
            return n
        try:
            return float(v.strip().rstrip("x×"))
        except ValueError:
            return None
    return None


def _as_enum(v: Any, allowed: tuple[str, ...]) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    for a in allowed:
        if s.lower() == a.lower():
            return a
    return None


def _as_words(v: Any) -> tuple[str, ...]:
    if isinstance(v, (list, tuple)):
        items = [str(x) for x in v]
    elif isinstance(v, str):
        items = [x for x in v.replace(";", ",").split(",")]
    else:
        return ()
    out: list[str] = []
    for w in items:
        w = w.strip().strip("\"'").lower()
        if w and w not in out:
            out.append(w)
    return tuple(out)


def normalize_slots(recipe: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Canonical slot values for `recipe` from a model draft or the grammar.
    Unknown slots are dropped; unparsable values are dropped (the recipe's
    default then applies) — never raised, because a 7B model's `"count":
    "three"` should become 3, not a fall-through to the next brain."""
    card = RECIPE_BY_NAME[recipe]
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if value is None or (key not in card.slots and not key.startswith("_")):
            continue
        if key.startswith("_"):
            out[key] = value
            continue
        kind = card.slots[key]
        if kind == "enum":
            allowed = card.slot_values[key]
            if allowed == _YES_NO:
                b = _as_bool(value)
                if b is not None:
                    out[key] = b
                continue
            if allowed == ("2", "4"):
                f = _as_float(value)
                if f in (2.0, 4.0):
                    out[key] = int(f)
                continue
            e = _as_enum(value, allowed)
            if e is None and key == "look":
                e = _as_enum(f"{value}.cube", allowed) or S.extract(f"{value} look").look
            if e is None and key == "platform":
                e = S.extract(f"for {value}").platform
            if e is None and key == "ratio":
                e = S.extract(str(value)).ratio
            if e is None and key in ("target", "target_lang", "language"):
                e = S.extract(f"in {value}").language
            if e is None and key == "mood":
                e = S.extract(f"{value} music").mood
            if e is None and key == "voice":
                e = S.extract(f"{value} voice").voice
            if e is None and key == "style":
                e = S.extract(f"{value} captions").caption_style
            if e is None and key == "type" and recipe == "transitions":
                e = S.transition_type_of(str(value))
            if e is not None:
                out[key] = e
        elif kind == "number":
            f = _as_float(value)
            if f is not None:
                out[key] = f
        else:  # text
            if key == "words":
                out[key] = _as_words(value)
            elif key == "hashtags":
                tags = _as_words(value) if not isinstance(value, str) else tuple(
                    t if t.startswith("#") else f"#{t}" for t in S.extract(value).hashtags or _as_words(value))
                out[key] = tuple(t if t.startswith("#") else f"#{t}" for t in tags)
            elif key == "palette":
                out[key] = tuple(p if p.startswith("#") else f"#{p}" for p in _as_words(value))
            elif key == "range":
                if isinstance(value, S.TimeRange):
                    out[key] = value
                else:
                    r = S.extract(f"cut {value}").range or S.extract(str(value)).range
                    if r is not None:
                        out[key] = r
            elif key == "clip_ref":
                s = str(value).strip()
                out[key] = s if s.startswith(("$", "c_")) else (S.extract(f"{s} clip").clip_ref or "$v1_all")
            elif key == "src":
                out[key] = str(value).strip()
            elif key == "at" and recipe == "transitions":
                where: Any = S.transition_at_of(str(value)) if isinstance(value, str) else _as_float(value)
                if where is None and isinstance(value, str) and value.strip().lower() in ("first", "last", "all"):
                    where = value.strip().lower()
                if where is not None:
                    out[key] = where
            else:
                text = str(value).strip()
                if text:
                    out[key] = text
    return out


# --------------------------------------------------------------------------
# 3. Expansion types + helpers
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Intent:
    """One requested recipe with canonical slots (see `normalize_slots`).
    Internal keys start with `_` and never come from a model."""
    recipe: str
    slots: dict[str, Any] = field(default_factory=dict)
    score: float = 1.0
    clause: str = ""

    def get(self, key: str, default: Any = None) -> Any:
        return self.slots.get(key, default)


@dataclass(frozen=True)
class Context:
    """What one expander may know about the REST of the plan."""
    recipes: frozenset[str]                  # every recipe in the plan (after auto_edit expansion)
    exclusions: frozenset[str]
    has_cut_steps: bool                      # a stage-2 step that changes the timeline is present
    hook_text: tuple[str, str] | None = None  # (text, brain id) from Brain.text (§3.7)
    allow_downloads: bool = True
    explicit_lufs: bool = False


@dataclass(frozen=True)
class Expansion:
    steps: tuple[Step, ...] = ()
    postconditions: tuple[Postcondition, ...] = ()
    questions: tuple[NeedsInput, ...] = ()
    downloads: tuple[DownloadNeeded, ...] = ()
    notes: tuple[str, ...] = ()
    content_brain: str | None = None
    prerequisites: tuple["Intent", ...] = ()   # intents that must join the plan (transcribe, music, captions, reframe)


def pc(check: str, human: str, /, needs_render: bool | None = None, headline: bool | None = None,
       **args: Any) -> Postcondition:
    """A postcondition whose args are all names the verifier's CHECK_SPECS
    declares — a recipe cannot invent a check argument."""
    spec = CHECK_SPECS[check]
    unknown = set(args) - set(spec.args)
    if unknown:
        raise KeyError(f"{check}: unknown postcondition args {sorted(unknown)}")
    return Postcondition(check=check, args=args, human=human,
                         needs_render=spec.needs_render if needs_render is None else needs_render,
                         headline=spec.headline if headline is None else headline)


def step(tool: str, stage: int, why: str, *, optional: bool = False, **args: Any) -> Step:
    return Step(tool=tool, args={k: v for k, v in args.items() if v is not None},
                why=why[:160], optional=optional, stage=stage)


def ask(key: str, question: str, *, kind: str = "choice", options: list[tuple[Any, str]] | None = None,
        default: Any = None, required: bool = True, **extra: Any) -> NeedsInput:
    # `(value, label)` or `(value, label, hint)` — the hint is the one place a
    # gate question may carry its technical remedy without putting it in the chip.
    opts = [NeedsInputOption(value=o[0], label=o[1], hint=(o[2] if len(o) > 2 else None))
            for o in (options or [])] or None
    return NeedsInput(key=key, question=question[:200], kind=kind, options=opts,  # type: ignore[arg-type]
                      default=default, required=required, **extra)


def download(key: str, tool: str) -> DownloadNeeded:
    size, label = FIRST_USE_BYTES[key]
    return DownloadNeeded(what=label, bytes=size, tool=tool)


ASK = "$ask:"


def placeholder(key: str) -> str:
    """The arg value a blocking question fills in on resume (planner.apply_answers)."""
    return f"{ASK}{key}"


#: Prefix a required question carries once an EMPTY answer has been refused.
#: The resume paths (planner.apply_answers, pending.apply_answers) refuse an
#: empty answer to a blocking question once, with this note, and drop the
#: step on the second — the benchmark runner answered "" to "What should the
#: title say?" four times and hit its round cap; a user tapping send on an
#: empty box would have looped the same way (case 16).
REASK_PREFIX = "I still need the "


def _answer_noun(q: NeedsInput) -> str:
    return q.key.replace("_", " ")


def reask(q: NeedsInput) -> NeedsInput:
    """`q` asked a second time: the same question with the refusal in front.
    WHY a text marker and not a field: `needs_input` items are
    `additionalProperties: false` on the wire (spec §1.2), and the phone
    shows the question verbatim, so the marker IS the user-facing note."""
    if was_reasked(q):
        return q
    return NeedsInput.model_validate({**q.model_dump(),
                                      "question": f"{reask_note(q)}. {q.question}"[:200]})


def was_reasked(q: NeedsInput) -> bool:
    return q.question.startswith(REASK_PREFIX)


def reask_note(q: NeedsInput) -> str:
    return f"{REASK_PREFIX}{_answer_noun(q)}"


def dropped_note(q: NeedsInput, whys: list[str]) -> str:
    """The honest reply for a step dropped after two empty answers."""
    what = ", ".join(whys) or q.key
    return f"skipped {what} — no {_answer_noun(q)} was given"


def is_blank_answer(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def consumes_answer(args: dict[str, Any], key: str) -> bool:
    """True when a step waits on the answer to `key`: `$ask:<key>` anywhere
    in `args` (nested included) or an arg NAMED `key` left None — the two
    binding conventions the module docstring of pending.py describes."""
    if key in args and args[key] is None:
        return True
    return _mentions_placeholder(args, f"{ASK}{key}")


def _mentions_placeholder(value: Any, ph: str) -> bool:
    if isinstance(value, str):
        return value == ph
    if isinstance(value, dict):
        return any(_mentions_placeholder(v, ph) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_mentions_placeholder(v, ph) for v in value)
    return False


# --------------------------------------------------------------------------
# 4. IntentDraft → Plan
# --------------------------------------------------------------------------

def intents_from_draft(draft: IntentDraft) -> list[Intent]:
    unknown = [i.recipe for i in draft.intents if i.recipe not in RECIPE_BY_NAME]
    if unknown:
        raise KeyError(f"unknown recipe(s): {unknown}")
    return [Intent(i.recipe, normalize_slots(i.recipe, dict(i.slots)), score=draft.confidence)
            for i in draft.intents]


def from_intents(draft: IntentDraft, facts: TimelineFacts, *, hook_text: tuple[str, str] | None = None,
                 allow_downloads: bool = True) -> Plan:
    """Expand an `IntentDraft` into a stage-ordered `Plan` with prerequisites,
    postconditions and idempotence rules (§1.1, §2.4–2.6).

    Raises `KeyError` for a recipe name outside `RECIPE_NAMES` (the router
    reports `rejected:<why>`), drops slots a recipe does not declare, and
    returns a Plan whose `brain` is left as "recipes" for the router to set.
    `hook_text` = (text, brain_id) from `Brain.text` (§3.7) when a content
    brain wrote the hook; `allow_downloads=False` is the resume path after a
    "skip" answer to the downloads question (§1.4).
    """
    from .planner import compose
    intents = intents_from_draft(draft)
    exclusions = frozenset(x for x in draft.exclusions if x in RECIPE_BY_NAME)
    return compose(intents, facts, exclusions=exclusions, confidence=float(draft.confidence),
                   reply_prefix=draft.reply.strip() or None, hook_text=hook_text,
                   allow_downloads=allow_downloads, brain="recipes",
                   extra_questions=[ask(q.key, q.question, kind="choice" if q.options else "text",
                                        options=[(o, str(o)) for o in q.options], default=q.default,
                                        required=q.default is None)
                                    for q in draft.needs_input if q.key and q.question])


def __getattr__(name: str) -> Any:
    """Lazy re-export of `expanders.py` (PEP 562). WHY lazy: expanders.py
    imports the helper types above, so an eager import here would be a
    cycle that breaks whichever module is imported first. Resolving on
    first access keeps `recipes.EXPANDERS` / `from recipes import
    heuristic_hook` working for every consumer in either import order."""
    if name.startswith("__"):
        # The import system probes `__path__`/`__wrapped__` on every module;
        # answering those with an import would re-enter the cycle.
        raise AttributeError(name)
    from . import expanders as _x
    if name in _x.EXPANDER_EXPORTS:
        return getattr(_x, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["FILLERS_STRICT", "SlotKind", "RecipeCard", "RECIPE_CARDS", "RECIPE_NAMES",
           "RECIPE_SLOTS", "RECIPE_BY_NAME", "TERMINAL_RECIPES", "NON_STEP_RECIPES", "TRANSCRIPT_RECIPES",
           "SHORTS_FINISH_DRAFT", "cards", "normalize_slots", "Intent", "Context", "Expansion",
           "pc", "step", "ask", "download", "ASK", "placeholder", "EXPANDERS", "expand_auto_edit",
           "audit_expansion", "beat_split_times", "heuristic_hook", "RECIPE_COST", "step_cost",
           "estimate_seconds", "intents_from_draft", "from_intents"]
