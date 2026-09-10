"""The recipes brain: prompt → intents → composed Plan (§1.2, §2.5–2.7).

  plan(prompt, facts)                grammar → bind slots → compose
  compose(intents, facts, …)         the composition rules; also the target
                                     of `recipes.from_intents` for the
                                     on-device brains
  apply_answers(plan, answers, facts) the resume path (§4.3): fills `$ask:`
                                     placeholders, honours downloads/go/gate
                                     answers, re-validates

Composition rules (§2.5), each with the reason it exists:

  * Stages sort the steps; the recipe table owns the stage of every step, so
    "transitions before captions" is a number, not a convention.
  * Prerequisites are Intents an expander asks for (transcribe, music,
    captions, reframe) and are added ONCE — the same recipe requested twice
    merges its slots, later non-empty values winning.
  * Never two transcription passes: when captions must `auto_caption` (a
    language change or model upgrade) that step transcribes, so any
    `transcribe` step is dropped.
  * Feature gates: a step whose tool is not in `facts.tools_available` is
    dropped when optional, else it becomes a blocking `gate_<tool>`
    question (skip / abort). `auto_reframe` is exempt: without cv2 the
    recipe already runs it with `subject_track=False`, which the handler
    supports.
  * Downloads and long runs are QUESTIONS (§1.1/§1.4): `downloads` and `go`
    confirms are prepended when needed and never answered by the planner.
  * Caps come from the wire schema (24 steps, 20 postconditions, 4
    questions) and are enforced by trimming the least specific items with
    a note, never by producing an invalid Plan.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

from . import grammar as G
from . import slots as S
from .facts import TimelineFacts
from .expanders import EXPANDERS, audit_expansion, estimate_seconds, expand_auto_edit, step_cost
from .recipes import (ASK, REASK_PREFIX, RECIPE_BY_NAME, Context, Expansion, Intent, consumes_answer,
                      dropped_note, is_blank_answer, normalize_slots, placeholder, pc, reask, was_reasked)
from .recipes import ask as _ask
from .schema import (ARG_REF, LONG_RUN_SECONDS, STAGE_PREREQ, DownloadNeeded, NeedsInput, Plan, Postcondition,
                     Step)

#: Recipes whose steps re-time v1 (so later positional maths is stale).
CUT_RECIPES: frozenset[str] = frozenset({"tighten", "remove_silences", "remove_fillers", "trim", "speed"})

#: Expansion order — prerequisites before dependents, then stage order.
RECIPE_ORDER: tuple[str, ...] = (
    "transcribe", "trim", "speed", "stabilize", "upscale", "remove_silences", "remove_fillers", "tighten",
    "shorts", "color_look", "transitions", "reframe", "captions", "translate_captions", "hook", "title",
    "brand", "end_card", "voiceover", "music", "duck", "beat_sync", "clean_audio", "loudness",
    "export_preset", "_audit", "ask",
)

#: Tools the feature gate never blocks (see module docstring).
GATE_EXEMPT_TOOLS: frozenset[str] = frozenset({"auto_reframe"})

MAX_STEPS, MAX_POSTCONDITIONS, MAX_QUESTIONS = 24, 20, 4

_TITLES: dict[str, str] = {
    "transcribe": "Transcribe", "captions": "Captions", "translate_captions": "Translate captions",
    "remove_silences": "Remove silences", "remove_fillers": "Remove fillers", "tighten": "Tighten",
    "shorts": "Shorts", "reframe": "Reframe", "music": "Music", "duck": "Duck music", "beat_sync": "Beat sync",
    "hook": "Hook", "color_look": "Colour look", "clean_audio": "Clean audio", "loudness": "Loudness",
    "speed": "Speed", "trim": "Trim", "title": "Title", "brand": "Brand kit", "end_card": "End card",
    "transitions": "Transitions", "export_preset": "Export preset", "voiceover": "Voiceover",
    "stabilize": "Stabilise", "upscale": "Upscale", "auto_edit": "Auto edit", "ask": "Question",
}


# --------------------------------------------------------------------------
# 1. grammar hit → Intent
# --------------------------------------------------------------------------

_TITLE_TEXT_RE = re.compile(r"\b(?:title|text|label|heading|headline|super)\s+(?:that says\s+|saying\s+|reading\s+)?(.+?)(?=\s+(?:at|in|on|for|during)\s+the\b|\s+at\s+\d|$)")
#: A `title` clause that asks for a NAME card rather than a headline — the
#: expander then asks for the name (not "what should the title say?") when
#: neither a name nor a handle was given.
_LOWER_THIRD_RE = re.compile(r"\blower[- ]?third\b|\bname\s*(?:tag|plate|card|strap|title|banner)\b|\bnameplate\b"
                             r"|\bintroduce\b|\bintroducing\b|\bname and handle\b|\bspeaker name\b|\bwho'?s talking\b")


def _hit_slots(hit: G.IntentHit, whole: S.Slots) -> dict[str, Any]:
    c, w = hit.slots, whole
    r = hit.intent
    # Clause slots come from the lower-cased clause; the whole-prompt slots
    # keep the user's case, which is what a hook or voiceover must carry.
    q0 = None
    if c.quoted_text:
        q0 = next((q for q in w.quoted_text if q.lower() == c.quoted_text[0].lower()), c.quoted_text[0])
    lang = c.language or w.language
    platform = c.platform or w.platform
    lufs = c.lufs if c.lufs is not None else w.lufs
    if r == "captions":
        return {"style": c.caption_style or w.caption_style, "position": c.caption_position,
                "target": lang, "model_upgrade": c.model_upgrade or w.model_upgrade}
    if r == "translate_captions":
        return {"target_lang": lang}
    if r in ("remove_fillers", "tighten"):
        return {"words": c.filler_words or w.filler_words}
    if r == "shorts":
        return {"count": c.count or w.count, "max_dur": c.duration_s or w.duration_s, "platform": platform}
    if r == "reframe":
        return {"ratio": c.ratio or w.ratio, "platform": platform}
    if r == "music":
        return {"mood": c.mood or w.mood, "_replace": c.replace_existing}
    if r == "duck":
        return {"_mood": w.mood}
    if r == "beat_sync":
        return {"_mood": w.mood}
    if r == "hook":
        dur = c.range.end if (c.range and c.range.kind == "first") else c.duration_s
        return {"text": q0, "duration_s": dur}
    if r == "color_look":
        return {"look": c.look or w.look}
    if r in ("clean_audio", "loudness"):
        return {"lufs": lufs, "_platform": platform}
    if r == "speed":
        return {"factor": c.speed or w.speed, "clip_ref": c.clip_ref, "_smooth": c.smooth}
    if r == "trim":
        return {"range": c.range or w.range}
    if r == "title":
        # A proper-noun run is a NAME only on a name card: `NAME_RE` also
        # reads "a title that says Big Launch" as name="Big Launch", which is
        # headline text, not a person.
        is_name_card = bool(_LOWER_THIRD_RE.search(hit.clause))
        name = c.name if is_name_card else None
        text = q0
        if text is None and not name:
            m = _TITLE_TEXT_RE.search(hit.clause)
            if m and len(m.group(1).split()) <= 8:
                text = m.group(1).strip()
        return {"text": text, "name": name, "handle": c.handle, "dur": c.duration_s,
                "at": "end" if c.at_end else ("start" if c.at_start else None),
                "_style": "label_tag" if c.caption_position == "top" else "bold_pop",
                "_lower_third": is_name_card}
    if r == "brand":
        return {"handle": c.handle or w.handle, "hashtags": c.hashtags or w.hashtags,
                "palette": (c.color_hex,) if c.color_hex else ()}
    if r == "end_card":
        return {"handle": c.handle or w.handle}
    if r == "transitions":
        return {"look": c.transition_look, "type": c.transition_type, "at": c.transition_at,
                "duration": c.duration_s if c.transition_type else None}
    if r == "export_preset":
        return {"platform": platform, "_ratio": c.ratio or w.ratio}
    if r == "voiceover":
        return {"text": q0, "voice": c.voice or w.voice, "_at_end": c.at_end}
    if r in ("stabilize", "upscale"):
        return {"clip_ref": c.clip_ref, "upscale_factor": c.upscale_factor or w.upscale_factor}
    if r == "auto_edit":
        return {"platform": platform, "language": lang, "mood": c.mood or w.mood, "look": c.look or w.look,
                "_ratio": c.ratio or w.ratio, "_template": hit.template, "_caption_style": w.caption_style,
                "_caption_position": w.caption_position, "_hook_text": None, "_lufs": lufs,
                # "make this a 30s reel": the target length used to be extracted
                # and then dropped on the floor — no trim, no check, no note.
                "_duration_s": c.duration_s or w.duration_s}
    return {}


def bind(hit: G.IntentHit, whole: S.Slots) -> Intent:
    raw = {k: v for k, v in _hit_slots(hit, whole).items() if v not in (None, (), "")}
    return Intent(hit.intent, normalize_slots(hit.intent, raw) if hit.intent in RECIPE_BY_NAME else raw,
                  score=hit.score, clause=hit.clause)


# --------------------------------------------------------------------------
# 2. compose
# --------------------------------------------------------------------------

def _merge_intents(intents: Iterable[Intent]) -> list[Intent]:
    """One Intent per recipe; later non-empty slot values win."""
    by_name: dict[str, Intent] = {}
    order: list[str] = []
    for it in intents:
        if it.recipe in by_name:
            prev = by_name[it.recipe]
            merged = {**prev.slots, **{k: v for k, v in it.slots.items() if v not in (None, (), "")}}
            by_name[it.recipe] = Intent(it.recipe, merged, max(prev.score, it.score), prev.clause or it.clause)
        else:
            by_name[it.recipe] = it
            order.append(it.recipe)
    return [by_name[n] for n in order]


def _ordered(intents: list[Intent]) -> list[Intent]:
    rank = {name: i for i, name in enumerate(RECIPE_ORDER)}
    return sorted(intents, key=lambda it: rank.get(it.recipe, len(rank)))


def _expand_all(intents: list[Intent], facts: TimelineFacts, exclusions: frozenset[str],
                hook_text: tuple[str, str] | None, allow_downloads: bool) -> tuple[list[Intent], dict[str, Expansion]]:
    """Expand with prerequisites resolved to a fixpoint (bounded)."""
    current = _merge_intents(intents)
    for _ in range(6):
        names = frozenset(it.recipe for it in current)
        ctx = Context(recipes=names, exclusions=exclusions,
                      has_cut_steps=bool(names & CUT_RECIPES),
                      hook_text=hook_text, allow_downloads=allow_downloads)
        expansions: dict[str, Expansion] = {}
        wanted: list[Intent] = []
        for it in _ordered(current):
            if it.recipe == "_audit":
                expansions[it.recipe] = audit_expansion()
                continue
            expansions[it.recipe] = EXPANDERS[it.recipe](it, facts, ctx)
            wanted.extend(p for p in expansions[it.recipe].prerequisites
                          if p.recipe not in names and p.recipe not in exclusions)
        if not wanted:
            return _ordered(current), expansions
        current = _merge_intents([*current, *wanted])
    return _ordered(current), expansions


def _dedupe_steps(steps: list[Step]) -> list[Step]:
    seen: set[str] = set()
    out: list[Step] = []
    for s in steps:
        key = s.tool + json.dumps(s.args, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _dedupe_postconditions(pcs: list[Postcondition]) -> list[Postcondition]:
    seen: set[str] = set()
    out: list[Postcondition] = []
    for p in pcs:
        key = p.check + json.dumps(p.args, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    if len(out) > MAX_POSTCONDITIONS:
        out = [p for p in out if p.check != "tool_ok"]
    if len(out) > MAX_POSTCONDITIONS:
        keep = [p for p in out if p.headline]
        out = (keep + [p for p in out if not p.headline])[:MAX_POSTCONDITIONS]
    return out


def _trim_steps(steps: list[Step]) -> tuple[list[Step], list[str]]:
    """Enforce the 24-step cap by thinning the most repeated tool (split_at,
    add_transition, add_keyframe fan-outs) rather than dropping a recipe."""
    notes: list[str] = []
    while len(steps) > MAX_STEPS:
        counts: dict[str, int] = {}
        for s in steps:
            counts[s.tool] = counts.get(s.tool, 0) + 1
        tool, n = max(counts.items(), key=lambda kv: kv[1])
        if n <= 1:
            dropped = steps.pop()
            notes.append(f"dropped {dropped.tool} to fit the 24-step limit")
            continue
        idx = max(i for i, s in enumerate(steps) if s.tool == tool)
        steps.pop(idx)
        if not notes or not notes[-1].startswith(f"fewer {tool}"):
            notes.append(f"fewer {tool} steps to fit the 24-step limit")
    return steps, notes


def _gate_steps(steps: list[Step], facts: TimelineFacts) -> tuple[list[Step], list[NeedsInput], list[str]]:
    kept: list[Step] = []
    questions: list[NeedsInput] = []
    notes: list[str] = []
    asked: set[str] = set()
    for s in steps:
        if s.tool in facts.tools_available or s.tool in GATE_EXEMPT_TOOLS or not facts.tools_available:
            kept.append(s)
            continue
        if s.optional:
            notes.append(f"{s.tool} skipped — not available on this machine (see /api/features for the fix)")
            continue
        kept.append(s)
        if s.tool not in asked:
            asked.add(s.tool)
            # Options are OUTCOMES a user can choose between, not verbs from
            # the plan format: the raw `skip` / `abort` chips (plus a CUDA
            # command as the only explanation) read as a developer dialog.
            # The technical fix stays reachable through /api/features.
            questions.append(_ask(f"gate_{s.tool}"[:32],
                                  f"{_TOOL_HUMAN.get(s.tool, s.tool)} is not available on this machine. "
                                  f"Continue without it, or stop?",
                                  options=[("skip", f"Continue without {_TOOL_HUMAN.get(s.tool, s.tool)}",
                                            "The rest of the edit still runs"),
                                           ("abort", "Stop", "Nothing changes; see the AI panel for the fix")]))
    return kept, questions, notes


#: Plain names for tools a gate question may name — the user reads these.
_TOOL_HUMAN: dict[str, str] = {
    "auto_caption": "auto captions", "transcribe": "transcription", "add_caption_track": "captions",
    "noise_reduce": "noise removal", "stabilize": "stabilisation", "upscale": "upscaling",
    "smooth_slow_motion": "smooth slow motion", "translate_captions": "caption translation",
    "tts_voiceover": "the voiceover", "make_shorts": "shorts",
}


def _human_minutes(seconds: float) -> str:
    if seconds < 90:
        return f"{int(round(seconds))} seconds"
    return f"{max(1, int(round(seconds / 60)))} minutes"


def compose(intents: list[Intent], facts: TimelineFacts, *, exclusions: frozenset[str] = frozenset(),
            confidence: float = 1.0, reply_prefix: str | None = None,
            hook_text: tuple[str, str] | None = None, allow_downloads: bool = True,
            brain: str = "recipes", extra_questions: list[NeedsInput] | None = None) -> Plan:
    """Intents → a complete, stage-ordered Plan. Pure; never touches the store."""
    flat: list[Intent] = []
    for it in intents:
        if it.recipe == "auto_edit":
            flat.extend(expand_auto_edit(it, facts, exclusions))
        elif it.recipe not in exclusions:
            flat.append(it)
    flat = [it for it in flat if it.recipe not in exclusions]
    flat = _reconcile(flat)
    ordered, expansions = _expand_all(flat, facts, exclusions, hook_text, allow_downloads)

    steps: list[Step] = []
    postconditions: list[Postcondition] = []
    questions: list[NeedsInput] = list(extra_questions or [])
    downloads: list[DownloadNeeded] = []
    notes: list[str] = []
    content_brain: str | None = None
    for it in ordered:
        x = expansions[it.recipe]
        steps.extend(x.steps)
        postconditions.extend(x.postconditions)
        questions.extend(x.questions)
        downloads.extend(x.downloads)
        notes.extend(x.notes)
        content_brain = content_brain or x.content_brain

    # Never two transcription passes (§2.5).
    if any(s.tool == "auto_caption" and s.stage == STAGE_PREREQ for s in steps):
        steps = [s for s in steps if s.tool != "transcribe"]
        downloads = [d for d in downloads if d.tool != "transcribe"]

    steps = _dedupe_steps(steps)
    steps.sort(key=lambda s: (s.stage if s.stage is not None else 12,))   # stable → original order within a stage
    steps, gate_questions, gate_notes = _gate_steps(steps, facts)
    steps, trim_notes = _trim_steps(steps)
    notes.extend(gate_notes + trim_notes)
    questions.extend(gate_questions)

    est = estimate_seconds(steps, facts)
    front: list[NeedsInput] = []
    if downloads and allow_downloads:
        total = sum(d.bytes for d in downloads)
        listing = "; ".join(f"{d.what} for {d.tool}" for d in downloads)
        front.append(_ask("downloads", f"First use downloads {listing} ({total / 1e9:.1f} GB total). Reply download or skip.",
                          kind="confirm", options=[("yes", "Download"), ("no", "Skip")]))
    if est > LONG_RUN_SECONDS and steps:
        heavy = max(steps, key=lambda s: _step_weight(s, facts))
        front.append(_ask("go", f"This will take about {_human_minutes(est)} ({heavy.why}). Start?",
                          kind="confirm", options=[("yes", "Start"), ("no", "Cancel")]))
    questions = _cap_questions(front + questions)

    postconditions = _dedupe_postconditions(postconditions)
    recipes_in = [it.recipe for it in ordered if it.recipe not in ("_audit",)]
    headline = "auto_edit" if any(i.recipe == "auto_edit" for i in intents) else "+".join(recipes_in)
    title = " · ".join(_TITLES.get(r, r) for r in ([headline] if headline == "auto_edit" else recipes_in))[:80]
    reply_parts = ([reply_prefix] if reply_prefix else []) + notes
    reply = "; ".join(dict.fromkeys(p for p in reply_parts if p))[:400] or None
    return Plan.new(intent=headline[:64] or "noop", brain=brain, steps=steps, needs_input=questions,
                    postconditions=postconditions, confidence=max(0.0, min(1.0, confidence)),
                    title=title or None, downloads_needed=downloads[:6], estimated_seconds=est,
                    content_brain=content_brain, reply=reply)


def _reconcile(intents: list[Intent]) -> list[Intent]:
    """§2.5 conflicts: an explicit reframe ratio wins over the export
    preset's aspect (the preset is re-derived by `_preset_for`)."""
    reframe = next((it for it in intents if it.recipe == "reframe" and it.get("ratio")), None)
    if reframe is None:
        return intents
    return [Intent(it.recipe, {**it.slots, "_ratio": reframe.get("ratio")}, it.score, it.clause)
            if it.recipe == "export_preset" else it for it in intents]


def _step_weight(s: Step, facts: TimelineFacts) -> float:
    return step_cost(s, facts)


def _cap_questions(questions: list[NeedsInput]) -> list[NeedsInput]:
    seen: set[str] = set()
    uniq: list[NeedsInput] = []
    for q in questions:
        if q.key in seen:
            continue
        seen.add(q.key)
        uniq.append(q)
    if len(uniq) <= MAX_QUESTIONS:
        return uniq
    blocking = [q for q in uniq if q.pauses]
    rest = [q for q in uniq if not q.pauses]
    return (blocking + rest)[:MAX_QUESTIONS]


# --------------------------------------------------------------------------
# 3. plan(prompt)
# --------------------------------------------------------------------------

def _ask_reply(facts: TimelineFacts) -> str:
    bits = [f"{facts.duration:.1f}s, {facts.canvas_w}×{facts.canvas_h} ({facts.aspect}) at {facts.fps} fps",
            f"{len(facts.v1_clip_ids)} clip(s) on v1"]
    if facts.has_transcript:
        bits.append(f"transcript: {facts.words} words" + (f" ({facts.language})" if facts.language else "")
                    + (f", {facts.filler_count} fillers" if facts.filler_count else ""))
    elif facts.transcript_pending:
        bits.append("transcript: still being made")
    else:
        bits.append("no transcript yet")
    bits.append("captions: " + (facts.caption_style or "yes" if facts.has_captions else "none"))
    bits.append("music: " + ("ducked" if facts.music_ducked else "yes") if facts.has_music else "music: none")
    if facts.loudness_lufs is not None:
        bits.append(f"loudness target {facts.loudness_lufs:g} LUFS")
    if facts.brand_handle:
        bits.append(f"brand {facts.brand_handle}")
    return "; ".join(bits)[:400]


def _guesses(prompt: str) -> list[tuple[str, str]]:
    text = S.normalize(prompt)
    found: list[str] = []
    for intent, rows in G.PHRASES.items():
        if intent in ("ask", "undo", "redo"):
            continue
        for pat, _score in rows:
            if re.search(pat, text):
                found.append(intent)
                break
    for fallback in ("captions", "tighten", "auto_edit"):
        if fallback not in found:
            found.append(fallback)
    return [(i, _TITLES.get(i, i)) for i in found[:3]]


def plan(prompt: str, facts: TimelineFacts, *, hook_text: tuple[str, str] | None = None,
         allow_downloads: bool = True) -> Plan:
    """The recipes brain (§1.2): grammar → intents → recipes → steps."""
    det = G.detect(prompt)
    conf = det.confidence
    if det.hits and det.hits[0].intent in ("undo", "redo"):
        verb = det.hits[0].intent
        return Plan.new(intent=verb, brain="recipes", confidence=conf, title=verb.title(),
                        reply=f"{'Undoing' if verb == 'undo' else 'Redoing'} the last edit.")
    if det.hits and all(h.intent == "ask" for h in det.hits):
        return Plan.new(intent="ask", brain="recipes", confidence=conf, title="Question", reply=_ask_reply(facts))
    hits = [h for h in det.hits if h.intent != "ask"]
    if not hits:
        if det.exclusions and not det.unmatched:
            return Plan.new(intent="noop", brain="recipes", confidence=conf, title="Nothing to do",
                            reply="Nothing to change — you only said what not to do.")
        return Plan.new(
            intent="clarify", brain="recipes", confidence=0.0, title="Which edit?",
            needs_input=[_ask("intent", "I did not catch that. Which of these did you mean?",
                              options=_guesses(prompt))],
            reply="I did not understand that request.")
    intents = [bind(h, det.slots) for h in hits]
    exclusions = frozenset(x for x in det.exclusions if x in RECIPE_BY_NAME)
    prefix = None
    if G.NORMALISE_THRESHOLD <= conf < G.RUN_THRESHOLD:
        prefix = "I read that as: " + ", ".join(_TITLES.get(h.intent, h.intent) for h in hits)
    p = compose(intents, facts, exclusions=exclusions, confidence=conf, reply_prefix=prefix,
                hook_text=hook_text, allow_downloads=allow_downloads, brain="recipes")
    if conf < G.NORMALISE_THRESHOLD:
        # §2.7: below 0.4 the plan is a guess — ask, offering it as the first option.
        return p.with_(steps=[], postconditions=[], downloads_needed=[], estimated_seconds=None,
                       needs_input=[_ask("intent", "I am not sure what you meant. Which of these?",
                                         options=[(h.intent, _TITLES.get(h.intent, h.intent)) for h in hits][:3]
                                         + [g for g in _guesses(prompt) if g[0] not in {h.intent for h in hits}])],
                       intent="clarify")
    return p


# --------------------------------------------------------------------------
# 4. apply_answers — the resume path (§4.3)
# --------------------------------------------------------------------------

_NO = {"no", "skip", "n", "false", "nahi", "cancel", "abort", False, 0}
_YES = {"yes", "download", "go", "start", "y", "true", "haan", "ha", "ok", True, 1}


def _fill(value: Any, answers: dict[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith(ASK):
        key = value[len(ASK):]
        return answers.get(key, value)
    if isinstance(value, dict):
        return {k: _fill(v, answers) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, answers) for v in value]
    return value


def without_downloads(p: Plan, facts: TimelineFacts) -> Plan:
    """The plan a "skip" answer to the downloads question produces (§1.4):
    dependent steps degrade (cached model, spoken language, cached voice) or
    drop, and the reply says so. Pure plan transform, so it works for a plan
    from any brain."""
    steps: list[Step] = []
    notes: list[str] = []
    for s in p.steps:
        args = dict(s.args)
        if s.tool in ("transcribe", "auto_caption"):
            model = str(args.get("model") or ("small" if s.tool == "transcribe" else facts.whisper_cached_best()))
            if not facts.is_cached(f"whisper:{model}"):
                args["model"] = facts.whisper_cached_best()
                notes.append(f"{s.tool} uses the {args['model']} model ({model} not downloaded)")
            if s.tool == "auto_caption" and args.get("target") in ("hi", "hinglish", "es") and not facts.is_cached("madlad"):
                notes.append(f"captions stay in the spoken language ({args['target']} translation model not downloaded)")
                args.pop("target")
        elif s.tool == "translate_captions" and not facts.is_cached("madlad"):
            notes.append("translation skipped (model not downloaded)")
            continue
        elif s.tool == "tts_voiceover":
            voice = str(args.get("voice") or "en_US-amy-medium")
            if not facts.is_cached(f"piper:{voice}"):
                cached = next((v for v in ("en_US-amy-medium", "en_US-ryan-medium", "en_GB-alan-medium",
                                           "hi_IN-priyamvada-medium", "hi_IN-pratham-medium")
                               if facts.is_cached(f"piper:{v}")), None)
                if cached is None:
                    notes.append("voiceover skipped (no voice downloaded)")
                    continue
                args["voice"] = cached
                notes.append(f"voiceover uses the {cached} voice ({voice} not downloaded)")
        steps.append(s.model_copy(update={"args": args}))
    dropped_tools = {s.tool for s in p.steps} - {s.tool for s in steps}
    pcs = [c for c in p.postconditions
           if not (c.check == "captions_language" and ("translate_captions" in dropped_tools
                                                        or not any(s.args.get("target") for s in steps if s.tool == "auto_caption")))]
    reply = "; ".join(dict.fromkeys([*(p.reply.split("; ") if p.reply else []), *notes]))[:400] or None
    return p.with_(steps=steps, postconditions=pcs, downloads_needed=[], reply=reply,
                   needs_input=[q for q in p.needs_input if q.key != "downloads"])


def _sort_answers(p: Plan, answers: dict[str, Any]
                  ) -> tuple[dict[str, Any], list[NeedsInput], list[NeedsInput], list[NeedsInput]]:
    """(values, remaining, reasked, dropped) for the plan's questions.

    An EMPTY answer to a blocking question is refused once (the question comes
    back marked, see recipes.reask) and ends the step on the second try —
    never a third round of the same question. A key absent from `answers`
    is merely unanswered and is asked again unchanged: a client that resumes
    with a partial answer set has not refused anything."""
    values: dict[str, Any] = {}
    remaining: list[NeedsInput] = []
    reasked: list[NeedsInput] = []
    dropped: list[NeedsInput] = []
    for q in p.needs_input:
        given = answers.get(q.key)
        if q.key in answers and is_blank_answer(given) and q.pauses:
            (dropped if was_reasked(q) else reasked).append(q)
        elif q.key in answers and not is_blank_answer(given):
            values[q.key] = given
        elif q.default is not None:
            values[q.key] = q.default
        else:
            remaining.append(q)
    return values, remaining, reasked, dropped


def _drop_unanswered(steps: list[Step], pcs: list[Postcondition], dropped: list[NeedsInput]
                     ) -> tuple[list[Step], list[Postcondition], list[str]]:
    """The plan without the steps (and their `$arg:`/`$ask:` checks) that
    waited on a twice-refused answer, plus the honest notes."""
    notes: list[str] = []
    for q in dropped:
        gone = [s for s in steps if consumes_answer(s.args, q.key)]
        steps = [s for s in steps if s not in gone]
        refs = {placeholder(q.key), f"{ARG_REF}{q.key}"}
        pcs = [c for c in pcs if not any(v in refs for v in c.args.values() if isinstance(v, str))]
        notes.append(dropped_note(q, [s.why for s in gone]))
    return steps, pcs, notes


def _with_notes(p: Plan, notes: list[str]) -> Plan:
    """`notes` appended to the reply. A refusal note from an earlier pause
    (recipes.REASK_PREFIX) is dropped first: it described THAT pause, and
    on this resume the question was either answered or given up on."""
    kept = [part for part in (p.reply.split("; ") if p.reply else []) if not part.startswith(REASK_PREFIX)]
    reply = "; ".join(dict.fromkeys([*kept, *notes]))[:400] or None
    if reply == p.reply:
        return p
    return p.with_(reply=reply)


def apply_answers(p: Plan, answers: dict[str, Any], facts: TimelineFacts) -> Plan:
    """Resolve a paused plan with `answers` (key → value). Unanswered questions
    take their default; a still-missing required answer keeps its question so
    the caller pauses again. Returns a validated Plan; `steps == []` after a
    "no" to `go` or an "abort" to a gate means the caller should not run."""
    from .validate import validate_plan
    values, remaining, reasked, dropped = _sort_answers(p, answers)
    out = p
    if "downloads" in values and values["downloads"] in _NO:
        out = without_downloads(out, facts)
    if "go" in values and values["go"] in _NO:
        return out.with_(steps=[], postconditions=[], needs_input=[], estimated_seconds=None,
                         reply="Cancelled — the timeline is unchanged.")
    steps = list(out.steps)
    for key, val in values.items():
        if key.startswith("gate_"):
            tool = key[len("gate_"):]
            if val in _NO or val == "abort":
                if val == "abort" or val in ("no", False):
                    return out.with_(steps=[], postconditions=[], needs_input=[],
                                     reply=f"Aborted at your request — {tool} is not available here.")
            steps = [s for s in steps if s.tool != tool]
            values[key] = "skip"
    subs = {k: v for k, v in values.items() if not k.startswith("gate_") and k not in ("downloads", "go", "intent")}
    filled: list[Step] = []
    for s in steps:
        args = _fill(dict(s.args), subs)
        # The other binding convention (pending.py): a step arg NAMED like the
        # key and left None receives the answer (`translate_captions(target_lang=None)`).
        for key, val in subs.items():
            if key in args and args[key] is None:
                args[key] = val
        if s.tool == "cut_range" and "range" in subs:
            rng = S.extract(f"cut {subs['range']}").range if isinstance(subs["range"], str) else None
            if rng is None:
                remaining.append(next((q for q in p.needs_input if q.key == "range"), None) or
                                 _ask("range", "Which part should I cut? e.g. 'the first 5 seconds'.", kind="text"))
            else:
                a, b = rng.resolve(facts.duration)
                args["start"], args["end"] = round(a, 3), round(b, 3)
        filled.append(s.model_copy(update={"args": args}))
    # The refusal itself is the re-asked question's text (recipes.reask) —
    # the pause shows that question AND the reply, so no second copy here.
    filled, pcs, notes = _drop_unanswered(filled, list(out.postconditions), dropped)
    settled = set(values) | {q.key for q in reasked} | {q.key for q in dropped}
    kept_q = ([q for q in out.needs_input if q.key not in settled] + [reask(q) for q in reasked]
              + [q for q in remaining if q is not None])
    kept_q = _cap_questions(kept_q)
    result = _with_notes(out.with_(steps=filled, postconditions=pcs, needs_input=kept_q), notes)
    if not result.blocking_questions:
        return validate_plan(result, facts)
    return result


__all__ = ["CUT_RECIPES", "RECIPE_ORDER", "GATE_EXEMPT_TOOLS", "bind", "compose", "plan",
           "without_downloads", "apply_answers"]
