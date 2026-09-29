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

from . import anim_expanders as AXP
from . import clip_slots as CS
from . import grammar as G
from . import scope_planner as SP
from . import semantic_fixups as SF
from . import semantics as M
from . import slots as S
from .facts import TimelineFacts
from .langs import needs_translation
from .expanders import EXPANDERS, audit_expansion, clip_names_to_numbers, estimate_seconds, expand_auto_edit, step_cost
from .recipes import (ASK, REASK_PREFIX, RECIPE_BY_NAME, Context, Expansion, Intent, consumes_answer,
                      dropped_note, is_blank_answer, normalize_slots, placeholder, pc, reask, was_reasked)
from .recipes import ask as _ask
from .schema import (ARG_REF, LONG_RUN_SECONDS, STAGE_AUDIO, STAGE_CUTS, STAGE_PREREQ, STAGE_TEXT, DownloadNeeded, NeedsInput, Plan,
                     Postcondition, Step)

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
    # "and" / "with" may have been typed "&" / "w/" (final sweep 3 r2: 'Like &
    # Subscribe' became 'LIKE AND SUBSCRIBE')
    alt = {"and": r"(?:and|&)", "with": r"(?:with|w/)"}
    pattern = r"\s*".join(alt.get(w.lower(), re.escape(w)) for w in text.split()) if any(
        w.lower() in alt for w in text.split()) else r"\s+".join(re.escape(w) for w in text.split())
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
        if "&" in (prompt or "") or "w/" in (prompt or ""):
            q0 = _original_case(q0, prompt)       # final sweep 3 r2: 'Like & Subscribe' as typed
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
        head, tail = M.reference_split(hit.clause)
        if tail and not _MUSIC_WORD_RE.search(head) and _VOICE_WORD_RE.search(head):
            # "duck the video sound under the voiceover": only the MUSIC ducks
            # (it turned ducking on for the music, and the clips kept their level)
            out["_programme"] = True
        db = _DB_RE.search(hit.clause)
        bare = _DUCK_TO_RE.search(hit.clause)
        if db and db.group("n") and not db.group("by"):
            v = float(db.group("n"))
            out["to_db"] = -abs(v)
        elif bare:
            out["to_db"] = -abs(float(bare.group(1)))    # final sweep 4: "duck … to -20" ducked to -18
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
        # run 4: "make the warm look stronger / weaker" steps the strength
        # of the look the clips already carry (it re-applied warm at 80 %
        # to EVERY clip, and "weaker" turned it up)
        # final sweep 4: "mkae clp 3 balck adn wihte" — the semantics read the
        # clause (typos fixed) as a look, so the look word is read the same way
        return {"look": c.look or S.look_of(hit.clause) or S.look_of(M.norm(hit.clause)) or w.look,
                "_strength": M.direction(hit.clause, "look") if M.direction(hit.clause, "look") in ("up", "down") else None}
    if r == "loudness" and lufs is None and not platform:
        # "turn the volume down" / "make it louder": a direction, relative to
        # the current target — it used to set the SAME target (a no-op that
        # still verified) whichever way the user asked.
        down, up = bool(_LOUD_DOWN_RE.search(hit.clause)), bool(_LOUD_UP_RE.search(hit.clause))
        return {"_change": "down" if down and not up else ("up" if up and not down else None)}
    if r in ("clean_audio", "loudness"):
        return {"lufs": lufs, "_platform": platform}
    if r == "speed":
        if _SPEED_RESET_RE.search(hit.clause) or M.direction(hit.clause, "speed") == "reset":
            # Final QA: "reset the speed", "normal speed", "remove the speed
            # change" set 1.25x (the default factor) — even on a 1x clip.
            # Normal is 1x, and a factor of 1 also clears a curve.
            return {"factor": 1.0, "preset": None, "clip_ref": c.clip_ref, "_smooth": False, "_curve": False}
        # Review RD2: a named curve is its preset; "speed ramp/curve" with no
        # name asks which (`_curve`) — never a constant factor.
        preset = _speed_preset_in(hit.clause)
        # K3: the shared reading of direction and amount ("reduce the speed"
        # → slower, "2 times faster" → 2x, "30% slower" → 0.7x); the old slot
        # table had no row for most of them and fell to the 1.25x default.
        sa = M.speed_ask(hit.clause)
        factor = c.speed or w.speed
        by_default = sa.factor is None and sa.direction in ("up", "down") and not re.search(r"\d", hit.clause)
        if sa.factor is not None and not sa.ambiguous:
            factor = sa.factor
        elif sa.direction in ("up", "down") and (factor is None or (factor > 1) != (sa.direction == "up")):
            factor = M.default_speed(sa.direction)
        return {"factor": None if preset else factor, "preset": preset, "clip_ref": c.clip_ref,
                "_smooth": c.smooth, "_curve": bool(_CURVE_WORD_RE.search(hit.clause)),
                # "a bit slower" names no amount: a step from the clip's OWN
                # speed (2x went to 0.8x, 2.5 times slower)
                "_step": by_default,
                # run 4: "twice as fast", "half speed", "3 times faster" —
                # a MULTIPLE of the clip's own speed (a 2x clip → 4x)
                "_scale": bool(sa.scale and sa.factor is not None and not sa.ambiguous and not preset)}
    if r == "freeze":
        # K3: "freeze on 6 seconds" — the moment is not also the hold length
        dur = S.extract(_AT_TIME_RE.sub(" ", hit.clause)).duration_s if _AT_TIME_RE.search(hit.clause) else c.duration_s
        frac = M.fraction_seconds(hit.clause)
        if frac is not None:
            dur = frac                   # "for half a second" held the 3 s default
        return {"at": _at_seconds(hit.clause), "duration_s": dur}
    if r == "split":
        # Final QA r3: the clip a split names, and "in half" (its midpoint)
        ref = G.clip_ref_of(hit.clause)
        return {"at": _at_seconds(hit.clause), "clip_ref": None if ref == "$v1_all" else ref,
                "_half": bool(re.search(r"\bin(?:to)?\s+(?:half|two|2)\b|\bdown the middle\b", hit.clause)),
                # final sweep 4: "split every clip in half" split the selected clip once
                "_every": ref == "$v1_all"}
    if r == "reverse":
        return {"clip_ref": c.clip_ref, "reverse": not G.reverse_off(hit.clause)}
    if r == "trim":
        # Final QA r3: "keep only the first 10 seconds" is the range to KEEP —
        # it used to be cut, deleting exactly what the user wanted.
        rng = c.range or w.range or _range_of(hit.clause)
        out = {"range": rng, "_keep": bool(_KEEP_RE.search(hit.clause)) or M.keeps_only(hit.clause)}
        if _MUSIC_OBJECT_RE.search(hit.clause):
            # final sweep 2 r2: "remove the last 3 seconds of the music" cut the
            # VIDEO's last 3 s; the object is the music clip
            mt = _MUSIC_TO_RE.search(hit.clause)
            out["_music"] = {"end_at": float(mt.group(1))} if mt else True
            return out
        ref = G.clip_ref_of(hit.clause)
        if rng is not None and rng.kind in ("first", "last") and ref not in (None, "$v1_all", "$playhead"):
            # "cut the first 2 seconds of clip 3": the range is inside THAT
            # clip — it cut the whole video's first 2 seconds
            out["_clip_ref"] = ref
        return out
    if r == "title":
        if _COUNTDOWN_RE.search(hit.clause) and not c.quoted_text:
            # final sweep 4: a 3 · 2 · 1 countdown (three one-second cards)
            return {"_countdown": True, "at": "end" if c.at_end else ("start" if c.at_start and not
                                                                          G.clip_ref_of(hit.clause) else None)
                    if _at_seconds(hit.clause) is None else _at_seconds(hit.clause)}
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
        if text is None and not name and not is_name_card and _TEXT_REF_RE.search(hit.clause) \
                and not _NEW_TEXT_VERB_RE.search(hit.clause) and text_look_of(hit.clause) \
                and (not _TITLE_TEXT_RE.search(hit.clause) or _SIZE_PCT_RE.search(hit.clause)):
            # "font size 72 for the title": a look for the title the user has
            # (it asked "What should the title say?")
            return {"text": None, "_restyle": True, "_look": text_look_of(hit.clause)}
        if text is None and not name:
            m = _TITLE_TEXT_RE.search(hit.clause)
            if m and len(m.group(1).split()) <= 8:
                text = _original_case(m.group(1).strip(), prompt)
                # K3: "put a title at the top saying hi" — the words after
                # "saying" are the title; the place before it is not.
                said = re.search(r"\b(?:saying|that says|which says|reading|that reads|says)\s+(.+)$", text, re.I)
                if said:
                    text = said.group(1).strip(" .!?\"'“”")
                # final sweep 3 r2: "add the title Step 2 on clip 2" titled
                # "Step 2 on clip 2" — the clip is where it goes, not a word
                text = _TITLE_ON_CLIP_RE.sub("", text).strip() or text
                if _NO_WORDS_RE.fullmatch(text.strip(" .!?").lower()):
                    # "write a catchy title for this" was titled 'FOR THIS'
                    text = None
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
            return {"text": None, "_restyle": True, "_look": text_look_of(hit.clause)}
        # "add a red title 'Intro'": the new title's own colour — and (final
        # sweep 3 r2, HIGH) its place, size, outline, box, font and motion
        new_look = new_title_look(hit.clause, text) if text else {}
        return {"text": text, "name": name, "handle": handle, "dur": dur, "at": at, "_look": new_look or None,
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
        elif c.duration_s is not None and not c.transition_type and not c.transition_look:
            # "make all transitions 1 second long": the EXISTING transitions'
            # length, each keeping its type (they became 0.4 s dissolves —
            # the length was dropped because no type was named)
            out["_to_duration"] = float(c.duration_s)
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


def _range_of(clause: str) -> Any:
    """The shared time reading as a slot range, when the slot table read none
    ("remove seconds 10 through 12", "take 2 seconds off the end")."""
    for x in M.time_refs(clause):
        if x.kind in ("first", "last"):
            return S.TimeRange(kind=x.kind, end=x.a)
        if x.kind == "range" and x.b is not None:
            return S.TimeRange(kind="abs", start=x.a, end=x.b)
    return None


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
_AT_TIME_RE = re.compile(r"\b(?:at|@|on)\s+(?:(\d{1,2}):(\d{2}(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)?\b)")


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
_SIZE_TIMES_RE = re.compile(r"\b(?:twice|two times|2x|double|three times|3x|triple)\s+(?:as\s+)?(?:big|large|the size)\b"
                            r"|\bdouble\s+(?:its\s+|the\s+)?size\b|\bhalf\s+(?:the|its)\s+size\b|\bhalf\s+as\s+big\b")
_SIZE_PCT_RE = re.compile(r"\b(?P<n>\d{1,3})\s*(?:%|percent)\s+(?P<dir>bigger|larger|smaller)\b"
                          r"|\bsize\s+(?P<dir2>up|down)\s+(?:by\s+)?(?P<n2>\d{1,3})\s*(?:%|percent)?(?!\s*(?:px|pt|pixels?))\b")
_BIGGER_RE = re.compile(r"\b(?:bigger|larger|large|huge|big|increase (?:the )?size|size up|more readable)\b")
#: A 3 · 2 · 1 countdown asked from the Prompt bar (final sweep 4).
_COUNTDOWN_RE = re.compile(r"\bcount\s?down\b")
_SMALLER_RE = re.compile(r"\b(?:smaller|tinier|less big|reduce (?:the )?size|size down)\b")
_SIZE_PX_RE = re.compile(r"\bsize\s+(?:to\s+)?(\d{2,3})\b|\b(\d{2,3})\s*(?:px|pixels?|pt)\b")
_UPPER_RE = re.compile(r"\b(?:all caps|all-caps|upper ?case|capitals|caps lock|in caps)\b")
_LOWER_CASE_RE = re.compile(r"\b(?:lower ?case|sentence case|normal case|no caps|not all caps)\b")
#: final sweep 3 r2: "with no outline" never matched — "no" had no space
#: after it, so only "nooutline" did and the title kept its 6 px outline.
_NO_OUTLINE_RE = re.compile(r"\b(?:no\s+|remove (?:the )?|without (?:an? )?)(?:outline|stroke|border)\b")
_OUTLINE_RE = re.compile(r"\b(?:outline|stroke|border)\b")
_NO_BOX_RE = re.compile(r"\b(?:no\s+|remove (?:the )?|without (?:an? )?)(?:background|box|backdrop)\b")
_BOX_RE = re.compile(r"\b(?:background|box|backdrop|highlight)\b")
#: Words that only describe how a text LOOKS (Final QA r2).
_STYLE_WORD_RE = re.compile(
    r"(?:" + "|".join(_COLOUR_HEX) + r"|#[0-9a-f]{3,6}|bigger|smaller|larger|large|huge|tiny|big|small"
    r"|font|size|sized|px|pixels?|pt|points?|bold|bolder|italic|underlined?|outlined?|outlines?|stroke"
    r"|shadows?|glow|caps|uppercase|upper|lowercase|lower|case|colou?red|colou?rs?|thicker|thinner|heavier"
    r"|brighter|darker|with|a|an|and|the|in|more|less|much|bit|little|lot|slightly|very|really|border"
    r"|box|background|highlight|\d+(?:\.\d+)?"
    # K3: the words around a look ("title text red please", "change the title
    # font to Anton", "put the title at the top", "make teh title biger")
    r"|to|of|at|on|it|please|pls|plz|text|title|heading|typeface|anton|bebas|montserrat|inter|top|bottom|middle"
    r"|center|centre|screen|position|biger|bigg?er|larger|colour|up|higher|lower\s*down"
    r"|twice|double|triple|half|times|as|its|2x|3x|two|three"
    # run 4: the text's own In / Out animation ("make the title fade in")
    r"|fade|fades|fading|slide|slides|sliding|pop|pops|animation|animate|animated|out|down|should)")
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


#: ONE existing transition named by its place: "the second transition",
#: "the last transition", "the 1st transition".
_TR_NTH_RE = re.compile(r"\b(?:the\s+)?(first|second|third|fourth|fifth|last|final|1st|2nd|3rd|4th|5th)\s+"
                        r"(?:transition|crossfade|cross fade|dissolve|wipe|fade)\b(?!s)")
_TR_NTH = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4, "fifth": 5,
           "5th": 5, "last": -1, "final": -1}


def _nth_transition_edit(clause: str) -> dict[str, Any] | None:
    """final sweep 2 r2: "make the second transition 1 second" set BOTH to
    1 s, and "change the first transition to a wipe" also changed its length.
    The edit is bound to the ONE seam the ordinal names."""
    m = _TR_NTH_RE.search(clause)
    if not m or not re.search(r"\b(?:make|set|change|turn|switch|convert|replace|swap|lengthen|shorten|extend)\b", clause):
        return None
    out: dict[str, Any] = {"_nth": _TR_NTH[m.group(1)], "type": None, "look": None, "at": None, "duration": None}
    tail = clause[m.end():]
    dst = re.search(r"\b(?:in)?to\s+(?:a\s+|an\s+|the\s+)?(.+)$|\b(?:with|for)\s+(?:a\s+|an\s+)?(.+)$", tail)
    if dst:
        out["type"] = S.transition_type_of(dst.group(1) or dst.group(2))
    n = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)\b", tail)
    if n:
        out["duration"] = float(n.group(1))
    elif re.search(r"\b(?:longer|slower)\b", tail):
        out["_retime"] = 1.5
    elif re.search(r"\b(?:shorter|faster|quicker)\b", tail):
        out["_retime"] = 1 / 1.5
    if out["type"] is None and out["duration"] is None and "_retime" not in out:
        return None
    return out


def _existing_transition_edit(clause: str) -> dict[str, Any] | None:
    """Slots that change or retime the EXISTING transitions of ONE type the
    clause names by name (Final QA r3), or None."""
    nth = _nth_transition_edit(clause)
    if nth is not None:
        return nth
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
_RT_BY_RE = re.compile(rf"\b(extend|lengthen|longer|shorten|shorter|trim)\b.*?\bby\s+{_TS}")
_RT_LONGER_RE = re.compile(r"\b(?:longer|lengthen|extend|stay\s+(?:on\s+)?(?:screen\s+)?longer)\b")
_RT_SHORTER_RE = re.compile(r"\b(?:shorter|shorten)\b")


#: "2 seconds longer", "1 second shorter", "half a second longer" (a change
#: to an existing text's length, never its new length).
_RT_MORE_RE = re.compile(r"\b(\d+(?:\.\d+)?|a|one|two|three|four|five|half\s+a)\s*(?:s|sec|secs|seconds?)\s+(longer|shorter)\b")
_WORD_SECS = {"a": 1.0, "one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0, "five": 5.0}
_RT_START_AT_RE = re.compile(r"^(?:start|begin|show)\s+(?:the\s+|my\s+)?(?:title|text|heading|headline)\s+(?:at|from)\s+"
                             r"(\d{1,2}:\d{2}(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)?$")


def _clock_or_secs(v: str) -> float:
    if ":" in v:
        mm, ss = v.split(":", 1)
        return round(int(mm) * 60 + float(ss), 3)
    return float(v)


#: K3: "move the title 2 seconds later", "… 1 s earlier".
_RT_SHIFT_RE = re.compile(rf"\b(?:by\s+)?{_TS}\s+(later|earlier|sooner)\b")
#: K3: "make the title last until the end (of the video)".
_RT_TO_END_RE = re.compile(r"\b(?:until|till|to|through)\s+the\s+(?:very\s+)?end\b|\bthe\s+(?:whole|entire)\s+(?:video|time)\b")


def _title_retime(clause: str) -> dict[str, float] | None:
    """The new timing of an EXISTING title a clause asks for (Final QA r3), or
    None when the clause is not a retime (it adds, names new words, or says
    no time)."""
    if not _TEXT_REF_RE.search(clause) or _NEW_TEXT_VERB_RE.search(clause) or re.search(r"[\"“]", clause):
        return None
    if m := _RT_SHIFT_RE.search(clause):
        n = float(m.group(1))
        return {"shift": n if m.group(2) == "later" else -n}
    if _RT_TO_END_RE.search(clause):
        return {"to_end": 1.0}
    if m := _RT_RANGE_RE.search(clause):
        a = float(m.group(1) or m.group(3))
        b = float(m.group(2) or m.group(4))
        return {"start": a, "end": b} if b > a else None
    m_start, m_end = _RT_START_RE.search(clause), _RT_END_RE.search(clause)
    if m_start and m_end:
        # run 4: "make the title appear at 3s and disappear at 7s" — BOTH
        # clauses (it kept the old length: 3–6 s)
        a = float(next(g for g in m_start.groups() if g is not None))
        b = float(next(g for g in m_end.groups() if g is not None))
        return {"start": a, "end": b} if b > a else None
    if m := m_start:
        return {"start": float(next(g for g in m.groups() if g is not None))}
    if m := m_end:
        return {"end": float(next(g for g in m.groups() if g is not None))}
    if m := _RT_BY_RE.search(clause):
        # "extend the SALE text by 2 seconds" said it was already on screen
        n = float(m.group(2))
        return {"extend": n if m.group(1) in ("extend", "lengthen", "longer") else -n}
    if m := _RT_MORE_RE.search(clause):
        # final sweep 2 r2: "make the title last 2 seconds longer" read "last
        # 2 seconds" as the new length and SHORTENED a 3 s title to 2 s
        n = M.fraction_seconds(m.group(0)) or _WORD_SECS.get(m.group(1), None) or float(m.group(1))
        return {"extend": n if m.group(2) == "longer" else -n}
    if m := _RT_START_AT_RE.search(clause):
        # "start the title at 0:02"
        return {"start": _clock_or_secs(m.group(1))}
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
    if _TEXT_REF_RE.search(clause) and re.search(r"\b(?:put|move|place|position|shift|bring)\b", clause):
        return True           # K3: "put THE title at the top" places the existing one
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
    times = _SIZE_TIMES_RE.search(clause)
    pct = _SIZE_PCT_RE.search(clause)
    if px:
        look["size"] = float(px.group(1) or px.group(2))
    elif pct and 0 < float(pct.group("n") or pct.group("n2")) < 400:
        # final sweep 2 r2: "text 50% bigger", "title size up by 20" added a
        # NEW title reading "50% bigger"
        n = float(pct.group("n") or pct.group("n2")) / 100.0
        up = (pct.group("dir") or pct.group("dir2") or "") in ("bigger", "larger", "up")
        look["_grow"] = round(1 + n if up else max(0.1, 1 - n), 3)
    elif times:
        # "twice as big" / "double the size" / "half the size" (it added a
        # NEW title reading "twice as big")
        look["_grow"] = 0.5 if times.group(0).startswith("half") else (3.0 if "three" in times.group(0)
                                                                        or "triple" in times.group(0) else 2.0)
    elif _BIGGER_RE.search(clause):
        look["_grow"] = 1.25
    elif _SMALLER_RE.search(clause):
        look["_grow"] = 0.8
    if _BOLD_RE.search(clause):
        look["_bold"] = True
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
    pos = re.search(r"\b(?:to|at|on|in|up)\s+(?:the\s+)?(top|bottom|middle|center|centre)\b", clause)
    if pos and (look or re.search(r"\b(?:move|shift|place|position|raise|drop|bring|put|captions?|subtitles?|subs)\b",
                                  clause)):
        look["position"] = {"middle": "center", "centre": "center"}.get(pos.group(1), pos.group(1))
    return look


_FONT_NAMES = (("anton", "Anton-Regular"), ("bebas", "BebasNeue-Regular"), ("montserrat", "Montserrat-Bold"),
               ("inter", "Inter-Bold"))
_BOLD_RE = re.compile(r"\bbold(?:er)?\b|\bheavier\b|\bthicker (?:font|letters|text)\b")


def text_look_of(clause: str) -> dict[str, Any]:
    """K3: the `set_text_style` args a title / text restyle clause asks for
    (colour, size or bigger / smaller, font or bold, case, outline, box,
    top / middle / bottom) — {} when it names none."""
    look = _caption_look_of(clause)
    out: dict[str, Any] = {}
    if "color" in look:
        out["color"] = look["color"]
    if "size" in look:
        out["size"] = look["size"]
    elif "_grow" in look:
        out["size_scale"] = look["_grow"]
    elif re.search(r"\bbiger\b|\bbigg?er\b", clause):
        out["size_scale"] = 1.25
    for k in ("upper", "stroke_w"):
        if k in look:
            out[k] = look[k]
    if "background" in look:
        out["background"] = look["background"]
    if m := re.search(r"\b(anton|bebas|montserrat|inter)\b", clause):
        out["font"] = dict(_FONT_NAMES)[m.group(1)]
    elif _BOLD_RE.search(clause):
        out["bold"] = True
    pos = re.search(r"\b(top|bottom|middle|center|centre)\b", clause)
    if pos and not re.search(r"\b(?:box|background|outline)\b", clause):
        out["position"] = {"centre": "center", "center": "middle"}.get(pos.group(1), pos.group(1))
    out.update(_text_anim_of(clause))
    return out


#: run 4: "fade the title in", "make the title fade in", "the title should
#: fade out", "slide the title up", "pop the text in" — the text's own In /
#: Out animation (it used to fade the first CLIP's picture and sound).
_TEXT_ANIM_RE = re.compile(
    r"\b(?P<kind>fade|fades|fading|slide|slides|sliding|pop|pops|popping)\b"
    r"(?:\s+(?:the|my|this|that|our|its)\s+(?:[\w']+\s+){0,3}?(?:title|text|heading|headline|label|super|lower[- ]?third))?"
    r"(?:\s+(?:slowly|quickly|gently|softly|nicely))?\s+(?P<side>in|out|up|down|away)\b"
    r"|\b(?:with\s+)?(?:a\s+|an\s+)?(?P<kind2>fade|slide|pop)[- ](?P<side2>in|out|up|down)\b(?:\s+animation)?")
_TEXT_ANIM_OFF_RE = re.compile(r"\b(?:remove|take\s+off|no|without|turn\s+off|drop|kill|clear|stop)\b.*\b(?:fade|slide|pop|animation)")


def _text_anim_of(clause: str) -> dict[str, str]:
    t = M.norm(clause or "")
    if re.search(r"\b(?:video|picture|footage|clip|clips|shot|screen|black|audio|sound|music|song|voice|volume)\b", t):
        return {}
    if _TEXT_ANIM_OFF_RE.search(t):
        out: dict[str, str] = {}
        if re.search(r"\bfade[- ]?ins?\b|\b(?:animation|fade)\s+in\b|\bin\s+animation\b", t) or not re.search(r"\bout\b", t):
            out["anim_in"] = ""
        if re.search(r"\bfade[- ]?outs?\b|\b(?:animation|fade)\s+out\b|\bout\s+animation\b", t) or not re.search(r"\bin\b", t):
            out["anim_out"] = ""
        return out
    m = _TEXT_ANIM_RE.search(t)
    if not m:
        return {}
    kind = (m.group("kind") or m.group("kind2") or "fade").rstrip("s")
    kind = {"fading": "fade", "sliding": "slide", "popping": "pop"}.get(kind, kind)
    side = m.group("side") or m.group("side2") or "in"
    if kind == "slide":
        return {"anim_out": "slide_down" if side in ("out", "away") else "slide_up"} if side in ("out", "away") \
            else {"anim_in": "slide_down" if side == "down" else "slide_up"}
    if side in ("out", "away"):
        return {"anim_out": kind}
    return {"anim_in": kind}


#: What follows "a title" when no words were given ("a catchy title for this").
_NO_WORDS_RE = re.compile(r"(?:for|about|on|to)\s+(?:this|it|that|me|us|the\s+(?:video|clip|reel|edit)|this\s+(?:video|clip|"
                          r"reel|edit)|my\s+(?:video|clip|reel))(?:\s+(?:please|pls|now))?")
_TITLE_ON_CLIP_RE = re.compile(
    r"\s+(?:on|over|during|across|for|above)\s+(?:the\s+)?(?:clip\s+(?:\d{1,2}|one|two|three|four|five|six)"
    r"|(?:first|second|third|fourth|fifth|last|final)\s+(?:clip|shot)|this\s+clip|[\w-]+\s+(?:shot|clip))\s*$",
    re.I)
_BOX_COLOUR_RE = re.compile(r"\b(?:in|on|with|inside)\s+(?:a|an|the)\s+(" + "|".join(_COLOUR_HEX)
                            + r")\s+(?:box|background|banner|backdrop|bar|block)\b")
_CORNER_RE = re.compile(r"\b(?:top|bottom|upper|lower)[- ]?(left|right)\b|\b(left|right)[- ]?(?:top|bottom)\b"
                        r"|\b(?:on|at|to)\s+the\s+(left|right)(?:\s+(?:side|edge))?\b(?!\s+(?:of|clip|shot))")
_PLACE_RE = re.compile(r"\b(top|bottom|middle|center|centre|upper|lower)\b")
_SIZE_WORD_RE = re.compile(r"\b(tiny|small|little|big|huge|giant)\b(?!\s+(?:clip|shot|video))")
_TITLE_ANIM_RE = re.compile(r"\b(fades?|fading|slides?|sliding|pops?|popping|drops?)\s+(in|up|down|out)\b"
                            r"|\b(?:with\s+a\s+)?(fade|slide|pop)[- ](in|up|down|out)\b")


def new_title_look(clause: str, text: str | None) -> dict[str, Any]:
    """The look a NEW title's own clause asks for (final sweep 3 r2, HIGH):
    colour, size words, outline, box (and its colour — "in a black box" was
    black TEXT), font, top / middle / bottom and a left / right corner, and
    fade / slide / pop in. The title's own words never count."""
    rest = M.strip_quotes(clause or "").lower()
    if text:
        rest = rest.replace(str(text).lower(), " ")
    look = {k: v for k, v in text_look_of(rest).items()
            if k in ("color", "size", "size_scale", "stroke_w", "background", "font")}
    if m := _BOX_COLOUR_RE.search(rest):
        look["background"] = _COLOUR_HEX[m.group(1)] + "CC"
        rest_wo_box = rest[:m.start()] + " " + rest[m.end():]
        if (c := _COLOUR_RE.search(rest_wo_box)) is not None:
            look["color"] = (c.group(2) or _COLOUR_HEX[c.group(1)]).upper()
        else:
            look.pop("color", None)
            if look["background"].upper().startswith(("#FFFFFF", "#FFD400", "#FFFF00", "#A4FF00")):
                look["color"] = "#000000"          # dark words on a light box
    if "size" not in look and "size_scale" not in look and (m := _SIZE_WORD_RE.search(rest)):
        look["size_scale"] = {"tiny": 0.45, "small": 0.6, "little": 0.6, "big": 1.2, "huge": 1.5,
                              "giant": 1.5}[m.group(1)]
    if m := _PLACE_RE.search(rest):
        look["position"] = {"upper": "top", "lower": "bottom", "center": "middle", "centre": "middle"}.get(
            m.group(1), m.group(1))
    if m := _CORNER_RE.search(rest):
        look["_x"] = m.group(1) or m.group(2) or m.group(3)
    if m := _TITLE_ANIM_RE.search(rest):
        verb = (m.group(1) or m.group(3) or "").rstrip("s").replace("fading", "fade").replace("sliding", "slide") \
            .replace("popping", "pop")
        way = m.group(2) or m.group(4)
        kind = {"fade": "fade", "slide": "slide_down" if way == "down" else "slide_up", "pop": "pop",
                "drop": "slide_down"}.get(verb)
        if kind:
            look["anim_out" if way == "out" else "anim_in"] = kind
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


#: A fade REMOVAL: "take the fade off", "remove the fades", "no fade on clip 2".
_FADE_OFF_RE = re.compile(
    r"\b(?:remove|delete|get rid of|clear|kill|strip|lose|ditch|undo|cancel)\s+(?:the\s+|all\s+(?:the\s+)?|any\s+|my\s+"
    r"|that\s+|this\s+|both\s+)?(?:\w+\s+)?(?:fades?|fade[- ](?:ins?|outs?))\b"
    r"|\b(?:take|turn|switch|get)\s+(?:the\s+|all\s+(?:the\s+)?|that\s+|this\s+|both\s+)?(?:\w+\s+)?"
    r"(?:fades?|fade[- ](?:ins?|outs?))\s+off\b|\bno\s+(?:more\s+)?fades?\b|\bwithout\s+(?:the\s+|any\s+)?fades?\b")


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


#: "in over 1 second … out over 3": one fade edge and its own length.
_EDGE_DUR_RE = re.compile(r"\b(in|out|up|down)\s+(?:over|for|in|by)\s+(\d+(?:\.\d+)?|half\s+a|a|one|two|three)\s*"
                          r"(?:s|sec|secs|seconds?)?\b")


def _fade_len(word: str) -> float | None:
    w = word.strip()
    if "half" in w:
        return 0.5
    if w in ("a", "one"):
        return 1.0
    if w in ("two", "three"):
        return float({"two": 2, "three": 3}[w])
    try:
        return float(w)
    except ValueError:
        return None


#: The voice-over lane named as a level's object (review RE).
_VO_WORD_RE = re.compile(r"\bvoice[- ]?overs?\b|\bvo\b|\bnarration track\b|\bvoice track\b")
#: Final sweep 4: a bare "narration" is the voice-over lane when the project
#: has one ("lower the narration by 3 dB" lowered every main-track clip), and
#: the clips' own speech otherwise (`_vo_soft`: the expander decides).
_NARRATION_RE = re.compile(r"\bnarrations?\b")


def _volume_slots(clause: str, music: bool, head: str | None = None) -> dict[str, Any]:
    # the TARGET is read from the words before a reference phrase ("lower the
    # clip audio to -18 dB under the voiceover" lowered the VOICEOVER); the
    # amount from the whole clause ("put the music under the voiceover at -20 dB")
    h = clause if head is None else head
    out: dict[str, Any] = {"target": "music" if music or not _VOICE_WORD_RE.search(h) else "voice"}
    if not music and _VO_WORD_RE.search(h):
        out["target"] = "vo"
    elif not music and _NARRATION_RE.search(h):
        out["target"], out["_vo_soft"] = "vo", True
    if M.direction(clause, "level") == "reset":
        out["db"] = 0.0             # "back to normal" is the source's own level
        return out
    down, up = bool(_DOWN_RE.search(clause)), bool(_UP_RE.search(clause))
    change = "down" if down and not up else ("up" if up and not down else None)
    m = _DB_RE.search(clause)
    pct = _PCT_RE.search(clause)
    if m:
        n = float(m.group("n"))
        negative = (m.group("sign") or "").strip() in ("-", "minus")
        plus = (m.group("sign") or "").strip() in ("+", "plus") and not m.group("to")
        if plus and not change:
            # "music +3db": an explicit plus is a change (it set +3 dB, 17 dB
            # louder than asked); a bare "-6db" / "to 3 dB" stays a level
            change = "up"
        if m.group("by") or plus or (change and not m.group("to") and not m.group("sign")):
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
            out["_delta_db"] = round(abs(20.0 * math.log10(max(ratio, 0.01))), 2)
            out["change"] = change or "down"
        else:
            # run 4: "music volume 50%" / "set the music to 50%" / "200%" is
            # a ratio OF THE CURRENT LEVEL — a change of 20·log10(n/100) dB
            # (it SET −6 dB, making a −14 dB bed 8 dB louder); the shared
            # reading (semantics.level_ask) asks when the words contradict it
            delta = M.pct_to_db(n)
            out["_delta_db"] = abs(delta)
            out["change"] = "up" if delta > 0 else "down"
    else:
        # "clip 2 volume +2", "bring the music down to -25": a level number
        # without "dB" is read like one with it (it was dropped for a 6 dB step)
        la = M.level_ask(clause)
        if la.db is not None and la.direction != "reset":
            out["db"] = la.db
        elif la.delta_db is not None:
            out["_delta_db"] = abs(la.delta_db)
            out["change"] = "up" if la.delta_db > 0 else "down"
        else:
            out["change"] = change or "down"
            if re.search(r"\ba\s+lot\b|\bway\s+(?:down|up|quieter|louder)\b|\bmuch\b", clause):
                out["_delta_db"] = 12.0          # "lower clip 2 a lot"
            elif re.search(r"\ba\s+(?:little|bit|touch|tad)\b|\bslightly\b", clause):
                out["_delta_db"] = 3.0
    return out


_DUCK_DEEPER_RE = re.compile(r"\b(?:more|deeper|further|harder|stronger|lower|heavier|a lot)\b")
#: "duck the bed under the speech to -20": a level said without "dB".
_DUCK_TO_RE = re.compile(r"\b(?:to|at|down\s+to)\s+(?:-|minus\s+)?(\d+(?:\.\d+)?)(?![\d.:])(?!\s*(?:%|percent|s\b|sec|x\b|k\b|p\b))")
_DUCK_LIGHTER_RE = re.compile(r"\b(?:less|lighter|gentler|softer|shallower|not as much|a bit less)\b")
_MUTE_ALL_RE = re.compile(r"\b(?:everything|all (?:of )?(?:the )?(?:audio|sounds?|tracks)|every (?:track|sound)"
                          r"|the whole (?:mix|audio|soundtrack)|all sound)\b")
_EXCEPT_RE = re.compile(r"\b(?:except|but|apart from|other than|besides|save|leaving|keep(?:ing)?)\s+(?:for\s+)?"
                        r"(?:the\s+|my\s+|our\s+|just\s+|only\s+)?([\w'-]+(?:[ -](?:over|track|audio|sound|bed))?)")


#: A cut / trim whose OBJECT is the music ("the last 3 seconds of the music",
#: "trim the song to 6 seconds", "cut the music at 8 seconds").
_MUSIC_OBJECT_RE = re.compile(
    r"\b(?:of|off|from)\s+(?:the\s+|my\s+)?(?:background\s+)?(?:music|song|soundtrack|bgm|bed|tune)\b"
    r"|\b(?:trim|cut|shorten|end|stop|chop)\s+(?:the\s+|my\s+)?(?:background\s+)?(?:music|song|soundtrack|bgm|bed|tune)\s+"
    r"(?:to|at|after)\s+\d")
_MUSIC_TO_RE = re.compile(r"\b(?:music|song|soundtrack|bgm|bed|tune)\s+(?:to|at|after)\s+(\d+(?:\.\d+)?)\s*"
                          r"(?:s|sec|secs|seconds?)?\b")


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
        keep_clip = G.clip_ref_of(clause[m.start():]) if m else None
        if keep_clip not in (None, "$v1_all"):
            # final sweep 2 r2: "mute everything except clip 1" muted only the music
            return {"_everything": True, "_keep": None, "_keep_clip": keep_clip, "_keep_word": None,
                    "muted": not re.search(r"\bunmute\b", clause)}
        if m:
            what = m.group(1)
            keep = ("vo" if re.search(r"voice[- ]?over|narrat|\bvo\b", what)
                    else "music" if _MUSIC_WORD_RE.search(what)
                    else "voice" if re.search(r"voice|speech|dialogue|talking|original|clip|video", what) else None)
        return {"_everything": True, "_keep": keep, "_keep_word": m.group(1) if m else None,
                "muted": not re.search(r"\bunmute\b", clause)}
    if r == "mute":
        head = re.split(r"\b(?:but|except|apart from|other than|besides|and keep|keep(?:ing)?)\b", clause)[0]
        if music and not _MUSIC_WORD_RE.search(head) and _VOICE_WORD_RE.search(head):
            music = False                  # "mute all clips but keep music" muted the MUSIC
        if not music and _VO_WORD_RE.search(head):
            # final sweep 2 r2: "mute the voice over" muted every CLIP
            return {"target": "vo", "muted": not re.search(r"\bunmute\b", clause)}
        return {"target": "music" if music or not _VOICE_WORD_RE.search(clause) else "voice",
                "muted": not re.search(r"\bunmute|\bturn (?:the )?\w* ?(?:back )?on\b|\b(?:chalu|on) (?:karo|kar do|kardo|do)\b",
                                       clause)}
    if r == "volume":
        ref_head, ref_tail = M.reference_split(clause)
        if ref_tail:
            return _volume_slots(clause, bool(_MUSIC_WORD_RE.search(ref_head)), head=ref_head)
        head = re.split(r"\b(?:except|but not|other than|apart from|besides|excluding|but (?:leave|keep)|leaving"
                        r"|and (?:leave|keep))\b", clause)[0]
        if music and not _MUSIC_WORD_RE.search(head):
            # "turn down all the clips except the music": the music is the one
            # lane to leave alone (it lowered ONLY the music); final sweep 2
            # r2: "… but leave the music" lowered the music
            rest = re.sub(r"\b(?:except|but not|other than|apart from|besides|excluding|but (?:leave|keep)|leaving"
                          r"|and (?:leave|keep))\s+(?:for\s+)?(?:the\s+|my\s+)?"
                          r"(?:background\s+)?(?:music|song|track|bed|bgm|soundtrack|tune|score)\b(?:\s+(?:alone|as it is))?",
                          " ", clause)
            out = _volume_slots(rest, False)
            if out.get("target") == "music":
                out["target"] = "voice"
            return out
        return _volume_slots(clause, music)
    # fade
    target = "music" if music else ("audio" if re.search(r"\b(?:audio|sound|voice)\b", clause) else "video")
    if _FADE_OFF_RE.search(clause):
        # "take the fade off the last clip" / "remove the fades" ADDED fades
        # final sweep 2 r2: "remove the fade in" also removed the fade OUT
        off_edges = _fade_edges(clause)
        return {"target": target, "_off": True, "clip_ref": c.clip_ref or G.clip_ref_of(clause),
                "edge": next(iter(off_edges)) if len(off_edges) == 1 else "both"}
    edges = _fade_edges(clause)
    edge = "both" if len(edges) != 1 else next(iter(edges))
    if not edges:
        edge = "out" if (music or c.at_end) else ("in" if c.at_start else "both")
    dur = c.duration_s
    if dur is None and c.range is not None and c.range.kind in ("first", "last"):
        dur = c.range.end if c.range.kind == "first" else (c.range.end or c.range.start)
    if dur is None:
        dur = M.fraction_seconds(clause)          # final sweep 4: "over half a second" faded 1 s
    # each edge keeps its own length when two fade clauses merge ("music fade
    # in 1s and fade out 3s")
    per_edge = {"_in_s": dur} if edge == "in" and dur else {"_out_s": dur} if edge == "out" and dur else {}
    both = {_FADE_DIR.get(m.group(1)): _fade_len(m.group(2)) for m in _EDGE_DUR_RE.finditer(clause)}
    if len(both) == 2 and all(both.values()):
        # final sweep 4: "fade the music in over 1 second and out over 3"
        # faded in only — each edge its own length
        edge, dur, per_edge = "both", None, {"_in_s": both["in"], "_out_s": both["out"]}
    rel = None
    if dur is None and (m := re.search(r"\b(longer|slower|shorter|quicker|faster)\b", clause)):
        # final sweep 2 r2: "make the fade out longer" set the same 1 s again
        rel = "longer" if m.group(1) in ("longer", "slower") else "shorter"
    return {"target": target, "edge": edge, "duration_s": dur, "clip_ref": c.clip_ref, **per_edge,
            **({"_relative": rel} if rel else {})}


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


_PICTURE_WORD_RE = re.compile(r"\b(?:video|clips?|picture|image|footage|shots?|screen|black|visuals?|scene)\b")


#: Recipes an edit "from here to the end" / "before the marker" applies to.
_UI_RANGE_RECIPES = frozenset({"trim", "speed", "mute", "color_look", "reverse"})
#: "split at the marker", "cut at the intro marker", "split at the marker called b".
_AT_MARKER_RE = re.compile(r"\b(?:at|on|@)\s+(?:the|my)\s+(?:(?P<lbl>[\w' -]{1,30}?)\s+)?marker"
                           r"(?:\s+(?:called|named|labell?ed)\s+(?P<lbl2>[\w' -]{1,30}?))?\b")


def _ui_anchor_time(ur: M.UIRange, facts: TimelineFacts) -> tuple[float | None, str | None]:
    """The timeline second a UI-anchored range hangs on, or the question."""
    if ur.anchor == "playhead":
        if facts.playhead is None:
            return None, "Where is the playhead? Its position is not known here — say the time, like 'from 0:05 to the end'."
        return float(facts.playhead), None
    marks = list(facts.markers or [])
    if not marks:
        return None, ("There is no marker on the timeline. Add one on the ruler (M at the playhead), or say the time, "
                      "like 'from 0:05 to the end'.")
    if ur.label:
        want = ur.label.lower().strip()
        hit = [m for m in marks if want == m[1].lower().strip()] or [m for m in marks if want in m[1].lower()]
        if len(hit) == 1:
            return float(hit[0][0]), None
        listing = "; ".join(f"'{lbl or 'unnamed'}' at {t:g}s" for t, lbl in marks[:5])
        return None, (f"Which marker? There is no marker called '{ur.label}'. The markers are: {listing}."
                      if not hit else f"Which marker? {len(hit)} are called '{ur.label}': {listing}.")
    if len(marks) == 1:
        return float(marks[0][0]), None
    listing = "; ".join(f"'{lbl or 'unnamed'}' at {t:g}s" for t, lbl in marks[:5])
    named = next((lbl for _t, lbl in marks if lbl), None)
    hint = f"Say like 'from the {named} marker to the end'." if named else "Say the time instead, like 'from 0:05 to the end'."
    return None, f"Which marker? There are {len(marks)}: {listing}. {hint}"


def _ui_ranges(intents: list[Intent], facts: TimelineFacts) -> tuple[list[Intent], str | None]:
    """run 4: "from here to the end make it black and white", "delete
    everything after the playhead", "cut from the marker to the end", "mute
    everything up to here" — a range anchored on the UI becomes the same
    TimeRange a numeric one gives, bound to the playhead / marker NOW. The
    clip under the playhead used to be the whole reading ("from here" is a
    clip reference), so "speed up everything after the playhead" sped up
    every clip. A missing anchor is one clear question."""
    vend = float(facts.video_end or facts.duration or 0.0)
    out: list[Intent] = []
    for it in intents:
        if it.recipe == "split" and it.get("at") is None and (m := _AT_MARKER_RE.search(M.norm(it.clause or ""))):
            # "split at the marker" / "cut at the intro marker": the marker's moment
            label = (m.group("lbl2") or m.group("lbl") or "").strip() or None
            t, q = _ui_anchor_time(M.UIRange(anchor="marker", side="from", label=label), facts)
            if q:
                return intents, q
            out.append(Intent(it.recipe, {**it.slots, "at": round(float(t or 0.0), 3)}, it.score, it.clause))
            continue
        ur = M.ui_range(it.clause or "")
        if ur is None or it.recipe not in _UI_RANGE_RECIPES:
            out.append(it)
            continue
        t, q = _ui_anchor_time(ur, facts)
        if q:
            return intents, q
        assert t is not None
        if vend <= 0:
            return intents, "There is no video on the main track yet — add a clip first."
        if ur.side == "from" and t >= vend - 0.05:
            what = "playhead" if ur.anchor == "playhead" else "marker"
            return intents, f"The {what} is at the end of the video ({t:g}s) — nothing comes after it. Which part did you mean?"
        if ur.side == "to" and t <= 0.05:
            what = "playhead" if ur.anchor == "playhead" else "marker"
            return intents, f"The {what} is at the start of the video — nothing comes before it. Which part did you mean?"
        if ur.from_s is not None and ur.from_s >= t - 0.05:
            what = "playhead" if ur.anchor == "playhead" else "marker"
            return intents, (f"The {what} is at {t:g}s, which is not after {ur.from_s:g}s — which part did you mean?")
        if ur.span_s is not None or ur.from_s is not None:
            # final sweep 4: "the 2 seconds after the playhead" is 2 s long,
            # not everything to the end; "from 1s to the playhead" starts at 1 s
            a, b = ((t, min(vend, t + float(ur.span_s))) if ur.side == "from" and ur.span_s is not None
                    else (max(0.0, t - float(ur.span_s)), t) if ur.span_s is not None
                    else (float(ur.from_s or 0.0), t))
            rng = S.TimeRange(kind="abs", start=round(a, 3), end=round(b, 3))
        else:
            rng = (S.TimeRange(kind="last", end=round(vend - t, 3)) if ur.side == "from"
                   else S.TimeRange(kind="first", end=round(t, 3)))
        slots = dict(it.slots)
        if it.recipe == "trim":
            slots["range"] = rng
            slots["_keep"] = bool(slots.get("_keep")) or M.keeps_only(it.clause or "")
            slots.pop("_clip_ref", None)
        else:
            slots["_range"] = rng
            slots["clip_ref"] = None
            slots.pop("_half", None)
            if it.recipe == "mute":
                # "mute everything after the marker": the clips' sound from
                # there on (a track mute has no "from here")
                slots.pop("_everything", None)
                slots.pop("_keep", None)
                slots["target"] = "voice"
        slots["_ui_anchor"] = round(t, 3)
        out.append(Intent(it.recipe, slots, it.score, it.clause))
    return out, None


def _absorb_retime_tail(intents: list[Intent], clauses: tuple[str, ...]) -> list[Intent]:
    """run 4: "make the title appear at 3s and disappear at 7s" — the grammar
    splits on "and"; the tail ("disappear at 7s") names no text of its own
    and is the SAME title's end (it kept the old length, 3–6 s)."""
    out = list(intents)
    for i in range(1, len(clauses)):
        c = clauses[i]
        m = _RT_END_RE.search(c)
        if not m or _TEXT_REF_RE.search(c) or len(c.split()) > 6:
            continue
        end = float(next(g for g in m.groups() if g is not None))
        prev = clauses[i - 1]
        for k, it in enumerate(out):
            rt = it.get("_retime") if it.recipe == "title" else None
            if it.clause == prev and isinstance(rt, dict) and "start" in rt and "end" not in rt and end > rt["start"]:
                out[k] = Intent(it.recipe, {**it.slots, "_retime": {"start": rt["start"], "end": end}}, it.score, it.clause)
                break
    return out


def _carry_music_fades(intents: list[Intent], clauses: tuple[str, ...]) -> list[Intent]:
    """"music fade in 1s and fade out 3s", "turn the music down and fade it
    out": a fade clause that names no lane of its own (or says "it") after a
    clause about the music is the MUSIC's fade — it faded the last clip's
    picture and sound instead."""
    order = list(clauses)
    out: list[Intent] = []
    for it in intents:
        if it.recipe == "fade" and it.get("target") in (None, "video", "audio") and not it.get("clip_ref") \
                and it.clause in order and not _PICTURE_WORD_RE.search(it.clause) \
                and not M.media_of(it.clause):
            i = order.index(it.clause)
            prev = next((M.media_of(c) for c in reversed(order[:i]) if M.media_of(c) or M.clip_refs(c)), ())
            if prev and prev[0] == "music":
                it = Intent(it.recipe, {**it.slots, "target": "music"}, it.score, it.clause)
        out.append(it)
    return out


def _merge_intents(intents: Iterable[Intent]) -> list[Intent]:
    """One Intent per recipe; later non-empty slot values win."""
    by_name: dict[str, Intent] = {}
    order: list[str] = []
    for it in intents:
        if it.recipe in by_name and _another_title(by_name[it.recipe], it):
            # Final sweep 3: "add a title 'Step 2' at 8s and a title 'Step 3' at
            # 14s" is TWO titles — merging kept only the last one's slots.
            # The extra rides on the first; `_expand_all` expands each.
            prev = by_name[it.recipe]
            more = (*(prev.get("_more") or ()), it)
            by_name[it.recipe] = Intent(prev.recipe, {**prev.slots, "_more": more}, max(prev.score, it.score),
                                        prev.clause)
            continue
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


def _another_title(prev: Intent, it: Intent) -> bool:
    """`it` is a second NEW title with words of its own (not a restyle or a
    retime of the first, and not the same words said twice)."""
    if it.recipe != "title":
        return False
    if prev.get("_restyle") and it.get("_restyle"):
        # run 4: "make the Day One title red and the SALE text blue" — two
        # restyles that NAME different texts are two edits (merging kept only
        # the last one's look, on the first-named text: rolled back)
        a_name, b_name = _restyled_name(prev.clause), _restyled_name(it.clause)
        return bool(a_name and b_name) and a_name != b_name
    if any(x.get(k) for x in (prev, it) for k in ("_restyle", "_retime", "_lower_third", "name", "handle")):
        return False
    a, b = (str(prev.get("text") or "").strip(), str(it.get("text") or "").strip())
    return bool(a and b) and a.lower() != b.lower()


_RESTYLED_NAME_RE = re.compile(r"\b(?:the|my|this|that|our)\s+((?:[\w']+\s+){1,3}?)(?:title|text|heading|headline|label|super)\b")


def _restyled_name(clause: str | None) -> str:
    """The words that name WHICH text a restyle clause means ("the Day One
    title" → "day one"), "" when it says only "the title"."""
    m = _RESTYLED_NAME_RE.search(M.norm(clause or ""))
    return m.group(1).strip().lower() if m else ""


def _join_expansions(a: Expansion, b: Expansion) -> Expansion:
    return Expansion(steps=a.steps + b.steps, postconditions=a.postconditions + b.postconditions,
                     questions=a.questions + tuple(q for q in b.questions if q not in a.questions),
                     downloads=a.downloads + tuple(d for d in b.downloads if d not in a.downloads),
                     notes=a.notes + tuple(n for n in b.notes if n not in a.notes),
                     content_brain=a.content_brain or b.content_brain,
                     prerequisites=a.prerequisites + b.prerequisites)


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
            x = EXPANDERS[it.recipe](it, facts, ctx)
            for extra in it.get("_more") or ():
                x = _join_expansions(x, EXPANDERS[it.recipe](extra, facts, ctx))
            expansions[it.recipe] = x
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
        if key in seen and s.tool != "duplicate_clip":      # "duplicate it three times" is three copies
            continue
        seen.add(key)
        out.append(s)
    return out


def _compose_durations(pcs: list[Postcondition], facts: TimelineFacts) -> list[Postcondition]:
    """final sweep 2 r2: "cut the first 2 seconds and then speed up the rest
    by 1.5x" committed the right 6.67 s and reported two false failures —
    each step's expected duration was measured from the ORIGINAL timeline.
    Several duration changes are ONE expectation, each applied to the result
    of the one before (plan order), on the main lane the verifier measures."""
    durs = [p for p in pcs if p.check == "duration_between"]
    base = float(facts.video_end or facts.duration or 0.0)
    if len(durs) < 2 or base <= 0:
        return pcs
    d, tol, ratio = base, 0.0, 0.0
    for p in durs:
        a = p.args
        if a.get("target") is not None:
            d = float(a["target"])
        elif a.get("start") is not None and a.get("end") is not None:
            d -= float(a["end"]) - float(a["start"])
        elif a.get("factor"):
            d /= float(a["factor"])
        else:
            return pcs              # an expectation we cannot compose: keep them as they were
        tol = max(tol, float(a.get("tol") or 0.1))
        ratio = max(ratio, float(a.get("tol_ratio") or 0.0))
    one = durs[-1].model_copy(update={"args": {**durs[-1].args, "target": round(d, 3), "start": None, "end": None,
                                               "factor": None, "tol": round(tol, 3), "tol_ratio": ratio or None}})
    first = pcs.index(durs[0])
    rest = [p for p in pcs if p.check != "duration_between"]
    return rest[:first] + [one] + rest[first:]


def _speeds_in_duration(pcs: list[Postcondition], steps: list, facts: TimelineFacts) -> list[Postcondition]:
    """final sweep 3 r2 (LOW): "cut everything between 5 and 6 seconds and
    speed up clip 1" applied exactly what its card said and then reported
    "✗ the duration matches: measured 10.2 s, expected 11.00" — the cut's
    expectation did not count the speed change on clip 1. A one-clip speed
    step the cut does not touch shortens (or lengthens) the expectation by
    what that clip loses; one the cut overlaps cannot be composed, and the
    duration check is left to the steps' own checks."""
    durs = [p for p in pcs if p.check == "duration_between"]
    speeds = [st for st in steps if getattr(st, "tool", None) == "set_speed"
              and isinstance(st.args.get("factor"), (int, float)) and float(st.args["factor"]) > 0
              and isinstance(st.args.get("clip_id"), str) and st.args["clip_id"] != "$v1_all"]
    if len(durs) != 1 or not speeds:
        return pcs
    a = durs[0].args
    if a.get("target") is None and (a.get("start") is None or a.get("end") is None):
        return pcs
    v1 = sorted((c for c in facts.clips if c.track == "v1"), key=lambda c: c.start)
    ids = [c.id for c in v1]
    target = float(a["target"]) if a.get("target") is not None else \
        float(facts.video_end or facts.duration or 0.0) - (float(a["end"]) - float(a["start"]))
    for st in speeds:
        ref = str(st.args["clip_id"])
        idx = 0 if ref == "$v1_first" else len(ids) - 1 if ref == "$v1_last" else \
            (int(ref.split(":", 1)[1]) - 1 if ref.startswith("$v1_nth:") and ref.split(":", 1)[1].isdigit()
             else ids.index(ref) if ref in ids else None)
        if idx is None or not 0 <= idx < len(v1):
            return [p for p in pcs if p is not durs[0]]
        c = v1[idx]
        if a.get("start") is not None and float(a["start"]) < c.start + c.duration and float(a["end"]) > c.start:
            return [p for p in pcs if p is not durs[0]]
        now = float(c.speed or 1.0)
        target -= c.duration - c.duration * now / float(st.args["factor"])
    one = durs[0].model_copy(update={"args": {**a, "target": round(target, 3), "start": None, "end": None}})
    return [one if p is durs[0] else p for p in pcs]


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

    postconditions = _dedupe_postconditions(_speeds_in_duration(_compose_durations(postconditions, facts),
                                                                steps, facts))
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
#: Final sweep 4: a picture-in-picture is placed by hand for now.
PIP_REPLY = ("The Prompt bar cannot place a picture-in-picture yet — drag the clip from the media library onto the "
             "'+ New track: PIP / overlay video' row under the timeline, then size and place it with Transform in "
             "the Inspector.")
_PIP_RE = re.compile(r"\bpip\b|\bpicture[- ]in[- ]picture\b|\b(?:over|on\s+top\s+of|above|onto)\s+(?:the\s+)?(?:[\w'-]+\s+){0,4}?"
                     r"(?:shot|clip|video|footage)\b")
#: "keep the logo on screen for the whole video", "show the sticker until
#: the end" — a sticker's timing (final sweep 4: it asked which @handle the
#: watermark should show).
_STICKER_NOUN = r"(?:logo|sticker|emoji|overlay|watermark|badge|icon|png|image|graphic)"
_KEEP_STICKER_RE = re.compile(
    rf"\b(?:keep|show|leave|have|display|hold|make|let)\s+(?:the\s+|my\s+|that\s+|this\s+)?(?:\w+\s+)?(?:{_STICKER_NOUN}|it)\s+"
    r"(?:stay\s+|showing\s+|visible\s+|up\s+|there\s+|on\s+)?(?:on\s+screen\s+|on\s+the\s+screen\s+|on\s+)?"
    r"(?:for\s+the\s+(?:whole|entire|full)\s+(?:video|time|clip|thing|length|duration)|(?:until|till|to|through)\s+the\s+"
    r"(?:very\s+)?end(?:\s+of\s+the\s+video)?|the\s+whole\s+(?:time|way)(?:\s+through)?|throughout|all\s+the\s+way(?:\s+through)?"
    r"|from\s+start\s+to\s+(?:finish|end))\b")


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
        if _PIP_RE.search(hit.clause):
            return PIP_REPLY                # final sweep 4: it got the Trim / Speed / Title menu
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


def _guesses(prompt: str, fallback: bool = True) -> list[tuple[str, str]]:
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
    for generic in (("trim", "speed", "title") if fallback else ()):
        if len(found) >= 3:
            break
        if generic not in found:
            found.append(generic)
    return [(i, _TITLES.get(i, i)) for i in found[:3]]


#: final sweep 2 r2: the edits a rollback card may offer for each family of
#: words the prompt names ("a bit slower" was offered "trim or title").
_FAMILY_OPTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("mute", ("mute", "volume")), ("level", ("volume", "mute", "loudness")), ("speed", ("speed", "clip_length", "freeze")),
    ("look", ("color_look", "adjust", "remove_feature")), ("adjust", ("adjust", "color_look")),
    ("fade", ("fade", "transitions")), ("rotate", ("rotate", "flip")), ("zoom", ("zoom", "canvas")),
    ("flip", ("flip", "rotate")), ("text", ("title", "retext", "remove_feature")),
    ("transition", ("transitions", "remove_feature")), ("music", ("music", "volume", "fit_music", "remove_music")),
    ("order", ("move_clip",)), ("cut", ("trim", "delete_clip", "clip_length")), ("split", ("split", "trim")),
    ("freeze", ("freeze",)), ("duplicate", ("duplicate",)), ("reverse", ("reverse",)), ("captions", ("captions",)),
    ("vo", ("voiceover", "volume")), ("voice_fx", ("voice_effect",)), ("animation", ("animation",)),
    ("canvas", ("reframe", "canvas")), ("canvas_bg", ("canvas",)), ("blend", ("blend",)), ("duck", ("duck", "volume")),
)


def safe_options(prompt: str, plan: Plan | None = None, avoid: Iterable[str] = ()) -> list[tuple[str, str]]:
    """The closest safe readings to offer after the K3 net rolled a run back
    (executor._pause_after_rollback): the edits of the families the prompt's
    words name first (the rolled-back plan's own intent when it is one of
    them — a pick re-plans the same words as that edit, and the net judges
    the result again), then the "did you mean" ranking — but only readings
    related to those words. The generic trim / speed / title list is only
    for a prompt that names no kind of edit at all."""
    avoid = set(avoid)
    from .contract import INTENT_FAMILIES, families_of
    fams = set().union(*(families_of(c) for c in (M.clauses(prompt) or [prompt]))) - {"composite", "history"}
    related: list[str] = []
    for fam, intents in _FAMILY_OPTIONS:
        if fam in fams:
            related += [i for i in intents if i in RECIPE_BY_NAME and i not in related]
    first = (plan.intent.split("+")[0] if plan and plan.intent else "")
    out: list[str] = []
    if first in RECIPE_BY_NAME and first not in avoid and (not fams or first in related
                                                          or INTENT_FAMILIES.get(first, set()) & fams):
        out.append(first)
    out += [i for i in related if i not in avoid and i not in out]
    for g, _l in _guesses(prompt, fallback=not fams):
        if g in avoid or g in out:
            continue
        if not fams or g in related or INTENT_FAMILIES.get(g, set()) & fams:
            out.append(g)
    if not out and not fams:
        out = [i for i in ("trim", "speed", "title") if i not in avoid]
    return [(i, _TITLES.get(i, i)) for i in out[:3]]


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
                slots = {**it.slots, "clip_ref": _second_half_ref(split, facts)}
                if it.recipe in ("volume", "mute") and slots.get("target") == "music" \
                        and not _MUSIC_WORD_RE.search(text):
                    slots["target"] = "voice"      # "split at 6s then mute the second half" muted the MUSIC
                it = Intent(it.recipe, slots, it.score, it.clause)
            elif prev is not None and (_IT_WORD_RE.search(text) or (it.recipe == "color_look" and re.search(
                    r"\b(?:it|this|that)\b", text) and ref is None) or (ref is None and M.is_bare_continuation(text))):
                slots = {**it.slots, "clip_ref": prev}
                if it.recipe in ("volume", "mute") and slots.get("target") == "music" \
                        and not _MUSIC_WORD_RE.search(text) and _IT_WORD_RE.search(text):
                    slots["target"] = "voice"      # "reverse clip 2 and turn it up 3db": the clip's sound
                it = Intent(it.recipe, slots, it.score, it.clause)
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


_TEXT_PLACE_RE = re.compile(r"\b(?:title|text|heading|headline|lower[- ]?third|label)\b.*\b(?:top|bottom|middle"
                            r"|center|centre)\b|\b(?:top|bottom|middle|center|centre)\b.*\b(?:title|text|heading)\b")


_NEW_QUOTED_TITLE_RE = re.compile(r"\b(?:add|put|insert|create|place|write|drop|throw)\s+(?:in\s+)?(?:an?\s+|the\s+)?"
                                  r"(?:new\s+)?(?:title|text|heading|headline)\s*[:\-]?\s*[\"“'‘]")


def _text_place_hits(det: G.Detection) -> G.Detection:
    """K3: "put the title at the top" / "move the title to the bottom" is a
    title's LOOK (`set_text_style` position) — the grammar reads it as the
    read-only overlay `transform`, which answered "use the Inspector"."""
    from dataclasses import replace as _r
    hits = []
    changed = False
    for h in det.hits:
        if (h.intent == "transform" and _TEXT_PLACE_RE.search(h.clause)
                and not re.search(r"\b(?:sticker|emoji|overlay|pip|logo|opacity|transparen|corner)\w*", h.clause)):
            h = _r(h, intent="title")
            changed = True
        elif h.intent == "fade" and _NEW_QUOTED_TITLE_RE.search(h.clause):
            # final sweep 3 r2: "add title 'Intro' that fades in" faded clip 1
            # and added no title — the fade is the new title's own In
            h = _r(h, intent="title")
            changed = True
        hits.append(h)
    return _r(det, hits=tuple(hits)) if changed else det


_PLACE_IT_RE = re.compile(r"^(?:and\s+|then\s+)*(?:move|put|place|position|shift|bring|stick|set)\s+(?:it|them)\s+"
                          r"(?:up\s+|down\s+)?(?:to|at|on|in|near)?\s*(?:the\s+)?(?:very\s+)?(top|bottom|middle|center|centre)\b")


_IT_STYLE_RE = re.compile(r"^(?:and\s+|then\s+)*(?:make|turn|set|colou?r|have|give)\s+(?:it|them)\s+(.+)$")


def _absorb_style_clauses(intents: list[Intent], det: G.Detection) -> list[Intent]:
    """K3: "make the captions red and bigger", "make the title yellow and
    bigger" — the grammar splits on "and", and the second clause ("bigger")
    has no edit of its own, so it was dropped (only red was applied). A
    clause made only of look words belongs to the restyle before it."""
    hit_clauses = {h.clause for h in det.hits}
    out = list(intents)
    for i, c in enumerate(det.clauses):
        if i > 0 and _PLACE_IT_RE.search(c) and any(
                it.clause == det.clauses[i - 1] and ((it.recipe == "title" and it.get("_restyle"))
                                                     or (it.recipe == "captions" and it.get("_look") is not None))
                for it in out):
            # "make the title bigger and move it to the top", "make the
            # subtitles blue and move them up top": "it" is the text, and its
            # place is part of the same restyle (it was dropped)
            out = [it for it in out if it.clause != c]
            prev = det.clauses[i - 1]
            out = [Intent(it.recipe, {**it.slots, "_look": {**(it.get("_look") or {}), **(
                text_look_of(c) if it.recipe == "title" else _caption_look_of(c))}}, it.score, it.clause)
                   if it.clause == prev and it.recipe in ("title", "captions") else it for it in out]
            continue
        it_look = _IT_STYLE_RE.match(c)
        if i > 0 and it_look and _is_style_only(it_look.group(1), c) and any(
                it.clause == det.clauses[i - 1] and ((it.recipe == "title" and it.get("text")
                                                      and not it.get("_restyle")) or it.recipe == "captions")
                for it in out):
            # final sweep 2 r2: "add a title 'Intro' and make it red" — "it"
            # is the NEW title; its colour was dropped (a white title).
            # final sweep 4: "add subtitles and make them bold and big" too.
            prev = det.clauses[i - 1]
            out = [Intent(it.recipe, {**it.slots, "_look": {**(it.get("_look") or {}), **(
                text_look_of(c) if it.recipe == "title" else _caption_look_of(c))}},
                          it.score, it.clause) if it.clause == prev and it.recipe in ("title", "captions") else it
                   for it in out if it.clause != c]
            continue
        if c in hit_clauses or i == 0 or not _is_style_only(c, c):
            continue
        # final sweep 4: "add subtitles and make them bold and big" — the
        # "big" clause follows an absorbed one; the restyle it belongs to is
        # the nearest clause before it that still carries an intent
        prev = next((det.clauses[j] for j in range(i - 1, -1, -1) if any(it.clause == det.clauses[j] for it in out)),
                    det.clauses[i - 1])
        for k, it in enumerate(out):
            if it.clause != prev:
                continue
            if it.recipe == "captions" and it.get("_look") is not None:
                out[k] = Intent(it.recipe, {**it.slots, "_look": {**(it.get("_look") or {}), **_caption_look_of(c)}},
                                it.score, it.clause)
            elif it.recipe == "title" and it.get("_restyle"):
                out[k] = Intent(it.recipe, {**it.slots, "_look": {**(it.get("_look") or {}), **text_look_of(c)}},
                                it.score, it.clause)
    return out


def history_count(prompt: str) -> int:
    """How many edits an undo / redo prompt names ("undo the last 3 edits"),
    1 when it names none; capped at MAX_HISTORY_STEPS."""
    m = _HISTORY_COUNT_RE.search(S.normalize(prompt or ""))
    if not m:
        return 1
    word = m.group(1) or m.group(2)
    n = int(word) if word.isdigit() else _COUNT_WORDS.get(word, 1)
    return max(1, min(MAX_HISTORY_STEPS, n))


_CLIPW = r"(?:(?:the\s+)?(?:first|second|third|fourth|fifth|last|final|\d{1,2}(?:st|nd|rd|th))\s+(?:clip|shot)|clip\s+(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)|(?:this|that|the selected)\s+clip|it)"
#: Plain phrasings the phrase table has no row for, said the way it does
#: (Final sweep 2): each rewrite is the SAME edit in the grammar's words.
_REWORDS: tuple[tuple[re.Pattern, str], ...] = (
    # final sweep 4: everyday verbs the phrase table never had — each one
    # got the generic Trim / Speed / Title menu
    (re.compile(r"\b(?:nuke|zap|axe|toss|chuck)\s+(?=(?:the\s+|my\s+)?(?:first|second|third|fourth|fifth|last|final|middle"
                r"|selected|this|that|clip|shot|scene|it\b|everything|all\b|\d))", re.I), "delete "),
    (re.compile(r"\bbin\s+(?=(?:the\s+|my\s+)?(?:first|second|third|fourth|fifth|last|final|middle|selected|this|that"
                r"|clip|shot|scene|it\b|\d))", re.I), "delete "),
    (re.compile(r"\bthrow\s+(?:away|out)\s+", re.I), "delete "),
    (re.compile(r"\bdupe\s+", re.I), "duplicate "),
    (re.compile(r"\b(?:lop|shave|hack)\s+off\s+", re.I), "cut off "),
    (re.compile(r"\brazor\s+(?:it\s+|this\s+|the\s+clip\s+)?(?:at|@)\s+", re.I), "split at "),
    (re.compile(rf"\bmake\s+({_CLIPW})\s+(?:disappear|vanish|go\s+away)\b", re.I), r"delete \1"),
    (re.compile(r"\b(?:a\s+|one\s+)?half\s+(?:a\s+|of\s+a\s+)?second\b", re.I), "0.5 seconds"),
    (re.compile(r"\bkeep\s+(?:everything\s+|all\s+|it\s+)?from\s+(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds?)?\s+(?:on|onwards?)\b",
                re.I), lambda m: f"cut the first {m.group(1)} {m.group(2) or 'seconds'}"),
    # final sweep 3 r2 (CRITICAL): "revert clip 2 to normal speed" was read
    # as a bare "undo" and undid the LAST edit (a mute on clip 3) at once
    (re.compile(rf"\b(?:revert|undo|reset|return|put)\s+({_CLIPW})\s+(?:back\s+)?to\s+(?:its\s+|the\s+)?"
                r"(?:normal|regular|original|default|real)\s+speed\b", re.I), r"set \1 to normal speed"),
    # final sweep 3 r2: "give the logo a zoom in intro and a spin outro" asked
    # for an end-card handle — an intro / outro MOTION is the In / Out
    (re.compile(r"\bgive\s+(.+?)\s+an?\s+([a-z]+)(?:\s+in)?\s+(?:intro|entrance)\s+(?:and|&)\s+an?\s+"
                r"([a-z]+)(?:\s+out)?\s+(?:outro|exit)\b", re.I), r"make \1 \2 in and \3 out"),
    # final sweep 3 r2 (LOW): "cut everything between 5 and 6 seconds and speed
    # up clip 1" split at the first "and" — "6 seconds" was "Not done"
    (re.compile(r"\bbetween\s+(\d+(?:\.\d+)?)\s*(?:s|secs?|seconds?)?\s+and\s+(\d+(?:\.\d+)?)\s*"
                r"(s|secs?|seconds?)\b", re.I), r"from \1 to \2 \3"),
    # "put clip 1 between clip 2 and clip 3" added a TRANSITION
    (re.compile(rf"\b(?:put|move|place|drag|stick|slot|insert)\s+({_CLIPW})\s+(?:in\s+)?between\s+({_CLIPW})\s+"
                rf"(?:and|&)\s+({_CLIPW})", re.I), r"move \1 after \2"),
    # "switch clips 2 and 3" / "switch clip 2 with clip 3" was unread
    (re.compile(r"\bswitch\s+(?:the\s+)?(clips?\s+\w+\s+(?:and|&|with)\s+(?:clip\s+)?\w+)", re.I), r"swap \1"),
    (re.compile(r"\bswitch\s+((?:the\s+)?\w+\s+and\s+(?:the\s+)?\w+\s+clips)\b", re.I), r"swap \1"),
    # final sweep 2 r2 (MEDIUM): clear requests that rolled back or dead-ended,
    # each said the way the phrase table reads the SAME edit
    (re.compile(rf"\b(?:swap|switch|exchange)\s+clip\s+(\d{{1,2}}|one|two|three|four|five)\s+(?:and|&|with)\s+"
                rf"clip\s+(\d{{1,2}}|one|two|three|four|five)\b", re.I), r"swap clips \1 and \2"),
    (re.compile(r"\b(move|put|place|drag)\s+((?:the\s+)?(?:first|second|third|fourth|fifth|last)\s+clip)\s+(after|before|behind)"
                r"\s+the\s+(first|second|third|fourth|fifth|last)\b(?!\s+(?:clip|shot|one))", re.I), r"\1 \2 \3 the \4 clip"),
    (re.compile(r"\b(slow|speed)\s+everything\s+(down|up)\b", re.I), r"\1 the video \2"),
    (re.compile(rf"\b(lower|raise|drop|boost)\s+({_CLIPW})\s+(a\s+(?:lot|bit|little|touch)|way\s+(?:down|up)|much)\b", re.I),
     lambda m: f"turn {m.group(2)} {'down' if m.group(1).lower() in ('lower', 'drop') else 'up'} {m.group(3)}"),
    (re.compile(rf"\b(?:shorten|cut\s+down|trim\s+down)\s+({_CLIPW})\s+by\s+(?:a|one)\s+(second|sec)\b", re.I),
     r"trim \1 by 1 \2"),
    (re.compile(rf"\b(?:shorten|cut\s+down|trim\s+down)\s+({_CLIPW})\s+by\s+", re.I), r"trim \1 by "),
    (re.compile(rf"\bsaturate\s+({_CLIPW}|the\s+video|everything)\s+(?:more|a\s+bit\s+more|a\s+little\s+more)\b",
                re.I), r"make \1 more saturated"),
    (re.compile(r"\b(remove|take\s+off|take\s+out|strip|get\s+rid\s+of|clear)\s+the\s+(black\s+and\s+white|b\s*&\s*w|b and w"
                r"|mono(?:chrome)?|gr[ae]y\s*scale|sepia)\s+(from|off|on)\b", re.I), r"\1 the \2 filter \3"),
    (re.compile(r"\bmake\s+the\s+(music|song|soundtrack)\s+twice\s+as\s+loud\b", re.I), r"turn the \1 up 6 db"),
    (re.compile(r"\bmake\s+the\s+(music|song|soundtrack)\s+half\s+as\s+loud\b", re.I), r"turn the \1 down 6 db"),
    (re.compile(r"\bfad\s+(in|out)\b", re.I), r"fade \1"),
    (re.compile(r"\b(title|text|heading)\s+font\s+(\d{2,3})\b", re.I), r"\1 font size \2"),
    (re.compile(r"^([A-Z][\w'-]*(?:\s+[A-Z][\w'-]*){0,3})\s+(?:should|must|needs to|has to)\s+(?:show|stay|be on screen|stay on screen"
                r"|last|run)\s+(until|till|to)\s+the\s+end\b"), r"make the \1 text last until the end"),
    # final sweep 2 r2: "replace the music with silence" ADDED a new bed
    (re.compile(r"\b(?:replace|swap|switch)\s+(the\s+|my\s+)?(background\s+)?(music|song|soundtrack|bgm|bed)\s+"
                r"(?:with|for)\s+(?:silence|nothing|no\s+music|no\s+sound|quiet)\b", re.I), r"remove \1\2\3"),
    # final sweep 2 r2: both ends in one sentence committed only one end —
    # "trim the start by 1s and the end by 1s", "remove the first and last second"
    (re.compile(r"\b(trim|cut|remove|delete|chop|take)\s+(?:off\s+)?the\s+(?:start|beginning|front|head)\s+(?:by\s+)?"
                r"(\d+(?:\.\d+)?\s*(?:s|sec|secs|seconds?))\s+and\s+(?:the\s+)?(?:end|ending|back|tail)\s+(?:by\s+)?"
                r"(\d+(?:\.\d+)?\s*(?:s|sec|secs|seconds?))\b", re.I), r"\1 the first \2 and \1 the last \3"),
    (re.compile(r"\b(trim|cut|remove|delete|chop|drop|lose)\s+(?:off\s+)?the\s+(?:very\s+)?first\s+and\s+(?:the\s+)?"
                r"(?:very\s+)?last\s+((?:\d+(?:\.\d+)?|two|three|four|five)\s+)?(seconds?|secs?)\b", re.I),
     lambda m: f"{m.group(1)} the first {(m.group(2) or '1 ').strip()} {m.group(3)} and {m.group(1)} the last "
               f"{(m.group(2) or '1 ').strip()} {m.group(3)}"),
    # final sweep 2 r2: "fade transitions everywhere, 1 sec" dropped the 1 s
    (re.compile(r"(\btransitions?\b[^,;]*?)\s*,\s*((?:for\s+)?\d+(?:\.\d+)?\s*(?:s|sec|secs|seconds?)"
                r"(?:\s+(?:each|long|apiece))?)\s*$", re.I), r"\1 \2"),
    # final sweep 2 r2: "add a caption 'Welcome' at 2 seconds" re-laid the
    # transcript captions — one quoted line is ONE text
    (re.compile(r"\b(add|put|insert|create|write|place)\s+(a|an|one)\s+(?:caption|subtitle)\s+(?=[\"'“‘])", re.I),
     r"\1 \2 text "),
    # "zoom abit on clip one" planned transitions
    (re.compile(r"\babit\b", re.I), "a bit"), (re.compile(r"\balittle\b", re.I), "a little"),
    # final sweep 2 r2: "make the title say Road Trip 2026" added a SECOND title
    # reading "say Road Trip 2026"
    (re.compile(r"\b(?:make|have|let|get)\s+((?:the|my|this|that)\s+(?:title|text|heading|headline|label))\s+"
                r"(?:say|read)s?\s+(?=\S)", re.I), r"change \1 to say "),
    # "divide the first clip at 2s" was unread
    (re.compile(r"\bdivide\s+(.{0,40}?)\s+(at|in half|in two)\b", re.I), r"split \1 \2"),
)


_LANE_NOUN = (r"(?:music|song|soundtrack|background music|captions?|subtitles?|subs|transitions?|titles?|text"
              r"|filters?|effects?|voice[- ]?overs?|narration|stickers?|watermark|logo)")
#: "remove the music and the captions": the verb governs BOTH nouns (the
#: captions were re-laid — "the captions" alone read as "add captions")
_REMOVE_BOTH_RE = re.compile(
    rf"\b(remove|delete|get rid of|take off|take out|clear|lose|ditch|kill|drop|strip)\s+((?:the|my|all(?:\s+the)?)\s+)?"
    rf"({_LANE_NOUN})\s+(?:and|&|plus)\s+((?:the|my|all(?:\s+the)?)\s+)?({_LANE_NOUN})\b", re.I)


def _reword(prompt: str) -> str:
    out = prompt or ""
    for rx, to in _REWORDS:
        out = rx.sub(to, out)
    out = _REMOVE_BOTH_RE.sub(lambda m: f"{m.group(1)} {m.group(2) or ''}{m.group(3)} and {m.group(1)} "
                                        f"{m.group(4) or ''}{m.group(5)}", out)
    return out


#: final sweep 2 r2: "remove the gap" on a timeline with no gap removed
#: SILENCES (3.8 s of footage); "remove clip 2 but keep the gap" closed it.
_GAP_ONLY_RE = re.compile(r"^(?:please\s+)?(?:remove|close|delete|get rid of|kill|fill|cut out|take out|fix)\s+"
                          r"(?:the\s+|all\s+(?:the\s+)?|any\s+|every\s+|that\s+|this\s+)?(?:empty\s+|black\s+)?gaps?"
                          r"(?:\s+(?:between|on|in|from)\s+(?:the\s+)?(?:clips|main track|timeline|video|edit))?(?:\s+please)?$")
_KEEP_GAP_RE = re.compile(r"\b(?:but|and|while)\s+(?:keep|leave)\s+(?:the\s+|a\s+|its\s+)?(?:gap|space|hole|spot|empty space)\b"
                          r"|\bwithout\s+closing\s+(?:the\s+)?gap\b|\bleave\s+(?:a\s+|the\s+)?gap\b")


def _gap_reply(prompt: str, facts: TimelineFacts) -> Plan | None:
    t = S.normalize(prompt)
    if _GAP_ONLY_RE.match(t):
        spans = sorted((a, b) for _c, a, b in (_v1_spans_of(facts) or []))
        gaps = [(b0, a1) for (_a0, b0), (a1, _b1) in zip(spans, spans[1:]) if a1 - b0 > 0.05]
        if not gaps:
            return Plan.new(intent="noop", brain="recipes", confidence=1.0, title="Nothing to do",
                            reply="There is no gap on the main track — its clips always sit end to end. To cut "
                                  "the quiet moments inside them, say 'remove the silences'.")
        return None
    if _KEEP_GAP_RE.search(t) and re.search(r"\b(?:remove|delete|cut|drop|take out|get rid of)\b", t):
        return Plan.new(intent="noop", brain="recipes", confidence=1.0, title="Nothing changed",
                        reply="The main track is magnetic: deleting a clip there always closes its gap, so I did not "
                              "delete it. To keep the space, put a black or colour hold of the same length in its "
                              "place — or say 'delete clip 2' to delete it and close the gap.")
    return None


_MUSIC_START_RE = re.compile(r"^(?:please\s+)?(?:start|begin|move|put|shift|have)\s+(?:the\s+|my\s+)?(?:background\s+)?"
                             r"(?:music|song|soundtrack|bgm)\s+(?:to\s+)?(?:start\s+|come in\s+|begin\s+)?(?:at|from)\s+"
                             r"(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)?$")
_MUSIC_HALF_RE = re.compile(r"^(?:(?:keep|have|put|play)\s+(?:the\s+)?)?(?:background\s+)?(?:music|song|soundtrack)\s+"
                            r"(?:only\s+)?(?:in|for|during)\s+(?:only\s+)?the\s+first\s+half(?:\s+of\s+the\s+video)?(?:\s+only)?$")
_NAMED_TEXT_LOOK_RE = re.compile(r"^(?:make|turn|colou?r)\s+(?:the\s+)?(.{2,40}?)\s+(" + "|".join(_COLOUR_HEX)
                                 + r"|bigger|smaller|larger|bold|all caps)$")
_MUSIC_LOOP_RE = re.compile(r"^(?:please\s+)?(?:loop|repeat)\s+(?:the\s+)?(?:background\s+)?(?:music|song|soundtrack|bed)\b")
_RENAME_RE = re.compile(r"^(?:please\s+)?(?:rename|retitle)\s+(.+?)\s+(?:to|into|as)\s+(.+)$", re.I)
_VIDEO_LENGTH_RE = re.compile(r"^(?:please\s+)?make\s+(?:it|this|the\s+(?:whole\s+)?video|the\s+edit)\s+(?:exactly\s+)?"
                              r"(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)(?:\s+long)?$")
_RESET_CLIP_RE = re.compile(r"^(?:please\s+)?reset\s+((?:the\s+)?(?:first|second|third|fourth|fifth|last)\s+clip|clip\s+\w+"
                            r"|this\s+clip|it)$")
_EVERY_OTHER_RE = re.compile(r"^(?:please\s+)?(?:remove|delete|drop|cut)\s+every\s+(?:other|second|2nd)\s+clip$")
_DELETE_OVERLAY_RE = re.compile(r"^(?:please\s+)?(?:delete|remove|get rid of|drop)\s+(?:the\s+)?(?:overlay|pip"
                                r"|picture[- ]in[- ]picture)(?:\s+clip)?$")


def _facts_reading(prompt: str, facts: TimelineFacts) -> tuple[str | None, Plan | None]:
    """final sweep 2 r2 (MEDIUM): requests the phrase table answered with
    "music is already on the timeline" or "I did not catch that", read here
    with the timeline in hand. Returns (a prompt said the grammar's way, or
    a finished Plan) — (None, None) when nothing here applies."""
    t = S.normalize(prompt)
    beds = sorted((c for c in facts.clips if c.track == "music"), key=lambda c: c.start)
    vend = float(facts.video_end or facts.duration or 0.0)
    if m := _MUSIC_START_RE.match(t):
        if len(beds) != 1:
            return None, Plan.new(intent="ask", brain="recipes", confidence=1.0, title="Question",
                                  reply=("There is no music on the timeline to move — add some first?" if not beds else
                                         "The music is in several pieces — which one should start there? Select it "
                                         "and drag it on the Music lane."))
        at = float(m.group(1))
        return None, Plan.new(intent="music", brain="recipes", confidence=1.0, title="Move music",
                              steps=[Step(tool="move_clip", args={"clip_id": beds[0].id, "new_start": at},
                                          why=f"the music starts at {at:g}s", stage=STAGE_AUDIO)],
                              postconditions=[Postcondition(check="tool_ok", args={"tool": "move_clip"},
                                                            human="the music moved")],
                              reply=f"the music now starts at {at:g}s")
    if (m := _NAMED_TEXT_LOOK_RE.match(t)) and not re.search(r"\b(?:title|text|clip|video|music|captions?)\b", m.group(1)):
        # "make Day One purple": the words of a text already there
        name = m.group(1).strip()
        if any(x.text.lower().split("\n")[0].strip() == name for x in facts.texts):
            return f"make the {name} text {m.group(2)}", None
    if (m := _RENAME_RE.match((prompt or "").strip())):
        # final sweep 4: "rename Day One to Day Two" — the words of a text
        # already there (it got the Trim / Speed / Title menu)
        old, new = m.group(1).strip(" '\"“”‘’"), m.group(2).strip(" '\"“”‘’.")
        if old and new and any(x.text.lower().split("\n")[0].strip() == old.lower() for x in facts.texts):
            return f"change the {old} text to {new}", None
    if _MUSIC_HALF_RE.match(t) and vend > 0:
        return f"cut the music at {round(vend / 2, 2):g} seconds", None
    if _MUSIC_LOOP_RE.match(t):
        m_end = max((c.start + c.duration for c in beds), default=0.0)
        return None, Plan.new(intent="noop", brain="recipes", confidence=1.0, title="Nothing to do",
                              reply=("There is no music on the timeline to loop — say 'add music' for a bed that "
                                     "repeats to the end." if not beds else
                                     "The music already plays under the whole video — nothing to loop." if m_end >= vend - 0.05
                                     else "The Prompt bar cannot repeat the music you have yet — say 'add music' for a "
                                          "bed that loops to the end of the video."))
    if (m := _VIDEO_LENGTH_RE.match(t)) and vend > 0:
        n = float(m.group(1))
        if abs(n - vend) < 0.05:
            return None, Plan.new(intent="noop", brain="recipes", confidence=1.0, title="Nothing to do",
                                  reply=f"The video is already {vend:g}s long.")
        how = (f"cut the last {vend - n:g}s, or speed it up {vend / n:.2f}x? Say 'cut the last {vend - n:g} seconds' "
               f"or 'speed the video up {vend / n:.2f}x'.") if n < vend else               (f"slow it down to {vend / n:.2f}x? Say 'slow the video down to {vend / n:.2f}x'.")
        return None, Plan.new(intent="ask", brain="recipes", confidence=1.0, title="Question",
                              reply=f"The video is {vend:g}s. To make it {n:g}s, should I {how}")
    if m := _RESET_CLIP_RE.match(t):
        what = m.group(1)
        return None, Plan.new(intent="ask", brain="recipes", confidence=1.0, title="Question",
                              reply=f"Reset what on {what} — its speed, zoom, look or volume? Say like 'reset the "
                                    f"speed of {what}' or 'reset the zoom on {what}'.")
    if _EVERY_OTHER_RE.match(t) and len(facts.v1_clip_ids) >= 2:
        n = len(facts.v1_clip_ids)
        nums = [str(i) for i in range(2, n + 1, 2)]
        return (f"delete clip {nums[0]}" if len(nums) == 1 else
                "delete clips " + ", ".join(nums[:-1]) + f" and {nums[-1]}"), None
    if (m := _KEEP_STICKER_RE.search(t)) and re.search(rf"\b{_STICKER_NOUN}s?\b", t) \
            and not re.search(r"\b(?:title|text|heading|caption|subtitle|music|song|clip\s+\d)", t) \
            and not S.HANDLE_RE.search(t) and "watermark" not in m.group(0):
        ids = list(facts.sticker_ids)
        if not ids:
            return None, Plan.new(intent="sticker", brain="recipes", confidence=1.0, title="Nothing to do",
                                  reply="There is no sticker or logo on the timeline to keep on screen — add one from "
                                        "the Stickers panel (Upload PNG for a logo) first.")
        if len(ids) > 1:
            return None, Plan.new(intent="ask", brain="recipes", confidence=1.0, title="Question",
                                  reply=f"Which one? There are {len(ids)} stickers on the timeline — select it and "
                                        f"drag its end to the end of the video, or delete the others first.")
        if vend <= 0:
            return None, None
        notes = [f"the sticker now shows 0–{vend:g}s"]
        if re.search(G.TRANSFORM_REQUEST, t):
            # "put the logo in the top right corner and keep it on screen":
            # the position is still by hand, and says so — the rest runs
            notes.insert(0, TRANSFORM_REPLY.format(what="a sticker's position",
                                                    where="Position X / Y, or drag it on the preview"))
        return None, Plan.new(intent="sticker", brain="recipes", confidence=1.0, title="Keep sticker on screen",
                              steps=[Step(tool="set_clip_timing", args={"clip_id": ids[0], "start": 0.0,
                                                                         "end": round(vend, 3)},
                                          why="the sticker stays on screen for the whole video", stage=STAGE_TEXT)],
                              postconditions=[Postcondition(check="tool_ok", args={"tool": "set_clip_timing"},
                                                            human="the sticker's timing is set")],
                              reply="; ".join(notes))
    if _DELETE_OVERLAY_RE.match(t):
        over = [c for c in facts.clips if re.fullmatch(r"v\d+", c.track or "") and c.track != "v1"]
        if len(over) != 1:
            return None, Plan.new(intent="ask", brain="recipes", confidence=1.0, title="Question",
                                  reply=("There is no overlay on the timeline to delete." if not over else
                                         f"There are {len(over)} overlays — which one? Select it and press Delete, or "
                                         f"say 'delete the overlay at 0:05'."))
        return None, Plan.new(intent="delete_clip", brain="recipes", confidence=1.0, title="Delete overlay",
                              steps=[Step(tool="bulk_delete", args={"clip_ids": [over[0].id]},
                                          why="delete the overlay", stage=STAGE_CUTS)],
                              postconditions=[Postcondition(check="clips_absent", args={"clip_ids": [over[0].id]},
                                                            human="the overlay is gone")],
                              reply="deleted the overlay")
    return None, None


def _v1_spans_of(facts: TimelineFacts):
    from .clip_expanders import v1_spans
    return v1_spans(facts)


def plan(prompt: str, facts: TimelineFacts, *, hook_text: tuple[str, str] | None = None,
         allow_downloads: bool = True) -> Plan:
    """The recipes brain (§1.2): grammar → intents → recipes → steps."""
    prompt = clip_names_to_numbers(_reword(prompt), facts)
    if (gap := _gap_reply(prompt, facts)) is not None:
        return gap
    said, done = _facts_reading(prompt, facts)
    if done is not None:
        return done
    prompt = said or prompt
    det = SF.rewrite_hits(_text_place_hits(G.detect(prompt)), facts)
    typo_note = None
    if not [h for h in det.hits if h.intent != "ask"]:
        # K3: "spead up teh second clip" — retry with common slips fixed
        fixed = SF.fix_typos(prompt)
        if fixed.strip() and fixed != S.normalize(prompt):
            det2 = SF.rewrite_hits(_text_place_hits(G.detect(fixed)), facts)
            if [h for h in det2.hits if h.intent != "ask"] and det2.confidence >= G.NORMALISE_THRESHOLD:
                det, prompt, typo_note = det2, fixed, f"I read that as “{fixed}”"
    det, split_times = _absorb_split_times(det)
    conf = det.confidence
    if det.hits and det.hits[0].intent in ("undo", "redo"):
        verb = det.hits[0].intent
        n = history_count(prompt)
        return Plan.new(intent=verb, brain="recipes", confidence=conf,
                        title=verb.title() if n == 1 else f"{verb.title()} {n} edits",
                        reply=(f"{'Undoing' if verb == 'undo' else 'Redoing'} the last edit." if n == 1 else
                               f"{'Undoing' if verb == 'undo' else 'Redoing'} the last {n} edits."))
    # K3: a clause whose direction, amount or scope cannot be read safely
    # ASKS before anything is planned ("slow it down to 2x", "change the
    # speed", "chop the first 20 seconds" on a 12 s video, "the 7th clip").
    qs = M.ambiguities(prompt, video_end=facts.video_end or facts.duration or None,
                       clip_count=len(facts.v1_clip_ids) if facts.v1_clip_ids else None)
    if qs:
        return Plan.new(intent="ask", brain="recipes", confidence=conf, title="Question", reply=qs[0][:400])
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
    intents = SF.fix_intents(
        _absorb_style_clauses(_bind_pronouns([bind(h, det.slots, prompt) for h in hits], facts, det.clauses), det),
        det, facts)
    intents = _carry_music_fades(intents, det.clauses)
    intents = _absorb_retime_tail(intents, det.clauses)
    intents, ui_q = _ui_ranges(intents, facts)
    if ui_q:
        return Plan.new(intent="ask", brain="recipes", confidence=conf, title="Question", reply=ui_q[:400])
    intents = AXP.pair_animation_sides(intents, det.clauses)
    if split_times:
        intents = [Intent(it.recipe, {**it.slots, "_more_times": split_times}, it.score, it.clause)
                   if it.recipe == "split" and it.get("clip_ref") is None else it for it in intents]
    exclusions = frozenset(x for x in det.exclusions if x in RECIPE_BY_NAME)
    prefix = None
    if G.NORMALISE_THRESHOLD <= conf < G.RUN_THRESHOLD:
        prefix = "I read that as: " + ", ".join(_TITLES.get(h.intent, h.intent) for h in hits)
    if replies:
        prefix = "; ".join(x for x in (prefix, *dict.fromkeys(replies)) if x)
    if typo_note:
        prefix = "; ".join(x for x in (typo_note, prefix) if x)
    groups = SP.fan_out(intents, list(det.clauses), facts)
    if groups:
        # K3: several clips named in one prompt — one composition per group
        # (a group never repeats a recipe), merged in stage order.
        p = SP.merge_plans([compose(g, facts, exclusions=exclusions, confidence=conf,
                                    reply_prefix=prefix if i == 0 else None, hook_text=hook_text,
                                    allow_downloads=allow_downloads, brain="recipes")
                            for i, g in enumerate(groups)], base_duration=facts.duration or None)
    else:
        p = compose(intents, facts, exclusions=exclusions, confidence=conf, reply_prefix=prefix,
                    hook_text=hook_text, allow_downloads=allow_downloads, brain="recipes")
    missed = _not_done(det, intents)
    if missed and p.steps and not p.needs_input:
        # Final sweep 3: "set the overlay to screen blend and make it full
        # screen" planned the blend and said nothing of the rest — the
        # person pressed Apply thinking both were done.
        said = "; ".join(f"'{c}'" for c in missed[:3])
        line = f"Not done: {said} — I don't know that edit yet."
        p = p.with_(reply=f"{p.reply} {line}".strip() if p.reply else line)
    if conf < G.NORMALISE_THRESHOLD:
        # §2.7: below 0.4 the plan is a guess — ask, offering it as the first option.
        return p.with_(steps=[], postconditions=[], downloads_needed=[], estimated_seconds=None,
                       needs_input=[_ask("intent", "I am not sure what you meant. Which of these?",
                                         options=[(h.intent, _TITLES.get(h.intent, h.intent)) for h in hits][:3]
                                         + [g for g in _guesses(prompt) if g[0] not in {h.intent for h in hits}])],
                       intent="clarify")
    return p


_BARE_TIME_RE = re.compile(r"^(?:and\s+|then\s+)?(?:at\s+)?(\d{1,4}(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)?$")


def _absorb_split_times(det: G.Detection) -> tuple[G.Detection, tuple[float, ...]]:
    """"split at 2 and 10 seconds" / "split at 2s, 5s and 10s": the bare times
    after a split clause are more split points (final sweep 3: only the first
    was split, and the rest were read as nothing)."""
    from dataclasses import replace
    if not any(h.intent == "split" for h in det.hits):
        return det, ()
    bare = [(c, m) for c in det.unmatched if (m := _BARE_TIME_RE.match(c.strip()))]
    if not bare:
        return det, ()
    drop = {c for c, _ in bare}
    times = tuple(float(m.group(1)) for _, m in bare)
    return replace(det, clauses=tuple(c for c in det.clauses if c not in drop),
                   unmatched=tuple(c for c in det.unmatched if c not in drop)), times


def _not_done(det: G.Detection, intents: list[Intent]) -> list[str]:
    """Clauses with editing words that nothing in the plan reads — neither a
    grammar hit nor an intent the fix-ups made of them. "thanks" or
    "whatever" (no editing words) are not an edit left undone."""
    from .brains.content import unanchored_prompt
    read = {h.clause for h in det.hits} | {it.clause for it in intents}
    return [c for c in det.unmatched if c not in read and not unanchored_prompt(c) and not _scope_only_clause(c)]


#: run 4: "take clip 3, make it 2x and black and white" — the first clause
#: only NAMES the clip the next ones edit; it is not an edit left undone.
_SCOPE_LEAD_RE = re.compile(r"^(?:(?:please|ok|okay|now|so|then|and)\s+)*(?:take|select|pick|grab|use|on|for|with|go\s+to"
                            r"|open|find|choose|about|regarding)?\s*(?:the\s+|my\s+|this\s+|that\s+)?"
                            r"(?:[\w'-]+\s+){0,3}?(?:clips?|shots?|scenes?|segments?|parts?|bits?|ones?)?\s*(?:#\s*|number\s+)?"
                            r"(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|first|second|third|fourth|fifth"
                            r"|last|final|middle|opening|closing)?\s*(?:clips?|shots?)?\s*[:\-–]?\s*$")


def _scope_only_clause(clause: str) -> bool:
    c = M.norm(clause).strip(" .!?")
    return bool(M.clip_refs(c)) and bool(_SCOPE_LEAD_RE.fullmatch(c))


#: Words a typo'd prompt most often means (Final QA r2, `plan_as`).
_TYPO_VOCAB: tuple[str, ...] = (
    "the", "clip", "clips", "first", "second", "third", "fourth", "fifth", "last", "middle", "speed", "up",
    "down", "slow", "fast", "faster", "slower", "reverse", "trim", "cut", "title", "text", "music", "video",
    "delete", "remove", "duplicate", "split", "caption", "captions", "volume", "louder", "quieter", "mute",
    "black", "white", "warm", "cool", "zoom", "rotate", "freeze", "transition", "transitions", "please",
    "bigger", "smaller",
    # final sweep 3 r2 (LOW): real words that sit one slip from a vocabulary
    # word — "move SALE to 9 seconds" became "remove …" ("Which part should I cut?")
    "move", "moved", "moving", "playhead", "shift", "place",
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
