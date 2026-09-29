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
import math
import os
import re
import threading
from contextvars import ContextVar
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

from ..config import FONTS_DIR
from .fonts import resolve_font
from ..edl import EDL
from ..edl.schema import TextClip, Sticker
from ..edl.keyframes import frame_exact_expr, is_keyframed, sample
from .. import platformutil as _pu
from ..edl import timebase
from ..edl import clip_animations as _clip_anim
from . import clock
from . import shaping as _shaping


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
# the very same fetched PNG a sticker uses (Apple/iOS artwork; see ai/emoji.py
# for why it is fetched rather than fonted). One artwork source for the whole
# app, identical on macOS and Windows, identical between preview and export.
#
# NAME COLLISION, worth reading twice: this module ALSO has a bundled Noto
# fallback for non-Latin TEXT, and the two are unrelated. The emoji artwork is
# fetched PNGs from a pinned release; the text fallback is a local font file.
# Trying to draw an emoji with the text font would put us straight back in the
# "Pillow can only draw colour emoji at fixed bitmap sizes" hole above, and on
# a machine without it, back to emoji vanishing entirely.

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

# Fraction of that box the ARTWORK actually fills; the remainder is side
# bearing, centred. The advance stays EMOJI_BOX_RATIO, so nothing about
# wrapping changes — only the paste size and offset.
#
# This exists because a text glyph carries its own side bearing and a PNG does
# not, so an emoji butts straight against the letter beside it — surfaced as
# "GG🔥" rendering with the G's stroke touching the emoji.
#
# The tempting fix is "the artwork already has margin, leave it alone". It does
# not, and this was measured rather than assumed. Within APPLE ALONE the side
# margin runs 0.000 (😊 😂 🎉 — literally edge-to-edge) to 0.119 (🔥); across the
# other sources in the chain it runs 0.000 (Twemoji, Fluent flags) to 0.131
# (Noto fire). So without a bearing of our own, two emoji SIDE BY SIDE IN ONE
# LINE are spaced by whatever their respective artists happened to leave — and
# a face lands flush against neighbouring text every time.
#
# 0.92 gives 4% a side: enough to clear a stroked glyph, small enough not to
# reintroduce the complaint that fixed the previous spacing bug from the other
# direction (0.32em between emoji read as "the space is too much").
# TextLayer.tsx mirrors this; the two must move together or the preview spaces
# differently from the bake.
EMOJI_INK_RATIO = 0.92


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


#: Set while a text PNG renders (`_render_marking`): True once an emoji in
#: it could not be drawn from the downloaded artwork (drawn from the local
#: stand-in, or not at all). Such a PNG is never stored under its cache key.
_EMOJI_DEGRADED: ContextVar[list[bool] | None] = ContextVar("_EMOJI_DEGRADED", default=None)


def _mark_degraded() -> None:
    flag = _EMOJI_DEGRADED.get()
    if flag is not None:
        flag[0] = True


def _emoji_image(cluster: str, box: int) -> Image.Image | None:
    """Emoji artwork for one cluster, scaled to `box` px: the downloaded
    artwork (`fetch_emoji_png`) or, when that is unavailable (offline and
    uncached), the same Apple artwork drawn from the installed emoji font
    (`local_emoji_png`, what `add_sticker` uses offline). Final QA (round 3):
    it used only the download, so an offline export left every emoji out as
    a blank gap, silently. Either fallback marks the PNG degraded
    (`_EMOJI_DEGRADED`) so it is re-rendered once the network is back. None
    when neither has it — the caller then skips that one emoji."""
    from ..ai import emoji as _emoji
    p = None
    try:
        p = _emoji.fetch_emoji_png(cluster)
    except Exception:  # noqa: BLE001 — a failed download is the offline case
        p = None
    if not p:
        _mark_degraded()
        try:
            p = _emoji.local_emoji_png(cluster)
        except Exception:  # noqa: BLE001
            p = None
    if not p:
        return None
    try:
        with Image.open(p) as im:
            return im.convert("RGBA").resize((box, box), Image.LANCZOS)
    except Exception:  # noqa: BLE001 — unreadable art: skip that one emoji
        _mark_degraded()
        return None


def _render_marking(fn, *args, **kwargs):
    """`(fn(*args, **kwargs), degraded)`: whether any emoji in that render
    fell back from the downloaded artwork (`_emoji_image`)."""
    flag = [False]
    token = _EMOJI_DEGRADED.set(flag)
    try:
        return fn(*args, **kwargs), flag[0]
    finally:
        _EMOJI_DEGRADED.reset(token)


def _degraded_path(png: Path) -> Path:
    """Where a degraded text PNG is written instead of its cache key's path:
    never looked up, so the next render tries the downloaded artwork again
    (final QA round 3 — the offline PNG was reused by every later export)."""
    return png.with_name(f"{png.stem}.degraded{png.suffix}")


# Per-role rendering style.
ROLE_STYLES: dict[str, dict] = {
    "super":       {"font": "Anton-Regular.ttf",        "size": 140, "fill": (255, 255, 255, 255), "stroke": (0, 0, 0, 255), "stroke_w": 6, "shadow": True, "upper": True},
    "hook":        {"font": "BebasNeue-Regular.ttf",    "size": 170, "fill": (255, 255, 255, 255), "stroke": (0, 0, 0, 255), "stroke_w": 7, "shadow": True, "upper": True},
    "lower_third": {"font": "Montserrat-Bold.ttf",      "size": 56,  "fill": (255, 255, 255, 255), "stroke": (0, 0, 0, 255), "stroke_w": 3, "shadow": True},
    "caption":     {"font": "Inter-Black.ttf",          "size": 64,  "fill": (255, 255, 255, 255), "stroke": (0, 0, 0, 255), "stroke_w": 5, "shadow": True},
    "label":       {"font": "Inter-Bold.ttf",           "size": 48,  "fill": (255, 255, 255, 255), "stroke": (0, 0, 0, 255), "stroke_w": 3, "shadow": True},
    "watermark":   {"font": "Inter-Bold.ttf",           "size": 32,  "fill": (255, 255, 255, 200), "stroke": (0, 0, 0, 140), "stroke_w": 2, "shadow": False},
    "default":     {"font": "Inter-Bold.ttf",           "size": 64,  "fill": (255, 255, 255, 255), "stroke": (0, 0, 0, 255), "stroke_w": 4, "shadow": True},
}


def _font_path(name: str) -> Path:
    # Resolved by bundled NAME only (render/fonts.py): `FONTS_DIR / name`
    # let an absolute or ../ value escape FONTS_DIR and 500 every export.
    return resolve_font(name) or (FONTS_DIR / "Inter-Bold.ttf")


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
    # EDL v3 (QA-076): `style.font` is None when unset, so every string is an
    # explicit choice — including "Inter-Black", which the v2 sentinel made
    # impossible to pick. v2 projects are migrated on load (edl/schema.py).
    fname = (getattr(st, "font", "") or "").strip().removesuffix(".ttf")
    if fname and fname != role_font:
        font = resolve_font(fname)
    return fill, font


# ---- text animation presets (TextClip.anim_in / anim_out) -------------------
# The full accepted set. Names outside it are ignored WITH a log line (never
# crash a render over a stale EDL) — add_text validates loudly at the tool
# boundary so new bad names can't get in.
ANIM_PRESETS = ("pop", "fade", "slide_up", "slide_down")
ANIM_DUR = 0.35  # seconds, clamped to 40% of the clip at render time
#: Bounds of a clip's own `anim_dur` (QA-078). Mirrored by lib/textAnim.ts.
ANIM_DUR_RANGE = (0.1, 3.0)


def anim_duration(c: TextClip, rs: float, re: float) -> float:
    """How long each in/out animation lasts on screen: the clip's own
    `anim_dur` (QA-078) or the house ANIM_DUR, never more than 40 % of the
    RENDER window (so in and out never overlap) and never under 0.1 s."""
    want = getattr(c, "anim_dur", None)
    base = ANIM_DUR if not isinstance(want, (int, float)) else \
        min(ANIM_DUR_RANGE[1], max(ANIM_DUR_RANGE[0], float(want)))
    return min(base, max(0.1, (re - rs) * 0.4))


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


#: Bundled face per script for a RUN the clip's font does not cover
#: (render/shaping.py itemises every line by script). `_pick_script_font`
#: still picks the clip's PRIMARY face by dominant script — so single-script
#: text renders exactly as before — and this covers the rest: a Devanagari
#: line inside a Latin-dominant caption used to draw every glyph as .notdef.
_SCRIPT_FALLBACK_FONTS = {"Deva": "NotoSansDevanagari-VF.ttf",
                          "Arab": "NotoSansArabic-VF.ttf",
                          "Hani": "NotoSansSC-VF.ttf"}


def _script_fallback(role: str, size_px: int):
    """`ShapedFont` fallback: script tag -> a Noto face at the role's script
    weight (SCRIPT_FONT_WEIGHT; the SC face is not weighted, as before)."""
    def get(script: str):
        name = _SCRIPT_FALLBACK_FONTS.get(script)
        p = FONTS_DIR / name if name else None
        if p is None or not p.exists():
            return None
        w = None if "SC" in name else SCRIPT_FONT_WEIGHT.get(role, SCRIPT_FONT_WEIGHT["default"])
        return _shaping.ShapedFont(p, size_px, w)
    return get


#: Caption anchor on a PORTRAIT canvas, as a fraction of canvas height. The
#: historic `canvas_h - 0.16·h` = 0.84·h sits under the TikTok/Reels UI (their
#: chrome covers the bottom ~20% of a 9:16 frame — baseline finding 7 /
#: spec §2.8 SAFE_ZONES); 0.76 keeps captions inside the 9:16 safe zone
#: `y ∈ [0.10, 0.78]`. Landscape and square canvases keep 0.84 so every
#: existing 16:9 / 1:1 project renders byte-identically.
CAPTION_PORTRAIT_Y_FRAC = 0.76


def caption_anchor_y(canvas_w: int | None, canvas_h: int) -> float:
    """The captions block's own center-y for the `caption` role.

    Captions ignore per-clip transforms by design (resolve_anchor_overrides
    returns (None, None) for them — "the captions block owns caption
    positioning"), so a platform-aware default has to live HERE, not in the
    tool that lays the track: `add_caption_track` writes the same value into
    the EDL for honesty, but this function is what the pixels follow.
    `canvas_w=None` (a caller that only knows the height) means the historic
    landscape anchor. Mirrored by `serverAnchorY` / the caption branch in
    frontend TextLayer.tsx.
    """
    if canvas_w is not None and canvas_h > canvas_w:
        return canvas_h * CAPTION_PORTRAIT_Y_FRAC
    return canvas_h - canvas_h * 0.16


def _y_for_role(role: str, transform_y: float | None, canvas_h: int,
                canvas_w: int | None = None) -> float:
    """Center-y in canvas coords.

    `transform_y` is a RESOLVED override (resolve_anchor_overrides): a float
    means the user explicitly positioned this clip and it beats the role
    anchor; None means role positioning. Captions never get here with a
    float — resolve_anchor_overrides pins caption to (None, None) because
    the captions block owns caption positioning; their anchor comes from
    `caption_anchor_y`, which needs `canvas_w` to tell portrait from
    landscape (callers that omit it get the historic landscape anchor).
    """
    if transform_y is not None and role != "caption":
        return float(transform_y)
    if role == "watermark":
        return canvas_h - canvas_h * 0.04
    if role == "hook":
        return canvas_h * 0.50
    if role == "caption":
        return caption_anchor_y(canvas_w, canvas_h)
    if role == "lower_third":
        return canvas_h - canvas_h * 0.20
    return canvas_h * 0.75


def block_anchor_y(role: str, anchor_y: float | None, canvas_h: int, canvas_w: int | None) -> float:
    """The y a block is laid out on. A caption's `anchor_y` is its captions
    track's position (caption_position_y, QA-075) and IS honoured here; a
    cue's own transform never reaches it (resolve_anchor_overrides pins
    captions to None), which is the rule `_y_for_role` keeps for everyone else."""
    if role == "caption" and anchor_y is not None:
        return float(anchor_y)
    return _y_for_role(role, anchor_y, canvas_h, canvas_w)


def resolve_upper_override(c: TextClip, role: str) -> bool:
    """Whether this clip renders ALL-CAPS: explicit `style.upper`, else the role.

    The caps rule used to be a hardcoded `role in ("super", "hook")` right at the
    draw call, with no way to override it from the EDL — so "Text layer only
    shows capital alphabets and doesn't support the small alphabets" was exactly
    true of those two roles, in the preview AND the export.

    It now lives in ROLE_STYLES, mirroring the client's own role table
    (TextLayer.tsx), because that is the one place the two renderers already
    agree role by role — a second hardcoded list is how they drift.

    `None` means untouched, so an existing project's hooks and supers keep their
    capitals; only an explicit False lowercases one.
    """
    st = getattr(c, "style", None)
    want = getattr(st, "upper", None) if st is not None else None
    if isinstance(want, bool):
        return want
    return bool(ROLE_STYLES.get(role, ROLE_STYLES["default"]).get("upper", False))


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
        kfs = (v.get("keyframes") if isinstance(v, dict)
               else getattr(v, "keyframes", None))
        if kfs is not None and len(kfs) == 1:
            # A DEGENERATE one-key list is a constant someone set on purpose
            # (add_keyframe's first key): honour it, never a sentinel.
            return float(kfs[0][1])
        if not isinstance(v, (int, float)):
            return None  # animated (>= 2 keys — the xform path) / missing
        f = float(v)
        for s in sentinels:
            if abs(f - s) < 0.5:  # canvas px; rescale float noise tolerance
                return None
        return f

    # EDL v3 (QA-076): a scalar x/y renders where it says, except the TextClip
    # schema default of each axis (x 540, y 1700 — "never positioned on this
    # axis"; every tool writes real values, and the UI nudges a typed or
    # dragged 540/1700 by a pixel). The v2 sentinels that depended on the
    # canvas and role (canvas.w/2, canvas.h·0.85, the role anchor) collided
    # with ordinary values — a typed Y of 918 on 1080p snapped 76 px while 919
    # did not — and are gone; v2 projects are migrated on load.
    ax = _explicit(getattr(tx, "x", None), (_TRANSFORM_SENTINEL_X,))
    ay = _explicit(getattr(tx, "y", None), (_TRANSFORM_SENTINEL_Y,))
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


# ---- THE SHARED TEXT LAYOUT MODEL (QA-015) ----------------------------------
#
# Preview (frontend/src/lib/textLayout.ts, drawn by TextLayer.tsx) and export
# (this module) used to lay text out by two different rules, so text landed
# 20-50 px apart and the export outline was about twice as heavy:
#   * vertical — the server stacked (size+8)-px line boxes and drew from
#     Pillow's ASCENDER TOP, the client centred an em box (textBaseline
#     'middle') at 1.15 x size: the ink sat 18 px lower in the export at
#     size 120, more for bigger text and for multi-line blocks;
#   * role anchors — the client had its own table (label at the TOP, 'lower'
#     at 0.78 h, captions measured up from the bottom) while the export used
#     `_y_for_role`;
#   * outline — Pillow's `stroke_width` grows the glyph OUTWARD by the full
#     width, canvas `lineWidth` is centred on the path so only half of it
#     shows outside the fill; and the shadow was a hard offset copy here but a
#     blurred shadowBlur there.
#
# ONE model now, and both sides implement it from their own font metrics:
#   1. The block is centred on its anchor: x = transform.x or canvas centre,
#      y = transform.y or the role anchor `_y_for_role` (the client mirrors it
#      exactly — `serverAnchorY` used to be only a sentinel table there).
#   2. Line i of n is centred at  y + (i - (n-1)/2) * LINE_HEIGHT_RATIO * size.
#   3. "Centred" means the CAP BAND ('H' top to baseline) is centred on that
#      line centre: baseline = centre - (H_top + H_bottom) / 2, each side
#      measuring 'H' in its own rasteriser — the only common ground between
#      an ascender-top origin and an em-box origin.
#   4. Outline: `stroke_w` canvas px OUTSIDE the glyph edge (Pillow's
#      semantics; the client strokes at lineWidth = 2 x stroke_w under the
#      fill so exactly stroke_w shows).
#   5. Shadow (roles with shadow=True): a hard copy of fill+outline, offset
#      SHADOW_OFFSET canvas px, black at SHADOW_ALPHA — no blur on either side.
#   6. Transform scale k multiplies every length of the block (size, outline,
#      shadow offset, line height, wrap width); rotation turns the finished
#      block about its anchor (clockwise, degrees).
# `frontend/src/lib/__fixtures__/text_layout_cases.json` pins the numbers for
# BOTH sides (tests/test_text_layout_contract.py + lib/textLayout.test.ts).
LINE_HEIGHT_RATIO = 1.15
SHADOW_OFFSET = (4.0, 6.0)
SHADOW_ALPHA = 140
WRAP_WIDTH_RATIO = 0.86

#: `wght` the Noto script fallbacks (Devanagari / Arabic, both variable fonts)
#: render at, per role. They used to render at the VF default (400) while the
#: preview asked the browser for the role's CSS weight (700/900) — a visibly
#: lighter export. Mirrored by `SCRIPT_WEIGHT` in lib/textLayout.ts.
SCRIPT_FONT_WEIGHT: dict[str, int] = {
    "super": 700, "hook": 700, "lower_third": 700, "caption": 900,
    "label": 700, "watermark": 700, "default": 700,
}


def line_centers(anchor_y: float, n_lines: int, size: float, spacing: float = 1.0) -> list[float]:
    """Rule 2 of the shared model: each line's cap-band centre, top to bottom.
    `spacing` is the clip's `style.line_spacing` (QA-078), a multiplier."""
    lh = size * LINE_HEIGHT_RATIO * spacing
    return [anchor_y + (i - (n_lines - 1) / 2.0) * lh for i in range(n_lines)]


# ---- rule 7 (QA-078): alignment and the background box ----------------------
# Lines are aligned INSIDE the block, and the block stays centred on its
# anchor: `left` starts every line at the widest line's left edge, `right`
# ends it at the right edge. The box spans the widest line plus
# BG_PAD_X_RATIO·size each side, and the stacked line boxes (line height
# each, centred on the first/last line centre) plus BG_PAD_Y_RATIO·size above
# and below, corners rounded BG_RADIUS_RATIO·size — every length × the
# transform scale, drawn UNDER the shadow and text, turned with the block.
BG_PAD_X_RATIO = 0.3
BG_PAD_Y_RATIO = 0.08
BG_RADIUS_RATIO = 0.18


def line_x(align: str, anchor_x: float, block_w: float, w: float) -> float:
    """Left edge of a line of width `w` in a block of width `block_w`."""
    if align == "left":
        return anchor_x - block_w / 2
    if align == "right":
        return anchor_x + block_w / 2 - w
    return anchor_x - w / 2


def background_rect(anchor_x: float, centers: list[float], block_w: float, size: float,
                    spacing: float = 1.0) -> tuple[float, float, float, float, float]:
    """(left, top, right, bottom, radius) of the background box."""
    lh = size * LINE_HEIGHT_RATIO * spacing
    px, py = BG_PAD_X_RATIO * size, BG_PAD_Y_RATIO * size
    return (anchor_x - block_w / 2 - px, centers[0] - lh / 2 - py,
            anchor_x + block_w / 2 + px, centers[-1] + lh / 2 + py, BG_RADIUS_RATIO * size)


# ---- rule 8 (QA-078): letter spacing -----------------------------------------
# `style.letter_spacing` canvas px (× the transform scale) of extra advance
# after every grapheme cluster of a SIMPLE-script run and after every emoji —
# between clusters only, so a line's width grows by (clusters − 1) · spacing.
# A spaced cluster is shaped / measured on its own (no kerning or ligature
# across a spaced pair — CSS letter-spacing drops ligatures too). A complex-
# script run (Devanagari, Arabic, … `shaping.needs_shaping`) is never spaced:
# pulling its clusters apart breaks the joins and the headline stroke; and a
# right-to-left line is not spaced at all. The preview (lib/textLayout.ts
# `trackedUnits`/`trackedWidth`) lays out by the same rule from its own glyph
# measurements; the fixture's `block.letter_spacing` pins the arithmetic.
LETTER_SPACING_RANGE = (-20.0, 100.0)


def tracked_width(advances: list[float], tracked: list[bool], spacing: float) -> float:
    """Rule 8's arithmetic: units of `advances` wide, each `tracked` one but
    the LAST followed by `spacing`."""
    n = len(advances)
    return sum(advances) + sum(spacing for i in range(n - 1) if tracked[i]) if n else 0.0


def _simple_clusters(chunk: str) -> list[tuple[str, bool]]:
    """(unit, tracked) for a text chunk: complex-script pieces whole and
    untracked, every other piece split into tracked grapheme clusters —
    the same pieces the shaped path spaces (shaping._split_scripts)."""
    out: list[tuple[str, bool]] = []
    for _script, piece in _shaping._split_scripts(chunk):
        if _shaping.needs_shaping(piece):
            out.append((piece, False))
        else:
            out.extend((cl, True) for cl in _shaping.grapheme_clusters(piece))
    return out


def resolve_block_overrides(c: TextClip) -> dict:
    """The QA-078 block style of a clip: background rgba (or None), align,
    line spacing, shadow override (None = the role's). Defaults render exactly
    as before, so no existing project moves."""
    st = getattr(c, "style", None)
    bg = _parse_hex_color(getattr(st, "background", None) or "") if st is not None else None
    align = getattr(st, "align", "center") if st is not None else "center"
    spacing = getattr(st, "line_spacing", 1.0) if st is not None else 1.0
    shadow = getattr(st, "shadow_on", None) if st is not None else None
    tracking = getattr(st, "letter_spacing", 0.0) if st is not None else 0.0
    return {"background": bg, "align": align if align in ("left", "right") else "center",
            "line_spacing": float(spacing or 1.0), "shadow": shadow if isinstance(shadow, bool) else None,
            "letter_spacing": float(tracking or 0.0)}


def block_key(b: dict) -> str:
    """Cache-key fragment for resolve_block_overrides' result."""
    tracking = float(b.get("letter_spacing") or 0.0)
    return (f"bg{b['background'] or ''}|{b['align']}|ls{b['line_spacing']:.3f}|"
            f"sh{'' if b['shadow'] is None else int(b['shadow'])}"
            # Only when set, so every existing clip keeps its cached PNG.
            + (f"|lsp{tracking:.3f}" if tracking else ""))


def caption_position_y(edl: EDL) -> float | None:
    """The caption anchor y for the captions track's `config.position`
    (QA-075), or None for `bottom` — the platform-aware caption_anchor_y, so
    every existing project renders unchanged. `center` is the frame's middle;
    `top` sits just inside the 9:16 safe zone (y ≥ 0.10·h) on portrait and at
    0.12·h elsewhere. Mirrored by lib/textLayout.captionAnchorY."""
    cap = edl.get_track("captions")
    pos = getattr(getattr(cap, "config", None), "position", "bottom") if cap else "bottom"
    w, h = edl.canvas.w, edl.canvas.h
    if pos == "center":
        return h * 0.5
    if pos == "top":
        return h * (0.14 if h > w else 0.12)
    return None


def resolve_scale_rotation(c: TextClip, role: str) -> tuple[float, float]:
    """STATIC (scale, rotation) of a text clip: a scalar, a 1-key list's value,
    or — for an animated property — its last key (the xform path animates it
    per frame, so this is only what the static bake would use). Captions
    return (1, 0): the captions block owns caption geometry, the same rule as
    resolve_anchor_overrides. QA-036: both were accepted by add_keyframe /
    set_property / set_clip_transform and ignored by preview and export."""
    if role == "caption":
        return 1.0, 0.0
    tx = getattr(c, "transform", None)
    if tx is None:
        return 1.0, 0.0
    scale = max(0.01, _scalar_or_last(getattr(tx, "scale", 1.0), 1.0))
    rot = _scalar_or_last(getattr(tx, "rotation", 0.0), 0.0)
    return scale, rot


def is_xform_text(c: TextClip, role: str) -> bool:
    """True when x / y / scale / rotation is ANIMATED (>= 2 keys): the clip
    then renders through the per-frame transform path in build_overlay_chain
    (QA-036) instead of a canvas-sized still. Captions never do."""
    if role == "caption":
        return False
    tx = getattr(c, "transform", None)
    return tx is not None and any(
        is_keyframed(getattr(tx, p, None)) for p in ("x", "y", "scale", "rotation"))


def _cap_band(font: ImageFont.FreeTypeFont) -> tuple[float, float]:
    """(top, bottom) of the 'H' ink relative to the BASELINE (top < 0).

    'H' stands in for the cap band because cap height is what the eye aligns
    to and every font this app loads has one — including the Noto script
    fallbacks, which are Latin-complete. Falls back to 0.7 em if the glyph is
    missing or degenerate.
    """
    try:
        bb = font.getbbox("H", anchor="ls")
    except Exception:      # pragma: no cover - a font with no 'H' at all
        return -font.size * 0.7, 0.0
    if bb[3] <= bb[1]:
        return -font.size * 0.7, 0.0
    return float(bb[1]), float(bb[3])


def _cap_band_mid(draw: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont) -> float:
    """Vertical middle of the cap band relative to the BASELINE (negative)."""
    top, bottom = _cap_band(font)
    return (top + bottom) / 2


def _set_weight(font: ImageFont.FreeTypeFont, weight: float | None) -> None:
    """Apply a `wght` to a variable font (no-op for a static font)."""
    if weight is None:
        return
    try:
        axes = font.get_variation_axes()
    except Exception:
        return
    vals = []
    for ax in axes:
        name = ax.get("name")
        if name in (b"Weight", "Weight"):
            vals.append(max(ax["minimum"], min(ax["maximum"], float(weight))))
        else:
            vals.append(ax["default"])
    try:
        font.set_variation_by_axes(vals)
    except Exception:
        pass


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


def _tracked_line_w(line: str, draw: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont,
                    box: int, tracking: float) -> float:
    """Rule 8 width of a line on the shaper-less (Latin-only) path."""
    adv: list[float] = []
    for kind, chunk in _tokenize_emoji(line):
        if kind == "emoji":
            adv.append(float(box))
        else:
            adv.extend(draw.textlength(u, font=font) for u, _t in _simple_clusters(chunk))
    return tracked_width(adv, [True] * len(adv), tracking)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
          max_w: int, box: int = 0, measure=None) -> list[str]:
    """Greedy word wrap. `measure(line) -> px` overrides the Pillow width —
    the shaped (complex-script) path measures SHAPED advances, which is the
    width that is actually drawn (a conjunct is narrower than its letters)."""
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
            if measure is not None:
                width = measure(trial)
            else:
                width = _line_w(trial, draw, font, box) if box else draw.textlength(trial, font=font)
            if width <= max_w:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


def _paste_emoji(img: Image.Image, cluster: str, x: float, cy: float, box: int) -> None:
    """Emoji artwork at `ink`, centred in its `box` advance and on the line's
    cap-band centre `cy` (rule 3 — the line centre IS the cap-band middle)."""
    ink = max(1, int(round(box * EMOJI_INK_RATIO)))
    pad = (box - ink) / 2
    im_e = _emoji_image(cluster, ink)
    if im_e is not None:
        img.alpha_composite(im_e, dest=(int(round(x + pad)), int(round(cy - ink / 2))))


def render_text_png(text: str, role: str, canvas_w: int, canvas_h: int, *,
                    fill: tuple[int, int, int, int] | None = None,
                    font_file: Path | None = None,
                    size: float | None = None,
                    anchor_x: float | None = None,
                    anchor_y: float | None = None,
                    stroke: tuple[int, int, int, int] | None = None,
                    stroke_w: float | None = None,
                    opacity: float | None = None,
                    # None keeps the role's own default (see resolve_upper_override);
                    # an explicit bool is the caller's choice. A keyword with a
                    # None default so every existing caller is unaffected.
                    upper: bool | None = None,
                    scale: float = 1.0,
                    rotation: float = 0.0,
                    surface: tuple[int, int] | None = None,
                    background: tuple[int, int, int, int] | None = None,
                    align: str = "center",
                    line_spacing: float = 1.0,
                    shadow: bool | None = None,
                    letter_spacing: float = 0.0) -> Image.Image:
    """Render a transparent canvas-sized PNG with text drawn for the given role,
    laid out by the SHARED text layout model above (QA-015).

    `fill` / `font_file` / `size` / `stroke` / `stroke_w` are per-clip
    TextStyle overrides (see resolve_style_overrides / resolve_size_override
    / resolve_stroke_overrides); None means use the role style. An explicit
    fill with default alpha inherits the role fill's alpha so e.g. a colored
    watermark keeps its translucency. `size` is in EDL-canvas pixels — the
    same coordinate system ROLE_STYLES sizes live in.

    `anchor_x` / `anchor_y` are per-clip Transform overrides (see
    resolve_anchor_overrides) in ABSOLUTE canvas pixels: the text block is
    centered on the anchor. None keeps the role layout (horizontal centering;
    vertical role anchor via _y_for_role).

    `scale` / `rotation` are the resolved STATIC transform.scale / rotation
    (QA-036: accepted by add_keyframe / set_property for years and never
    drawn): the whole block scales about, and turns clockwise about, its
    anchor. `surface` is the image size when it is not the canvas (the
    animated-transform path renders onto a larger working surface).

    `opacity` is the resolved scalar transform.opacity (see
    resolve_opacity_override); it multiplies the finished image's alpha
    channel so fill, stroke and shadow all dim uniformly. None means fully
    opaque.

    Complex scripts (Devanagari, Arabic, … — `shaping.needs_shaping`) are
    SHAPED with HarfBuzz and laid out bidi-correctly (QA-003); everything else
    keeps Pillow's basic layout.
    """
    img_w, img_h = surface if surface is not None else (canvas_w, canvas_h)
    # Emoji are kept and composited as images below (see _emoji_image). Only a
    # string with NOTHING drawable left is an empty render.
    if not text.strip():
        return Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
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
    if shadow is not None:
        style = {**style, "shadow": bool(shadow)}
    k = max(0.01, float(scale or 1.0))
    size_px = max(1, int(round(style["size"] * k)))
    stroke_px = max(0, int(round(style["stroke_w"] * k)))
    # Fall back to a Noto script font when the caption isn't Latin. Judged on
    # the TEXT only: emoji live in pictograph blocks that no script font
    # covers, and letting them vote pushed a plain-Latin caption with one 🔥
    # onto a Devanagari font.
    script_font = _pick_script_font(_strip_emoji(text) or text)
    chosen_font = script_font if script_font is not None else (font_file or _font_path(style["font"]))
    weight = (SCRIPT_FONT_WEIGHT.get(role, SCRIPT_FONT_WEIGHT["default"])
              if script_font is not None and script_font.name.endswith("-VF.ttf")
              and "SC" not in script_font.name else None)
    font = ImageFont.truetype(str(chosen_font), size_px)
    _set_weight(font, weight)
    img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    max_w = int(canvas_w * WRAP_WIDTH_RATIO * k)
    box = int(round(font.size * EMOJI_BOX_RATIO))
    # The caps decision comes from ROLE_STYLES (or the caller's explicit
    # override), not a hardcoded role list — see resolve_upper_override.
    caps = bool(ROLE_STYLES.get(role, ROLE_STYLES["default"]).get("upper", False)) \
        if upper is None else upper
    body = text.upper() if caps else text

    shaped = None
    # EVERY string goes through HarfBuzz when it is available — not only the
    # scripts that cannot render without it. The browser preview shapes all
    # text (GPOS kerning, ligatures); Pillow's basic layout applies only the
    # legacy `kern` table, which Inter/Montserrat do not use, so "TILTED"
    # exported ~2 % wider with a visible L-T gap. Without the engine, Latin
    # falls back to Pillow; text that NEEDS shaping raises ShapingUnavailable
    # (a RuntimeError) — misspelled Hindi must never be delivered silently.
    # Rule 8: letter spacing, every length × the transform scale.
    tracking = min(LETTER_SPACING_RANGE[1], max(LETTER_SPACING_RANGE[0], float(letter_spacing or 0.0))) * k
    if _shaping.available() or _shaping.needs_shaping(body):
        shaped = _shaping.ShapedFont(chosen_font, size_px, weight,
                                     fallback=_script_fallback(role, size_px))
        measure = (lambda s: shaped.width(s, _tokenize_emoji, box, tracking))
        lines = _wrap(draw, body, font, max_w, box, measure=measure)
    elif tracking:
        lines = _wrap(draw, body, font, max_w, box,
                      measure=lambda s: _tracked_line_w(s, draw, font, box, tracking))
    else:
        lines = _wrap(draw, body, font, max_w, box)

    cap_top, cap_bottom = _cap_band(font)
    cap_mid = (cap_top + cap_bottom) / 2
    y_anchor = block_anchor_y(role, anchor_y, canvas_h, canvas_w)
    x_anchor = float(anchor_x) if anchor_x is not None else canvas_w / 2
    sdx, sdy = SHADOW_OFFSET[0] * k, SHADOW_OFFSET[1] * k
    shadow_rgba = (0, 0, 0, SHADOW_ALPHA)
    spacing = min(3.0, max(0.5, float(line_spacing or 1.0)))
    centers = line_centers(y_anchor, len(lines), size_px, spacing)
    # Rule 7: every line's width first — alignment and the box need the block's.
    run_cache = ([shaped.runs(ln, _tokenize_emoji, box, tracking) for ln in lines]
                 if shaped is not None else None)
    widths = ([_shaping.tracked_line_width(runs, tracking) for runs in run_cache] if run_cache is not None
              else [_tracked_line_w(ln, draw, font, box, tracking) if tracking
                    else _line_w(ln, draw, font, box) for ln in lines])
    block_w = max(widths) if widths else 0.0
    # The box is its own layer, composited UNDER the finished text below:
    # Pillow's draw REPLACES pixels, so a shadow drawn straight onto the box
    # would punch translucent holes in it (the preview draws it behind with
    # `destination-over` — the same result).
    bg_layer = None
    if background is not None and lines:
        l, t, r, b, rad = background_rect(x_anchor, centers, block_w, size_px, spacing)
        bg_layer = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
        ImageDraw.Draw(bg_layer).rounded_rectangle((l, t, r, b), radius=rad, fill=background)
    for li, (line, cy) in enumerate(zip(lines, centers)):
        baseline = cy - cap_mid
        if shaped is not None:
            runs = run_cache[li]
            w = widths[li]
            x = float(line_x(align, x_anchor, block_w, w))
            if style.get("shadow"):
                _shaping.draw_runs(img, shaped, runs, x + sdx, baseline + sdy,
                                   fill=shadow_rgba, stroke_w=stroke_px,
                                   stroke_fill=shadow_rgba)
            _shaping.draw_runs(img, shaped, runs, x, baseline, fill=style["fill"],
                               stroke_w=stroke_px, stroke_fill=style["stroke"])
            pen = x
            for r in runs:
                if r.kind == "emoji":
                    _paste_emoji(img, r.text, pen, cy, box)
                pen += r.width
            continue
        w = widths[li]
        x = float(line_x(align, x_anchor, block_w, w))
        # Walk the line's segments left to right, drawing text runs with the
        # font and pasting emoji artwork. One pass per visual layer (shadow,
        # then fill) so an emoji can't land under the next run's shadow.
        segs = _tokenize_emoji(line)
        if tracking:
            # Rule 8 without the shaper (Latin only reaches here): one cluster
            # at a time, `tracking` after each.
            segs = [("emoji", ch) if kind == "emoji" else ("text", u)
                    for kind, ch in segs
                    for u in ([ch] if kind == "emoji" else [u for u, _t in _simple_clusters(ch)])]
            box_adv, gap = box + tracking, tracking
        else:
            box_adv, gap = box, 0.0
        if style.get("shadow"):
            cx = x
            for kind, chunk in segs:
                if kind == "emoji":
                    cx += box_adv
                    continue
                draw.text((cx + sdx, baseline + sdy), chunk, font=font, anchor="ls",
                          fill=shadow_rgba, stroke_width=stroke_px,
                          stroke_fill=shadow_rgba)
                cx += draw.textlength(chunk, font=font) + gap
        cx = x
        for kind, chunk in segs:
            if kind == "emoji":
                _paste_emoji(img, chunk, cx, cy, box)
                cx += box_adv
                continue
            draw.text((cx, baseline), chunk, font=font, anchor="ls", fill=style["fill"],
                      stroke_width=stroke_px, stroke_fill=style["stroke"])
            cx += draw.textlength(chunk, font=font) + gap
    if bg_layer is not None:
        img = Image.alpha_composite(bg_layer, img)
    if abs(float(rotation or 0.0)) > 0.01:
        # Clockwise about the anchor (PIL rotates counter-clockwise).
        img = img.rotate(-float(rotation), resample=Image.BICUBIC,
                         center=(x_anchor, y_anchor))
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
    cap_y = caption_position_y(edl)
    paired: list[tuple[TextClip, str, Path]] = []
    for c, role in collect_text_clips(edl):
        if is_xform_text(c, role):
            continue   # cache_xform_text_pngs renders these (QA-036)
        displayable = c.text.strip()
        fill, font_file = resolve_style_overrides(c, role)
        size = resolve_size_override(c)
        stroke, stroke_w = resolve_stroke_overrides(c)
        anchor_x, anchor_y = resolve_anchor_overrides(c, role, canvas.w, canvas.h)
        if role == "caption":
            anchor_y = cap_y        # the captions track's position (QA-075)
        blk = resolve_block_overrides(c)
        opacity = resolve_opacity_override(c)
        caps = resolve_upper_override(c, role)
        k_scale, rotation = resolve_scale_rotation(c, role)
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
                   f"{'' if opacity is None else f'{opacity:.3f}'}|"
                   # The RESOLVED caps flag, because the key hashes the RAW text:
                   # without it, toggling ALL CAPS off keeps serving the
                   # capitalised PNG forever — the same dead-control bug the
                   # size/opacity notes above describe. Keyed on the resolved
                   # value (not `style.upper`) so a clip that explicitly asks for
                   # its role's own default still shares the untouched clip's PNG.
                   f"{'U' if caps else 'l'}|k{k_scale:.4f}|r{rotation:.3f}|{block_key(blk)}")
        key = hashlib.sha256(
            # v6: emoji moved from a fixed em-box fraction to the measured cap
            # band, so every PNG holding an emoji changed pixels. The key has no
            # other input that tracks placement, and a stale hit is invisible —
            # the export would silently keep the old misalignment.
            # v9: the artwork source changed again (Noto 512 -> Apple 160) AND
            # EMOJI_INK_RATIO gave the emoji a side bearing of its own, so both
            # the pixels and the placement moved again. Same reasoning: nothing
            # else in this key tracks either, so without the bump a project that
            # already has text PNGs would export the OLD artwork indefinitely
            # while the preview drew the new — the precise mismatch this whole
            # subsystem exists to prevent.
            # v10: the caption role's anchor became portrait-aware
            # (caption_anchor_y) — a 9:16 caption PNG keyed under v9 holds the
            # old 0.84·h placement and nothing else in the key tracks it.
            # v11: the shared preview/export layout model (QA-015: cap-band
            # centred lines at 1.15 x size, shadow/outline semantics), shaped
            # complex scripts at the role weight (QA-003) and baked static
            # scale/rotation (QA-036) — every PNG's pixels moved.
            f"v12|{role}|{canvas.w}x{canvas.h}|{style_key}|{geo_key}|{displayable}".encode()
        ).hexdigest()[:16]
        png = cache_dir / f"text_{key}.png"
        if not _png_is_valid(png):
            img, degraded = _render_marking(
                render_text_png, c.text, role, canvas.w, canvas.h,
                fill=fill, font_file=font_file,
                size=size, anchor_x=anchor_x, anchor_y=anchor_y,
                stroke=stroke, stroke_w=stroke_w,
                opacity=opacity, upper=caps,
                scale=k_scale, rotation=rotation,
                background=blk["background"], align=blk["align"],
                line_spacing=blk["line_spacing"], shadow=blk["shadow"],
                letter_spacing=blk["letter_spacing"])
            if degraded:
                png = _degraded_path(png)
            _save_png_atomic(img, png)
        paired.append((c, role, png))
    return paired


def _max_key_value(v, default: float = 1.0) -> float:
    kfs = (v.get("keyframes") if isinstance(v, dict) else getattr(v, "keyframes", None)) or []
    vals = [float(p[1]) for p in kfs]
    return max(vals) if vals else default


def _even(n: float) -> int:
    v = max(2, int(round(n)))
    return v + (v % 2)


def cache_xform_text_pngs(edl: EDL, cache_dir: Path) -> list[dict]:
    """Text clips whose x / y / scale / rotation is ANIMATED (QA-036), each
    rendered as a TIGHT PNG with the block's anchor at its exact centre, so
    build_overlay_chain can move it (overlay x/y), turn it (rotate) and size it
    (scale, eval=frame) per frame — the same mechanism an animated sticker
    uses. Before this, a keyframed x resolved to "no override" and the text sat
    centred and static in both preview and export.

    Returns dicts: {clip, role, png, size:(w,h) canvas px, smax, rot_kf}.
    A keyframed scale is baked at its LARGEST key (`smax`) so the animation
    only ever down-scales pixels; a keyframed rotation is baked upright and
    turned by ffmpeg; static scale/rotation are baked in.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    canvas = edl.canvas
    out: list[dict] = []
    for c, role in collect_text_clips(edl):
        if not is_xform_text(c, role):
            continue
        tx = c.transform
        fill, font_file = resolve_style_overrides(c, role)
        size = resolve_size_override(c)
        stroke, stroke_w = resolve_stroke_overrides(c)
        opacity = resolve_opacity_override(c)
        caps = resolve_upper_override(c, role)
        scale_kf = is_keyframed(tx.scale)
        rot_kf = is_keyframed(tx.rotation)
        static_scale, static_rot = resolve_scale_rotation(c, role)
        k = max(0.01, _max_key_value(tx.scale, 1.0)) if scale_kf else static_scale
        rot = 0.0 if rot_kf else static_rot
        blk = resolve_block_overrides(c)
        style_kw = dict(fill=fill, font_file=font_file, size=size, stroke=stroke,
                        stroke_w=stroke_w, opacity=opacity, upper=caps,
                        background=blk["background"], align=blk["align"],
                        line_spacing=blk["line_spacing"], shadow=blk["shadow"],
                        letter_spacing=blk["letter_spacing"])
        key = hashlib.sha256(
            (f"xf2|{role}|{canvas.w}x{canvas.h}|{fill or ''}|"
             f"{font_file.name if font_file else ''}|{size}|{stroke or ''}|{stroke_w}|"
             f"{opacity}|{caps}|{k:.4f}|{rot:.3f}|{block_key(blk)}|{c.text.strip()}").encode()
        ).hexdigest()[:16]
        png = cache_dir / f"textxf_{key}.png"
        if not _png_is_valid(png):
            # Probe the unscaled, upright block on a 2x-canvas surface to size
            # the real one: its half-diagonal x k bounds the block at ANY
            # rotation, so nothing is clipped however it is turned.
            pw, ph = canvas.w * 2, canvas.h * 2
            probe, degraded = _render_marking(
                render_text_png, c.text, role, canvas.w, canvas.h,
                anchor_x=pw / 2, anchor_y=ph / 2, surface=(pw, ph), **style_kw)
            if degraded:
                png = _degraded_path(png)       # never reused (`_emoji_image`)
            bb = probe.getbbox()
            if bb is None:
                continue
            hw = max(pw / 2 - bb[0], bb[2] - pw / 2)
            hh = max(ph / 2 - bb[1], bb[3] - ph / 2)
            side = min(8192, _even(2 * (math.hypot(hw, hh) * k + 8)))
            img = render_text_png(c.text, role, canvas.w, canvas.h,
                                  anchor_x=side / 2, anchor_y=side / 2,
                                  surface=(side, side), scale=k, rotation=rot,
                                  **style_kw)
            ib = img.getbbox()
            if ib is None:
                continue
            ex = max(side / 2 - ib[0], ib[2] - side / 2)
            ey = max(side / 2 - ib[1], ib[3] - side / 2)
            cw, ch = _even(2 * ex + 2), _even(2 * ey + 2)
            left, top = int(side / 2 - cw / 2), int(side / 2 - ch / 2)
            _save_png_atomic(img.crop((left, top, left + cw, top + ch)), png)
        try:
            with Image.open(png) as im:
                wh = im.size
        except Exception:
            continue
        out.append({"clip": c, "role": role, "png": png, "size": wh,
                    "smax": k if scale_kf else None, "rot_kf": rot_kf})
    return out


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
    """A sticker animates server-side if any of x / y / opacity has keyframes
    or it carries a clip animation (wave E, F1). Keyed scale + rotation
    animate in the browser preview only — the render bakes them as their
    current (last) value."""
    tx = s.transform
    return (any(is_keyframed(getattr(tx, p)) for p in ("x", "y", "opacity"))
            or _clip_anim.has_animation(s))


def _sticker_anim_peak(s: Sticker) -> float:
    """How much larger than its pose a sticker's animation draws it (≥ 1):
    the small PNG is rendered that large so the zoom only down-scales."""
    pl = _clip_anim.plan_of(s, max(0.0, s.end - s.start))
    return max(1.0, pl.peak("scale")) if pl is not None and pl.animates("scale") else 1.0


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


def _mirrored(img: "Image.Image", tx) -> "Image.Image":
    """A sticker's image mirrored by its Transform.flip_h / flip_v (wave E):
    before its rotation, like every other layer (StickerLayer scales by −1
    inside its rotate)."""
    if getattr(tx, "flip_h", False):
        img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if getattr(tx, "flip_v", False):
        img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    return img


def _flip_key(tx) -> str:
    """The cache-key suffix of a flipped sticker ("" unflipped: every
    existing key is unchanged)."""
    return ("|fh" if getattr(tx, "flip_h", False) else "") + ("|fv" if getattr(tx, "flip_v", False) else "")


def _render_sticker_smallpng(sticker: Sticker, canvas_w: int, canvas_h: int,
                             dst: Path, mult: float = 1.0) -> tuple[int, int] | None:
    """Render the sticker PNG at its natural size (no canvas padding), `mult`
    times its pose's size (an animation's peak zoom).
    Returns (w, h) of the resulting PNG, or None on failure."""
    try:
        src = Image.open(sticker.src).convert("RGBA")
    except Exception:
        return None
    tx = sticker.transform
    scale = _scalar_or_last(tx.scale, 1.0) * mult
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
    img = _mirrored(src.resize((tw, th), Image.LANCZOS), tx)
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
        mult = _sticker_anim_peak(s)
        # the peak only enters the key when it is not 1: every existing
        # animated sticker keeps its cache file
        peak_key = "" if mult == 1.0 else f"|x{mult:.4f}"
        key = hashlib.sha256(
            f"sa|{s.id}|{s.src}|{canvas.w}x{canvas.h}|{scale:.3f}|{rot:.1f}|{Path(s.src).stat().st_mtime}{peak_key}"
            f"{_flip_key(s.transform)}".encode()
        ).hexdigest()[:16]
        dst = cache_dir / f"sa_{key}.png"
        if not _png_is_valid(dst):
            sz = _render_sticker_smallpng(s, canvas.w, canvas.h, dst, mult)
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
            f"st|{s.id}|{s.src}|{canvas.w}x{canvas.h}|{scalar:.3f}|{x:.1f},{y:.1f}|{opacity:.2f}|{rotation:.1f}|{Path(s.src).stat().st_mtime}"
            f"{_flip_key(tx)}".encode()
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
            sticker_img = _mirrored(src.resize((tw, th), Image.LANCZOS), tx)
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


def _layout_window(item: dict) -> tuple[float, float]:
    """The `[start, end)` an overlay item was AUTHORED at, in layout time."""
    kind = item["kind"]
    if kind in ("anim_text", "xform_text"):
        tc = item["text_clip"]
        return float(tc.start), float(tc.end)
    if kind == "anim":
        s = item["sticker"]
        return float(s.start), float(s.end)
    return float(item["start"]), float(item["end"])


def _item_clip_id(item: dict) -> str | None:
    tc = item.get("text_clip")
    return tc.id if tc is not None else item.get("clip_id")


def _on_render_clock(items: list[dict], seams: clock.SeamTable,
                     linked: dict[str, tuple[float, float] | None] | None = None) -> list[dict]:
    """New item dicts carrying `rs`/`re` — the item's window on the render
    clock — with the items the seams consumed entirely left out. Pure: the
    input list and its dicts are not touched (a caller may still hold them).

    `linked` (`clock.linked_text_windows`): a caption made from a voiceover
    or PIP plays in that clip's window, not at `render_time(start)` (final
    sweep 3 r2: a word early after a transition before it)."""
    placed: list[dict] = []
    for it in items:
        win = clock.text_window(seams, linked or {}, _item_clip_id(it), *_layout_window(it))
        if win is None:
            continue
        placed.append({**it, "rs": win[0], "re": win[1]})
    return placed


def enable_expr(rs: float, re: float, fps: float | int | None) -> str:
    """The ffmpeg `enable=` gate for an overlay shown over `[rs, re)`.

    HALF-OPEN on frame-exact bounds (`timebase.enable_window`), never the
    closed `between(t,rs,re)` this used to be: with `between`, two abutting
    overlays (an SRT cue change, a title handoff) were BOTH drawn on the
    boundary frame — QA-016. Commas are pre-escaped for a filtergraph value.
    Shared by pip.py so every overlay lane uses the one rule.
    """
    lo, hi = timebase.enable_window(rs, re, fps)
    return f"gte(t\\,{lo:.6f})*lt(t\\,{hi:.6f})"


def _looped_input_seconds(rs: float, re: float) -> float:
    """`-t` of a looped overlay PNG placed at `rs` (`-itsoffset`): its
    render window, never longer (wave E gate, X2). Past the v1 base's end a
    longer input drives `overlay` on by itself — the render grew by the
    slack (measured: 255 frames for a 240-frame plan with a keyed-opacity
    sticker ending at the end). Inside the window nothing changes: the
    `enable` gate closes at `re`, before the next frame the slack held. A
    microsecond floor keeps a zero-length window a valid input."""
    return max(1e-6, float(re) - float(rs))


def _picture_overlay(cur: str, elem: str, opts: str, nxt: str, i: int,
                     relabel: tuple[str, str]) -> list[str]:
    """`cur` + `elem` composited by `overlay={opts}` into `nxt`: with the
    base labelled BT.709 around it when the base is untagged (`relabel`,
    canvas_bg.picture_overlay_tags — review RE: a sticker / text PNG was
    converted with swscale's BT.601 default, 16 luma levels off the engine's
    and the blend path's BT.709)."""
    pre, post = relabel
    if not pre:
        return [f"{cur}{elem}overlay={opts}{nxt}"]
    return [f"{cur}{pre}[tcs{i}]", f"[tcs{i}]{elem}overlay={opts},{post}{nxt}"]


def _xform_text_parts(item: dict, idx: int, i: int, cur: str, next_label: str,
                      canvas, rate, out_w: int, out_h: int, rs: float, re: float,
                      relabel: tuple[str, str] = ("", "")) -> list[str]:
    """Filters for an animated-transform text clip (QA-036).

    The input is the tight PNG from cache_xform_text_pngs (anchor at its
    centre), looped and offset to `rs`, so every expression below runs on
    clip-local time `(t - rs)` / `(T - rs)` — the same clock the keyframes are
    authored in (clip-local, render clock). Order: opacity → fades → rotate
    (fixed-size input, square hypot output) → per-frame scale (keyed scale
    and/or `pop`, LAST so nothing after it sees a varying size) → overlay
    centred on the animated anchor. Mirrored by TextLayer.tsx (textLayout).
    """
    tc: TextClip = item["text_clip"]
    tx = tc.transform
    role = item["role"]
    a_in, a_out = item["anim_in"], item["anim_out"]
    d = anim_duration(tc, rs, re)
    sx = out_w / max(1, canvas.w)
    sy = out_h / max(1, canvas.h)
    w, h = item["size"]
    tvar = f"(t-{rs:.9f})"
    pre = f"[ov{i}]"
    chain = f"[{idx}:v]format=rgba,scale={_even(w * sx)}:{_even(h * sy)}"
    if is_keyframed(getattr(tx, "opacity", None)):
        aexpr = frame_exact_expr(tx.opacity, f"(T-{rs:.9f})")
        chain += f",geq=r='r(X\\,Y)':g='g(X\\,Y)':b='b(X\\,Y)':a='alpha(X\\,Y)*({aexpr})'"
    if a_in == "fade":
        chain += f",fade=t=in:st={rs:.3f}:d={d:.3f}:alpha=1"
    if a_out == "fade":
        chain += f",fade=t=out:st={re - d:.3f}:d={d:.3f}:alpha=1"
    if item.get("rot_kf"):
        rexpr = frame_exact_expr(tx.rotation, tvar)
        chain += (f",rotate=a='({rexpr})*PI/180':ow='hypot(iw\\,ih)'"
                  f":oh='hypot(iw\\,ih)':c=black@0")
    s_terms: list[str] = []
    if item.get("smax"):
        s_terms.append(f"(({frame_exact_expr(tx.scale, tvar)})/{item['smax']:.6f})")
    if a_in == "pop":
        q = f"clip((t-{rs:.4f})/{d:.4f}\\,0\\,1)"
        s_terms.append(f"if(lt({q}\\,0.7)\\,0.6+0.657*{q}\\,1.06-0.2*({q}-0.7))")
    if a_out == "pop":
        q = f"clip((t-{re - d:.4f})/{d:.4f}\\,0\\,1)"
        s_terms.append(f"(1-0.4*{q})")
    if s_terms:
        f = "*".join(s_terms)
        chain += (f",scale=w='max(2\\,trunc(iw*({f})/2)*2)'"
                  f":h='max(2\\,trunc(ih*({f})/2)*2)':eval=frame")
    parts = [chain + pre]

    ax, ay = resolve_anchor_overrides(tc, role, canvas.w, canvas.h)
    if is_keyframed(tx.x):
        x_c = f"({frame_exact_expr(tx.x, tvar)})*{sx:.6f}"
    else:
        x_c = f"{(ax if ax is not None else canvas.w / 2) * sx:.3f}"
    if is_keyframed(tx.y):
        y_c = f"({frame_exact_expr(tx.y, tvar)})*{sy:.6f}"
    else:
        y_c = f"{_y_for_role(role, ay, canvas.h, canvas.w) * sy:.3f}"
    off = out_h * 0.04
    y_terms = [f"{y_c}-overlay_h/2"]
    if a_in == "slide_up":
        y_terms.append(f"+{off:.1f}*(1-clip((t-{rs:.4f})/{d:.4f}\\,0\\,1))")
    elif a_in == "slide_down":
        y_terms.append(f"-{off:.1f}*(1-clip((t-{rs:.4f})/{d:.4f}\\,0\\,1))")
    if a_out == "slide_up":
        y_terms.append(f"-{off:.1f}*clip((t-{re - d:.4f})/{d:.4f}\\,0\\,1)")
    elif a_out == "slide_down":
        y_terms.append(f"+{off:.1f}*clip((t-{re - d:.4f})/{d:.4f}\\,0\\,1)")
    parts.extend(_picture_overlay(
        cur, pre, f"x='{x_c}-overlay_w/2':y='{''.join(y_terms)}':enable='{enable_expr(rs, re, rate)}'",
        next_label, i, relabel))
    return parts


def _sticker_anim_parts(parts: list[str], an, stream: str, i: int, *, tvar: str, rs: float,
                        out_w: int, out_h: int, peak: float) -> tuple[str, str, str]:
    """A sticker's clip-animation stages, in pip.py's order: blur mix →
    rotate (a hypot square, so the centre stays put) → alpha fades; the zoom
    (`_sticker_anim_zoom`) runs after the sticker's opacity. Returns the new
    stream label and the x / y travel terms for the overlay."""
    blur = _clip_anim.blur_mix_filters(
        an, src=f"[stbs{i}]", dst=f"[stbd{i}]", uid=f"s{i}", sigma=_clip_anim.blur_sigma(out_w, out_h),
        tvar=tvar, alpha_input=True, t0=rs)
    if blur:
        parts.append(f"{stream}format=yuva420p[stbs{i}]")
        parts.append(blur)
        stream = f"[stbd{i}]"
    chain: list[str] = []
    if an.animates("rotation"):
        chain.append(f"rotate=a='({an.expr('rotation', tvar)})*PI/180':c=black@0"
                     f":ow='hypot(iw\\,ih)':oh='hypot(iw\\,ih)'")
    ramps = [f"fade=t={kind}:st={rs + st:.3f}:d={d:.3f}:alpha=1"
             for kind, ramp in (("in", an.fade_in), ("out", an.fade_out)) if ramp is not None
             for st, d in (ramp,)]
    if ramps:
        chain.append("format=yuva420p," + ",".join(ramps))
    if chain:
        parts.append(f"{stream}format=yuva420p,{','.join(chain)}[sta{i}]")
        stream = f"[sta{i}]"
    ax = f"+({an.expr('x', tvar)})*{out_w}" if an.animates("x") else ""
    ay = f"+({an.expr('y', tvar)})*{out_h}" if an.animates("y") else ""
    return stream, ax, ay


def _sticker_anim_zoom(an, tvar: str, peak: float) -> str:
    """The animation's per-frame zoom, the LAST stage before the overlay (so
    nothing after it sees a varying size — pip.py's rule)."""
    ratio = f"(({an.expr('scale', tvar)})/{peak:.6f})"
    return (f"scale=w='max(2\\,trunc(iw*{ratio}/2)*2)'"
            f":h='max(2\\,trunc(ih*{ratio}/2)*2)':eval=frame")


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
    fps=None,
) -> tuple[str, list[str], str]:
    """Return (filter_str, extra_inputs, final_label).

    `fps` is the RENDER rate (default: the project's). Final QA (0.8.0): the
    enable gates and looped inputs used the project rate, so a 60 fps export
    of a 30 fps project opened every text and sticker half a PROJECT frame
    early — one export frame, over the last frame of the previous shot —
    and animated keyed ones at 30 fps. Placement (`rs`/`re`) stays on the
    project clock; only the frame grid it is gated on is the export's.

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
    from .canvas_bg import base_src_of, picture_overlay_tags
    relabel = picture_overlay_tags(base_src_of(edl))     # review RE: BT.709 on an untagged base
    text_paired = [] if preview else cache_text_pngs(edl, cache_dir)
    xform_texts = [] if preview else cache_xform_text_pngs(edl, cache_dir)
    static_stickers = [] if preview else cache_sticker_pngs(edl, cache_dir)
    animated_stickers = [] if preview else cache_animated_sticker_pngs(edl, cache_dir)

    # Unified item list. Static items get full canvas-sized PNGs that scale to
    # output then overlay at (0,0). Animated stickers get small PNGs overlaid
    # via x/y expressions. Text with keyframed opacity or anim_in/anim_out
    # presets uses an "anim_text" path (looped input + per-frame filters).
    canvas = edl.canvas
    rate = canvas.fps if fps is None else fps

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
                          "opacity": 1.0, "clip_id": c.id,
                          "z": zmap.get(c.id, 0),
                          "sort_start": c.start, "is_sticker": 0})
    for xt in xform_texts:
        c = xt["clip"]
        items.append({"kind": "xform_text", "text_clip": c, "png": xt["png"],
                      "role": xt["role"], "size": xt["size"], "smax": xt["smax"],
                      "rot_kf": xt["rot_kf"],
                      "anim_in": _anim_name(c, "anim_in"),
                      "anim_out": _anim_name(c, "anim_out"),
                      "z": zmap.get(c.id, 0), "sort_start": c.start, "is_sticker": 0})
    for s, png in static_stickers:
        items.append({"kind": "static", "start": s.start, "end": s.end, "png": png,
                      "opacity": 1.0, "z": zmap.get(s.id, 0),
                      "clip_z": getattr(s, "z", 0),
                      "sort_start": s.start, "is_sticker": 1})
    for s, png, (sw, sh) in animated_stickers:
        items.append({"kind": "anim", "sticker": s, "png": png, "size": (sw, sh),
                      "z": zmap.get(s.id, 0), "clip_z": getattr(s, "z", 0),
                      "sort_start": s.start, "is_sticker": 1})

    # Place every item on the RENDER clock (render/clock.py). The v1 lane these
    # composite onto is pulled left by each cross-fade at or before the item,
    # so `enable=between(t,start,end)` in raw LAYOUT time drew captions, hooks
    # and stickers late by the accumulated overlap (2.4 s after twelve
    # Zoom-Ins, measured). Done BEFORE indices are assigned: an item whose
    # window the seams consumed entirely is dropped here, so the `-i` list
    # and the filter labels can never disagree about how many items exist.
    items = _on_render_clock(items, clock.seam_table(edl), clock.linked_text_windows(edl))
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
        # `rs`/`re` are the item's RENDER window (set by _on_render_clock); the
        # offset, the enable gate and every clip-local `(t - start)` below use
        # them, never the layout `start`, or the pts and the gate would sit
        # `overlap` seconds apart and the item would open on its final frame.
        rs, re = float(item["rs"]), float(item["re"])
        if item["kind"] == "anim" and _clip_anim.has_animation(item["sticker"]):
            # A clip animation moves every frame: a frame per OUTPUT frame
            # (the render rate), placed at `rs` like an animated text — and
            # no longer than its window (+ one frame): the half-second of
            # slack the keyed paths add outlives v1 at the timeline's end and
            # the render grows by it (measured: 255 frames for a 240-frame
            # plan with a keyed-opacity sticker ending at the end; one frame
            # of slack still made it 961 of 960).
            dur = max(0.0, re - rs)
            extra_inputs += ["-itsoffset", f"{rs:.6f}",
                             "-loop", "1", "-framerate", timebase.ffmpeg_rate(rate),
                             "-t", f"{dur:.6f}", "-i", str(item["png"])]
        elif item["kind"] == "anim" and is_keyframed(item["sticker"].transform.opacity):
            # Exactly the window (wave E gate, X2), like the animated sticker
            # above: the old window + 0.5 s outlived v1 at the timeline's end
            # and drove `overlay` 0.5 s past the plan (255 of 240 frames) —
            # and at the RENDER rate: a 30 fps input's last frame sits past
            # a 24 fps timeline's last frame (193 of 192), and a keyed value
            # is sampled on the output grid, where the editor samples it.
            dur = _looped_input_seconds(rs, re)
            extra_inputs += ["-itsoffset", f"{rs:.3f}",
                             "-loop", "1", "-framerate", timebase.ffmpeg_rate(rate),
                             "-t", f"{dur:.6f}", "-i", str(item["png"])]
        elif item["kind"] == "anim_text":
            dur = _looped_input_seconds(rs, re)
            extra_inputs += ["-itsoffset", f"{rs:.3f}",
                             "-loop", "1", "-framerate", timebase.ffmpeg_rate(rate),
                             "-t", f"{dur:.6f}", "-i", str(item["png"])]
        elif item["kind"] == "xform_text":
            # At the RENDER rate: a transform that moves every frame must
            # have a frame for every output frame, or a 60 fps project steps.
            dur = _looped_input_seconds(rs, re)
            extra_inputs += ["-itsoffset", f"{rs:.3f}",
                             "-loop", "1", "-framerate", timebase.ffmpeg_rate(rate),
                             "-t", f"{dur:.6f}", "-i", str(item["png"])]
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
            parts.extend(_picture_overlay(cur, scaled, f"enable='{enable_expr(rs, re, rate)}'",
                                          next_label, i, relabel))
        elif item["kind"] == "xform_text":
            parts.extend(_xform_text_parts(item, idx, i, cur, next_label, canvas, rate,
                                           out_w, out_h, rs, re, relabel))
        elif item["kind"] == "anim_text":
            tc = item["text_clip"]
            role = item["role"]
            a_in, a_out = item["anim_in"], item["anim_out"]
            # Anim duration, clamped so in+out never overlap on short clips.
            # Clamped against the RENDER length: that is how long the clip is
            # on screen, and in+out must not overlap inside it.
            d = anim_duration(tc, rs, re)
            preprocessed = f"[ov{i}]"

            chain = f"[{idx}:v]scale={out_w}:{out_h},format=rgba"

            # Pop: per-frame scale (verified: scale exposes `t` under
            # eval=frame). In: overshoot 0.6→1.06→1.0; out: shrink to 0.6.
            if a_in == "pop" or a_out == "pop":
                s_terms = []
                if a_in == "pop":
                    q = f"clip((t-{rs:.4f})/{d:.4f}\\,0\\,1)"
                    s_terms.append(f"if(lt({q}\\,0.7)\\,0.6+0.657*{q}\\,1.06-0.2*({q}-0.7))")
                if a_out == "pop":
                    q = f"clip((t-{re - d:.4f})/{d:.4f}\\,0\\,1)"
                    s_terms.append(f"(1-0.4*{q})")
                s_expr = "*".join(s_terms)
                chain += (f",scale=w='ceil(iw*({s_expr})/2)*2'"
                          f":h='ceil(ih*({s_expr})/2)*2':eval=frame")

            # Keyframed opacity (the pre-existing path, time-shifted to
            # clip-local now that the input pts sit at absolute time).
            if is_keyframed(getattr(tc.transform, "opacity", None)):
                aexpr = frame_exact_expr(tc.transform.opacity, f"(T-{rs:.9f})")
                chain += f",geq=r='r(X\\,Y)':g='g(X\\,Y)':b='b(X\\,Y)':a='alpha(X\\,Y)*({aexpr})'"

            # Fades ride the fade filter's alpha mode (cheap, no geq).
            if a_in == "fade":
                chain += f",fade=t=in:st={rs:.3f}:d={d:.3f}:alpha=1"
            if a_out == "fade":
                chain += f",fade=t=out:st={re - d:.3f}:d={d:.3f}:alpha=1"
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
            if role == "caption":
                anchor_y = caption_position_y(edl)   # QA-075, as cache_text_pngs baked it
            cy = block_anchor_y(role, anchor_y, canvas.h, canvas.w) * (out_h / max(1, canvas.h))
            cx = ((float(anchor_x) if anchor_x is not None else canvas.w / 2)
                  * (out_w / max(1, canvas.w)))
            off = out_h * 0.04
            y_terms = [f"{cy:.2f}*(1-overlay_h/main_h)"]
            if a_in == "slide_up":
                y_terms.append(f"+{off:.1f}*(1-clip((t-{rs:.4f})/{d:.4f}\\,0\\,1))")
            elif a_in == "slide_down":
                y_terms.append(f"-{off:.1f}*(1-clip((t-{rs:.4f})/{d:.4f}\\,0\\,1))")
            if a_out == "slide_up":
                y_terms.append(f"-{off:.1f}*clip((t-{re - d:.4f})/{d:.4f}\\,0\\,1)")
            elif a_out == "slide_down":
                y_terms.append(f"+{off:.1f}*clip((t-{re - d:.4f})/{d:.4f}\\,0\\,1)")
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
            parts.extend(_picture_overlay(
                cur, preprocessed, f"x='{x_expr}':y='{''.join(y_terms)}':enable='{enable_expr(rs, re, rate)}'",
                next_label, i, relabel))
        else:
            s: Sticker = item["sticker"]
            sw, sh = item["size"]  # PNG natural pixel size (canvas-aligned)
            tx = s.transform
            tvar = f"(t-{rs:.9f})"  # clip-local time, on the render clock
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
            # CLIP ANIMATION (wave E, F1): over the render window, on the
            # clock `(t - rs)`; StickerLayer mirrors every stage.
            an = _clip_anim.plan_of(s, re - rs)
            ax = ay = ""
            if an is not None:
                sticker_stream, ax, ay = _sticker_anim_parts(
                    parts, an, sticker_stream, i, tvar=tvar, rs=rs, out_w=out_w, out_h=out_h,
                    peak=_sticker_anim_peak(s))

            # Position. Center on (x, y): subtract overlay_w/_h via ffmpeg vars.
            if is_keyframed(tx.x):
                xe = frame_exact_expr(tx.x, tvar)
                xexpr = f"({xe})*{sx:.6f}{ax}-overlay_w/2"
            elif an is not None:
                # centred on overlay_w: the animation rotates/zooms the frame
                xc = _scalar_or_last(tx.x, canvas.w / 2)
                xexpr = f"{xc * sx:.2f}{ax}-overlay_w/2"
            else:
                xc = _scalar_or_last(tx.x, canvas.w / 2)
                xexpr = f"{xc * sx - sticker_out_w / 2:.2f}"
            if is_keyframed(tx.y):
                ye = frame_exact_expr(tx.y, tvar)
                yexpr = f"({ye})*{sy:.6f}{ay}-overlay_h/2"
            elif an is not None:
                yc = _scalar_or_last(tx.y, canvas.h / 2)
                yexpr = f"{yc * sy:.2f}{ay}-overlay_h/2"
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
                aexpr = frame_exact_expr(tx.opacity, f"(T-{rs:.9f})")
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

            if an is not None and an.animates("scale"):
                parts.append(f"{preprocessed}{_sticker_anim_zoom(an, tvar, _sticker_anim_peak(s))}[ovz{i}]")
                preprocessed = f"[ovz{i}]"
            parts.extend(_picture_overlay(
                cur, preprocessed, f"x='{xexpr}':y='{yexpr}':enable='{enable_expr(rs, re, rate)}'",
                next_label, i, relabel))
        cur = next_label
    return ";".join(parts), extra_inputs, cur
