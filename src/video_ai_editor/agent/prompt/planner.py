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
import math
import re
from typing import Any, Iterable

from . import clip_slots as CS
from . import grammar as G
from . import slots as S
from .facts import TimelineFacts
from .langs import needs_translation
from .expanders import EXPANDERS, audit_expansion, estimate_seconds, expand_auto_edit, step_cost
from .recipes import (ASK, REASK_PREFIX, RECIPE_BY_NAME, Context, Expansion, Intent, consumes_answer,
                      dropped_note, is_blank_answer, normalize_slots, placeholder, pc, reask, was_reasked)
from .recipes import ask as _ask
from .schema import (ARG_REF, LONG_RUN_SECONDS, STAGE_PREREQ, DownloadNeeded, NeedsInput, Plan, Postcondition,
                     Step)

#: Recipes whose steps re-time v1 (so later positional maths is stale).
CUT_RECIPES: frozenset[str] = frozenset({"tighten", "remove_silences", "remove_fillers", "trim", "speed",
                                         # wave D3 (E3): they move or remove clips
                                         "delete_clip", "duplicate", "move_clip", "freeze",
                                         # wave E (F4b): a clip's new length re-times what follows
                                         "clip_length"})

#: Expansion order — prerequisites before dependents, then stage order.
RECIPE_ORDER: tuple[str, ...] = (
    "transcribe", "trim", "clip_length", "delete_clip", "duplicate", "move_clip", "split", "freeze", "speed",
    "reverse", "stabilize", "upscale", "remove_silences", "remove_fillers", "tighten", "zoom", "rotate", "flip",
    "adjust", "remove_feature", "blend",
    "shorts", "color_look", "transitions", "reframe", "canvas", "captions", "translate_captions", "hook", "title",
    "retext",
    "brand", "end_card", "voiceover", "remove_music", "music", "duck", "beat_sync", "fit_music", "fade", "volume",
    "mute", "voice_effect", "animation",
    "clean_audio", "loudness", "export_preset", "_audit", "preview", "ask",
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
    "fade": "Fade", "volume": "Volume", "mute": "Mute", "fit_music": "Fit music", "audit": "Audit",
    "remove_music": "Remove music", "reverse": "Reverse", "freeze": "Freeze", "split": "Split",
    "delete_clip": "Delete clip", "duplicate": "Duplicate", "move_clip": "Move clip", "zoom": "Zoom",
    "rotate": "Rotate", "adjust": "Adjust", "sticker": "Sticker", "remove_feature": "Remove", "flip": "Flip",
    "clip_length": "Clip length", "retext": "Edit text", "transform": "Transform",
    "canvas": "Canvas", "blend": "Blend", "voice_effect": "Voice effect", "animation": "Animation",
    "_audit": "Audit", "preview": "Preview",
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


#: The name a lower-third clause gives ("lower third: Jane Doe", "a lower
#: third saying Jane Doe, Producer", "a lower third for Jane Doe").
_LOWER_THIRD_TEXT_RE = re.compile(
    r"\blower[- ]?third\b(?:\s*[:—-]\s*|\s+(?:saying|that says|which says|reading|showing|for|with(?: the name)?"
    r"|of)\s+)(?!(?:the|a|an|this|that|it|my|clip|video)\b)(.{1,80}?)"
    r"(?=\s+(?:at|from|for|during|in|on)\s+(?:the\s+)?(?:\d|start|end|beginning|first|last)|$)")


def _original_case(text: str, prompt: str | None) -> str:
    """`text` as the user TYPED it (QA-073). Clause slots come from the
    lower-cased clause, so "a title that says BIG SALE" became "big sale";
    the words are found again in the original prompt, whitespace-tolerant,
    and returned with their case. Unchanged when they cannot be found (a
    Hinglish verb the normaliser rewrote, say)."""
    if not prompt or not text:
        return text
    pattern = r"\s+".join(re.escape(w) for w in text.split())
    m = re.search(pattern, prompt.replace("’", "'"), flags=re.IGNORECASE)
    return m.group(0) if m else text


def _hit_slots(hit: G.IntentHit, whole: S.Slots, prompt: str | None = None) -> dict[str, Any]:
    """The recipe slots one grammar hit carries. The clip-level readings
    (which clip, which range — wave D3, E3) come from `clip_slots`."""
    if hit.intent in CS.READERS:
        return CS.READERS[hit.intent](hit, hit.slots)
    out = _base_slots(hit, whole, prompt)
    extras = CS.clip_extras(hit.intent, hit, hit.slots)
    return {**out, **{k: v for k, v in extras.items() if v is not None}}


def _base_slots(hit: G.IntentHit, whole: S.Slots, prompt: str | None = None) -> dict[str, Any]:
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
                "target": lang, "model_upgrade": c.model_upgrade or w.model_upgrade,
                # Final QA: "make the captions yellow / bigger" re-laid every cue
                # unchanged; the look is its own step (set_caption_style).
                "_look": _caption_look_of(hit.clause), "_new": _captions_new(hit.clause)}
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
        # QA-031: the negation lives in the clause ("turn off ducking").
        out = {"_mood": w.mood, "enabled": False if G.duck_off(hit.clause) else None}
        db = _DB_RE.search(hit.clause)
        if db and db.group("n") and not db.group("by"):
            v = float(db.group("n"))
            out["to_db"] = -abs(v)
        elif _DUCK_DEEPER_RE.search(hit.clause):
            out["_deeper"] = -6.0            # Final QA r3: "duck the music more" changed nothing
        elif _DUCK_LIGHTER_RE.search(hit.clause):
            out["_deeper"] = 6.0
        return out
    if r in ("fade", "volume", "mute", "fit_music"):
        return _level_slots(r, hit.clause, c)
    if r == "beat_sync":
        return {"_mood": w.mood}
    if r == "hook":
        dur = c.range.end if (c.range and c.range.kind == "first") else c.duration_s
        return {"text": q0, "duration_s": dur}
    if r == "color_look":
        # Final QA r2: the look words are read whatever the verb — "make this
        # clip black and white" read no look (only "make it <look>" did) and
        # the expander's default applied the teal-orange cinematic LUT.
        return {"look": c.look or S.look_of(hit.clause) or w.look}
    if r == "loudness" and lufs is None and not platform:
        # "turn the volume down" / "make it louder": a direction, relative to
        # the current target — it used to set the SAME target (a no-op that
        # still verified) whichever way the user asked.
        down, up = bool(_LOUD_DOWN_RE.search(hit.clause)), bool(_LOUD_UP_RE.search(hit.clause))
        return {"_change": "down" if down and not up else ("up" if up and not down else None)}
    if r in ("clean_audio", "loudness"):
        return {"lufs": lufs, "_platform": platform}
    if r == "speed":
        if _SPEED_RESET_RE.search(hit.clause):
            # Final QA: "reset the speed", "normal speed", "remove the speed
            # change" set 1.25x (the default factor) — even on a 1x clip.
            # Normal is 1x, and a factor of 1 also clears a curve.
            return {"factor": 1.0, "preset": None, "clip_ref": c.clip_ref, "_smooth": False, "_curve": False}
        # Review RD2: a named curve is its preset; "speed ramp/curve" with no
        # name asks which (`_curve`) — never a constant factor.
        preset = _speed_preset_in(hit.clause)
        return {"factor": None if preset else (c.speed or w.speed), "preset": preset, "clip_ref": c.clip_ref,
                "_smooth": c.smooth, "_curve": bool(_CURVE_WORD_RE.search(hit.clause))}
    if r == "freeze":
        return {"at": _at_seconds(hit.clause), "duration_s": c.duration_s}
    if r == "split":
        # Final QA r3: the clip a split names, and "in half" (its midpoint)
        ref = G.clip_ref_of(hit.clause)
        return {"at": _at_seconds(hit.clause), "clip_ref": None if ref == "$v1_all" else ref,
                "_half": bool(re.search(r"\bin(?:to)?\s+(?:half|two|2)\b|\bdown the middle\b", hit.clause))}
    if r == "reverse":
        return {"clip_ref": c.clip_ref, "reverse": not G.reverse_off(hit.clause)}
    if r == "trim":
        # Final QA r3: "keep only the first 10 seconds" is the range to KEEP —
        # it used to be cut, deleting exactly what the user wanted.
        return {"range": c.range or w.range, "_keep": bool(_KEEP_RE.search(hit.clause))}
    if r == "title":
        # A proper-noun run is a NAME only on a name card: `NAME_RE` also
        # reads "a title that says Big Launch" as name="Big Launch", which is
        # headline text, not a person.
        is_name_card = bool(_LOWER_THIRD_RE.search(hit.clause))
        name = c.name if is_name_card else None
        handle = c.handle
        if is_name_card and not name:
            # Final QA: "lower third: Jane Doe" / "a lower third saying Jane
            # Doe, Producer" asked "Whose name?" — the words after the noun
            # ARE the name; a comma starts the card's second line.
            m = _LOWER_THIRD_TEXT_RE.search(hit.clause)
            if m:
                words = _original_case(m.group(1).strip(" .!?\"'“”"), prompt)
                first, _, rest = words.partition(",")
                name = first.strip() or None
                handle = handle or (rest.strip() or None)
        text = q0
        restyle = False
        if text is None and not name and not is_name_card:
            # Final QA r3: "make the title longer", "move the title to 4
            # seconds", "title from 1s to 4s" retime the user's title — the
            # leftover words were added as a NEW title that replaced it.
            retime = _title_retime(hit.clause)
            if retime is not None:
                return {"text": None, "_retime": retime}
        if text is None and not name:
            m = _TITLE_TEXT_RE.search(hit.clause)
            if m and len(m.group(1).split()) <= 8:
                text = _original_case(m.group(1).strip(), prompt)
                # Final QA r2: "make the title red", "the Summer Trip text
                # bigger", "title font size 80" — words that only describe a
                # LOOK are not a new title's wording. They were added as a
                # title saying "red" that replaced the user's own.
                restyle = _is_style_only(m.group(1), hit.clause)
        at: Any = "end" if c.at_end else ("start" if c.at_start else None)
        dur = c.duration_s
        rng = c.range
        # "over the last 2 seconds" / "for the first 3 seconds" / "from 0:04
        # to 0:07" places the card there (review RD3: it went on at 0-3 s)
        if rng is not None and rng.kind == "last" and rng.end:
            at, dur = "end", float(rng.end)
        elif rng is not None and rng.kind == "first" and rng.end:
            at, dur = "start", float(rng.end)
        elif rng is not None and rng.kind == "abs" and rng.start is not None and rng.end is not None \
                and rng.end > rng.start:
            at, dur = float(rng.start), float(rng.end) - float(rng.start)
        if restyle:
            return {"text": None, "_restyle": True}
        return {"text": text, "name": name, "handle": handle, "dur": dur, "at": at,
                "_style": "label_tag" if c.caption_position == "top" else "bold_pop",
                "_lower_third": is_name_card}
    if r == "retext":
        return _retext_slots(hit.clause, w, prompt)
    if r == "brand":
        return {"handle": c.handle or w.handle, "hashtags": c.hashtags or w.hashtags,
                "palette": (c.color_hex,) if c.color_hex else ()}
    if r == "end_card":
        return {"handle": c.handle or w.handle}
    if r == "transitions":
        out = {"look": c.transition_look, "type": c.transition_type, "at": c.transition_at,
               "duration": c.duration_s if c.transition_type else None}
        # Final QA r2: an EXISTING transition named by its type ("change the
        # glitch transition to a dissolve") is the one to replace — the
        # crossfade next to it was replaced too; "make the transitions
        # longer" changes their length and keeps their types (it replaced
        # both with shorter cross dissolves).
        named = _existing_transition_edit(hit.clause)
        if named:
            out.update(named)
            return out
        t = _TR_RETIME_RE.search(hit.clause)
        if t and not c.transition_type and c.duration_s is None:
            out["_retime"] = 1.5 if (t.group(1) or t.group(2)) in ("longer", "slower") else 1 / 1.5
        return out
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


#: A trim clause that names the part to KEEP (Final QA r3).
_KEEP_RE = re.compile(r"\b(?:only|just)\s+keep\b|\bkeep\s+(?:only\s+|just\s+)?(?:the\s+)?"
                      r"(?:first|last|opening|closing|final|beginning|end)\b|\bleave\s+(?:only|just)\b"
                      r"|\btrim\s+(?:it\s+|this\s+|the\s+video\s+)?down\s+to\b")


#: A speed curve's preset by any spelling ("jump cut", "flash-in", "Hero").
_SPEED_PRESET_RE = re.compile(
    r"\b(montage|hero|bullet|jump[- _]?cut|flash[- _]?in|flash[- _]?out|ramp[- _]?up|ramp[- _]?down)\b")
_CURVE_WORD_RE = re.compile(r"\b(?:ramp|curve)s?\b|\bramping\b")
#: Back to normal speed (Final QA): never the 1.25x default factor.
_SPEED_RESET_RE = re.compile(
    r"\b(?:normal|regular|original|real[- ]?time|default|standard|usual)\s+(?:playback\s+)?speed\b"
    r"|\breset\s+(?:the\s+|its\s+|their\s+)?(?:playback\s+)?speed\b|\bspeed\s+(?:back\s+)?to\s+normal\b"
    r"|\b(?:remove|undo|clear|drop|take\s+(?:off|out)|get\s+rid\s+of)\s+(?:the\s+|its\s+|any\s+)?"
    r"(?:speed\s+(?:change|ramp|curve|up|effect|adjustment)s?|slow[- ]?(?:mo(?:tion)?|down)|speed[- ]?up)\b"
    r"|\bback\s+to\s+(?:1x|1\s*x|100\s*%)|\bun-?(?:speed|slow)\b")
#: "at 3 seconds", "at 0:05", "at 1.5s" (the moment a freeze or a split is at).
_AT_TIME_RE = re.compile(r"\b(?:at|@)\s+(?:(\d{1,2}):(\d{2}(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)?\b)")


#: A retext's new wording follows one of these (the most specific first).
_RETEXT_NEW_RE = re.compile(
    r"\b(?:to\s+say|to\s+read|so\s+(?:that\s+)?it\s+(?:says|reads)|(?:it\s+|that\s+)?should\s+(?:say|read)"
    r"|(?:and\s+)?make\s+it\s+(?:say|read))\s+(.+)$")
_RETEXT_TO_RE = re.compile(r"\b(?:to|with|into)\s+(.+)$")
_RETEXT_NOUN_RE = re.compile(
    r"\b(?:text|title|heading|headline|lower[- ]?third|name\s+(?:card|tag|plate)|label|super)s?\b")
_RETEXT_OLD_RE = re.compile(
    r"\b(?:change|rename|edit|replace|fix|update|correct|retype|reword|swap|alter)\s+"
    r"(?:the\s+|my\s+|that\s+|this\s+|our\s+)?(.*?)\s*\b(?:text|title|heading|headline|lower[- ]?third"
    r"|name\s+(?:card|tag|plate)|label|super)s?\b")
#: Words before the noun that are not the text's own words.
_RETEXT_NOT_A_NAME = re.compile(
    r"\b(?:typo|spelling|mistake|wording|words?|in|on|of|at|first|second|third|last|main|big|small|opening"
    r"|closing|top|bottom|new|old|current|existing|only)\b")


def _retext_slots(clause: str, whole: S.Slots, prompt: str | None) -> dict[str, Any]:
    """(new wording, current wording, which kind of overlay) for a retext,
    in the user's own case (Final QA)."""
    quoted = [q for q in whole.quoted_text]
    new = old = None
    if len(quoted) >= 2:
        old, new = quoted[0], quoted[1]
    else:
        noun = _RETEXT_NOUN_RE.search(clause)
        tail = clause[noun.end():] if noun else clause
        m = _RETEXT_NEW_RE.search(tail) or _RETEXT_TO_RE.search(tail)
        if m:
            new = _original_case(m.group(1).strip(" .,!?:;\"'“”‘’"), prompt)
        if quoted:
            # one quoted phrase: the new words when it is what follows the
            # marker, else the current ones ("change 'SALE' to half price")
            if new and quoted[0].lower() == new.lower().strip("\"'“”‘’"):
                new = quoted[0]
            else:
                old = quoted[0]
        o = _RETEXT_OLD_RE.search(clause)
        if old is None and o and o.group(1).strip() and not _RETEXT_NOT_A_NAME.search(o.group(1)):
            old = _original_case(o.group(1).strip(" \"'“”‘’"), prompt)
    lower = bool(re.search(r"\blower[- ]?third|\bname\s+(?:card|tag|plate)\b", clause))
    return {"text": new or None, "old": old or None, "_role": "lower_third" if lower else None}


#: Colour words a caption look may name → the hex set_caption_style stores.
_COLOUR_HEX: dict[str, str] = {
    "yellow": "#FFD400", "white": "#FFFFFF", "black": "#000000", "red": "#FF3B30", "green": "#34C759",
    "blue": "#0A84FF", "pink": "#FF2D92", "orange": "#FF9500", "purple": "#AF52DE", "cyan": "#32D2FF",
    "gold": "#FFC300", "grey": "#8E8E93", "gray": "#8E8E93", "lime": "#A4FF00", "teal": "#30B0C7",
}
_COLOUR_RE = re.compile(r"\b(" + "|".join(_COLOUR_HEX) + r")\b|(#[0-9a-f]{6})\b")
_BIGGER_RE = re.compile(r"\b(?:bigger|larger|large|huge|increase (?:the )?size|size up|more readable)\b")
_SMALLER_RE = re.compile(r"\b(?:smaller|tinier|less big|reduce (?:the )?size|size down)\b")
_SIZE_PX_RE = re.compile(r"\bsize\s+(?:to\s+)?(\d{2,3})\b|\b(\d{2,3})\s*(?:px|pixels?|pt)\b")
_UPPER_RE = re.compile(r"\b(?:all caps|all-caps|upper ?case|capitals|caps lock|in caps)\b")
_LOWER_CASE_RE = re.compile(r"\b(?:lower ?case|sentence case|normal case|no caps|not all caps)\b")
_NO_OUTLINE_RE = re.compile(r"\b(?:no|remove (?:the )?|without (?:an? )?)(?:outline|stroke|border)\b")
_OUTLINE_RE = re.compile(r"\b(?:outline|stroke|border)\b")
_NO_BOX_RE = re.compile(r"\b(?:no|remove (?:the )?|without (?:an? )?)(?:background|box|backdrop)\b")
_BOX_RE = re.compile(r"\b(?:background|box|backdrop|highlight)\b")
#: Words that only describe how a text LOOKS (Final QA r2).
_STYLE_WORD_RE = re.compile(
    r"(?:" + "|".join(_COLOUR_HEX) + r"|#[0-9a-f]{3,6}|bigger|smaller|larger|large|huge|tiny|big|small"
    r"|font|size|sized|px|pixels?|pt|points?|bold|bolder|italic|underlined?|outlined?|outlines?|stroke"
    r"|shadows?|glow|caps|uppercase|upper|lowercase|lower|case|colou?red|colou?rs?|thicker|thinner|heavier"
    r"|brighter|darker|with|a|an|and|the|in|more|less|much|bit|little|lot|slightly|very|really|border"
    r"|box|background|highlight|\d+(?:\.\d+)?)")
#: A clause that restyles an EXISTING text rather than adding one.
_RESTYLE_VERB_RE = re.compile(r"\b(?:make|turn|change|set|colou?r|recolou?r|resize|style|restyle|give)\b")


#: "change / replace / swap / turn the glitch (transition) (in)to a dissolve".
#: Final QA r3: the noun is optional — "change the crossfade to a wipe"
#: rewrote EVERY transition — and the source must name a transition type.
_TR_DET = r"(?:the\s+|all\s+(?:the\s+)?|every\s+|that\s+|this\s+|my\s+)?"
_TR_CHANGE_RE = re.compile(
    rf"\b(?:change|replace|swap|switch|turn|make|convert)\s+{_TR_DET}"
    r"(?P<src>[\w' -]{2,30}?)(?:\s+(?:transitions?|one|effect))?\s+(?:in)?to\s+(?P<dst>.+)$")
#: "replace the glitch with a crossfade", "swap the glitch for a dissolve".
_TR_WITH_RE = re.compile(
    rf"\b(?:replace|swap|switch|exchange|substitute)\s+{_TR_DET}"
    r"(?P<src>[\w' -]{2,30}?)(?:\s+(?:transitions?|one|effect))?\s+(?:with|for|by)\s+(?P<dst>.+)$")
#: "make the crossfade longer", "make the glitch transition 1 second (long)".
_TR_NAMED_RETIME_RE = re.compile(
    rf"\b(?:make|set|change|turn|lengthen|shorten|extend)\s+{_TR_DET}"
    r"(?P<src>[\w' -]{2,30}?)(?:\s+(?:transitions?|one|effect))?\s+"
    r"(?:(?P<adj>longer|shorter|slower|faster|quicker)\b|(?:(?:to|last|be)\s+)?(?P<n>\d+(?:\.\d+)?)\s*"
    r"(?:s|sec|secs|seconds?)\b(?:\s+long)?)")


def _existing_transition_edit(clause: str) -> dict[str, Any] | None:
    """Slots that change or retime the EXISTING transitions of ONE type the
    clause names by name (Final QA r3), or None."""
    for rx in (_TR_WITH_RE, _TR_CHANGE_RE):
        m = rx.search(clause)
        if m:
            src = S.transition_type_of(m.group("src"))
            dst = S.transition_type_of(m.group("dst"))
            if src and dst and src != dst:
                return {"_from_type": src, "type": dst, "look": None, "at": None}
    m = _TR_NAMED_RETIME_RE.search(clause)
    if m:
        src = S.transition_type_of(m.group("src"))
        if src:
            out: dict[str, Any] = {"_from_type": src, "type": None, "look": None, "at": None}
            if m.group("adj"):
                out["_retime"] = 1.5 if m.group("adj") in ("longer", "slower") else 1 / 1.5
            else:
                out["duration"] = float(m.group("n"))
            return out
    return None
#: The existing transitions' LENGTH: "make the transitions longer / shorter".
_TR_RETIME_RE = re.compile(r"\btransitions?\b.*\b(longer|shorter|slower|faster|quicker)\b"
                           r"|\b(longer|shorter|slower|faster|quicker)\s+transitions?\b")


#: An EXISTING text named in a clause ("the title", "my text", "title from 1s…").
_TEXT_REF_RE = re.compile(r"^(?:the\s+)?(?:title|text|heading|headline)\b"
                          r"|\b(?:the|my|this|that|our|its)\s+(?:[\w']+\s+){0,3}?"
                          r"(?:title|text|heading|headline|label|super|caption text)\b")
_NEW_TEXT_VERB_RE = re.compile(r"\b(?:add|put|write|insert|create|type|another|new|say(?:s|ing)?|reads?|reading)\b")
_TS = r"(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)?"
_RT_RANGE_RE = re.compile(rf"\bfrom\s+{_TS}\s+(?:to|until|till|-)\s+{_TS}\b|\b{_TS}\s*(?:to|-|–)\s*{_TS}\b")
_RT_START_RE = re.compile(
    rf"\b(?:move|shift|push|slide|bring|drag)\b.*?\b(?:to|at)\s+{_TS}\b"
    rf"|\b(?:appear|appears|show\s+up|shows\s+up|come\s+(?:in|on)|comes\s+(?:in|on)|pop\s+(?:in|up)|start|starts"
    rf"|begin|begins|show|shows)\s+(?:at|from|after)\s+{_TS}\b")
_RT_END_RE = re.compile(rf"\b(?:end|ends|disappear|disappears|finish|finishes|go\s+away|goes\s+away|stop|stops"
                        rf"|leave|leaves)\s+(?:at|by)\s+{_TS}\b|\buntil\s+{_TS}\b")
_RT_DUR_RE = re.compile(
    rf"(?<!the )\blasts?\s+(?:for\s+)?{_TS}\b|\b(?:stay|stays|remain|remains|be)\b.*?\bfor\s+{_TS}\b"
    rf"|\b(?:extend|extended|lengthen|shorten|shortened|trim|cut|make|set|change)\b.*?\b(?:to\s+)?{_TS}\s*"
    rf"(?:long\b)?$|\bfor\s+{_TS}\b")
_RT_LONGER_RE = re.compile(r"\b(?:longer|lengthen|extend|stay\s+(?:on\s+)?(?:screen\s+)?longer)\b")
_RT_SHORTER_RE = re.compile(r"\b(?:shorter|shorten)\b")


def _title_retime(clause: str) -> dict[str, float] | None:
    """The new timing of an EXISTING title a clause asks for (Final QA r3), or
    None when the clause is not a retime (it adds, names new words, or says
    no time)."""
    if not _TEXT_REF_RE.search(clause) or _NEW_TEXT_VERB_RE.search(clause) or re.search(r"[\"“]", clause):
        return None
    if m := _RT_RANGE_RE.search(clause):
        a = float(m.group(1) or m.group(3))
        b = float(m.group(2) or m.group(4))
        return {"start": a, "end": b} if b > a else None
    if m := _RT_START_RE.search(clause):
        return {"start": float(next(g for g in m.groups() if g is not None))}
    if m := _RT_END_RE.search(clause):
        return {"end": float(next(g for g in m.groups() if g is not None))}
    if m := _RT_DUR_RE.search(clause):
        return {"dur": float(next(g for g in m.groups() if g is not None))}
    if _RT_LONGER_RE.search(clause):
        return {"factor": 1.5}
    if _RT_SHORTER_RE.search(clause):
        return {"factor": 1 / 1.5}
    return None


def _is_style_only(words: str, clause: str) -> bool:
    toks = [t for t in re.split(r"[\s,]+", words.lower().strip(" .!?")) if t]
    if not toks or not all(_STYLE_WORD_RE.fullmatch(t) for t in toks):
        return False
    # "add a title red" is odd but explicit — only a restyle verb (or none at
    # all: "title font size 80") makes these words a look change
    return bool(_RESTYLE_VERB_RE.search(clause)) or not re.search(r"\b(?:add|put|write|insert|create)\b", clause)


#: A request for NEW captions (a re-lay), not a look change.
_CAPTIONS_NEW_RE = re.compile(r"\b(?:add|generate|create|put|burn|redo|regenerate|re-?transcribe|rebuild|caption it"
                              r"|caption this|auto[- ]?captions?|in (?:hindi|hinglish|english|spanish))\b")


def _caption_look_of(clause: str) -> dict[str, Any]:
    """The caption LOOK a clause asks for (colour, size change, case, outline,
    box, position) — {} when it names none."""
    look: dict[str, Any] = {}
    m = _COLOUR_RE.search(clause)
    if m and not re.search(r"\b(?:background|box|outline|stroke|border)\s+(?:colou?r\s+)?(?:to\s+)?"
                           + re.escape(m.group(0)), clause):
        look["color"] = (m.group(2) or _COLOUR_HEX[m.group(1)]).upper()
    px = _SIZE_PX_RE.search(clause)
    if px:
        look["size"] = float(px.group(1) or px.group(2))
    elif _BIGGER_RE.search(clause):
        look["_grow"] = 1.25
    elif _SMALLER_RE.search(clause):
        look["_grow"] = 0.8
    if _UPPER_RE.search(clause):
        look["upper"] = True
    elif _LOWER_CASE_RE.search(clause):
        look["upper"] = False
    if _NO_OUTLINE_RE.search(clause):
        look["stroke_w"] = 0.0
    elif _OUTLINE_RE.search(clause):
        look["stroke_w"] = 6.0
    if _NO_BOX_RE.search(clause):
        look["background"] = ""
    elif _BOX_RE.search(clause):
        look["background"] = "#000000B3"
    # Final QA r2: "put the captions IN the middle", "captions at the top
    # please" — a place on its own is a look change too (it re-laid every
    # cue in a different style).
    pos = re.search(r"\b(?:to|at|on|in)\s+the\s+(top|bottom|middle|center|centre)\b", clause)
    if pos and (look or re.search(r"\b(?:move|shift|place|position|raise|drop|bring|put|captions?|subtitles?|subs)\b",
                                  clause)):
        look["position"] = {"middle": "center", "centre": "center"}.get(pos.group(1), pos.group(1))
    return look


def _captions_new(clause: str) -> bool:
    """A request for NEW captions (a re-lay). "put" alone is not one when
    the clause also names a look ("put the captions in the middle")."""
    words = {m.group(0) for m in _CAPTIONS_NEW_RE.finditer(clause)}
    if not words:
        # a bare noun ("add smooth transitions and captions" splits into
        # "captions") asks for captions, as it always did
        return bool(re.fullmatch(r"\s*(?:and\s+|also\s+)?(?:some\s+|the\s+)?(?:captions?|subtitles?|subs)"
                                 r"(?:\s+(?:too|as well|please))?\s*", clause))
    return not (words <= {"put"} and _caption_look_of(clause))


def _speed_preset_in(clause: str) -> str | None:
    from ...edl.speed_presets import preset_id
    if re.search(r"\bbullet[- ]?time\b", clause):
        return preset_id("bullet")
    m = _SPEED_PRESET_RE.search(clause)
    if not m or not _CURVE_WORD_RE.search(clause[m.end():m.end() + 24] + " " + clause):
        return None
    return preset_id(m.group(1).replace("-", " ").replace("_", " "))


def _single_time(answer: str) -> float | None:
    """The one moment an answer names ("at 7 seconds", "7s", "0:07"), else None."""
    text = S.normalize(answer or "")
    t = _at_seconds(text)
    if t is not None:
        return t
    m = re.fullmatch(r"\s*(?:(\d{1,2}):(\d{2}(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?))\s*", text)
    if not m:
        return None
    return round(int(m.group(1)) * 60 + float(m.group(2)), 3) if m.group(1) else round(float(m.group(3)), 3)


def _at_seconds(clause: str) -> float | None:
    m = _AT_TIME_RE.search(clause)
    if not m:
        return None
    if m.group(1) is not None:
        return round(int(m.group(1)) * 60 + float(m.group(2)), 3)
    return round(float(m.group(3)), 3)


_MUSIC_WORD_RE = re.compile(r"\b(?:music|song|track|bed|bgm|soundtrack|tune|score)\b")
_VOICE_WORD_RE = re.compile(r"\b(?:voice|vocals?|speech|dialogue|narration|original (?:audio|sound)|clip audio|"
                            r"video (?:audio|sound)|audio|sound|video|clips?|footage)\b")
#: "-20 dB", "to -20db", "at minus 12 dB" (absolute) / "by 6 dB" (a change).
_DB_RE = re.compile(r"(?:(?P<by>\bby\s+)|(?P<to>\b(?:to|at)\s+))?(?P<sign>-|minus\s+|\+|plus\s+)?"
                    r"(?P<n>\d+(?:\.\d+)?)\s*(?:d\s?b|decibels?)\b")
#: "to 50%" (absolute: 100 % is the source's own level, as in CapCut) / "by 30%".
_PCT_RE = re.compile(r"(?P<by>\bby\s+)?(?P<n>\d+(?:\.\d+)?)\s*(?:%|percent\b)")
_DOWN_RE = re.compile(r"\b(?:down|lower|quieter|softer|reduce|decrease|drop|too loud|too high|less"
                      r"|kam|dheere|dheema|dheemi|halka|halki)\b")
_UP_RE = re.compile(r"\b(?:up|louder|raise|increase|boost|higher|too quiet|too soft|too low|more"
                    r"|zyada|jyada|tez|badha\w*)\b")
_LOUD_DOWN_RE = re.compile(r"\b(?:down|lower|quieter|softer|reduce|decrease|too loud|kam)\b")
_LOUD_UP_RE = re.compile(r"\b(?:up|louder|raise|increase|boost|too quiet|too soft|zyada|jyada|tez)\b")
#: One "fade" and the words after it: the FIRST direction word names that
#: fade's edge — "fade out the music in the last 2 seconds" is a fade OUT
#: (the "in" is a preposition), "fade the music in" a fade IN.
_FADE_WORD_RE = re.compile(r"\bfade(?:s|d)?(?:[- ]?(?P<fused>ins?|outs?|up|down))?\b(?P<tail>(?:\s+[\w'-]+){0,5})")
_FADE_DIR = {"in": "in", "ins": "in", "up": "in", "out": "out", "outs": "out", "down": "out", "away": "out"}
_FADE_START_RE = re.compile(r"\bfrom black\b|\bfade\b.*\b(?:start|beginning|intro|opening)\b")
_FADE_END_RE = re.compile(r"\bto black\b|\bfade\b.*\b(?:end|ending|outro|close)\b")


def _fade_edges(clause: str) -> set[str]:
    """The edges ("in" / "out") a fade clause names, from the first direction
    word after each "fade"; "in and out" names both."""
    edges: set[str] = set()
    for m in _FADE_WORD_RE.finditer(clause):
        if m.group("fused"):
            edges.add(_FADE_DIR[m.group("fused")])
            continue
        for word in m.group("tail").split():
            if word in _FADE_DIR:
                edges.add(_FADE_DIR[word])
                break
    if re.search(r"\bin (?:and|&|/) ?out\b|\bin/out\b", clause):
        edges |= {"in", "out"}
    if not edges:
        if _FADE_START_RE.search(clause):
            edges.add("in")
        if _FADE_END_RE.search(clause):
            edges.add("out")
    return edges


#: The voice-over lane named as a level's object (review RE).
_VO_WORD_RE = re.compile(r"\bvoice[- ]?overs?\b|\bvo\b|\bnarration track\b|\bvoice track\b")


def _volume_slots(clause: str, music: bool) -> dict[str, Any]:
    out: dict[str, Any] = {"target": "music" if music or not _VOICE_WORD_RE.search(clause) else "voice"}
    if not music and _VO_WORD_RE.search(clause):
        out["target"] = "vo"
    down, up = bool(_DOWN_RE.search(clause)), bool(_UP_RE.search(clause))
    change = "down" if down and not up else ("up" if up and not down else None)
    m = _DB_RE.search(clause)
    pct = _PCT_RE.search(clause)
    if m:
        n = float(m.group("n"))
        negative = (m.group("sign") or "").strip() in ("-", "minus")
        if m.group("by") or (change and not m.group("to") and not m.group("sign")):
            # Final QA r2: "bring the music down 4db" / "music up 3 db" — a
            # direction word with no "to/at" and no sign is a CHANGE. It was
            # read as a level: -12 dB music went to -4 dB ("down" made it
            # louder) and "up 3 db" set +3 dB.
            out["_delta_db"] = n
            out["change"] = change or "down"
        else:
            out["db"] = -n if negative or (change == "down" and n > 0 and not m.group("sign")) else n
    elif pct and float(pct.group("n")) > 0:
        n = float(pct.group("n"))
        if pct.group("by"):
            ratio = 1.0 - n / 100.0 if (change or "down") == "down" else 1.0 + n / 100.0
            out["_delta_db"] = round(abs(20.0 * math.log10(max(ratio, 0.01))), 1)
            out["change"] = change or "down"
        else:
            out["db"] = round(20.0 * math.log10(n / 100.0), 1)
    else:
        out["change"] = change or "down"
    return out


_DUCK_DEEPER_RE = re.compile(r"\b(?:more|deeper|further|harder|stronger|lower|heavier|a lot)\b")
_DUCK_LIGHTER_RE = re.compile(r"\b(?:less|lighter|gentler|softer|shallower|not as much|a bit less)\b")
_MUTE_ALL_RE = re.compile(r"\b(?:everything|all (?:of )?(?:the )?(?:audio|sounds?|tracks)|every (?:track|sound)"
                          r"|the whole (?:mix|audio|soundtrack)|all sound)\b")
_EXCEPT_RE = re.compile(r"\b(?:except|but|apart from|other than|besides|save|leaving|keep(?:ing)?)\s+(?:for\s+)?"
                        r"(?:the\s+|my\s+|our\s+|just\s+|only\s+)?([\w'-]+(?:[ -](?:over|track|audio|sound|bed))?)")


def _level_slots(r: str, clause: str, c: S.Slots) -> dict[str, Any]:
    """Slots for the QA-018 one-liners, read from the clause text: which bed
    (music vs the programme's own sound), which edge of a fade, a level in dB
    or a direction, and a mute/unmute."""
    music = bool(_MUSIC_WORD_RE.search(clause))
    if r == "fit_music":
        return {"duration_s": c.duration_s or (c.range.end if c.range and c.range.kind == "last" else None)}
    if r == "mute" and _MUTE_ALL_RE.search(clause):
        # Final QA: "mute everything except the voiceover" muted only the
        # main-track clips (the music kept playing) and "… except the music"
        # muted the MUSIC. Everything is every sound lane; `_keep` is the one
        # the user wants to hear.
        m = _EXCEPT_RE.search(clause)
        keep = None
        if m:
            what = m.group(1)
            keep = ("vo" if re.search(r"voice[- ]?over|narrat|\bvo\b", what)
                    else "music" if _MUSIC_WORD_RE.search(what)
                    else "voice" if re.search(r"voice|speech|dialogue|talking|original|clip|video", what) else None)
        return {"_everything": True, "_keep": keep, "_keep_word": m.group(1) if m else None,
                "muted": not re.search(r"\bunmute\b", clause)}
    if r == "mute":
        return {"target": "music" if music or not _VOICE_WORD_RE.search(clause) else "voice",
                "muted": not re.search(r"\bunmute|\bturn (?:the )?\w* ?(?:back )?on\b|\b(?:chalu|on) (?:karo|kar do|kardo|do)\b",
                                       clause)}
    if r == "volume":
        return _volume_slots(clause, music)
    # fade
    target = "music" if music else ("audio" if re.search(r"\b(?:audio|sound|voice)\b", clause) else "video")
    edges = _fade_edges(clause)
    edge = "both" if len(edges) != 1 else next(iter(edges))
    if not edges:
        edge = "out" if (music or c.at_end) else ("in" if c.at_start else "both")
    dur = c.duration_s
    if dur is None and c.range is not None and c.range.kind in ("first", "last"):
        dur = c.range.end if c.range.kind == "first" else (c.range.end or c.range.start)
    return {"target": target, "edge": edge, "duration_s": dur, "clip_ref": c.clip_ref}


def bind(hit: G.IntentHit, whole: S.Slots, prompt: str | None = None) -> Intent:
    if hit.intent == "audit":
        # "... then audit it": the recipe table's own final audit (stage 12).
        return Intent("_audit", {}, score=hit.score, clause=hit.clause)
    raw = {k: v for k, v in _hit_slots(hit, whole, prompt).items() if v not in (None, (), "")}
    return Intent(hit.intent, normalize_slots(hit.intent, raw) if hit.intent in RECIPE_BY_NAME else raw,
                  score=hit.score, clause=hit.clause)


# --------------------------------------------------------------------------
# 2. compose
# --------------------------------------------------------------------------

def _merge_fades(a: dict[str, Any], b: dict[str, Any], merged: dict[str, Any]) -> dict[str, Any]:
    """Two fade clauses ("add a fade in and a fade out", "fade in the video
    and fade out the music") are ONE fade recipe: the edges add up instead of
    the later clause replacing the earlier (which dropped the fade in). A
    music fade next to a picture fade rides along as `_music_edge`."""
    ta, tb = a.get("target") or "video", b.get("target") or "video"
    if (ta == "music") == (tb == "music"):
        edges = {e for s in (a, b) for e in ({"in", "out"} if s.get("edge") in (None, "both") else {s["edge"]})}
        return {**merged, "edge": "both" if len(edges) == 2 else next(iter(edges))}
    pic, mus = (b, a) if ta == "music" else (a, b)
    return {**pic, "_music_edge": mus.get("edge") or "out", "_music_duration_s": mus.get("duration_s")}


def _merge_intents(intents: Iterable[Intent]) -> list[Intent]:
    """One Intent per recipe; later non-empty slot values win."""
    by_name: dict[str, Intent] = {}
    order: list[str] = []
    for it in intents:
        if it.recipe in by_name:
            prev = by_name[it.recipe]
            merged = {**prev.slots, **{k: v for k, v in it.slots.items() if v not in (None, (), "")}}
            if it.recipe == "fade":
                merged = _merge_fades(prev.slots, it.slots, merged)
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
        # A question for a card with Download / Skip buttons (QA-063): no
        # "Reply download or skip", no tool ids.
        listing = "; ".join(d.what for d in downloads)
        front.append(_ask("downloads", f"This needs a one-time download: {listing} ({total / 1e9:.1f} GB in total). Download it now?",
                          kind="confirm", options=[("yes", "Download"), ("no", "Skip")]))
    if est > LONG_RUN_SECONDS and steps:
        heavy = max(steps, key=lambda s: _step_weight(s, facts))
        front.append(_ask("go", f"This will take about {_human_minutes(est)} ({heavy.why}). Start?",
                          kind="confirm", options=[("yes", "Start"), ("no", "Cancel")]))
    questions = _cap_questions(front + questions)

    postconditions = _dedupe_postconditions(postconditions)
    recipes_in = [it.recipe for it in ordered if it.recipe not in ("_audit",)] or \
        [it.recipe for it in ordered if it.recipe == "_audit"]
    headline = "auto_edit" if any(i.recipe == "auto_edit" for i in intents) else "+".join(recipes_in)
    titles = {**_TITLES, **{it.recipe: "Ducking off" for it in ordered
                            if it.recipe == "duck" and it.get("enabled") is False}}
    # Final QA r3: a clause that made no step ("trim clip 3 to 3 seconds and
    # speed it up" — the length is refused next to a speed change) is not
    # listed under "done"; its note says why.
    acted = [r for r in recipes_in if r not in expansions or expansions[r].steps or expansions[r].questions]
    title = " · ".join(titles.get(r, r) for r in ([headline] if headline == "auto_edit"
                                                  else (acted or recipes_in)))[:80]
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
    seen: dict[str, int] = {}
    uniq: list[NeedsInput] = []
    for q in questions:
        if q.key in seen:
            # QA-032: a brain's draft question with a BLANK default (Apple
            # Intelligence sent `default_value: ""` for `handle`) used to shadow
            # the recipe's blocking one — nothing paused and `$ask:handle`
            # was burned into the video. The question that will actually get
            # an answer wins.
            i = seen[q.key]
            prev = uniq[i]
            if q.pauses and not prev.pauses and is_blank_answer(prev.default):
                uniq[i] = q
            continue
        seen[q.key] = len(uniq)
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


#: Stickers (wave D3, E3): `add_sticker` fetches its artwork from a CDN, so no
#: plan may name it (schema.PLAN_DENY) — the Prompt bar says where they are
#: instead of "I did not catch that" (which offered captions for "add a sticker").
STICKER_REPLY = ("Want a sticker? Open the Stickers panel in the left rail and pick one — it lands at the playhead "
                 "for 3 s. The Prompt bar cannot place one itself (a sticker downloads its artwork).")


#: What "remove the captions / the filter / the transitions" is about, and
#: where that is done by hand (no plan tool can find those by name yet).
_FEATURE_WORDS: tuple[tuple[str, str], ...] = (
    (r"captions?|subtitles?|subs", "the captions"), (r"filters?|luts?|looks?|colou?r grade|grades?|grading", "a filter"),
    (r"effects?", "an effect"), (r"transitions?", "a transition"), (r"lower thirds?|titles?|text", "the text"),
    (r"hooks?", "the hook"), (r"keyframes?|zoom|ken burns", "the zoom keyframes"), (r"stickers?|emojis?", "a sticker"),
    (r"watermark|end ?card", "the brand overlay"), (r"speed ramp|speed curve", "the speed curve"),
    (r"freeze(?: frame)?", "the freeze frame"),
)
#: Wave E (F4b): flip / mirror and removing captions, filters, transitions
#: and text are recipes now (agent/prompt/name_expanders.py); a removal the
#: Prompt bar has no recipe for still gets the honest reply below.
READ_ONLY_INTENTS: tuple[str, ...] = ("sticker", "transform")

#: Final QA: an overlay's opacity / position — honest, with where to do it.
TRANSFORM_REPLY = ("The Prompt bar cannot set {what} yet — select it on the timeline and use Transform in the "
                   "Inspector ({where}).")


#: What a CLIP carries (Final QA r2): "select it and press Delete" deletes
#: the clip itself, so these point at their Inspector section instead.
_CLIP_PROPERTY_SECTIONS: dict[str, str] = {
    "a filter": "Filters", "an effect": "Effects", "the zoom keyframes": "Animation / Keyframes",
    "the speed curve": "Speed", "the captions": "Captions",
}


def unsupported_removal_reply(clause: str) -> str:
    """The honest reply for "remove the <something no recipe removes>"."""
    what = next((label for pat, label in _FEATURE_WORDS if re.search(rf"\b(?:{pat})\b", clause)), "that")
    section = _CLIP_PROPERTY_SECTIONS.get(what)
    if section:
        return (f"The Prompt bar cannot remove {what} yet — select the clip and remove it in the Inspector "
                f"({section}). Was it your last edit? Then say 'undo'.")
    return (f"The Prompt bar cannot remove {what} yet — select it on the timeline and press Delete, or remove it "
            f"in the Inspector. Was it your last edit? Then say 'undo'.")


_EXISTING_STICKER_RE = re.compile(r"\b(?:the|this|that|my|these|those)\s+(?:\w+\s+)?(?:stickers?|emojis?)\b")
_STICKER_SIZE_RE = re.compile(r"\b(?:bigger|smaller|larger|tinier|size|resize|scale|enlarge|shrink|grow)\b")


def _read_only_reply(hit: G.IntentHit) -> str:
    if hit.intent == "sticker":
        if _EXISTING_STICKER_RE.search(hit.clause) and _STICKER_SIZE_RE.search(hit.clause):
            # Final QA r3: "make the sticker bigger" answered "Want a sticker?"
            return TRANSFORM_REPLY.format(what="a sticker's size", where="Scale, or drag its corner on the preview")
        return STICKER_REPLY
    if hit.intent == "transform":
        opacity = bool(re.search(r"transparen|opacity|opaque|see[- ]through|translucent", hit.clause))
        noun = ("a sticker's" if re.search(r"sticker|emoji", hit.clause)
                else "a text's" if re.search(r"text|title|lower[- ]?third", hit.clause) else "an overlay's")
        return TRANSFORM_REPLY.format(what=f"{noun} {'opacity' if opacity else 'position'}",
                                      where="Opacity" if opacity else "Position X / Y, or drag it on the preview")
    return unsupported_removal_reply(hit.clause)


#: Words that point at an intent when no phrase matched (review RD3: "slow
#: the middle clip down…" was offered Captions / Tighten / Auto edit).
_GUESS_WORDS: tuple[tuple[str, str], ...] = (
    (r"slow|fast|quick|speed|pace|tempo", "speed"), (r"text|words?|write|writ|type|title|caption|say", "title"),
    (r"animat|bounce|wobble|swing|pendulum|entrance|exit", "animation"),
    (r"zoom|closer|punch|push in", "zoom"), (r"rotat|turn|tilt|spin|upside", "rotate"),
    (r"cut|trim|lose|remove|delete|chop|shorten|drop", "trim"), (r"split|slice|blade", "split"),
    (r"music|song|soundtrack|beat", "music"), (r"loud|quiet|volume|louder|softer", "volume"),
    (r"colou?r|filter|look|grade|warm|cool|cinematic", "color_look"), (r"bright|dark|contrast|saturat", "adjust"),
    (r"fade", "fade"), (r"freeze|hold|pause the picture", "freeze"), (r"transition|dissolve|wipe", "transitions"),
    (r"reverse|backwards?", "reverse"), (r"mute|silence the", "mute"), (r"subtitle|captions", "captions"),
)


#: The phrase's own NOUNS rank first (review RE: "put a pink background behind
#: the video" and "take off the vignette" were offered Trim | Speed | Title).
_NOUN_GUESSES: tuple[tuple[str, str], ...] = (
    # Final QA r2: "delete the overlay" / "take out the b-roll" were offered
    # Trim | Speed | Title — a removal verb offers the removal first.
    (r"^(?:please\s+)?(?:delete|remove|take out|take away|get rid of|lose|ditch|drop)\b", "delete_clip"),
    (r"back\s*ground|canvas|backdrop|black bars|letter\s*box", "canvas"), (r"blend", "blend"),
    (r"(?:remove|delete|take off|take out|get rid of|turn off|clear|drop|lose)\b.*\b(?:vignett|grain|vintage|vhs|glow|"
     r"rgb split|sharpen|effect|filter|look|lut)", "remove_feature"),
    (r"voice|vocal|pitch|robot|echo|reverb|chipmunk", "voice_effect"),
    (r"animat|bounce|swing|pendulum|shake|wobble|slide (?:in|out)", "animation"),
    (r"mirror|flip", "flip"),
)


def _guesses(prompt: str) -> list[tuple[str, str]]:
    text = S.normalize(prompt)
    found: list[str] = []
    for pat, intent in _NOUN_GUESSES:
        if intent not in found and re.search(rf"\b(?:{pat})", text):
            found.append(intent)
    for intent, rows in G.PHRASES.items():
        if intent in ("ask", "undo", "redo") or intent in found:
            continue
        for pat, _score in rows:
            if re.search(pat, text):
                found.append(intent)
                break
    # the nearest intents by keyword, before any fixed suggestion
    for pat, intent in _GUESS_WORDS:
        if intent not in found and re.search(rf"\b(?:{pat})", text):
            found.append(intent)
    for fallback in ("trim", "speed", "title"):
        if len(found) >= 3:
            break
        if fallback not in found:
            found.append(fallback)
    return [(i, _TITLES.get(i, i)) for i in found[:3]]


#: Recipes whose "it" / "this" may point at ONE clip (Final QA r2; r3: every
#: per-clip recipe — "mute clip 2 and slow it down" slowed EVERY clip).
_PRONOUN_RECIPES = frozenset({"color_look", "speed", "reverse", "flip", "mute", "volume", "fade", "animation",
                              "voice_effect", "blend", "canvas", "zoom", "rotate", "adjust", "clip_length",
                              "stabilize", "upscale"})
_IT_WORD_RE = re.compile(r"\b(?:it|them)\b")
_OVERLAY_WORD_RE = re.compile(r"\b(?:overlays?|pips?|picture[- ]in[- ]picture)\b")
_SECOND_HALF_RE = re.compile(r"\b(?:the\s+)?(?:second|other|right|back|latter|last)\s+(?:half|part|piece)\b")
#: A clause's pronoun reading that means "whatever `it` is" (not "this clip").
_PRONOUN_REFS = (None, "$selected")


def _overlay_ref(facts: TimelineFacts) -> str | None:
    ov = [c.id for c in facts.clips if re.fullmatch(r"v\d+", c.track or "") and c.track != "v1"]
    if facts.selection in ov:
        return facts.selection
    return ov[0] if len(ov) == 1 else None


def _second_half_ref(split: Intent, facts: TimelineFacts) -> str:
    """The right-hand piece a split earlier in the prompt makes: the LAST
    clip when the split is in the last clip (a live sentinel), else a
    reference that asks (its id is made at run time)."""
    from .clip_expanders import bind_clip, v1_spans
    spans = v1_spans(facts) or []
    cid = None
    if split.get("clip_ref") is not None:
        cid, _q = bind_clip(split.get("clip_ref"), facts)
        ids = list(facts.v1_clip_ids)
        cid = {"$v1_first": ids[0] if ids else None, "$v1_last": ids[-1] if ids else None}.get(cid, cid)
    at = split.get("at")
    if cid is None and at is not None:
        cid = next((c for c, a, b in spans if a < float(at) < b), None)
    if spans and cid == spans[-1][0]:
        return "$v1_last"
    return f"$second_half:{float(at) if at is not None else 0:g}"


def _bind_pronouns(intents: list[Intent], facts: TimelineFacts, clauses: tuple[str, ...] = ()) -> list[Intent]:
    """"it" is the clip named earlier in the SAME prompt: "trim the last clip
    to 4 seconds then make it black and white" graded every clip, "mute clip
    2 and slow it down" slowed every clip, "reverse clip 3 and flip it"
    flipped the SELECTED clip, "take the last clip, reverse it and mute it"
    reversed the selection. A clause with no edit of its own ("take the last
    clip") still names the clip. "the pip … make it fade in" is the overlay;
    "the second half" after a split is the split's right-hand piece. "this"
    is the selected clip ("make this black and white" graded every clip). A
    bare "make it …" with nothing named before it stays the whole video."""
    out: list[Intent] = []
    prev: Any = None
    split: Intent | None = None
    order = list(clauses) or [it.clause for it in intents]
    by_clause: dict[str, list[Intent]] = {}
    for it in intents:
        by_clause.setdefault(it.clause, []).append(it)
    seen: set[int] = set()
    walk: list[tuple[str, Intent | None]] = []
    for clause in order:
        group = [it for it in by_clause.get(clause, []) if id(it) not in seen]
        seen.update(id(it) for it in group)
        if group:
            walk.extend((clause, it) for it in group)
        else:
            walk.append((clause, None))
    walk.extend((it.clause, it) for it in intents if id(it) not in seen)
    for clause, it in walk:
        if it is None:
            named = G.clip_ref_of(clause or "")
            if named not in (None, "$v1_all", "$selected"):
                prev = named
            elif _OVERLAY_WORD_RE.search(clause or ""):
                prev = _overlay_ref(facts) or prev
            continue
        ref = it.get("clip_ref")
        text = it.clause or ""
        if it.recipe in _PRONOUN_RECIPES and ref in _PRONOUN_REFS:
            if split is not None and _SECOND_HALF_RE.search(text):
                it = Intent(it.recipe, {**it.slots, "clip_ref": _second_half_ref(split, facts)}, it.score, it.clause)
            elif prev is not None and (_IT_WORD_RE.search(text) or (it.recipe == "color_look" and re.search(
                    r"\b(?:it|this|that)\b", text) and ref is None)):
                it = Intent(it.recipe, {**it.slots, "clip_ref": prev}, it.score, it.clause)
            elif it.recipe == "color_look" and ref is None and re.search(r"\bthis\b", text) and facts.selection \
                    and facts.selection in set(facts.v1_clip_ids):
                it = Intent(it.recipe, {**it.slots, "clip_ref": "$selected"}, it.score, it.clause)
        if it.recipe == "split":
            split = it
        cur = it.get("clip_ref")
        if cur not in (None, "$v1_all") and not str(cur).startswith("$second_half"):
            prev = cur
        elif it.recipe == "blend" or (it.get("_target") == "overlay") or (
                cur is None and _OVERLAY_WORD_RE.search(text)):
            prev = _overlay_ref(facts) or prev
        elif cur is None:
            named = G.clip_ref_of(text)
            if named not in (None, "$v1_all", "$selected"):
                prev = named
        out.append(it)
    return out


_HISTORY_COUNT_RE = re.compile(r"\b(\d{1,2}|two|three|four|five|six|seven|eight|nine|ten)\s+"
                               r"(?:edits?|changes?|steps?|things?|actions?|times)\b|\b(twice)\b")
_COUNT_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                "ten": 10, "twice": 2}
#: The most undo/redo steps one prompt takes (Final QA r3).
MAX_HISTORY_STEPS = 20


def history_count(prompt: str) -> int:
    """How many edits an undo / redo prompt names ("undo the last 3 edits"),
    1 when it names none; capped at MAX_HISTORY_STEPS."""
    m = _HISTORY_COUNT_RE.search(S.normalize(prompt or ""))
    if not m:
        return 1
    word = m.group(1) or m.group(2)
    n = int(word) if word.isdigit() else _COUNT_WORDS.get(word, 1)
    return max(1, min(MAX_HISTORY_STEPS, n))


def plan(prompt: str, facts: TimelineFacts, *, hook_text: tuple[str, str] | None = None,
         allow_downloads: bool = True) -> Plan:
    """The recipes brain (§1.2): grammar → intents → recipes → steps."""
    det = G.detect(prompt)
    conf = det.confidence
    if det.hits and det.hits[0].intent in ("undo", "redo"):
        verb = det.hits[0].intent
        n = history_count(prompt)
        return Plan.new(intent=verb, brain="recipes", confidence=conf,
                        title=verb.title() if n == 1 else f"{verb.title()} {n} edits",
                        reply=(f"{'Undoing' if verb == 'undo' else 'Redoing'} the last edit." if n == 1 else
                               f"{'Undoing' if verb == 'undo' else 'Redoing'} the last {n} edits."))
    if det.hits and all(h.intent == "ask" for h in det.hits):
        return Plan.new(intent="ask", brain="recipes", confidence=conf, title="Question", reply=_ask_reply(facts))
    replies = [_read_only_reply(h) for h in det.hits if h.intent in READ_ONLY_INTENTS]
    if det.hits and all(h.intent in (*READ_ONLY_INTENTS, "ask") for h in det.hits):
        first = next(h.intent for h in det.hits if h.intent != "ask") if replies else "ask"
        return Plan.new(intent=first, brain="recipes", confidence=conf, title=_TITLES.get(first, first),
                        reply=" ".join(dict.fromkeys(replies))[:400] or _ask_reply(facts))
    hits = [h for h in det.hits if h.intent not in ("ask", *READ_ONLY_INTENTS)]
    if not hits:
        if det.exclusions and not det.unmatched:
            return Plan.new(intent="noop", brain="recipes", confidence=conf, title="Nothing to do",
                            reply="Nothing to change — you only said what not to do.")
        return Plan.new(
            intent="clarify", brain="recipes", confidence=0.0, title="Which edit?",
            needs_input=[_ask("intent", "I did not catch that. Which of these did you mean?",
                              options=_guesses(prompt))],
            reply="I did not understand that request.")
    intents = _bind_pronouns([bind(h, det.slots, prompt) for h in hits], facts, det.clauses)
    exclusions = frozenset(x for x in det.exclusions if x in RECIPE_BY_NAME)
    prefix = None
    if G.NORMALISE_THRESHOLD <= conf < G.RUN_THRESHOLD:
        prefix = "I read that as: " + ", ".join(_TITLES.get(h.intent, h.intent) for h in hits)
    if replies:
        prefix = "; ".join(x for x in (prefix, *dict.fromkeys(replies)) if x)
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


#: Words a typo'd prompt most often means (Final QA r2, `plan_as`).
_TYPO_VOCAB: tuple[str, ...] = (
    "the", "clip", "clips", "first", "second", "third", "fourth", "fifth", "last", "middle", "speed", "up",
    "down", "slow", "fast", "faster", "slower", "reverse", "trim", "cut", "title", "text", "music", "video",
    "delete", "remove", "duplicate", "split", "caption", "captions", "volume", "louder", "quieter", "mute",
    "black", "white", "warm", "cool", "zoom", "rotate", "freeze", "transition", "transitions", "please",
)


def _typo_fixed(prompt: str) -> str:
    """`prompt` with each unknown word replaced by the vocabulary word it is
    one slip away from ("spead up teh secnd clip" → "speed up the second
    clip"). Only used once the user has PICKED the intent from the menu."""
    import difflib
    out = []
    for tok in S.normalize(prompt).split():
        if tok.isalpha() and len(tok) >= 3 and tok not in _TYPO_VOCAB:
            near = difflib.get_close_matches(tok, _TYPO_VOCAB, n=1, cutoff=0.75)
            tok = near[0] if near else tok
        out.append(tok)
    return " ".join(out)


_RETEXT_SHAPE_RE = re.compile(r"\b(?:change|rename|replace|edit|fix|update|correct)\b.+\b(?:to|into|with)\b")


def plan_as(prompt: str, intent: str, facts: TimelineFacts, *, hook_text: tuple[str, str] | None = None,
            allow_downloads: bool = True) -> Plan:
    """The recipes plan for `prompt` read AS `intent` — the answer to "I did
    not catch that. Which of these did you mean?" (Final QA r2: the pick
    resumed a plan with no steps and ended on "Nothing to change", so the
    user chose an option and was dropped). The intent's own recipe runs on
    the prompt's words, so it acts, or asks its own question."""
    text = _typo_fixed(prompt)
    det = G.detect(text)
    hit = next((h for h in det.hits if h.intent == intent), None)
    if intent == "title" and _RETEXT_SHAPE_RE.search(text):
        intent = "retext"            # "change Summer Trip to Autumn Days" names a text's new words
    if hit is None or hit.intent != intent:
        clause = det.clauses[0] if len(det.clauses) == 1 else text
        hit = G.IntentHit(intent=intent, score=G.RUN_THRESHOLD, clause=clause,
                          slots=G._clause_slots(clause, det.slots))
    if intent not in RECIPE_BY_NAME:
        return Plan.new(intent="noop", brain="recipes", confidence=G.RUN_THRESHOLD, title="Nothing to do",
                        reply=f"I cannot do '{_TITLES.get(intent, intent)}' from the Prompt bar — try saying it "
                              "another way.")
    intents = _bind_pronouns([bind(hit, det.slots, prompt)], facts)
    p = compose(intents, facts, confidence=G.RUN_THRESHOLD, hook_text=hook_text,
                allow_downloads=allow_downloads, brain="recipes")
    if not p.steps and not p.needs_input and not (p.reply or "").strip():
        return p.with_(reply=f"{_TITLES.get(intent, intent)}: I could not tell what to change from "
                             f"“{prompt[:80]}” — say it with the clip and the amount, like 'speed up the "
                             "second clip 2x'.")
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
            spoken = args.get("language") or facts.spoken_language or facts.language
            target = args.get("target")
            if (s.tool == "auto_caption" and target in ("hi", "hinglish", "es") and not facts.is_cached("madlad")
                    and needs_translation(target, spoken) is not False):
                args.pop("target")
                if target == "hinglish":
                    # QA-043: never Devanagari when Hinglish was asked. The
                    # captions come out in the spoken language and a
                    # transliteration pass follows: Hindi → Latin with the
                    # bundled romaniser; any other language is refused by the
                    # executor's guard (it would need the skipped model).
                    steps.append(s.model_copy(update={"args": args}))
                    steps.append(s.model_copy(update={
                        "tool": "translate_captions", "args": {"target_lang": "hinglish"},
                        "why": "Hindi → Latin script (romanise, no model)", "optional": False}))
                    notes.append("Hinglish by transliteration if the speech is Hindi (translation model skipped)")
                    continue
                notes.append(f"captions stay in the spoken language ({target} translation model not downloaded)")
        elif s.tool == "translate_captions" and not facts.is_cached("madlad"):
            need = needs_translation(args.get("target_lang"), args.get("source_lang"))
            hinglish_unknown = str(args.get("target_lang") or "").lower() == "hinglish" and need is None
            if need is not False and not hinglish_unknown:
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
    keeps_language = any(st.tool == "translate_captions" or (st.tool == "auto_caption" and st.args.get("target"))
                         for st in steps)
    pcs = [c for c in p.postconditions if not (c.check == "captions_language" and not keeps_language)]
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
        if s.tool == "split_at" and "split_time" in subs:
            # Final QA r3: "split clip 2" → "Where in clip 2?" → "at 7 seconds"
            t = subs["split_time"] if isinstance(subs["split_time"], (int, float)) else _single_time(
                str(subs["split_time"]))
            if t is None:
                m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*", str(subs["split_time"]))
                t = float(m.group(1)) if m else None
            if t is None:
                remaining.append(next((q for q in p.needs_input if q.key == "split_time"), None))
            else:
                args["time"] = round(float(t), 3)
        if s.tool == "cut_range" and "range" in subs:
            rng = S.extract(f"cut {subs['range']}").range if isinstance(subs["range"], str) else None
            one = _single_time(subs["range"]) if rng is None and isinstance(subs["range"], str) else None
            if one is not None:
                # Final QA r2: "at 7 seconds" names a MOMENT, not a part — the
                # same question came back forever. Offer the split it means.
                remaining.append(_ask("range", f"A cut needs a start and an end. To split the clip at {one:g}s "
                                               f"instead, say 'split at {one:g} seconds'; to cut, say like "
                                               f"'from {one:g} to {one + 1:g} seconds'.", kind="text"))
            elif rng is None:
                remaining.append(next((q for q in p.needs_input if q.key == "range"), None) or
                                 _ask("range", "Which part should I cut? e.g. 'the first 5 seconds'.", kind="text"))
            else:
                a, b = rng.resolve(float(facts.video_end or facts.duration))
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


__all__ = ["CUT_RECIPES", "RECIPE_ORDER", "GATE_EXEMPT_TOOLS", "STICKER_REPLY", "bind", "compose", "plan",
           "plan_as", "without_downloads", "apply_answers"]
