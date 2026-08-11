"""Render text overlay clips as RGBA PNGs (via Pillow), then composite via ffmpeg `overlay`.

Reason: brew's ffmpeg 8 lacks libass and libfreetype, so neither `subtitles=` nor
`drawtext=` is available. PNG overlays via `overlay=` filter work on every build.
PNGs are cached by content hash so re-renders are cheap.

Emoji handling: bundled fonts (Inter, Anton, Bebas Neue, Montserrat) carry no
emoji glyphs, so emoji codepoints would draw as boxes. We fall back to the
system's Apple Color Emoji font when present (macOS) for emoji runs, and strip
emoji entirely on non-Mac systems.
"""
from __future__ import annotations
import hashlib
import logging
import os
import re
import threading
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

from ..config import FONTS_DIR
from ..edl import EDL
from ..edl.schema import TextClip, Sticker
from ..edl.keyframes import is_keyframed, sample, to_ffmpeg_expr
from .. import platformutil as _pu


def _png_is_valid(p: Path) -> bool:
    """True if `p` exists and holds a decodable PNG.

    A cache file can exist but be 0-byte or truncated when a prior render was
    killed mid-write (no atomic rename) or two renders raced on the same
    content-hash path. ffmpeg fed such a file as `-i` fails with "Invalid data
    found when processing input" and aborts the whole filter_complex — this
    guard is what keeps a torn cache file from being reused forever.
    """
    if not p.exists() or p.stat().st_size == 0:
        return False
    try:
        with Image.open(p) as im:
            im.verify()
        return True
    except Exception:
        return False


def _save_png_atomic(img: Image.Image, dst: Path) -> None:
    """Save `img` as a PNG at `dst` via write-to-temp + atomic rename.

    Mirrors render/compositor.py's `_part_path` + `replace_with_retry` pattern
    for mp4 outputs: a concurrent reader sees either the old complete file or
    the new complete file, never a partially-written one.
    """
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        img.save(tmp, format="PNG")
        _pu.replace_with_retry(tmp, dst)
    finally:
        _pu.unlink_with_retry(tmp)


# Emoji Unicode ranges — pragmatic, not exhaustive.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"  # most pictographs
    "\U00002600-\U000027BF"  # misc symbols / dingbats
    "\U0001F1E6-\U0001F1FF"  # flags
    "‍️⃣"      # ZWJ + variation selectors + keycap
    "]+", flags=re.UNICODE,
)


def _strip_emoji(s: str) -> str:
    return _EMOJI_RE.sub("", s).strip()


# --- inline emoji in text -----------------------------------------------------
#
# The bundled role fonts (Anton, Bebas, Inter, Montserrat) carry no emoji
# glyphs, so text used to be pushed through _strip_emoji before drawing and
# every emoji simply VANISHED — typing "🔥 SALE" exported "SALE", and typing
# only emoji exported an empty PNG ("I was unable to apply the emojis through
# the text section"). Rather than bundle a ~10MB colour-emoji font (which
# Pillow can only draw at its own fixed bitmap sizes, and which would still
# differ from what the browser paints), each emoji is composited as an IMAGE —
# the very same Fluent 3D PNG a sticker uses. One artwork source for the whole
# app, identical on macOS and Windows, identical between preview and export.

# An emoji occupies a square this many times the font size. The client mirrors
# this constant (TextLayer) so the drawn layout matches the baked one.
#
# VERTICAL placement is measured, not a constant: the square is centred on the
# text's CAP BAND — the top of a capital to the baseline, which is what the eye
# reads as the middle of a line. A fixed fraction of the em box was wrong twice
# over, because "where the cap band sits inside the em box" is a property of the
# font, and the two sides start from different origins: Pillow draws from the
# ASCENDER TOP while canvas 'middle' draws from the em-box MIDDLE. One nudge of
# 0.06em therefore put the bake 26.5px ABOVE the text and the preview below it —
# misaligned on both sides AND disagreeing with each other. Each side now
# measures the same band from its own font metrics, so they land together.
EMOJI_BOX_RATIO = 1.0


_ZWJ = "‍"
_VS16 = "️"
_KEYCAP = "⃣"
# Trailing codepoints that belong to the emoji BEFORE them rather than
# starting a new one: variation selector, keycap, and the five skin tones.
_EMOJI_MODIFIERS = {_VS16, _KEYCAP, *(chr(cp) for cp in range(0x1F3FB, 0x1F400))}


def _emoji_clusters(run: str) -> list[str]:
    """Split a matched emoji RUN into individual renderable emoji.

    `_EMOJI_RE` ends in `+`, so it swallows adjacent emoji into one match —
    "😍🤩" arrived as a single token, was looked up as codepoints
    `1f60d-1f929`, found no artwork, and drew NOTHING. But the run genuinely
    can contain multi-codepoint emoji that must stay together: a ZWJ sequence
    (👨‍💻), a regional-indicator pair (🇮🇳), a skin tone, a keycap. So walk it
    rather than splitting per character.
    """
    out: list[str] = []
    i, n = 0, len(run)
    while i < n:
        start = i
        ch = run[i]
        i += 1
        if "\U0001F1E6" <= ch <= "\U0001F1FF":
            # Regional indicator: exactly two make a flag.
            if i < n and "\U0001F1E6" <= run[i] <= "\U0001F1FF":
                i += 1
        else:
            while i < n and run[i] in _EMOJI_MODIFIERS:
                i += 1
            while i < n and run[i] == _ZWJ:
                i += 1                       # the joiner
                if i < n:
                    i += 1                   # the joined base
                while i < n and run[i] in _EMOJI_MODIFIERS:
                    i += 1
        out.append(run[start:i])
    return out


def _tokenize_emoji(s: str) -> list[tuple[str, str]]:
    """Split into ('text'|'emoji', chunk) segments, preserving order. Each
    'emoji' segment is exactly ONE renderable emoji (see _emoji_clusters)."""
    out: list[tuple[str, str]] = []
    pos = 0
    for m in _EMOJI_RE.finditer(s):
        if m.start() > pos:
            out.append(("text", s[pos:m.start()]))
        for cluster in _emoji_clusters(m.group()):
            out.append(("emoji", cluster))
        pos = m.end()
    if pos < len(s):
        out.append(("text", s[pos:]))
    return out


def _emoji_words(text: str) -> list[tuple[bool, str]]:
    """Split into wrap units as `(glued_to_previous, unit)`.

    A unit is one whitespace-delimited word or one emoji cluster. Wrapping
    works on words, and an emoji has to be its own unit or it would be measured
    with the text font (≈0 px, it has no glyph) and lines would overflow by
    exactly the emoji boxes they contain.

    `glued` is what keeps that from changing the text: the wrapper rejoins
    units, and joining unconditionally with " " INSERTED a space at every
    emoji boundary that the writer never typed — "GG🔥🔥" laid out as "GG 🔥 🔥",
    spacing emoji 0.32 em apart when a word space is 0.24 ("the space between
    the emoji is too much"). Splitting a string for measurement must not alter
    what gets drawn.
    """
    units: list[tuple[bool, str]] = []
    prev_ws = True          # start of string separates like whitespace does
    for kind, chunk in _tokenize_emoji(text):
        if kind == "emoji":
            units.append((bool(units) and not prev_ws, chunk))
            prev_ws = False
            continue
        parts = chunk.split()
        if not parts:       # a run of pure whitespace between two emoji
            prev_ws = True
            continue
        for i, w in enumerate(parts):
            units.append((i == 0 and bool(units) and not chunk[:1].isspace(), w))
        prev_ws = chunk[-1:].isspace()
    return units


def _emoji_image(cluster: str, box: int) -> Image.Image | None:
    """Fluent 3D artwork for one emoji cluster, scaled to `box` px. None if
    unavailable (offline and uncached) — the caller then skips it, which is
    the old strip-it behaviour for that one emoji rather than a broken render."""
    try:
        from ..ai.emoji import fetch_emoji_png
        p = fetch_emoji_png(cluster)
        if not p:
            return None
        with Image.open(p) as im:
            return im.convert("RGBA").resize((box, box), Image.LANCZOS)
    except Exception:
        return None


# Per-role rendering style.
ROLE_STYLES: dict[str, dict] = {
    "super":       {"font": "Anton-Regular.ttf",        "size": 140, "fill": (255, 255, 255, 255), "stroke": (0, 0, 0, 255), "stroke_w": 6, "shadow": True},
    "hook":        {"font": "BebasNeue-Regular.ttf",    "size": 170, "fill": (255, 255, 255, 255), "stroke": (0, 0, 0, 255), "stroke_w": 7, "shadow": True},
    "lower_third": {"font": "Montserrat-Bold.ttf",      "size": 56,  "fill": (255, 255, 255, 255), "stroke": (0, 0, 0, 255), "stroke_w": 3, "shadow": True},
    "caption":     {"font": "Inter-Black.ttf",          "size": 64,  "fill": (255, 255, 255, 255), "stroke": (0, 0, 0, 255), "stroke_w": 5, "shadow": True},
    "label":       {"font": "Inter-Bold.ttf",           "size": 48,  "fill": (255, 255, 255, 255), "stroke": (0, 0, 0, 255), "stroke_w": 3, "shadow": True},
    "watermark":   {"font": "Inter-Bold.ttf",           "size": 32,  "fill": (255, 255, 255, 200), "stroke": (0, 0, 0, 140), "stroke_w": 2, "shadow": False},
    "default":     {"font": "Inter-Bold.ttf",           "size": 64,  "fill": (255, 255, 255, 255), "stroke": (0, 0, 0, 255), "stroke_w": 4, "shadow": True},
}


def _font_path(name: str) -> Path:
    p = FONTS_DIR / name
    if not p.exists():
        p = FONTS_DIR / "Inter-Bold.ttf"
    return p


_log = logging.getLogger(__name__)

# ---- per-clip style overrides (TextClip.style) ------------------------------
# TextStyle is a non-nullable field with schema defaults ("#FFFFFF" /
# "Inter-Black") — every TextClip carries a populated TextStyle whether or
# not the caller ever chose one, so those defaults double as "no explicit
# choice — use the role style" sentinels. Honoring them literally would
# restyle every existing clip (hooks would drop BebasNeue for Inter-Black).
#
# The font sentinel is a TWO-PART check: (1) the raw schema default
# ("Inter-Black") always means unset — this is the actual "did the caller
# touch this field" signal, since nothing here tracks Pydantic field-set-ness
# across dict-based construction; (2) a value equal to the RESOLVED ROLE'S
# OWN font also means unset, since requesting your own role's font is a
# semantic no-op regardless of whether the caller "meant" to override.
# (2) alone is wrong on its own: the "default" role's real font is
# Inter-Bold, not Inter-Black, so comparing ONLY per-role would misread the
# schema's default-populated TextStyle on a default-role clip as an explicit
# Inter-Black override — the opposite bug, and the common case.
#
# KNOWN LIMITATION: a caller who explicitly asks for the literal string
# "Inter-Black" on a role whose own font ISN'T Inter-Black is indistinguishable
# from "never touched this field" — (1) always wins. In practice this is
# harmless: that role simply keeps rendering in its own font instead of
# switching to Inter-Black, a reasonable (not wrong) result, not a crash or a
# silently-dropped user-visible promise. Fixing it for real needs `font:
# str | None = None` on TextStyle (a schema migration touching every existing
# TextClip construction site), which is out of scope for this fix.
_STYLE_SENTINEL_COLOR = "#FFFFFF"
_STYLE_SENTINEL_FONT = "Inter-Black"
# TextStyle.size schema default. Any other scalar means "explicit size in
# EDL-canvas pixels" (same coordinate system ROLE_STYLES sizes live in).
_STYLE_SENTINEL_SIZE = 96.0
# TextStyle stroke defaults ("#000000" / 4) — same sentinel posture: the
# schema default means "use the role's stroke", anything else is explicit.
_STYLE_SENTINEL_STROKE = "#000000"
_STYLE_SENTINEL_STROKE_W = 4.0
# TextClip's Transform default is Transform(x=540, y=1700) (edl/schema.py) —
# absolute canvas pixels that no renderer ever read. Honoring them literally
# would move every existing clip, so they are "unset" sentinels, exactly
# like the color/font pair above. See resolve_anchor_overrides for the full
# per-axis sentinel sets (tool defaults + role anchors are sentinels too).
_TRANSFORM_SENTINEL_X = 540.0
_TRANSFORM_SENTINEL_Y = 1700.0


def _parse_hex_color(s: str) -> tuple[int, int, int, int] | None:
    v = (s or "").strip().lstrip("#")
    if len(v) == 6:
        v += "FF"
    if len(v) != 8:
        return None
    try:
        r, g, b, a = (int(v[i:i + 2], 16) for i in (0, 2, 4, 6))
    except ValueError:
        return None
    return (r, g, b, a)


def resolve_style_overrides(c: TextClip, role: str = "default"
                           ) -> tuple[tuple[int, int, int, int] | None, Path | None]:
    """Per-clip (fill_rgba, font_path) overrides from TextClip.style, or Nones.

    This is what makes `add_text(color=..., font=...)` and the brand-kit
    palette/font (materialised into clip styles by show/brand_kit.py) actually
    render — TextStyle used to be accepted, persisted, and silently ignored.

    `role` resolves the font sentinel PER ROLE (see the module comment above)
    rather than against a single global default — this matters concretely
    for role="caption", whose own role font genuinely is "Inter-Black".
    """
    st = getattr(c, "style", None)
    if st is None:
        return None, None
    fill = font = None
    color = (getattr(st, "color", "") or "").strip()
    if color and color.upper() != _STYLE_SENTINEL_COLOR:
        fill = _parse_hex_color(color)
    role_font = ROLE_STYLES.get(role, ROLE_STYLES["default"])["font"].replace(".ttf", "")
    fname = (getattr(st, "font", "") or "").strip()
    if fname and fname != _STYLE_SENTINEL_FONT and fname != role_font:
        p = FONTS_DIR / fname
        if not p.exists():
            p = FONTS_DIR / f"{fname}.ttf"
        if p.exists():
            font = p
    return fill, font


# ---- text animation presets (TextClip.anim_in / anim_out) -------------------
# The full accepted set. Names outside it are ignored WITH a log line (never
# crash a render over a stale EDL) — add_text validates loudly at the tool
# boundary so new bad names can't get in.
ANIM_PRESETS = ("pop", "fade", "slide_up", "slide_down")
ANIM_DUR = 0.35  # seconds, clamped to 40% of the clip at render time


def _anim_name(c: TextClip, attr: str) -> str | None:
    v = (getattr(c, attr, None) or "").strip().lower()
    if not v:
        return None
    if v not in ANIM_PRESETS:
        _log.warning("unknown text animation %r on clip %s — ignoring "
                     "(valid: %s)", v, getattr(c, "id", "?"), ", ".join(ANIM_PRESETS))
        return None
    return v


# Script detection — pick a Noto fallback font when the text uses non-Latin
# codepoints. PIL's truetype rendering can't auto-fallback, so we switch the
# whole text's font when its dominant script is non-Latin. Single-script
# captions cover ~all real-world cases.
def _pick_script_font(text: str) -> Path | None:
    """Return a path to a Noto font if `text` is dominantly a non-Latin script."""
    counts = {"deva": 0, "arab": 0, "cjk": 0, "latin": 0}
    for ch in text:
        cp = ord(ch)
        if 0x0900 <= cp <= 0x097F:
            counts["deva"] += 1
        elif 0x0600 <= cp <= 0x06FF or 0x0750 <= cp <= 0x077F:
            counts["arab"] += 1
        elif (0x4E00 <= cp <= 0x9FFF) or (0x3000 <= cp <= 0x30FF) or (0x3400 <= cp <= 0x4DBF):
            counts["cjk"] += 1
        elif ch.isalpha():
            counts["latin"] += 1
    dominant = max(counts, key=counts.get)
    if counts[dominant] == 0 or dominant == "latin":
        return None
    name = {"deva": "NotoSansDevanagari-VF.ttf",
            "arab": "NotoSansArabic-VF.ttf",
            "cjk":  "NotoSansSC-VF.ttf"}[dominant]
    p = FONTS_DIR / name
    return p if p.exists() else None


def _y_for_role(role: str, transform_y: float | None, canvas_h: int) -> float:
    """Center-y in canvas coords.

    `transform_y` is a RESOLVED override (resolve_anchor_overrides): a float
    means the user explicitly positioned this clip and it beats the role
    anchor; None means role positioning. Captions never get here with a
    float — resolve_anchor_overrides pins caption to (None, None) because
    the captions block owns caption positioning.
    """
    if transform_y is not None and role != "caption":
        return float(transform_y)
    if role == "watermark":
        return canvas_h - canvas_h * 0.04
    if role == "hook":
        return canvas_h * 0.50
    if role == "caption":
        return canvas_h - canvas_h * 0.16
    if role == "lower_third":
        return canvas_h - canvas_h * 0.20
    return canvas_h * 0.75


def resolve_size_override(c: TextClip) -> float | None:
    """Explicit style.size in EDL-canvas px, or None for the role size.

    The schema default (96) is the "never touched" sentinel — honoring it
    literally would resize every existing clip (a hook would drop from 170
    to 96). KNOWN LIMITATION (same class as the Inter-Black font sentinel):
    explicitly asking for exactly 96 is indistinguishable from unset and
    keeps the role size — harmless, not a crash or a dropped promise.
    """
    st = getattr(c, "style", None)
    size = getattr(st, "size", None) if st is not None else None
    if not isinstance(size, (int, float)):
        return None
    if abs(float(size) - _STYLE_SENTINEL_SIZE) < 1e-6 or float(size) <= 0:
        return None
    return float(size)


def resolve_stroke_overrides(c: TextClip
                             ) -> tuple[tuple[int, int, int, int] | None, float | None]:
    """Explicit (stroke_rgba, stroke_w) from TextClip.style, or Nones.

    Schema defaults ("#000000" / 4) are unset sentinels. A role whose own
    stroke_w happens to be 4 (the "default" role) renders identically either
    way, so the collision is a no-op by construction.
    """
    st = getattr(c, "style", None)
    if st is None:
        return None, None
    stroke = sw = None
    raw = (getattr(st, "stroke", "") or "").strip()
    if raw and raw.upper() != _STYLE_SENTINEL_STROKE:
        stroke = _parse_hex_color(raw)
    w = getattr(st, "stroke_w", None)
    if isinstance(w, (int, float)) and w >= 0 and abs(float(w) - _STYLE_SENTINEL_STROKE_W) > 1e-6:
        sw = float(w)
    return stroke, sw


def resolve_anchor_overrides(c: TextClip, role: str,
                             canvas_w: int, canvas_h: int
                             ) -> tuple[float | None, float | None]:
    """Explicit (anchor_x, anchor_y) in EDL-canvas px, or Nones (role layout).

    x/y are ABSOLUTE CANVAS PIXELS of the text anchor (the block's center).
    A value counts as explicit only when it can't be a construction-site
    default, mirroring the color/font sentinel pattern:

      x sentinels: 540 (Transform schema default on TextClip) and
        canvas.w/2 (every tool's default AND the renderer's historic
        hard-coded centering — requesting center is a semantic no-op).
      y sentinels: 1700 (schema default), canvas.h*0.85 (add_text's no-arg
        default, which never matched what actually rendered), and the
        role's OWN anchor y (add_super_text / brand_kit write the anchor
        value itself — again a no-op).

    caption: always (None, None) — the captions block owns caption
    positioning (task/product rule), so a stray transform can't move it.

    Keyframed x/y resolve as None: text x/y were never animated server-side
    and silently baking the last keyframe value would move existing EDLs.

    KNOWN LIMITATION (accepted, same class as the Inter-Black font case):
    explicitly typing a sentinel value (e.g. x exactly 540 on a canvas
    whose center isn't 540, or y exactly at the role anchor) reads as
    unset and renders at the role position — a reasonable result, never a
    crash. After set_canvas/set_aspect_ratio, dispatch's
    _rescale_overlays_for_canvas_change multiplies stored x/y, so a legacy
    sentinel like y=1700 becomes a non-sentinel value at the SAME RELATIVE
    position it always claimed — the clip then renders at that relative
    position instead of snapping to the role anchor, which is exactly what
    the rescale is documented to preserve.
    """
    if role == "caption":
        return None, None
    tx = getattr(c, "transform", None)
    if tx is None:
        return None, None

    def _explicit(v: object, sentinels: tuple[float, ...]) -> float | None:
        if not isinstance(v, (int, float)):
            return None  # keyframed / missing
        f = float(v)
        for s in sentinels:
            if abs(f - s) < 0.5:  # canvas px; rescale float noise tolerance
                return None
        return f

    anchor_y_role = _y_for_role(role, None, canvas_h)
    ax = _explicit(getattr(tx, "x", None),
                   (_TRANSFORM_SENTINEL_X, canvas_w / 2))
    ay = _explicit(getattr(tx, "y", None),
                   (_TRANSFORM_SENTINEL_Y, canvas_h * 0.85, anchor_y_role))
    return ax, ay


def resolve_opacity_override(c: TextClip) -> float | None:
    """Explicit scalar transform.opacity in [0, 1), or None (fully opaque).

    Unlike the color/font/anchor sentinels above there is no collision
    problem: the schema default is 1.0, which is a natural no-op, so any
    scalar below 1 is unambiguously an explicit override. Values are clamped
    to [0, 1].

    ANIMATED opacity resolves as None ("keyframed resolves unset", same
    convention as resolve_anchor_overrides): the PNG stays at full alpha and
    build_overlay_chain's anim_text path animates it per frame via geq —
    baking a keyframe value into the PNG would double-apply it. The browser
    preview mirrors this by treating any non-scalar value as 1.0.

    "Animated" here means `is_keyframed` (>= 2 keyframes) — EXACTLY the
    condition build_overlay_chain uses to pick the anim_text path, so the
    bake and the filtergraph can never both apply (or both skip) the value.
    A DEGENERATE 1-keyframe list is not animated: it takes the static
    branch, where the old code applied `_scalar_or_last` via
    colorchannelmixer, so it must bake here or the clip silently renders
    fully opaque (`add_keyframe` on opacity once produces exactly this).
    """
    tx = getattr(c, "transform", None)
    if tx is None:
        return None
    v = getattr(tx, "opacity", None)
    if v is None or is_keyframed(v):
        return None  # missing, or animated per-frame by the geq path
    f = max(0.0, min(1.0, _scalar_or_last(v, 1.0)))
    return None if f >= 0.999 else f


def _cap_band_mid(draw: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont) -> float:
    """Vertical middle of the cap band, as an offset from the y passed to
    `draw.text()` (Pillow's default 'la' anchor = the ascender top).

    'H' stands in for the band because cap height is what the eye aligns to and
    every font this app loads has one — including the Noto script fallbacks,
    which are Latin-complete. Falls back to the em-box middle if the glyph is
    missing or degenerate rather than letting a zero bbox slam every emoji to
    the top of the line.
    """
    try:
        bb = draw.textbbox((0, 0), "H", font=font)
    except Exception:      # pragma: no cover - a font with no 'H' at all
        return font.size * 0.5
    return (bb[1] + bb[3]) / 2 if bb[3] > bb[1] else font.size * 0.5


def _word_w(word: str, draw: ImageDraw.ImageDraw,
            font: ImageFont.FreeTypeFont, box: int) -> float:
    """Width of one wrap-word: an emoji is its fixed box, text is measured."""
    return _line_w(word, draw, font, box)


def _line_w(line: str, draw: ImageDraw.ImageDraw,
            font: ImageFont.FreeTypeFont, box: int) -> float:
    """Width of a laid-out line, counting emoji as boxes. Text runs are
    measured whole (not per-word) so kerning matches what Pillow draws."""
    total = 0.0
    for kind, chunk in _tokenize_emoji(line):
        total += float(box) if kind == "emoji" else draw.textlength(chunk, font=font)
    return total


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
          max_w: int, box: int = 0) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines():
        # Emoji become their own words so their real (box) width counts toward
        # the line budget — the font measures them at ~0px.
        # (glued, unit) pairs when emoji are in play; a plain split otherwise
        # keeps the no-emoji path byte-identical to what it always produced.
        words = (_emoji_words(paragraph) if box
                 else [(False, w) for w in paragraph.split()])
        if not words:
            lines.append("")
            continue
        cur = words[0][1]
        for glued, w in words[1:]:
            trial = f"{cur}{'' if glued else ' '}{w}"
            width = _line_w(trial, draw, font, box) if box else draw.textlength(trial, font=font)
            if width <= max_w:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


def render_text_png(text: str, role: str, canvas_w: int, canvas_h: int, *,
                    fill: tuple[int, int, int, int] | None = None,
                    font_file: Path | None = None,
                    size: float | None = None,
                    anchor_x: float | None = None,
                    anchor_y: float | None = None,
                    stroke: tuple[int, int, int, int] | None = None,
                    stroke_w: float | None = None,
                    opacity: float | None = None) -> Image.Image:
    """Render a transparent canvas-sized PNG with text drawn for the given role.

    Emoji are stripped from the text before drawing (bundled fonts have no
    emoji glyphs and would render as boxes).

    `fill` / `font_file` / `size` / `stroke` / `stroke_w` are per-clip
    TextStyle overrides (see resolve_style_overrides / resolve_size_override
    / resolve_stroke_overrides); None means use the role style. An explicit
    fill with default alpha inherits the role fill's alpha so e.g. a colored
    watermark keeps its translucency. `size` is in EDL-canvas pixels — the
    same coordinate system ROLE_STYLES sizes live in.

    `anchor_x` / `anchor_y` are per-clip Transform overrides (see
    resolve_anchor_overrides) in ABSOLUTE canvas pixels: the text block is
    centered on the anchor. None keeps the historic layout (horizontal
    centering; vertical role anchor via _y_for_role).

    `opacity` is the resolved scalar transform.opacity (see
    resolve_opacity_override); it multiplies the finished image's alpha
    channel so fill, stroke and shadow all dim uniformly. None means fully
    opaque.
    """
    # Emoji are kept and composited as images below (see _emoji_image). Only a
    # string with NOTHING drawable left is an empty render.
    if not text.strip():
        return Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    style = ROLE_STYLES.get(role, ROLE_STYLES["default"])
    if fill is not None:
        role_alpha = style["fill"][3]
        style = {**style, "fill": (fill[0], fill[1], fill[2],
                                   fill[3] if fill[3] != 255 else role_alpha)}
    if size is not None:
        style = {**style, "size": max(1, int(round(size)))}
    if stroke is not None:
        style = {**style, "stroke": stroke}
    if stroke_w is not None:
        style = {**style, "stroke_w": max(0, int(round(stroke_w)))}
    # Fall back to a Noto script font when the caption isn't Latin. Judged on
    # the TEXT only: emoji live in pictograph blocks that no script font
    # covers, and letting them vote pushed a plain-Latin caption with one 🔥
    # onto a Devanagari font.
    script_font = _pick_script_font(_strip_emoji(text) or text)
    chosen_font = script_font if script_font is not None else (font_file or _font_path(style["font"]))
    font = ImageFont.truetype(str(chosen_font), style["size"])
    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    max_w = int(canvas_w * 0.86)
    box = int(round(font.size * EMOJI_BOX_RATIO))
    cap_mid = _cap_band_mid(draw, font)
    lines = _wrap(draw, text.upper() if role in ("super", "hook") else text,
                  font, max_w, box)
    line_h = font.size + 8
    total_h = line_h * len(lines)
    y_center = _y_for_role(role, anchor_y, canvas_h)
    y_top = int(y_center - total_h / 2)
    x_center = float(anchor_x) if anchor_x is not None else canvas_w / 2
    for i, line in enumerate(lines):
        w = _line_w(line, draw, font, box)
        x = float(x_center - w / 2)
        y = y_top + i * line_h
        # Walk the line's segments left to right, drawing text runs with the
        # font and pasting emoji artwork. One pass per visual layer (shadow,
        # then fill) so an emoji can't land under the next run's shadow.
        segs = _tokenize_emoji(line)
        if style.get("shadow"):
            cx = x
            for kind, chunk in segs:
                if kind == "emoji":
                    cx += box
                    continue
                draw.text((cx + 4, y + 6), chunk, font=font, fill=(0, 0, 0, 140),
                          stroke_width=style["stroke_w"], stroke_fill=(0, 0, 0, 140))
                cx += draw.textlength(chunk, font=font)
        cx = x
        for kind, chunk in segs:
            if kind == "emoji":
                im_e = _emoji_image(chunk, box)
                if im_e is not None:
                    img.alpha_composite(
                        im_e, dest=(int(round(cx)), int(round(y + cap_mid - box / 2))))
                cx += box
                continue
            draw.text((cx, y), chunk, font=font, fill=style["fill"],
                      stroke_width=style["stroke_w"], stroke_fill=style["stroke"])
            cx += draw.textlength(chunk, font=font)
    if opacity is not None and opacity < 0.999:
        # Multiply the finished image's alpha so fill, stroke and shadow dim
        # uniformly — same pattern as the static-sticker opacity bake in
        # cache_sticker_pngs.
        a = img.getchannel("A").point(lambda v: int(v * opacity))
        img.putalpha(a)
    return img


def collect_text_clips(edl: EDL) -> list[tuple[TextClip, str]]:
    """Return all text clips paired with their resolved role.

    Skips tracks that are muted.
    """
    out: list[tuple[TextClip, str]] = []
    for track in edl.tracks:
        if track.type not in ("text", "captions"):
            continue
        if track.muted:
            continue
        for c in track.clips:
            if isinstance(c, TextClip) and c.text.strip():
                out.append((c, c.role or "default"))
    out.sort(key=lambda x: (x[0].start, x[0].end))
    return out


def cache_text_pngs(edl: EDL, cache_dir: Path) -> list[tuple[TextClip, str, Path]]:
    """Render each text clip to a PNG (cached by content hash). Return paired list.

    Cache key uses the FULL text including emoji — they are composited into
    the PNG now (see render_text_png), so two clips differing only by an emoji
    are genuinely different pixels and must not share a cache entry. The `v5`
    prefix below invalidates every entry keyed under the old emoji-stripped
    rule, which would otherwise serve emoji-less art forever.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    canvas = edl.canvas
    paired: list[tuple[TextClip, str, Path]] = []
    for c, role in collect_text_clips(edl):
        displayable = c.text.strip()
        fill, font_file = resolve_style_overrides(c, role)
        size = resolve_size_override(c)
        stroke, stroke_w = resolve_stroke_overrides(c)
        anchor_x, anchor_y = resolve_anchor_overrides(c, role, canvas.w, canvas.h)
        opacity = resolve_opacity_override(c)
        # Every override is part of the pixels, so every override is part of
        # the key — keyed on the RESOLVED values (sentinels normalize to '')
        # so a sentinel-valued clip shares its PNG with an untouched one.
        # (v3→v4 bump also invalidates every pre-transform/size cache entry;
        # without size/x/y in the key, a size edit silently served the
        # stale PNG forever. opacity joined the key when it started baking
        # into the alpha channel — same dead-control bug class otherwise.)
        style_key = f"{fill or ''}|{font_file.name if font_file else ''}"
        geo_key = (f"{'' if size is None else f'{size:.2f}'}|"
                   f"{'' if anchor_x is None else f'{anchor_x:.2f}'},"
                   f"{'' if anchor_y is None else f'{anchor_y:.2f}'}|"
                   f"{stroke or ''}|{'' if stroke_w is None else f'{stroke_w:.2f}'}|"
                   f"{'' if opacity is None else f'{opacity:.3f}'}")
        key = hashlib.sha256(
            # v6: emoji moved from a fixed em-box fraction to the measured cap
            # band, so every PNG holding an emoji changed pixels. The key has no
            # other input that tracks placement, and a stale hit is invisible —
            # the export would silently keep the old misalignment.
            f"v7|{role}|{canvas.w}x{canvas.h}|{style_key}|{geo_key}|{displayable}".encode()
        ).hexdigest()[:16]
        png = cache_dir / f"text_{key}.png"
        if not _png_is_valid(png):
            img = render_text_png(c.text, role, canvas.w, canvas.h,
                                  fill=fill, font_file=font_file,
                                  size=size, anchor_x=anchor_x, anchor_y=anchor_y,
                                  stroke=stroke, stroke_w=stroke_w,
                                  opacity=opacity)
            _save_png_atomic(img, png)
        paired.append((c, role, png))
    return paired


def collect_stickers(edl: EDL) -> list[Sticker]:
    out: list[Sticker] = []
    for track in edl.tracks:
        if track.type != "sticker" or track.muted:
            continue
        for c in track.clips:
            if isinstance(c, Sticker):
                out.append(c)
    # Per-clip z first (set_clip_z override), then legacy start-order for
    # ties — later start still wins at equal z, pinning the old behavior.
    out.sort(key=lambda s: (getattr(s, "z", 0), s.start))
    return out


def _sticker_is_animated(s: Sticker) -> bool:
    """A sticker animates server-side if any of x / y / opacity has keyframes.
    Scale + rotation animate in the browser preview only — the render bakes
    them as their current (last) value."""
    tx = s.transform
    return any(is_keyframed(getattr(tx, p)) for p in ("x", "y", "opacity"))


def _scalar_or_last(v: float | dict | object, default: float = 0.0) -> float:
    """Return either the scalar or the value of the last keyframe."""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        kfs = v.get("keyframes") or []
    else:
        kfs = getattr(v, "keyframes", []) or []
    if not kfs:
        return default
    return float(sorted(kfs, key=lambda p: p[0])[-1][1])


def _render_sticker_smallpng(sticker: Sticker, canvas_w: int, canvas_h: int,
                             dst: Path) -> tuple[int, int] | None:
    """Render the sticker PNG at its natural size (no canvas padding).
    Returns (w, h) of the resulting PNG, or None on failure."""
    try:
        src = Image.open(sticker.src).convert("RGBA")
    except Exception:
        return None
    tx = sticker.transform
    scale = _scalar_or_last(tx.scale, 1.0)
    rotation = _scalar_or_last(tx.rotation, 0.0)
    base = max(canvas_w, canvas_h)
    target_long = max(16, int(base * 0.22 * scale))
    sw, sh = src.size
    if sw >= sh:
        tw = target_long
        th = max(8, int(sh * (tw / sw)))
    else:
        th = target_long
        tw = max(8, int(sw * (th / sh)))
    img = src.resize((tw, th), Image.LANCZOS)
    if abs(rotation) > 0.01:
        img = img.rotate(-rotation, resample=Image.BICUBIC, expand=True)
    _save_png_atomic(img, dst)
    return img.size


def cache_animated_sticker_pngs(edl: EDL, cache_dir: Path
                                ) -> list[tuple[Sticker, Path, tuple[int, int]]]:
    """For animated stickers, render the PNG at natural size (no canvas padding).
    Returns [(sticker, png_path, (w, h)), ...]."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    canvas = edl.canvas
    out: list[tuple[Sticker, Path, tuple[int, int]]] = []
    for s in collect_stickers(edl):
        if not _sticker_is_animated(s):
            continue
        if not s.src or not Path(s.src).exists():
            continue
        scale = _scalar_or_last(s.transform.scale, 1.0)
        rot = _scalar_or_last(s.transform.rotation, 0.0)
        key = hashlib.sha256(
            f"sa|{s.id}|{s.src}|{canvas.w}x{canvas.h}|{scale:.3f}|{rot:.1f}|{Path(s.src).stat().st_mtime}".encode()
        ).hexdigest()[:16]
        dst = cache_dir / f"sa_{key}.png"
        if not _png_is_valid(dst):
            sz = _render_sticker_smallpng(s, canvas.w, canvas.h, dst)
            if sz is None:
                continue
        try:
            with Image.open(dst) as im:
                sz = im.size
        except Exception:
            continue
        out.append((s, dst, sz))
    return out


def cache_sticker_pngs(edl: EDL, cache_dir: Path) -> list[tuple[Sticker, Path]]:
    """For each STATIC sticker, produce a canvas-sized RGBA PNG with the sticker
    image placed at its transform position + scale. Animated stickers use the
    expression-based path in cache_animated_sticker_pngs."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    canvas = edl.canvas
    out: list[tuple[Sticker, Path]] = []
    for s in collect_stickers(edl):
        if _sticker_is_animated(s):
            continue
        if not s.src or not Path(s.src).exists():
            continue
        tx = s.transform
        scalar = float(tx.scale if not isinstance(tx.scale, dict) else 1)
        x = float(tx.x if not isinstance(tx.x, dict) else canvas.w / 2)
        y = float(tx.y if not isinstance(tx.y, dict) else canvas.h / 2)
        opacity = float(tx.opacity if not isinstance(tx.opacity, dict) else 1)
        rotation = float(tx.rotation if not isinstance(tx.rotation, dict) else 0)
        key = hashlib.sha256(
            f"st|{s.id}|{s.src}|{canvas.w}x{canvas.h}|{scalar:.3f}|{x:.1f},{y:.1f}|{opacity:.2f}|{rotation:.1f}|{Path(s.src).stat().st_mtime}".encode()
        ).hexdigest()[:16]
        dst = cache_dir / f"st_{key}.png"
        if not _png_is_valid(dst):
            try:
                src = Image.open(s.src).convert("RGBA")
            except Exception:
                continue
            # Default sticker size is 22% of the canvas's longer edge
            base = max(canvas.w, canvas.h)
            target_long = max(16, int(base * 0.22 * scalar))
            sw, sh = src.size
            if sw >= sh:
                tw = target_long
                th = max(8, int(sh * (tw / sw)))
            else:
                th = target_long
                tw = max(8, int(sw * (th / sh)))
            sticker_img = src.resize((tw, th), Image.LANCZOS)
            if abs(rotation) > 0.01:
                sticker_img = sticker_img.rotate(-rotation, resample=Image.BICUBIC, expand=True)
                tw, th = sticker_img.size
            if opacity < 0.999:
                # Multiply alpha by opacity
                a = sticker_img.split()[-1].point(lambda v: int(v * opacity))
                sticker_img.putalpha(a)
            canvas_img = Image.new("RGBA", (canvas.w, canvas.h), (0, 0, 0, 0))
            paste_x = int(x - tw / 2)
            paste_y = int(y - th / 2)
            canvas_img.alpha_composite(sticker_img, dest=(paste_x, paste_y))
            _save_png_atomic(canvas_img, dst)
        out.append((s, dst))
    return out


def build_overlay_chain(
    edl: EDL,
    cache_dir: Path,
    *,
    source_label: str,
    out_label: str,
    first_input_index: int,
    out_w: int,
    out_h: int,
    preview: bool = False,
) -> tuple[str, list[str], str]:
    """Return (filter_str, extra_inputs, final_label).

    `first_input_index` is the index of the first overlay input we'll add (after
    the existing video clip inputs). Each PNG is added as a new `-i` input.

    `preview`: when True, skip baking TEXT/caption clips AND stickers — the
    browser draws both live over the <video> with no ffmpeg round-trip
    (TextLayer for text/captions, StickerLayer for stickers; Preview.tsx's
    docstring: "no server roundtrip per edit"). Baking them here too doubles
    them up in the preview: server copy + client copy. For text that showed as
    "big and small captions simultaneously" (issue 40, at different sizing
    math); for stickers it showed as a GHOST — the baked copy sits frozen at
    the pre-drag position while the client draws the live one at the pointer,
    so a drag displayed two stickers at once and, after release, the sticker
    "vanished" from the new spot and left a copy at the old one for the whole
    commit→re-render gap (seconds on a long timeline). There is no way to
    erase a baked pixel from the client, so smooth direct manipulation
    requires the client to own the pixels — the same conclusion text reached.

    Export has no TextLayer/StickerLayer, so it always bakes both regardless
    of this flag; this only ever changes what the in-app preview looks like.
    """
    text_paired = [] if preview else cache_text_pngs(edl, cache_dir)
    static_stickers = [] if preview else cache_sticker_pngs(edl, cache_dir)
    animated_stickers = [] if preview else cache_animated_sticker_pngs(edl, cache_dir)

    # Unified item list. Static items get full canvas-sized PNGs that scale to
    # output then overlay at (0,0). Animated stickers get small PNGs overlaid
    # via x/y expressions. Text with keyframed opacity or anim_in/anim_out
    # presets uses an "anim_text" path (looped input + per-frame filters).
    canvas = edl.canvas

    # Track z per clip id: compositing order is the track z index (the design
    # rule CLAUDE.md states). Items are sorted by z below — appending text
    # before stickers unconditionally used to draw every sticker over every
    # text clip regardless of z (e.g. the brand end-card image, a sticker at
    # z=12, covered the end-card text at z=15).
    zmap: dict[str, int] = {}
    for tr in edl.tracks:
        for cl in tr.clips:
            zmap[cl.id] = tr.z

    items: list[dict] = []
    for c, role, png in text_paired:
        opa = getattr(c.transform, "opacity", None) if hasattr(c, "transform") else None
        a_in, a_out = _anim_name(c, "anim_in"), _anim_name(c, "anim_out")
        if is_keyframed(opa) or a_in or a_out:
            items.append({"kind": "anim_text", "text_clip": c, "png": png, "role": role,
                          "anim_in": a_in, "anim_out": a_out, "z": zmap.get(c.id, 0),
                          "sort_start": c.start, "is_sticker": 0})
        else:
            # Scalar transform.opacity is baked into the PNG's alpha channel
            # by cache_text_pngs (resolve_opacity_override) — pass 1.0 here,
            # or the colorchannelmixer branch below would apply it a SECOND
            # time (0.4 baked × aa=0.4 → effective 0.16).
            items.append({"kind": "static", "start": c.start, "end": c.end, "png": png,
                          "opacity": 1.0,
                          "z": zmap.get(c.id, 0),
                          "sort_start": c.start, "is_sticker": 0})
    for s, png in static_stickers:
        items.append({"kind": "static", "start": s.start, "end": s.end, "png": png,
                      "opacity": 1.0, "z": zmap.get(s.id, 0),
                      "clip_z": getattr(s, "z", 0),
                      "sort_start": s.start, "is_sticker": 1})
    for s, png, (sw, sh) in animated_stickers:
        items.append({"kind": "anim", "sticker": s, "png": png, "size": (sw, sh),
                      "z": zmap.get(s.id, 0), "clip_z": getattr(s, "z", 0),
                      "sort_start": s.start, "is_sticker": 1})

    if not items:
        return "", [], source_label

    # Later overlays composite on top. Sort key:
    #   (track_z, clip_z, is_sticker, start)
    #
    # The previous key was (track_z, clip_z) relying on sort() stability to make
    # ties "resolve by start" — a guarantee the code did NOT provide. Static and
    # animated stickers are appended in two SEPARATE blocks above, so at equal
    # (z, clip_z) every static sticker sorted ahead of every animated one
    # regardless of start: a static sticker starting at 5s drew under an animated
    # one starting at 1s. Making `start` an explicit key removes the dependence
    # on insertion order.
    #
    # `is_sticker` MUST stay ahead of `start`: empty_edl gives BOTH the `tx_lt`
    # (Lower thirds, text) and `stickers` tracks z=12, so a bare
    # (z, clip_z, start) key would start interleaving lower-third TEXT with
    # stickers and silently change existing renders. With is_sticker first,
    # text-below-sticker at equal track z is preserved byte-for-byte.
    items.sort(key=lambda it: (it["z"], it.get("clip_z", 0),
                               it.get("is_sticker", 0), it.get("sort_start", 0.0)))

    extra_inputs: list[str] = []
    parts: list[str] = []
    cur = source_label
    for i, item in enumerate(items):
        idx = first_input_index + i
        # Animated overlays (sticker with keyframed opacity, or animated text)
        # need a time-dimension input so per-frame filters actually tick.
        # `-itsoffset {start}` places the looped stream's pts at the clip's
        # ABSOLUTE timeline position: keyframes are clip-local (see
        # edl/keyframes.sample), so filters convert with (T - start). The old
        # form (no offset, `-t` = clip duration) only lined up for clips
        # starting at t≈0 — an animated overlay later on the timeline had
        # finished its whole animation before its enable-window even opened,
        # rendering frozen at the final value. Static items use plain `-i`.
        if item["kind"] == "anim" and is_keyframed(item["sticker"].transform.opacity):
            s: Sticker = item["sticker"]
            dur = max(0.5, s.end - s.start) + 0.5
            extra_inputs += ["-itsoffset", f"{s.start:.3f}",
                             "-loop", "1", "-framerate", "30", "-t", f"{dur:.3f}", "-i", str(item["png"])]
        elif item["kind"] == "anim_text":
            tc = item["text_clip"]
            dur = max(0.5, tc.end - tc.start) + 0.5
            extra_inputs += ["-itsoffset", f"{tc.start:.3f}",
                             "-loop", "1", "-framerate", "30", "-t", f"{dur:.3f}", "-i", str(item["png"])]
        else:
            extra_inputs += ["-i", str(item["png"])]
        is_last = i == len(items) - 1
        next_label = out_label if is_last else f"[ov_post{i}]"

        if item["kind"] == "static":
            scaled = f"[ov{i}]"
            pre = f"[{idx}:v]scale={out_w}:{out_h}"
            opa = float(item.get("opacity", 1.0))
            if opa < 0.999:
                # No current producer sends <1.0 here — text bakes scalar
                # opacity into the PNG in cache_text_pngs, stickers in
                # cache_sticker_pngs — but the branch stays for any future
                # static item whose PNG doesn't carry its own opacity.
                pre += f",format=rgba,colorchannelmixer=aa={opa:.3f}"
            parts.append(pre + scaled)
            parts.append(
                f"{cur}{scaled}overlay=enable='between(t\\,{item['start']:.3f}\\,{item['end']:.3f})'{next_label}"
            )
        elif item["kind"] == "anim_text":
            tc = item["text_clip"]
            role = item["role"]
            a_in, a_out = item["anim_in"], item["anim_out"]
            # Anim duration, clamped so in+out never overlap on short clips.
            d = min(ANIM_DUR, max(0.1, (tc.end - tc.start) * 0.4))
            preprocessed = f"[ov{i}]"

            chain = f"[{idx}:v]scale={out_w}:{out_h},format=rgba"

            # Pop: per-frame scale (verified: scale exposes `t` under
            # eval=frame). In: overshoot 0.6→1.06→1.0; out: shrink to 0.6.
            if a_in == "pop" or a_out == "pop":
                s_terms = []
                if a_in == "pop":
                    q = f"clip((t-{tc.start:.4f})/{d:.4f}\\,0\\,1)"
                    s_terms.append(f"if(lt({q}\\,0.7)\\,0.6+0.657*{q}\\,1.06-0.2*({q}-0.7))")
                if a_out == "pop":
                    q = f"clip((t-{tc.end - d:.4f})/{d:.4f}\\,0\\,1)"
                    s_terms.append(f"(1-0.4*{q})")
                s_expr = "*".join(s_terms)
                chain += (f",scale=w='ceil(iw*({s_expr})/2)*2'"
                          f":h='ceil(ih*({s_expr})/2)*2':eval=frame")

            # Keyframed opacity (the pre-existing path, time-shifted to
            # clip-local now that the input pts sit at absolute time).
            if is_keyframed(getattr(tc.transform, "opacity", None)):
                aexpr = to_ffmpeg_expr(tc.transform.opacity,
                                       time_var=f"(T-{tc.start:.4f})")
                chain += f",geq=r='r(X\\,Y)':g='g(X\\,Y)':b='b(X\\,Y)':a='alpha(X\\,Y)*({aexpr})'"

            # Fades ride the fade filter's alpha mode (cheap, no geq).
            if a_in == "fade":
                chain += f",fade=t=in:st={tc.start:.3f}:d={d:.3f}:alpha=1"
            if a_out == "fade":
                chain += f",fade=t=out:st={tc.end - d:.3f}:d={d:.3f}:alpha=1"
            parts.append(chain + preprocessed)

            # Overlay position. x/y center the (possibly pop-scaled) frame on
            # the text's own anchor: text sits at its anchor inside the
            # canvas-sized PNG (horizontal center by default, explicit
            # transform.x when set; vertical role anchor or explicit
            # transform.y — recomputed here exactly as render_text_png
            # placed it, via the SAME resolve_anchor_overrides so the
            # animated path always agrees with the static one). For non-pop
            # clips overlay_w==main_w / overlay_h==main_h, so both exprs
            # collapse to 0 — identical to the static path.
            anchor_x, anchor_y = resolve_anchor_overrides(tc, role, canvas.w, canvas.h)
            cy = _y_for_role(role, anchor_y, canvas.h) * (out_h / max(1, canvas.h))
            cx = ((float(anchor_x) if anchor_x is not None else canvas.w / 2)
                  * (out_w / max(1, canvas.w)))
            off = out_h * 0.04
            y_terms = [f"{cy:.2f}*(1-overlay_h/main_h)"]
            if a_in == "slide_up":
                y_terms.append(f"+{off:.1f}*(1-clip((t-{tc.start:.4f})/{d:.4f}\\,0\\,1))")
            elif a_in == "slide_down":
                y_terms.append(f"-{off:.1f}*(1-clip((t-{tc.start:.4f})/{d:.4f}\\,0\\,1))")
            if a_out == "slide_up":
                y_terms.append(f"-{off:.1f}*clip((t-{tc.end - d:.4f})/{d:.4f}\\,0\\,1)")
            elif a_out == "slide_down":
                y_terms.append(f"+{off:.1f}*clip((t-{tc.end - d:.4f})/{d:.4f}\\,0\\,1)")
            # x compensation mirrors y: center the (possibly pop-scaled)
            # frame on the anchor x. For a centered anchor cx == out_w/2 and
            # `cx*(1-overlay_w/main_w)` is algebraically `(main_w-overlay_w)/2`
            # — the historic expression — so keep emitting the exact legacy
            # string in that case (byte-identical filtergraphs for every
            # existing project).
            if anchor_x is None:
                x_expr = "(main_w-overlay_w)/2"
            else:
                x_expr = f"{cx:.2f}*(1-overlay_w/main_w)"
            parts.append(
                f"{cur}{preprocessed}overlay=x='{x_expr}':y='{''.join(y_terms)}'"
                f":enable='between(t\\,{tc.start:.3f}\\,{tc.end:.3f})'{next_label}"
            )
        else:
            s: Sticker = item["sticker"]
            sw, sh = item["size"]  # PNG natural pixel size (canvas-aligned)
            tx = s.transform
            tvar = f"(t-{s.start:.4f})"  # clip-local time inside expressions
            sx = out_w / max(1, canvas.w)
            sy = out_h / max(1, canvas.h)
            # The PNG is at canvas-pixel size; rescale to match output pixels.
            sticker_out_w = max(2, int(round(sw * sx)))
            sticker_out_h = max(2, int(round(sh * sy)))

            # Pre-scale the sticker stream so overlay_w / overlay_h match
            # output-pixel size — the centering math then works.
            scaled_label = f"[ovs{i}]"
            parts.append(f"[{idx}:v]scale={sticker_out_w}:{sticker_out_h}{scaled_label}")
            sticker_stream = scaled_label

            # Position. Center on (x, y): subtract overlay_w/_h via ffmpeg vars.
            if is_keyframed(tx.x):
                xe = to_ffmpeg_expr(tx.x, time_var=tvar)
                xexpr = f"({xe})*{sx:.6f}-overlay_w/2"
            else:
                xc = _scalar_or_last(tx.x, canvas.w / 2)
                xexpr = f"{xc * sx - sticker_out_w / 2:.2f}"
            if is_keyframed(tx.y):
                ye = to_ffmpeg_expr(tx.y, time_var=tvar)
                yexpr = f"({ye})*{sy:.6f}-overlay_h/2"
            else:
                yc = _scalar_or_last(tx.y, canvas.h / 2)
                yexpr = f"{yc * sy - sticker_out_h / 2:.2f}"

            # Opacity. Animated → geq with `T` (now meaningful because we loop
            # the input above so the still PNG becomes a video stream). Static
            # → cheap colorchannelmixer.
            preprocessed = f"[ov{i}]"
            if is_keyframed(tx.opacity):
                # Keyframes are clip-local; the looped input's pts sit at
                # absolute time via -itsoffset (see the input-building comment
                # above), so shift: local = T - start.
                aexpr = to_ffmpeg_expr(tx.opacity, time_var=f"(T-{s.start:.4f})")
                parts.append(
                    f"{sticker_stream}format=yuva420p,"
                    f"geq=r='r(X\\,Y)':g='g(X\\,Y)':b='b(X\\,Y)':a='alpha(X\\,Y)*({aexpr})'"
                    f"{preprocessed}"
                )
            else:
                opa = _scalar_or_last(tx.opacity, 1.0)
                if opa < 0.999:
                    parts.append(f"{sticker_stream}format=yuva420p,colorchannelmixer=aa={opa:.3f}{preprocessed}")
                else:
                    parts.append(f"{sticker_stream}null{preprocessed}")

            parts.append(
                f"{cur}{preprocessed}overlay=x='{xexpr}':y='{yexpr}':enable='between(t\\,{s.start:.3f}\\,{s.end:.3f})'{next_label}"
            )
        cur = next_label
    return ";".join(parts), extra_inputs, cur
