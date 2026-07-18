"""Per-clip effects → ffmpeg filter chain fragments.

Each Effect on a Clip is converted to one or more ffmpeg video filters that
run between the clip's scale/pad and the concat into the timeline. Filters
chosen are all available in the standard brew ffmpeg build (no libass/libzimg
dependencies).
"""
from __future__ import annotations
import math
import os
import re
import threading
from pathlib import Path
from PIL import Image, ImageDraw

from ..edl.schema import Effect, Mask, Clip, ChromaKey
from .. import platformutil as _pu


def mask_png_is_valid(p: Path) -> bool:
    """True if `p` exists and holds a decodable PNG (see text_overlay._png_is_valid
    for why a bare exists() check is unsafe: a killed/raced render can leave a
    0-byte or truncated cache file that `exists()` alone would still reuse)."""
    if not p.exists() or p.stat().st_size == 0:
        return False
    try:
        with Image.open(p) as im:
            im.verify()
        return True
    except Exception:
        return False


def _hex_to_ffmpeg_color(hex_color: str) -> str:
    """Normalize '#00FF00' / '00ff00' / 'green' → ffmpeg-friendly form."""
    s = (hex_color or "").strip().lstrip("#")
    if len(s) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in s):
        return f"0x{s.upper()}"
    return hex_color  # let ffmpeg parse named colors like "green"


def build_chromakey_filter(ck: ChromaKey) -> str:
    """Return an ffmpeg filter chain that keys out `ck.color`.

    Chain layout:
        format=yuva420p   — ensures an alpha plane exists.
        chromakey=color:similarity:blend
        despill=type=...  — only when spill_suppress > 0 and color is green/blue.
    """
    color = _hex_to_ffmpeg_color(ck.color)
    sim = max(0.001, min(1.0, float(ck.similarity)))
    blend = max(0.0, min(1.0, float(ck.smoothness)))
    parts = [f"format=yuva420p", f"chromakey={color}:{sim:.3f}:{blend:.3f}"]
    spill = max(0.0, min(1.0, float(ck.spill_suppress)))
    if spill > 0.001:
        # Heuristic: assume green if R<128 G>128, else blue. Default green.
        try:
            r = int(color[2:4], 16); g = int(color[4:6], 16); b = int(color[6:8], 16)
            despill_type = "blue" if (b > g and b > r) else "green"
        except Exception:
            despill_type = "green"
        parts.append(f"despill=type={despill_type}:mix={spill:.3f}:expand=0")
    return ",".join(parts)


# ---- color / look ----
#
# Every builder takes (params, uid). `uid` is a graph-unique suffix for
# builders that open split/blend sub-branches (lut, glow, rgb_split): ffmpeg
# link labels are GLOBAL across the whole filter_complex, so a fixed label
# like `[a]` breaks the graph as soon as two clips carry the same effect in
# one monolithic render (transitions disable the chunk cache, putting every
# clip in a single graph). Builders that emit a purely linear chain ignore it.

def _color(p: dict, uid: str = "") -> str:
    """Per-channel color grading via eq + colorbalance."""
    eq_parts: list[str] = []
    if "brightness" in p:
        eq_parts.append(f"brightness={float(p['brightness']):.3f}")
    if "contrast" in p:
        eq_parts.append(f"contrast={float(p['contrast']):.3f}")
    if "sat" in p or "saturation" in p:
        eq_parts.append(f"saturation={float(p.get('sat', p.get('saturation'))):.3f}")
    if "gamma" in p:
        eq_parts.append(f"gamma={float(p['gamma']):.3f}")
    chain = []
    if eq_parts:
        chain.append("eq=" + ":".join(eq_parts))
    # color temperature: simple shift via colorbalance midtones
    if "temp" in p:
        t = float(p["temp"])  # -1 cool, +1 warm
        chain.append(f"colorbalance=rm={t * 0.3:.3f}:bm={-t * 0.3:.3f}")
    if "tint" in p:
        t = float(p["tint"])  # -1 magenta, +1 green
        chain.append(f"colorbalance=gm={t * 0.3:.3f}")
    return ",".join(chain) if chain else "null"


def _lut(p: dict, uid: str = "") -> str:
    src = p.get("src")
    if not src:
        return "null"
    intensity = max(0.0, min(1.0, float(p.get("intensity", 1.0))))
    # The LUT path is embedded in the lut3d= filter option, not passed as -i, so
    # it needs filtergraph escaping (raw Windows C:\ paths break the parser).
    src_arg = _pu.ffmpeg_filter_path(src)
    if intensity >= 0.999:
        return f"lut3d={src_arg}"
    if intensity <= 0.001:
        return "null"
    # Partial intensity: split, LUT one branch, blend it back over the original.
    # blend all_mode=normal weights its FIRST-listed input by all_opacity and
    # the SECOND by (1-opacity) — verified empirically (black-first/white-
    # second at opacity=0.8 → mean 50 ≈ 255*0.2, i.e. output = first*opacity).
    # The LUT'd branch must be listed FIRST so intensity=I means "I parts
    # LUT'd, (1-I) parts original" — listing them the other way around (as an
    # earlier version of this code did) made intensity apply almost entirely
    # to the ORIGINAL instead, e.g. intensity=0.9 rendered ~90% original.
    o, l, d = f"[lo{uid}]", f"[lb{uid}]", f"[ll{uid}]"
    return (f"split=2{o}{l};"
            f"{l}lut3d={src_arg}{d};"
            f"{d}{o}blend=all_mode=normal:all_opacity={intensity:.3f}")


def _blur(p: dict, uid: str = "") -> str:
    radius = float(p.get("radius", p.get("amount", 8)))
    return f"gblur=sigma={max(0.5, radius):.2f}"


def _sharpen(p: dict, uid: str = "") -> str:
    amount = float(p.get("amount", 1.0))
    return f"unsharp=lx=5:ly=5:la={amount:.2f}"


def _vignette(p: dict, uid: str = "") -> str:
    angle = float(p.get("angle", math.pi / 4))
    return f"vignette=angle={angle:.3f}"


def _grain(p: dict, uid: str = "") -> str:
    strength = int(p.get("strength", 20))
    return f"noise=alls={max(1, strength)}:allf=t"


def _vintage(_: dict, uid: str = "") -> str:
    """Stylized look: warm + slight desat + grain + vignette."""
    return ("eq=contrast=1.05:saturation=0.85:gamma=1.05,"
            "colorbalance=rm=0.06:bm=-0.06,"
            "noise=alls=14:allf=t,"
            "vignette=angle=PI/4")


def _vhs(_: dict, uid: str = "") -> str:
    return ("eq=saturation=0.7:contrast=1.1,"
            "noise=alls=22:allf=t,"
            "boxblur=lr=0:lp=1")


def _glow(p: dict, uid: str = "") -> str:
    """Soft-glow via blurred copy blended with original (uses split+blend)."""
    s = float(p.get("strength", 0.4))
    # Split → one branch blurred → blend with original
    a, b, g = f"[ga{uid}]", f"[gb{uid}]", f"[gg{uid}]"
    return f"split=2{a}{b};{b}gblur=sigma=12{g};{a}{g}blend=all_mode=screen:all_opacity={s:.2f}"


def _hflip(_: dict, uid: str = "") -> str:
    return "hflip"


def _vflip(_: dict, uid: str = "") -> str:
    return "vflip"


def _rgb_split(p: dict, uid: str = "") -> str:
    """Cheap chromatic aberration: split RGB and offset."""
    off = int(p.get("offset", 6))
    # split into R/G/B then merge offset versions; this is a pragmatic approximation
    r0, g0, b0 = f"[r0{uid}]", f"[g0{uid}]", f"[b0{uid}]"
    r1, g1, b1, rg = f"[r1{uid}]", f"[g1{uid}]", f"[b1{uid}]", f"[rg{uid}]"
    return (f"split=3{r0}{g0}{b0};"
            f"{r0}lutrgb=g=0:b=0,crop=iw-{off}:ih:0:0,pad=iw+{off}:ih:0:0{r1};"
            f"{g0}lutrgb=r=0:b=0{g1};"
            f"{b0}lutrgb=r=0:g=0,crop=iw-{off}:ih:{off}:0,pad=iw+{off}:ih:0:0{b1};"
            f"{r1}{g1}blend=all_mode=addition{rg};"
            f"{rg}{b1}blend=all_mode=addition")


EFFECT_BUILDERS = {
    "color":      _color,
    "color_grade": _color,
    "lut":        _lut,
    "blur":       _blur,
    "sharpen":    _sharpen,
    "vignette":   _vignette,
    "grain":      _grain,
    "vintage":    _vintage,
    "vhs":        _vhs,
    "glow":       _glow,
    "hflip":      _hflip,
    "vflip":      _vflip,
    "rgb_split":  _rgb_split,
}


def effect_chain(effects: list[Effect], uid: str = "") -> str:
    """Combine an ordered list of effects into a single filter chain string.

    Returns "" if the list is empty (caller should skip applying anything).

    `uid` should be graph-unique per clip (the caller passes the clip id).
    It is combined with the effect's position so that two effects of the
    same type — on one clip or across clips in a monolithic graph — never
    emit colliding link labels (see the builder-comment above _color).
    """
    safe_uid = re.sub(r"\W", "", uid or "")
    parts: list[str] = []
    for j, e in enumerate(effects):
        builder = EFFECT_BUILDERS.get(e.type)
        if builder is None:
            continue
        chunk = builder(e.params or {}, f"_{safe_uid}_{j}")
        if chunk and chunk != "null":
            parts.append(chunk)
    return ",".join(parts)


# ---- masks (Pillow PNG → ffmpeg alphamerge) ----

def render_mask_png(mask: Mask, w: int, h: int, dst: Path) -> Path:
    """Generate a grayscale mask PNG (white = visible, black = hidden).

    Used as the second input to `alphamerge` so we can multi-shape per clip.
    """
    img = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(img)
    if mask.type == "rectangle":
        rw = w * 0.7
        rh = h * 0.7
        cx, cy = mask.position
        draw.rectangle([cx - rw / 2, cy - rh / 2, cx + rw / 2, cy + rh / 2], fill=255)
    elif mask.type == "circle":
        r = min(w, h) * 0.35
        cx, cy = mask.position
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    elif mask.type == "linear":
        # Soft horizontal gradient from left to right
        cx, _cy = mask.position
        cutoff = int(cx)
        # gradient over feather width
        feather = max(1, int(mask.feather))
        for x in range(w):
            d = x - cutoff
            if d <= -feather: v = 0
            elif d >= feather: v = 255
            else: v = int(255 * (d + feather) / (2 * feather))
            draw.line([(x, 0), (x, h)], fill=v)
    else:
        # Fallback: full visible
        draw.rectangle([0, 0, w, h], fill=255)
    if mask.invert:
        img = Image.eval(img, lambda v: 255 - v)
    if mask.feather > 0 and mask.type in ("rectangle", "circle"):
        # Blur the binary mask to feather edges
        from PIL import ImageFilter
        img = img.filter(ImageFilter.GaussianBlur(radius=mask.feather))
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        img.save(tmp, format="PNG")
        _pu.replace_with_retry(tmp, dst)
    finally:
        _pu.unlink_with_retry(tmp)
    return dst
