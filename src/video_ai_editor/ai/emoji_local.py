"""Emoji sticker artwork made ON THIS MACHINE, with no network — the offline floor.

WHY THIS EXISTS. `add_sticker` is an edit op. It used to call
`emoji.fetch_emoji_png`, i.e. open a connection to cdn.jsdelivr.net in the
middle of a dispatch: an unguarded network fetch inside the single mutation
path, and a sticker that simply could not be added offline (gate X3). An edit
must never wait on, or fail because of, a CDN. So dispatch resolves artwork
from local sources only (`emoji.local_emoji_png`), and this module is the last
of them: it draws the emoji tile itself.

WHAT IT DRAWS, AND WHY THAT DOES NOT BREAK THE ARTWORK INVARIANT. `ai/emoji.py`
holds that sticker artwork is a fixed PNG, never a live system-font glyph, so
the preview and the export show the same pixels and a project looks the same
on every machine. This module keeps that: it renders the tile ONCE, to a PNG,
and that PNG is what the session copies and what both the preview and the
exporter then draw — the font is never consulted again for that sticker, and a
collaborator opening the project gets the same bytes.

And the pixels ARE the pinned set's. The pinned artwork (`img-apple-160`) is
the Apple emoji font's own 160 px bitmap strike, extracted to PNG — the bitmap
the installed font stores for 😂 is byte-identical to the cached download.
Measured on the machine this was written on, rendering every one of
3433 cached primary tiles through this module: the alpha channel matched on
every tile, premultiplied colour within 1 level on 3415 and within 2 on 8
(unpremultiply rounding), and 10 differ outright because the installed font is
a newer revision than the pinned 16.0.0 release (🇮🇳 🇵🇷 🇸🇦 and the 👯 / 🤼
people-pair designs, 🧾). When the network is back, the serving-path restyle
(`emoji.refresh_session_sticker_art`) swaps such a copy for the pinned bytes,
exactly as it does after any style change.

LICENCE — why this is not a bundled asset set. Apple's artwork is Apple's
copyright: the app may draw it on the user's own Mac (the same position the
module docstring of `ai/emoji.py` takes for the CDN mirror), but it may not
ship it. An openly-licensed bundle (Noto Color Emoji, OFL; Twemoji, CC-BY 4.0)
would be a different house style from every other emoji in the app. So the
offline floor is the installed font, and it exists only where that font does
(macOS); elsewhere this returns None and `add_sticker` says why.

Never raises: anything unexpected is None, and the caller turns None into a
clean 400.
"""
from __future__ import annotations

import io
import sys
import threading

#: The installed font's PostScript name. Its largest bitmap strike is 160 px,
#: the pinned set's tile size, so a 160-point draw blits the bitmap 1:1.
_FONT_NAME = "AppleColorEmoji"
_PPEM = 160
#: A glyph whose box is the font's emoji tile (every emoji glyph shares it:
#: origin (0, -20), 160x160 at 160 ppem). Read from the font, not hard-coded,
#: so a font revision that moves the tile moves the crop with it.
_REFERENCE = "\U0001F602"
#: AppKit string drawing is serialised: it is documented thread-safe for a
#: private context, but one lock costs nothing at this rate (a draw is ~30 ms)
#: and keeps the thread-local "current context" dance impossible to interleave.
_LOCK = threading.Lock()


def _appkit():
    if sys.platform != "darwin":
        return None
    try:
        import AppKit  # noqa: PLC0415 — macOS only, and only when needed
        import Quartz  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — no pyobjc: no local artwork, never a crash
        return None
    return AppKit, Quartz


def _font(AppKit):
    return AppKit.NSFont.fontWithName_size_(_FONT_NAME, _PPEM)


def available() -> bool:
    """True when this machine can draw emoji artwork without the network."""
    mods = _appkit()
    return bool(mods and _font(mods[0]) is not None)


def _only_emoji_font(AppKit, font, emoji: str) -> bool:
    """Every character must be drawn by the emoji font itself. Font fixing
    substitutes another face for anything the emoji font lacks — plain text
    ("A"), an unassigned codepoint — and that glyph would be a system-font
    letter or a LastResort box, not emoji artwork."""
    s = AppKit.NSMutableAttributedString.alloc().initWithString_attributes_(
        emoji, {AppKit.NSFontAttributeName: font})
    s.fixAttributesInRange_((0, s.length()))
    i = 0
    while i < s.length():
        f, rng = s.attribute_atIndex_effectiveRange_(AppKit.NSFontAttributeName, i, None)
        if f is None or f.fontName() != font.fontName():
            return False
        i = rng.location + rng.length
    return True


def _tile_box(font) -> tuple[int, int, int, int] | None:
    """(x, y, w, h) of the emoji tile relative to the baseline origin, in
    pixels at `_PPEM` (y up)."""
    g = font.glyphWithName_("u%04X" % ord(_REFERENCE))
    if not g:
        return None
    r = font.boundingRectForCGGlyph_(g)
    w, h = round(r.size.width), round(r.size.height)
    if w <= 0 or h <= 0:
        return None
    return round(r.origin.x), round(r.origin.y), w, h


def _draw(AppKit, Quartz, font, emoji: str, box: tuple[int, int, int, int], ink):
    """Draw `emoji` in text colour `ink` on a canvas three tiles wide and tall,
    the baseline placed so the tile lands in the middle cell; return (pixels,
    tile slice)."""
    import numpy as np  # noqa: PLC0415

    x0, y0, w, h = box
    W, H = 3 * w, 3 * h
    bx, by = w - x0, h - y0                   # baseline origin (CG: y up)
    cs = Quartz.CGColorSpaceCreateWithName(Quartz.kCGColorSpaceSRGB)
    ctx = Quartz.CGBitmapContextCreate(None, W, H, 8, W * 4, cs,
                                       Quartz.kCGImageAlphaPremultipliedLast)
    if ctx is None:
        return None
    gctx = AppKit.NSGraphicsContext.graphicsContextWithCGContext_flipped_(ctx, False)
    s = AppKit.NSAttributedString.alloc().initWithString_attributes_(
        emoji, {AppKit.NSFontAttributeName: font, AppKit.NSForegroundColorAttributeName: ink})
    AppKit.NSGraphicsContext.saveGraphicsState()
    try:
        AppKit.NSGraphicsContext.setCurrentContext_(gctx)
        # Without NSStringDrawingUsesLineFragmentOrigin the rect's origin is
        # the BASELINE of the single line — the placement the tile box is in.
        s.drawWithRect_options_(((bx, by), (W, H)), 0)
    finally:
        AppKit.NSGraphicsContext.restoreGraphicsState()
    img = Quartz.CGBitmapContextCreateImage(ctx)
    raw = bytes(Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(img)))
    bpr = Quartz.CGImageGetBytesPerRow(img)
    px = np.frombuffer(raw, np.uint8).reshape(H, bpr // 4, 4)[:, :W]
    # Memory rows run top-down; the tile spans CG y [by+y0, by+y0+h).
    top = H - (by + y0 + h)
    return px, (slice(top, top + h), slice(bx + x0, bx + x0 + w))


def render_png(emoji: str) -> bytes | None:
    """The emoji's 160x160 RGBA tile as PNG bytes, drawn from the installed
    Apple emoji font, or None (not macOS, no pyobjc, not an emoji the font
    draws as ONE tile of bitmap artwork)."""
    if not emoji:
        return None
    mods = _appkit()
    if mods is None:
        return None
    AppKit, Quartz = mods
    try:
        import numpy as np  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415

        with _LOCK:
            font = _font(AppKit)
            if font is None or not _only_emoji_font(AppKit, font, emoji):
                return None
            box = _tile_box(font)
            if box is None:
                return None
            drawn = _draw(AppKit, Quartz, font, emoji, box, AppKit.NSColor.redColor())
            again = _draw(AppKit, Quartz, font, emoji, box, AppKit.NSColor.blueColor())
        if drawn is None or again is None:
            return None
        px, (rows, cols) = drawn
        # Emoji ARTWORK is a bitmap and ignores the text colour; an OUTLINE
        # glyph is painted in it. The font carries outlines for a few symbols
        # it has no artwork for (♂ draws as a black text-style sign) — that is
        # a font glyph, not a sticker, so anything the colour reaches is
        # refused rather than baked.
        if not np.array_equal(px, again[0]):
            return None
        tile = px[rows, cols]
        outside = int(px[..., 3].astype(np.int64).sum()) - int(tile[..., 3].astype(np.int64).sum())
        # Nothing drawn, or ink outside the tile: a sequence the font does not
        # join into one emoji comes out as two side by side, and cropping that
        # to one tile would be a WRONG sticker, not a missing one.
        if not tile[..., 3].any() or outside != 0:
            return None
        # CoreGraphics hands back PREMULTIPLIED colour; a PNG stores straight.
        t = tile.astype(np.float64)
        a = t[..., 3:4]
        rgb = np.where(a > 0, np.clip(np.round(t[..., :3] * 255.0 / np.maximum(a, 1.0)), 0, 255), 0)
        out = np.concatenate([rgb, a], axis=2).astype(np.uint8)
        buf = io.BytesIO()
        Image.fromarray(out, "RGBA").save(buf, format="PNG")
        return buf.getvalue()
    except Exception:  # noqa: BLE001 — a local-art failure is "no art", never a 500
        return None
