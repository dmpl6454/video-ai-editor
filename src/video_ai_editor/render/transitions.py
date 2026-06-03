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

  2. Breadth. ~45 curated friendly names covering the directional wipes/slides,
     shapes (circle/rect/radial), reveals/covers, blurs, and three stylized
     ones (glitch / whip / spin) — glitch via a custom expr, whip + spin via
     the closest native that reads right.

`resolve_transition(name)` → (xfade_transition_arg, expr_or_None).
  - native:  ("slideleft", None)         → xfade=transition=slideleft
  - custom:  ("custom", "<expr string>") → xfade=transition=custom:expr='...'
"""
from __future__ import annotations

# --- custom-expr transitions --------------------------------------------------
# xfade expr vars: X Y (pixel), W H (dims), P (progress 0→1), A B (the two
# source pixels for the current plane). Expr returns the output pixel value.

# Glitch: per-row pseudo-random slice flicker. floor(Y/6)*12.9898 gives a
# stable per-6px-row hash; sin(P*36) makes it flicker over the transition;
# thresholding A vs B at 0.5 produces digital "tear" slices that resolve to B.
_GLITCH_EXPR = "if(gt(P+0.45*sin(floor(Y/6)*12.9898)*sin(P*36),0.5),B,A)"

CUSTOM_EXPRS: dict[str, str] = {
    "glitch": _GLITCH_EXPR,
}

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
    "blinds": "hrslice",
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
    "whip": "smoothleft",     # fast directional sweep reads as a whip pan
    "whippan": "smoothleft",
    "spin": "radial",         # clock-wipe gives the rotational feel
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
    "stylized": ["glitch", "whip", "spin"],
}

# Short human descriptions for the most-used ones (UI tooltips / chat).
DESCRIPTIONS: dict[str, str] = {
    "fade": "Classic crossfade A→B.",
    "dissolve": "Grainy pixel dissolve.",
    "fadeblack": "Dip to black between clips.",
    "fadewhite": "Flash to white between clips.",
    "slideleft": "Incoming clip pushes in from the right.",
    "smoothright": "Soft directional slide rightward.",
    "zoomin": "Punch-zoom into the next clip.",
    "circleopen": "Iris opens to reveal the next clip.",
    "radial": "Clock-wipe sweep.",
    "pixelize": "Mosaic-out, mosaic-in (digital feel).",
    "glitch": "Per-row digital tear/slice glitch.",
    "whip": "Fast directional whip-pan.",
    "spin": "Rotational clock-wipe.",
    "hblur": "Blur out, blur in.",
}

# Backwards-compat: the five names the old schema shipped that were broken.
LEGACY_ALIASES = {"slide", "zoom", "glitch", "whip", "spin"}


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
    # unknown → safe default
    return "fade", None


def catalog() -> dict:
    """Structured catalog for list_transitions: categories + aliases + count."""
    return {
        "categories": CATEGORIES,
        "aliases": ALIASES,
        "descriptions": DESCRIPTIONS,
        "count": len(all_names()),
        "all": all_names(),
    }
