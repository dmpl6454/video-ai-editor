"""Fetch an emoji PNG for an emoji string and cache it locally.

ARTWORK CHOICE — why not the OS emoji font, and why not Twemoji.

The renderer bakes a PNG; the browser's StickerLayer draws that same PNG. Both
must show the same thing, on every machine, or a sticker changes appearance
between preview and export (and between a Mac and a Windows user opening the
same project). That rules out drawing the emoji with a system font: macOS
would paint Apple Color Emoji, Windows would paint Segoe UI Emoji, and the
exported video would match neither.

So the artwork is fetched, not fonted. It used to be Twemoji at 72x72 —
deliberately FLAT 2D line-art, and so small that a 1080x1920 canvas upscaled
it ~6x (a sticker is 22% of the longer edge = 422px from a 72px source). It
read as blurry AND, next to the OS emoji the picker shows, visibly "2D".

Now: Microsoft Fluent Emoji, **3D style** (MIT licensed) at 256x256 — shaded,
highlighted, genuinely dimensional, and 3.5x the linear resolution. Served
codepoint-addressably from the `@lobehub/fluent-emoji-3d` npm package via
jsDelivr, because Microsoft's own repo addresses assets by human-readable
CLDR name ("Smiling face with heart-eyes"), which is NOT derivable from
`unicodedata.name()` ("SMILING FACE WITH HEART-SHAPED EYES") — the two
diverge often enough that name-guessing is not a strategy.

Twemoji stays as a fallback: Fluent's coverage is broad (flags and ZWJ
sequences included — verified) but not total, and a missing sticker is worse
than a flat one.
"""
from __future__ import annotations
import io
import os
import threading
import urllib.request
import urllib.error
from pathlib import Path

from .. import platformutil as _pu

_LEGACY_EMOJI_CACHE = Path.home() / ".cache" / "video-ai-editor" / "emoji"
_EMOJI_CACHE_ROOT = _LEGACY_EMOJI_CACHE if _LEGACY_EMOJI_CACHE.exists() else \
    _pu.user_cache_dir("Video AI Editor") / "emoji"
# Style-namespaced: the flat Twemoji files already cached under the root would
# otherwise shadow the new 3D artwork forever (same `<codepoint>.png` name),
# so an existing install would silently keep rendering the old look.
EMOJI_CACHE = _EMOJI_CACHE_ROOT / "fluent3d"

FLUENT3D_BASE = "https://cdn.jsdelivr.net/npm/@lobehub/fluent-emoji-3d/assets"
TWEMOJI_BASE = "https://raw.githubusercontent.com/jdecked/twemoji/main/assets/72x72"


def _codepoints(emoji: str, *, keep_vs16: bool) -> str:
    """Dash-joined hex codepoints.

    VS16 (FE0F) handling differs by source and by emoji: Twemoji strips it
    from every filename, Fluent keeps it for the ones whose base codepoint is
    a legacy dingbat (`2764-fe0f` for a heart — plain `2764` 404s there).
    Both spellings are tried against Fluent rather than guessing which.
    """
    cps = []
    for ch in emoji:
        cp = ord(ch)
        if cp == 0xFE0F and not keep_vs16:
            continue
        cps.append(f"{cp:x}")
    return "-".join(cps)


def _download(url: str) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "video-ai-editor/0.1"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
        return data or None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return None


def _write_png_atomic(img_bytes: bytes, dst: Path, *, convert: bool) -> Path | None:
    """Write `img_bytes` to `dst` as a PNG, atomically.

    `convert` re-encodes (Fluent ships WebP; everything downstream — the
    Pillow bake, `_png_is_valid`, the browser <img> — expects PNG, and keeping
    one format avoids a second "does this build have the WebP plugin" question
    in the frozen app). The temp name carries pid+thread so two concurrent
    adds of the same emoji can't tear each other's file — same pattern as the
    text/sticker PNG caches.
    """
    try:
        if convert:
            from PIL import Image
            with Image.open(io.BytesIO(img_bytes)) as im:
                buf = io.BytesIO()
                im.convert("RGBA").save(buf, format="PNG")
                img_bytes = buf.getvalue()
        tmp = dst.with_name(f".{dst.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_bytes(img_bytes)
        _pu.replace_with_retry(tmp, dst)
        return dst
    except Exception:
        return None


def fetch_emoji_png(emoji: str) -> Path | None:
    """Return a local PNG path for `emoji`, downloading it if not cached.

    Order: cached → Fluent 3D (both VS16 spellings) → Twemoji → any legacy
    flat cache entry. The last step matters offline: an install that already
    has Twemoji art for this emoji should keep showing SOMETHING rather than
    failing `add_sticker` outright just because the 3D upgrade can't reach
    the network.
    """
    EMOJI_CACHE.mkdir(parents=True, exist_ok=True)
    seq = _codepoints(emoji, keep_vs16=False)
    if not seq:
        return None
    dst = EMOJI_CACHE / f"{seq}.png"
    if dst.exists() and dst.stat().st_size > 100:
        return dst

    # Fluent 3D — the normal path.
    for cp in dict.fromkeys([_codepoints(emoji, keep_vs16=True), seq]):
        data = _download(f"{FLUENT3D_BASE}/{cp}.webp")
        if data and (out := _write_png_atomic(data, dst, convert=True)):
            return out

    # Fluent doesn't cover this one (or we're offline) — flat Twemoji beats
    # no sticker at all.
    data = _download(f"{TWEMOJI_BASE}/{seq}.png")
    if data and (out := _write_png_atomic(data, dst, convert=False)):
        return out

    legacy = _EMOJI_CACHE_ROOT / f"{seq}.png"
    if legacy.exists() and legacy.stat().st_size > 100:
        return legacy
    return None
