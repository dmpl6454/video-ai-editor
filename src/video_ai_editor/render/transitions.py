"""Transition catalog + name resolver.

ffmpeg's `xfade` filter ships ~58 built-in transitions plus a `custom` mode
that takes a per-pixel `expr`. This module is the single source of truth that
maps the editor's friendly transition names (what Claude and the UI use) to
the actual ffmpeg invocation.

Two things it fixes / provides:

  1. The old schema advertised `slide`, `zoom`, `glitch`, `whip`, `spin` but
     passed them straight to `xfade=transition=`, which only `fade`/`dissolve`
     are valid for. The other five crashed the render. Every name here now
     resolves to something ffmpeg actually accepts.

  2. Breadth. All 58 of ffmpeg's natives, plus custom-expr looks it has no
     equivalent for (shaped and luma-driven wipes), plus the whip family.

Three kinds of entry, and the difference matters when adding one:
  - NATIVE        — a real ffmpeg xfade transition.
  - CUSTOM_EXPRS  — `xfade=custom` with a per-pixel expr. Can express any MASK
                    (shape, band, hash, luma key) but CANNOT sample another
                    coordinate, so no zoom/rotation/smear.
  - POST_FILTERS  — a real filter on the xfade output, gated to the transition
                    window. This is the only way to get motion blur, which is
                    what makes a whip pan a whip pan.

ALIASES are synonyms, NOT extra effects. `catalog()` reports the two counts
separately: publishing only the combined number is what made a tester try the
list and find `mosaic`, `pixelate` and `pixel` were all the same transition.

`resolve_transition(name)` → (xfade_transition_arg, expr_or_None).
  - native:  ("slideleft", None)         → xfade=transition=slideleft
  - custom:  ("custom", "<expr string>") → xfade=transition=custom:expr='...'
`post_filter(name, start, end, w, h)` → the extra filter, or None.
`default_duration(name)` → the seconds a transition takes when the caller
names none (per FAMILY, below); `family_of(name)` → its CapCut-style family;
`entries()` → one product-facing record per distinct look for the UI grid,
the prompt planner and `list_transitions`.
"""
from __future__ import annotations
import logging

_log = logging.getLogger(__name__)

# --- custom-expr transitions --------------------------------------------------
# xfade expr vars: X Y (pixel), W H (dims), P (progress 0→1), A B (the two
# source pixels for the current plane). Expr returns the output pixel value.

# Glitch: per-row pseudo-random slice flicker. floor(Y/6)*12.9898 gives a
# stable per-6px-row hash; sin(P*36) makes it flicker over the transition;
# thresholding A vs B at 0.5 produces digital "tear" slices that resolve to B.
_GLITCH_EXPR = "if(gt(P+0.45*sin(floor(Y/6)*12.9898)*sin(P*36),0.5),B,A)"

# --- shape/luma wipes that ffmpeg has no native transition for ----------------
#
# All 58 of ffmpeg's built-in xfade transitions are already exposed, so the only
# way to add a genuinely NEW look is a custom expr (or a post-filter, below).
# The tester's "the transition quality is far behind CapCut" is mostly this: the
# native set is all linear wipes and fades, with no shaped or texture-driven
# reveals.
#
# What an expr can and cannot do — this is the whole design constraint:
#   • It runs PER PLANE and can only choose or blend the two pixels at the SAME
#     coordinate (A and B). So any MASK is available: shapes, bands, hashes,
#     luma keys.
#   • It CANNOT sample a different coordinate, so no geometric warp — no true
#     zoom, rotation, or motion smear. Those need a real filter (see POST).
# Selecting `if(cond,B,A)` and blending `A*(1-k)+B*k` are both plane-safe;
# anything that mixes the two unevenly per plane would shift colour.
_CUSTOM: dict[str, str] = {
    "glitch": _GLITCH_EXPR,
    # Wavy vertical wipe — the edge ripples as it crosses.
    "wave": "if(gt(P*1.15,X/W+0.06*sin(Y/H*18+P*10)),B,A)",
    # Vertical blinds: 10 bands each wiping left→right together.
    "blinds": "if(lt(mod(X,W/10)/(W/10),P),B,A)",
    # Horizontal bars: the same, rotated.
    "bars": "if(lt(mod(Y,H/10)/(H/10),P),B,A)",
    # Checkerboard: cells resolve in a scattered (hashed) order.
    "checker": "if(gt(P,0.2+0.7*mod(floor(X/(W/10))*7+floor(Y/(H/10))*13,8)/8),B,A)",
    # Luma burn: the bright parts of A dissolve away first, like film burn.
    "burn": "if(gt(P*1.25,A/255*0.7+0.15),B,A)",
    # Diamond iris.
    "diamond": "if(gt(P*1.05,(abs(X-W/2)/(W/2)+abs(Y-H/2)/(H/2))/2),B,A)",
    # Rectangular iris opening from the centre.
    "boxopen": "if(gt(P,max(abs(X-W/2)/(W/2),abs(Y-H/2)/(H/2))),B,A)",
    # Radial ripple spreading out from the centre.
    "ripple": ("if(gt(P*1.2,hypot(X-W/2,Y-H/2)/hypot(W/2,H/2)"
               "-0.08*sin(hypot(X-W/2,Y-H/2)/20-P*12)),B,A)"),
    # Spiral sweep: an angular wipe whose angle is offset by radius, so it
    # reads as rotation. This is what `spin` should always have been — the old
    # mapping to `radial` is a clock wipe, which has no rotational motion.
    "spiral": ("if(gt(P*7,mod(atan2(Y-H/2,X-W/2)+PI+hypot(X-W/2,Y-H/2)/W*3,"
               "2*PI)),B,A)"),
}

CUSTOM_EXPRS: dict[str, str] = _CUSTOM

# --- post filters: applied to the xfade OUTPUT, gated to the transition -------
#
# An expr cannot smear pixels, so a whip-pan (the defining short-form
# transition) was aliased to `smoothleft` — a soft slide with no motion blur,
# which is why it never read as a whip. A directional blur gated to the
# transition window with `enable=` gives the real thing. `gblur` documents
# timeline support, so the blur exists only for the duration of the cut.
# `{start}`/`{end}` are substituted with OUTPUT-timeline seconds by the caller.
#
# `{sigma}` is scaled to the RENDER width by the caller, never a fixed pixel
# count: preview and export deliberately render at different resolutions, so a
# constant sigma would smear the preview far harder than the delivered file —
# the preview/export mismatch this codebase keeps having to design against.
_H_BLUR = ("h", "gblur=sigma={sigma}:sigmaV=0:enable='between(t,{start},{end})'")
_V_BLUR = ("v", "gblur=sigma=0:sigmaV={sigma}:enable='between(t,{start},{end})'")

# (axis, template). The axis picks WHICH dimension scales the blur: a sideways
# whip smears across the width, a vertical one down the height. On a 9:16
# canvas those differ by ~1.8x, so scaling both off the long edge would make a
# horizontal whip nearly twice as strong as its vertical twin.
POST_FILTERS: dict[str, tuple[str, str]] = {
    "whip":      _H_BLUR,
    "whipright": _H_BLUR,
    "whipup":    _V_BLUR,
    "whipdown":  _V_BLUR,
}

# Whip-blur strength as a fraction of the smeared axis. Measured, not guessed:
# at this ratio the mid-transition frame keeps ~4% of a plain slide's detail —
# unmistakably motion — while the rest of the timeline is byte-identical.
WHIP_SIGMA_RATIO = 1 / 45

# --- friendly name → native xfade transition ---------------------------------
# Curated set. Keys are what callers type; values are valid ffmpeg xfade names.
NATIVE: dict[str, str] = {
    # crossfades
    "fade": "fade",
    "fadefast": "fadefast",
    "fadeslow": "fadeslow",
    "fadeblack": "fadeblack",
    "fadewhite": "fadewhite",
    "fadegrays": "fadegrays",
    "dissolve": "dissolve",
    "distance": "distance",
    # directional wipes
    "wipeleft": "wipeleft",
    "wiperight": "wiperight",
    "wipeup": "wipeup",
    "wipedown": "wipedown",
    "wipetl": "wipetl",
    "wipetr": "wipetr",
    "wipebl": "wipebl",
    "wipebr": "wipebr",
    # slides (push)
    "slideleft": "slideleft",
    "slideright": "slideright",
    "slideup": "slideup",
    "slidedown": "slidedown",
    # smooth directional
    "smoothleft": "smoothleft",
    "smoothright": "smoothright",
    "smoothup": "smoothup",
    "smoothdown": "smoothdown",
    # covers / reveals
    "coverleft": "coverleft",
    "coverright": "coverright",
    "coverup": "coverup",
    "coverdown": "coverdown",
    "revealleft": "revealleft",
    "revealright": "revealright",
    "revealup": "revealup",
    "revealdown": "revealdown",
    # shapes
    "circleopen": "circleopen",
    "circleclose": "circleclose",
    "circlecrop": "circlecrop",
    "rectcrop": "rectcrop",
    "radial": "radial",
    "vertopen": "vertopen",
    "vertclose": "vertclose",
    "horzopen": "horzopen",
    "horzclose": "horzclose",
    # slices / blinds
    "hlslice": "hlslice",
    "hrslice": "hrslice",
    "vuslice": "vuslice",
    "vdslice": "vdslice",
    # squeeze / zoom
    "squeezeh": "squeezeh",
    "squeezev": "squeezev",
    "zoomin": "zoomin",
    # texture
    "pixelize": "pixelize",
    "hblur": "hblur",
    # wind smears
    "hlwind": "hlwind",
    "hrwind": "hrwind",
    "vuwind": "vuwind",
    "vdwind": "vdwind",
    # whips — a slide plus a directional blur burst (see POST_FILTERS)
    "whip": "slideleft",
    "whipright": "slideright",
    "whipup": "slideup",
    "whipdown": "slidedown",
    # diagonals
    "diagtl": "diagtl",
    "diagtr": "diagtr",
    "diagbl": "diagbl",
    "diagbr": "diagbr",
}

# --- aliases: the names a human actually types → canonical catalog name -------
ALIASES: dict[str, str] = {
    # generic → a sensible default direction
    "slide": "slideleft",
    "push": "slideleft",
    "wipe": "wiperight",
    "smooth": "smoothright",
    "cover": "coverleft",
    "reveal": "revealright",
    "zoom": "zoomin",
    "zoomout": "circleclose",
    "circle": "circleopen",
    "blur": "hblur",
    "pixel": "pixelize",
    "pixelate": "pixelize",
    "mosaic": "pixelize",
    "wind": "hrwind",
    "clock": "radial",
    "iris": "circleopen",
    "flash": "fadewhite",
    "flashwhite": "fadewhite",
    "flashblack": "fadeblack",
    "blackout": "fadeblack",
    "whiteout": "fadewhite",
    "grayscale": "fadegrays",
    "desaturate": "fadegrays",
    "crossfade": "fade",
    "crossdissolve": "dissolve",
    # stylized aliases that resolve to closest reliable look
    "whippan": "whip",
    "spin": "spiral",         # a real rotational sweep, not a clock wipe
    "swirl": "spiral",
    "venetian": "blinds",
    "stripes": "bars",
    "luma": "burn",
    "filmburn": "burn",
    "boxin": "boxopen",
}

# --- categories (for list_transitions UI grouping) ---------------------------
CATEGORIES: dict[str, list[str]] = {
    "fades": ["fade", "fadefast", "fadeslow", "dissolve", "fadeblack",
              "fadewhite", "fadegrays", "distance"],
    "wipes": ["wipeleft", "wiperight", "wipeup", "wipedown",
              "wipetl", "wipetr", "wipebl", "wipebr",
              "diagtl", "diagtr", "diagbl", "diagbr"],
    "slides": ["slideleft", "slideright", "slideup", "slidedown",
               "smoothleft", "smoothright", "smoothup", "smoothdown"],
    "covers": ["coverleft", "coverright", "coverup", "coverdown",
               "revealleft", "revealright", "revealup", "revealdown"],
    "shapes": ["circleopen", "circleclose", "circlecrop", "rectcrop",
               "radial", "vertopen", "vertclose", "horzopen", "horzclose"],
    "slices": ["hlslice", "hrslice", "vuslice", "vdslice",
               "hlwind", "hrwind", "vuwind", "vdwind"],
    "zoom": ["squeezeh", "squeezev", "zoomin"],
    "texture": ["pixelize", "hblur"],
    "shaped": ["wave", "ripple", "diamond", "boxopen", "checker",
               "blinds", "bars", "burn"],
    "stylized": ["glitch", "spiral", "whip", "whipright", "whipup", "whipdown"],
}

# One human description per DISTINCT look (UI tooltips / chat / the prompt
# planner's cards). Complete on purpose: `entries()` reads this table, and
# 47 of the 72 tiles had no tooltip while a second, hand-written copy of the
# same list lived in presets/transitions/catalog.json — which also disagreed
# with this module on 4 families, 20 default durations and 30+ labels. This
# module is now the ONLY catalog; `agent/prompt/presets.transition_catalog()`
# wraps `entries()` and a test pins that every look has a description.
DESCRIPTIONS: dict[str, str] = {
    "bars": "Horizontal bars wipe together.",
    "blinds": "Venetian blinds — vertical bands wipe together.",
    "boxopen": "A rectangular iris opens from the centre.",
    "burn": "The bright parts of the outgoing shot burn away first.",
    "checker": "Checkerboard cells resolve in a scattered order.",
    "circleclose": "An iris closes on the old clip.",
    "circlecrop": "The frame pulls in to a circle and opens back out.",
    "circleopen": "An iris opens from the centre to reveal the next clip.",
    "coverdown": "The new clip slides in over the old one, downward.",
    "coverleft": "The new clip slides in over the old one, leftward.",
    "coverright": "The new clip slides in over the old one, rightward.",
    "coverup": "The new clip slides in over the old one, upward.",
    "diagbl": "A soft diagonal reveal from the bottom-left.",
    "diagbr": "A soft diagonal reveal from the bottom-right.",
    "diagtl": "A soft diagonal reveal from the top-left.",
    "diagtr": "A soft diagonal reveal from the top-right.",
    "diamond": "A diamond iris opens from the centre.",
    "dissolve": "Grainy pixel dissolve with a film-lab feel.",
    "distance": "The two frames blend by pixel distance — a soft morph.",
    "fade": "Classic crossfade from one clip into the next.",
    "fadeblack": "Dip through black — the cinematic scene change.",
    "fadefast": "A crossfade that resolves early — snappy, almost a cut.",
    "fadegrays": "Drains the colour, passes through grey, refills.",
    "fadeslow": "A crossfade that lingers in the blend.",
    "fadewhite": "Flash through white — bright and upbeat.",
    "glitch": "Per-row digital tear — the TikTok glitch cut.",
    "hblur": "Blur out, blur in.",
    "hlslice": "Horizontal slices peel away to the left.",
    "hlwind": "Streaks smear the picture away to the left.",
    "horzclose": "Two halves close in from the top and bottom.",
    "horzopen": "The frame splits across the middle and opens outward.",
    "hrslice": "Horizontal slices peel away to the right.",
    "hrwind": "Streaks smear the picture away to the right.",
    "pixelize": "Mosaic out, mosaic in — a digital feel.",
    "radial": "A clock-hand sweep around the centre.",
    "rectcrop": "The frame pulls in to a rectangle and opens back out.",
    "revealdown": "The old clip slides away downward, revealing the new one.",
    "revealleft": "The old clip slides away leftward, revealing the new one.",
    "revealright": "The old clip slides away rightward, revealing the new one.",
    "revealup": "The old clip slides away upward, revealing the new one.",
    "ripple": "A ring ripples out from the centre.",
    "slidedown": "The new clip pushes the old one down and out.",
    "slideleft": "The new clip pushes the old one out to the left.",
    "slideright": "The new clip pushes the old one out to the right.",
    "slideup": "The new clip pushes the old one up and out.",
    "smoothdown": "A soft-edged slide downward.",
    "smoothleft": "A soft-edged slide to the left.",
    "smoothright": "A soft-edged slide to the right.",
    "smoothup": "A soft-edged slide upward.",
    "spiral": "An angular sweep that rotates outward.",
    "squeezeh": "The old clip squeezes flat sideways.",
    "squeezev": "The old clip squeezes flat vertically.",
    "vdslice": "Vertical slices peel away downward.",
    "vdwind": "Streaks smear the picture away downward.",
    "vertclose": "Two halves close in from the sides.",
    "vertopen": "The frame splits down the middle and opens outward.",
    "vuslice": "Vertical slices peel away upward.",
    "vuwind": "Streaks smear the picture away upward.",
    "wave": "A vertical wipe whose edge ripples as it crosses.",
    "whip": "A whip pan to the left — a hard push with a motion-blur burst.",
    "whipdown": "A whip pan downward with a motion-blur burst.",
    "whipright": "A whip pan to the right with a motion-blur burst.",
    "whipup": "A whip pan upward with a motion-blur burst.",
    "wipebl": "A diagonal wipe towards the bottom-left corner.",
    "wipebr": "A diagonal wipe towards the bottom-right corner.",
    "wipedown": "A hard edge wipes downward.",
    "wipeleft": "A hard edge wipes the new clip in from the right.",
    "wiperight": "A hard edge wipes the new clip in from the left.",
    "wipetl": "A diagonal wipe towards the top-left corner.",
    "wipetr": "A diagonal wipe towards the top-right corner.",
    "wipeup": "A hard edge wipes upward.",
    "zoomin": "A punch-zoom into the next clip.",
    # aliases a reader may look up directly
    "spin": "Spiral sweep — reads as rotation (alias of `spiral`).",
    "zoomout": "Iris-close. NOT a true zoom-out — the render engine cannot "
               "scale during a transition; this is the closest look it has.",
}

# Backwards-compat: the five names the old schema shipped that were broken.
LEGACY_ALIASES = {"slide", "zoom", "glitch", "whip", "spin"}


# --- product families + per-transition default durations ---------------------
#
# CapCut groups its transitions by what they DO to the eye, not by which
# ffmpeg mechanism draws them; the owner asked for "all the transitions from
# CapCut", so the product surface (the Transitions panel's tabs, the prompt
# planner's "smooth zoom between every clip") speaks these eight families.
# `CATEGORIES` above stays the renderer's own grouping (fades/wipes/slices…)
# because tests and the chat tool already read it; `FAMILIES` is the second,
# product-facing view of the SAME canonical names. Every distinct look is in
# exactly one family (a test pins that), aliases inherit their target's.
FAMILY_NAMES: tuple[str, ...] = ("Basic", "Wipe", "Slide", "Zoom", "Blur", "Shape",
                                 "Glitch/Stylised", "Light")

FAMILIES: dict[str, list[str]] = {
    "Basic": ["fade", "fadefast", "fadeslow", "dissolve", "distance"],
    "Light": ["fadeblack", "fadewhite", "fadegrays", "burn"],
    "Wipe": ["wipeleft", "wiperight", "wipeup", "wipedown", "wipetl", "wipetr", "wipebl", "wipebr",
             "diagtl", "diagtr", "diagbl", "diagbr", "radial", "wave",
             "hlslice", "hrslice", "vuslice", "vdslice"],
    "Slide": ["slideleft", "slideright", "slideup", "slidedown",
              "smoothleft", "smoothright", "smoothup", "smoothdown",
              "coverleft", "coverright", "coverup", "coverdown",
              "revealleft", "revealright", "revealup", "revealdown"],
    "Zoom": ["zoomin", "squeezeh", "squeezev", "circlecrop", "rectcrop"],
    "Blur": ["hblur", "hlwind", "hrwind", "vuwind", "vdwind"],
    "Shape": ["circleopen", "circleclose", "vertopen", "vertclose", "horzopen", "horzclose",
              "ripple", "diamond", "boxopen", "checker", "blinds", "bars"],
    "Glitch/Stylised": ["glitch", "pixelize", "spiral", "whip", "whipright", "whipup", "whipdown"],
}

# Seconds a transition runs when the caller names no duration. Per family,
# because that is how the eye reads them: a whip must be over before it is
# noticed, a dip to black needs time to land, a plain crossfade sits between.
# The tool used to hard-code 0.5 for everything, which made a whip pan look
# like a slow smear and a fade-to-black look like a flicker; CapCut's own
# defaults sit in the same 0.25–0.7 s band. Per-name overrides sit on top
# for the few looks whose family default is wrong for them.
FAMILY_DEFAULT_DURATION_S: dict[str, float] = {
    "Basic": 0.5, "Light": 0.6, "Wipe": 0.4, "Slide": 0.35, "Zoom": 0.3,
    "Blur": 0.5, "Shape": 0.5, "Glitch/Stylised": 0.3,
}
_NAME_DEFAULT_DURATION_S: dict[str, float] = {
    "fadefast": 0.3, "fadeslow": 0.8, "whip": 0.25, "whipright": 0.25, "whipup": 0.25,
    "whipdown": 0.25, "spiral": 0.6, "pixelize": 0.5, "burn": 0.7,
}
#: The renderer never runs an xfade shorter than this; a zero or negative
#: duration in a legacy EDL is "use the default", not "a 0 s cross-fade"
#: (which ffmpeg rejects and which the timeline would count as free).
MIN_DURATION_S = 0.1
FALLBACK_DURATION_S = 0.5

_FAMILY_OF: dict[str, str] = {name: fam for fam, names in FAMILIES.items() for name in names}


def family_of(name: str) -> str | None:
    """CapCut-style family of a transition (aliases resolve to their target);
    None for a name the catalog does not know."""
    canon = canonical(name)
    return _FAMILY_OF.get(canon)


def default_duration(name: str) -> float:
    """Seconds this transition runs when no duration is given — the value
    `add_transition` writes into the EDL, so the timeline arithmetic
    (`EDL.transition_overlap`) and the xfade the compositor emits agree
    without either knowing about the other."""
    canon = canonical(name)
    if canon in _NAME_DEFAULT_DURATION_S:
        return _NAME_DEFAULT_DURATION_S[canon]
    fam = _FAMILY_OF.get(canon)
    return FAMILY_DEFAULT_DURATION_S.get(fam, FALLBACK_DURATION_S) if fam else FALLBACK_DURATION_S


def effective_duration(name: str, duration: float | None) -> float:
    """The duration the renderer actually uses for a transition record: the
    stored one when it is a real positive number, else the per-transition
    default. Shared by the compositor and anything that predicts its output
    length, so the two can never disagree on what a stored `0.0` means."""
    try:
        d = float(duration) if duration is not None else 0.0
    except (TypeError, ValueError):
        d = 0.0
    return d if d >= MIN_DURATION_S else default_duration(name)


def display_name(name: str) -> str:
    """A CapCut-style label for a canonical look: "Slide Left", "Fade to
    Black", "Whip Pan Up". Derived, so every look has one; a presets file
    may carry a nicer one and the UI may prefer it."""
    canon = canonical(name)
    special = {
        "fadeblack": "Fade to Black", "fadewhite": "Fade to White", "fadegrays": "Fade Through Gray",
        "fadefast": "Fast Fade", "fadeslow": "Slow Fade", "hblur": "Blur Dissolve",
        "hlslice": "Slice Left", "hrslice": "Slice Right", "vuslice": "Slice Up", "vdslice": "Slice Down",
        "hlwind": "Wind Left", "hrwind": "Wind Right", "vuwind": "Wind Up", "vdwind": "Wind Down",
        "squeezeh": "Squeeze Horizontal", "squeezev": "Squeeze Vertical", "circlecrop": "Circle Zoom",
        "rectcrop": "Box Zoom", "vertopen": "Barn Doors Open", "vertclose": "Barn Doors Close",
        "horzopen": "Curtain Open", "horzclose": "Curtain Close", "radial": "Clock Wipe",
        "wipetl": "Wipe Top-Left", "wipetr": "Wipe Top-Right", "wipebl": "Wipe Bottom-Left",
        "wipebr": "Wipe Bottom-Right", "diagtl": "Diagonal Top-Left", "diagtr": "Diagonal Top-Right",
        "diagbl": "Diagonal Bottom-Left", "diagbr": "Diagonal Bottom-Right",
        "whip": "Whip Pan Left", "whipright": "Whip Pan Right", "whipup": "Whip Pan Up",
        "whipdown": "Whip Pan Down", "zoomin": "Zoom In", "boxopen": "Box Open",
        "circleopen": "Iris Open", "circleclose": "Iris Close", "burn": "Film Burn",
        "pixelize": "Pixelate", "checker": "Checkerboard", "blinds": "Blinds", "bars": "Bars",
    }
    if canon in special:
        return special[canon]
    for prefix in ("slide", "smooth", "cover", "reveal", "wipe"):
        for direction in ("left", "right", "up", "down"):
            if canon == f"{prefix}{direction}":
                return f"{prefix.capitalize()} {direction.capitalize()}"
    return canon.capitalize()


def entries() -> list[dict]:
    """One product-facing record per DISTINCT look (aliases folded in), in
    family order then name order: `{name, display, family, category,
    default_duration, description, aliases, kind}`. `kind` says which
    mechanism draws it (native xfade / custom expr / post-filter) — the UI
    hover preview picks its animation by family, the renderer by kind."""
    alias_of: dict[str, list[str]] = {}
    for alias, target in ALIASES.items():
        alias_of.setdefault(target, []).append(alias)
    category_of = {name: cat for cat, names in CATEGORIES.items() for name in names}
    out: list[dict] = []
    for fam in FAMILY_NAMES:
        for name in sorted(FAMILIES[fam]):
            kind = "post" if name in POST_FILTERS else "custom" if name in CUSTOM_EXPRS else "native"
            out.append({
                "name": name, "display": display_name(name), "family": fam,
                "category": category_of.get(name), "default_duration": default_duration(name),
                "description": DESCRIPTIONS.get(name, ""), "aliases": sorted(alias_of.get(name, [])),
                "kind": kind,
            })
    return out


def all_names() -> list[str]:
    """Every accepted transition name (catalog + aliases + custom), sorted."""
    names = set(NATIVE) | set(ALIASES) | set(CUSTOM_EXPRS)
    return sorted(names)


def is_valid(name: str) -> bool:
    n = (name or "").strip().lower()
    return n in NATIVE or n in ALIASES or n in CUSTOM_EXPRS


def resolve_transition(name: str) -> tuple[str, str | None]:
    """Resolve a friendly name to (xfade_transition_arg, expr_or_None).

    Unknown names fall back to `fade` rather than raising — a render should
    never crash because someone typed an unrecognised transition. Validation
    with a helpful error is the dispatch layer's job (add_transition).
    """
    n = (name or "").strip().lower()
    # custom-expr transitions
    if n in CUSTOM_EXPRS:
        return "custom", CUSTOM_EXPRS[n]
    # alias → canonical, which may itself be a custom expr
    if n in ALIASES:
        canon = ALIASES[n]
        if canon in CUSTOM_EXPRS:
            return "custom", CUSTOM_EXPRS[canon]
        return NATIVE.get(canon, canon), None
    if n in NATIVE:
        return NATIVE[n], None
    # Unknown → safe default, but never SILENTLY: an unknown name can only
    # reach here by bypassing add_transition's validation (direct EDL edits,
    # stale project files), and a silent fade turns that into "my transition
    # doesn't work" with zero signal anywhere.
    _log.warning("unknown transition %r — rendering as 'fade' "
                 "(valid names: render.transitions.all_names())", name)
    return "fade", None


def canonical(name: str) -> str:
    """The catalog entry a name resolves to (aliases followed once)."""
    n = (name or "").strip().lower()
    return ALIASES.get(n, n)


def post_filter(name: str, start: float, end: float,
                width: int = 1080, height: int = 1920) -> str | None:
    """ffmpeg filter to apply to the xfade OUTPUT, or None.

    `start`/`end` are OUTPUT-timeline seconds — the filter is gated to exactly
    the transition window so it cannot touch the rest of the clip. `width`/
    `height` scale the effect to the render size (see WHIP_SIGMA_RATIO).
    """
    spec = POST_FILTERS.get(canonical(name))
    if spec is None:
        return None
    axis, template = spec
    sigma = max(4.0, (int(width) if axis == "h" else int(height)) * WHIP_SIGMA_RATIO)
    return template.format(start=f"{start:.3f}", end=f"{end:.3f}", sigma=f"{sigma:.1f}")


def catalog() -> dict:
    """Structured catalog for list_transitions: categories + aliases + counts.

    Reports `looks` (distinct effects) SEPARATELY from `count` (accepted names).
    A single number counting both read as "88 transitions" when a third of those
    are synonyms — `mosaic`, `pixelate` and `pixel` are all `pixelize` — so a
    tester trying them one by one found repeats and reported the catalogue as
    padded. Both numbers are true; only publishing the larger one is not.
    """
    return {
        "categories": CATEGORIES,
        "aliases": ALIASES,
        "descriptions": DESCRIPTIONS,
        "count": len(all_names()),
        "looks": len(set(NATIVE) | set(CUSTOM_EXPRS)),
        "alias_count": len(ALIASES),
        "note": (f"{len(set(NATIVE) | set(CUSTOM_EXPRS))} distinct looks; "
                 f"{len(ALIASES)} of the {len(all_names())} accepted names are "
                 "synonyms for one of them (see `aliases`)."),
        "all": all_names(),
        # Product view (CapCut-style): family tabs, per-look defaults and
        # display metadata. `defaults` is keyed by EVERY accepted name so a
        # caller holding an alias needs no second lookup.
        "families": {fam: sorted(names) for fam, names in FAMILIES.items()},
        "family_order": list(FAMILY_NAMES),
        "defaults": {n: default_duration(n) for n in all_names()},
        "entries": entries(),
    }
