"""CapCut Canvas backgrounds and overlay blend modes: ONE table read by the
schema (`edl/schema.CanvasBackground`, `Clip.blend`), the renderers
(`render/compositor.py`'s contain fit, `render/pip.py`), the editing engine
(`agent/dispatch.set_canvas_background` / `set_blend_mode`), the agent tool
schemas (`agent/tools.py`), the Prompt Editor's validator and grammar, and the
Inspector (`GET /api/canvas-blend/presets`, `api/canvas_blend_routes.py` — the
browser keeps no copy of the menus).

CANVAS (wave E, lane F2). A `contain`-fit main-track clip is letterboxed:
the part of the canvas its picture does not cover used to be black. CapCut's
Canvas fills it with one of
  * `color` — any #RRGGBB,
  * `blur`  — a scaled-to-cover, blurred copy of the clip itself (CapCut shows
              four strengths; `CANVAS_BLUR_LEVELS`),
  * `image` — an imported picture, scaled to cover the canvas.
The background belongs to the fitted frame, so a transform, rotation, opacity
or fade applied to the clip applies to it too, exactly as it does to the black
bars today.

BLEND. An overlay (PiP, v2+) clip composites onto what is beneath it with one
of CapCut's 14 blend modes. The maths is the W3C Compositing Level 1 formula
for every SEPARABLE mode, in gamma-encoded sRGB — what a browser's
`mix-blend-mode` computes, so the live preview draws the export's pixels:

    result = (1 − α)·Cb + α·B(Cb, Cs)

`add` and `linear_burn` are CSS's Porter-Duff operators `plus-lighter` /
`plus-darker` instead (min(1, Cb + α·Cs) and max(0, Cb − α·(1 − Cs))): the
same pixels at full opacity, and the only way a browser draws them at all.
`render/pip.py` builds each mode from `lut2_expr` below.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

# --------------------------------------------------------------- blend modes

BlendMode = Literal[
    "normal", "darken", "multiply", "color_burn", "linear_burn",
    "lighten", "screen", "color_dodge", "add",
    "overlay", "soft_light", "hard_light", "difference", "exclusion",
]


@dataclass(frozen=True)
class Blend:
    id: str
    label: str
    #: The CSS `mix-blend-mode` keyword the browser composites the live PiP
    #: with (lib/pipBlendLayers.ts reads it from the presets payload).
    css: str
    #: Porter-Duff (premultiplied) rather than W3C separable: add, linear burn.
    porter_duff: bool
    #: Extra words the agent and the Prompt bar accept for this mode.
    aliases: tuple[str, ...] = ()


#: CapCut's menu order: Normal, then the darkening group, the lightening
#: group, the contrast group and the comparative group.
BLENDS: tuple[Blend, ...] = (
    Blend("normal", "Normal", "normal", False, ("none", "default", "no blend", "regular")),
    Blend("darken", "Darken", "darken", False, ("darker",)),
    Blend("multiply", "Multiply", "multiply", False, ("multiplied",)),
    Blend("color_burn", "Color Burn", "color-burn", False, ("colour burn", "burn")),
    Blend("linear_burn", "Linear Burn", "plus-darker", True, ("plus darker", "subtractive")),
    Blend("lighten", "Lighten", "lighten", False, ("lighter",)),
    Blend("screen", "Screen", "screen", False, ()),
    Blend("color_dodge", "Color Dodge", "color-dodge", False, ("colour dodge", "dodge")),
    Blend("add", "Add", "plus-lighter", True, ("linear dodge", "additive", "plus lighter", "addition")),
    Blend("overlay", "Overlay", "overlay", False, ()),
    Blend("soft_light", "Soft Light", "soft-light", False, ("softlight",)),
    Blend("hard_light", "Hard Light", "hard-light", False, ("hardlight",)),
    Blend("difference", "Difference", "difference", False, ("diff",)),
    Blend("exclusion", "Exclusion", "exclusion", False, ("exclude",)),
)
BLEND_IDS: tuple[str, ...] = tuple(b.id for b in BLENDS)
_BLEND_BY_ID = {b.id: b for b in BLENDS}


def blend_of(mode: str) -> Blend:
    return _BLEND_BY_ID[mode]


def resolve_blend(value: Any) -> str:
    """A blend id from a caller's word ("Screen", "color-burn", "linear
    dodge"); ValueError naming the choices for anything else."""
    s = re.sub(r"[\s_\-]+", " ", str(value or "").strip().lower())
    if not s:
        raise ValueError("blend mode is empty")
    for b in BLENDS:
        if s in (b.id.replace("_", " "), b.label.lower(), b.css.replace("-", " "), *b.aliases):
            return b.id
    raise ValueError(f"unknown blend mode {value!r}; choose one of: " + ", ".join(b.label for b in BLENDS))


def _w3c(mode: str) -> str:
    """B(Cb, Cs) of a separable mode as an ffmpeg expression over x = Cb and
    y = Cs, 8-bit, before rounding. a/b below are x/255 and y/255."""
    a, b = "(x/255)", "(y/255)"
    exprs = {
        "normal": "y",
        "darken": "min(x,y)",
        "lighten": "max(x,y)",
        "multiply": f"255*{a}*{b}",
        "screen": f"255*({a}+{b}-{a}*{b})",
        # HardLight(Cs, Cb): conditioned on the BACKDROP
        "overlay": f"255*if(lte({a},0.5),2*{a}*{b},1-2*(1-{a})*(1-{b}))",
        # conditioned on the SOURCE
        "hard_light": f"255*if(lte({b},0.5),2*{a}*{b},1-2*(1-{a})*(1-{b}))",
        "soft_light": (f"255*if(lte({b},0.5),{a}-(1-2*{b})*{a}*(1-{a}),"
                       f"{a}+(2*{b}-1)*(if(lte({a},0.25),((16*{a}-12)*{a}+4)*{a},sqrt({a}))-{a}))"),
        "color_dodge": f"if(eq(x,0),0,if(eq(y,255),255,255*min(1,{a}/(1-{b}))))",
        "color_burn": f"if(eq(x,255),255,if(eq(y,0),0,255*(1-min(1,(1-{a})/{b}))))",
        "difference": "abs(x-y)",
        "exclusion": f"255*({a}+{b}-2*{a}*{b})",
    }
    return exprs[mode]


def lut2_expr(mode: str) -> str:
    """The `lut2` expression for one colour plane, x = backdrop, y = the
    overlay layer. For a separable mode y is the overlay's colour Cs and the
    result is B(Cb, Cs) (the α mix happens in the overlay that follows); for
    `add` y is α·Cs and for `linear_burn` y is α·(1 − Cs) (premultiplied), and
    the result is the final pixel. Rounded half up and clipped to 0..255;
    commas escaped for the filtergraph parser."""
    if mode == "add":
        e = "min(255,x+y)"
    elif mode == "linear_burn":
        e = "max(0,x-y)"
    else:
        e = f"clip(floor({_w3c(mode)}+0.5),0,255)"
    return e.replace(",", "\\,")


def blend_reference(mode: str, cb: float, cs: float, alpha: float = 1.0) -> float:
    """The composited value (0..1) of one channel: backdrop `cb`, overlay
    colour `cs` at coverage `alpha`. The test oracle for both renderers."""
    if mode == "add":
        return min(1.0, cb + alpha * cs)
    if mode == "linear_burn":
        return max(0.0, cb - alpha * (1.0 - cs))
    a, b = cb, cs
    if mode == "normal":
        B = b
    elif mode == "darken":
        B = min(a, b)
    elif mode == "lighten":
        B = max(a, b)
    elif mode == "multiply":
        B = a * b
    elif mode == "screen":
        B = a + b - a * b
    elif mode == "overlay":
        B = 2 * a * b if a <= 0.5 else 1 - 2 * (1 - a) * (1 - b)
    elif mode == "hard_light":
        B = 2 * a * b if b <= 0.5 else 1 - 2 * (1 - a) * (1 - b)
    elif mode == "soft_light":
        if b <= 0.5:
            B = a - (1 - 2 * b) * a * (1 - a)
        else:
            d = ((16 * a - 12) * a + 4) * a if a <= 0.25 else a ** 0.5
            B = a + (2 * b - 1) * (d - a)
    elif mode == "color_dodge":
        B = 0.0 if a == 0 else 1.0 if b >= 1 else min(1.0, a / (1 - b))
    elif mode == "color_burn":
        B = 1.0 if a >= 1 else 0.0 if b == 0 else 1 - min(1.0, (1 - a) / b)
    elif mode == "difference":
        B = abs(a - b)
    elif mode == "exclusion":
        B = a + b - 2 * a * b
    else:
        raise ValueError(mode)
    return (1 - alpha) * cb + alpha * B


# --------------------------------------------------------------- canvas

CanvasKind = Literal["color", "blur", "image"]
CANVAS_KINDS: tuple[str, ...] = ("color", "blur", "image")


@dataclass(frozen=True)
class BlurLevel:
    level: int
    label: str
    #: Gaussian sigma as a share of the canvas's SHORT side.
    sigma_frac: float


#: CapCut's four blur strengths.
CANVAS_BLUR_LEVELS: tuple[BlurLevel, ...] = (
    BlurLevel(1, "Light", 0.012),
    BlurLevel(2, "Medium", 0.024),
    BlurLevel(3, "Strong", 0.040),
    BlurLevel(4, "Heavy", 0.060),
)
CANVAS_BLUR_DEFAULT = 2
#: The blurred copy is built at 1/`CANVAS_BLUR_DOWNSCALE` of the canvas and
#: scaled back up (bilinear): the blur removes everything a full-size pass
#: would keep, at 1/16 of the pixels — and the engine's shader does the same.
CANVAS_BLUR_DOWNSCALE = 4
#: CapCut's colour swatches (the picker also takes any #RRGGBB).
CANVAS_SWATCHES: tuple[str, ...] = (
    "#000000", "#FFFFFF", "#1F1F1F", "#7F7F7F", "#E53935", "#FB8C00", "#FDD835",
    "#43A047", "#00ACC1", "#1E88E5", "#5E35B1", "#D81B60", "#F8BBD0", "#FFE0B2",
)
_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")
_NAMED_COLOURS: dict[str, str] = {
    "black": "#000000", "white": "#FFFFFF", "grey": "#7F7F7F", "gray": "#7F7F7F",
    "dark grey": "#1F1F1F", "dark gray": "#1F1F1F", "red": "#E53935", "orange": "#FB8C00",
    "yellow": "#FDD835", "green": "#43A047", "teal": "#00ACC1", "cyan": "#00ACC1", "blue": "#1E88E5",
    "navy": "#1A237E", "purple": "#5E35B1", "violet": "#5E35B1", "pink": "#D81B60", "magenta": "#D81B60",
    "beige": "#FFE0B2", "cream": "#FFF8E1", "brown": "#6D4C41",
}
#: Image kinds a canvas image may be (the importer's picture types).
CANVAS_IMAGE_EXTS: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif")


def normalize_color(value: Any) -> str:
    """'#rrggbb' / 'rrggbb' / a colour name → '#RRGGBB'; ValueError otherwise."""
    s = str(value or "").strip()
    m = _HEX_RE.match(s)
    if m:
        return "#" + m.group(1).upper()
    named = _NAMED_COLOURS.get(re.sub(r"\s+", " ", s.lower()))
    if named:
        return named
    raise ValueError(f"colour must be #RRGGBB or a colour name, got {value!r}")


def color_names() -> tuple[str, ...]:
    return tuple(_NAMED_COLOURS)


def blur_level(level: Any) -> BlurLevel:
    n = int(round(float(level)))
    n = max(1, min(len(CANVAS_BLUR_LEVELS), n))
    return CANVAS_BLUR_LEVELS[n - 1]


def blur_sigma(level: Any, canvas_w: int, canvas_h: int) -> float:
    """The Gaussian sigma, in CANVAS pixels, of blur `level` on a canvas."""
    return blur_level(level).sigma_frac * min(int(canvas_w), int(canvas_h))


def presets_payload() -> dict:
    """What `GET /api/canvas-blend/presets` serves (and the Inspector reads)."""
    return {
        "blends": [{"id": b.id, "label": b.label, "css": b.css, "porter_duff": b.porter_duff}
                   for b in BLENDS],
        "canvas": {
            "kinds": list(CANVAS_KINDS),
            "blur_levels": [{"level": x.level, "label": x.label, "sigma_frac": x.sigma_frac}
                            for x in CANVAS_BLUR_LEVELS],
            "blur_default": CANVAS_BLUR_DEFAULT,
            "blur_downscale": CANVAS_BLUR_DOWNSCALE,
            "swatches": list(CANVAS_SWATCHES),
            "image_exts": list(CANVAS_IMAGE_EXTS),
        },
    }


__all__ = ["BlendMode", "Blend", "BLENDS", "BLEND_IDS", "blend_of", "resolve_blend", "lut2_expr",
           "blend_reference", "CanvasKind", "CANVAS_KINDS", "BlurLevel", "CANVAS_BLUR_LEVELS",
           "CANVAS_BLUR_DEFAULT", "CANVAS_BLUR_DOWNSCALE", "CANVAS_SWATCHES", "CANVAS_IMAGE_EXTS",
           "normalize_color", "color_names", "blur_level", "blur_sigma", "presets_payload"]
