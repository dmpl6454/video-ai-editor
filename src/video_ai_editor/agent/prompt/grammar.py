"""Intent grammar — which recipes a prompt asks for, and how sure we are (§2.3).

`detect(prompt) -> Detection`: the prompt is split into clauses on `,`,
`and`, `then`, `aur`, `phir`, `;`; each clause is scored against a phrase
table (1.0 exact phrase · 0.85 synonym · 0.5 weak keyword) and yields at most
one intent — except the deliberate composites (`tighten`, `auto_edit`,
templates) which ARE one intent. Negated clauses ("no captions", "without
music", "bina music") become `exclusions` and never a hit.

Confidence (§2.7) = mean(clause_scores) × coverage, where coverage is the
share of clauses that produced a hit or an exclusion. The router runs recipes
alone at ≥ 0.75, lets the on-device brains normalise between 0.4 and 0.75,
and asks at < 0.4.

WHY a precedence table for `cut` instead of a smarter regex: "cut" is the
most overloaded verb in editing. `cut to the beat` is beat_sync, `cut the
first 5 seconds` is trim, `cut out the ums` is remove_fillers, `cut into 3`
is shorts, `cut the silences` is remove_silences, and bare `cut` with
nothing else is a trim only when it has a range. The table below is checked
IN ORDER before the phrase table so the specific reading always wins, and
tests/test_prompt_grammar.py pins every row.

WHY templates are read from presets, not hard-coded: `presets/templates/*.json`
name and describe whole edits ("lecture cleanup", "talking head reel"). Their
`keywords` join the grammar as `auto_edit` variants, so a new template file
is a new vocabulary entry with no code change.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import slots as S
from . import canvas_vocab as _CV
from . import voice_vocab as _VV
from . import anim_vocab as _AV

INTENTS: tuple[str, ...] = (
    "auto_edit", "captions", "translate_captions", "remove_silences", "remove_fillers", "tighten",
    "shorts", "reframe", "music", "duck", "beat_sync", "hook", "color_look", "clean_audio",
    "loudness", "speed", "trim", "title", "brand", "end_card", "transitions", "export_preset",
    "voiceover", "stabilize", "upscale", "undo", "redo", "ask",
    # QA-018: the everyday one-liners — fades, levels, mutes, fitting the bed.
    "fade", "volume", "mute", "fit_music", "audit", "preview", "remove_music",
    # QA-037: play a clip backwards (or forwards again).
    "reverse",
    # Wave D (review RD2): a freeze frame, and a split at a named moment.
    "freeze", "split",
    # Wave D3 (E3 key-free sweep): the CapCut clip edits — delete / duplicate /
    # move a clip, zoom (Ken Burns, punch-in, a zoom level), rotate, the Adjust
    # sliders (brightness, contrast, saturation) — and stickers, which a plan
    # may not place (add_sticker fetches artwork), so they get an honest reply.
    "delete_clip", "duplicate", "move_clip", "zoom", "rotate", "adjust", "sticker",
    # Final QA: change what an existing text SAYS ("change the Summer Trip
    # text to Winter Trip") — it ADDED a new title reading "to Winter Trip".
    "retext",
    # Final QA: an overlay's opacity or position ("make the overlay 50%
    # transparent", "move the overlay to the top right corner") — no plan
    # tool sets them yet, so the reply says where (Apple Intelligence asked
    # "cannot remove that" / "which blend mode?" instead).
    "transform",
    # "remove the captions / the filter / the transitions": no plan tool can
    # find those by name, so the reply says where to do it — it used to ADD
    # captions and apply a LUT (the E3 sweep's wrong edits).
    "remove_feature",
    # Flip / mirror (wave E, F4b): the Transform's flip_h / flip_v, toggled
    # like CapCut's Mirror; "turn it upside down" stays a 180° rotation.
    "flip",
    # Wave E (F4b): "trim the second clip to 2 seconds", "make the first clip
    # 3 seconds long" — a clip's LENGTH, not a range cut out of the video.
    "clip_length",    # Wave E (F2): CapCut Canvas (blur / colour / picture behind a
    # letterboxed clip) and an overlay clip's blend mode (agent/prompt/
    # canvas_expanders.py; words in canvas_vocab.py).
    "canvas", "blend",
    # Wave E (F3): CapCut's voice changer on a clip's sound (agent/prompt/
    # voice_expanders.py; words in voice_vocab.py).
    "voice_effect",
    # Wave E (F1): CapCut clip animations (In / Out / Combo) on a clip or a
    # sticker (agent/prompt/anim_expanders.py).
    "animation",
)

EXACT, SYNONYM, WEAK = 1.0, 0.85, 0.5

#: Confidence policy (§2.7).
RUN_THRESHOLD = 0.75
NORMALISE_THRESHOLD = 0.4


@dataclass(frozen=True)
class IntentHit:
    intent: str
    score: float
    clause: str
    slots: S.Slots
    template: str | None = None      # auto_edit variant from presets/templates


@dataclass(frozen=True)
class Detection:
    prompt: str
    clauses: tuple[str, ...]
    hits: tuple[IntentHit, ...]
    exclusions: tuple[str, ...]
    slots: S.Slots                   # whole-prompt slots (cross-clause: platform, language …)
    unmatched: tuple[str, ...] = field(default_factory=tuple)

    @property
    def confidence(self) -> float:
        total = len(self.clauses)
        if total == 0:
            return 0.0
        matched = len(self.hits) + len(self.unmatched_exclusion_clauses)
        scores = [h.score for h in self.hits] + [EXACT] * len(self.unmatched_exclusion_clauses)
        if not scores:
            return 0.0
        return round((sum(scores) / len(scores)) * (min(matched, total) / total), 3)

    @property
    def unmatched_exclusion_clauses(self) -> tuple[str, ...]:
        """Clauses that were pure negations — they count as understood."""
        hit_clauses = {h.clause for h in self.hits}
        return tuple(c for c in self.clauses if c not in hit_clauses and c not in self.unmatched)

    @property
    def intents(self) -> list[str]:
        return [h.intent for h in self.hits]

    @property
    def tier(self) -> str:
        c = self.confidence
        if c >= RUN_THRESHOLD:
            return "run"
        if c >= NORMALISE_THRESHOLD:
            return "normalise"
        return "clarify"


# --------------------------------------------------------------------------
# 1. Clause splitting
# --------------------------------------------------------------------------

_QUOTE_RE = re.compile(r"[\"“”']([^\"“”']{1,200})[\"“”']")
#: A QUOTED span as a person types one: the opening mark after the start or a
#: space, the closing one before the end, a space or punctuation — so the
#: apostrophes in "don't" and "it's" are never taken for quotes.
_TEXT_QUOTE_RE = re.compile(r"(?:(?<=^)|(?<=[\s(:]))([\"“”'‘])([^\"“”‘]{1,200}?)([\"“”'’])(?=$|[\s.,;:!?)])")


def mask_quotes(clause: str) -> str:
    """`clause` with each quoted span's WORDS replaced by a neutral token and
    its marks kept: the words of a title are the title, never an edit
    ("add a title 'Chop the onions'" asked "Which part should I cut?", and
    "'Trim the fat'" cut the silences). Rows that look for a quote still
    see one; the slots are read from the unmasked clause."""
    return _TEXT_QUOTE_RE.sub(lambda m: f"{m.group(1)}q{m.group(3)}", clause)
_AMOUNT_TAIL_RE = re.compile(r",\s*(?:like|about|around|roughly|say|maybe|by|of)\s+[-+]?\d+(?:\.\d+)?\s*"
                             r"(?:d\s?b|decibels?|%|percent|x|s|sec|secs|seconds?)\b(?=\s*(?:[,;.!?]|$))")
_LOWER_THIRD_TWO_LINES_RE = re.compile(
    r"(?<=lower third)(?:\s*[:—-]\s*|\s+(?:saying|that says|reading|showing|for|with)\s+)"
    r"[^,;:]{1,40},\s*[^,;]{1,40}?(?=\s*(?:[,;]|\band\b|\bthen\b|$))")
_SPLIT_RE = re.compile(r"\s*(?:,|;|\bthen\b|\band then\b|\band also\b|\band\b|\bplus\b|\bafter that\b)\s*")

#: `and` inside these phrases joins words, not clauses.
_PROTECTED = (
    "black and white", "b and w", "you know", "so basically", "name and handle", "hook and", "back and forth",
    "in and out", "cut and pulse", "silences and fillers", "silences and filler words",
    "fillers and silences", "pauses and fillers", "ums and uhs", "ums and ahs", "um and uh",
    "clean and", "warm and", "loud and clear", "rock and roll", "drum and bass",
    "second and a half", "seconds and a half", "sec and a half", "one and a half",
)


#: Ordinal words a clip reference uses ("the second clip", "the 3rd shot").
ORDINAL = (r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\d{1,2}(?:st|nd|rd|th)"
           r"|last|final|opening|closing|middle|penultimate|second[- ]to[- ]last|next[- ]to[- ]last)")
#: What a clip is called in a prompt.
CLIP_NOUN = r"(?:clips?|shots?|segments?|scenes?|video clips?)"
#: A reference to ONE clip: "the second clip", "clip 2", "the selected clip",
#: "the last one", "the clip at 0:05" (the planner resolves which one).
CLIP_PHRASE = (rf"(?:(?:the\s+|that\s+|this\s+)?(?:{ORDINAL}|selected|current|this|that)\s+(?:{CLIP_NOUN}|one|part|bit|section|piece)\b"
               rf"|(?:the\s+)?{CLIP_NOUN}\s+(?:#\s*|number\s+|no\.?\s*)?(?:\d{{1,2}}|one|two|three|four|five|six|seven|eight|nine|ten)\b"
               rf"|(?:the\s+|this\s+|that\s+|all\s+(?:the\s+|of\s+the\s+)?|every\s+|each\s+){CLIP_NOUN}\b)")
#: "the first and second clip", "clips 1 and 2": one reference, not two clauses.
_ORDINAL_PAIR_RE = re.compile(rf"\b({ORDINAL})\s+and\s+((?:the\s+)?{ORDINAL})\b|\b({CLIP_NOUN}\s+\d{{1,2}})\s+and\s+(\d{{1,2}})\b"
                              # Final QA r3: "between clip 1 and clip 2" is one seam, not two edits
                              rf"|\bbetween\s+(?:the\s+)?{CLIP_NOUN}\s+\d{{1,2}}\s+and\s+(?:the\s+)?{CLIP_NOUN}\s+\d{{1,2}}\b")


_ENUM_N = r"(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)(?!\s*(?:s|sec|secs|seconds?|%|x)\b)(?!\.\d)"
_ENUM_NOUN = r"(?:clips?|shots?|scenes?|segments?)"
#: "clip 2 and clip 4", "clip 1, clip 3 and clip 5": the noun said once per
#: number (final sweep 4: the clause split made "make clip 2" / "clip 4
#: black and white" — the look landed on one clip and the net rolled back).
_ENUM_NUM_RE = re.compile(rf"(?<!\bbetween )\b(?:the\s+)?{_ENUM_NOUN}\s+(?:#\s*|number\s+)?({_ENUM_N})"
                          rf"((?:\s*,\s*(?:the\s+)?{_ENUM_NOUN}\s+(?:#\s*|number\s+)?{_ENUM_N})*)"
                          rf"\s+(?:and|&)\s+(?:the\s+)?{_ENUM_NOUN}\s+(?:#\s*|number\s+)?({_ENUM_N})\b")
#: "the second clip and the fourth clip" → "the second and fourth clips".
_ENUM_ORD_RE = re.compile(rf"(?<!\bbetween )\b(?:the\s+)?({ORDINAL})\s+{_ENUM_NOUN}"
                          rf"((?:\s*,\s*(?:the\s+)?{ORDINAL}\s+{_ENUM_NOUN})*)\s+(?:and|&)\s+(?:the\s+)?({ORDINAL})\s+{_ENUM_NOUN}\b")


_CLIP_LIST_RE = re.compile(rf"\b{_ENUM_NOUN}\s+{_ENUM_N}(?:\s*,\s*{_ENUM_N})+\s+(?:and|&)\s+{_ENUM_N}\b"
                           rf"|\bthe\s+{ORDINAL}(?:\s*,\s*{ORDINAL})+\s+(?:and|&)\s+{ORDINAL}\s+{_ENUM_NOUN}\b")
_IN_OUT_OVER_RE = re.compile(r"\b(?:in|out)\s+(?:over|for|in)\s+(?:\d+(?:\.\d+)?|half a|a|one|two|three)\s*(?:s|sec|secs|seconds?)?"
                             r"\s+and\s+(?:in|out)\s+(?:over|for|in)\s+(?:\d+(?:\.\d+)?|half a|a|one|two|three)\s*(?:s|sec|secs|seconds?)?\b")


def join_clip_enumerations(text: str) -> str:
    """One clip list from a repeated-noun enumeration: "clip 2 and clip 4" →
    "clips 2 and 4", "the second clip and the fourth clip" → "the second and
    fourth clips". Anything else is returned as it was."""
    def _nums(m: re.Match) -> str:
        mids = re.findall(rf"\b{_ENUM_NOUN}\s+(?:#\s*|number\s+)?({_ENUM_N})", m.group(2) or "")
        nums = [m.group(1), *mids, m.group(3)]
        return "clips " + ", ".join(nums[:-1]) + f" and {nums[-1]}"

    def _ords(m: re.Match) -> str:
        mids = re.findall(rf"\b({ORDINAL})\s+{_ENUM_NOUN}", m.group(2) or "")
        words = [m.group(1), *mids, m.group(3)]
        return "the " + ", ".join(words[:-1]) + f" and {words[-1]} clips"

    return _ENUM_ORD_RE.sub(_ords, _ENUM_NUM_RE.sub(_nums, text))


def split_clauses(prompt: str) -> list[str]:
    """Clauses of a normalised prompt. Quoted spans are opaque (an `and`
    inside quotes is text, and the quotes are restored afterwards)."""
    text = join_clip_enumerations(S.normalize(prompt))
    if not text:
        return []
    keep: dict[str, str] = {}

    def _stash(m: re.Match) -> str:
        key = f"\x00{len(keep)}\x00"
        keep[key] = m.group(0)
        return key

    text = _QUOTE_RE.sub(_stash, text)
    # Final sweep 3: "make the music louder, like 3db" — the amount after the
    # comma belongs to the edit before it (it was a clause of its own, read
    # as nothing, and the music went up the default 6 dB).
    text = _AMOUNT_TAIL_RE.sub(_stash, text)
    # Final sweep 4: "clips 1, 3 and 5" is one list; "in over 1 second and
    # out over 3" is one fade's two edges (the tail was a clause of its own,
    # read as nothing).
    text = _CLIP_LIST_RE.sub(_stash, text)
    text = _IN_OUT_OVER_RE.sub(_stash, text)
    # Final QA: "add a lower third saying Jane Doe, Producer" — the comma is
    # the card's second line, not a second edit.
    text = _LOWER_THIRD_TWO_LINES_RE.sub(_stash, text)
    # "swap the first and second clip" / "between clips 2 and 3" name clips,
    # not two edits: the pair is stashed like a quote so `and` cannot split it.
    text = _ORDINAL_PAIR_RE.sub(_stash, text)
    for i, phrase in enumerate(_PROTECTED):
        text = text.replace(phrase, phrase.replace(" and ", f"\x01{i}\x01"))
    parts = [p for p in _SPLIT_RE.split(text) if p and p.strip()]
    out: list[str] = []
    for p in parts:
        for i, phrase in enumerate(_PROTECTED):
            p = p.replace(f"\x01{i}\x01", " and ")
        for key, val in keep.items():
            p = p.replace(key, val)
        p = p.strip(" .,")
        if p:
            out.append(p)
    return out


_ORDINAL_N: dict[str, int] = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
                              "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "opening": 1}
#: What ONE clip may be called when an ordinal points at it ("the second
#: part", "the last bit").
_PIECE = rf"(?:{CLIP_NOUN}|one|part|bit|section|piece)"
_NTH_CLIP_RE = re.compile(rf"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|opening|(\d{{1,2}})(?:st|nd|rd|th))"
                          rf"\s+{_PIECE}\b")
_CLIP_NUM_RE = re.compile(rf"\b{CLIP_NOUN}\s+(?:#\s*|number\s+|no\.?\s*)?(\d{{1,2}}|one|two|three|four|five|six|seven|eight|nine|ten)\b"
                          # "every clip 1.5x" is a speed; final sweep 4: "the last
                          # clip 20% brighter" is not clip 20 ("%" has no word boundary)
                          r"(?!\s*(?:s|sec|secs|seconds?|x)\b)(?!\s*(?:%|percent))(?!\.\d)")
_NUM_WORD = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_PENULT_RE = re.compile(rf"\b(?:second[- ]to[- ]last|next[- ]to[- ]last|penultimate|second[- ]last)\s+{_PIECE}\b")
_MIDDLE_RE = re.compile(rf"\bmiddle\s+{_PIECE}\b|\bin the middle\b")
_LAST_CLIP_RE = re.compile(rf"\b(?:last|final|closing)\s+{_PIECE}\b")
#: "the intro" / "the outro" of a multi-clip edit are its first / last clip
#: ("the intro drags, speed it up" sped up EVERY clip).
_INTRO_RE = re.compile(r"\bthe\s+(?:intro|opening)\b(?!\s+(?:text|title|music|song|card|line|hook|marker)\b)")
_OUTRO_RE = re.compile(r"\bthe\s+(?:outro|ending)\b(?!\s+(?:text|title|music|song|card|line|screen|marker)\b)")
_CLIP_AT_RE = re.compile(rf"\b{CLIP_NOUN}\s+(?:at|around|that starts at|starting at)\s+"
                         r"(?:(\d{1,2}):(\d{2}(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)?)\b")
_THIS_CLIP_RE = re.compile(rf"\b(?:this|that|the selected|selected|current|the current)\s+(?:{CLIP_NOUN}|one|part|bit|section|portion)\b"
                           r"|\bthe selection\b")
#: Internal clip references the expanders bind to a real id against facts
#: (never reach validate_plan): the N-th v1 clip (1-based; -2 = second to
#: last) and the v1 clip under a timeline time.
NTH_REF, AT_REF = "$v1_nth:", "$v1_at:"


def clip_ref_of(text: str) -> str | None:
    """Which ONE clip `text` names, as a sentinel or an internal reference:
    "the second clip" → `$v1_nth:2`, "clip 3" → `$v1_nth:3`, "the first clip"
    → `$v1_first`, "the last one" → `$v1_last`, "the second to last clip" →
    `$v1_nth:-2`, "the clip at 0:05" → `$v1_at:5.0`, "this clip" →
    `$selected`, "every clip" → `$v1_all`. None when no clip is named."""
    t = S.normalize(text or "")
    if not t:
        return None
    if m := _CLIP_AT_RE.search(t):
        secs = int(m.group(1)) * 60 + float(m.group(2)) if m.group(1) is not None else float(m.group(3))
        return f"{AT_REF}{round(secs, 3):g}"
    if _PENULT_RE.search(t):
        return f"{NTH_REF}-2"
    if _MIDDLE_RE.search(t):
        return f"{NTH_REF}mid"
    if m := _NTH_CLIP_RE.search(t):
        n = int(m.group(2)) if m.group(2) else _ORDINAL_N[m.group(1)]
        return "$v1_first" if n == 1 else f"{NTH_REF}{n}"
    if m := _CLIP_NUM_RE.search(t):
        n = int(m.group(1)) if m.group(1).isdigit() else _NUM_WORD[m.group(1)]
        return "$v1_first" if n == 1 else f"{NTH_REF}{n}"
    if _LAST_CLIP_RE.search(t):
        return "$v1_last"
    if _THIS_CLIP_RE.search(t):
        return "$selected"
    ref = S.extract(t).clip_ref
    if ref is None and _INTRO_RE.search(t):
        return "$v1_first"
    if ref is None and _OUTRO_RE.search(t):
        return "$v1_last"
    return ref


# --------------------------------------------------------------------------
# 2. Negation
# --------------------------------------------------------------------------

_NEG_TARGETS: tuple[tuple[str, str], ...] = (
    (r"captions?|subtitles?|subs", "captions"),
    (r"music|song|track|bed|bgm|soundtrack", "music"),
    (r"hook|hook text|opener", "hook"),
    (r"transitions?", "transitions"),
    (r"lut|colou?r (?:grade|look)|grade|look|filter", "color_look"),
    (r"watermark|brand(?: kit)?|logo", "brand"),
    (r"end ?card|outro", "end_card"),
    (r"voice ?over|narration|tts", "voiceover"),
    (r"reframe|crop|resize|reframing", "reframe"),
    (r"speed ?up|speed changes?|speed", "speed"),
    (r"cuts?|trims?|cutting|trimming", "trim"),
    (r"silences?|pauses?", "remove_silences"),
    (r"fillers?|ums|uhs", "remove_fillers"),
    (r"noise reduction|denoise|denoising|audio clean ?up", "clean_audio"),
    (r"loudness|normali[sz]ation|normali[sz]e", "loudness"),
    (r"translation|translate", "translate_captions"),
    (r"duck(?:ing)?|sidechain(?:ing)?|auto[- ]?duck(?:ing)?", "duck"),
)
_NEG_RE = re.compile(
    r"(?:\bno\b|\bwithout\b|\bdon'?t\b|\bdo not\b|\bskip(?:\s+the)?\b|\bleave out\b|\bnot?\s+any\b|\bnever\b|\bminus\b|\bexcept\b|\bbut no\b|\bnahi\b|\bmat\b)"
    r"\s+(?:the\s+|any\s+|a\s+|an\s+)?(?:add(?:ing)?\s+|put(?:ting)?\s+)?(?:the\s+|a\s+|an\s+|any\s+)?"
    r"(?P<what>[a-z][a-z ]{1,40}?)(?=$|\s+(?:and|or|but|,|please|though|either|just)\b|[,.!?]|\s+(?:on|in|to|for)\b)")


def exclusions_in(clause: str) -> list[str]:
    out: list[str] = []
    for m in _NEG_RE.finditer(clause):
        what = m.group("what").strip()
        for pat, intent in _NEG_TARGETS:
            if re.fullmatch(rf"(?:{pat})(?:\s+\w+)?", what) or re.match(rf"(?:{pat})\b", what):
                if intent not in out:
                    out.append(intent)
                break
    return out


#: A duck clause that asks for ducking to be OFF (QA-031). Checked on the
#: clause the `duck` hit came from — "turn off ducking", "turn ducking off",
#: "stop ducking", "disable auto-duck", "don't duck the music", "no ducking",
#: "unduck". Without it every one of those produced `set_duck(enabled=True)`
#: at confidence 1.0 and a VERIFIED card.
_DUCK_OFF_RE = re.compile(
    r"\b(?:turn(?:ed)?|switch(?:ed)?|shut)\s+(?:the\s+)?(?:music\s+)?(?:(?:auto[- ]?)?duck(?:ing)?\s+)?off\b"
    r"|\b(?:turn|switch)\s+off\b|\bstop(?:ped)?\s+(?:the\s+)?(?:auto[- ]?)?(?:duck|sidechain)"
    r"|\bstop(?:ped)?\s+(?:the\s+)?(?:music|song|track|soundtrack|bed)\s+(?:from\s+)?(?:duck|sidechain)"
    r"|\bdisabl\w*|\bdeactivat\w*|\bdon'?t\b|\bdo not\b|\bno\b|\bwithout\b|\bnever\b|\bnot\b"
    r"|\bun-?duck\w*|\bremove (?:the )?(?:auto[- ]?)?(?:duck|sidechain)\w*|\b(?:duck(?:ing)?|sidechain) off\b"
    r"|\b(?:kill|cancel|drop) (?:the )?(?:auto[- ]?)?(?:duck|sidechain)\w*|\bnahi\b|\bband karo\b|\bmat\b"
    r"|\b(?:undo|revert|get rid of|lose|remove) (?:the |all (?:the )?|that )?(?:auto[- ]?)?(?:duck|sidechain)\w*"
    # final sweep 2 r2: "remove the music ducking" turned ducking ON
    r"|\b(?:undo|revert|get rid of|lose|remove|clear|kill|cancel|drop|disable|take off|turn off|switch off|stop)\s+"
    r"(?:the\s+|all\s+(?:the\s+)?|that\s+|any\s+)?(?:background\s+)?(?:music|song|soundtrack|bed)(?:'s)?\s+"
    r"(?:auto[- ]?)?(?:duck|sidechain)\w*"
    # QA-018 paraphrases: "stop the music from lowering when I talk", "keep
    # the soundtrack steady under my voice" — ducking OFF, said in effects.
    r"|\bstop(?:ped)?\s+(?:the\s+)?(?:background\s+)?(?:music|song|track|soundtrack|tune|bed)\s+from\s+"
    r"(?:lowering|dipping|ducking|dropping|going down|getting quieter)"
    r"|\bkeep\s+(?:the\s+)?(?:background\s+)?(?:music|song|track|soundtrack|tune|bed)\s+"
    r"(?:steady|level|constant|at (?:the same|one|a constant) level)"
    r"|\b(?:music|song|track|soundtrack|tune|bed)\s+stop\s+(?:dipping|lowering|ducking|dropping)\b")


def duck_off(clause: str) -> bool:
    """True when a `duck` clause asks for ducking to be turned OFF."""
    return bool(_DUCK_OFF_RE.search(S.normalize(clause)))


#: A `reverse` clause asking for FORWARDS playback again (QA-037).
_REVERSE_OFF_RE = re.compile(
    r"\bun-?reverse\w*|\bforwards?\b|\bnormally again\b|\bnormal direction\b|\bno longer revers\w*"
    r"|\b(?:stop|turn off|remove|undo|cancel|drop|get rid of|take off)\s+(?:the\s+)?(?:reverse|reversing|reversal|backwards?)\w*"
    r"|\b(?:don'?t|do not|not|never)\s+(?:play\s+)?(?:it\s+|this\s+)?(?:reverse|backwards?)\w*|\bseedha\b|\bsidha\b")


def reverse_off(clause: str) -> bool:
    """True when a `reverse` clause asks for the clip to play forwards again."""
    return bool(_REVERSE_OFF_RE.search(S.normalize(clause)))


def strip_negations(clause: str) -> str:
    """The clause with negated phrases removed, so "add music but no captions"
    still yields `music`."""
    return _NEG_RE.sub(" ", clause).strip(" ,")


# --------------------------------------------------------------------------
# 3. Phrase tables
# --------------------------------------------------------------------------

_HAS_RANGE = r"(?:\bfirst\b|\blast\b|\bfrom\b|\bbetween\b|\d+\s*(?:s|sec|secs|seconds?|m|min|mins|minutes?)\b|\d{1,2}:\d{2})"

#: A verb that takes one CLIP away ("delete the second clip", "ripple delete
#: this clip", "remove clip 3", "delete it"). Not "remove the first 5 seconds"
#: (no clip noun) and not "remove the clip's audio / the filter on the clip".
_DELETE_CLIP = (rf"\b(?:ripple[- ]?)?(?:delete|remove|drop|get rid of|take out|cut out|lose|trash|erase|ditch|scrap|kill)"
                rf"\s+{CLIP_PHRASE}(?!\s*'s\b)(?!\s+(?:audio|sound|music|voice|captions?|text|filter|effect|"
                r"transitions?|fades?|keyframes?|speed|look|colou?r)\b)"
                r"|\bripple[- ]?delete\b|^(?:ripple[- ]?)?(?:delete|remove)\s+(?:it|this|that)$")
#: Taking away something a clip or the timeline CARRIES (not a clip, not the
#: music bed, not silences or noise — those have their own recipes).
#: Wave E (F4b): the recipe removes captions, filters, transitions and text by
#: name (`remove_feature`); one look / quoted word may sit before the noun
#: ("the black and white filter", "the 'SALE' text") — "remove the black and
#: white filter" used to APPLY the mono look.
#: The Effects panel's effects by name (review RE: "take off the vignette"
#: got an unrelated clarify card; "delete the vintage filter" looked only at
#: the LUT looks). Never a bare "blur": "remove the background blur" is the
#: canvas.
FX_NOUN = (r"(?:vignett(?:e|ing)|film grain|grain|vintage effect|vhs(?: effect| look)?|glow(?: effect)?"
           r"|rgb[- ]split|chromatic aberration|sharpen(?:ing)?(?: effect)?|blur effect|flip [hv] effect)")
REMOVE_FEATURE = (r"\b(?:remove|delete|get rid of|take off|take out|clear|turn off|switch off|hide|drop|lose|kill|strip|undo)"
                  r"\s+(?:the\s+|all\s+(?:of\s+)?(?:the\s+|my\s+)?|my\s+|every\s+|that\s+|this\s+|those\s+|these\s+|both\s+)?"
                  r"(?:black and white\s+|b ?& ?w\s+|teal (?:and |& )?orange\s+|[\"'“”][^\"'“”]{1,60}[\"'“”]\s+|[\w'-]+\s+"
                  # Final QA: a NAME of up to four words ("delete the Summer
                  # Trip title" asked what a NEW title should say) — never a
                  # clip / position word, so "remove the clip with the title"
                  # stays a clip delete.
                  r"|(?:(?!(?:clips?|shots?|video|from|on|in|with|that|which|at|and|of)\b)[\w'-]+\s+){2,4})?"
                  r"(?:captions?|subtitles?|subs|filters?|luts?|looks?|colou?r grade|colou?r grading|grades?|grading|effects?"
                  r"|transitions?|cross[- ]?fades?|dissolves?|text|titles?|lower thirds?|hooks?|keyframes?|zoom|ken burns"
                  r"|stickers?|emojis?|watermark|end ?card|speed ramp|speed curve|freeze(?: frame)?)\b"
                  r"(?!\s*(?:from|on)\s+the\s+music)"
                  r"|\b(?:remove|delete|get rid of|take off|take out|clear|turn off|switch off|drop|lose|kill|strip)"
                  r"\s+(?:the\s+|all\s+(?:the\s+)?|my\s+|that\s+|this\s+)?" + FX_NOUN + r"\b"
                  r"|\b(?:take|turn|switch|strip|get)\s+(?:the\s+|that\s+|this\s+)?" + FX_NOUN + r"\s+off\b"
                  # a LOOK named by its words alone: "take the black and white
                  # off" / "turn the black and white off" APPLIED black and
                  # white to every clip (Final sweep 2)
                  r"|\b(?:take|turn|switch|strip|get)\s+(?:the\s+|that\s+|this\s+)?"
                  r"(?:black and white|b ?& ?w|b and w|mono(?:chrome)?|gr[ae]y ?scale|sepia)\s+(?:off|out)\b"
                  # final sweep 4: a look named by its one word between the verb
                  # and the particle ("take the warm off clip 1" re-APPLIED warm)
                  r"|\b(?:take|strip|peel|get)\s+(?:the\s+|that\s+|this\s+)?"
                  r"(?:warm|cool|cold|cinematic|teal[- ]orange|punchy?|vivid|faded|vintage|retro|film|moody|golden"
                  r"|sunset|icy|matte|nostalgic|blockbuster|filmic)\s+(?:look\s+|filter\s+|lut\s+|grade\s+|tone\s+)?"
                  r"(?:off|out)\b"
                  # the particle after the object: "take the warm filter off"
                  # (it APPLIED the warm look), "turn the captions off";
                  # Final QA: or `out`, with up to four words before the noun
                  # ("take the glitch transition out", "take the transitions
                  # out" ADDED transitions and said done).
                  r"|\b(?:take|turn|switch|strip|get|lose)\s+(?:the\s+|all\s+(?:the\s+)?|my\s+|that\s+|this\s+)?"
                  r"(?:black and white\s+|b ?& ?w\s+|(?:[\w'-]+\s+){1,4})?"
                  r"(?:captions?|subtitles?|subs|filters?|luts?|looks?|colou?r grade|grades?|grading|transitions?"
                  r"|cross[- ]?fades?|dissolves?|text|titles?|lower thirds?|hooks?)\s+(?:off|out)\b")
#: A clip's LENGTH on the timeline (wave E, F4b): "trim the second clip to 2
#: seconds", "make the first clip 3 seconds long", "clip 2 should be 1.5 s".
#: Never "trim 2 seconds off the second clip" (a range) nor "make it 30s" (the
#: whole video's target length).
_LEN = r"\d+(?:\.\d+)?\s*(?:s|sec|secs|seconds?)\b"
CLIP_LENGTH = (rf"\b(?:trim|cut|shorten|extend|lengthen|stretch)\s+{CLIP_PHRASE}\s+(?:down\s+|back\s+|out\s+)?to\s+(?:be\s+)?"
               rf"(?:only\s+|just\s+|exactly\s+)?{_LEN}"
               rf"|\b(?:make|set|change|get)\s+{CLIP_PHRASE}\s+(?:to\s+(?:be\s+)?)?(?:only\s+|just\s+|exactly\s+)?{_LEN}"
               r"(?:\s+long)?(?!\s*(?:slower|faster|later|earlier|shorter|longer|in|of|off)\b)"
               rf"|\b(?:set|change|make)\s+(?:the\s+)?(?:length|duration)\s+of\s+{CLIP_PHRASE}\s+(?:to\s+)?{_LEN}"
               rf"|\bmake\s+{CLIP_PHRASE}\s+last\s+(?:only\s+|just\s+|exactly\s+)?{_LEN}"
               rf"|\b{CLIP_PHRASE}\s+(?:should|must|needs to|has to)\s+(?:be|last)\s+(?:only\s+|just\s+|exactly\s+)?{_LEN}"
               # by an amount: "trim 2 seconds off (the end of) the second clip",
               # "shorten clip 2 by 1 s", "make the last clip 2 seconds longer"
               # ("…off the end of the second clip" cut the VIDEO's last 2 s)
               rf"|\b(?:trim|cut|shave|take|chop|lose|remove)\s+(?:off\s+)?{_LEN}\s+(?:off|from)\s+"
               rf"(?:(?:the\s+)?(?:end|start|beginning|front|head|tail)\s+of\s+)?{CLIP_PHRASE}"
               rf"|\b(?:shorten|trim|cut|extend|lengthen|stretch)\s+{CLIP_PHRASE}\s+by\s+{_LEN}"
               rf"|\bmake\s+{CLIP_PHRASE}\s+{_LEN}\s+(?:shorter|longer)\b")
#: A zoom on a clip's picture — Ken Burns, a slow push, a punch-in, a zoom
#: level. Never a clause about transitions or seams ("zoom transition at
#: 0:04", "a zoom at every cut" stay transitions), and never the hook's own
#: punch-in ("add a hook with a punch in").
_ZOOM = (r"^(?!.*\btransitions?\b)(?!.*\bhook\b)(?!.*\b(?:music|volume|audio|sound|voice|song)\b)"
         r"(?!.*\b(?:between|at|on)\s+(?:the\s+|every\s+|each\s+|all\s+(?:the\s+)?)?(?:cuts?|seams?|scene changes?|clip changes?)\b)"
         r".*?(?:\b(?:zoom|push)(?:s|ed|ing)?[- ]?(?:in|out|into)\b|\bpunch(?:es|ed|ing)?[- ]?in(?:to)?\b"
         r"|\bken[- ]?burns\b|\bpan (?:and|&) zoom\b|\bslow(?:ly)? zoom|\bzoom (?:effect|animation)\b"
         r"|\b(?:zoom|scale|enlarge|magnify|shrink)\b[^%]*?\b\d{2,3}(?:\.\d+)?\s*(?:%|percent\b)"
         # final sweep 2 r2: "zoom clip 1 to 2x" set the clip's SPEED to 2x
         r"|^(?!.*\b(?:speed|fast\w*|slow\w*|pace|playback)\b).*\b(?:zoom|scale|enlarge|magnify)\b.*?(?<![\w.])\d+(?:\.\d+)?\s*x\b"
         r"|\bzoom (?:it|this|that|the [\w ]{0,20}?clip)\b)")

#: The overlay nouns a retext / a removal by name may name.
_TEXT_NOUN = r"(?:text|title|heading|headline|lower[- ]?third|name\s+(?:card|tag|plate)|label|super)"
#: What an existing text should SAY (Final QA): "change the Summer Trip text
#: to Winter Trip", "rename the title to Winter Trip", "edit the text so it
#: says Hello World", "change the title to say Winter Trip", "fix the typo in
#: the title, it should say Summer Trips", "replace 'Summer Trip' with
#: 'Winter Trip'". Never a look change ("change the title font / colour /
#: size") and never "replace the title with a lower third".
_RETEXT_MARKER = (r"(?:to\s+say|to\s+read|so\s+(?:that\s+)?it\s+(?:says|reads)|(?:it\s+|that\s+)?should\s+(?:say|read)"
                  r"|(?:and\s+)?make\s+it\s+(?:say|read)|with|into|to)")
RETEXT = (r"^(?!.*\b(?:font|colou?r|size|bigger|smaller|larger|bold|italic|style|position|animation|opacity"
          r"|transparent|move|placement)\b)"
          r".*?\b(?:change|rename|edit|replace|fix|update|correct|retype|reword|swap|alter)\s+"
          r"(?:[\w'’-]+\s+){0,6}?" + _TEXT_NOUN + r"s?\b"
          r"[^\n]*?\b" + _RETEXT_MARKER + r"\s+"
          r"(?!(?:a|an|some|the|my)\s+(?:new\s+)?(?:lower|title|text|caption|sticker|hook|animation|effect"
          r"|transition|font|colou?r|different\s+(?:font|colou?r|style))\b)\S"
          r"|\breplace\s+[\"'“‘][^\"'“”‘’]{1,60}[\"'”’]\s+with\s+[\"'“‘]"
          r"|\b(?:change|rename|edit|fix|update|correct)\s+[\"'“‘][^\"'“”‘’]{1,60}[\"'”’]\s+(?:to|into)\s+\S")

#: An overlay's opacity or its place on the canvas (Final QA) — never the
#: captions (their position is a caption look) and never a CLIP's order on
#: the timeline ("move the second clip to the end" is move_clip).
_OVERLAY_NOUN = (r"(?:overlays?|pips?|picture[- ]in[- ]pictures?|stickers?|emojis?|logos?|watermarks?|text|titles?"
                 r"|lower[- ]?thirds?|top\s+(?:clip|layer|video)|images?|photos?|pictures?)")
TRANSFORM_REQUEST = (
    rf"^(?!.*\b(?:captions?|subtitles?|subs)\b)(?=.*\b{_OVERLAY_NOUN}\b)"
    r".*(?:\b\d{1,3}\s*(?:%|percent)\s+(?:transparen(?:t|cy)|opacity|opaque|see[- ]through)"
    r"|\b(?:transparen(?:t|cy)|opacity|see[- ]through|translucent|semi[- ]?transparent)\b)"
    rf"|\b(?:move|put|place|position|drag|shift|slide|push)\s+(?:the\s+|my\s+|that\s+|this\s+)?{_OVERLAY_NOUN}\s+"
    r"(?:(?:to|into|in|at|on|towards?)\s+)(?:the\s+)?(?:(?:top|bottom|upper|lower)(?:[- ](?:left|right))?|left|right"
    r"|(?:top|bottom|upper|lower)?\s*(?:left|right)?\s*corner|cent(?:er|re)|middle)\b"
    # final sweep 4: a picture-in-picture ("put the kitchen after shot over
    # the kitchen before shot as a picture in picture") — an honest reply
    # with where to do it, never the Trim / Speed / Title menu
    r"|\b(?:as\s+an?\s+|(?:add|make|create|build|do)\s+(?:an?\s+|the\s+)?)(?:pip|picture[- ]in[- ]picture)\b(?!\s*(?:lane|track|row))"
    rf"|\b(?:put|place|add|overlay|layer|stack|show)\s+(?:the\s+)?(?:[\w'-]+\s+){{0,4}}?{CLIP_NOUN}(?:\s+\d{{1,2}})?\s+"
    rf"(?:over|on\s+top\s+of|above|onto)\s+(?:the\s+)?(?:[\w'-]+\s+){{0,4}}?{CLIP_NOUN}(?:\s+\d{{1,2}})?\b"
)

#: (regex, intent, score) checked in order BEFORE the phrase table (§2.3).
CUT_PRECEDENCE: tuple[tuple[str, str, float], ...] = (
    # QA-018: a fade to/from black anchored at the END / START of the video is
    # the programme fade (last / first clip), not a seam transition — as a
    # transition it landed on the last SEAM (mid-video) or, on a one-clip
    # timeline, did nothing ("there is no seam").
    (r"\bfade (?:out |down )?(?:to|into) black (?:at|by|towards?|for) (?:the )?(?:very )?(?:end|ending|finish|close|outro)\b"
     r"|\bfade (?:in |up )?from black (?:at|in|for) (?:the )?(?:very )?(?:start|beginning|opening|intro)\b"
     r"|\b(?:end|finish|close) (?:it |the video |everything )?(?:with|on) a fade(?: out| to black)?\b", "fade", EXACT),
    (r"\bcut(?:s|ting)?\s+(?:it\s+|this\s+|the\s+video\s+)?(?:to|on|with|along)\s+the\s+(?:beat|music|rhythm|drums?|bpm)\b|\bcut to the beat\b|\bon the beat\b|\bbeat[- ]sync\b|\bsync(?:ed)?\s+to\s+the\s+(?:beat|music)\b|\bbeat[- ]match\b", "beat_sync", EXACT),
    # Final QA: a text's new wording before the voiceover "say" row and the
    # title row that ADDED a second title.
    (RETEXT, "retext", EXACT),
    # Wave E (F3): taking a VOICE effect off ("remove the voice effect", "turn
    # off the echo") before the generic "remove the ___ effect" row.
    (_VV.VOICE_OFF_PHRASE, "voice_effect", EXACT),
    # Wave E (F1): a clip ANIMATION ("add a zoom in animation", "make the
    # sticker bounce in", "slide the last clip out", "remove the animation")
    # before the remove-feature and Ken Burns zoom rows.
    (_AV.ANIM_PHRASE, "animation", EXACT),
    (TRANSFORM_REQUEST, "transform", EXACT),
    # Final sweep 4: a 3 · 2 · 1 countdown is a title (three cards), before
    # the sticker row ("countdown sticker") and the generic menu.
    (r"\bcount\s?down\b", "title", EXACT),
    (REMOVE_FEATURE, "remove_feature", EXACT),
    # Final QA: taking the VOICE-OVER itself away ("remove the voiceover",
    # "delete the narration", "take the voiceover out") offered a NEW spoken
    # voice-over. Never "remove the voiceover's echo" (a voice effect) or a
    # level/noise clause about it.
    (r"\b(?:remove|delete|get rid of|take out|take off|drop|lose|kill|clear|scrap|ditch|trash)\s+"
     r"(?:the\s+|my\s+|that\s+|this\s+|all\s+(?:of\s+)?(?:the\s+|my\s+)?|every\s+)?"
     r"(?:voice[- ]?overs?|narrations?|vo|voice\s+track|voice[- ]?over\s+(?:track|clips?|recordings?))\b"
     r"(?!['’]s)(?!\s+(?:effects?|filters?|changer|volume|level|gain|noise|echo|reverb|sound)\b)"
     r"|\b(?:take|get)\s+(?:the\s+|my\s+|that\s+)?(?:voice[- ]?overs?|narration|vo)\s+out\b", "remove_feature", EXACT),
    (CLIP_LENGTH, "clip_length", EXACT),
    (_ZOOM, "zoom", EXACT),
    # "export in 4k" is the 4K export preset, not an AI upscale of every clip.
    (r"\bexport\b(?!.*\bupscal)(?=.*\b(?:4k|uhd|2160p)\b)", "export_preset", EXACT),
    # Final sweep 4: "delete everything between the first and third clips" /
    # "cut out the part between clip 1 and clip 3" — the clips between two
    # named ones (the transitions row read the seam words first).
    (rf"^(?!.*\b(?:transitions?|dissolves?|cross ?fades?|wipes?|fades?|gaps?|space)\b)"
     r"\b(?:delete|remove|cut(?:\s+out)?|get rid of|lose|drop|trash|erase|ditch|scrap|kill|take out)\b.*"
     rf"\bbetween\s+(?:the\s+)?(?:{ORDINAL}|{CLIP_NOUN}\s+\d{{1,2}})(?:\s+{CLIP_NOUN})?\s+(?:and|&)\s+"
     rf"(?:the\s+)?(?:{ORDINAL}|{CLIP_NOUN}\s+\d{{1,2}})", "delete_clip", EXACT),
    # A CLIP taken out wins over the range trim below ("remove the first clip"
    # has `first` in it, which `_HAS_RANGE` reads as a range with no length).
    (_DELETE_CLIP, "delete_clip", EXACT),
    # CapCut's "cut" at a moment is its Split: "cut it at 6s", "cut here";
    # "chop / slice / snip it at 00:06" too (review RD3: those asked which
    # part to cut).
    # Final QA r2: with a clip named first too — "cut clip 2 at 7 seconds"
    # planned a range cut and asked "Which part should I cut?" forever.
    (rf"\b(?:cut|chop|slice|snip)\s+(?:it\s+|this\s+|that\s+|the\s+(?:\w+\s+)?(?:clip|video|footage|shot)\s+"
     rf"|{CLIP_PHRASE}\s+)?"
     r"(?:(?:right\s+)?(?:at|@)\s+(?:\d|the playhead\b|the cursor\b|this point\b|the (?:[\w'-]+ ){0,2}marker\b)|here\b|in (?:two|half) at\b)", "split", EXACT),
    (r"\bcut\s+(?:out\s+|away\s+)?(?:the\s+|all\s+(?:the\s+)?|every\s+)?(?:ums?|uhs?|umms?|filler(?:s| words?)|hesitations?|stutters?)\b", "remove_fillers", EXACT),
    (r"\bcut\s+(?:out\s+|away\s+)?(?:the\s+|all\s+(?:the\s+)?|every\s+)?(?:silences?|pauses?|dead air|gaps?|quiet parts?)\b", "remove_silences", EXACT),
    (r"\bcut\s+(?:it\s+|this\s+|the\s+video\s+)?(?:up\s+)?into\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|a few|several|some)\s*(?:shorts?|clips?|parts?|pieces?|highlights?|reels?|segments?|videos?)\b|\bcut\s+(?:it\s+)?into\s+shorts\b", "shorts", EXACT),
    (rf"\bcut\s+(?:off\s+|out\s+|away\s+)?(?:the\s+)?(?=.*{_HAS_RANGE})", "trim", EXACT),
    (r"\bcut\s+(?:it\s+|this\s+|the\s+video\s+)?(?:down|shorter|tighter)\b", "tighten", SYNONYM),
)

#: The music bed, as the object of a level / fade / mute / fit request.
_MUSIC_NOUN = r"(?:music|song|track|bed|bgm|soundtrack|tune|score|music bed|background music|backing track)"
#: The programme's own sound (v1 clip audio), as the object of a level / mute.
_VOICE_NOUN = (r"(?:voice|vocals?|speech|dialogue|narration|original audio|original sound|clip audio|video audio|video sound"
               r"|original video (?:audio|sound)|original clip (?:audio|sound))")
#: The voice-over lane, as the object of a level request (review RE). Final
#: sweep 4: a bare "narration" too — on a project with a voice-over lane it
#: names that lane (the expander falls back to the clips' own sound when
#: there is none).
_VO_NOUN = r"(?:voice[- ]?overs?|vo|narrations?(?:\s+track)?|voice track)"
#: An amount inside a level phrase ("make the voiceover 3 dB quieter").
_LVL_AMT = r"(?:[-+]?\d+(?:\.\d+)?\s*(?:d\s?b|decibels?|%|percent)\s+)?"
#: What "fade the ___ in/out" may name besides the music.
_FADE_OBJECT = (r"video|clip|clips|first clip|last clip|opening clip|final clip|picture|image|footage|start|end|"
                r"beginning|ending|intro|outro|audio|sound|voice|whole thing|whole video")

#: intent → list of (regex, score). First match per pattern; best score wins.
PHRASES: dict[str, tuple[tuple[str, float], ...]] = {
    "undo": ((r"^(?:undo|undo that|undo it|undo the last(?: one| edit| change)?|go back|revert(?: that| it)?|take that back|ctrl ?z|cmd ?z|wapas karo|wapas)$"
              # Final QA r3: a count ("undo the last 3 edits", "undo twice")
              r"|^(?:undo|revert)\s+(?:the\s+)?(?:last\s+)?(?:\d{1,2}|two|three|four|five|six|seven|eight|nine|ten)\s+"
              r"(?:edits?|changes?|steps?|things?|actions?)$|^undo\s+(?:that\s+)?(?:twice|(?:\d{1,2}|two|three|four|five)\s+times)$", EXACT),
             (r"\bundo\b|\brevert\b", SYNONYM)),
    "redo": ((r"^(?:redo|redo that|redo it|redo the last(?: one| edit)?)$", EXACT), (r"\bredo\b", SYNONYM)),
    "auto_edit": ((r"\b(?:auto[- ]?edit|full edit|complete (?:the )?(?:video|edit)|finish (?:the |this )?(?:video|edit)|do (?:the|a) (?:whole|full|complete) (?:edit|thing)|edit (?:the|this) (?:whole )?video|make (?:it|this) (?:ready|good|great|perfect|nice)|make it pop|polish (?:it|this|the video)|do everything|clean this up for|get (?:it|this) ready for|prepare (?:it|this) for|optimi[sz]e (?:it|this) for|turn this into a (?:reel|tiktok|short|youtube video)|make (?:a|this a|it a) (?:reel|tiktok|short))\b", EXACT),
                  # Final QA: never when the clause names a frame shape — an
                  # explicit ratio is a reframe, not an automatic edit.
                  (r"^(?!.*(?:\b(?:9:16|16:9|1:1|4:5|4:3|21:9|vertical|square|portrait|landscape|widescreen|horizontal"
                   r"|aspect|ratio)\b|\b\d{3,4}\s*x\s*\d{3,4}\b))"
                   r".*\b(?:for|ready for|good for|great for|fit for)\s+(?:instagram|reels|tiktok|youtube|shorts|linkedin|a reel|a short)\b", SYNONYM),
                  (r"\bedit (?:it|this)\b|\bfix (?:it|this) up\b|\bmake it better\b|\bimprove (?:it|this)\b", WEAK)),
    "translate_captions": ((r"\btranslate\b|\btranslation\b|\banuvad\b", EXACT),
                           (r"\bcaptions?\s+(?:in|into|to)\s+(?:hindi|hinglish|english|spanish|hi|en|es)\b|\b(?:hindi|hinglish|english|spanish)\s+(?:captions?|subtitles?)\b", SYNONYM)),
    "captions": ((r"\b(?:add|put|generate|create|make|lay|burn(?: in)?|do|turn on|give (?:it|me)|i want|need)\s+(?:some\s+|the\s+|auto\s+|automatic\s+|\w+\s+)?(?:captions?|subtitles?|subs|cc)\b|\b(?:auto[- ]?)?captions?\b|\bsubtitles?\b|\bcaption (?:it|this|the video)\b|\btranscribe and caption\b|\bkaraoke\b", EXACT),
                 (r"\bsubs\b|\bcc\b|\bwords on screen\b|\btext of what (?:i|he|she|they) (?:say|said)\b|\bon[- ]screen text of the speech\b", SYNONYM),
                 (r"\btranscri(?:be|ption)\b", WEAK)),
    "remove_fillers": ((r"\b(?:remove|delete|strip|drop|kill|clean(?: up)?|get rid of|take out|edit out|nix|cut)\s+(?:the\s+|all\s+(?:the\s+)?|every\s+|my\s+)?(?:ums?|uhs?|umms?|uhhs?|erms?|hmms?|filler(?:s| words?)|stutters?|hesitations?|verbal (?:tics|fillers))\b|\bfiller words?\b|\bno more ums\b|\bums and uhs\b|\bthe ums\b", EXACT),
                       (r"\bums?\b|\buhs?\b|\bfillers?\b", SYNONYM)),
    "remove_silences": ((r"\b(?:remove|delete|strip|drop|kill|trim|take out|edit out|cut)\s+(?:the\s+|all\s+(?:the\s+)?|every\s+|any\s+|long\s+|awkward\s+)?(?:silences?|silent (?:parts?|bits?|gaps?)|pauses?|dead air|gaps?|quiet parts?)\b|\bsilence removal\b|\bjump ?cut (?:it|this|the pauses)\b|\bauto[- ]?cut the (?:silence|pauses)\b", EXACT),
                        (r"\bsilences?\b|\bpauses?\b|\bdead air\b|\bawkward gaps?\b", SYNONYM)),
    "tighten": ((r"\btighten(?: it| this| the video| up| it up| this up)?\b|\bmake (?:it|this) (?:tighter|snappier|punchier|faster paced)\b|\bremove (?:the )?(?:silences?|pauses?) and (?:the )?(?:fillers?|filler words|ums)\b|\b(?:silences?|pauses?) and (?:fillers?|filler words|ums)\b|\bjump ?cut (?:it|this|the video)\b|\bcut the (?:fat|fluff|dead weight)\b|\btrim the fat\b|\bshorten (?:it|this|the video)\b(?!\s+(?:down\s+)?by\s+(?:\d|a\b|an\b|one\b|two\b|three\b|half\b))|\btrim (?:it|this) down\b(?!\s+by\s+\d)|\bsmart cut\b", EXACT),
                (r"\bsnappier\b|\bpacier\b|\bfaster paced\b|\bless rambling\b|\bconcise\b", SYNONYM)),
    "shorts": ((r"\b(?:make|create|generate|give me|produce|extract|pull|find|get)\s+(?:me\s+)?(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|a few|several|some|a couple of)?\s*(?:short|vertical|quick|viral|best)?\s*(?:shorts?|clips?|highlights?|reels?|snippets?|teasers?|moments)\b(?! (?:transitions?|captions?))(?!\s+(?:#\s*|number\s+)?(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\b)|\bhighlights? reel\b|\bbest (?:bits|moments|parts)\b|\bsplit (?:it|this) into (?:\d+|shorts|clips)\b|\bchop (?:it|this) (?:up )?into\b|\bmake shorts\b|\bshorts out of (?:this|it)\b", EXACT),
               (r"\bhighlights?\b|\bclip (?:it|this) up\b|\bviral moments?\b", SYNONYM)),
    "reframe": ((r"\b(?:make|turn|flip|convert|reframe|crop|resize|change|set|switch|put)\s+(?:it|this|the video|the canvas|the aspect(?: ratio)?"
                 # Final QA: "set it to 4:5 for instagram", "change the ratio to
                 # 9:16 for youtube shorts" ran a whole automatic edit
                 r"|the (?:(?:aspect )?ratio|format|size|shape|frame|canvas size|video size|dimensions))?\s*(?:to|into)?\s*(?:vertical|portrait|landscape|horizontal|square|9:16|16:9|1:1|4:5|1080x1920|1920x1080|widescreen)\b|\b(?:auto[- ]?)?reframe\b|\bvertical version\b|\bportrait mode\b|\baspect ratio\b|\bcrop (?:it|this) (?:to|for)\b|\bfit (?:it|this) (?:to|for) (?:reels|tiktok|shorts|instagram|youtube|story|stories)\b|\bresize (?:it|this|the video) for\b|\bsubject[- ]track(?:ed|ing)? crop\b"
                 # QA-018 live pass: "make it fit a phone screen", "turn this into a phone video"
                 r"|\b(?:fit|for|into|onto)\s+(?:a\s+|the\s+)?(?:phone|mobile)(?:\s+(?:screen|video|format))?\b"
                 # final sweep 4: "back to widescreen" / "back to portrait"
                 r"|\bback\s+to\s+(?:widescreen|landscape|horizontal|portrait|vertical|square|16:9|9:16|1:1|4:5)\b", EXACT),
                (r"\bvertical\b|\bportrait\b|\blandscape\b|\bsquare\b|\b9:16\b|\b16:9\b|\b1:1\b|\b4:5\b", SYNONYM)),
    "duck": ((r"\bduck(?:ing)?\b|\blower the music (?:under|behind|when|during)\b|\bmusic (?:under|behind|below) (?:my|the) (?:voice|speech|talking|dialogue)\b|\bquiet(?:er)? (?:the )?music (?:when|while|under)\b|\bmusic (?:quieter|softer|lower|down) (?:when|while|under|during)\b|\bturn (?:the )?music down (?:when|while|under)\b|\bsidechain\b|\bauto[- ]?duck\b"
             # final sweep 4: said in the third person — "lower the music
             # while the coach is talking", "drop the music when she speaks",
             # "keep the music under his voice" (a static -6 dB before)
             rf"|\b(?:lower|drop|dip|reduce|quiet(?:en)?|turn\s+down|bring\s+down|pull\s+down|soften|duck)\s+(?:the\s+)?(?:background\s+)?{_MUSIC_NOUN}\s+"
             r"(?:(?:down|a\s+bit|a\s+little)\s+)?(?:while|when|whenever|as|during|if)\s+(?:i|he|she|they|we|someone|anyone|somebody|people|the\s+\w+|my\s+\w+|our\s+\w+)\s+"
             r"(?:is\s+|are\s+|am\s+|'s\s+|'re\s+|start\w*\s+)?(?:talk|speak|say|narrat)\w*"
             rf"|\b(?:keep|put|sit|have|hold)\s+(?:the\s+)?(?:background\s+)?{_MUSIC_NOUN}\s+(?:down\s+|low\s+|lower\s+)?(?:under|underneath|beneath|below|behind)\s+"
             r"(?:the|his|her|their|my|our|your)\s+(?:\w+'s\s+)?(?:voice|speech|talking|dialogue|narration|words)\b"
             rf"|\b{_MUSIC_NOUN}\s+(?:under|underneath|beneath|below|behind)\s+(?:his|her|their|our|your)\s+(?:\w+'s\s+)?(?:voice|speech|talking|dialogue|narration)\b"
             # QA-018 paraphrases ("have the song dip under speech", "stop the
             # music from lowering when I talk", "keep the soundtrack steady")
             r"|\b(?:music|song|track|soundtrack|tune|bed)\s+(?:dip|dips|duck|ducks|drop|drops|lower|lowers|go(?:es)? down)\s+(?:under|when|while|during|whenever|for)\b"
             r"|\bstop(?:ped)?\s+(?:the\s+)?(?:background\s+)?(?:music|song|track|soundtrack|tune|bed)\s+from\s+(?:lowering|dipping|ducking|dropping|going down|getting quieter)\b"
             r"|\bkeep\s+(?:the\s+)?(?:background\s+)?(?:music|song|track|soundtrack|tune|bed)\s+(?:steady|level|constant)\b"
             r"|\b(?:music|song|track|soundtrack|tune|bed)\s+stop\s+(?:dipping|lowering|ducking|dropping)\b"
             r"|\b(?:music|song|track|soundtrack|tune|bed)\s+(?:should(?:n'?t| not)|must(?:n'?t| not)|shall not|can'?t|cannot|may not)\s+(?:dip|duck|drop|lower|get quieter)\b", EXACT),
             (r"\bmusic (?:too )?loud\b|\bmusic (?:is )?drowning\b|\bbalance (?:the )?music\b|\bmusic under\b", SYNONYM)),
    # --- QA-018 -------------------------------------------------------
    # Music nouns a level/fade/mute/fit row names (the object, never the verb).
    "fit_music": ((rf"\b(?:trim|cut|fit|match|shorten|end|stop|sync|clip|crop|chop|cut off)\s+(?:the\s+)?(?:background\s+)?{_MUSIC_NOUN}\s+(?:to|with|at)\s+(?:the\s+)?(?:(?:same\s+)?length of the video|video(?:'s)?(?: length| duration)?|length(?: of the video)?|end(?: of the video)?|footage|clip|same length|duration)\b"
                   rf"|\bmake (?:the\s+)?{_MUSIC_NOUN} (?:end|stop|finish) (?:with|when|at) the (?:video|end|footage)\b"
                   rf"|\b{_MUSIC_NOUN} (?:is |runs? )?(?:too long|longer than the video|past the (?:video|end)|over the end|after the video ends)\b"
                   rf"|\b{_MUSIC_NOUN} (?:to|matches|should match) (?:the )?video(?:'s)? length\b"
                   rf"|\b{_MUSIC_NOUN} (?:keeps? (?:on )?(?:playing|going)|continues|carries on|goes on|plays on|runs on|still plays)\b"
                   rf"|\b(?:clip|cut|stop|end|kill)\s+(?:the\s+)?(?:background\s+)?{_MUSIC_NOUN}\s+(?:when|where|as)\s+(?:the\s+)?(?:picture|video|footage|image)\s+(?:stops|ends|finishes)\b"
                   r"(?:\s+(?:after|past|beyond|over|when|once))?"
                   rf"|\b(?:end|stop|finish) (?:the\s+)?(?:background\s+)?{_MUSIC_NOUN} (?:when|where|as|with|at) (?:the\s+)?video\b"
                   rf"|\b{_MUSIC_NOUN} (?:should |must |needs to |has to )?(?:end|stop|finish) (?:with|when|at|where) the (?:video|footage)\b"
                   r"|\bblack (?:tail|screen|frames?) at the end\b"
                   # "the music outlives the video" (QA-018 live pass, wave C)
                   rf"|\b{_MUSIC_NOUN}\s+(?:outlives|outlasts|overruns|runs longer than|lasts longer than|is longer than"
                   r"|goes on after|keeps going after|plays past|runs past)\s+(?:the\s+)?(?:video|footage|picture|clip|clips)\b"
                   # "trim the song so it matches the clip" (QA-018 paraphrase)
                   rf"|\b(?:trim|cut|fit|shorten)\s+(?:the\s+)?(?:background\s+)?{_MUSIC_NOUN}\s+so\s+(?:that\s+)?(?:it\s+)?"
                   r"(?:matches|fits|ends with|lines up with|stops with)\s+(?:the\s+)?(?:clip|video|footage)\b", EXACT),),
    # "remove the music" / "delete the song" / "music hatao" — the bed goes.
    "remove_music": ((rf"\b(?:remove|delete|get rid of|take out|take off|clear|lose|ditch|scrap)\s+(?:the\s+|all\s+(?:the\s+)?|my\s+)?(?:background\s+)?{_MUSIC_NOUN}\b(?!\s+(?:from|in|at|for|between|during|under)\b)"
                      rf"|\b{_MUSIC_NOUN}\s+(?:hatao|hata do|nikalo|nikal do|remove|delete)\b"
                      rf"|\b(?:can|could|should|let)\s+(?:the\s+)?(?:background\s+)?{_MUSIC_NOUN}\s+(?:go away|disappear|be gone)\b"
                      rf"|\bno (?:more\s+)?(?:background\s+)?{_MUSIC_NOUN}(?:\s+at all)?$", EXACT),),
    "fade": ((rf"\bfade(?:s|d)?\s+(?:it\s+|this\s+|everything\s+|the\s+(?:{_FADE_OBJECT})\s+|{_MUSIC_NOUN}\s+)?(?:in|out|up|down|away)\b"
              # "have the soundtrack bow out at the end", "let the tune drift away"
              rf"|\b{_MUSIC_NOUN}\s+(?:bow|drift|trail|taper|die|ease|fade|slip)\s+(?:out|away|off)\b"
              r"|\bfade[- ]?(?:ins?|outs?)\b|\bfade (?:up |in )?from black\b"
              rf"|\bfade (?:the\s+)?(?:{_FADE_OBJECT}|{_MUSIC_NOUN})\b", EXACT),
             (r"\bfades?\b|\bfading\b", SYNONYM)),
    "mute": ((rf"\b(?:un)?mute(?:d)?\s+(?:the\s+)?(?:background\s+)?(?:{_MUSIC_NOUN}|{_VOICE_NOUN}|audio|sound|clip|video|it|everything)\b"
              rf"|\b(?:kill|cut)\s+(?:the\s+)?(?:audio|sound)\s+(?:on|of|from)\s+(?:the\s+)?(?:background\s+)?{_MUSIC_NOUN}\b"
              rf"|\bmake\s+(?:the\s+)?(?:background\s+)?{_MUSIC_NOUN}\s+(?:silent|mute|muted|inaudible)\b"
              rf"|\bhush\s+(?:the\s+)?(?:background\s+)?{_MUSIC_NOUN}\b"
              rf"|\b(?:silence|turn off|switch off|kill)\s+(?:the\s+)?(?:background\s+)?{_MUSIC_NOUN}\b"
              rf"|\b(?:turn|switch)\s+(?:the\s+)?(?:background\s+)?{_MUSIC_NOUN}\s+off\b"
              rf"|\b(?:turn|switch|put)\s+(?:the\s+)?(?:background\s+)?{_MUSIC_NOUN}\s+(?:back\s+)?on\b"
              rf"|\b{_MUSIC_NOUN}\s+(?:band|bandh|off|chalu|on)\s*(?:karo|kar do|kardo|kijiye|karein|do)\b"
              # QA-018 live pass: "kill the audio from the camera" (it read as
              # noise reduction) — the programme's own sound, off.
              r"|\b(?:kill|cut|drop|lose|silence|switch off|turn off)\s+(?:the\s+|all\s+(?:the\s+)?)?(?:audio|sound)\s+"
              r"(?:from|of|on|in)\s+(?:the\s+|my\s+)?(?:camera|clips?|video|footage|recording|phone)\b"
              rf"|\b(?:take|pull|strip|remove|kill|cut)\s+(?:the\s+)?(?:sound|audio)\s+(?:out\s+)?(?:of|from|on)\s+{CLIP_PHRASE}"
              # final sweep 3: "silence the original audio but keep the
              # music" cut the pauses (remove_silences) instead of muting
              r"|\bsilence\s+(?:the\s+|my\s+|all\s+(?:the\s+)?)?(?:original|clip|clips'?|camera|video|footage)\s+"
              r"(?:audio|sound)\b"
              # final sweep 4: the VERB "silence" on a clip ("silence the first
              # two clips") ran remove_silences over the whole track — footage
              # deleted from every clip, nothing muted
              rf"|\bsilence\s+(?:(?!(?:silences?|pauses?|gaps?|dead)\b)[\w'-]+\s+){{0,4}}?"
              rf"(?:{CLIP_NOUN}|it|them|this|that|everything|ones?)\b"
              rf"|\b(?:un)?mute\b", EXACT),),
    "volume": ((rf"\b(?:turn|bring|put|set|make|drop|lower|raise|reduce|increase|boost|pull|dial|knock|lift|push|decrease)\s+(?:the\s+|my\s+)?(?:background\s+)?(?:{_MUSIC_NOUN}|{_VOICE_NOUN})(?:'s)?\s*(?:volume|level|gain)?\s*(?:down|up|lower|louder|quieter|softer|higher|to|by|at)\b"
                rf"|\b(?:lower|raise|reduce|increase|boost|decrease|drop)\s+(?:the\s+|my\s+)?(?:background\s+)?(?:{_MUSIC_NOUN}|{_VOICE_NOUN})(?:'s)?(?:\s+(?:volume|level|gain))?\b"
                rf"|\b(?:turn|bring|crank|pump|dial|knock|push|pull)\s+(?:up|down)\s+(?:the\s+|my\s+)?(?:background\s+)?(?:{_MUSIC_NOUN}|{_VOICE_NOUN})\b"
                rf"|\b(?:{_MUSIC_NOUN}|{_VOICE_NOUN})(?:'s)?\s+(?:volume|level|gain)\b"
                rf"|\b(?:{_MUSIC_NOUN})(?:'s)?\s+(?:volume\s+|level\s+|gain\s+)?(?:at|to|=)?\s*[-+]?\d+(?:\.\d+)?\s*(?:d\s?b|%)"
                # final sweep 2 r2: "put the music under the voiceover at -20 dB"
                # was the add-music recipe ("music is already on the timeline")
                rf"|\b(?:put|set|keep|sit|place|have|bring)\s+(?:the\s+|my\s+)?(?:background\s+)?{_MUSIC_NOUN}\s+"
                r"(?:[\w'-]+\s+){0,5}?(?:at|to)\s+[-+]?\d+(?:\.\d+)?\s*(?:d\s?b|decibels?|%)"
                rf"|\b(?:{_MUSIC_NOUN})\s+(?:ka\s+volume\s+|ki\s+awaa?z\s+)?(?:thoda\s+|thodi\s+|aur\s+)?(?:kam|dheere|dheema|dheemi|halka|halki|zyada|jyada|tez|badha\w*)\b"
                rf"|\b(?:volume|level|gain) (?:of|on|for) (?:the\s+)?(?:background\s+)?(?:{_MUSIC_NOUN}|{_VOICE_NOUN})\b"
                rf"|\bmake (?:the\s+|my\s+)?(?:background\s+)?(?:{_MUSIC_NOUN}|{_VOICE_NOUN}) {_LVL_AMT}(?:quieter|softer|louder|lower)\b"
                # QA-018 paraphrases: "a touch softer", "less loud", "quiet the music a little"
                rf"|\bmake (?:the\s+|my\s+)?(?:background\s+)?(?:{_MUSIC_NOUN}|{_VOICE_NOUN}) (?:a\s+(?:touch|bit|little|tad|notch)\s+|a\s+lot\s+|much\s+|way\s+)?(?:quieter|softer|louder|lower|less loud|more quiet)\b"
                rf"|\bquiet(?:en)?\s+(?:down\s+)?(?:the\s+|my\s+)?(?:background\s+)?{_MUSIC_NOUN}\b(?!\s+(?:down\s+)?(?:when|while|under|during))"
                rf"|\b(?:{_MUSIC_NOUN}) (?:quieter|softer|louder|lower|down|up)\b(?!\s+(?:when|while|under|during))"
                rf"|\b(?:{_MUSIC_NOUN}|{_VOICE_NOUN}) (?:is |'s )?(?:way |far |much |a bit |a little |kind of |so |really )?(?:too loud|too quiet|too soft|too low|too high)\b(?!\s+(?:when|while|under|during))"
                rf"|\bvolume (?:down|up) (?:on|for) (?:the\s+)?(?:background\s+)?{_MUSIC_NOUN}\b"
                rf"|\b(?:quieter|softer|louder|lower) (?:background\s+)?{_MUSIC_NOUN}\b"
                rf"|\bcan'?t hear (?:my|the) (?:voice|speech|dialogue|narration|words|talking) (?:over|under|because of|with|through) (?:the\s+)?{_MUSIC_NOUN}\b"
                rf"|\b{_MUSIC_NOUN} (?:drowns|is drowning|covers|buries|overpowers) (?:out )?(?:my|the) (?:voice|speech|dialogue|narration)\b"
                # QA-018 live pass (wave C): paraphrases the grammar used to hand
                # to the loudness target or to add-music — "let the song swell
                # louder", "I want the beat pushed further back in the mix".
                rf"|\b{_MUSIC_NOUN}\s+(?:\w+\s+){{1,2}}(?:louder|quieter|softer)\b(?!\s+(?:when|while|under|during))"
                rf"|\b(?:{_MUSIC_NOUN}|beat|beats)\s+(?:pushed|pulled|sat|tucked|set|moved|nudged|placed|sits?|sitting)\s+"
                r"(?:further\s+|more\s+|a (?:bit|little|touch)\s+|way\s+)?(?:back|behind|down|lower|forward|up|louder)"
                r"(?:\s+in the mix)?\b"
                rf"|\b(?:push|pull|sit|tuck|set|move|nudge|put)\s+(?:the\s+)?(?:background\s+)?(?:{_MUSIC_NOUN}|beat|beats)\s+"
                r"(?:further\s+|more\s+|a (?:bit|little|touch)\s+|way\s+)?(?:back|behind|forward)(?:\s+in the mix)?\b"
                rf"|\b(?:{_MUSIC_NOUN}|beat|beats)\b.*\bin the mix\b"
                # one clip's level (wave D3, E3): "lower the volume of the second
                # clip" used to move the whole programme's loudness target
                rf"|\b(?:lower|raise|reduce|increase|boost|drop|decrease|turn (?:up|down)|bring (?:up|down))\s+(?:the\s+)?"
                rf"(?:volume|level|gain|audio|sound)\s+(?:of|on|for|in)\s+{CLIP_PHRASE}"
                rf"|\bmake\s+{CLIP_PHRASE}\s+(?:a\s+(?:bit|little|touch|lot)\s+|much\s+)?(?:louder|quieter|softer)\b"
                rf"|\b(?:turn|bring)\s+{CLIP_PHRASE}\s+(?:up|down)\b"
                # Final QA r2: "turn down clip 2 by 6 decibels" (particle first)
                # was unread and went to Apple Intelligence, which raised the MUSIC.
                rf"|\b(?:turn|bring)\s+(?:up|down)\s+{CLIP_PHRASE}"
                # Review RE: "raise the first clip's volume by 3 db" moved the
                # PROJECT loudness target; "lower clip 2 by 4 dB" was unread.
                rf"|\b(?:lower|raise|reduce|increase|boost|drop|decrease|lift|turn\s+(?:up|down)|bring\s+(?:up|down))\s+"
                rf"{CLIP_PHRASE}(?:'s)?\s+(?:volume|level|gain|audio|sound)\b"
                rf"|\b(?:lower|raise|reduce|increase|boost|drop|decrease|lift|turn|bring)\s+{CLIP_PHRASE}"
                r"(?:\s+(?:up|down))?\s+by\s+[-+]?\d+(?:\.\d+)?\s*(?:d\s?b|decibels?|%|percent)\b"
                rf"|\b{CLIP_PHRASE}(?:'s)?\s+(?:volume|level|gain)\s+(?:up|down|by|to)\b"
                # the voice-over lane's level ("lower the voiceover by 6 dB" asked
                # to download a TTS voice)
                rf"|\b(?:turn|bring|put|set|make|drop|lower|raise|reduce|increase|boost|pull|dial|knock|lift|push|decrease)"
                rf"\s+(?:the\s+|my\s+)?{_VO_NOUN}(?:'s)?\s*(?:volume|level|gain)?\s*(?:down|up|lower|louder|quieter|softer|higher|to|by|at)\b"
                rf"|\b(?:lower|raise|reduce|increase|boost|decrease|drop)\s+(?:the\s+|my\s+)?{_VO_NOUN}\b"
                rf"|\b{_VO_NOUN}(?:'s)?\s+(?:volume|level|gain)\b"
                rf"|\bmake\s+(?:the\s+|my\s+)?{_VO_NOUN}\s+(?:a\s+(?:bit|little|touch|lot)\s+|much\s+)?{_LVL_AMT}(?:louder|quieter|softer)\b", EXACT),),
    # QA-037: "reverse the clip", "play it backwards", "ulta chala do"; the
    # forwards-again wording is read by `reverse_off`. "reverse that" / "reverse
    # the last edit" is an undo and "reverse the order" a reorder, so neither
    # reads as this row.
    "reverse": ((r"\b(?:un-?)?reverse(?:d|s)?\b(?!\s+(?:that|it all|the (?:last|previous) (?:edit|change|step)|my last|the order|order|the sequence))"
                 r"|\bplay(?:s|ing)?\s+(?:it\s+|this\s+|that\s+|(?:the|this|that)\s+(?:\w+\s+)?(?:clip|video|footage|shot)\s+)?(?:backwards?|in reverse|forwards? again|forwards?|normally again)\b"
                 r"|\brun(?:s)?\s+(?:it\s+|this\s+|the\s+(?:clip|video|footage|shot)\s+)?(?:backwards?|in reverse)\b"
                 r"|\brewind(?:ing)? effect\b|\bbackwards? (?:clip|video|playback|effect)\b"
                 r"|\bulta\s+(?:chala\w*|karo|kar do|kardo|play)\b", EXACT),),
    "audit": ((r"\b(?:audit|review|check|score|grade)\s+(?:it|this|the (?:edit|video|result|timeline)|everything)\b|\b(?:aesthetic )?audit\b|\bquality check\b", EXACT),),
    "preview": ((r"\brender (?:a |the )?preview\b|\bpreview render\b|\b(?:render|make|build|export) (?:a |the )?(?:quick |low[- ]res )?(?:preview|draft)(?: render| video| file)?\b", EXACT),),
    "beat_sync": ((r"\b(?:cut|edit|sync|snap|match|time|align)\w*\s+(?:it\s+|this\s+|the\s+(?:video|cuts|clips|footage)\s+)?(?:to|on|with|along)\s+(?:the\s+)?(?:beat|music|rhythm|drums?|bpm|tempo)\b|\bbeat[- ]?sync\b|\bon[- ]beat\b|\bbeat[- ]match(?:ed|ing)?\b|\bpulse (?:to|with|on) the (?:beat|music)\b|\bcuts? on (?:the )?beats?\b|\bbeat drops?\b", EXACT),
                  (r"\bto the (?:beat|music|rhythm)\b|\brhythm\b", SYNONYM)),
    "music": ((r"\b(?:add|put|drop|lay|throw in|give (?:it|me)|i want|need|play|with|use|set)\s+(?:some\s+|a\s+|the\s+|an?\s+\w+\s+|\w+\s+)?(?:background\s+)?(?:music|track|song|bed|beat|soundtrack|bgm|tune|score)\b|\bbackground music\b|\b(?:chill|upbeat|lo-?fi|cinematic|calm|energetic|epic|happy|dramatic|relaxing)\s+(?:background\s+)?(?:music|track|song|bed|beat|vibes?|tune)\b|\bmusic bed\b|\bbgm\b|\bsome music\b|\bmusic (?:please|pls)\b|\banother (?:track|song|music)\b|\breplace the (?:music|track|song)\b", EXACT),
              # A bare music noun is WEAK, not a synonym (QA-018 live pass):
              # at 0.85 it cleared the run bar, so "the song is overpowering
              # me" / "have the soundtrack bow out at the end" were answered by
              # the add-music recipe ("music is already on the timeline")
              # instead of reaching the Apple Intelligence band, which reads
              # them. Asking for music always hits an EXACT row above.
              (r"\bmusic\b|\bsoundtrack\b|\bsong\b|\btune\b", WEAK)),
    "hook": ((r"\b(?:add|put|write|create|give (?:it|me)|make|need|i want|generate|open with)\s+(?:a\s+|an\s+|the\s+|some\s+|a\s+\w+\s+)?(?:hook|opener|opening (?:line|text|title|hook)|cold open|scroll[- ]stopper|attention grabber|punchy (?:intro|opening|start))\b|\bhook (?:it|this|them|the viewer)\b|\b(?:a |the )?hook\b|\bstop the scroll\b|\bgrab attention\b|\bpunchy (?:intro|opening|start)\b|\bfirst (?:3|three) seconds\b", EXACT),
             (r"\bintro text\b|\bopening text\b|\bopener\b|\battention\b", SYNONYM)),
    "color_look": ((r"\b(?:give|make|apply|add|put|use|grade|colou?r[- ]grade|slap on|throw on|set)\s+(?:it|this|the (?:(?:whole|entire|full) )?(?:video|footage|thing)|the clips?)?\s*(?:a\s+|an\s+|the\s+|some\s+)?(?:\w+[- ])?(?:look|grade|lut|filter|tone|vibe|feel|colou?r(?:s| grade| grading| correction| look)?|preset|teal[- ]orange|black and white|b ?and ?w)\b|\b(?:cinematic|warm(?:er)?|cool(?:er)?|cold|punchy|vivid|faded|vintage|retro|film|moody|teal(?: and orange| orange)?|black and white|monochrome|b ?and ?w|greyscale|grayscale)\s+(?:look|grade|lut|filter|tone|vibe|feel|colou?rs?|preset|footage)\b|\bmake (?:it|this|everything|the (?:(?:whole|entire|full) )?(?:video|footage|thing|clips?|colou?rs?)) (?:more )?(?:cinematic|warm(?:er)?|cool(?:er)?|cold(?:er)?|punchy|punchier|vivid|faded|vintage|retro|moody|black and white|monochrome|b ?and ?w|pop)\b|\bapply (?:a |the )?lut\b|\bcolou?r[- ]?grad(?:e|ing)\b|\blut\b"
                    # Final QA r2: a look on a NAMED clip ("make the second clip
                    # black and white" was unread; Apple Intelligence then graded it)
                    rf"|\b(?:make|turn)\s+{CLIP_PHRASE}\s+(?:more\s+)?(?:cinematic|warm(?:er)?|cool(?:er)?|cold(?:er)?"
                    r"|punchy|punchier|vivid|faded|vintage|retro|moody|black and white|monochrome|b ?and ?w"
                    r"|gr[ae]yscale)\b"
                    # final sweep 2 r2: a look on a TIME RANGE ("make the first 4
                    # seconds black and white" put it on every clip)
                    r"|\b(?:first|last|opening|final|closing)\s+(?:\d+(?:\.\d+)?|one|two|three|four|five|six|ten)\s*"
                    r"(?:s|sec|secs|seconds?)\b.*\b(?:black and white|b ?and ?w|mono(?:chrome)?|gr[ae]yscale|cinematic"
                    r"|warm(?:er)?|cool(?:er)?|vintage|retro|moody)\b"
                    r"|\b(?:first|second|last|other|latter)\s+half\b.*\b(?:black and white|b ?and ?w|mono(?:chrome)?"
                    r"|gr[ae]yscale|cinematic|warm(?:er)?|cool(?:er)?|vintage|retro|moody)\b", EXACT),
                   (r"\bcinematic\b|\bwarm\b|\bvintage\b|\bcolou?rs?\b|\bfilter\b|\bmoody\b", SYNONYM)),
    "clean_audio": ((r"\b(?:clean(?: up)?|fix|improve|enhance|de-?noise|denoise|reduce (?:the )?(?:background )?noise (?:in|on|of))\s+(?:up\s+)?(?:the\s+|my\s+|this\s+)?(?:audio|sound|voice|speech|mic|recording|hiss|hum|background noise|noise)\b|\bnoise (?:reduction|removal|cancel\w*)\b|\bremove (?:the )?(?:background )?(?:noise|hiss|hum|buzz|static)\b|\bdenois\w+\b|\baudio (?:clean ?up|enhance\w*|repair)\b|\bmake (?:the )?(?:audio|sound|voice) (?:clearer|cleaner|better|crisper)\b|\bbackground noise\b", EXACT),
                    (r"\bnoisy\b|\bhiss\b|\bhum\b|\bmuffled\b|\baudio\b", SYNONYM)),
    "loudness": ((r"\bnormali[sz]e\b|\bnormali[sz]ation\b|\blufs\b|\bloudness\b|\bset (?:the )?(?:volume|level|loudness) (?:to|at)\b|\b(?:broadcast|platform|youtube|spotify|streaming) (?:loudness|levels?|standard)\b|\bmake (?:it|the audio|the volume) (?:louder|consistent|even|level|uniform)\b|\bvolume (?:consistent|even|level)\b|\blevel (?:the|out the) (?:audio|volume|sound)\b", EXACT),
                 (r"\btoo quiet\b|\btoo loud\b|\bvolume\b|\blouder\b|\bquieter\b", SYNONYM)),
    # Wave D speed curves (review RD2): a curve by NAME ("hero speed ramp",
    # "the montage curve") or "speed ramp/curve" is this intent too; the
    # planner reads the name into the preset slot (never a constant factor).
    "speed": ((r"\b(?:montage|hero|bullet|jump[- ]?cut|flash[- ]?(?:in|out)|ramp[- ]?(?:up|down))\s+(?:speed\s+)?(?:ramp|curve|preset)\b"
               r"|\bspeed[- ]?(?:ramp|curve)s?\b|\bspeed[- ]?ramp(?:ing|ed)?\b|\bbullet[- ]?time\b"
               # "the intro drags": too slow, said as a complaint (wave D3, E3)
               r"|^(?!.*\b(?:music|song|track|beat)\b).*\b(?:drags?|dragging|feels? (?:too )?(?:slow|long)|(?:is|are|feels?) too slow)\b", EXACT),
              (r"\b(?:slow|speed)\s+(?:that|the\s+[\w ]{0,20}?(?:clip|video|footage|shot|part|bit|section))\s+(?:down|up)\b"
               # Final QA r3: "slow clip 3 down", "speed clip two up"
               rf"|\b(?:slow|speed)\s+{CLIP_PHRASE}\s+(?:(?:a\s+)?(?:little|bit|lot|touch)\s+)?(?:down|up)\b|\bspeed (?:it|this|the (?:video|clip|footage)|everything)?\s*(?:up|down)\b|\b(?:slow|speed) (?:it|this|the (?:video|clip|footage))? ?(?:down|up)\b|\bslow[- ]?mo(?:tion)?\b|\bslowmo\b|\b\d+(?:\.\d+)?\s*x\b(?![\dx:])|\b(?:double|half|quarter|twice the|half the|1\.5x|2x|0\.5x) (?:the )?speed\b|\bfaster\b|\bslower\b|\btime[- ]?lapse\b|\bplayback (?:speed|rate)\b|\bfast[- ]?forward\b|\bmake (?:it|this) (?:faster|slower|quicker)\b|\bspeed ramp\b|\btwice as fast\b"
               # final sweep 2 r2: "make clip 2 (take) twice as long" is its speed
               rf"|\b(?:make|have|let)\s+{CLIP_PHRASE}\s+(?:take\s+|last\s+|play\s+)?(?:twice|half)\s+as\s+long\b", EXACT),
              (r"\bspeed\b|\bquick(?:er)?\b|\btempo of the video\b", SYNONYM)),
    # CapCut's Freeze (wave D): hold the frame at a moment. "freeze frame"
    # used to reach the on-device model and come back as a title (RD2).
    "freeze": ((r"\bfreeze(?:[- ]?frames?|s|d)?\b(?!\s+(?:up|out)\b)|\bhold (?:the|this|that) (?:frame|shot|picture|image)\b"
                r"|\bhold\s+(?:on\s+)?(?:the|this|that)\s+(?:final|last|first|opening|end|ending)\s+(?:frame|shot|image)\b"
                r"|\bstill frame\b", EXACT),),
    # A split at a named time ("split at 3 seconds", "split the clip at 1:05");
    # "split it into 3 shorts" is the shorts intent.
    "split": ((r"\bsplit\s+(?:it\s+|this\s+|that\s+|here\s+|the\s+(?:\w+\s+)?(?:clip|video|footage|shot|timeline)\s+)?(?:at|@)\s+(?:\d|the playhead\b|the (?:[\w'-]+ ){0,2}marker\b)"
               r"|\bsplit (?:it |the clip )?(?:at|on) the (?:playhead|(?:[\w'-]+ ){0,2}marker)\b|\bcut (?:it|the clip) in (?:two|half) at\b"
               # CapCut's Split button: "split here", "split this clip" = at the playhead
               r"|\bsplit\s+(?:it\s+|this\s+|that\s+|the\s+(?:\w+\s+)?clip\s+)?(?:right\s+)?(?:here|now|at the cursor)\b"
               r"|^split(?:\s+(?:it|this|that|the (?:\w+ )?clip|this clip))?$"
               # Final QA r3: "split clip 2", "split the third clip at 7s",
               # "cut clip one in half" (a named clip; its time, or a question)
               rf"|\bsplit\s+{CLIP_PHRASE}"
               rf"|\b(?:cut|split|chop|slice)\s+(?:{CLIP_PHRASE}|it|this|that)\s+(?:in|into)\s+(?:half|two|2)\b", EXACT),),
    # --- Wave D3 (E3): the CapCut clip edits ------------------------------
    "delete_clip": ((_DELETE_CLIP, EXACT),),
    "duplicate": ((rf"\b(?:duplicate|copy|clone|repeat)\s+{CLIP_PHRASE}(?!\s*'s\b)"
                   r"|^(?:duplicate|copy|clone)\s+(?:it|this|that)$"
                   rf"|\b(?:make|add|create)\s+(?:a\s+|another\s+)?(?:copy|duplicate)\s+of\s+(?:{CLIP_PHRASE}|it|this|that)", EXACT),),
    "move_clip": ((rf"\b(?:move|put|drag|place|shift|bring|send|shove|slide)\s+(?:{CLIP_PHRASE}|it|this|that)\s+"
                   r"(?:(?:to|at)\s+(?:the\s+)?(?:very\s+)?(?:end|start|beginning|front|back|finish)\b|(?:to\s+)?(?:be\s+)?(?:first|last)\b"
                   rf"|(?:right\s+|just\s+)?(?:before|after|in front of|behind)\s+{CLIP_PHRASE})"
                   rf"|\bswap\s+(?:the\s+)?(?:{ORDINAL}|{CLIP_NOUN}\s+\d)"
                   rf"|\bswitch\s+(?:the\s+)?(?:order of\s+(?:the\s+)?)?(?:{ORDINAL})\s+(?:and|&)\s+(?:the\s+)?{ORDINAL}\s+{CLIP_NOUN}"
                   r"|\breverse\s+the\s+(?:order|sequence)\s+of\s+(?:the\s+|all\s+(?:the\s+)?)?clips\b"
                   # final sweep 4: "reverse the order" alone got the menu
                   r"|\breverse\s+the\s+(?:clip\s+|clips'?\s+)?(?:order|sequence)\b"
                   rf"|\bmake\s+{CLIP_PHRASE}\s+(?:the\s+)?(?:first|last)(?:\s+(?:one|clip))?\b", EXACT),),
    "zoom": ((_ZOOM, EXACT),),
    "rotate": ((r"^rotate(?:\s+(?:it|this|that|the\s+[\w ]{0,20}?(?:clip|video|shot)))?$"
                r"|\brotat(?:e|ed|ing)\b(?=.*?\b\d{1,3}\s*(?:°|degrees?\b|deg\b)|.*?\b(?:by\s+)?(?:90|180|270|45)\b(?!\s*(?:%|x\b|s\b|sec))|.*?\b(?:upside down|sideways|clockwise|counter[- ]?clockwise|anti[- ]?clockwise)\b)"
                r"|\b(?:turn|flip)\s+(?:it|this|that|the\s+[\w ]{0,20}?(?:clip|video|shot)"
                r"|(?:clip|shot)\s+(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten))\s+upside down\b"
                r"|\bstraighten\s+(?:it|this|the\s+[\w ]{0,20}?(?:clip|video|shot))\s+by\s+\d", EXACT),),
    "adjust": ((r"\b(?:brighten|darken|desaturate)\b"
                # final sweep 2 r2: "make clip 1 more saturated" was unread
                rf"|\b(?:make|turn|get)\s+{CLIP_PHRASE}\s+(?:a\s+(?:bit|little|touch|lot)\s+|much\s+|way\s+|slightly\s+)?"
                r"(?:brighter|darker|lighter|dimmer|more saturated|less saturated|more colou?rful|less colou?rful"
                r"|more contrasty|less contrasty|flatter)\b"
                r"|\b(?:make|turn|get)\s+(?:it|this|that|everything|the\s+[\w ]{0,24}?)\s+(?:a\s+(?:bit|little|touch|lot)\s+|much\s+|way\s+|slightly\s+)?"
                r"(?:brighter|darker|lighter|dimmer|more saturated|less saturated|more colou?rful|less colou?rful|more contrasty|less contrasty|flatter)\b"
                r"|\b(?:increase|boost|raise|bump(?: up)?|up|turn up|crank(?: up)?|add(?: more| some)?|more|lower|reduce|decrease|drop|lessen|turn down|less|tone down|dial (?:up|down|back))\s+"
                r"(?:the\s+|some\s+)?(?:brightness|contrast|saturation|exposure|vibrance|colou?r saturation)\b"
                r"|\b(?:brightness|contrast|saturation|exposure|vibrance)\s+(?:up|down|higher|lower|\+|-)"
                r"|\b(?:it'?s|it is|looks?|the (?:video|clip|footage|picture) (?:is|looks))\s+(?:way |far |a bit |a little |too |very |so )*(?:too )?(?:dark|bright|dull|washed out|flat|oversaturated)\b", EXACT),
               (r"\bbrighter\b|\bdarker\b|\bbrightness\b|\bcontrast\b|\bsaturation\b|\bexposure\b", SYNONYM)),
    "remove_feature": ((REMOVE_FEATURE, EXACT),),
    "retext": ((RETEXT, EXACT),),
    "transform": ((TRANSFORM_REQUEST, EXACT),),
    # Stickers are placed from the Stickers panel: `add_sticker` fetches its
    # artwork from a CDN, so a plan may not name it (schema.PLAN_DENY).
    "sticker": ((r"\bstickers?\b|\bemojis?\b|\bgifs?\b|\bheart (?:icon|emoji|sticker)\b", EXACT),),
    # a mirror image; "flip it upside down" is a rotation (tie-break below).
    # Wave E (F4b): "flip the second clip" / "flip it" (CapCut's Mirror is
    # the horizontal flip), "unflip it", "flip it back".
    "flip": ((r"\bmirror(?:ed|ing|s)?\b|\bflip(?:ped|ping|s)?\b(?:(?!upside).)*\b(?:horizontal(?:ly)?|vertical(?:ly)?|sideways|left to right|right to left)\b"
              rf"|\b(?:un-?)?flip(?:ped|s)?\s+(?:{CLIP_PHRASE}|it|this|that|them|everything|the (?:video|picture|image|footage))"
              r"(?!\s+(?:upside|over|to (?:the )?(?:vertical|portrait|landscape|horizontal|square)))"
              r"|\bun-?flip\b|^flip(?:\s+it)?(?:\s+back)?$", EXACT),),
    "clip_length": ((CLIP_LENGTH, EXACT),),
    # Wave E (F2): "blur the background", "make the background black",
    # "use this image as the background"; "set the overlay to screen",
    # "multiply blend the top clip" (canvas_vocab.py).
    "canvas": ((_CV.CANVAS_PHRASE, EXACT),),
    "blend": ((_CV.BLEND_PHRASE, EXACT),),
    # Wave E (F3): "make my voice sound like a robot", "add echo to the
    # voiceover", "remove the voice effect" (voice_vocab.py).
    "voice_effect": ((_VV.VOICE_PHRASE, EXACT),),
    "animation": ((_AV.ANIM_PHRASE, EXACT),),
    "trim": ((rf"\b(?:trim|cut|remove|delete|drop|chop|lose|take (?:off|out)|get rid of|skip|shave)\s+(?:off\s+|out\s+|away\s+)?(?:the\s+)?(?:first|last|opening|closing|intro|outro)?\s*(?=.*{_HAS_RANGE})|\btrim (?:it|this|the (?:start|end|beginning|intro|outro|clip|video))\b|\bstart (?:it |the video )?(?:at|from)\s+\d|\bend (?:it |the video )?at\s+\d|\bkeep (?:only )?(?:the )?(?:first|last)\b|\bremove the (?:intro|outro|beginning|ending)\b|\bcut (?:the )?(?:intro|outro|beginning|ending|start|end)\b"
              # final sweep 4: "keep 3s through 9s" (a range to KEEP, no "only")
              r"|\bkeep\s+(?:only\s+|just\s+)?(?:everything\s+|the\s+(?:part|bit|section)\s+)?(?:from\s+|between\s+)?\d+(?:\.\d+)?\s*(?:s|sec|secs|seconds?|m|min|mins|minutes?)?\s*(?:to|-|–|until|till|through|thru|and)\s*\d", EXACT),
             (r"\btrim\b|\bshorter\b|\bchop\b", SYNONYM)),
    # "write 'The End' over the last 2 seconds" (review RD3): quoted words
    # written / put / typed somewhere are a title.
    "title": ((r"\b(?:write|type|put|add|show|display|overlay)\s+(?:the\s+(?:words?|text|line)\s+)?[\"'][^\"']+[\"']\s+(?:over|on|across|at|during|for|in)\b"
               # final sweep 4: "… and 'AFTER' on clip 3" — a second quoted
               # title with its own place, no verb of its own
               r"|^[\"'“”‘’]q[\"'“”‘’]\s+(?:over|on|across|at|during|for|in)\b|\blower[- ]?third\b|\bname (?:tag|plate|card|strap|title|banner)\b|\bnameplate\b|\bstrap(?:line)?\b|\b(?:add|put|show|display|write|overlay)\s+(?:a\s+|the\s+|some\s+|my\s+)?(?:title|text|caption text|label|heading|headline|super|on[- ]screen text|text overlay|name)\b|\btitle (?:card|it|this)\b|\bintroduce (?:me|him|her|them|the speaker|the guest)\b|\bname and handle\b|\bspeaker name\b|\bwho'?s talking\b", EXACT),
              (r"\btext\b|\btitle\b|\blabel\b|\bheadline\b", SYNONYM)),
    "brand": ((r"\bbrand(?:ing| kit| it| this|ed)?\b|\bwatermark\b|\bmy (?:handle|logo|colou?rs|brand)\b|\bapply (?:my |the )?(?:brand|kit)\b|\badd (?:my |the |a )?(?:handle|watermark|logo)\b|\bbrand colou?rs\b|\bhashtags?\b", EXACT),
              (r"\bhandle\b|\blogo\b|\b@\w+\b", SYNONYM)),
    "end_card": ((r"\bend[- ]?card\b|\bend (?:screen|slate|plate|title|frame)\b|\bouttro card\b|\boutro(?: card| screen)?\b|\bclosing (?:card|slate|screen)\b|\bfollow (?:me|us) (?:card|screen|at the end)\b|\bcall to action at the end\b|\bcta at the end\b|\bcta card\b|\bsubscribe (?:card|screen|reminder)\b", EXACT),
                 (r"\bcta\b|\bcall to action\b|\bfollow (?:me|us)\b", SYNONYM)),
    "transitions": ((rf"\bbetween\s+(?:the\s+)?{ORDINAL}\s+(?:and|&)\s+(?:the\s+)?{ORDINAL}\s+{CLIP_NOUN}"
                     rf"|\bbetween\s+{CLIP_NOUN}\s+\d{{1,2}}\s+(?:and|&)\s+\d{{1,2}}\b"
                     r"|\btransitions?\b|\bcross[- ]?(?:fade|dissolve)s?\b|\bdissolves?\b|\bwipes?\b|\bwhip pans?\b|\bswipes? between\b|\b(?:smooth|soft|clean|punchy|cinematic|fancy|nice|cool|fun)\s+(?:cuts|transitions?)\s+between\b|\bfade between (?:the )?clips\b|\bblend (?:the )?(?:clips|cuts)\b|\bsoften the cuts\b|\bcut points? (?:smoother|softer)\b"
                     # A named look + a seam reference is a transition request even without the word:
                     # "smooth zoom between every clip", "a glitch at every cut", "fade to black at the last cut".
                     # ("fade to black at the END" is the closing fade — CUT_PRECEDENCE routes it to `fade`.)
                     r"|\b(?:zoom|glitch|whip|wipe|slide|push|blur|dissolve|flash|pixelat\w*|mosaic|spiral|spin|ripple|iris|diamond|blinds|checkerboard|film burn|(?:dip|fade) to (?:black|white))\b(?=.*\b(?:between|at|on) (?:the |every |each |all (?:the )?)?(?:(?:last|first|final|next|second) )?(?:clips?|cuts?|seams?|scenes?|shots?|hook|start|end|beginning|clip changes?)\b)", EXACT),
                    (r"\bbetween (?:the |every |each |all (?:the )?)?(?:clips?|cuts?|scenes?|shots?)\b|\bsmooth(?:er)? cuts\b|\bthe cuts\b", SYNONYM)),
    # Review RD3: "render / export / output / save IT (or this) for <platform>"
    # is the export preset, whatever the pronoun; the platform phrase alone
    # ("for youtube", 0.85) had made it the Auto edit (silences cut, text
    # added). "make it into a short" stays the Auto edit.
    "export_preset": ((r"\b(?:render|export|output|save|encode)\s+(?:it|this|that|the (?:video|edit|project|clip|timeline|movie))?\s*(?:for|as)\s+(?:instagram|reels?|tiktok|youtube|shorts?|linkedin|stories|story|facebook|twitter)\b|\bexport (?:preset|settings?|for|as|to)\b|\bexport[- ]ready\b|\brender (?:for|as|settings?)\b|\b(?:set|use|apply)\s+(?:the\s+)?(?:\w+\s+)?(?:export\s+)?preset\b|\boptimi[sz]e (?:the )?(?:export|output|render|settings) for\b|\bformat (?:it|this) for\b|\bsettings for (?:instagram|reels|tiktok|youtube|shorts|linkedin|stories)\b|\b(?:instagram|reels|tiktok|youtube|shorts|linkedin|story|stories) (?:export|settings?|specs?|format|preset|ready|spec)\b|\bbitrate\b|\bready to (?:upload|post|publish)\b", EXACT),
                      (r"\bexport\b|\bpreset\b|\bupload (?:it|this|ready)\b", SYNONYM)),
    # Review RE: the bare noun "voiceover" used to be this EXACT row, so
    # "lower the voiceover by 6 dB" planned a NEW voice-over (a 60 MB voice
    # download, then "what should it say?"). A new one needs a creation verb,
    # quoted words, or the noun on its own.
    # Final sweep 4: never with a LEVEL word in the clause — "make the
    # voiceover 3 dB quieter" asked to download a 60 MB Piper voice.
    "voiceover": ((r"(?!.*\b(?:quieter|louder|softer|less loud|more quiet|volume|level|gain|\d+(?:\.\d+)?\s*(?:d\s?b|decibels?))\b)"
                   r"\b(?:add|put|record|generate|create|make|need|want|give(?:\s+(?:it|me))?|write|lay|include|do)\s+"
                   r"(?:a\s+|an\s+|some\s+|the\s+|my\s+)?(?:[\w'-]+\s+){0,2}?(?:voice[- ]?over|narration|narrator)\b"
                   r"|^(?:an?\s+|some\s+)?(?:ai\s+)?(?:voice[- ]?over|narration)$"
                   r"|\bvoice[- ]?over\s+(?:saying|that says|reading|of the|for the)\b|\bnarrate\b|\btts\b|\btext[- ]to[- ]speech\b|\bai voice\b|\b(?:add|put|record|generate|make|have)\s+(?:a\s+|an\s+|some\s+|the\s+)?(?:\w+\s+)?voice\s+(?:say(?:ing)?|read(?:ing)?|that says|line|clip|narration)\b|\bvoice (?:say|saying|read|reading)\b|\bsay(?:ing)?\s+[\"“'].+[\"”']|\bread (?:this|it|the text) (?:out|aloud)\b|\bspoken (?:intro|outro|line)\b", EXACT),
                  (r"\bnarration\b|\bsay\b", SYNONYM), (r"\bvoice\b|\bvoice[- ]?over\b", WEAK)),
    "stabilize": ((r"\bstabili[sz](?:e|ation|er)\b|\b(?:fix|remove|reduce|smooth out|smooth)\s+(?:the\s+)?(?:shake|shakiness|shaky (?:footage|video|camera|cam|clip)|camera shake|jitter|wobble)\b|\bshaky\b|\bhandheld shake\b|\bsteady (?:it|this|the (?:shot|footage|video|clip))\b|\bsteadicam\b|\bwarp stabili[sz]er\b", EXACT),
                  # Review RE: a bare "shake" is CapCut's Shake COMBO ("shake the
                  # second clip the whole time" replaced the clip with a
                  # stabilised render); only "fix / remove the shake" and
                  # "shaky" (EXACT above) mean stabilise.
                  (r"\bjittery\b|\bwobbly\b", SYNONYM)),
    "upscale": ((r"\bupscal(?:e|ed|ing)\b|\bup-?res\b|\bsuper[- ]?resolution\b|\benhance (?:the )?resolution\b|\b(?:make|render|export) (?:it|this|the video) (?:4k|1080p|hd|sharper|higher res(?:olution)?)\b|\b(?:2|4)x (?:resolution|res|upscale)\b|\bincrease (?:the )?resolution\b|\bsharpen (?:it|this|the video) up\b|\bto 4k\b|\b(?:in|at) 4k\b", EXACT),
                (r"\bresolution\b|\b4k\b|\bsharper\b|\bblurry\b|\bhd\b", SYNONYM)),
    "ask": ((r"^(?:how long|what(?:'s| is) the (?:length|duration|aspect|resolution|size|canvas|loudness|language)|what (?:did|does|do) (?:i|we|you|this|it)|what(?:'s| is) (?:on|in) (?:the|this)|is there|are there|does (?:it|this) have|do (?:i|we) have|how many|which (?:brain|model|tools?)|what can you do|what tools|help|tell me about|describe (?:the|this)|summari[sz]e (?:the|this)|explain (?:the|this)|why (?:did|is|was)|where (?:is|are)|show me the|list the|kya hai|kitna lamba|kitne)\b|\?$", EXACT),
            (r"\bwhat\b|\bhow\b|\bwhich\b|\bwhy\b|\bstatus\b|\binfo\b", WEAK)),
}

#: Intents whose SYNONYM row is a bare noun that also appears inside other
#: intents' phrases; they only score when nothing more specific matched.
_NOUN_ONLY_FALLBACK: frozenset[str] = frozenset({"captions", "music", "hook", "color_look", "loudness",
                                                  "title", "brand", "transitions", "export_preset",
                                                  "voiceover", "speed", "trim", "clean_audio", "shorts",
                                                  "remove_fillers", "remove_silences", "ask", "reframe",
                                                  "beat_sync", "duck", "stabilize", "upscale", "end_card",
                                                  "auto_edit", "tighten", "translate_captions"})

_COMPILED: dict[str, tuple[tuple[re.Pattern, float], ...]] = {
    intent: tuple((re.compile(p), s) for p, s in rows) for intent, rows in PHRASES.items()}
_CUT_COMPILED = tuple((re.compile(p), i, s) for p, i, s in CUT_PRECEDENCE)

#: When two intents both match at EXACT in one clause, the more specific one
#: wins. Rows are (winner, loser).
_TIE_BREAKS: tuple[tuple[str, str], ...] = (
    ("title", "trim"), ("title", "tighten"),
    ("translate_captions", "captions"), ("tighten", "remove_silences"), ("tighten", "remove_fillers"),
    ("tighten", "trim"), ("beat_sync", "music"), ("beat_sync", "trim"), ("beat_sync", "speed"),
    ("duck", "music"), ("duck", "loudness"), ("end_card", "title"), ("end_card", "brand"),
    ("brand", "title"), ("brand", "end_card"), ("hook", "title"), ("hook", "captions"),
    ("voiceover", "title"), ("voiceover", "captions"), ("voiceover", "speed"), ("voiceover", "trim"),
    ("shorts", "trim"), ("shorts", "reframe"), ("shorts", "auto_edit"), ("reframe", "export_preset"),
    ("export_preset", "auto_edit"), ("auto_edit", "captions"), ("auto_edit", "music"),
    ("auto_edit", "reframe"), ("auto_edit", "color_look"), ("auto_edit", "clean_audio"),
    ("color_look", "transitions"), ("transitions", "trim"), ("transitions", "speed"),
    ("transitions", "hook"), ("transitions", "reframe"), ("transitions", "upscale"), ("transitions", "end_card"),
    ("clean_audio", "loudness"), ("loudness", "clean_audio"), ("remove_fillers", "trim"),
    ("remove_silences", "trim"), ("remove_fillers", "captions"), ("upscale", "export_preset"),
    ("upscale", "reframe"), ("stabilize", "speed"), ("speed", "trim"), ("title", "captions"),
    ("captions", "translate_captions"), ("undo", "trim"), ("ask", "captions"),
    # QA-018: "turn the music down" is a LEVEL, "set the music to -20 dB" is not
    # a request for a second bed, "fade to black between the clips" stays the
    # seam transition it always was, and "lower the music under my voice" stays duck.
    ("volume", "music"), ("fade", "music"), ("mute", "music"), ("fit_music", "music"),
    ("fit_music", "trim"), ("fit_music", "fade"), ("fit_music", "volume"), ("fade", "trim"),
    ("volume", "loudness"), ("volume", "clean_audio"), ("mute", "clean_audio"), ("mute", "volume"),
    ("duck", "volume"), ("duck", "mute"), ("transitions", "fade"), ("fade", "volume"),
    ("audit", "ask"), ("preview", "export_preset"),
    ("remove_music", "music"), ("remove_music", "trim"), ("remove_music", "mute"),
    ("remove_music", "clean_audio"), ("remove_music", "fit_music"),
    ("reverse", "speed"), ("reverse", "trim"), ("undo", "reverse"),
    # Wave D3 (E3): a clip edit names its clip; the look / range readings lose.
    ("adjust", "color_look"), ("adjust", "loudness"), ("duplicate", "trim"), ("move_clip", "trim"),
    ("move_clip", "reverse"), ("rotate", "transitions"), ("sticker", "title"), ("sticker", "brand"),
    ("rotate", "flip"),
    # Final QA r3: "flip the sticker horizontally" is a flip of the sticker
    # there, not a request to ADD one ("Want a sticker?")
    ("flip", "sticker"),
    # Wave E (F2): a canvas / blend clause names the background or the
    # overlay; the look, colour and title readings of its words lose.
    ("canvas", "color_look"), ("canvas", "adjust"), ("canvas", "transitions"), ("canvas", "title"),
    ("canvas", "reframe"), ("canvas", "remove_feature"), ("canvas", "trim"),
    ("blend", "title"), ("blend", "color_look"), ("blend", "transitions"), ("blend", "adjust"),
    # Wave E (F3): a voice-effect clause names an effect on a sound ("add echo
    # to the voiceover", "remove the voice effect", "make my voice deeper").
    ("voice_effect", "voiceover"), ("voice_effect", "volume"), ("voice_effect", "remove_feature"),
    ("voice_effect", "clean_audio"), ("voice_effect", "mute"), ("voice_effect", "music"),
    ("voice_effect", "loudness"), ("voice_effect", "fade"), ("voice_effect", "title"),
    ("voice_effect", "speed"), ("voice_effect", "transitions"), ("voice_effect", "color_look"),
    ("voice_effect", "trim"),
    ("delete_clip", "trim"), ("zoom", "transitions"), ("zoom", "reframe"),
    # final sweep 2 r2: "zoom clip 1 to 2x" / "scale clip 3 to 1.5x" is a size
    ("zoom", "speed"),
    # Wave E (F1): an animation names its motion and its clip / sticker.
    ("animation", "zoom"), ("animation", "rotate"), ("animation", "fade"), ("animation", "sticker"),
    ("animation", "stabilize"), ("animation", "remove_feature"), ("animation", "title"),
    ("animation", "color_look"), ("animation", "speed"), ("animation", "adjust"),
)


#: A speed curve BY NAME ("flash in speed curve", "jump cut speed ramp") and
#: taking a speed curve off (Final QA r2). Checked before every other row:
#: the transitions row read "flash … on the second clip" as a Flash
#: transition at every cut, the cut/trim rows read "jump CUT", and the
#: remove-feature row refused "remove the speed curve" (telling the user to
#: press Delete, which deletes the clip).
_SPEED_CURVE_FIRST_RE = re.compile(
    r"\b(?:montage|hero|bullet|jump[- ]?cut|flash[- ]?(?:in|out)|ramp[- ]?(?:up|down))\s+(?:speed\s+)?"
    r"(?:ramp|curve|preset)s?\b"
    r"|\b(?:remove|reset|clear|delete|drop|undo|lose|kill|strip|take\s+(?:off|out)|get\s+rid\s+of|turn\s+off)\s+"
    r"(?:the\s+|its\s+|any\s+|this\s+|that\s+)?speed[- ]?(?:ramp|curve)s?\b")


#: An EXISTING title's timing (final sweep 2 r2): "make the title last 2
#: seconds longer" SHORTENED it, "title 1 second shorter" asked which part of
#: the VIDEO to cut, "start the title at 0:02" added a second title. Checked
#: before the cut rows, which read "shorter" / "start at" as a trim.
_TEXT_RETIME_FIRST_RE = re.compile(
    r"^(?:(?:please\s+)?(?:make|have|let|set|get|move|shift|start|begin|end|extend|shorten|lengthen)\s+)?"
    r"(?:the\s+|my\s+|this\s+|that\s+)?(?:title|heading|headline)\s+"
    r"(?!.*\b(?:add|new|another|insert|write|put|says?|saying|reads?|reading|clips?|video|music|song)\b)(?!.*[\"“])"
    r"(?:.*\b(?:longer|shorter|lasts?|later|earlier|until|till)\b"
    r"|(?:(?:to\s+)?(?:start|begin|end|appear|disappear)s?\s+)?(?:at|from)\s+\d)")


def _resolve_clause(clause: str) -> tuple[str, float] | None:
    """The single best intent for one clause."""
    if _SPEED_CURVE_FIRST_RE.search(clause):
        return "speed", EXACT
    if _TEXT_RETIME_FIRST_RE.search(clause):
        return "title", EXACT
    for rx, intent, score in _CUT_COMPILED:
        if rx.search(clause):
            return intent, score
    best: dict[str, float] = {}
    for intent, rows in _COMPILED.items():
        for rx, score in rows:
            if rx.search(clause):
                best[intent] = max(best.get(intent, 0.0), score)
                break
    if not best:
        return None
    # `ask` at EXACT only when the clause reads as a question — a stray "?"
    # after an edit verb ("add captions?") is still an edit.
    if "ask" in best and len(best) > 1 and best["ask"] >= EXACT:
        if re.match(r"^(?:add|put|make|remove|cut|trim|give|apply|turn|speed|slow|translate|tighten|reframe|crop|clean|normali[sz]e|export|upscale|stabili[sz]e|duck|brand)\b", clause):
            best["ask"] = WEAK
    top = max(best.values())
    tied = [i for i, s in best.items() if s == top]
    if len(tied) == 1:
        return tied[0], top
    # Specific-over-general at equal score, via the explicit table; a pair the
    # table does not order keeps phrase-table order (declared first wins).
    losers = {loser for winner, loser in _TIE_BREAKS if winner in tied and loser in tied}
    for intent in PHRASES:
        if intent in tied and intent not in losers:
            return intent, top
    return tied[0], top


# --------------------------------------------------------------------------
# 4. Templates (auto_edit variants)
# --------------------------------------------------------------------------

def _template_hit(clause: str) -> str | None:
    from .presets import edit_templates
    try:
        templates = edit_templates()
    except Exception:
        return None
    for name, tpl in templates.items():
        for kw in tpl.keywords:
            if re.search(rf"\b{re.escape(kw)}\b", clause):
                return name
        if re.search(rf"\b{re.escape(name.replace('_', ' '))}\b", clause):
            return name
    return None


# --------------------------------------------------------------------------
# 5. detect
# --------------------------------------------------------------------------

_UNDO_ONLY = re.compile(r"^(?:undo|redo)\b")
#: "… and make it red / bigger / bold": a look for the text the clause before added
_LOOK_WORD = (r"(?:red|yellow|white|black|green|blue|pink|orange|purple|cyan|gold|grey|gray|lime|teal|bigger|smaller"
              r"|larger|huge|bold|bolder|italic|uppercase|all caps|caps)")
_IT_LOOK_RE = re.compile(rf"^(?:and\s+|then\s+)*(?:make|turn|set|colou?r|have|give)\s+(?:it|them)\s+"
                         rf"(?:(?:a\s+)?(?:bit\s+|little\s+|lot\s+)?{_LOOK_WORD}\s*(?:and\s+|,\s*)?)+$")


def _clause_slots(clause: str, whole: S.Slots) -> S.Slots:
    """The clause's own slots, with the case-bearing `name` restored from the
    whole prompt when the clause contains it.

    WHY: `split_clauses` lower-cases the prompt and `NAME_RE` reads a proper-
    noun run by its capitals, so "add a lower third for Priya Sharma
    @priya.codes" found the name at prompt level and lost it at clause level
    — the title recipe then asked "What should the title say?" for a name it
    had been given, and the benchmark's empty answer looped it to the round
    cap (case 16). Only a name the clause actually contains crosses over, so
    a two-clause prompt keeps its names apart."""
    own = S.extract(clause)
    if own.name is None and whole.name and whole.name.lower() in clause:
        return S.merge(own, S.Slots(name=whole.name))
    return own


_RETEXT_RX = re.compile(RETEXT)


def _whole_prompt_retext(prompt: str, hits: list[IntentHit], whole: S.Slots) -> list[IntentHit]:
    """"fix the typo in the title, it should say Summer Trips" (Final QA): the
    clause split puts the noun and the new words in different clauses, so the
    title clause asked what to ADD and the "say" clause planned a spoken
    voice-over. When the whole prompt reads as a retext, the clauses inside
    the match become ONE retext hit."""
    text = S.normalize(prompt)
    m = _RETEXT_RX.search(text)
    if not m:
        return hits
    span = text[m.start():]
    keep = [h for h in hits if h.clause not in span or h.intent not in ("title", "voiceover", "ask", "remove_feature")]
    return keep + [IntentHit(intent="retext", score=EXACT, clause=span, slots=_clause_slots(span, whole))]


def detect(prompt: str) -> Detection:
    clauses = tuple(split_clauses(prompt))
    whole = S.extract(prompt)
    hits: list[IntentHit] = []
    exclusions: list[str] = []
    unmatched: list[str] = []
    for clause in clauses:
        clause_ex = exclusions_in(clause)
        positive = strip_negations(clause) if clause_ex else clause
        resolved = _resolve_clause(mask_quotes(positive)) if positive.strip() else None
        if "duck" in clause_ex and (resolved is None or (resolved[0] == "music" and resolved[1] < EXACT)):
            # QA-031: "don't duck the music" / "no ducking on the music" is a
            # request to turn ducking OFF, not a pure negation to ignore (and
            # never, as it used to be, a request to turn it ON). The duck hit
            # carries the clause; `duck_off()` reads the negation from it.
            clause_ex = [x for x in clause_ex if x != "duck"]
            positive, resolved = clause, ("duck", EXACT)
        for ex in clause_ex:
            if ex not in exclusions:
                exclusions.append(ex)
        if not positive.strip():
            continue                       # pure negation clause: counted as understood
        template = _template_hit(positive)
        if template and (resolved is None or resolved[0] != "shorts"):
            resolved = ("auto_edit", EXACT)
        if resolved is None and hits and hits[-1].intent in ("title", "captions") and _IT_LOOK_RE.match(positive):
            continue                       # "add a title 'Intro' and make it red": the title's own look
        if resolved is None:
            unmatched.append(clause)
            continue
        intent, score = resolved
        hits.append(IntentHit(intent=intent, score=score, clause=clause,
                              slots=_clause_slots(clause, whole), template=template))
    if not any(h.intent == "retext" for h in hits):
        hits = _whole_prompt_retext(prompt, hits, whole)
    # undo/redo are whole-prompt intents: "undo that and add music" is a
    # sequence the executor cannot honour in one op, so undo alone wins.
    if hits and hits[0].intent in ("undo", "redo") and _UNDO_ONLY.match(S.normalize(prompt)):
        hits = [hits[0]]
    return Detection(prompt=prompt, clauses=clauses, hits=tuple(hits), exclusions=tuple(exclusions),
                     slots=whole, unmatched=tuple(unmatched))


__all__ = ["REMOVE_FEATURE", "FX_NOUN", "CLIP_LENGTH", "INTENTS", "EXACT", "SYNONYM", "WEAK", "RUN_THRESHOLD", "NORMALISE_THRESHOLD",
           "IntentHit", "Detection", "split_clauses", "exclusions_in", "strip_negations", "duck_off",
           "reverse_off", "clip_ref_of", "NTH_REF", "AT_REF", "ORDINAL", "CLIP_NOUN", "CLIP_PHRASE",
           "CUT_PRECEDENCE", "PHRASES", "detect"]
