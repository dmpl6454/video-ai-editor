"""CapCut Canvas and blend modes from a key-free prompt (wave E, lane F2).

"blur the background", "make the background black", "fill the bars with
white", "use this image as the background", "set the overlay to screen",
"multiply blend the top clip". The ONE table is `edl/canvas_blend.py`; the
tools are `set_canvas_background` and `set_blend_mode`.

The rules `clip_expanders` keeps hold here too:

  * Everything is bound at plan time from `TimelineFacts`: the main-track
    clips (a canvas with no clip named is CapCut's "Apply to all" — every
    main-track clip, one undo step), the overlay clips (`clips` on a `vN`
    lane above v1), the pictures the project holds (`uploads_images`).
  * What the prompt leaves open is ASKED, never guessed: which overlay when
    there are several and none is selected, which blend mode when none is
    named, which picture when several are imported, and whether blur / a
    colour / a picture was meant. The expansion is then a question with no
    steps, so the plan commits nothing.
  * Each step carries a postcondition measured on the EDL the renderer reads
    (`canvas_bg_set`, `blend_is`).
"""
from __future__ import annotations

import re
from typing import Any

from ...edl import canvas_blend as CB
from . import grammar as G
from . import slots as S
from .clip_expanders import _label, bind_clip
from .facts import TimelineFacts
from .recipes import Context, Expansion, Intent, ask, pc, placeholder, step
from .schema import STAGE_LOOK, STAGE_REFRAME
from . import canvas_vocab as _CV
from .canvas_vocab import BLEND_WORDS, OVERLAY_NOUN, COLOUR_WORDS as _COLOUR_WORDS, PICTURE as _PICTURE

# --------------------------------------------------------------------------
# 2. Slot readers (clip_slots.READERS)
# --------------------------------------------------------------------------

_BLUR_LEVEL_WORDS = (
    (re.compile(r"\b(?:slight(?:ly)?|light(?:ly)?|subtle|a (?:little|bit)|soft(?:ly)?|gentle|gently)\b"), 1),
    (re.compile(r"\b(?:medium|moderate(?:ly)?)\b"), 2),
    (re.compile(r"\b(?:strong(?:ly)?|lots of|a lot|really|very|more)\b"), 3),
    (re.compile(r"\b(?:heavy|heavily|max(?:imum)?|fully|completely|extreme(?:ly)?|super)\b"), 4),
)
_BLUR_NUM = re.compile(r"\b(?:blur|level|strength)\s*(?:of\s+|to\s+|at\s+)?([1-4])\b|\b([1-4])\s*(?:/\s*4)?\s+blur\b")
_HEX_RE = re.compile(r"#?\b([0-9a-f]{6})\b")
_FILE_RE = re.compile(r"([\w()+-][\w.()+-]*\.(?:png|jpe?g|webp|heic|heif|bmp|tiff?))\b", re.I)
_ALL_RE = re.compile(r"\b(?:all|every|each|whole|entire)\b(?:\s+(?:the\s+)?(?:clips?|video|timeline|project|shots?))?")
_NONE_RE = re.compile(r"\b(?:reset|clear|remove|no\s+more|no|back\s+to\s+black\s+bars|black\s+bars\s+again|undo)\b")
#: "remove the background blur", "no more canvas colour": the background goes.
_OFF_RE = re.compile(r"^(?:please\s+)?(?:remove|reset|clear|turn\s+off|get\s+rid\s+of|take\s+(?:off|away)|undo|drop)\b"
                     r"|\bno\s+(?:more\s+)?(?:canvas|back\s*ground)\b|\bback\s+to\s+black\s+bars\b")


def _colour_in(clause: str) -> str | None:
    # "fill the black bars with white": "black bars" names the letterbox
    clause = re.sub(r"\bblack\s+(?:bars|borders?|edges)\b", " bars", clause)
    m = _HEX_RE.search(clause)
    if m:
        return "#" + m.group(1).upper()
    m = re.search(rf"\b{_COLOUR_WORDS}\b", clause)
    if m:
        return CB.normalize_color(m.group(0))
    return None


def read_canvas(hit: G.IntentHit, c: S.Slots) -> dict[str, Any]:
    """kind / color / blur / image / clip_ref / all, from ONE clause."""
    clause = hit.clause
    out: dict[str, Any] = {}
    if _OFF_RE.search(clause):
        out["kind"] = "none"
    elif re.search(r"\bblur", clause):
        out["kind"] = "blur"
        m = _BLUR_NUM.search(clause)
        if m:
            out["blur"] = int(m.group(1) or m.group(2))
        else:
            lv = next((n for rx, n in _BLUR_LEVEL_WORDS if rx.search(clause)), None)
            if lv is not None:
                out["blur"] = lv
    elif re.search(rf"\b{_PICTURE}\b", clause) or _FILE_RE.search(clause):
        out["kind"] = "image"
        m = _FILE_RE.search(clause)
        name = (c.quoted_text[0] if c.quoted_text else None) or (m.group(1).strip() if m else None)
        if name:
            out["image"] = name
    else:
        col = _colour_in(clause)
        if col is not None:
            out["kind"] = "color"
            out["color"] = col
        elif _NONE_RE.search(clause):
            out["kind"] = "none"
        elif re.search(r"\bcolou?r(?:ed)?\b", clause):
            out["kind"] = "color"
    ref = G.clip_ref_of(clause)
    if ref is not None and ref != "$v1_all":
        out["clip_ref"] = ref
    elif ref == "$v1_all" or _ALL_RE.search(clause):
        out["all"] = True
    return out


#: "the overlay" / "the overlay's" / "the overlay clip" as the TARGET noun —
#: blanked before a mode is looked for (review RE: "set the overlay blend
#: mode to multiply" committed Overlay, reading the noun as the mode).
_OVERLAY_TARGET = re.compile(r"\b(?:the|this|that|my|each|every|all(?:\s+the)?)\s+overlay(?:'s|s)?"
                             r"(?:\s+(?:clips?|videos?|layers?))?\b|\boverlay(?:'s)?\s+(?:clips?|videos?|layers?)\b"
                             r"|^overlay(?:'s)?\b|\boverlay's\b")


def blend_mode_in(clause: str) -> str | None:
    """The blend mode a clause names (the word after to/into/as/with, or the
    one before "blend"/"mode"), or None. "overlay" as the NOUN of "set the
    overlay to screen" is blanked first: only a mode in a mode position
    counts, and a mode after "to / into / as / mode" beats an earlier word."""
    clause = _OVERLAY_TARGET.sub(" the clip ", clause)
    pats = (
        # unambiguous positions first: "use linear dodge on the overlay"
        # must not read the NOUN "overlay" after "on the" as the mode
        rf"^(?:please\s+)?(?:use|apply|try)\s+(?:a\s+|an\s+|the\s+)?({BLEND_WORDS})\b",
        rf"\bblend(?:ing)?(?:[- ]mode)?\s+(?:back\s+)?(?:to\s+|of\s+|=\s*|as\s+|into\s+)(?:a\s+|the\s+)?({BLEND_WORDS})\b",
        rf"\bblend(?:ing)?[- ]mode\s+({BLEND_WORDS})\b",
        rf"\b({BLEND_WORDS})\s+(?:blend(?:ed|ing|s)?|mode)\b",
        rf"\b(?:make|turn|set)\s+(?:the\s+|this\s+|that\s+|my\s+)?{OVERLAY_NOUN}\s+({BLEND_WORDS})\b",
        rf"\b(?:to|into|as|using|with|in|on)\s+(?:a\s+|the\s+|an\s+)?({BLEND_WORDS})(?:\s+(?:blend(?:ing)?|mode|blend\s+mode))?\b",
        rf"^(?:a\s+|the\s+)?({BLEND_WORDS})\b",
    )
    for p in pats:
        for m in re.finditer(p, clause):
            word = m.group(1)
            # "set the overlay to …": the noun, not the mode
            if word == "overlay" and re.match(rf"\s*(?:clip|video|layer)?\s*(?:to|into|as)\s+", clause[m.end():]):
                continue
            try:
                return CB.resolve_blend(word)
            except ValueError:
                continue
    return None


_NTH_OVERLAY = re.compile(r"\b(first|second|third|fourth|last|top|bottom)\s+(?:overlay|pip|layer)\b")
_THIS_RE = re.compile(r"\b(?:this|that|selected|current)\s+(?:clip|overlay|pip|layer|video|one)\b|\b(?:it|this|that)$")


def read_blend(hit: G.IntentHit, c: S.Slots) -> dict[str, Any]:
    clause = hit.clause
    out: dict[str, Any] = {}
    mode = blend_mode_in(clause)
    if re.search(_CV.BLEND_OFF, clause):
        # Final QA r3: "remove the screen blend" re-applied Screen and said
        # "the overlay now blends as Screen" — a removal is Normal.
        mode = "normal"
    if mode is not None:
        out["mode"] = mode
    m = _NTH_OVERLAY.search(clause)
    if m:
        out["overlay"] = m.group(1)
    elif _THIS_RE.search(clause):
        out["clip_ref"] = "$selected"
    elif re.search(r"\b(?:all|every|each)\s+(?:the\s+)?(?:overlays?|pips?|layers?)\b", clause):
        out["all"] = True
    return out


# --------------------------------------------------------------------------
# 3. Expanders (expanders.EXPANDERS)
# --------------------------------------------------------------------------

def _ask(q: str) -> Expansion:
    return Expansion(notes=(q,))


_OVERLAY_LANE = re.compile(r"^v(\d+)$")


def overlay_clips(f: TimelineFacts) -> list[tuple[str, str]]:
    """(clip id, lane) of every overlay video clip, bottom lane first, then
    by start — what "the top clip" / "the overlay" can mean."""
    rows = []
    for c in f.clips:
        m = _OVERLAY_LANE.match(c.track or "")
        if m and c.track != "v1":
            rows.append((int(m.group(1)), c.start, c.id, c.track))
    rows.sort()
    return [(cid, lane) for _n, _s, cid, lane in rows]


def _blend_names() -> str:
    return ", ".join(b.label for b in CB.BLENDS if b.id != "normal")


def x_blend(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    """CapCut blend mode on the overlay clip(s) the prompt names."""
    ov = overlay_clips(f)
    if not ov:
        return Expansion(notes=("There is no overlay clip to blend — drag a clip onto a track above the main "
                                "video first, then name the blend mode.",))
    ids = [cid for cid, _lane in ov]
    targets: list[str]
    which = it.get("overlay")
    if it.get("all"):
        targets = ids
    elif it.get("clip_ref") == "$selected":
        if f.selection in ids:
            targets = [f.selection]
        else:
            return _ask("Which overlay? The selected clip is not an overlay — select the clip on a track "
                        "above the main video, or say 'the top clip'.")
    elif which in ("top", "last"):
        top_lane = max(int(_OVERLAY_LANE.match(lane).group(1)) for _cid, lane in ov)  # type: ignore[union-attr]
        on_top = [cid for cid, lane in ov if lane == f"v{top_lane}"]
        if len(on_top) == 1 or which == "last":
            targets = [on_top[-1]]
        elif f.selection in on_top:
            targets = [f.selection]
        else:
            return _ask(f"Which clip on the top track? There are {len(on_top)} — select one and say "
                        f"'this clip', or say 'all the overlays'.")
    elif which in ("first", "bottom", "second", "third", "fourth"):
        n = {"first": 1, "bottom": 1, "second": 2, "third": 3, "fourth": 4}[which]
        if n > len(ov):
            return _ask(f"Which overlay? There {'is only one' if len(ov) == 1 else f'are only {len(ov)}'}.")
        targets = [ov[n - 1][0]]
    elif f.selection in ids:
        targets = [f.selection]
    elif len(ids) == 1:
        targets = ids
    else:
        return _ask(f"Which overlay? There are {len(ids)} overlay clips — select one and say 'this clip', "
                    f"or say 'the top clip' or 'all the overlays'.")
    mode = it.get("mode")
    if mode is None:
        return _ask(f"Which blend mode? {_blend_names()} — say like 'set the overlay to screen'.")
    label = CB.blend_of(mode).label
    who = "the overlay" if len(targets) == 1 else f"{len(targets)} overlays"
    return Expansion(
        steps=(step("set_blend_mode", STAGE_LOOK, f"{label} blend on {who}",
                    **({"clip_id": targets[0]} if len(targets) == 1 else {"clip_ids": targets}), mode=mode),),
        postconditions=(pc("blend_is", f"the overlay blends as {label}",
                           clip_id=targets[0] if len(targets) == 1 else targets, mode=mode),),
        notes=(f"{who} now blend{'s' if len(targets) == 1 else ''} as {label}",))


def _image_choice(it: Intent, f: TimelineFacts) -> tuple[str | None, str | None, list[tuple[str, str]]]:
    """(path, question, options): the picture a canvas uses."""
    name = it.get("image")
    pictures = list(f.uploads_images)
    if name:
        n = str(name).strip().lower()
        exact = [p for p in pictures + sorted(f.allowed_paths) if p.replace("\\", "/").rsplit("/", 1)[-1].lower() == n]
        if exact:
            return exact[0], None, []
        loose = [p for p in pictures if n in p.replace("\\", "/").rsplit("/", 1)[-1].lower()]
        if len(loose) == 1:
            return loose[0], None, []
    if f.selection:
        sel = next((p for p in pictures if f.selection in p), None)
        if sel:
            return sel, None, []
    if len(pictures) == 1 and not name:
        return pictures[0], None, []
    if not pictures:
        return None, ("There is no picture in this project yet — import one (Media → Import), then say "
                      "'use <its name> as the background'."), []
    opts = [(p, p.replace("\\", "/").rsplit("/", 1)[-1]) for p in pictures[:12]]
    q = ("Which picture? " + ("I could not find " + repr(name) + ". " if name else "")
         + "Reply with its name: " + ", ".join(lbl for _p, lbl in opts[:5]) + ("…" if len(opts) > 5 else ""))
    return None, q, opts


def x_canvas(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    """CapCut Canvas on one named main-track clip, or (the default when none
    is named) every main-track clip — "Apply to all" in one step."""
    if not f.v1_clip_ids:
        return Expansion(notes=("There is no clip on the main track yet — add one first.",))
    kind = it.get("kind")
    if kind is None:
        return _ask("Blur, a colour or a picture behind the video? Say like 'blur the background', "
                    "'make the background white' or 'use my photo as the background'.")
    if kind != "none" and f.source_aspect and f.source_aspect == f.aspect:
        # review RE: "blur the background" on a 16:9 clip in a 16:9 project
        # committed an invisible canvas on every clip — nothing is letterboxed,
        # so the person most likely meant something else
        return _ask(f"Your clips fill the {f.aspect} frame, so there are no bars for a background to fill. "
                    "Did you mean to blur the video itself (say 'add a blur effect to the first clip'), or to "
                    "change the shape first (say 'make it vertical')?")
    ref = it.get("clip_ref")
    args: dict[str, Any] = {}
    if ref is not None and not it.get("all"):
        cid, q = bind_clip(ref, f)
        if q:
            return _ask(q)
        ids = list(f.v1_clip_ids)
        cid = {"$v1_first": ids[0], "$v1_last": ids[-1]}.get(cid, cid)
        if cid == "$v1_all":
            args["all"] = True
            who, target = "every main-track clip", "$v1_all"
        elif cid not in ids:
            return _ask("The canvas background belongs to main-track clips — that one is an overlay. "
                        "Name a clip on the main track, or say 'blur the background' for all of them.")
        else:
            args["clip_id"] = cid
            who, target = _label(cid, f), cid
    else:
        args["all"] = True
        who, target = "every main-track clip", "$v1_all"
    questions: tuple = ()
    check: dict[str, Any] = {"clip_id": target, "type": kind}
    if kind == "blur":
        lv = it.get("blur")
        level = int(lv) if lv is not None else CB.CANVAS_BLUR_DEFAULT
        args.update(type="blur", blur=level)
        check["blur"] = level
        what = f"a {CB.blur_level(level).label.lower()} blur of the clip"
    elif kind == "color":
        col = it.get("color")
        if col is None:
            return _ask("Which colour? Say like 'make the background white' or give a hex like #1E88E5.")
        args.update(type="color", color=col)
        check["color"] = col
        what = f"colour {col}"
    elif kind == "image":
        path, q, opts = _image_choice(it, f)
        if q and not opts:
            return Expansion(notes=(q,))
        if q:
            questions = (ask("canvas_image", q, kind="choice", options=opts),)
            path = placeholder("canvas_image")
        args.update(type="image", image=path)
        what = "the picture" if q else f"the picture {str(path).replace(chr(92), '/').rsplit('/', 1)[-1]}"
    else:
        args.update(type="none")
        check["type"] = None
        what = "black bars"
    notes = [f"canvas background: {what} on {who}"]
    return Expansion(
        steps=(step("set_canvas_background", STAGE_REFRAME, f"{what} behind {who}", **args),),
        postconditions=(pc("canvas_bg_set", f"the letterbox shows {what}", **check),),
        questions=questions, notes=tuple(notes))


__all__ = ["read_canvas", "read_blend",
           "blend_mode_in", "overlay_clips", "x_blend", "x_canvas"]
