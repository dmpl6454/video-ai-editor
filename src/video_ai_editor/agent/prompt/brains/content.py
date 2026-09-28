"""Content tasks — where a language model adds value over a grammar (§3.7).

Routing is the grammar's job; WORDS are not. The baseline runs showed the
content-blind hook heuristic rotating canned clickbait ("THEY LIED ABOUT HOW
THIS ACTUALLY WORKS") over a camera review (findings 3 and 9). This module
asks the brains for text and, when none can answer, falls back to a
transcript-DERIVED line — the first real claim in the speaker's own words,
fillers stripped, capped at seven words — never a canned template.

Two tasks:
  hook_candidates(transcript_head, n=3, max_words=7) → [str]
  rank_windows(windows=[{start,end,text}], k)        → [int] window indices

Everything a model returns is sanitised before a recipe sees it:
strings only, one line, ≤ `max_words`, ≤ `HOOK_MAX_CHARS` (the
`apply_hook_stack.text` bound in §1.3 rule 8), deduplicated; indices must be
in range and unique. `plan.content_brain` records which brain wrote the text
so the badge can say "Recipes · text by Apple Intelligence".
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from ..facts import TimelineFacts
from ..recipes import FILLERS_STRICT, heuristic_hook
from ..schema import IntentDraft, Plan
from .base import BRAIN_LABELS, Brain, TextResult, TextTask
from .jsonfix import JsonRepairFailed, repair

__all__ = ["HOOK_MAX_CHARS", "HOOK_MAX_WORDS", "HOOK_TOOL", "HOOK_RECIPE", "HookText", "RankResult",
           "hook_candidates_task", "rank_windows_task", "sanitize_hook_items", "sanitize_ranking",
           "needs_hook_text", "strip_model_hook_text", "ground_duck_off", "ground_to_prompt",
           "ground_music_level", "music_level_direction",
           "mentioned_intents", "parse_text_items", "text_task_prompts",
           "heuristic_hook_candidates", "hook_text", "rank_windows", "HEURISTIC_SOURCE"]

HOOK_TOOL = "apply_hook_stack"
HOOK_RECIPE = "hook"


def strip_model_hook_text(draft: IntentDraft, prompt: str) -> IntentDraft:
    """Drop a `hook.text` slot the MODEL wrote — one that is not the user's
    own words (a quoted or literal substring of the prompt).

    WHY (real 7B run, 2026-09-10): asked for "a hook at the start", Qwen
    filled `text` with "Get ready for an amazing story!" — the planning
    prompt carries no transcript words, so a line written there can only be
    generic, exactly the canned clickbait findings 3 and 9 caught. Without
    the slot the recipe falls back to the heuristic, `needs_hook_text` is
    true, and the router's content pass asks the same brain WITH the
    transcript head — words grounded in what the speaker says, attributed to
    the brain that wrote them."""
    haystack = re.sub(r"\s+", " ", prompt or "").lower()
    items = []
    changed = False
    for it in draft.intents:
        text = it.slots.get("text") if it.recipe == HOOK_RECIPE else None
        if isinstance(text, str) and text.strip() and _one_line(text).lower() not in haystack:
            items.append(it.model_copy(update={"slots": {k: v for k, v in it.slots.items() if k != "text"}}))
            changed = True
        else:
            items.append(it)
    return draft.model_copy(update={"intents": items}) if changed else draft


def mentioned_intents(prompt: str) -> set[str]:
    """Every intent whose phrase table (ANY row, weak ones included) matches
    the prompt — "the user used words for this kind of edit"."""
    from .. import grammar as G
    from .. import slots as S
    text = S.normalize(prompt or "")
    found: set[str] = set()
    for intent, rows in G.PHRASES.items():
        if any(re.search(p, text) for p, _score in rows):
            found.add(intent)
    for rx, intent, _score in G.CUT_PRECEDENCE:
        if re.search(rx, text):
            found.add(intent)
    return found


#: Word stems that make a prompt ABOUT an edit even when no intent phrase
#: matches (a paraphrase: "let the ending melt to black", "I want the backing
#: track to sit lower", "slap LAUNCH DAY across the top"). Matched at a word
#: start. Deliberately broad — the cost of a miss is a "did you mean" list;
#: the cost of a false hit is an unasked plan running (see unanchored_prompt).
_EDIT_STEMS = (
    # sound
    "music", "song", "tune", "track", "beat", "bed", "soundtrack", "audio", "sound", "voice", "vocal",
    "speech", "speak", "talk", "narrat", "mic", "noise", "hiss", "hum", "loud", "quiet", "soft",
    "volume", "level", "mute", "silen", "duck", "bass", "treble", "pitch", "echo", "reverb", "sing",
    # picture and time
    "fade", "ease", "melt", "black", "white", "begin", "start", "end", "intro", "outro", "open",
    "clos", "tail", "cut", "trim", "split", "clip", "shot", "scene", "frame", "video", "picture",
    "footage", "image", "photo", "colo", "bright", "dark", "contrast", "satur", "grade", "lut",
    "filter", "effect", "blur", "zoom", "crop", "reframe", "vertical", "horizontal", "portrait",
    "landscape", "square", "ratio", "aspect", "speed", "slow", "fast", "quick", "reverse",
    "backward", "rewind", "loop", "freeze", "pause", "gap", "filler", "umm", "tight", "stabili",
    "shak", "transition", "keyframe", "mask", "marker", "chapter", "thumbnail", "length",
    "duration", "second", "sec", "minute", "pace", "pacing", "hook", "highlight", "moment",
    # text and layout
    "caption", "subtitle", "transcri", "title", "text", "word", "headline", "heading", "label",
    "top", "bottom", "left", "right", "cent", "corner", "sticker", "emoji", "logo", "watermark",
    "brand", "font", "bold", "animat", "overlay", "pip", "background", "translat", "hindi",
    "english", "spanish", "hinglish", "language",
    # delivery
    "tiktok", "reel", "short", "youtube", "instagram", "insta", "story", "stories", "platform",
    "export", "render", "podcast", "interview", "vlog",
    # verbs of editing
    "remove", "delete", "add", "insert", "put", "make", "move", "place", "drop", "replace", "chang",
    "lower", "raise", "boost", "louder", "softer", "longer", "shorter", "edit", "polish", "clean",
    "enhanc", "improv", "fix", "undo", "redo", "slap", "sit",
    # Hinglish (Latin script) editing words
    "gaan", "awaaz", "awaz", "aawaz", "kam", "zyada", "jyada", "kaat", "hata", "dheer", "tez",
    "shuru", "aakhir", "likh", "badh", "chhot",
)
_EDIT_WORD_RE = re.compile(r"\b(?:" + "|".join(_EDIT_STEMS) + r")", re.IGNORECASE)
_NON_ASCII_RE = re.compile(r"[^\x00-\x7f]")


def unanchored_prompt(prompt: str) -> bool:
    """True when nothing in `prompt` is about editing: no intent phrase
    matches AND no editing word appears ("banana wobble zebra", "purple
    monkey dishwasher"). The router then keeps it from the on-device model,
    which answered such prompts with an unasked captions plan that ran
    unconfirmed; the recipes' "did you mean" list answers instead.

    A prompt in another script (or with accented letters) is never judged
    here: the grammar and this word list are English and Hinglish, and the
    model reads languages they do not."""
    text = (prompt or "").strip()
    if not text:
        return True
    if _NON_ASCII_RE.search(text):
        return False
    if mentioned_intents(text):
        return False
    return not _EDIT_WORD_RE.search(text)


#: A named recipe also grounds the recipes it is made of or implies.
_GROUND_RELATIVES: dict[str, frozenset[str]] = {
    "tighten": frozenset({"remove_silences", "remove_fillers"}),
    "shorts": frozenset({"reframe", "captions", "hook"}),
    "captions": frozenset({"transcribe"}),
    "translate_captions": frozenset({"captions"}),
    # A bare music noun ("the song is overpowering me") is WEAK in the
    # grammar, so the model reads it: any level/fade/mute/fit of the bed.
    "music": frozenset({"duck", "fit_music", "volume", "mute", "fade"}),
    "beat_sync": frozenset({"music"}),
    "export_preset": frozenset({"reframe", "loudness"}),
    "fade": frozenset({"fit_music"}),
    "fit_music": frozenset({"fade"}),
}


_NUMERIC_SLOTS = ("duration_s", "db", "lufs", "count", "factor", "to_db", "volume_db", "max_dur",
                  "min_dur", "intensity", "strength", "duration", "dur")
_NUMBER_RE = re.compile(r"\d|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|"
                        r"forty|fifty|sixty|ninety|half|a couple|a few|several|dozen)\b")
_EDGE_IN_RE = re.compile(r"\b(?:in|up|begin\w*|start\w*|opening|open|intro|from black)\b")
_EDGE_OUT_RE = re.compile(r"\b(?:out|away|end\w*|finish\w*|close|closing|outro|to black|melt|tail)\b")


_PICTURE_RE = re.compile(r"\b(?:picture|video|clips?|image|footage|screen|black|dark\w*|white|scene|shot|frame)\b")


def _fade_edge_of(text: str) -> str | None:
    """"ease the picture in from black" → in; "let the ending melt to black"
    → out; both named → both; neither → None (the recipe's default)."""
    i, o = bool(_EDGE_IN_RE.search(text)), bool(_EDGE_OUT_RE.search(text))
    return "both" if i and o else "in" if i else "out" if o else None


def ground_to_prompt(draft: IntentDraft, prompt: str) -> IntentDraft:
    """Keep an on-device draft to what the prompt talks about (QA-018 live
    pass). With the real Apple Intelligence model, "mute the music" came
    back as mute + voiceover, "fade out at the end" as fade + trim +
    remove_music, "stop ducking the music" as duck + shorts: the right edit
    plus unrelated ones, some destructive. When at least one planned recipe
    is one the prompt's words name, the others are dropped (a draft whose
    recipes are ALL unnamed is a paraphrase the grammar cannot read and is
    left to the router's other guards). An exclusion survives only when the
    grammar also reads it as one — the model listed "remove_music" and "do
    not mute the music" as exclusions for requests that said neither."""
    from .. import grammar as G
    from ..planner import _LOWER_THIRD_RE
    mentioned = mentioned_intents(prompt)
    for word in list(mentioned):
        mentioned |= _GROUND_RELATIVES.get(word, frozenset())
    named = [it for it in draft.intents if it.recipe in mentioned]
    intents = list(draft.intents) if ("auto_edit" in mentioned or not named) else named
    from .. import slots as S
    if S.look_of(prompt or "") and not _SOUND_WORD_RE.search((prompt or "").lower()):
        # Final QA r3: "make the whole video black and white" came back as
        # noise_reduce + a loudness target — every clip's sound was replaced
        # and the reply said "Clean audio: done". A prompt that names a
        # colour look and no sound keeps no audio recipe.
        intents = [it for it in intents if it.recipe not in _AUDIO_RECIPES]
    said_not = set(G.detect(prompt or "").exclusions)
    exclusions = [x for x in draft.exclusions if x in said_not]
    text = " ".join((prompt or "").lower().split())
    says_a_number = bool(_NUMBER_RE.search(text))
    named_clip = G.clip_ref_of(prompt or "")
    fixed = []
    for it in intents:
        slots = dict(it.slots)
        if (it.recipe in _ONE_CLIP_CAPABLE and named_clip and named_clip != "$v1_all"
                and str(slots.get("clip_ref") or "").strip().lower() in _ALL_CLIP_WORDS):
            # Final QA: "revers clip 1 pls" / "revrse the 2nd clip" (the
            # grammar misses the typo, Apple Intelligence answers) came back
            # with no clip_ref and reversed EVERY clip: the prompt names one
            # clip, so the draft edits that one.
            slots["clip_ref"] = named_clip
        if (it.recipe == "volume" and named_clip and named_clip != "$v1_all"
                and not _BED_WORD_RE.search(text)
                and str(slots.get("target") or "music").lower() in ("music", "")):
            # Final QA r2: "turn down clip 2 by 6 decibels" came back as
            # set_volume target=music db=-6 — the music got 6 dB LOUDER and
            # clip 2 was untouched. The prompt names a clip and no bed: it is
            # that clip's level, read the way the grammar reads a level
            # ("by N" is a change from its current gain).
            from ..planner import _volume_slots
            level = _volume_slots(text, False)
            slots = {k: v for k, v in slots.items() if k not in ("target", "db", "change", "_delta_db")}
            slots.update({k: v for k, v in level.items() if k in ("db", "change", "_delta_db")})
            slots.update({"target": "voice", "clip_ref": named_clip})
        if not says_a_number:
            # "let the ending melt to black" came back with duration_s=85 (the
            # timeline length), "lower the music" with db=-20: a number the
            # user never said is the recipe's default's job, not the model's.
            for key in _NUMERIC_SLOTS:
                slots.pop(key, None)
        if it.recipe == "fade" and not slots.get("edge"):
            edge = _fade_edge_of(text)
            if edge:
                slots["edge"] = edge
        if (it.recipe == "fade" and slots.get("target") in (None, "video")
                and _BED_WORD_RE.search(text) and not _PICTURE_RE.search(text)):
            # "let the track ease in at the beginning" came back as a PICTURE
            # fade (QA-018 live pass, wave C): the words name the bed only.
            slots["target"] = "music"
        if it.recipe == "duck" and slots.get("enabled") is False and not G.duck_off(prompt or ""):
            # "I want the backing track to sit lower" came back as ducking OFF.
            slots.pop("enabled")
        if it.recipe == "transitions":
            # Final QA r3: "add a wipe between clip 1 and clip 2" came back as
            # Cross Dissolves — the type the prompt names wins.
            named_type = S.transition_type_of(text)
            if named_type and slots.get("type") != named_type:
                slots["type"] = named_type
                slots.pop("look", None)
        if it.recipe == "title" and _LOWER_THIRD_RE.search(text) and slots.get("text"):
            # Final QA: "add a lower third saying Jane Doe, Producer" came back
            # as a SUPER title with that text — a lower-third prompt's words
            # are the card's name (the expander splits the second line off).
            slots["_lower_third"] = True
        if (it.recipe == "title" and slots.get("name") and not slots.get("text")
                and not _LOWER_THIRD_RE.search((prompt or "").lower())):
            # "slap LAUNCH DAY across the top" came back as a NAME card
            # (a lower third) — headline words are the title's text.
            slots["text"] = slots.pop("name")
        fixed.append(it if slots == it.slots else it.model_copy(update={"slots": slots}))
    # The model's own questions and its reply are dropped: every live answer
    # asked "which platform?" (options: the music file's name) for edits that
    # need no platform, and replied "I've stopped ducking the music" before
    # anything ran. The recipes ask what is really missing; the reply is
    # built from what the run did (summary.py).
    return draft.model_copy(update={"intents": fixed, "exclusions": exclusions,
                                    "needs_input": [], "reply": ""})


#: Recipes that change the SOUND (Final QA r3: dropped when the prompt names
#: a colour look and no sound).
_AUDIO_RECIPES = frozenset({"clean_audio", "loudness", "volume", "mute", "duck", "music", "fit_music",
                            "remove_music", "voice_effect", "voiceover", "remove_silences", "remove_fillers",
                            "tighten"})
_SOUND_WORD_RE = re.compile(r"\b(?:audio|sound|sounds|noise|noisy|voice|volume|loud|louder|quiet|quieter|music"
                            r"|song|mute|hiss|hum|speech|mic|silence|silences|filler|fillers)\b")


#: Recipes whose clip_ref DEFAULTS to every clip, so a draft that names no
#: clip edits them all. (The one-clip recipes — delete, duplicate, move,
#: length, flip, zoom, rotate — already ask "which clip?" instead.)
_ONE_CLIP_CAPABLE = frozenset({"reverse", "speed", "voice_effect", "animation", "adjust", "color_look",
                               "stabilize", "upscale", "canvas"})
#: A draft's clip_ref that means "no particular clip".
_ALL_CLIP_WORDS = frozenset({"", "none", "$v1_all", "all", "all clips", "every clip", "everything",
                             "the whole video", "whole video", "all the clips"})

#: The bed, named (the grammar's own music nouns).
_BED_WORD_RE = re.compile(r"\b(?:music|song|track|bed|bgm|soundtrack|tune|score|beat|backing track)\b")
#: The speaker — what ducking is relative to. Without one a duck reading of a
#: level complaint is a guess ("the soundtrack needs to breathe less loudly").
_SPEECH_RE = re.compile(r"\b(?:talk\w*|speak\w*|speech|voice|dialogue|narrat\w*|vocals?|words|says?|saying"
                        r"|someone|anyone|people|when i|while i|whenever i|under me|over me|behind me)\b")
#: Asking for a (new / another) bed rather than about the one on the timeline.
_ADD_BED_RE = re.compile(r"\b(?:add|put|lay|throw in|drop in|use|play|another|new|different|replace|swap|switch"
                         r"|pick|choose|find|want (?:some|a|an)|need (?:some|a|an)|give (?:it|me) (?:some|a))\b")
_REMOVE_BED_RE = re.compile(r"\b(?:remove|delete|get rid of|take (?:it )?(?:out|off)|lose|ditch|scrap|no more"
                            r"|without|cut (?:the )?(?:music|song|track) (?:out|off))\b")
#: A complaint names the direction by its opposite: "too timid" means louder.
_TOO_QUIET_RE = re.compile(r"\btoo (?:quiet|soft|softly|low|timid|weak|faint|thin|subtle|meek)\b"
                           r"|\b(?:can'?t|cannot|barely|hardly) hear (?:the\s+)?(?:music|song|track|bed|tune|soundtrack)\b"
                           r"|\b(?:lost|buried) (?:in|under) the mix\b")
_TOO_LOUD_RE = re.compile(r"\btoo (?:loud|loudly|strong|much|busy|heavy|big|intense|harsh|aggressive)\b"
                          r"|\boverpower\w*|\bdrown\w*|\bbarely audible\b|\bdistract\w*")
_LEVEL_UP_RE = re.compile(r"\b(?:louder|presence|boost\w*|harder|punch\w*|stronger|bigger|pump\w*|lift\w*|swell\w*"
                          r"|bolder|fuller|forward|prominent|up|raise|higher)\b")
_LEVEL_DOWN_RE = re.compile(r"\b(?:quieter|softer|lower|less|down|under|behind|back|tame|subtle|gentle|calm\w*"
                            r"|breathe|recede|sit|tone (?:it )?down|background|duller|smaller)\b")


def music_level_direction(prompt: str) -> str | None:
    """"up" / "down" when the prompt asks for the bed louder / quieter in
    words the grammar has no row for, else None. A complaint wins over the
    imperative words around it ("it hits too softly, make it hit harder")."""
    text = " ".join((prompt or "").lower().replace("’", "'").split())
    if _TOO_QUIET_RE.search(text):
        return "up"
    if _TOO_LOUD_RE.search(text):
        return "down"
    up, down = bool(_LEVEL_UP_RE.search(text)), bool(_LEVEL_DOWN_RE.search(text))
    return "up" if up and not down else "down" if down and not up else None


def ground_music_level(draft: IntentDraft, prompt: str, facts: TimelineFacts | None) -> IntentDraft:
    """A level paraphrase the model read as something else becomes `volume`
    on the bed (QA-018, wave C). The recorded Apple Intelligence misreads:
    "the soundtrack needs to breathe less loudly" → duck; "can the tune sit
    under me more", "the song feels too timid", "the music drowns
    everything, tame it" → add-music (a no-op "music is already on the
    timeline"); "the backing track is overpowering me" → remove the music
    (rejected as ungrounded, so the user got "did you mean").

    Only when a bed EXISTS, the prompt names it, and it says which way:
      * `music` without an add/replace verb → the level of the bed it has;
      * `remove_music` without a removal verb → a level, not a deletion;
      * `loudness` (the programme target) when the words are about the bed;
      * `duck` with no speaker in the prompt — ducking is relative to speech,
        so without one it is a level change.
    A draft that already plans a `volume` keeps it (grounding fills a gap)."""
    if facts is None or not getattr(facts, "has_music", False):
        return draft
    text = " ".join((prompt or "").lower().replace("’", "'").split())
    if not _BED_WORD_RE.search(text):
        return draft
    direction = music_level_direction(text)
    if direction is None:
        return draft
    from .. import grammar as G
    from ..schema import IntentItem

    def _misread(it) -> bool:
        if it.recipe == "music":
            return not _ADD_BED_RE.search(text)
        if it.recipe == "remove_music":
            return not _REMOVE_BED_RE.search(text)
        if it.recipe == "loudness":
            return it.slots.get("lufs") is None
        if it.recipe == "duck":
            return not G.duck_off(text) and not _SPEECH_RE.search(text)
        return False

    if not any(_misread(it) for it in draft.intents):
        return draft
    has_volume = any(it.recipe == "volume" for it in draft.intents)
    items = []
    for it in draft.intents:
        if not _misread(it):
            items.append(it)
        elif not has_volume:
            items.append(IntentItem(recipe="volume", slots={"target": "music", "change": direction}))
            has_volume = True
    return draft.model_copy(update={"intents": items})


def ground_duck_off(draft: IntentDraft, prompt: str) -> IntentDraft:
    """A `duck` intent whose `enabled` the model left unset, on a prompt the
    grammar reads as "turn ducking OFF", gets `enabled=False` (QA-018
    remainder). Unset means "on" to the recipe, so "stop ducking the music"
    planned by an on-device model used to turn ducking ON — the opposite of
    what was asked, and verified as held because the check measured "on".
    A value the model DID set is left alone: grounding fills a gap, it does
    not overrule an answer."""
    from .. import grammar as G       # lazy: grammar imports slots, content is imported early
    if not G.duck_off(prompt or ""):
        return draft
    items = []
    changed = False
    for it in draft.intents:
        if it.recipe == "duck" and it.slots.get("enabled") is None:
            items.append(it.model_copy(update={"slots": {**it.slots, "enabled": False}}))
            changed = True
        else:
            items.append(it)
    return draft.model_copy(update={"intents": items}) if changed else draft


def needs_hook_text(plan: Plan, facts: TimelineFacts) -> bool:
    """True when the plan carries a hook whose text is exactly the recipe
    table's transcript heuristic — the one case a content brain improves on.

    WHY compare against `recipes.heuristic_hook` instead of reading the
    recipe's reply note: the note is prose P may reword; the text the step
    carries is the contract. A hook the user typed (`"add a hook that says
    'X'"`) never equals the heuristic, a hook a brain already wrote sets
    `content_brain`, and with no transcript there are no words for any
    brain to ground a line in — so all three answer False."""
    if plan.content_brain is not None or not (facts.transcript_head or "").strip():
        return False
    guess = heuristic_hook(facts.transcript_head)
    if not guess:
        return False
    return any(s.tool == HOOK_TOOL and str(s.args.get("text", "")).strip() == guess for s in plan.steps)

HOOK_MAX_CHARS = 60          # apply_hook_stack.text ≤ 60 (§1.3 rule 8)
HOOK_MAX_WORDS = 7
HEURISTIC_SOURCE = "heuristic (no local model available)"

#: Words that open a sentence without saying anything; stripped before a
#: sentence is judged or shown.
_OPENERS = frozenset({"so", "okay", "ok", "well", "hi", "hey", "hello", "today", "now", "alright",
                      "right", "yeah", "basically", "actually", "welcome", "guys", "everyone", "and",
                      "but", "um", "uh", "like"})
_FILLERS = frozenset(FILLERS_STRICT)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_WORD = re.compile(r"[A-Za-z0-9'ऀ-ॿ][A-Za-z0-9'\-ऀ-ॿ]*")


def hook_candidates_task(transcript_head: str, *, n: int = 3, max_words: int = HOOK_MAX_WORDS) -> TextTask:
    return TextTask(kind="hook_candidates",
                    payload={"transcript_head": (transcript_head or "")[:1500], "n": n, "max_words": max_words})


def rank_windows_task(windows: list[dict[str, Any]], k: int) -> TextTask:
    clean = [{"start": float(w.get("start", 0.0)), "end": float(w.get("end", 0.0)),
              "text": str(w.get("text", ""))[:300]} for w in windows]
    return TextTask(kind="rank_windows", payload={"windows": clean, "k": int(k)})


# --- sanitising model output ---------------------------------------------------

#: Discourse connectors a model copies from the transcript's list structure
#: ("Second, the battery…"): meaningless on a title card, so they go.
_LEADING_CONNECTOR = re.compile(r"^(?:first(?:ly)?|second(?:ly)?|third(?:ly)?|next|also|and|so|then|finally)[,:]?\s+",
                                re.I)


def _one_line(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip().strip("\"'`“”‘’")
    text = re.sub(r"^\d+[.)]\s*", "", text)          # "1. HOOK" → "HOOK"
    text = _LEADING_CONNECTOR.sub("", text)
    return text.strip()


def _cap_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]).rstrip(",;:—-")


def sanitize_hook_items(items: Iterable[Any], *, max_words: int = HOOK_MAX_WORDS,
                        max_chars: int = HOOK_MAX_CHARS) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items or ():
        if not isinstance(item, str):
            continue
        line = _cap_words(_one_line(item), max_words)
        if len(line) > max_chars:
            line = line[:max_chars].rsplit(" ", 1)[0].rstrip(",;:—-") or line[:max_chars]
        key = line.lower()
        if len(line) < 3 or key in seen:
            continue
        seen.add(key)
        out.append(line)
    return out


def sanitize_ranking(items: Iterable[Any], *, n_windows: int, k: int) -> list[int]:
    out: list[int] = []
    for item in items or ():
        if isinstance(item, bool) or not isinstance(item, (int, float, str)):
            continue
        try:
            idx = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < n_windows and idx not in out:
            out.append(idx)
        if len(out) >= k:
            break
    return out


# --- what a free-text model is asked, and how its answer is read ----------------

def text_task_prompts(task: TextTask) -> tuple[str, str] | None:
    """`(system, user)` for the brains that prompt in free text (MLX, Claude).
    The FM helper has its own typed `@Generable` shapes. None for a task the
    brain cannot serve (empty payload or unknown kind)."""
    if task.kind == "hook_candidates":
        head = str(task.payload.get("transcript_head") or "").strip()
        if not head:
            return None
        n = max(1, min(int(task.payload.get("n") or 3), 6))
        words = max(3, min(int(task.payload.get("max_words") or HOOK_MAX_WORDS), 12))
        # WHY the literal example: with only a schema-ish hint the real 7B
        # answered `{"items": ["a"], ["b"], ["c"]}` (each line in its own
        # list) — unparseable. A worked example fixes the shape; the
        # quoted-string fallback in `parse_text_items` covers the rest.
        example = json.dumps({"items": [f"hook line {i + 1}" for i in range(n)]})
        system = (f"You write short-form video hooks. Propose {n} hook lines, each at most {words} words, "
                  "grounded in what the speaker actually says — a claim, a number, a surprise from the "
                  "transcript. No generic filler like 'wait for it' or 'get ready'. "
                  f"Reply with ONE JSON object exactly like {example} and nothing else.")
        return system, f"Transcript start:\n{head}"
    if task.kind == "rank_windows":
        windows = task.payload.get("windows") or []
        if not windows:
            return None
        k = max(1, min(int(task.payload.get("k") or len(windows)), len(windows)))
        listing = "\n".join(f"[{i}] {w.get('text', '')}" for i, w in enumerate(windows))
        system = ("You pick the most self-contained, engaging moments of a talk for short clips. "
                  f"Return the {k} best window indices, best first, each index at most once. "
                  'Reply with ONE JSON object exactly like {"items": [0, 3, 1]} and nothing else.')
        return system, f"Windows:\n{listing}"
    return None


_QUOTED = re.compile(r'"((?:[^"\\\n]|\\.){3,120})"')
_INDEX_LIST = re.compile(r"\[([\d\s,]+)\]")


def parse_text_items(out: str, kind: str) -> list[Any] | None:
    """The `items` of a free-text answer. `jsonfix.repair` first; when even
    that fails, the strings (or the integers) are still recoverable from a
    mis-bracketed answer, and `sanitize_*` vets every one of them."""
    try:
        items = repair(out).get("items")
        if isinstance(items, list):
            return items
    except JsonRepairFailed:
        pass
    if kind == "hook_candidates":
        strings = [m.group(1) for m in _QUOTED.finditer(out or "")]
        return [s for s in strings if s.strip().lower() not in ("items",)] or None
    if kind == "rank_windows":
        m = _INDEX_LIST.search(out or "")
        return [int(x) for x in m.group(1).replace(",", " ").split()] if m else None
    return None


# --- the transcript-derived fallback -------------------------------------------

def _content_words(sentence: str) -> list[str]:
    words = [w for w in _WORD.findall(sentence)]
    while words and words[0].lower().strip("'") in _OPENERS:
        words = words[1:]
    return [w for w in words if w.lower().strip("'") not in _FILLERS]


def _sentence_score(words: list[str]) -> float:
    """Prefer a sentence with a claim in it: a number, a question word, a
    comparative, or an imperative — over a pleasantry."""
    if len(words) < 3:
        return -1.0
    text = " ".join(words).lower()
    score = min(len(words), 8) / 8.0
    if re.search(r"\d", text):
        score += 1.0
    if re.search(r"\b(why|how|what|never|always|secret|mistake|wrong|best|worst|only|stop)\b", text):
        score += 0.8
    if re.search(r"\b(than|most|every|nobody|everyone)\b", text):
        score += 0.4
    if re.search(r"\b(thanks? for|subscribe|my name is|in this video|welcome back)\b", text):
        score -= 1.5
    return score


def heuristic_hook_candidates(transcript_head: str, *, n: int = 3,
                              max_words: int = HOOK_MAX_WORDS) -> list[str]:
    """Hook lines from the speaker's own words: the strongest early sentence
    first, then the first sentence, then the strongest sentence trimmed to
    its first clause. Upper-cased like `generate_hook`'s heuristic, but with
    fillers and throat-clearing removed. Empty transcript → []."""
    sentences = [s for s in _SENTENCE_SPLIT.split(transcript_head or "") if s.strip()][:12]
    scored = []
    for order, sentence in enumerate(sentences):
        words = _content_words(sentence)
        score = _sentence_score(words) - order * 0.05
        if words:
            scored.append((score, order, words))
    if not scored:
        return []
    best = max(scored, key=lambda t: t[0])
    first = min(scored, key=lambda t: t[1])
    clause = re.split(r"[,;:—]| but | because | which ", " ".join(best[2]), maxsplit=1)[0].split()
    raw = [" ".join(best[2]), " ".join(first[2]), " ".join(clause)]
    raw += [" ".join(w) for s, o, w in sorted(scored, reverse=True) if (s, o, w) not in (best, first)]
    return sanitize_hook_items((line.upper() for line in raw), max_words=max_words)[:n]


# --- asking the brains -------------------------------------------------------------

@dataclass(frozen=True)
class HookText:
    text: str
    brain: str            # BRAIN id that wrote it; "recipes" for the heuristic
    source: str           # human note for the reply, e.g. "hook text: Apple Intelligence"
    candidates: tuple[str, ...] = ()

    @property
    def content_brain(self) -> str | None:
        """What goes in `plan.content_brain`: None when no model wrote it."""
        return None if self.brain == "recipes" else self.brain


@dataclass(frozen=True)
class RankResult:
    order: tuple[int, ...]
    brain: str


def _ask(brains: Iterable[Brain], task: TextTask, *, timeout_s: float) -> TextResult | None:
    for brain in brains:
        try:
            if not brain.availability().get("available"):
                continue
            result = brain.text(task, timeout_s=timeout_s)
        except Exception:      # a content task must never take the plan down
            continue
        if result is not None and result.items:
            return result
    return None


def hook_text(brains: Iterable[Brain], transcript_head: str, *, n: int = 3,
              max_words: int = HOOK_MAX_WORDS, timeout_s: float = 6.0) -> HookText:
    """The hook line for `apply_hook_stack(text=…)`: the first sanitised
    candidate from the first brain that answers, else the transcript
    heuristic. Never raises; never returns an empty string."""
    task = hook_candidates_task(transcript_head, n=n, max_words=max_words)
    result = _ask(brains, task, timeout_s=timeout_s)
    if result is not None:
        items = sanitize_hook_items(result.items, max_words=max_words)
        if items:
            label = BRAIN_LABELS.get(result.brain, result.brain)
            return HookText(text=items[0], brain=result.brain, source=f"hook text: {label}",
                            candidates=tuple(items))
    items = heuristic_hook_candidates(transcript_head, n=n, max_words=max_words)
    text = items[0] if items else "WATCH THIS"
    return HookText(text=text, brain="recipes", source=f"hook text: {HEURISTIC_SOURCE}",
                    candidates=tuple(items))


def rank_windows(brains: Iterable[Brain], windows: list[dict[str, Any]], k: int, *,
                 timeout_s: float = 6.0) -> RankResult:
    """Ordered window indices, best first. Fallback = the windows' own order
    (what `make_shorts` would do anyway), attributed to `recipes`."""
    k = max(1, min(int(k), len(windows))) if windows else 0
    if not windows:
        return RankResult(order=(), brain="recipes")
    result = _ask(brains, rank_windows_task(windows, k), timeout_s=timeout_s)
    if result is not None:
        order = sanitize_ranking(result.items, n_windows=len(windows), k=k)
        if order:
            return RankResult(order=tuple(order), brain=result.brain)
    return RankResult(order=tuple(range(k)), brain="recipes")
