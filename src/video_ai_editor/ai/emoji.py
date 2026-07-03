"""Fetch a Twemoji PNG for an emoji string and cache it locally.

Twemoji asset URL pattern (jdecked fork — actively maintained):
  https://raw.githubusercontent.com/jdecked/twemoji/main/assets/72x72/<codepoint(s)>.png
where the filename is dash-joined hex codepoints with ZWJ/variation selectors
preserved according to Twemoji's own naming.
"""
from __future__ import annotations
import urllib.request
import urllib.error
from pathlib import Path

from .. import platformutil as _pu

_LEGACY_EMOJI_CACHE = Path.home() / ".cache" / "video-ai-editor" / "emoji"
EMOJI_CACHE = _LEGACY_EMOJI_CACHE if _LEGACY_EMOJI_CACHE.exists() else \
    _pu.user_cache_dir("Video AI Editor") / "emoji"
TWEMOJI_BASE = "https://raw.githubusercontent.com/jdecked/twemoji/main/assets/72x72"


def _codepoint_seq(emoji: str) -> str:
    """Twemoji-style codepoint filename. Strips VS16 (FE0F) but keeps ZWJ (200D)."""
    cps = []
    for ch in emoji:
        cp = ord(ch)
        if cp == 0xFE0F:  # variation selector, dropped from twemoji filenames
            continue
        cps.append(f"{cp:x}")
    return "-".join(cps)


def fetch_emoji_png(emoji: str) -> Path | None:
    """Return a local PNG path for `emoji`, downloading from Twemoji if needed."""
    EMOJI_CACHE.mkdir(parents=True, exist_ok=True)
    seq = _codepoint_seq(emoji)
    if not seq:
        return None
    dst = EMOJI_CACHE / f"{seq}.png"
    if dst.exists() and dst.stat().st_size > 100:
        return dst
    url = f"{TWEMOJI_BASE}/{seq}.png"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "video-ai-editor/0.1"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
        if not data:
            return None
        dst.write_bytes(data)
        return dst
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return None
