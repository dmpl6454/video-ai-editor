"""Shared prompt semantics — ONE reading of direction, amount, scope,
pronouns and time, used by the grammar / slots, the planner, the expanders,
the on-device brain adapters (`brains/content.ground_semantics`) and the
executor's contract net (`contract.py`).

Why one module: three Final-QA sweep rounds each found 5-8 NEW wrong edits
from new phrasings, and every one was the same few questions answered
differently in different places — "reduce the speed" was a speed-UP in
slots (no row for it, so the recipe's 1.25x default), "slow the footage
down" read as 1.25x, "mute clip 2 and slow it down" slowed every clip
(the pronoun was bound in one reader and not another), "keep only the first
10 seconds" was the range to CUT. A reader that each call site re-derives
drifts; a table here is answered once and tested once
(tests/test_k3_semantics.py).

Pure functions over text (no facts, no I/O) and no imports from the other
prompt modules, so grammar/slots/planner can all import it without a cycle.

The vocabulary:
  * DIRECTION per axis — speed (faster/slower), level (louder/quieter),
    size (bigger/smaller), length (longer/shorter), brightness, contrast,
    saturation, zoom — as "up" / "down" / "reset", or "both" when a clause
    says both ways (that clause must ASK);
  * AMOUNT — relative vs absolute: "by 6 dB" is a change, "to -6 dB" a
    level; "50% faster" is 1.5x, "25% slower" 0.75x, "3 times faster" 3x,
    "half speed" 0.5x, "normal/reset" 1x; a direction that contradicts its
    own absolute target ("slow it down to 2x") is `ambiguous`;
  * SCOPE — the clips a clause names (several: "clips 1 and 2", "the first
    and last clips"), "only/just", "everything / all clips / the whole
    video", "except …", "this / the selected clip", "it / them" (carried
    from the clause before by `resolve_scopes`), and the MEDIA it is about
    (the main lane, the music, the voice, captions, a text, the overlay);
  * TIME — "the first/last N seconds", "at 1:20", "from 2 to 4", "after 9s",
    "before 3s";
  * KEEP — "keep only X" / "just keep X" deletes everything BUT X.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal

Direction = Literal["up", "down", "reset", "both"]
Axis = Literal["speed", "level", "size", "length", "brightness", "contrast", "saturation", "zoom"]

# --------------------------------------------------------------------------
# 0. Normalisation (the subset of slots.normalize this module needs; slots
#    imports semantics, not the other way round)
# --------------------------------------------------------------------------

_WS = re.compile(r"\s+")
_WORD_NUM = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
             "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "a": 1, "an": 1, "half a": 0.5, "half": 0.5}
_HINGLISH = (("dheere karo", "slow down"), ("dheema karo", "slow down"), ("slow karo", "slow down"),
             ("tez karo", "speed up"), ("fast karo", "speed up"), ("thoda kam karo", "a little lower"),
             ("kam karo", "lower"), ("zyada karo", "louder"), ("jyada karo", "louder"), ("kar do", "do"))


#: Slips editors actually type (the K3 corpus), fixed before anything is
#: read — by the contract as much as by the planner, so both read the same.
TYPOS: dict[str, str] = {
    "teh": "the", "hte": "the", "mkae": "make", "amke": "make", "maek": "make", "spead": "speed", "speeed": "speed",
    "sped": "speed", "slwo": "slow", "slwoer": "slower", "solw": "slow", "fsater": "faster", "fatser": "faster",
    "secnd": "second", "seond": "second", "frist": "first", "fisrt": "first", "thrid": "third", "lsat": "last",
    "clpi": "clip", "cilp": "clip", "clp": "clip", "clips1": "clip 1", "delte": "delete", "delet": "delete",
    "deleet": "delete", "remvoe": "remove", "reomve": "remove", "tunr": "turn", "trun": "turn", "musci": "music",
    "muisc": "music", "msuic": "music", "volumn": "volume", "voluem": "volume", "blak": "black", "balck": "black",
    "whit": "white", "wite": "white", "captoins": "captions", "captoin": "caption", "captiosn": "captions",
    "subtitels": "subtitles", "biger": "bigger", "smaler": "smaller", "abit": "a bit", "alittle": "a little",
    "vidoe": "video", "viedo": "video", "transtion": "transition", "transitons": "transitions", "rotaet": "rotate",
    "reverese": "reverse", "revrse": "reverse", "revers": "reverse", "mtue": "mute", "muet": "mute", "fdae": "fade",
    "splti": "split", "zoon": "zoom", "zom": "zoom", "titel": "title", "tittle": "title", "nr": "number",
    "bigr": "bigger", "biggr": "bigger", "smallr": "smaller", "fad": "fade",
}
_TYPO_RE = re.compile(r"\b(" + "|".join(sorted(map(re.escape, TYPOS), key=len, reverse=True)) + r")\b")


def fix_typos(text: str) -> str:
    return _TYPO_RE.sub(lambda m: TYPOS[m.group(1)], (text or "").lower())


def norm(text: str) -> str:
    t = (text or "").replace("’", "'").replace("“", '"').replace("”", '"').replace("&", " and ")
    t = _WS.sub(" ", t).strip().lower().rstrip(" .!?;,")
    t = fix_typos(t)
    for a, b in _HINGLISH:
        t = re.sub(rf"\b{a}\b", b, t)
    # "3db" / "2dB": a glued unit has no word boundary before "db", so every
    # `\bdb\b` reader missed it and "turn it up 3db" was dropped
    t = _GLUED_DB_RE.sub(r"\1 \2", t)
    # final sweep 3 r2: "clip2 1.5x" / "clip3 black and white" named no clip
    # (no space), so the change widened to every clip
    t = GLUED_CLIP_RE.sub(r"\1 \2", t)
    return t


_GLUED_DB_RE = re.compile(r"(\d)(db|decibels?)\b")
GLUED_CLIP_RE = re.compile(r"\b(clips?|shots?|scenes?)(\d{1,2})\b")
#: "except the first", "but the second", "but the last one": a bare ordinal
#: in an except tail is a clip (final sweep 3 r2 — "all clips except the
#: first to 2x" sped up all three)
_EXCEPT_ORD_RE = re.compile(r"^(?:the\s+)?(first|second|third|fourth|fifth|sixth|last|final|\d{1,2}(?:st|nd|rd|th))"
                            r"(?:\s+(?:one|clip|shot))?\b(?!\s+(?:half|part|piece|seconds?|minutes?|frames?|few|couple"
                            r"|\d))")


_QUOTE_RE = re.compile(r"[\"“”']([^\"“”']{1,200})[\"“”']")


def strip_quotes(text: str) -> str:
    """Quoted words are content (a title's text), never an instruction."""
    return _QUOTE_RE.sub(" ", text)


# --------------------------------------------------------------------------
# 1. Clauses (the grammar's splitting rule, restated so this module has no
#    import of grammar; `and` inside a clip PAIR or a protected phrase joins)
# --------------------------------------------------------------------------

_PROTECTED = ("black and white", "black and whites", "b and w", "in and out", "back and forth", "ums and uhs",
              "umms and uhs", "ums and ahs", "silences and fillers", "pauses and fillers", "rock and roll",
              "loud and clear", "zoom in and out", "fade in and out", "fade in and fade out",
              "bold and yellow", "red and bigger", "yellow and bigger", "bigger and bolder",
              "second and a half", "seconds and a half", "sec and a half", "one and a half")
_ORD_WORD = (r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\d{1,2}(?:st|nd|rd|th)"
             r"|last|final|opening|closing|middle)")
_CLIP_NOUN = r"(?:clips?|shots?|segments?|scenes?|parts?|bits?)"
_NUMW = r"(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)"
_PAIR_RE = re.compile(
    rf"\b(?:the\s+)?{_ORD_WORD}\s+(?:and|&)\s+(?:the\s+)?{_ORD_WORD}(?:\s+ones?)?\b"
    rf"|\b{_CLIP_NOUN}\s+{_NUMW}\s*(?:,\s*{_NUMW}\s*)*(?:and|&)\s+(?:{_CLIP_NOUN}\s+)?{_NUMW}\b"
    rf"|\bbetween\s+(?:the\s+)?(?:{_CLIP_NOUN}\s+{_NUMW}|{_ORD_WORD}(?:\s+{_CLIP_NOUN})?)\s+(?:and|&)\s+"
    rf"(?:the\s+)?(?:{_CLIP_NOUN}\s+{_NUMW}|{_ORD_WORD}(?:\s+{_CLIP_NOUN})?)\b"
    rf"|\b{_CLIP_NOUN}\s+{_NUMW}\s+(?:and|&)\s+{_CLIP_NOUN}\s+{_NUMW}\b")
_SPLIT_RE = re.compile(r"\s*(?:,|;|:(?=\s)|\bthen\b|\band then\b|\band also\b|\band\b|\bplus\b|\bafter that\b|\balso\b)\s*")


def clauses(prompt: str) -> list[str]:
    text = norm(prompt)
    if not text:
        return []
    keep: dict[str, str] = {}

    def stash(m: re.Match) -> str:
        key = f"\x00{len(keep)}\x00"
        keep[key] = m.group(0)
        return key

    text = _QUOTE_RE.sub(stash, text)
    text = _PAIR_RE.sub(stash, text)
    for i, phrase in enumerate(_PROTECTED):
        text = text.replace(phrase, phrase.replace(" and ", f"\x01{i}\x01"))
    out: list[str] = []
    for p in _SPLIT_RE.split(text):
        if not p or not p.strip():
            continue
        for i, phrase in enumerate(_PROTECTED):
            p = p.replace(f"\x01{i}\x01", " and ")
        for k in list(keep)[::-1]:
            p = p.replace(k, keep[k])
        p = p.strip(" .,")
        if p and out and _BARE_LENGTH_RE.fullmatch(p):
            # "fade transitions everywhere, 1 sec": a bare length after a comma
            # belongs to the clause before it (the 1 s was dropped)
            out[-1] = f"{out[-1]} {p}"
            continue
        if p:
            out.append(p)
    return out


_BARE_LENGTH_RE = re.compile(r"(?:for\s+)?\d+(?:\.\d+)?\s*(?:s|sec|secs|seconds?)(?:\s+(?:each|long|apiece))?")


# --------------------------------------------------------------------------
# 2. Direction
# --------------------------------------------------------------------------

_DIR_WORDS: dict[str, dict[str, str]] = {
    "speed": {
        "up": (r"\bfaster\b|\bquicker\b|\bspeed\s+(?:[\w']+\s+){0,5}?up\b|\bspeed[- ]?up\b|\bsped\s+up\b"
               r"|\b(?:increase|raise|boost|up|bump(?:\s+up)?|more)\s+(?:the\s+|its\s+|their\s+)?(?:playback\s+)?speed\b"
               # "increase clip 1 speed by 30%" (it set 0.3x)
               r"|\b(?:increase|raise|boost|bump(?:\s+up)?)\s+(?:[\w']+\s+){1,4}?(?:playback\s+)?speed\b"
               r"|\bspeed\s+(?:increase|boost)\b|\baccelerat\w*|\bhurry\b|\bquicken\b|\bfast[- ]?forward\b"
               r"|\bpick\s+up\s+the\s+pace\b|\bdouble[- ]?speed\b|\btwice\s+as\s+fast\b"),
        "down": (r"\bslower\b|\bslow\s+(?:[\w']+\s+){0,5}?down\b|\b(?:clip|shot|it)\s+(?:\w+\s+)?(?:take\s+|last\s+|play\s+)?twice\s+as\s+long\b|\bslow[- ]?down\b|\bslowed\s+down\b"
                 r"|\bslow[- ]?mo(?:tion)?\b|\bslowmo\b|\bslo[- ]?mo\b|\bslow\b(?=\s+(?:the|this|that|it|clip|them|everything)\b)"
                 r"|\b(?:reduce|decrease|lower|drop|cut|less)\s+(?:the\s+|its\s+|their\s+)?(?:playback\s+)?speed\b"
                 r"|\b(?:reduce|decrease|lower)\s+(?:[\w']+\s+){1,4}?(?:playback\s+)?speed\b"
                 r"|\bdecelerat\w*|\bhalf[- ]?speed\b|\bhalf\s+the\s+speed\b|\bhalve\s+the\s+speed\b"
                 r"|\bquarter[- ]?speed\b"),
        "reset": (r"\b(?:normal|regular|original|real[- ]?time|default|standard|usual|natural)\s+(?:playback\s+)?speed\b"
                  r"|\breset\s+(?:the\s+|its\s+|their\s+)?(?:playback\s+)?speed\b|\bspeed\s+(?:back\s+)?to\s+normal\b"
                  r"|\bback\s+to\s+(?:1\s*x|100\s*%|normal|regular)\b"
                  r"|\b(?:remove|undo|clear|drop|take\s+(?:off|out)|get\s+rid\s+of)\s+(?:the\s+|its\s+|any\s+)?"
                  r"(?:speed\s+(?:change|ramp|curve|up|effect|adjustment)s?|slow[- ]?(?:mo(?:tion)?|down)|speed[- ]?up)\b"
                  r"|\bun-?(?:speed|slow)\b"),
    },
    "level": {
        # "clip 2 volume back to normal" went further DOWN (-6 → -12 dB): a
        # level has a normal too — the source's own, 0 dB
        "reset": (r"\b(?:volume|level|gain|sound|audio)\b.*\bback\s+to\s+(?:normal|original|default|100\s*%|0\s*db|unity)\b"
                  r"|\breset\s+(?:the\s+|its\s+|their\s+)?(?:volume|level|gain)\b"
                  r"|\b(?:normal|original|default)\s+(?:volume|level)\b|\bunity\s+gain\b"),
        "up": (r"\blouder\b|\bturn\s+(?:[\w']+\s+){0,4}?up\b|\bturn[- ]up\b|\bbring\s+(?:[\w']+\s+){0,4}?up\b"
               r"|\b(?:raise|increase|boost|lift|crank|pump)\b|\bup\s+by\b|\bhigher\b"
               r"|\btoo\s+(?:quiet|soft|low|faint|weak)\b|\bcan'?t\s+hear\b|\bmore\s+(?:volume|audible)\b|\bzyada\b"
               r"|\bup\s+(?:by\s+)?\d|\b(?:voice|vocals?|music|song|volume|audio|sound|dialogue|it)\s+(?:way\s+)?up\b"),
        "down": (r"\bquieter\b|\bsofter\b|\bturn\s+(?:[\w']+\s+){0,4}?down\b|\bturn[- ]down\b"
                 r"|\bbring\s+(?:[\w']+\s+){0,4}?down\b|\b(?:lower|reduce|decrease|drop|dim|tone\s+down|dial\s+down)\b"
                 r"|\bdown\s+by\b|\btoo\s+loud\b|\b(?:not\s+so|less)\s+loud\b|\bway\s+too\s+loud\b|\bkam\b"
                 r"|\bway\s+down\b|\bdown\s+a\s+(?:bit|little|lot|touch)\b|\bdown\s+(?:by\s+)?\d"
                 r"|\b(?:voice|vocals?|music|song|volume|audio|sound|dialogue)\s+(?:way\s+)?down\b"),
    },
    "size": {
        "up": (r"\bbigger\b|\blarger\b|\bbiger\b|\benlarge\b|\bincrease\s+(?:the\s+)?(?:font\s+)?size\b|\bsize\s+up\b"
               r"|\bgrow\b|\bhuge\b|\bmore\s+readable\b|\b(?:twice|two times|double|triple|three times)\s+as\s+(?:big|large)\b"
               r"|\bdouble\s+(?:the\s+|its\s+)?size\b"),
        "down": (r"\bsmaller\b|\btinier\b|\bshrink\b|\breduce\s+(?:the\s+)?(?:font\s+)?size\b|\bsize\s+down\b"
                 r"|\bdecrease\s+(?:the\s+)?(?:font\s+)?size\b|\bless\s+big\b|\bhalf\s+(?:the|its)\s+size\b"
                 r"|\bhalf\s+as\s+big\b"),
    },
    "length": {
        "up": r"\blonger\b|\blengthen\b|\bextend\b|\bstretch\b|\bstay\s+(?:on\s+)?(?:screen\s+)?longer\b",
        "down": r"\bshorter\b|\bshorten\b",
    },
    "brightness": {
        "up": r"\bbrighter\b|\bbrighten\b|\blighten\b|\bmore\s+(?:light|bright)\w*\b|\b(?:increase|raise|boost)\s+(?:the\s+)?brightness\b",
        "down": (r"\bdarker\b|\bdarken\b|\bdim\b|\bless\s+bright\b"
                 r"|\b(?:lower|reduce|decrease|drop)\s+(?:the\s+)?brightness\b"),
    },
    "contrast": {
        "up": r"\b(?:more|boost|increase|raise|higher|punch\s+up)\s+(?:the\s+)?contrast\b|\bcontrast\s+up\b",
        "down": r"\b(?:less|lower|reduce|decrease|drop|soften)\s+(?:the\s+)?contrast\b|\bflatten\b|\bcontrast\s+down\b",
    },
    "saturation": {
        "up": (r"\bmore\s+(?:saturat\w*|vibran\w*|colou?rful|colou?r)\b|\bvibrant\b|\bsaturate\b"
               r"|\b(?:boost|increase|raise)\s+(?:the\s+)?(?:saturation|colou?rs?)\b"),
        "down": (r"\bdesaturat\w*|\bless\s+(?:saturat\w*|colou?rful|colou?r|vibrant)\b|\bmuted\s+colou?rs?\b"
                 r"|\bwashed[- ]out\b|\b(?:lower|reduce|decrease|drop)\s+(?:the\s+)?saturation\b"),
    },
    "zoom": {
        "up": r"\bzoom(?:s|ed|ing)?\s+in\b|\bpunch(?:es|ed|ing)?[- ]?in\b|\bpush[- ]in\b|\bcloser\b|\bmore\s+zoom\b",
        "down": r"\bzoom(?:s|ed|ing)?[- ]?out\b|\bpull\s+back\b|\bwider\b|\bless\s+zoom\b",
    },
}
_DIR_RX = {axis: {d: re.compile(p) for d, p in rows.items()} for axis, rows in _DIR_WORDS.items()}


def direction(text: str, axis: str) -> Direction | None:
    """Which way `text` asks `axis` to go: "up", "down", "reset" (back to
    normal — speed only), "both" (it says both, e.g. "faster … slower"), or
    None when it names no direction for that axis."""
    t = strip_quotes(norm(text))
    rows = _DIR_RX.get(axis)
    if not rows:
        return None
    if "reset" in rows and rows["reset"].search(t):
        return "reset"
    up = bool(rows["up"].search(t))
    down = bool(rows["down"].search(t))
    if axis == "level":
        # "turn it down … louder" is two instructions; one clause saying both
        # (the grammar split them already) is the ambiguous case.
        pass
    if up and down:
        return "both"
    return "up" if up else "down" if down else None


# --------------------------------------------------------------------------
# 3. Amounts
# --------------------------------------------------------------------------

_NUM = r"(\d+(?:\.\d+)?)"
_TIMES_WORDS = {"twice": 2.0, "double": 2.0, "triple": 3.0, "thrice": 3.0, "quadruple": 4.0, "half": 0.5}


def _num_word(s: str) -> float | None:
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        return float(_WORD_NUM[s]) if s in _WORD_NUM else None


@dataclass(frozen=True)
class SpeedAsk:
    """What a clause asks of a clip's speed. `factor` is the speed to END on
    when the clause fixes it (absolute, relative-to-1x, a named multiple or a
    reset); `direction` the way it goes; `ambiguous` a reason to ASK."""
    direction: Direction | None = None
    factor: float | None = None
    relative: bool = False               # "50% faster": relative to 1x (never to the current speed)
    ambiguous: str | None = None


_X_RE = re.compile(rf"(?<![\w.]){_NUM}\s*x(?![\w:])")
_TIMES_RE = re.compile(rf"\b({_NUM[1:-1]}|one|two|three|four|five|ten)\s*(?:times|x)\s+(faster|quicker|slower|as\s+fast|the\s+speed)\b")
_TIMESW_RE = re.compile(r"\b(twice|double|triple|thrice|quadruple)\s+(?:as\s+fast|the\s+speed|speed|faster)\b|\bdouble[- ]?speed\b|\btriple[- ]?speed\b")
_AS_LONG_RE = re.compile(r"\b(twice|double|half)\s+as\s+long\b")
_NOT_CLIP_LENGTH_RE = re.compile(r"\b(?:title|text|caption|subtitle|music|song|transition|fade|freeze|hold|sticker"
                                 r"|heading|label|video)s?\b")
_HALF_RE = re.compile(r"\bhalf[- ]?speed\b|\bhalf\s+(?:the\s+speed|as\s+fast)\b|\bhalve\s+the\s+speed\b|\bslow[- ]?mo(?:tion)?\b|\bslo[- ]?mo\b|\bslowmo\b")
_QUARTER_RE = re.compile(r"(?<!three\s)(?<!three-)(?<!3\s)(?<!3-)\bquarter[- ]?speed\b")
#: Speeds said as fractions (final sweep 2 r2: "three quarter speed" read as
#: "quarter speed", 0.25x; "by a third" fell to the 0.8x / 1.25x defaults).
_FRAC_NUM = {"a": 1, "one": 1, "1": 1, "two": 2, "2": 2, "three": 3, "3": 3}
_FRAC_DEN = {"third": 3, "thirds": 3, "quarter": 4, "quarters": 4}
#: "two thirds speed", "three quarters of the speed", "3/4 speed" — the speed itself
_FRAC_ABS_RE = re.compile(r"\b(a|one|two|three|3|2|1)[- ](thirds?|quarters?)\s+(?:of\s+(?:the\s+|its\s+|their\s+)?)?"
                          r"(?:normal\s+|original\s+)?(?:playback\s+)?speed\b|\b([123])\s*/\s*([34])\s+(?:of\s+(?:the\s+)?)?speed\b")
#: "by a third", "a third faster", "two thirds slower" — a change from 1x
_FRAC_REL_RE = re.compile(r"\b(a|one|two|three)[- ](thirds?|quarters?)\b"
                          r"(?!\s+(?:of\s+(?:the\s+|its\s+|their\s+)?)?(?:normal\s+|original\s+)?(?:playback\s+)?speed)"
                          r"(?!\s+(?:of\s+(?:a\s+)?)?(?:second|sec|secs|minute|min|clip|video|way|shot|part)\b)")
_PCT_RE = re.compile(rf"(?<![\w.]){_NUM}\s*(?:%|percent\b)")
_SPEED_NUM_RE = re.compile(r"\b(?:at|to)\s+(?:like\s+|about\s+|around\s+|roughly\s+)?(\d+(?:\.\d+)?)\s*x?\s+speed\b"
                           r"|\bspeed\s*(?:=|:|of|at|to|is)?\s*(\d+(?:\.\d+)?)\s*x?\b"
                           r"(?!\s*(?:%|s\b|sec|secs\b|seconds?\b|db\b|times\b|fps\b))")


def speed_ask(clause: str) -> SpeedAsk:
    """The speed `clause` asks for — see `SpeedAsk`. Pure over the clause.

    Rules (each one a sweep-round finding):
      * "reset / normal / regular speed / back to 1x" → 1.0;
      * "N times faster" → N, "N times slower" → 1/N, "twice as fast" → 2;
      * "half speed / slow motion" → 0.5, "quarter speed" → 0.25;
      * "N% faster" / "speed up by N%" → 1 + N/100; "N% slower" / "slow
        down by N%" → 1 - N/100 (≥ 100% slower asks); "at/to N% speed" → N/100;
      * "Nx" → N — but a direction that contradicts it ("slow it down to
        2x", "speed up to 0.5x") is AMBIGUOUS: the clause asks;
      * a direction alone ("slower", "reduce the speed") → 0.8 / 1.25, the
        recipe defaults, applied relative to 1x.
    """
    t = strip_quotes(norm(clause))
    if ZOOM_WORD_RE.search(t) and not SPEED_WORD_RE.search(t):
        # "zoom clip 1 to 2x" set the clip's SPEED to 2x: an "Nx" on a zoom /
        # scale clause is its size
        return SpeedAsk()
    d = direction(t, "speed")
    if d == "reset":
        return SpeedAsk(direction="reset", factor=1.0)
    if d == "both":
        return SpeedAsk(direction="both", ambiguous="It says both faster and slower — which one?")
    if m := _TIMES_RE.search(t):
        n = _num_word(m.group(1))
        word = m.group(2)
        if n and n > 0:
            if word.startswith("slower"):
                return SpeedAsk(direction="down", factor=round(1.0 / n, 4), relative=True)
            return SpeedAsk(direction="up", factor=n, relative=True)
    if (m := _AS_LONG_RE.search(t)) and not _NOT_CLIP_LENGTH_RE.search(t):
        # final sweep 2 r2: "make clip 2 (take) twice as long" is half speed
        return SpeedAsk(direction="down" if m.group(1) in ("twice", "double") else "up",
                        factor={"twice": 0.5, "double": 0.5, "half": 2.0}[m.group(1)], relative=True)
    if m := _TIMESW_RE.search(t):
        w = (m.group(1) or m.group(0).split("-")[0].split()[0]).lower()
        f = _TIMES_WORDS.get(w, 2.0)
        return _checked(d, f)
    if re.search(r"\bby half\b|\bin half\b", t) and d in ("down", "up"):
        return SpeedAsk(direction=d, factor=0.5 if d == "down" else 2.0, relative=True)
    if m := _FRAC_ABS_RE.search(t):
        num, den = (_FRAC_NUM[m.group(1)], _FRAC_DEN[m.group(2)]) if m.group(1) else (int(m.group(3)), int(m.group(4)))
        if 0 < num < den:
            return _checked(d if d in ("up", "down") else None, round(num / den, 4))
    if m := _FRAC_REL_RE.search(t):
        num, den = _FRAC_NUM[m.group(1)], _FRAC_DEN[m.group(2)]
        tail = t[m.end():m.end() + 16]
        way = "up" if re.match(r"\s*(?:faster|quicker)\b", tail) else "down" if re.match(r"\s*slower\b", tail) else d
        if way in ("up", "down") and 0 < num < den:
            f = 1 + num / den if way == "up" else 1 - num / den
            return SpeedAsk(direction=way, factor=round(f, 4), relative=True)
    if _QUARTER_RE.search(t):
        return _checked(d if d != "up" else "up", 0.25)
    if _HALF_RE.search(t):
        return _checked(d, 0.5)
    for m in _PCT_RE.finditer(t):
        n = float(m.group(1))
        tail = t[m.end():m.end() + 24]
        head = t[max(0, m.start() - 8):m.start()]
        # "slow clip 1 down 25%" / "speed it up 50%": the verb's own
        # particle before the amount makes it a change, like "by" (it set
        # 0.25x); "to 25%" stays a speed
        if d in ("up", "down") and re.search(r"\b(?:up|down)\s*$", head):
            head = head + " by "
        if re.match(r"\s*(?:faster|quicker)\b", tail) or (d == "up" and re.search(r"\bby\s*$", head)):
            return SpeedAsk(direction="up", factor=round(1 + n / 100, 4), relative=True)
        if re.match(r"\s*slower\b", tail) or (d == "down" and re.search(r"\bby\s*$", head)):
            if n >= 100:
                return SpeedAsk(direction="down", ambiguous=f"{n:g}% slower would stop the clip — what speed?")
            return SpeedAsk(direction="down", factor=round(1 - n / 100, 4), relative=True)
        if re.search(r"\bspeed|\bfast|\bslow|\bquick|\bplay", t):
            if d is None and re.search(r"\bby\s*$", head):
                # "change clip 1 speed by 30%": by how much, but which way?
                return SpeedAsk(ambiguous=f"{n:g}% faster or {n:g}% slower?")
            return _checked(d, round(n / 100, 4))
    if m := _X_RE.search(t):
        return _checked(d, float(m.group(1)))
    if m := _SPEED_NUM_RE.search(t):
        # "at like 1.5 speed", "speed = 2", "set speed 0.8 for all clips"
        # (each planned 1.25x and was rolled back)
        return _checked(d, float(m.group(1) or m.group(2)))
    if d == "up":
        return SpeedAsk(direction="up")
    if d == "down":
        return SpeedAsk(direction="down")
    return SpeedAsk()


#: A zoom / scale clause, and the words that make a clause about SPEED.
ZOOM_WORD_RE = re.compile(r"\bzoom\w*|\bscal(?:e|es|ed|ing)\b|\bpunch(?:es|ed|ing)?[- ]?in\b|\benlarg\w*|\bmagnif\w*"
                          r"|\bcrop\s+in\b|\bpush[- ]in\b")
SPEED_WORD_RE = re.compile(r"\bspeed\w*|\bsped\b|\bfast\w*|\bslow\w*|\bslo[- ]?mo\b|\bpace\b|\bplayback\b|\btempo\b"
                           r"|\bquick\w*|\btime[- ]?lapse\b|\bhurry\b|\baccelerat\w*|\bdecelerat\w*|\bramp\b|\bcurve\b")


def _checked(d: Direction | None, f: float) -> SpeedAsk:
    if f <= 0:
        return SpeedAsk(direction=d, ambiguous="A speed must be above 0x — what speed?")
    if d == "up" and f < 1.0:
        return SpeedAsk(direction=d, factor=f,
                        ambiguous=f"Speed up to {f:g}x would slow it down — faster, or {f:g}x?")
    if d == "down" and f > 1.0:
        return SpeedAsk(direction=d, factor=f,
                        ambiguous=f"Slow down to {f:g}x would speed it up — slower, or {f:g}x?")
    return SpeedAsk(direction=d or ("up" if f > 1 else "down" if f < 1 else "reset"), factor=f)


def default_speed(d: Direction | None) -> float | None:
    return {"up": 1.25, "down": 0.8, "reset": 1.0}.get(d or "")


@dataclass(frozen=True)
class LevelAsk:
    """What a clause asks of a level. `delta_db` a change ("by 6 dB", "down
    4 db"), `db` an absolute level ("to -6 dB", "-6db", "50%" → -6 dB)."""
    direction: Direction | None = None
    delta_db: float | None = None
    db: float | None = None
    ambiguous: str | None = None
    #: a level number with no unit the reading could not place ("clip 2
    #: volume 3"): nothing licenses a default step
    bare_number: bool = False


_DB_RE = re.compile(r"(?P<by>\bby\s+)?(?P<to>\b(?:to|at)\s+)?(?P<sign>-|minus\s+|\+|plus\s+)?(?P<n>\d+(?:\.\d+)?)\s*"
                    r"(?:db|decibels?)\b")
_LVL_PCT_RE = re.compile(r"(?P<by>\bby\s+)?(?P<to>\b(?:to|at)\s+)?(?P<n>\d+(?:\.\d+)?)\s*(?:%|percent\b)")


def pct_to_db(pct: float) -> float:
    return round(20 * math.log10(max(pct, 0.1) / 100.0), 1)


def level_ask(clause: str) -> LevelAsk:
    t = strip_quotes(norm(clause))
    d = direction(t, "level")
    if d == "both":
        return LevelAsk(direction="both", ambiguous="It says both louder and quieter — which one?")
    if d == "reset":
        return LevelAsk(direction="reset", db=0.0)
    if m := _DB_RE.search(t):
        n = float(m.group("n"))
        neg = bool(m.group("sign")) and m.group("sign").strip() in ("-", "minus")
        plus = bool(m.group("sign")) and m.group("sign").strip() in ("+", "plus") and not m.group("to")
        if plus and d is None:
            return LevelAsk(direction="up", delta_db=n)      # "music +3db" is a change
        if m.group("by") or (d in ("up", "down") and not m.group("to") and not neg):
            delta = abs(n) if d == "up" else -abs(n) if d == "down" else (-n if neg else n)
            return LevelAsk(direction=d or ("up" if delta > 0 else "down"), delta_db=delta)
        return LevelAsk(direction=d, db=-n if neg else n)
    if m := _LVL_PCT_RE.search(t):
        n = float(m.group("n"))
        if m.group("by"):
            if d == "down":
                return LevelAsk(direction="down", delta_db=pct_to_db(max(1.0, 100 - n)))
            if d == "up":
                return LevelAsk(direction="up", delta_db=round(20 * math.log10(1 + n / 100), 1))
        return LevelAsk(direction=d, db=pct_to_db(n))
    if _LVL_NOUN_RE.search(t) and direction(t, "speed") is None:
        if m := (_BARE_LVL_RE.search(t) or _BARE_DIR_RE.search(t)):
            return _bare_level(m, d)
    return LevelAsk(direction=d)


#: A level number said without "dB" (final sweep 2 r2: "clip 2 volume +2"
#: took -6 dB to -12 dB, "bring the music down to -25" set -20 — the number
#: was ignored and the recipe's 6 dB step applied). Only next to a level word.
_LVL_NOUN_RE = re.compile(r"\b(?:volume|level|gain|music|song|soundtrack|bgm|audio|sound|voice[- ]?overs?|vo|loud\w*"
                          r"|quiet\w*)\b")
_BARE_TAIL = (r"(?P<sign>-|minus\s+|\+|plus\s+)?(?P<n>\d+(?:\.\d+)?)(?![\d.:])"
              r"(?!\s*(?:%|percent|x\b|°|deg|s\b|sec|secs\b|seconds?\b|m\b|min|minutes?\b|times\b|db\b|decibels?"
              r"|px\b|fps\b|k\b|p\b|st\b|nd\b|rd\b|th\b))")
_BARE_LVL_RE = re.compile(r"\b(?:volume|level|gain|music|song|soundtrack|bgm|audio|sound|voice[- ]?overs?|vo)\s+"
                          r"(?:(?:volume|level|gain)\s+)?(?:(?P<to>to|at|=)\s*)?" + _BARE_TAIL)
_BARE_DIR_RE = re.compile(r"\b(?:down|up)\s+(?:(?P<to>to)\s+)?" + _BARE_TAIL)


def _bare_level(m: re.Match, d: Direction | None) -> LevelAsk:
    n = float(m.group("n"))
    sign = (m.group("sign") or "").strip()
    if m.group("to"):
        neg = sign in ("-", "minus") or (d == "down" and not sign and n > 0)
        return LevelAsk(direction=d, db=-n if neg else n)
    if sign in ("+", "plus"):
        return LevelAsk(direction="up", delta_db=n)                  # "music volume +2" is a change
    if sign in ("-", "minus"):
        return LevelAsk(direction=d or "down", db=-n)               # like "-6db": a level
    if d in ("up", "down"):
        return LevelAsk(direction=d, delta_db=n if d == "up" else -n)   # "turn the music up 3"
    return LevelAsk(direction=d, bare_number=True)


@dataclass(frozen=True)
class SizeAsk:
    direction: Direction | None = None
    px: float | None = None


_SIZE_PX_RE = re.compile(r"\b(?:font\s+)?size\s+(?:to\s+|of\s+)?(\d{2,3})\b|\b(\d{2,3})\s*(?:px|pixels?|pt)\b"
                         r"|\bto\s+(\d{2,3})\s*(?:px|pt)?$")


def size_ask(clause: str) -> SizeAsk:
    t = strip_quotes(norm(clause))
    d = direction(t, "size")
    m = _SIZE_PX_RE.search(t)
    px = float(next(g for g in m.groups() if g)) if m else None
    return SizeAsk(direction=d if d != "both" else None, px=px)


#: An explicit angle with its unit — the sign (or "minus") is part of it:
#: "rotate clip 3 -45 degrees" rotated +45 when a bare-angle reading matched
#: first from "rotate" and `\b` cannot stand before "-".
_DEG_UNIT_RE = re.compile(r"(?<![\w.])(-|minus\s+|negative\s+)?(\d{1,3}(?:\.\d+)?)\s*(?:°|degrees?\b|deg\b)")
_DEG_BARE_RE = re.compile(r"\brotat\w*\b.*?(?:\bby\s+)?(?<![\w-])(-|minus\s+)?(90|180|270|45)\b"
                          r"(?!\s*(?:%|x\b|s\b|sec))")


def rotation_degrees(clause: str) -> float | None:
    """The SIGNED angle a rotate clause names ("-45 degrees" → -45, "minus 30
    degrees" → -30, "rotate it 90" → 90), or None."""
    t = strip_quotes(norm(clause))
    m = _DEG_UNIT_RE.search(t) or _DEG_BARE_RE.search(t)
    if not m:
        return None
    deg = -float(m.group(2)) if m.group(1) else float(m.group(2))
    if _CCW_RE.search(t):
        # final sweep 2 r2: "90 degrees counterclockwise" is -90 (the net
        # refused the planner's right answer as "did not rotate 90°")
        deg = -abs(deg)
    return deg


_CCW_RE = re.compile(r"\b(?:counter[- ]?clockwise|anti[- ]?clockwise|ccw|to the left)\b")


_FRACTION_S: tuple[tuple[re.Pattern, float], ...] = (
    (re.compile(r"\b(?:a|one)\s+(?:second|sec)\s+and\s+a\s+half\b|\b(?:one|1)\s+and\s+a\s+half\s+(?:seconds?|secs?)\b"),
     1.5),
    (re.compile(r"\bhalf\s+(?:a\s+)?(?:second|sec)\b|\ba\s+half(?:[- ]a)?[- ](?:second|sec)\b"), 0.5),
    (re.compile(r"\b(?:a\s+)?quarter\s+(?:of\s+a\s+)?(?:second|sec)\b"), 0.25),
)


def fraction_seconds(text: str) -> float | None:
    """A length said in words: "half a second" 0.5, "a quarter second" 0.25,
    "a second and a half" 1.5 ("for half a second" held a 3 s freeze)."""
    t = norm(text)
    for rx, v in _FRACTION_S:
        if rx.search(t):
            return v
    return None


# --------------------------------------------------------------------------
# 4. Scope: which clips, which media
# --------------------------------------------------------------------------

ClipRef = int | str      # 1-based index, -1 last, -2 penultimate, "mid", "sel", "at:<t>", "playhead"

_ORD_N = {"first": 1, "opening": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4,
          "4th": 4, "fifth": 5, "5th": 5, "sixth": 6, "6th": 6, "seventh": 7, "7th": 7, "eighth": 8,
          "8th": 8, "ninth": 9, "9th": 9, "tenth": 10, "10th": 10, "last": -1, "final": -1, "closing": -1,
          "middle": "mid"}
_NUM_N = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
          "ten": 10}
_PIECE = r"(?:clips?|shots?|segments?|scenes?|ones?|parts?|bits?|sections?|pieces?)"
_ORD_ANY = r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\d{1,2}(?:st|nd|rd|th)|last|final|opening|closing|middle)"
_ORD_LIST_RE = re.compile(rf"\b(?:the\s+)?({_ORD_ANY})((?:\s*,\s*(?:the\s+)?{_ORD_ANY})*)\s+(?:and|&)\s+(?:the\s+)?({_ORD_ANY})\s+{_PIECE}\b")
_ORD_ONE_RE = re.compile(rf"\b({_ORD_ANY})\s+{_PIECE}\b")
_NUM_LIST_RE = re.compile(rf"\b{_PIECE}\s+(?:#\s*|number\s+|no\.?\s*|nr\.?\s*)?({_NUMW})((?:\s*(?:,|and|&)\s*(?:{_PIECE}\s+)?(?:#\s*|number\s+|nr\.?\s*)?{_NUMW})*)\b"
                          r"(?!\d)(?!\.\d)(?!\s*(?:%|°|x\b|db\b|s\b|sec|secs\b|seconds?\b|deg|degrees?\b|px\b|fps\b|k\b|p\b|times\b))")
_FIRST_N_RE = re.compile(rf"\b(?:first|opening)\s+({_NUMW})\s+{_PIECE}\b")
_LAST_N_RE = re.compile(rf"\b(?:last|final|closing)\s+({_NUMW})\s+{_PIECE}\b")
_PENULT_RE = re.compile(rf"\b(?:second[- ]to[- ]last|next[- ]to[- ]last|penultimate|second[- ]last)\s+{_PIECE}\b")
_SEL_RE = re.compile(rf"\b(?:this|that|the\s+selected|selected|current|the\s+current)\s+(?:{_PIECE}|portion)\b"
                     r"|\bthe\s+(?:clip|one|part)\s+(?:i|i've|i\s+have)\s+(?:selected|picked|chose|highlighted)\b"
                     r"|\bthe\s+selection\b|\bthis\s+one\b|\b(?:this|that)\s+bit\b")
_CLIP_AT_RE = re.compile(rf"\b{_PIECE}\s+(?:at|around|that\s+starts\s+at|starting\s+at)\s+"
                         r"(?:(\d{1,2}):(\d{2}(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)?)\b")
_PLAYHEAD_CLIP_RE = re.compile(r"\b(?:clip|shot|one|part|bit)\s+(?:under|at|beneath|below|on|by)\s+the\s+"
                               r"(?:playhead|cursor|scrubber)\b|\bthe\s+clip\s+i'?m\s+on\b"
                               r"|\bthe\s+clip\s+(?:where|that)\s+the\s+(?:playhead|cursor)\s+is\b")
_INTRO_RE = re.compile(r"\bthe\s+(?:intro|opening|beginning\s+clip)\b(?!\s+(?:text|title|music|song|card|line|hook)\b)")
_OUTRO_RE = re.compile(r"\bthe\s+(?:outro|ending)\b(?!\s+(?:text|title|music|song|card|line|screen)\b)")
_ALL_RE = re.compile(r"\b(?:everything|every\s+(?:clip|shot|single\s+clip|part|scene|one)|all\s+(?:the\s+|of\s+the\s+)?"
                     r"(?:clips|shots|scenes|parts|footage|videos?)|all\s+of\s+(?:it|them)|each\s+(?:clip|shot|one)"
                     r"|(?:the\s+)?(?:whole|entire|full)\s+(?:video|thing|clip|timeline|edit|project|movie|footage)"
                     r"|the\s+(?:video|footage|timeline|movie)\b|\bit\s+all\b|\ball\s+clips\b|\bthe\s+rest\b"
                     r"|\bthem\s+all\b|\ball\s+(?:three|two|four)\b)")
_EXCEPT_RE = re.compile(r"\b(?:except(?:\s+for)?|but\s+not|but|other\s+than|apart\s+from|besides|excluding|save\s+for)\s+(.+)$")
_ONLY_RE = re.compile(r"\b(?:only|just)\b")
_PRONOUN_RE = re.compile(r"\b(?:it|them|that\s+one|this\s+one|the\s+same|those|these)\b")

Media = Literal["v1", "music", "voice", "captions", "text", "overlay", "vo", "master", "sticker"]

#: What a sticker is called — ONE vocabulary for the planner's animation
#: reader (anim_expanders) and this reading (the contract): the planner read
#: "animate the logo" as the sticker and the contract did not, so the net
#: rolled a correct animation back (final sweep 3).
STICKER_NOUNS = r"stickers?|emojis?|logos?|gifs?"

_MEDIA_RX: tuple[tuple[str, re.Pattern], ...] = (
    ("captions", re.compile(r"\b(?:captions?|subtitles?|subs|captoins?)\b")),
    ("vo", re.compile(r"\bvoice[- ]?overs?\b|\bvo\b|\bnarration\s+track\b|\bvoice\s+track\b")),
    ("music", re.compile(r"\b(?:music|musci|song|soundtrack|bgm|tune|score|bed|backing\s+track|background\s+music|beat)\b")),
    ("text", re.compile(r"\b(?:title|titles|text|texts|heading|headline|lower[- ]?third|label|caption\s+text)\b")),
    ("overlay", re.compile(r"\b(?:overlays?|pips?|picture[- ]in[- ]pictures?|top\s+clip|b-?roll)\b")),
    ("sticker", re.compile(rf"\b(?:{STICKER_NOUNS})\b")),
    ("voice", re.compile(r"\b(?:voice|vocals?|dialogue|speech|talking|narration|original\s+(?:audio|sound)|clip\s+audio)\b")),
)


@dataclass(frozen=True)
class Scope:
    """What ONE clause is about.

    `refs` are the main-lane clips it names (ClipRef), `all` whether it says
    everything, `except_refs` the clips an "except/but" leaves out, `only`
    whether it restricts ("only the second clip", "just the last clip"),
    `pronoun` whether it says it/them with no clip of its own, `media` the
    lanes it names (music, voice, captions, text, …) in the order found."""
    refs: tuple[ClipRef, ...] = ()
    all: bool = False
    except_refs: tuple[ClipRef, ...] = ()
    only: bool = False
    pronoun: bool = False
    carried: bool = False                # refs came from an earlier clause (it/them)
    media: tuple[str, ...] = ()
    #: lanes named inside the except / but-not tail ("all the clips except
    #: the music"): the clause must leave them alone
    except_media: tuple[str, ...] = ()
    #: lanes named only as the REFERENCE ("under the voiceover", "than the
    #: music", "so my voiceover is clear"): the clause must leave them alone
    ref_media: tuple[str, ...] = ()

    @property
    def names_clips(self) -> bool:
        return bool(self.refs)


def _ord(word: str) -> ClipRef | None:
    w = word.strip().lower()
    if w in _ORD_N:
        return _ORD_N[w]
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)", w)
    return int(m.group(1)) if m else None


def _numref(word: str) -> int | None:
    w = word.strip().lower()
    if w.isdigit():
        return int(w)
    return _NUM_N.get(w)


#: "clips 1-2", "clips 1 through 3", "shots 2 to 4": a RANGE of clips
_CLIP_RANGE_RE = re.compile(rf"\b(clips|shots|segments|scenes)\s+({_NUMW})\s*(?:-|–|to|through|thru|till|until)\s*({_NUMW})\b"
                            r"(?!\s*(?:%|°|x\b|db\b|s\b|sec|secs\b|seconds?\b))")


def _expand_clip_ranges(t: str) -> str:
    def sub(m: re.Match) -> str:
        a, b = _numref(m.group(2)), _numref(m.group(3))
        if not a or not b or b <= a or b - a > 20:
            return m.group(0)
        nums = [str(i) for i in range(a, b + 1)]
        return f"{m.group(1)} " + ", ".join(nums[:-1]) + f" and {nums[-1]}"
    return _CLIP_RANGE_RE.sub(sub, t)


def clip_refs(text: str) -> list[ClipRef]:
    """Every main-lane clip `text` names, in order, without duplicates."""
    t = _expand_clip_ranges(strip_quotes(norm(text)))
    out: list[ClipRef] = []

    def add(r: ClipRef | None) -> None:
        if r is not None and r not in out:
            out.append(r)

    for m in _CLIP_AT_RE.finditer(t):
        secs = int(m.group(1)) * 60 + float(m.group(2)) if m.group(1) is not None else float(m.group(3))
        add(f"at:{round(secs, 3):g}")
    if _PENULT_RE.search(t):
        add(-2)
    for m in _FIRST_N_RE.finditer(t):
        n = _numref(m.group(1)) or 0
        for i in range(1, n + 1):
            add(i)
    for m in _LAST_N_RE.finditer(t):
        n = _numref(m.group(1)) or 0
        for i in range(n, 0, -1):
            add(-i)
    for m in _ORD_LIST_RE.finditer(t):
        add(_ord(m.group(1)))
        for w in re.findall(_ORD_ANY, m.group(2) or ""):
            add(_ord(w))
        add(_ord(m.group(3)))
    for m in _ORD_ONE_RE.finditer(t):
        if re.match(r"(?:second|next)[- ]", t[m.start():]) and _PENULT_RE.search(t[m.start():m.end() + 12]):
            continue
        # "the first 3 seconds" is time, not a clip ("first … seconds" never reaches here: needs a piece noun)
        add(_ord(m.group(1)))
    for m in _NUM_LIST_RE.finditer(t):
        add(_numref(m.group(1)))
        for w in re.findall(_NUMW, m.group(2) or ""):
            add(_numref(w))
    if _SEL_RE.search(t):
        add("sel")
    if _PLAYHEAD_CLIP_RE.search(t):
        add("playhead")
    if not out and _INTRO_RE.search(t):
        add(1)
    if not out and _OUTRO_RE.search(t):
        add(-1)
    return out


def media_of(text: str) -> tuple[str, ...]:
    t = strip_quotes(norm(text))
    found: list[tuple[int, str]] = []
    for name, rx in _MEDIA_RX:
        m = rx.search(t)
        if m:
            found.append((m.start(), name))
    # "the title music" / "caption text": the first noun is the lane
    return tuple(n for _, n in sorted(found))


#: The REFERENCE a sound is set against: "lower the clip audio under the
#: voiceover", "make the clips quieter than the voiceover", "turn the video
#: sound down so my voiceover is clear". The lane named after it is what the
#: user wants to HEAR, never the lane to change (it lowered the voiceover).
#: "voice over" (two words) is a lane, not "voice" + "over".
_REF_TAIL_RE = re.compile(r"\b(?:under(?:neath)?|beneath|below|behind|than|while|when|whenever"
                          r"|so\s+(?:that\s+)?(?:i\s+can\s+hear\s+)?(?:my|the|our)|(?<!voice\s)(?<!voice-)over)\b")
_REF_LANE_RE = re.compile(r"\bvoice[- ]?overs?\b|\bvo\b|\bnarrat\w*|\bvoice\b|\bspeech\b|\bdialogue\b|\btalking\b"
                          r"|\b(?:music|song|soundtrack|bgm|bed)\b")


def reference_split(text: str) -> tuple[str, str]:
    """(`head`, `tail`): `text` split before the reference phrase that names a
    sound lane ("… under the voiceover", "… than the music", "… so my
    voiceover is clear"). `tail` is "" when there is none — or when the head
    names no sound of its own to change ("duck under the voiceover")."""
    t = strip_quotes(norm(text))
    for m in _REF_TAIL_RE.finditer(t):
        head, tail = t[:m.start()].strip(), t[m.start():]
        if _REF_LANE_RE.search(tail[m.end() - m.start():]) and head:
            return head, tail
    return t, ""


def scope_of(clause: str) -> Scope:
    t = strip_quotes(norm(clause))
    ex_refs: tuple[ClipRef, ...] = ()
    ex_media: tuple[str, ...] = ()
    main = t
    m = _EXCEPT_RE.search(t)
    bare = _EXCEPT_ORD_RE.match(m.group(1)) if m and not clip_refs(m.group(1)) else None
    if m and bare is not None and _ord(bare.group(1).replace("final", "last")) is not None:
        ex_refs = (_ord(bare.group(1).replace("final", "last")),)
        main = t[:m.start()]
    elif m and (clip_refs(m.group(1)) or media_of(m.group(1))):
        ex_refs = tuple(clip_refs(m.group(1)))
        ex_media = media_of(m.group(1))
        main = t[:m.start()]
    refs = tuple(r for r in clip_refs(main) if r not in ex_refs)
    everything = bool(_ALL_RE.search(main))
    pron = bool(_PRONOUN_RE.search(main)) and not refs
    head, tail = reference_split(main)
    media = media_of(head) if tail else media_of(main)
    ref_media = tuple(x for x in media_of(tail) if x not in media) if tail else ()
    return Scope(refs=refs, all=everything and not refs, except_refs=ex_refs,
                 only=bool(_ONLY_RE.search(main)) and bool(refs), pronoun=pron,
                 media=media, except_media=ex_media, ref_media=ref_media)


def resolve_scopes(prompt_or_clauses: str | list[str]) -> list[Scope]:
    """`scope_of` per clause, with "it / them / that one" in a clause that
    names no clip carried from the NEAREST earlier clause that did ("mute
    clip 2 and slow it down" → clip 2 both times; "take the last clip,
    reverse it and mute it" → the last clip three times). A clause with no
    clip and no pronoun is its own scope (whole video / media)."""
    cls = clauses(prompt_or_clauses) if isinstance(prompt_or_clauses, str) else list(prompt_or_clauses)
    out: list[Scope] = []
    prev: Scope | None = None
    named_ordinal = False
    for c in cls:
        s = scope_of(c)
        if not s.refs and not s.all and named_ordinal and (m := _ELIDED_ORD_RE.match(strip_quotes(norm(c)))):
            # "give the first clip a warm look and the last a cool look": "the
            # last" is the last CLIP (it was every clip)
            r = _ord(m.group(1))
            if r is not None:
                s = Scope(refs=(r,), media=s.media, only=s.only)
        if _ORD_ONE_RE.search(strip_quotes(norm(c))):
            named_ordinal = True
        if s.refs or s.all:
            prev = s
        elif prev is not None and (s.pronoun or _BARE_CONTINUATION_RE.search(c)):
            s = Scope(refs=prev.refs, all=prev.all, except_refs=prev.except_refs, only=prev.only,
                      pronoun=s.pronoun, carried=True, media=s.media or (), ref_media=s.ref_media)
        out.append(s)
    return out


#: A clause that opens on a bare ordinal ("the last a cool look", "the second
#: one slower") — never a time ("the last 5 seconds") nor a half.
_ELIDED_ORD_RE = re.compile(rf"^(?:and\s+|then\s+)?(?:make\s+|give\s+|do\s+|put\s+)?(?:the\s+)({_ORD_ANY})\b"
                            r"(?!\s+(?:\d|one|two|three|four|five|few|half|part|piece|bit|second|seconds|minute|time)\b)")
#: A clause with no object at all ("… and black and white", "then 2x") is the
#: same thing as the one before it.
def is_bare_continuation(clause: str) -> bool:
    """"and brighter", "1.5x", "black and white": a clause that only says
    HOW — it is about the clip the previous clause named."""
    return bool(_BARE_CONTINUATION_RE.search(strip_quotes(norm(clause))))


_BARE_CONTINUATION_RE = re.compile(r"^(?:make\s+it\s+|and\s+)?(?:\d+(?:\.\d+)?\s*x|black and white|b\s*and\s*w|mono|"
                                   r"warm|cool|faster|slower|reversed?|backwards|muted?|flipped|louder|quieter|"
                                   # final sweep 3: "make clip 1 black and white and brighter" graded every clip
                                   r"brighter|darker|dimmer|sharper|more vivid|more saturated|less saturated)$")


# --------------------------------------------------------------------------
# 5. Time phrases and "keep only"
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TimeRef:
    kind: Literal["first", "last", "range", "at", "after", "before"]
    a: float
    b: float | None = None


_T = r"(?:(\d{1,2}):(\d{2}(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds?|m|min|mins|minutes?)?)"
_FIRSTLAST_RE = re.compile(rf"\b(first|last|final|opening|closing)\s+(\d+(?:\.\d+)?|{'|'.join(k for k in _WORD_NUM if ' ' not in k and k not in ('a', 'an'))}|a|one)?\s*"
                           r"(s|sec|secs|seconds?|m|min|mins|minutes?)\b(?!\s+(?:clip|shot|part|half))")
#: "remove 15 seconds from the end", "take 2 seconds off the start"
_FROM_EDGE_RE = re.compile(rf"\b(\d+(?:\.\d+)?|{'|'.join(k for k in _WORD_NUM if ' ' not in k and k not in ('a', 'an'))}|a|an)\s*"
                           r"(s|sec|secs|seconds?|m|min|mins|minutes?)\s+(?:from|off|at|of)\s+(?:the\s+)?(?:very\s+)?"
                           r"(end|ending|start|beginning|front|back)\b(?!\s+of\s+(?:the\s+)?(?:clip|shot|\w+\s+clip))")
_RANGE_RE = re.compile(rf"\b(?:from\s+|between\s+|seconds?\s+)?{_T}\s*(?:to|-|–|until|till|thru|through|and)\s*{_T}(?![\w:])")
_AT_RE = re.compile(rf"\b(?:at|@)\s+{_T}(?![\w:%.])(?!\s*(?:%|x\b|percent))")
_AFTER_RE = re.compile(rf"\b(after|past|before)\s+(?:the\s+)?{_T}(?![\w:])")


def _secs(mm, ss, n, unit) -> float:
    if mm is not None:
        return int(mm) * 60 + float(ss)
    v = float(n)
    return v * 60 if unit and unit.startswith("m") else v


#: "the first and last second", "the first and last 2 seconds": BOTH ends
_FIRST_AND_LAST_RE = re.compile(r"\b(?:first|opening)\s+and\s+(?:the\s+)?(?:very\s+)?(?:last|final|closing)\s+"
                                r"(\d+(?:\.\d+)?|two|three|four|five|ten|a|one)?\s*(s|sec|secs|seconds?|m|min|mins|minutes?)\b")


_EDGE_BY_RE = re.compile(r"\b(?:the\s+)?(start|beginning|front|head|end|ending|back|tail)\s+by\s+"
                         r"(\d+(?:\.\d+)?|a|one|two|three|four|five)\s*(s|sec|secs|seconds?|m|min|mins|minutes?)\b")


def time_refs(text: str) -> list[TimeRef]:
    t = strip_quotes(norm(text))
    out: list[TimeRef] = []
    for m in _FIRST_AND_LAST_RE.finditer(t):
        n = _num_word(m.group(1)) if m.group(1) else 1.0
        if n:
            v = n * 60 if m.group(2).startswith("m") else n
            out += [TimeRef("first", float(v)), TimeRef("last", float(v))]
    for m in _FIRSTLAST_RE.finditer(t):
        if out and any(x.kind == "last" and m.group(1) in ("last", "final", "closing") for x in out) \
                and _FIRST_AND_LAST_RE.search(t):
            continue
        n = _num_word(m.group(2)) if m.group(2) else 1.0
        if n is None:
            continue
        unit = m.group(3)
        v = n * 60 if unit.startswith("m") else n
        out.append(TimeRef("first" if m.group(1) in ("first", "opening") else "last", float(v)))
    for m in _EDGE_BY_RE.finditer(t):
        # "trim the start by 1s and the end by 1s" (only the head was cut)
        n = _num_word(m.group(2))
        if n:
            v = n * 60 if m.group(3).startswith("m") else n
            out.append(TimeRef("first" if m.group(1) in ("start", "beginning", "front", "head") else "last", float(v)))
    if not out:
        for m in _FROM_EDGE_RE.finditer(t):
            n = _num_word(m.group(1))
            if n is None:
                continue
            v = n * 60 if m.group(2).startswith("m") else n
            out.append(TimeRef("first" if m.group(3) in ("start", "beginning", "front") else "last", float(v)))
    for m in _RANGE_RE.finditer(t):
        g = m.groups()
        # a range needs a unit or a clock on at least one end, or "from/between"
        if not (g[3] or g[7] or g[0] is not None or g[4] is not None or re.match(r"\s*(?:from|between|seconds?)\b", t[m.start():])):
            continue
        a, b = _secs(*g[0:4]), _secs(*g[4:8])
        if b > a:
            out.append(TimeRef("range", a, b))
    for m in _AFTER_RE.finditer(t):
        g = m.groups()
        out.append(TimeRef("before" if g[0] == "before" else "after", _secs(*g[1:5])))
    for m in _AT_RE.finditer(t):
        g = m.groups()
        out.append(TimeRef("at", _secs(*g)))
    return out


_KEEP_RE = re.compile(r"\b(?:only|just)\s+keep\b|\bkeep\s+(?:only|just)\b|\bkeep\b.+\bonly\b|\bleave\s+(?:only|just)\b"
                      r"|\bkeep\s+(?:the\s+)?(?:first|last|opening|closing|final|middle|beginning|end)\b"
                      r"|\bkeep\s+(?:seconds?|from|between)\b|\btrim\s+(?:it\s+|this\s+|the\s+video\s+)?down\s+to\b")
_KEEP_NOT_RE = re.compile(r"\bkeep\s+(?:the\s+)?(?:music|audio|sound|captions?|text|title|volume|pitch)\b"
                          r"|\bbut\s+keep\b|\bkeep(?:ing)?\s+(?:it|them)\s+(?:the\s+same|as)\b")


def keeps_only(clause: str) -> bool:
    """"keep only X" / "just keep X" / "keep X only" — X is what STAYS."""
    t = strip_quotes(norm(clause))
    return bool(_KEEP_RE.search(t)) and not _KEEP_NOT_RE.search(t)


# --------------------------------------------------------------------------
# 6. Ambiguity: a clause whose direction or scope cannot be read ASKS
# --------------------------------------------------------------------------

_SPEED_NOUN_RE = re.compile(r"\bspeed\b|\bpace\b|\btempo\b")
_CHANGE_VERB_RE = re.compile(r"\b(?:change|adjust|alter|modify|tweak|fix|edit|set)\b")
_VOLUME_NOUN_RE = re.compile(r"\b(?:volume|level|levels|loudness|gain)\b")
_MUSIC_SPEED_RE = re.compile(r"\b(?:slow|speed|faster|slower|quicker)\b.*\b(?:music|song|soundtrack|bgm|tune|track)\b"
                             r"|\b(?:music|song|soundtrack|bgm|tune)\b.*\b(?:faster|slower|speed\s+up|slow\s+down)\b")


def ambiguities(prompt: str, *, video_end: float | None = None, clip_count: int | None = None) -> list[str]:
    """Questions for clauses whose direction, amount or scope cannot be read
    safely. Empty when the prompt is clear enough to plan."""
    qs: list[str] = []
    for c in clauses(prompt):
        t = strip_quotes(c)
        if _SPEED_NOUN_RE.search(t) or direction(t, "speed"):
            sa = speed_ask(t)
            if sa.ambiguous:
                qs.append(sa.ambiguous)
            elif (_SPEED_NOUN_RE.search(t) and _CHANGE_VERB_RE.search(t) and sa.direction is None
                  and sa.factor is None and not re.search(r"\b(?:ramp|curve|montage|hero|bullet|jump|flash)\b", t)):
                qs.append("Faster or slower — and by how much? Say like 'speed it up 1.5x' or 'slow it down to 0.5x'.")
            if _MUSIC_SPEED_RE.search(t) and not clip_refs(t):
                qs.append("The Prompt bar changes the speed of clips on the timeline, not the music's tempo — "
                          "which clip should play faster or slower?")
        if _VOLUME_NOUN_RE.search(t) and _CHANGE_VERB_RE.search(t):
            la = level_ask(t)
            if la.direction is None and la.db is None and la.delta_db is None and not re.search(r"\blufs\b|\bnormali", t):
                qs.append("Louder or quieter — and by how much? Say like 'music down 6 dB' or 'set clip 2 to -10 dB'.")
        la = level_ask(t)
        if la.ambiguous:
            qs.append(la.ambiguous)
        if video_end:
            for tr in time_refs(t):
                far = tr.b if tr.kind == "range" else tr.a
                if tr.kind in ("first", "last") and tr.a >= video_end - 1e-6 and re.search(
                        r"\b(?:cut|trim|delete|remove|chop|lose|drop|get rid of|keep)\b", t):
                    qs.append(f"The video is only {video_end:g}s long — the {tr.kind} {tr.a:g}s is all of it. "
                              "How much should go?")
                elif tr.kind == "range" and far is not None and far > video_end + 0.05 and re.search(
                        r"\b(?:cut|trim|delete|remove|chop|lose|drop|get rid of|keep)\b", t):
                    qs.append(f"{far:g}s is past the end of the video ({video_end:g}s) — which moment did you mean?")
        if clip_count is not None:
            for r in clip_refs(t):
                if isinstance(r, int) and r > 0 and r > clip_count:
                    qs.append(f"There are only {clip_count} clips — which one did you mean?")
    return list(dict.fromkeys(qs))


__all__ = ["Direction", "Axis", "TYPOS", "fix_typos", "norm", "strip_quotes", "clauses", "direction", "SpeedAsk", "speed_ask",
           "default_speed", "LevelAsk", "level_ask", "pct_to_db", "SizeAsk", "size_ask", "ClipRef", "Scope",
           "clip_refs", "media_of", "scope_of", "reference_split", "resolve_scopes", "TimeRef", "time_refs", "keeps_only",
           "rotation_degrees", "fraction_seconds", "ZOOM_WORD_RE", "SPEED_WORD_RE",
           "ambiguities"]
