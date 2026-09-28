"""Slot readers for the clip-level prompts (wave D3, lane E3) — which clip,
which range, which direction — read from ONE clause's text.

`planner._hit_slots` hands a grammar hit here for the recipes this module
knows (`READERS`) and merges `clip_extras` into the rest (speed, reverse,
mute, fade, look, stabilise, upscale, title, transitions, trim, freeze). Pure
functions over text; the clip itself is bound to an id later, against facts
(`clip_expanders.bind_clip`), because a clause cannot know the timeline.

Times the grammar reads: "3s", "3 sec", "00:03", "1:20", "at 1:20", "the
last 5 seconds", "from 0:04 to 0:06", "3s to 5s", "00:04-00:06".
"""
from __future__ import annotations

import re
from typing import Any, Callable

from . import grammar as G
from . import slots as S

_T = r"(?:(\d{1,2}):(\d{2}(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds?)?)"
#: "3s to 5s", "00:04-00:06", "3 to 5 seconds" — a range with no "from".
_BARE_RANGE_RE = re.compile(rf"(?<![\w:.]){_T}\s*(?:to|-|–|until|till|through|thru)\s*{_T}(?![\w:])")
_IT_RE = re.compile(r"\b(?:it|this|that)$")


def _secs(mm: str | None, ss: str | None, n: str | None) -> float:
    return int(mm) * 60 + float(ss) if mm is not None else float(n)


#: "trim 2 seconds off the end", "cut the start by 1.5 s".
_OFF_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)\s+(?:off|from)\s+(?:the\s+)?(end|ending|start|beginning|front)\b"
                     r"|\b(end|ending|start|beginning)\s+by\s+(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)\b")


#: "everything after 10 seconds", "anything before 0:03".
_AFTER_RE = re.compile(rf"\b(?:everything|anything|all|the rest|the part|what'?s)?\s*(after|past|before)\s+(?:the\s+)?{_T}(?![\w:])")


def bare_range(clause: str) -> S.TimeRange | None:
    """A range the slot extractor misses (it needs "from"/"between", or
    "first/last N"). At least one end must carry a unit or a colon, so "2 to 3
    clips" is not a time."""
    if a := _AFTER_RE.search(clause):
        t = _secs(a.group(2), a.group(3), a.group(4))
        return (S.TimeRange(kind="abs", start=t, end=None) if a.group(1) == "after"
                else S.TimeRange(kind="first", end=t))
    if o := _OFF_RE.search(clause):
        n = float(o.group(1) or o.group(4))
        edge = o.group(2) or o.group(3)
        return S.TimeRange(kind="last" if edge.startswith("end") else "first", end=n)
    m = _BARE_RANGE_RE.search(clause)
    if not m:
        return None
    a_mm, a_ss, a_n, a_u, b_mm, b_ss, b_n, b_u = m.groups()
    if not (a_mm or a_u or b_mm or b_u):
        return None
    a, b = _secs(a_mm, a_ss, a_n), _secs(b_mm, b_ss, b_n)
    return S.TimeRange(kind="abs", start=a, end=b) if b > a else None


def clip_ref(hit: G.IntentHit) -> str | None:
    """The one clip a clause names; "delete it" / "duplicate this" is the
    selection."""
    ref = G.clip_ref_of(hit.clause)
    if ref is None and _IT_RE.search(hit.clause):
        return "$selected"
    return ref


def _range(c: S.Slots, clause: str) -> S.TimeRange | None:
    return c.range or bare_range(clause)


# -------------------------------------------------------------- new recipes

_PLURAL_RE = re.compile(rf"\b(?:the|these|those|my)\s+(?:clips|shots|segments|scenes)\b")


def _one(hit: G.IntentHit, c: S.Slots) -> dict[str, Any]:
    ref = clip_ref(hit)
    if ref is None and _PLURAL_RE.search(hit.clause):
        # "delete the clips": several — the expander asks, never the selection
        ref = "$v1_all"
    return {"clip_ref": ref}


_TO_RE = (
    (re.compile(r"\breverse\s+the\s+(?:order|sequence)\b"), "reverse"),
    (re.compile(r"\bswap\b|\bswitch\b"), "swap"),
    (re.compile(r"\b(?:before|in front of)\b"), "before"),
    (re.compile(r"\b(?:after|behind)\b"), "after"),
    (re.compile(r"\b(?:to|at)\s+(?:the\s+)?(?:very\s+)?(?:start|beginning|front)\b|\b(?:be\s+)?(?:the\s+)?first(?:\s+(?:one|clip|place))?$"), "start"),
    (re.compile(r"\b(?:to|at)\s+(?:the\s+)?(?:very\s+)?(?:end|back|finish)\b|\b(?:be\s+)?(?:the\s+)?last(?:\s+(?:one|clip|place))?$"), "end"),
)
_SWAP_FIRST_TWO_RE = re.compile(r"\b(?:first|opening)\s+two\b")
_SWAP_LAST_TWO_RE = re.compile(r"\b(?:last|final)\s+two\b")
_PAIR_RE = re.compile(rf"\b({G.ORDINAL})\s+(?:and|&|with)\s+(?:the\s+)?({G.ORDINAL})\b"
                      rf"|\b{G.CLIP_NOUN}\s+(\d{{1,2}})\s+(?:and|&|with)\s+(\d{{1,2}})\b")


def _ord_ref(word: str) -> str | None:
    return G.clip_ref_of(f"the {word} clip")


def _move(hit: G.IntentHit, c: S.Slots) -> dict[str, Any]:
    clause = hit.clause
    to = next((v for rx, v in _TO_RE if rx.search(clause)), None)
    if to == "reverse":
        return {"to": "reverse"}
    if to == "swap":
        if _SWAP_FIRST_TWO_RE.search(clause):
            return {"to": "swap", "clip_ref": "$v1_first", "anchor": f"{G.NTH_REF}2"}
        if _SWAP_LAST_TWO_RE.search(clause):
            return {"to": "swap", "clip_ref": f"{G.NTH_REF}-2", "anchor": "$v1_last"}
        m = _PAIR_RE.search(clause)
        if m:
            if m.group(1):
                return {"to": "swap", "clip_ref": _ord_ref(m.group(1)), "anchor": _ord_ref(m.group(2))}
            return {"to": "swap", "clip_ref": G.clip_ref_of(f"clip {m.group(3)}"),
                    "anchor": G.clip_ref_of(f"clip {m.group(4)}")}
        return {"to": "swap"}
    if to in ("before", "after"):
        m = re.search(r"\b(?:before|in front of|after|behind)\b", clause)
        head, tail = (clause[:m.start()], clause[m.end():]) if m else (clause, "")
        ref = G.clip_ref_of(head) or ("$selected" if re.search(r"\b(?:it|this|that)\b", head) else None)
        return {"to": to, "clip_ref": ref, "anchor": G.clip_ref_of(tail)}
    head = re.split(r"\b(?:to|at)\s+(?:the\s+)?(?:very\s+)?(?:start|beginning|front|end|back|finish)\b", clause)[0]
    ref = G.clip_ref_of(head)
    if ref is None and re.search(r"\b(?:it|this|that)\b", head):
        ref = "$selected"
    # "make the second clip the first one": the clip is the one BEFORE "the".
    m = re.match(rf"^make\s+({G.CLIP_PHRASE})", clause)
    if m:
        ref = G.clip_ref_of(m.group(1))
    return {"to": to or "end", "clip_ref": ref}


_PCT_RE = re.compile(r"\b(\d{2,3}(?:\.\d+)?)\s*(?:%|percent\b)")
_ZOOM_OUT_RE = re.compile(r"\b(?:zoom|push|pull)(?:s|ed|ing)?[- ]?out\b|\bpull back\b")
_SLOW_RE = re.compile(r"\bslow(?:ly)?\b|\bgradual(?:ly)?\b|\bken[- ]?burns\b|\bpan (?:and|&) zoom\b|\bover the (?:clip|shot)\b"
                      r"|\bsmooth(?:ly)?\b|\bgentle\b|\bsubtle\b|\bzoom (?:effect|animation)\b")
_STATIC_RE = re.compile(r"\bpunch(?:es|ed|ing)?[- ]?in\b|\b\d{2,3}(?:\.\d+)?\s*(?:%|percent\b)")


def _zoom_level(clause: str, m: re.Match, direction: str) -> float:
    """The scale a percentage in a zoom asks for (review RD3). A level is
    ABSOLUTE only when it is one ("to 150%", or a bare 120 % on a zoom in);
    with a direction it is RELATIVE: "zoom in 20%", "punch in 20%" and "in by
    20%" are 120 %, "zoom out 20%" / "out by 20%" 80 %. It was always the
    number itself: "punch in 20% on the third clip" shrank it to a fifth."""
    pct = float(m.group(1))
    before = clause[:m.start()]
    to_level = re.search(r"\bto\s*$", before) is not None
    by = re.search(r"\bby\s*$", before) is not None
    if direction == "out":
        level = pct / 100.0 if to_level else 1.0 - pct / 100.0
    elif to_level and pct >= 100 or (not by and pct >= 100):
        level = pct / 100.0
    else:
        level = 1.0 + pct / 100.0
    return round(min(5.0, max(0.1, level)), 3)


def _zoom(hit: G.IntentHit, c: S.Slots) -> dict[str, Any]:
    clause = hit.clause
    out: dict[str, Any] = {"clip_ref": clip_ref(hit),
                           "direction": "out" if _ZOOM_OUT_RE.search(clause) else "in"}
    m = _PCT_RE.search(clause)
    if m:
        out["scale"] = _zoom_level(clause, m, out["direction"])
        # "to 80%": a level the user named, whichever way it goes
        out["absolute"] = re.search(r"\bto\s*$", clause[:m.start()]) is not None
    if _STATIC_RE.search(clause) and not re.search(r"\bslow(?:ly)?\b|\bgradual", clause):
        out["style"] = "static"
    elif _SLOW_RE.search(clause) or "scale" not in out:
        out["style"] = "slow"
    return out


_DEG_RE = re.compile(r"(-?\d{1,3})\s*(?:°|degrees?|deg)\b")


def _rotate(hit: G.IntentHit, c: S.Slots) -> dict[str, Any]:
    clause = hit.clause
    deg: float | None = None
    m = _DEG_RE.search(clause)
    if m:
        deg = float(m.group(1))
    elif re.search(r"\bupside down\b", clause):
        deg = 180.0
    elif re.search(r"\bsideways\b|\bclockwise\b", clause):
        deg = 90.0
    if deg is not None and re.search(r"\b(?:counter[- ]?clockwise|anti[- ]?clockwise|to the left|left)\b", clause):
        deg = -abs(deg)
    return {"clip_ref": clip_ref(hit), "degrees": deg}


_PROP_RE = (
    (re.compile(r"\bcontrast\w*|\bflatter\b|\bflat\b"), "contrast"),
    (re.compile(r"\bsaturat\w*|\bcolou?rful\b|\bvibran\w*|\bdull\b|\bwashed out\b"), "saturation"),
    (re.compile(r"\bbright\w*|\bdark\w*|\blight(?:er)?\b|\bdim\w*|\bexposure\b"), "brightness"),
)
_DOWN_RE = re.compile(r"\b(?:lower|reduce|decrease|drop|lessen|less|turn down|tone down|dial (?:down|back)|darker|darken|"
                      r"dimmer|flatter|desaturate|down|too bright|oversaturated|too saturated)\b")
_ADJ_PCT_RE = re.compile(r"\bby\s+(\d{1,3}(?:\.\d+)?)\s*(?:%|percent\b)")


def _adjust(hit: G.IntentHit, c: S.Slots) -> dict[str, Any]:
    clause = hit.clause
    prop = next((v for rx, v in _PROP_RE if rx.search(clause)), "brightness")
    down = bool(_DOWN_RE.search(clause))
    # "too dark" / "it looks dull" ask for the opposite of the adjective
    if re.search(r"\btoo (?:dark|dull|flat|washed out)\b|\b(?:looks?|is) (?:\w+ )?(?:dark|dull|flat|washed out)\b", clause):
        down = False
    out: dict[str, Any] = {"property": prop, "change": "down" if down else "up",
                           "clip_ref": G.clip_ref_of(clause)}
    m = _ADJ_PCT_RE.search(clause)
    if m:
        out["amount"] = float(m.group(1)) / 100.0
    return out


# ------------------------------------------------ wave E (F4b) readers

_SECONDS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)\b")
_BY_RE = re.compile(r"\bby\s+\d|\b(?:off|from)\s+(?:the\s+)?(?:end|start|beginning|front|head|tail\s+of\s+)?"
                    r"|\b\d+(?:\.\d+)?\s*(?:s|sec|secs|seconds?)\s+(?:shorter|longer|off|from)\b")
_GROW_RE = re.compile(r"\b(?:extend|lengthen|stretch|longer)\b")
_HEAD_RE = re.compile(r"\b(?:start|beginning|front|head)\s+of\b")


def _length(hit: G.IntentHit, c: S.Slots) -> dict[str, Any]:
    """A clip's new timeline length ("to 2 seconds") or a change of it ("by
    1 s", "2 seconds off the end of", "2 seconds longer") — `_delta` is
    signed; `_edge` says which end moves (the tail unless the clause names
    the start)."""
    clause = hit.clause
    m = _SECONDS_RE.search(clause)
    out: dict[str, Any] = {"clip_ref": clip_ref(hit)}
    if not m:
        return out
    n = float(m.group(1))
    if _BY_RE.search(clause):
        out["_delta"] = n if _GROW_RE.search(clause) else -n
        out["_edge"] = "head" if _HEAD_RE.search(clause) else "tail"
    else:
        out["seconds"] = n
    return out


#: What "remove the ___" names (wave E, F4b), in the order the nouns are tried.
_REMOVE_WHAT: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b(?:captions?|subtitles?|subs)\b"), "captions"),
    (re.compile(r"\b(?:transitions?|cross[- ]?fades?|dissolves?)\b"), "transition"),
    (re.compile(r"\b(?:filters?|luts?|looks?|colou?r grade|colou?r grading|grades?|grading)\b"), "filter"),
    (re.compile(r"\b(?:text|titles?|lower thirds?|hooks?|headlines?)\b"), "text"),
)
_PLURAL_REMOVE_RE = re.compile(r"\b(?:all|every|both|those|these)\b|\b(?:captions|subtitles|subs|transitions|crossfades|"
                               r"cross fades|dissolves|filters|luts|looks|titles|lower thirds|hooks|headlines|grades)\b")
_GRADE_RE = re.compile(r"\b(?:colou?r grade|colou?r grading|grades?|grading)\b")
_WHICH_TR_RE = re.compile(r"\b(first|last|final|opening|closing)\s+(?:transition|cross[- ]?fade|dissolve)\b")


#: Effects-panel effect words → the effect types they name (review RE).
_FX_TYPES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bvignett(?:e|ing)\b"), "vignette"), (re.compile(r"\bgrain\b"), "grain"),
    (re.compile(r"\bvintage\b"), "vintage"), (re.compile(r"\bvhs\b"), "vhs"), (re.compile(r"\bglow\b"), "glow"),
    (re.compile(r"\brgb[- ]split\b|\bchromatic\b"), "rgb_split"), (re.compile(r"\bsharpen"), "sharpen"),
    (re.compile(r"\bblur effect\b"), "blur"), (re.compile(r"\bflip h\b"), "hflip"), (re.compile(r"\bflip v\b"), "vflip"),
)


#: "the text that says subscribe", "the title saying hello" (review RE).
_SAYS_RE = re.compile(r"\b(?:that|which)\s+(?:says|reads|said|read)\s+(.{1,60})$|\bsaying\s+(.{1,60})$")


#: Transition words that name the NOUN, not one look ("remove the dissolve"
#: is any transition; a stored "crossdissolve" must not make it miss).
_GENERIC_TRANSITION_WORDS = frozenset({"fade", "dissolve", "crossfade", "crossdissolve", "cut", "none"})


def _transition_type_in(clause: str) -> str | None:
    """The one transition LOOK a removal names ("take the glitch transition
    out", "remove the wipe"), canonical, or None (Final QA: the look was
    ignored, so a named transition asked "which one?" or took them all)."""
    from ...render import transitions as T
    for name in sorted(T.all_names(), key=len, reverse=True):
        if name in _GENERIC_TRANSITION_WORDS:
            continue
        if re.search(rf"\b{re.escape(name)}\b", clause):
            return T.canonical(name)
    return None


def _remove(hit: G.IntentHit, c: S.Slots) -> dict[str, Any]:
    clause = hit.clause
    what = next((v for rx, v in _REMOVE_WHAT if rx.search(clause)), "other")
    fx = [t for rx, t in _FX_TYPES if rx.search(clause)]
    said = _SAYS_RE.search(clause)
    if what == "other" and not fx and re.search(r"\bglitch(?:es|y)?\b", clause):
        # Final QA: "remove the glitch effect" (a 0.8.0 release-notes phrase)
        # refused — Glitch is a TRANSITION in the app (TransitionsPanel
        # "Glitch/Stylised"), or the RGB Split effect; the expander picks the
        # one the timeline has.
        return {"what": "other", "_glitch": True, "all": True}
    if what == "other" and not fx and re.search(r"\b(?:all|every|any)\s+(?:the\s+|of\s+the\s+)?effects\b"
                                               r"|\beffects\s+(?:from|off|on)\b", clause):
        # Final QA r2: "clear all effects from clip 2" was refused (and told
        # the user to press Delete) although remove_effects exists.
        return {"what": "filter", "_all_fx": True, "clip_ref": G.clip_ref_of(clause), "all": True}
    if what == "other" and not fx and re.search(r"\b(?:voice[- ]?overs?|narrations?|vo|voice\s+track)\b", clause):
        # Final QA: "remove the voiceover" — the voice-over lane's clips.
        return {"what": "other", "_voiceover": True, "all": True}
    if fx and what in ("filter", "other"):
        what = "filter"                       # an Effects-panel effect by name
    out: dict[str, Any] = {"what": what, "all": bool(_PLURAL_REMOVE_RE.search(clause))}
    if fx:
        out["_fx"] = fx
    if what == "filter":
        out["clip_ref"] = G.clip_ref_of(clause)
        out["_grade"] = bool(_GRADE_RE.search(clause))
        out["_look"] = c.look                  # "the black and white filter" → mono.cube
    elif what == "transition":
        from .planner import _at_seconds
        out["_seam"] = seam_index(clause)
        w = _WHICH_TR_RE.search(clause)
        out["_which"] = {"first": "first", "opening": "first"}.get(w.group(1), "last") if w else None
        out["_at"] = _at_seconds(clause)
        out["_type"] = _transition_type_in(clause)
    elif what == "text":
        out["_role"] = ("lower_third" if re.search(r"\blower thirds?\b", clause)
                        else "hook" if re.search(r"\bhooks?\b", clause) else None)
        if c.quoted_text:
            out["text"] = c.quoted_text[0]
        elif said:
            out["text"] = (said.group(1) or said.group(2)).strip(" .!?")   # "the text that says subscribe"
        else:
            # Final QA: "delete the Summer Trip title" — the words before the
            # noun are the text's own (not an ordinal or a position).
            m = _NAMED_TEXT_RE.search(clause)
            if m and not _NOT_A_TEXT_NAME.search(m.group(1)):
                out["text"] = m.group(1).strip()
    return out


_NAMED_TEXT_RE = re.compile(r"\b(?:the|my|that|this)\s+((?:[\w'-]+\s+){1,4}?)(?:text|title|heading|headline"
                            r"|lower[- ]?third|label|hook|super)s?\b")
_NOT_A_TEXT_NAME = re.compile(r"\b(?:first|second|third|last|final|opening|closing|main|big|small|top|bottom|new"
                              r"|old|current|only|all|every|both|black|white|and|b|w|on|in|of|at|from)\b")


def _flip(hit: G.IntentHit, c: S.Slots) -> dict[str, Any]:
    """Which axis ("mirror" / "flip" / "horizontally" = horizontal, CapCut's
    Mirror; "vertically" = vertical) and whether it goes ON, OFF ("unflip",
    "flip it back", "remove the mirror") or toggles (None)."""
    clause = hit.clause
    vertical = bool(re.search(r"\bvertical(?:ly)?\b|\btop to bottom\b|\bbottom to top\b", clause))
    horizontal = bool(re.search(r"\bhorizontal(?:ly)?\b|\bleft to right\b|\bright to left\b|\bsideways\b"
                                r"|\bmirror", clause))
    axis = "both" if vertical and horizontal else ("vertical" if vertical else "horizontal")
    off = bool(re.search(r"\bun-?flip|\bun-?mirror|\bflip\w*\s+(?:it\s+|this\s+|that\s+|[\w ]{0,24}?\s+)?back\b"
                         r"|\bno longer (?:flip|mirror)|\b(?:remove|undo|turn off|take off)\s+(?:the\s+)?(?:flip|mirror)",
                         clause))
    out: dict[str, Any] = {"clip_ref": clip_ref(hit), "axis": axis, "on": False if off else None}
    # review RE: "mirror the overlay" asked "which clip?" (and, with a main-
    # track clip selected, would have mirrored THAT one)
    if re.search(r"\b(?:stickers?|emojis?)\b", clause):
        out["_target"] = "sticker"
    elif re.search(r"\b(?:overlays?|pips?|picture[- ]in[- ]picture|top\s+(?:clip|layer|video))\b", clause):
        out["_target"] = "overlay"
    return out


def _canvas_read(hit: G.IntentHit, c: S.Slots) -> dict[str, Any]:
    from .canvas_expanders import read_canvas
    return read_canvas(hit, c)


def _blend_read(hit: G.IntentHit, c: S.Slots) -> dict[str, Any]:
    from .canvas_expanders import read_blend
    return read_blend(hit, c)


def _voice_read(hit: G.IntentHit, c: S.Slots) -> dict[str, Any]:
    from .voice_expanders import read_voice
    return read_voice(hit, c)


def _anim_read(hit: G.IntentHit, c: S.Slots) -> dict[str, Any]:
    from .anim_expanders import read_animation
    return read_animation(hit, c)


READERS: dict[str, Callable[[G.IntentHit, S.Slots], dict[str, Any]]] = {
    "delete_clip": _one, "duplicate": _one, "move_clip": _move, "zoom": _zoom, "rotate": _rotate,
    "adjust": _adjust, "clip_length": _length, "remove_feature": _remove, "flip": _flip,
    # Wave E (F2): CapCut Canvas and blend modes (canvas_expanders.py)
    "canvas": _canvas_read, "blend": _blend_read,
    # Wave E (F3): CapCut's voice changer (voice_expanders.py)
    "voice_effect": _voice_read,
    # Wave E (F1): CapCut clip animations (anim_expanders.py)
    "animation": _anim_read,
}


# ----------------------------------------------------- existing recipes

_AT_END_RE = re.compile(r"\b(?:last|final|end|ending)\s+frame\b|\bat the (?:very )?end\b")
_AT_START_RE = re.compile(r"\b(?:first|opening|start(?:ing)?)\s+frame\b|\bat the (?:very )?(?:start|beginning)\b")
_MUSIC_RE = re.compile(r"\b(?:music|song|track|bed|bgm|soundtrack|tune|score)\b")
_SEAM_PAIR_RE = re.compile(rf"\bbetween\s+(?:the\s+)?({G.ORDINAL})\s+(?:and|&)\s+(?:the\s+)?({G.ORDINAL})\s+{G.CLIP_NOUN}"
                           rf"|\bbetween\s+{G.CLIP_NOUN}\s+(\d{{1,2}})\s+(?:and|&)\s+(\d{{1,2}})\b"
                           rf"|\bafter\s+(?:the\s+)?({G.ORDINAL})\s+{G.CLIP_NOUN}"
                           rf"|\bafter\s+{G.CLIP_NOUN}\s+(\d{{1,2}})\b")
_ORD_NUM = {"first": 1, "opening": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
            "eighth": 8, "ninth": 9, "tenth": 10}


def _ord_num(word: str | None) -> int | None:
    if not word:
        return None
    w = word.strip()
    if w in _ORD_NUM:
        return _ORD_NUM[w]
    m = re.match(r"(\d{1,2})", w)
    return int(m.group(1)) if m else None


#: Final QA r3: "between the first two clips" (1) / "the last two clips" (-1),
#: "between clip 1 and clip 2", "between the 2nd and 3rd" (no noun).
_SEAM_FIRST_TWO_RE = re.compile(rf"\bbetween\s+(?:the\s+)?(?:first|opening)\s+(?:two|2)\s+{G.CLIP_NOUN}")
_SEAM_LAST_TWO_RE = re.compile(rf"\bbetween\s+(?:the\s+)?(?:last|final|closing)\s+(?:two|2)\s+{G.CLIP_NOUN}")
_SEAM_CLIP_CLIP_RE = re.compile(rf"\bbetween\s+(?:the\s+)?{G.CLIP_NOUN}\s+(\d{{1,2}})\s+(?:and|&)\s+"
                                rf"(?:the\s+)?(?:{G.CLIP_NOUN}\s+)?(\d{{1,2}})\b")
_SEAM_ORD_ORD_RE = re.compile(rf"\bbetween\s+(?:the\s+)?({G.ORDINAL})\s+(?:{G.CLIP_NOUN}\s+)?(?:and|&)\s+"
                              rf"(?:the\s+)?({G.ORDINAL})\b")


def seam_index(clause: str) -> int | None:
    """The seam a transition clause names: "between the first and second
    clip" → 1, "between clips 2 and 3" → 2, "after the second clip" → 2,
    "between the first two clips" → 1, "between the last two clips" → -1."""
    if _SEAM_FIRST_TWO_RE.search(clause):
        return 1
    if _SEAM_LAST_TWO_RE.search(clause):
        return -1
    for rx in (_SEAM_CLIP_CLIP_RE, _SEAM_ORD_ORD_RE):
        pm = rx.search(clause)
        if pm:
            a, b = _ord_num(pm.group(1)), _ord_num(pm.group(2))
            if a is not None and b is not None:
                return min(a, b) if abs(a - b) == 1 else None
    m = _SEAM_PAIR_RE.search(clause)
    if not m:
        return None
    a = _ord_num(m.group(1)) or (int(m.group(3)) if m.group(3) else None)
    b = _ord_num(m.group(2)) or (int(m.group(4)) if m.group(4) else None)
    if a is not None and b is not None:
        return min(a, b) if abs(a - b) == 1 else None
    after = _ord_num(m.group(5)) or (int(m.group(6)) if m.group(6) else None)
    return after


def clip_extras(r: str, hit: G.IntentHit, c: S.Slots) -> dict[str, Any]:
    """Extra slots for the existing recipes: the clip an ordinal names, the
    range an edit covers, a title's time, a transition's seam, a freeze at
    the end, a bare trim range."""
    clause = hit.clause
    if r in ("speed", "reverse", "fade", "color_look", "stabilize", "upscale"):
        ref = G.clip_ref_of(clause)
        out: dict[str, Any] = {"clip_ref": ref} if ref else {}
        if r == "color_look" and not c.look and re.search(r"\bpop\b|\bpoppy\b", clause):
            out["look"] = "punch.cube"           # "make the colours pop"
        if r == "speed" and ref is None:
            rng = _range(c, clause)
            if rng is not None:
                out["_range"] = rng
        return out
    if r in ("mute", "volume"):
        if _MUSIC_RE.search(clause):
            return {}
        ref = G.clip_ref_of(clause)
        if ref is None and _IT_RE.search(clause):
            ref = "$selected"                  # "mute it" with a clip selected
        rng = None if (ref or r == "volume") else _range(c, clause)
        if ref or rng:
            return {"clip_ref": ref, "_range": rng, "target": "voice"}
        return {}
    if r == "freeze":
        if _AT_START_RE.search(clause):
            return {"at": 0.0}
        return {"_at_end": True} if _AT_END_RE.search(clause) else {}
    if r == "trim":
        return {} if c.range else ({"range": bare_range(clause)} if bare_range(clause) else {})
    if r == "title":
        from .planner import _at_seconds
        at = _at_seconds(clause)
        return {"at": at} if at is not None else {}
    if r == "transitions":
        out = {"_seam_index": seam_index(clause)}
        if c.transition_type is None and re.search(r"\bflash\b", clause):
            out["type"] = "fadewhite"          # CapCut's Flash is a white flash
        elif c.transition_type is None and out["_seam_index"] is not None:
            # "a fade between the second and third clip": the look word names
            # the type even where the seam words are ordinals
            out["type"] = S.transition_type_of(clause)
        return out
    return {}


__all__ = ["READERS", "clip_extras", "clip_ref", "bare_range", "seam_index"]
