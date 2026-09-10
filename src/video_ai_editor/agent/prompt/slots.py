"""Slot extraction — the numbers, names and enums inside a prompt (spec §2.2).

Pure functions over text. `extract(prompt) -> Slots`; the grammar calls it per
clause and per whole prompt, the planner turns slots into recipe args, and
`pending.try_parse_answer` (X) reuses the duration/number extractors and the
synonym tables for whole-message clarification answers.

Ordering is the whole trick (§2.2): `ratio` and `upscale_factor` are extracted
BEFORE `speed` and their matched text is masked, so `1080x1920` never reads as
a 1080× speed-up and `4x upscale` never reads as 4× speed; `count` is masked
too so "make 2 shorts" is not "2×". `youtube shorts` is a platform, never a
count of shorts.

`HINGLISH_VERBS` maps the Hinglish imperatives the phone's users actually
type onto the English verbs the grammar keys on. Small and tested rather
than clever: a stemmer for Hindi verbs would be a project of its own, and the
thirty entries cover what the benchmark and the tester logs contain.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Literal

# --------------------------------------------------------------------------
# 1. Normalisation
# --------------------------------------------------------------------------

#: Hinglish imperative → English verb phrase. Ordered longest-first at
#: compile time so "chhota kar do" wins over "kar do".
HINGLISH_VERBS: dict[str, str] = {
    "kar do": "do", "kar dijiye": "do", "karo": "do", "kijiye": "do",
    "laga do": "add", "lagao": "add", "lagaiye": "add", "daal do": "add", "dalo": "add",
    "jod do": "add", "jodo": "add",
    "hata do": "remove", "hatao": "remove", "hataiye": "remove", "nikal do": "remove", "nikalo": "remove",
    "mita do": "remove", "kaat do": "cut", "kaato": "cut", "kat do": "cut",
    "banao": "make", "bana do": "make", "banaiye": "make",
    "chhota karo": "shorten", "chhota kar do": "shorten", "chota karo": "shorten", "chota kar do": "shorten",
    "tez karo": "speed up", "tez kar do": "speed up", "fast karo": "speed up",
    "dheema karo": "slow down", "dheere karo": "slow down", "slow karo": "slow down",
    "sudharo": "fix", "theek karo": "fix", "saaf karo": "clean up", "clean karo": "clean up",
    "badlo": "change", "badal do": "change",
    "bina": "without", "mat": "do not", "nahi chahiye": "without",
    "aur": "and", "phir": "then", "ke saath": "with", "ke liye": "for",
}
_HINGLISH_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(HINGLISH_VERBS, key=len, reverse=True)) + r")\b")

_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lower-case, collapse whitespace, strip trailing punctuation, `&`→and,
    `w/`→with, Hinglish verbs → English. Quoted text keeps its case? No —
    `quoted_text` is re-extracted from the ORIGINAL by `extract`, so the
    normalised form may lower-case everything."""
    t = text.replace("’", "'").replace("“", '"').replace("”", '"')
    t = t.replace("&", " and ").replace(" w/ ", " with ")
    t = _WS_RE.sub(" ", t).strip().lower()
    t = t.rstrip(" .!?;,")
    t = _HINGLISH_RE.sub(lambda m: HINGLISH_VERBS[m.group(1)], t)
    return _WS_RE.sub(" ", t).strip()


# --------------------------------------------------------------------------
# 2. Tables
# --------------------------------------------------------------------------

Platform = Literal["reels", "tiktok", "shorts", "story", "youtube_16x9", "youtube_4k",
                   "ig_feed_1x1", "ig_feed_4x5"]

#: Ordered: more specific phrases first (`youtube shorts` before `youtube`).
PLATFORM_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\byou ?tube shorts?\b|\byt shorts?\b|\bshorts\b", "shorts"),
    (r"\b4k\b|\buhd\b|\b2160p\b", "youtube_4k"),
    (r"\byou ?tube\b|\byt\b", "youtube_16x9"),
    # `story` before `reels`: "instagram story" is a story, not a reel.
    (r"\bstory\b|\bstories\b|\bsnap(chat)?\b", "story"),
    (r"\breels?\b|\binstagram\b|\big\b|\binsta\b", "reels"),
    (r"\btik ?tok\b", "tiktok"),
    (r"\bsquare\b|\bfeed post\b|\b1:1\b|\bcarousel\b", "ig_feed_1x1"),
    (r"\b4:5\b|\bportrait feed\b|\blinkedin\b", "ig_feed_4x5"),
)

PLATFORM_RATIO: dict[str, str] = {
    "reels": "9:16", "tiktok": "9:16", "shorts": "9:16", "story": "9:16",
    "youtube_16x9": "16:9", "youtube_4k": "16:9", "ig_feed_1x1": "1:1", "ig_feed_4x5": "4:5",
}

#: Single source for platform loudness — mirrors dispatch._EXPORT_PRESETS and
#: is asserted equal to it by tests/test_prompt_slots.py.
PLATFORM_LUFS: dict[str, float] = {
    "reels": -16.0, "tiktok": -16.0, "shorts": -14.0, "story": -16.0,
    "ig_feed_1x1": -16.0, "ig_feed_4x5": -16.0, "youtube_16x9": -14.0, "youtube_4k": -14.0,
}

#: Shorts length caps by platform (§2.4 `shorts.max_dur`).
PLATFORM_SHORT_MAX_S: dict[str, float] = {
    "tiktok": 60.0, "shorts": 60.0, "reels": 90.0, "story": 30.0,
}

RATIO_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b9\s*[:x×/]\s*16\b|\b1080\s*[x×]\s*1920\b|\bvertical\b|\bportrait\b(?! feed)|\bupright\b", "9:16"),
    (r"\b16\s*[:x×/]\s*9\b|\b1920\s*[x×]\s*1080\b|\blandscape\b|\bhorizontal\b|\bwidescreen\b|\bwide\b", "16:9"),
    (r"\b1\s*[:x×/]\s*1\b|\b1080\s*[x×]\s*1080\b|\bsquare\b", "1:1"),
    (r"\b4\s*[:x×/]\s*5\b|\b1080\s*[x×]\s*1350\b|\bportrait feed\b", "4:5"),
)

LANGUAGE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bhinglish\b|\broman(?:ised|ized)?\s+hindi\b|\bhindi in english letters\b|\blatin hindi\b", "hinglish"),
    (r"\bhindi\b|\bdevanagari\b|\b(?:in|to)\s+hi\b", "hi"),
    (r"\bspanish\b|\bespañol\b|\bespanol\b|\b(?:in|to)\s+es\b", "es"),
    (r"\benglish\b|\b(?:in|to)\s+en\b|\bangrezi\b", "en"),
)

CAPTION_STYLE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bkaraoke\b|\bword[- ]by[- ]word\b|\bhighlight(?:ed)?\b|\bword emphasis\b|\bbouncing\b|\bpop(?:ping)? words?\b", "word_emphasis"),
    (r"\bchunky\b|\bbold captions?\b|\bbig captions?\b|\big[- ]style\b|\binstagram[- ]style\b|\bblock captions?\b", "ig_chunky"),
    (r"\bplain\b|\bsimple\b|\bclean captions?\b|\bminimal captions?\b|\bnormal captions?\b|\bregular captions?\b", "default"),
)
CAPTION_POSITION_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(?:at|on|to|near)?\s*(?:the\s+)?top\b", "top"),
    (r"\b(?:in|at|to)?\s*(?:the\s+)?(?:center|centre|middle)\b", "center"),
    (r"\b(?:at|on|to|near)?\s*(?:the\s+)?bottom\b", "bottom"),
)
MODEL_UPGRADE_RE = re.compile(r"\b(accurate|better|precise|high[- ]quality|hq|best)\s+(captions?|subtitles?|transcript(?:ion)?)\b"
                              r"|\b(captions?|subtitles?)\s+(?:more\s+)?(accurate|precise)\b")

MOOD_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\blo-?fi\b", "lofi"),
    (r"\bchill\b|\bcalm\b|\brelax(?:ed|ing)?\b|\bmellow\b|\bsoft\b|\bambient\b", "chill"),
    (r"\bupbeat\b|\benergetic\b|\bhype\b|\bpeppy\b|\bfast music\b|\bhappy\b|\bfun\b", "upbeat"),
    (r"\bcinematic\b|\bepic\b|\bdramatic\b|\bemotional\b|\borchestral\b", "cinematic"),
)

LOOK_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bteal\b|\bcinematic\b|\bblockbuster\b|\bmovie look\b|\bfilmic\b", "teal_orange.cube"),
    (r"\bwarm(?:er)?\b|\bgolden\b|\bsunset\b|\bcozy\b|\bcosy\b", "warm.cube"),
    (r"\bcool(?:er)?\b|\bcold\b|\bblue(?:ish)?\b|\bicy\b", "cool.cube"),
    (r"\bpunch(?:y|ier)?\b|\bvivid\b|\bsaturated\b|\bpoppy colou?rs?\b|\bcolou?r pop\b", "punch.cube"),
    (r"\bfaded\b|\bfilm\b|\bvintage\b|\bretro\b|\bmatte\b|\bnostalgic\b", "faded.cube"),
    (r"\bb\s*[&and]*\s*w\b|\bblack and white\b|\bmono(?:chrome)?\b|\bgr[ae]yscale\b", "mono.cube"),
)

TRANSITION_LOOK_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bpunchy\b|\bwhip\b|\bfast transitions?\b|\bsnappy\b|\bzoom transitions?\b", "punchy"),
    (r"\bcinematic\b|\bfade to black\b|\bslow transitions?\b|\bdramatic\b", "cinematic"),
    (r"\bclean\b|\bsimple\b|\bsubtle\b|\bplain\b|\bminimal\b", "clean"),
    (r"\bsmooth\b|\bdissolve\b|\bcross ?fade\b|\bsoft\b|\bgentle\b", "smooth"),
)

#: Clauses that are ABOUT transitions — the only place a transition name or
#: seam reference is read, so "zoom in on the product" stays a reframe
#: request and "cut the intro" stays a trim.
TRANSITION_CONTEXT_RE = re.compile(
    r"\btransitions?\b|\bbetween (?:the |every |each |all (?:the )?)?(?:clips?|cuts?|scenes?|shots?|clip changes?)\b"
    r"|\b(?:at|on) (?:the |every |each |all (?:the )?)?(?:first |last |final |next |opening |closing )?(?:cuts?|seams?|hook|clip changes?)\b|\bcross ?(?:fade|dissolve)\b"
    r"|\b(?:wipe|whip|glitch|dissolve|pixelat\w*|mosaic|spiral)\b|\b(?:dip|fade) to (?:black|white)\b"
    r"|\bat the (?:end|start|beginning|hook|top|last|close)\b|\bafter the (?:first|opening|intro) (?:clip|shot|scene)\b")

#: Where a transition goes (§2.4 `transitions.at`): a seam index word or a
#: time. Checked before `at_start`/`at_end` so "at the hook" (which is not
#: a title position) still reads as the first seam.
TRANSITION_AT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(?:between|on|at) (?:every|each|all(?: the)?|all of the) (?:clips?|cuts?|seams?|scenes?|shots?|edits?|clip changes?)\b"
     r"|\bbetween (?:the )?(?:clips|cuts|scenes|shots)\b|\bevery (?:cut|seam|clip)\b|\ball (?:the )?(?:cuts|seams)\b|\beverywhere\b", "all"),
    (r"\bat the (?:hook|start|beginning|open(?:ing)?|top)\b|\b(?:on|at) the first (?:cut|seam|edit|boundary|clip change)\b"
     r"|\bfirst (?:cut|seam|transition)\b|\bafter the (?:first|opening|intro) (?:clip|shot|scene)\b", "first"),
    (r"\bat the (?:end|close|outro|last)\b|\b(?:on|at) the (?:last|final) (?:cut|seam|edit|boundary|clip change)\b"
     r"|\b(?:last|final) (?:cut|seam|transition)\b|\bbefore the (?:last|final|outro) (?:clip|shot|scene)\b", "last"),
)
TRANSITION_AT_TIME_RE = re.compile(r"\bat (?:(\d{1,2}):(\d{2})|(\d+(?:\.\d+)?)\s*(?:s|sec|secs|second|seconds))\b")

#: Spoken names that are not catalog names or labels. Everything else
#: (catalog names, aliases, "Fade to Black"-style labels) is generated in
#: `_transition_name_patterns` from the renderer and the catalog, so a new
#: look needs no code here.
TRANSITION_SYNONYMS: tuple[tuple[str, str], ...] = (
    (r"\b(?:dip|fade|cut) to black\b|\bblack dip\b", "fadeblack"),
    (r"\b(?:dip|fade|flash) to white\b|\bwhite flash\b", "fadewhite"),
    (r"\bcross ?dissolve\b", "dissolve"),
    (r"\bcross ?fade\b", "fade"),
    (r"\bwhip ?pans?\b", "whip"),
    (r"\bzoom ?(?:in|punch)?\b|\bpunch ?zoom\b", "zoomin"),
    (r"\bpixel(?:ate|ated|ation|ise|ize)\b|\bmosaic\b", "pixelize"),
    (r"\bcheckerboard\b|\bchequerboard\b|\bchecker\b", "checker"),
    (r"\bfilm ?burn\b|\blight leak\b", "burn"),
    (r"\bclock ?wipe\b|\bclock\b", "radial"),
    (r"\biris\b|\bcircle (?:open|in)\b", "circleopen"),
    (r"\bspin\b|\bswirl\b|\brotat\w*\b", "spiral"),
    (r"\bglitch\w*\b", "glitch"),
    (r"\bblur\w*\b", "hblur"),
    (r"\bwipe\b", "wiperight"),
    (r"\bslide\b|\bpush\b", "slideleft"),
    (r"\bdissolve\b", "dissolve"),
    (r"\bfade\b", "fade"),
)
_TRANSITION_NAME_RE: list[tuple[re.Pattern, str]] | None = None
_TRANSITION_SUFFIXES = ("left", "right", "up", "down", "in", "out", "open", "close", "black", "white",
                        "fast", "slow", "grays", "crop", "tl", "tr", "bl", "br")
#: Accepted names that are also ordinary look words — never read as a type.
_TRANSITION_NAME_SKIP = frozenset({"smooth", "distance", "cover", "reveal", "wind", "luma", "flash"})


def _transition_name_patterns() -> list[tuple[re.Pattern, str]]:
    """(regex, canonical name) for every catalog label, canonical name and
    alias, longest first. Built once, lazily: the catalog is a preset file."""
    global _TRANSITION_NAME_RE
    if _TRANSITION_NAME_RE is not None:
        return _TRANSITION_NAME_RE
    from ...render.transitions import ALIASES, CUSTOM_EXPRS, NATIVE, canonical
    rows: list[tuple[str, str]] = []
    try:
        from .presets import transition_catalog
        for name, entry in transition_catalog().items():
            label = re.sub(r"[()]", "", entry.label.lower()).strip()
            rows.append((r"\b" + r"[- ]?".join(map(re.escape, re.split(r"[-\s]+", label))) + r"\b", name))
    except Exception:
        pass
    for name in sorted(set(NATIVE) | set(CUSTOM_EXPRS) | set(ALIASES)):
        if name in _TRANSITION_NAME_SKIP:
            continue
        pats = [re.escape(name)]
        for suf in _TRANSITION_SUFFIXES:
            if name.endswith(suf) and len(name) > len(suf) + 2:
                pats.append(re.escape(name[:-len(suf)]) + r" ?" + suf)
        rows.append((r"\b(?:" + "|".join(pats) + r")\b", canonical(name)))
    rows.sort(key=lambda r: -len(r[0]))
    _TRANSITION_NAME_RE = [(re.compile(pat), name) for pat, name in rows]
    return _TRANSITION_NAME_RE


def transition_type_of(text: str) -> str | None:
    """The canonical transition look a phrase names ("Fade to Black", "whip
    pan", "zoomin", "smooth zoom" → fadeblack / whip / zoomin / zoomin), or
    None. Used by the slot extractor and by `recipes.normalize_slots` for a
    model's free-text `type`."""
    t = normalize(text)
    for rx, name in _transition_name_patterns():
        if rx.search(t):
            return name
    for pat, name in TRANSITION_SYNONYMS:
        if re.search(pat, t):
            return name
    return None


def transition_at_of(text: str) -> str | float | None:
    """`"first"` / `"last"` / `"all"` / seconds, or None when the clause
    does not say where the transition goes."""
    t = normalize(text)
    m = TRANSITION_AT_TIME_RE.search(t)
    if m:
        return float(m.group(1)) * 60 + float(m.group(2)) if m.group(1) else float(m.group(3))
    for pat, where in TRANSITION_AT_PATTERNS:
        if re.search(pat, t):
            return where
    return None


VOICE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bbritish\b|\buk\b|\bengland\b|\benglish accent\b", "en_GB-alan-medium"),
    (r"\b(?:indian|hindi)\s+(?:male|man|guy)\b|\bmale\s+(?:indian|hindi)\b", "hi_IN-pratham-medium"),
    (r"\b(?:indian|hindi)\b", "hi_IN-priyamvada-medium"),
    (r"\bmale\b|\bman'?s?\b|\bguy\b|\bmasculine\b|\bdeep voice\b", "en_US-ryan-medium"),
    (r"\bfemale\b|\bwoman'?s?\b|\blady\b|\bgirl\b|\bfeminine\b", "en_US-amy-medium"),
)

WORD_NUMBERS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "a couple of": 2, "a couple": 2, "a few": 3, "several": 3,
    "half a dozen": 6, "a dozen": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
    "forty-five": 45, "forty five": 45, "fifty": 50, "sixty": 60, "ninety": 90,
}
_NUM_WORD = "|".join(re.escape(k) for k in sorted(WORD_NUMBERS, key=len, reverse=True))

_UNIT_S = r"(?:s|sec|secs|second|seconds)"
_UNIT_M = r"(?:m|min|mins|minute|minutes)"
_NUM = r"(\d+(?:\.\d+)?)"

DURATION_RE = re.compile(
    rf"(?P<q>under|below|max(?:imum)?|at most|no (?:more|longer) than|less than|within|"
    rf"around|about|approx(?:imately)?|roughly|~|exactly|to|of|for)?\s*"
    rf"(?:(?P<n>\d+(?:\.\d+)?)|(?P<w>{_NUM_WORD}))\s*(?P<u>{_UNIT_S}|{_UNIT_M})\b")
HALF_MINUTE_RE = re.compile(r"\bhalf a minute\b")
A_MINUTE_RE = re.compile(r"\b(?:a|one) minute\b")
MMSS_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")

# Lookbehinds must be fixed-width in `re`, so the two spellings of YouTube
# get one each; `youtube shorts` is a platform, never a count.
COUNT_RE = re.compile(
    rf"(?<!youtube )(?<!you tube )(?<!yt )\b(?:{_NUM}|(?P<w>{_NUM_WORD}))\s+(?:(?:short|vertical|quick|separate)\s+)?"
    rf"(?P<noun>shorts?|clips?|highlights?|reels?|parts?|pieces?|segments?|cuts?|videos?)\b")

UPSCALE_RE = re.compile(r"\b(2|4)\s*x\s*(?:upscale|upscaling|resolution|res)\b|\bupscal\w*\s*(?:to|by|at)?\s*(2|4)\s*x\b")
SPEED_X_RE = re.compile(rf"{_NUM}\s*x(?![\dx:])\b")
SPEED_PCT_RE = re.compile(r"\b(\d{2,3})\s*(?:%|percent)\s*(?:speed|faster)?\b")
SPEED_WORD_PATTERNS: tuple[tuple[str, float], ...] = (
    (r"\bslow[- ]?mo(?:tion)?\b|\bslowmo\b", 0.5),
    (r"\bhalf speed\b|\bhalf the speed\b|\bhalve the speed\b", 0.5),
    (r"\bdouble speed\b|\bdouble the speed\b|\btwice as fast\b|\btwice the speed\b", 2.0),
    (r"\bquarter speed\b", 0.25),
    (r"\bspeed (?:it |this |the video |the clip )?up\b|\bfaster\b|\bquicker\b|\bspeed up\b", 1.25),
    (r"\bslow (?:it |this |the video |the clip )?down\b|\bslower\b", 0.8),
)

LUFS_RE = re.compile(r"(-?\d{1,2}(?:\.\d+)?)\s*lufs\b")
COLOR_HEX_RE = re.compile(r"#([0-9a-f]{6}|[0-9a-f]{3})\b")
HANDLE_RE = re.compile(r"(?<![\w.])@([a-z0-9_](?:[a-z0-9_.]{0,28}[a-z0-9_])?)")
HASHTAG_RE = re.compile(r"(?<![\w&])#([a-z0-9_]{2,40})\b")
QUOTED_RE = re.compile(r"[\"“”']([^\"“”']{1,200})[\"“”']")
NAME_RE = re.compile(r"\b(?:for|named|called|by|of|introduce|introducing|says)\s+((?:[A-Z][\w'’.-]*\s?){1,3})")
CLIP_ID_RE = re.compile(r"\b(c_[0-9a-f]{6,12})\b")
RANGE_ABS_RE = re.compile(
    rf"(?:from|between)\s+(?:(\d{{1,2}}):(\d{{2}})|{_NUM}\s*(?:{_UNIT_S}|{_UNIT_M})?)\s*(?:to|-|–|and|until|till)\s*"
    rf"(?:(\d{{1,2}}):(\d{{2}})|{_NUM}\s*(?:{_UNIT_S}|{_UNIT_M})?)\b")
RANGE_FIRST_RE = re.compile(rf"\b(?:the\s+)?first\s+(?:{_NUM}|(?P<w>{_NUM_WORD}))\s*(?P<u>{_UNIT_S}|{_UNIT_M})\b")
RANGE_LAST_RE = re.compile(rf"\b(?:the\s+)?last\s+(?:{_NUM}|(?P<w>{_NUM_WORD}))\s*(?P<u>{_UNIT_S}|{_UNIT_M})\b")
FILLER_LIST_RE = re.compile(r"\b(ums?|uhs?|umms?|uhhs?|erms?|hmms?|likes?|you knows?|so basically|basically|actually|literally)\b")

CLIP_REF_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(?:this|the selected|selected|that|current) clip\b|\bselection\b|\bwhat i selected\b", "$selected"),
    (r"\b(?:the )?first clip\b|\bopening clip\b|\bintro clip\b", "$v1_first"),
    (r"\b(?:the )?last clip\b|\bfinal clip\b|\bclosing clip\b|\boutro clip\b", "$v1_last"),
    (r"\bat the playhead\b|\bfrom here\b|\bright here\b|\bat the cursor\b", "$playhead"),
    (r"\b(?:all|every|each) clips?\b|\bwhole (?:video|timeline|thing)\b|\bentire (?:video|timeline)\b|\beverything\b", "$v1_all"),
)


# --------------------------------------------------------------------------
# 3. Result
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TimeRange:
    kind: Literal["first", "last", "abs"]
    start: float | None = None
    end: float | None = None

    def resolve(self, duration: float) -> tuple[float, float]:
        if self.kind == "first":
            return 0.0, min(float(self.end or 0.0), duration)
        if self.kind == "last":
            n = float(self.end or 0.0)
            return max(0.0, duration - n), duration
        return max(0.0, float(self.start or 0.0)), min(float(self.end or duration), duration)


@dataclass(frozen=True)
class Slots:
    duration_s: float | None = None
    duration_qualifier: Literal["max", "target", "exact"] | None = None
    count: int | None = None
    platform: str | None = None
    ratio: str | None = None
    upscale_factor: int | None = None
    speed: float | None = None
    language: str | None = None
    caption_style: str | None = None
    caption_position: str | None = None
    model_upgrade: bool = False
    mood: str | None = None
    look: str | None = None
    transition_look: str | None = None
    transition_type: str | None = None       # canonical render/transitions name
    transition_at: str | float | None = None  # "first" | "last" | "all" | seconds
    color_hex: str | None = None
    handle: str | None = None
    hashtags: tuple[str, ...] = ()
    name: str | None = None
    quoted_text: tuple[str, ...] = ()
    clip_ref: str | None = None
    range: TimeRange | None = None
    filler_words: tuple[str, ...] = ()
    lufs: float | None = None
    voice: str | None = None
    at_start: bool = False
    at_end: bool = False
    smooth: bool = False
    replace_existing: bool = False

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for k, v in self.__dict__.items():
            if v in (None, (), False):
                continue
            d[k] = v
        return d


# --------------------------------------------------------------------------
# 4. Extraction
# --------------------------------------------------------------------------

def _first(patterns: tuple[tuple[str, str], ...], text: str) -> str | None:
    for pat, value in patterns:
        if re.search(pat, text):
            return value
    return None


def _mask(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for a, b in spans:
        for i in range(a, b):
            chars[i] = " "
    return "".join(chars)


def _number(m: re.Match, num_group: int | str = 1, word_group: str = "w") -> float | None:
    """The numeric value of a match that captured either digits (`num_group`)
    or a number word (`word_group`)."""
    raw = m.group(num_group)
    if raw:
        return float(raw)
    w = m.groupdict().get(word_group)
    if w and w in WORD_NUMBERS:
        return float(WORD_NUMBERS[w])
    return None


def parse_duration(text: str) -> tuple[float, str | None] | None:
    """(seconds, qualifier) from free text — also used by the clarification
    parser for `duration` questions. `half a minute` = 30, `a minute` = 60."""
    t = normalize(text)
    if HALF_MINUTE_RE.search(t):
        return 30.0, None
    if A_MINUTE_RE.search(t):
        return 60.0, None
    m = DURATION_RE.search(t)
    if not m:
        mm = MMSS_RE.search(t)
        if mm and not RANGE_ABS_RE.search(t):
            return float(mm.group(1)) * 60 + float(mm.group(2)), None
        return None
    n = _number(m, "n", "w")
    if n is None:
        return None
    secs = n * 60 if re.fullmatch(_UNIT_M, m.group("u")) else n
    q = (m.group("q") or "").strip()
    qual: str | None = None
    if q in ("under", "below", "max", "maximum", "at most", "no more than", "no longer than",
             "less than", "within"):
        qual = "max"
    elif q in ("around", "about", "approx", "approximately", "roughly", "~"):
        qual = "target"
    elif q == "exactly":
        qual = "exact"
    return secs, qual


def parse_number(text: str) -> float | None:
    """A bare number or number word — for `number` clarification answers."""
    t = normalize(text)
    m = re.fullmatch(r"-?\d+(?:\.\d+)?", t)
    if m:
        return float(t)
    if t in WORD_NUMBERS:
        return float(WORD_NUMBERS[t])
    return None


def extract(prompt: str) -> Slots:
    """Every slot the prompt states. Never guesses defaults — that is the
    recipe table's job, where the defaults can see the facts."""
    original = prompt
    t = normalize(prompt)
    masked_spans: list[tuple[int, int]] = []

    # --- quoted text first: it is opaque to every other extractor ---------
    quoted = tuple(q.strip() for q in QUOTED_RE.findall(original) if q.strip())
    t_noq = QUOTED_RE.sub(lambda m: " " * len(m.group(0)), t)

    # --- platform / ratio / upscale / count BEFORE speed -----------------
    platform = _first(PLATFORM_PATTERNS, t_noq)
    ratio = None
    for pat, value in RATIO_PATTERNS:
        m = re.search(pat, t_noq)
        if m:
            ratio = value
            masked_spans.append(m.span())
            break
    if ratio is None and platform is not None and re.search(
            r"\b(reframe|vertical|portrait|landscape|square|aspect|resize|crop|format|fit|ratio|make it|for|turn|convert|into)\b", t_noq):
        ratio = PLATFORM_RATIO[platform]

    upscale_factor = None
    m = UPSCALE_RE.search(t_noq)
    if m:
        upscale_factor = int(m.group(1) or m.group(2))
        masked_spans.append(m.span())

    count = None
    m = COUNT_RE.search(t_noq)
    if m:
        n = _number(m, 1, "w")
        if n is not None:
            count = int(n)
            masked_spans.append(m.span())
            # "make 2 shorts": the noun is the count's unit, not YouTube
            # Shorts — a platform here would switch `shorts.finish` on.
            if (platform == "shorts" and m.group("noun").startswith("short")
                    and not re.search(r"\byou ?tube\b|\byt\b", t_noq)):
                # "make 3 shorts for tiktok": the platform is whatever is left
                # once the "3 shorts" noun phrase is masked out.
                platform = _first(PLATFORM_PATTERNS, _mask(t_noq, [m.span()]))
                if ratio == PLATFORM_RATIO["shorts"] and not any(
                        re.search(pat, t_noq) for pat, _ in RATIO_PATTERNS):
                    ratio = None

    duration_s = None
    duration_q: str | None = None
    for rx in (RANGE_FIRST_RE, RANGE_LAST_RE, RANGE_ABS_RE):
        for mm in rx.finditer(t_noq):
            masked_spans.append(mm.span())
    # "at 0:12" / "at 5 seconds" is a POSITION, never a length — a transition
    # or title placed there must not inherit it as `duration_s`.
    for mm in TRANSITION_AT_TIME_RE.finditer(t_noq):
        masked_spans.append(mm.span())
    t_nodur = _mask(t_noq, masked_spans)
    d = parse_duration(t_nodur)
    if d is not None:
        duration_s, duration_q = d
        dm = DURATION_RE.search(t_nodur)
        if dm:
            masked_spans.append(dm.span())

    # --- speed on the masked text ----------------------------------------
    t_speed = _mask(t_noq, masked_spans)
    speed = None
    m = SPEED_X_RE.search(t_speed)
    if m:
        speed = float(m.group(1))
    if speed is None:
        m = SPEED_PCT_RE.search(t_speed)
        if m and re.search(r"\bspeed|fast|slow", t_speed):
            speed = float(m.group(1)) / 100.0
    if speed is None:
        for pat, value in SPEED_WORD_PATTERNS:
            if re.search(pat, t_speed):
                speed = value
                break

    # --- language ---------------------------------------------------------
    language = None
    if re.search(r"[ऀ-ॿ]", original):
        language = "hi"
    else:
        language = _first(LANGUAGE_PATTERNS, t_noq)

    # --- captions ---------------------------------------------------------
    caption_style = _first(CAPTION_STYLE_PATTERNS, t_noq)
    caption_position = None
    if re.search(r"\bcaptions?\b|\bsubtitles?\b|\btext\b|\btitle\b|\blower[- ]third\b", t_noq):
        caption_position = _first(CAPTION_POSITION_PATTERNS, t_noq)
    model_upgrade = bool(MODEL_UPGRADE_RE.search(t_noq))

    # --- looks, moods, voices --------------------------------------------
    mood = _first(MOOD_PATTERNS, t_noq) if re.search(
        r"\bmusic\b|\btrack\b|\bbed\b|\bsong\b|\bbeat\b|\bsoundtrack\b|\bbgm\b|\bbackground\b", t_noq) else None
    look = None
    if re.search(r"\blook\b|\bgrade\b|\blut\b|\bcolou?rs?\b|\bfilter\b|\btone\b|\bvibe\b|\bfeel\b|\bmake it (?:warm|cool|cold|cinematic|punchy|vivid|faded|vintage|retro|black and white|mono)", t_noq):
        look = _first(LOOK_PATTERNS, t_noq)
    transition_look = None
    transition_type = None
    transition_at: str | float | None = None
    if TRANSITION_CONTEXT_RE.search(t_noq):
        transition_look = _first(TRANSITION_LOOK_PATTERNS, t_noq)
        transition_type = transition_type_of(t_noq)
        transition_at = transition_at_of(t_noq)
    voice = None
    if re.search(r"\bvoice\b|\bvoice ?over\b|\bnarrat\w*\b|\bsay(?:ing|s)?\b|\bread\b|\btts\b|\bspeak\b", t_noq):
        voice = _first(VOICE_PATTERNS, t_noq)

    # --- identifiers -----------------------------------------------------
    color_hex = None
    m = COLOR_HEX_RE.search(t)
    if m:
        color_hex = "#" + m.group(1).upper()
    handle = None
    m = HANDLE_RE.search(t)
    if m:
        handle = "@" + m.group(1)
    hashtags = tuple("#" + h for h in HASHTAG_RE.findall(t))
    name = None
    m = NAME_RE.search(original)
    if m:
        cand = " ".join(w.strip(".,'’") for w in m.group(1).split())
        if cand and not cand.lower().startswith(("the ", "a ", "my ")) and cand.lower() not in {
                "tiktok", "reels", "instagram", "youtube", "shorts", "facebook", "linkedin", "twitter", "hindi", "english"}:
            name = cand.strip()
    if name is None and quoted and re.search(r"\blower[- ]third\b|\bname\b|\btitle\b", t):
        name = quoted[0]

    clip_ref = None
    m = CLIP_ID_RE.search(t)
    if m:
        clip_ref = m.group(1)
    else:
        clip_ref = _first(CLIP_REF_PATTERNS, t_noq)

    rng: TimeRange | None = None
    m = RANGE_ABS_RE.search(t_noq)
    if m:
        a = float(m.group(1)) * 60 + float(m.group(2)) if m.group(1) else float(m.group(3))
        b = float(m.group(4)) * 60 + float(m.group(5)) if m.group(4) else float(m.group(6))
        if b > a:
            rng = TimeRange(kind="abs", start=a, end=b)
    if rng is None:
        m = RANGE_FIRST_RE.search(t_noq)
        if m:
            n = _number(m, 1, "w")
            if n is not None:
                rng = TimeRange(kind="first", end=n * 60 if re.fullmatch(_UNIT_M, m.group("u")) else n)
    if rng is None:
        m = RANGE_LAST_RE.search(t_noq)
        if m:
            n = _number(m, 1, "w")
            if n is not None:
                rng = TimeRange(kind="last", end=n * 60 if re.fullmatch(_UNIT_M, m.group("u")) else n)

    filler_words: tuple[str, ...] = ()
    if re.search(r"\bfiller|\bums?\b|\buhs?\b|\bumms?\b|\bstutter|\bhesitation", t_noq) or (
            quoted and re.search(r"\bremove|\bcut|\bdelete|\bstrip", t)):
        found = [w.rstrip("s") if w in ("ums", "uhs", "umms", "uhhs", "erms", "hmms") else w
                 for w in FILLER_LIST_RE.findall(t_noq)]
        found += [q.lower() for q in quoted if len(q.split()) <= 3]
        seen: list[str] = []
        for w in found:
            if w not in seen and w not in ("like", "likes", "actually", "literally", "basically"):
                seen.append(w)
        # unquoted `like`/`you know`/`basically` are content words (§2.4)
        quoted_lower = {q.lower() for q in quoted}
        seen += [w for w in ("like", "you know", "so basically", "basically", "actually", "literally")
                 if w in quoted_lower and w not in seen]
        filler_words = tuple(seen)

    lufs = None
    m = LUFS_RE.search(t)
    if m:
        lufs = float(m.group(1))
        if lufs > 0:
            lufs = -lufs

    at_start = bool(re.search(r"\bat the (?:start|beginning|top|open(?:ing)?)\b|\bin the first\b|\bintro\b|\bopening\b", t_noq))
    at_end = bool(re.search(r"\bat the end\b|\bat the close\b|\boutro\b|\bending\b|\bfinal seconds\b|\bto finish\b|\bclosing\b", t_noq))
    smooth = bool(re.search(r"\bsmooth\b|\binterpolat\w*\b|\bbuttery\b", t_noq))
    replace_existing = bool(re.search(r"\banother\b|\breplace\b|\bdifferent\b|\bswap\b|\bchange the music\b|\bnew music\b|\bnew track\b", t_noq))

    return Slots(
        duration_s=duration_s, duration_qualifier=duration_q, count=count, platform=platform,
        ratio=ratio, upscale_factor=upscale_factor, speed=speed, language=language,
        caption_style=caption_style, caption_position=caption_position, model_upgrade=model_upgrade,
        mood=mood, look=look, transition_look=transition_look, transition_type=transition_type,
        transition_at=transition_at, color_hex=color_hex, handle=handle,
        hashtags=hashtags, name=name, quoted_text=quoted, clip_ref=clip_ref, range=rng,
        filler_words=filler_words, lufs=lufs, voice=voice, at_start=at_start, at_end=at_end,
        smooth=smooth, replace_existing=replace_existing,
    )


def default_lufs(platform: str | None, facts_lufs: float | None) -> float:
    """§2.2 `lufs`: explicit beats platform beats the timeline's current target
    beats −16. Explicit values are handled by the caller (they are in Slots)."""
    if platform and platform in PLATFORM_LUFS:
        return PLATFORM_LUFS[platform]
    if facts_lufs is not None:
        return float(facts_lufs)
    return -16.0


def merge(base: Slots, override: Slots) -> Slots:
    """`override` wins wherever it states a value; tuples concatenate."""
    changes: dict[str, Any] = {}
    for k, v in override.__dict__.items():
        if isinstance(v, tuple):
            merged = tuple(dict.fromkeys(base.__dict__[k] + v))
            if merged != base.__dict__[k]:
                changes[k] = merged
        elif v not in (None, False):
            changes[k] = v
    return replace(base, **changes) if changes else base


__all__ = ["HINGLISH_VERBS", "normalize", "PLATFORM_PATTERNS", "PLATFORM_RATIO", "PLATFORM_LUFS",
           "PLATFORM_SHORT_MAX_S", "WORD_NUMBERS", "TimeRange", "Slots", "extract",
           "parse_duration", "parse_number", "default_lufs", "merge",
           "TRANSITION_CONTEXT_RE", "TRANSITION_SYNONYMS", "transition_type_of", "transition_at_of"]
