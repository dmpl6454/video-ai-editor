"""Fetch an emoji PNG for an emoji string and cache it locally.

ARTWORK CHOICE — why the artwork is FETCHED, and which set it comes from.

The renderer bakes a PNG; the browser's StickerLayer draws that same PNG. Both
must show the same thing, on every machine, or a sticker changes appearance
between preview and export (and between a Mac and a Windows user opening the
same project). That rules out drawing the emoji with a system font: macOS
would paint Apple Color Emoji, Windows would paint Segoe UI Emoji, and the
exported video would match neither. So the artwork is fetched, not fonted —
and *that* is the invariant this module exists to hold. Which set it fetches
is a look decision on top of it.

The set is **Apple / iOS artwork at 160x160**, mirrored codepoint-addressably by
`iamcal/emoji-data`'s `img-apple-160`. This is a deliberate, informed choice —
the app shipped Noto (Android) before this and the reasoning is recorded here so
nobody "corrects" it back by accident.

**160 is not a mirror limitation; it is Apple's ceiling.** Apple Color Emoji is
an `sbix` BITMAP font whose strikes are 20/32/40/48/64/96/160 (verified against
`tmm1/emoji-extractor`, which reads the system font directly). There is no
vector and no larger raster anywhere: `img-apple-320` and `-512` 404,
`emoji-datasource-apple` stops at 64, and Emojipedia serves 160 even from its
`/thumbs/320/` path. So "get a higher-res Apple set" is not a task anyone can
complete — don't go looking again.

What that costs, precisely, because the headline number is misleading:
  * **Emoji inside TEXT are unaffected.** They render at
    `size * EMOJI_BOX_RATIO * EMOJI_INK_RATIO`, and the largest role (`hook`,
    170pt) needs 156px — under 160, so every text role DOWNscales and stays
    sharp.
  * **Only standalone stickers upscale**, 422px from 160 = 2.64x. Apple's art
    is gradient-heavy and soft-edged, so it survives that better than a
    hard-edged vector set would, but it is genuinely softer than Noto was.

The trade Apple wins on is CONSISTENCY: it covers 3778 of the 3781 RGI emoji
(99.9%), flags included. Noto's PNG export ships no flags at all, so the
previous chain drew them from Fluent — a visibly different house style inside
one line of text. Under Apple exactly three emoji fall back (♀️ ♂️ ⚕️, to Noto).

**LICENCE — the real cost, accepted deliberately.** Apple's emoji are Apple's
copyright and this is a third-party mirror. That is fine for local and internal
work and is a genuine redistribution question if a sticker is baked into a video
that gets published commercially. The openly-licensed sets remain wired up below
(Noto Apache-2.0/OFL, Fluent MIT, Twemoji CC-BY 4.0), so reverting is a
one-constant change — see `_PRIOR_STYLE_CACHES` for the namespacing rule that
makes a switch actually visible.

Fallback order is Apple -> Noto -> Fluent 3D -> Twemoji.
"""
from __future__ import annotations
import http.client
import io
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .. import platformutil as _pu

_LEGACY_EMOJI_CACHE = Path.home() / ".cache" / "video-ai-editor" / "emoji"
_EMOJI_CACHE_ROOT = _LEGACY_EMOJI_CACHE if _LEGACY_EMOJI_CACHE.exists() else \
    _pu.user_cache_dir("Video AI Editor") / "emoji"
# Style-namespaced, and it must get a NEW name on every style change: every set
# names its cached file `<codepoint>.png`, so reusing a directory would let each
# existing install keep serving the art it already cached and the switch would
# look like it had silently done nothing. Earlier namespaces stay readable as
# OFFLINE fallbacks only, newest first.
#
# `apple2` rather than reusing `apple`: that directory holds art from an earlier
# round whose chain was Apple -> Fluent -> Twemoji with no Noto, so its entries
# for the emoji Apple lacks were filled from a DIFFERENT source than this chain
# would pick. The primary artwork is byte-identical, but a cache namespace has
# to identify the whole chain, not just its first link.
_PRIOR_STYLE_CACHES = ("noto", "apple", "fluent3d", ".")
EMOJI_CACHE = _EMOJI_CACHE_ROOT / "apple2"

# All pinned to release tags, never branches: `@main` moves under us, and the
# artwork every already-cached sticker was fetched with would change silently.
APPLE_BASE = "https://cdn.jsdelivr.net/gh/iamcal/emoji-data@16.0.0/img-apple-160"
NOTO_BASE = "https://cdn.jsdelivr.net/gh/googlefonts/noto-emoji@v2.047/png/512"
FLUENT3D_BASE = "https://cdn.jsdelivr.net/npm/@lobehub/fluent-emoji-3d/assets"
TWEMOJI_BASE = "https://raw.githubusercontent.com/jdecked/twemoji/main/assets/72x72"


def _codepoints(emoji: str, *, keep_vs16: bool, pad: bool = False, sep: str = "-") -> str:
    """Lowercase hex codepoints joined into a filename stem.

    THREE independent spelling axes, and each source sits at its own point on
    all three — so the stem is built per-source rather than assumed. Every one
    of these was verified against the live CDN, because getting one wrong does
    not error: the fetch just 404s, falls through to the next source, and that
    emoji quietly renders in the WRONG STYLE while everything else is right.

    * **VS16 (FE0F).** Noto and Twemoji strip it from every filename. Apple and
      Fluent keep it for the emoji whose base codepoint is a legacy dingbat
      (`2764-fe0f` for a heart — plain `2764` 404s in both). Which emoji those
      are is not worth encoding, so both spellings are tried against each.
    * **Zero-padding to 4 digits.** Apple and Noto pad (`0023-20e3`,
      `emoji_u0023_20e3`; bare `a9` for `©` 404s in both). Fluent and Twemoji do
      not (`23-20e3`). Invisible for the ~99% of emoji above U+1000 and decisive
      for the rest — keycaps (`#`, `*`, `0`-`9`) and `©`/`®`.
    * **Separator.** Noto joins with `_` and prefixes `emoji_u`; everything else
      joins with `-` and has no prefix.
    """
    cps = []
    for ch in emoji:
        cp = ord(ch)
        if cp == 0xFE0F and not keep_vs16:
            continue
        cps.append(f"{cp:04x}" if pad else f"{cp:x}")
    return sep.join(cps)


def _noto_stem(emoji: str) -> str:
    """Noto's filename stem: `emoji_u` + padded, `_`-joined, VS16-stripped.

    One spelling, not a candidate list — Noto is consistent about all three
    axes (checked against ⚠️ ▶️ ✏️ ☺️ ⌨️ © ® and a ZWJ sequence containing an
    interior VS16: every `*_fe0f` spelling 404s, every stripped one resolves).
    """
    return "emoji_u" + _codepoints(emoji, keep_vs16=False, pad=True, sep="_")


# Keep-alive connections, one per (thread, host).
#
# `urlopen` opens a fresh TCP+TLS connection per call, and at this size that
# handshake IS the cost: measured against the real CDN, 8 sequential fetches
# took 333 ms each with a new connection and 30 ms each over one reused
# connection — 11x. That is the difference between the picker warming in
# ~13 seconds and ~8 minutes, and it speeds up every on-demand fetch too.
#
# Thread-local rather than a shared pool because `http.client` connections are
# not thread-safe and the warm pool runs six at once; per-thread gives each
# worker its own without a lock on the hot path.
_CONNS = threading.local()
_UA = {"User-Agent": "video-ai-editor/0.1"}


def _conn(host: str) -> http.client.HTTPSConnection:
    m = getattr(_CONNS, "m", None)
    if m is None:
        m = _CONNS.m = {}
    c = m.get(host)
    if c is None:
        c = m[host] = http.client.HTTPSConnection(host, timeout=10)
    return c


def _drop(host: str) -> None:
    m = getattr(_CONNS, "m", None) or {}
    c = m.pop(host, None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass


def _get_keepalive(url: str, depth: int = 0) -> bytes | None:
    """GET over a reused connection. None = the server said no (404 etc).

    Raises on a TRANSPORT problem so the caller can distinguish "this emoji
    does not exist here" (don't retry, don't fall back — it would double the
    cost of every miss) from "the socket died" (retry on a fresh one).
    """
    u = urllib.parse.urlsplit(url)
    c = _conn(u.netloc)
    path = u.path + (f"?{u.query}" if u.query else "")
    c.request("GET", path, headers=_UA)
    r = c.getresponse()
    data = r.read()                      # must drain, or the connection desyncs
    if r.status in (301, 302, 303, 307, 308) and depth < 3:
        loc = r.getheader("Location")
        if loc:
            return _get_keepalive(urllib.parse.urljoin(url, loc), depth + 1)
    if r.status != 200:
        return None
    return data or None


def _download_ex(url: str) -> tuple[bytes | None, str]:
    """Fetch `url`, reporting WHY it failed as well as that it did.

    Two attempts over keep-alive (an idle connection the server has since
    closed fails exactly once), then a plain one-shot `urlopen` so a transport
    quirk that breaks the pooled path can never break fetching outright.

    The reason matters because this chain CACHES what it gets, so a failure of
    the primary source is written to disk and, without this, believed forever:

      * ``absent``      — the server answered and said no. This emoji really is
                          not in that set; falling back is correct and final.
      * ``unreachable`` — the transport failed. The file may exist perfectly
                          well, so art from a later source is a GUESS that must
                          be re-checked rather than trusted permanently.

    Both looked identical before (plain None), which is how two ordinary
    skin-tone emoji — 1f44b-1f3fe and 1f91a-1f3fe, both present in the Apple
    set, verified live — ended up cached as 512px Noto art on this machine and
    stayed that way: one hiccup during a warm run, then never re-tried.
    """
    if url.startswith("https://"):
        host = urllib.parse.urlsplit(url).netloc
        for _ in range(2):
            try:
                data = _get_keepalive(url)
                return (data, "ok" if data else "absent")
            except Exception:
                _drop(host)
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
        return (data, "ok" if data else "absent")
    except urllib.error.HTTPError:
        # Subclass of URLError, so it MUST be caught first: the server spoke.
        return (None, "absent")
    except (urllib.error.URLError, TimeoutError, OSError):
        return (None, "unreachable")


def _download(url: str) -> bytes | None:
    """Fetch `url`, or None if it isn't there (reason discarded)."""
    return _download_ex(url)[0]


def _write_png_atomic(img_bytes: bytes, dst: Path, *, convert: bool) -> Path | None:
    """Write `img_bytes` to `dst` as a PNG, atomically.

    `convert` re-encodes, and is needed only by the Fluent fallback, which
    ships WebP; Noto and Twemoji both ship PNG already. Everything downstream
    — the Pillow bake, `_png_is_valid`, the browser <img> — expects PNG, and
    keeping one format avoids a second "does this build have the WebP plugin"
    question in the frozen app. The temp name carries pid+thread so two concurrent
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


# --- fallback provenance ------------------------------------------------------
#
# A cache entry is just `<codepoints>.png`, so nothing in it records WHICH
# source in the chain produced it — and the whole point of the chain is that
# most entries come from the primary and a handful do not. Without provenance a
# fallback write is indistinguishable from primary art forever, which is how
# wrong-style artwork survives silently (the failure this module's namespacing
# already exists to prevent, arriving by a different route).
#
# Only NON-primary writes get a marker, so the ~3400 correct entries cost
# nothing and the marker count stays in single digits.
_FALLBACK_RETRY = "retry"   # primary was unreachable — re-check on next fetch
_FALLBACK_FINAL = "final"   # primary answered and does not have it — settled
_PRIMARY_SIZE = (160, 160)  # img-apple-160 is always exactly this


def _fallback_marker(dst: Path) -> Path:
    return dst.with_name(dst.name + ".alt")


def _fallback_state(dst: Path) -> str | None:
    """`None` if this entry came from the primary source."""
    try:
        return _fallback_marker(dst).read_text(encoding="utf-8").strip() or _FALLBACK_RETRY
    except OSError:
        return None


def _set_fallback_state(dst: Path, state: str | None) -> None:
    m = _fallback_marker(dst)
    try:
        if state is None:
            m.unlink(missing_ok=True)
        else:
            m.write_text(state, encoding="utf-8")
    except OSError:
        pass                                    # provenance is best-effort


def audit_style() -> list[tuple[str, tuple[int, int] | None, str]]:
    """Every cached entry that is NOT primary artwork: (seq, size, state).

    Answers "is every emoji really iOS art?" against the bytes on disk rather
    than against the intent of the code. `state` is the fallback marker, or
    `unmarked` for an entry written before markers existed — which is exactly
    the population that can be silently wrong.
    """
    out: list[tuple[str, tuple[int, int] | None, str]] = []
    if not EMOJI_CACHE.exists():
        return out
    from PIL import Image
    for p in sorted(EMOJI_CACHE.glob("*.png")):
        try:
            with Image.open(p) as im:
                size = im.size
        except Exception:
            size = None
        if size != _PRIMARY_SIZE:
            out.append((p.stem, size, _fallback_state(p) or "unmarked"))
    return out


def repair_style() -> dict:
    """Re-attempt the primary source for every non-primary cache entry.

    Idempotent, and safe to run offline: an entry whose primary is unreachable
    keeps the art it has. Entries the primary genuinely lacks (♀️ ♂️ ⚕️) are
    marked `final` so they stop being re-checked.
    """
    fixed, kept, failed = [], [], []
    for seq, _size, _state in audit_style():
        emoji = _emoji_from_seq(seq)
        if emoji is None:
            failed.append(seq)
            continue
        dst = EMOJI_CACHE / f"{seq}.png"
        _set_fallback_state(dst, _FALLBACK_RETRY)   # force the re-check
        before = dst.read_bytes() if dst.exists() else b""
        fetch_emoji_png(emoji)
        after = dst.read_bytes() if dst.exists() else b""
        (fixed if after != before else kept).append(seq)
    return {"fixed": fixed, "unchanged": kept, "unreadable": failed}


def refresh_session_sticker_art(stickers_dir: Path) -> list[str]:
    """Bring a session's copied emoji artwork back in line with the current set.

    `add_sticker` copies artwork INTO the session (for `.vae` portability, and
    because the shared cache lives outside the session), and that copy used to
    be written once and never revisited. So switching the emoji set updated the
    shared cache while every existing project kept serving whatever it had —
    the switch looked like it had partly worked, which is worse than not having
    happened, because one frame could show two houses' artwork side by side.

    Only files named as a CODEPOINT SEQUENCE are touched, so a user's own PNG
    sticker in the same directory is never rewritten. Returns the stems it
    refreshed. Never raises: failing to restyle a sticker must not stop it
    being served.
    """
    refreshed: list[str] = []
    try:
        entries = sorted(stickers_dir.glob("*.png"))
    except OSError:
        return refreshed
    for p in entries:
        emoji = _emoji_from_seq(p.stem)
        if emoji is None or not emoji:
            continue                            # a user PNG, not emoji artwork
        try:
            canon = fetch_emoji_png(emoji)
            if canon is None or canon.resolve() == p.resolve():
                continue
            new = canon.read_bytes()
            if new and new != p.read_bytes():
                tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                tmp.write_bytes(new)
                _pu.replace_with_retry(tmp, p)
                refreshed.append(p.stem)
        except Exception:
            continue
    return refreshed


def _emoji_from_seq(seq: str) -> str | None:
    """Inverse of `_codepoints(..., keep_vs16=False)`."""
    try:
        return "".join(chr(int(part, 16)) for part in seq.split("-") if part)
    except ValueError:
        return None


def fetch_emoji_png(emoji: str) -> Path | None:
    """Return a local PNG path for `emoji`, downloading it if not cached.

    Order: cached → Apple/iOS → Noto → Fluent 3D → Twemoji → any previous-style
    cache entry. That last step matters offline: an install that already has
    Noto, Fluent or Twemoji art for this emoji should keep showing SOMETHING
    rather than failing `add_sticker` outright just because the style switch
    can't reach the network.

    Apple answers 3778 of the 3781 RGI emoji, so in practice this is a single
    request and the fallbacks are genuinely exceptional — unlike the previous
    Noto-primary chain, where every flag fell through to Fluent.
    """
    EMOJI_CACHE.mkdir(parents=True, exist_ok=True)
    seq = _codepoints(emoji, keep_vs16=False)
    if not seq:
        return None
    dst = EMOJI_CACHE / f"{seq}.png"
    cached = dst.exists() and dst.stat().st_size > 100
    # Art from the PRIMARY source is final and answers immediately. Art from a
    # fallback carries a marker (see _fallback_state) and is re-checked against
    # the primary, unless the primary was already confirmed not to have it.
    if cached and _fallback_state(dst) in (None, _FALLBACK_FINAL):
        return dst

    unpadded = dict.fromkeys([_codepoints(emoji, keep_vs16=True), seq])
    padded = dict.fromkeys([_codepoints(emoji, keep_vs16=True, pad=True),
                            _codepoints(emoji, keep_vs16=False, pad=True)])

    # Apple/iOS — the normal path, and almost always the only request made.
    # Already PNG, so no re-encode.
    reached = True          # did the primary actually ANSWER (vs. time out)?
    for cp in padded:
        data, why = _download_ex(f"{APPLE_BASE}/{cp}.png")
        if data and (out := _write_png_atomic(data, dst, convert=False)):
            _set_fallback_state(dst, None)      # primary art needs no marker
            return out
        if why == "unreachable":
            reached = False

    # Holding fallback art already: keep it rather than re-downloading the same
    # fallback, and record whether the primary is genuinely missing it (stop
    # paying for the re-check) or merely unreachable (try again next time).
    if cached:
        _set_fallback_state(dst, _FALLBACK_FINAL if reached else _FALLBACK_RETRY)
        return dst

    state = _FALLBACK_FINAL if reached else _FALLBACK_RETRY

    # The three Apple lacks (♀️ ♂️ ⚕️), and anything newer than the pinned tag.
    data = _download(f"{NOTO_BASE}/{_noto_stem(emoji)}.png")
    if data and (out := _write_png_atomic(data, dst, convert=False)):
        _set_fallback_state(dst, state)
        return out

    # Fluent ships WebP, hence the re-encode.
    for cp in unpadded:
        data = _download(f"{FLUENT3D_BASE}/{cp}.webp")
        if data and (out := _write_png_atomic(data, dst, convert=True)):
            _set_fallback_state(dst, state)
            return out

    # Broadest coverage of anything here, so it anchors the chain.
    data = _download(f"{TWEMOJI_BASE}/{seq}.png")
    if data and (out := _write_png_atomic(data, dst, convert=False)):
        _set_fallback_state(dst, state)
        return out

    for style in _PRIOR_STYLE_CACHES:
        legacy = (_EMOJI_CACHE_ROOT / style / f"{seq}.png").resolve()
        if legacy.exists() and legacy.stat().st_size > 100:
            return legacy
    return None


# --- background cache warming -------------------------------------------------
#
# The picker offers ~1900 emoji and paints an <img> per swatch. On a cold cache
# every one of those is a CDN round trip, and a browser opens ~6 connections to
# an origin — so opening a large group trickled artwork in over many seconds
# ("some emojis render very late"). Nothing about a single fetch is slow; it is
# the serialisation.
#
# Warming fixes it once. It is deliberately NOT automatic on import or on app
# start: it is real network traffic and the user may never open the picker. The
# desktop app asks for it when the picker is first opened, with the exact list
# the picker will draw, so what gets warmed is what will be needed.
_WARM_LOCK = threading.Lock()
_WARM_STATE = {"running": False, "done": 0, "total": 0}
# Bounded low so warming stays a background courtesy — it shares the CDN, the
# disk and the request threadpool with whatever the user is actually doing.
_WARM_WORKERS = 6


def warm_state() -> dict:
    with _WARM_LOCK:
        return dict(_WARM_STATE)


def is_cached(emoji: str) -> bool:
    seq = _codepoints(emoji, keep_vs16=False)
    if not seq:
        return True                      # nothing to fetch; never "pending"
    p = EMOJI_CACHE / f"{seq}.png"
    return p.exists() and p.stat().st_size > 100


def prewarm(emojis: list[str]) -> dict:
    """Fetch any of `emojis` not already cached, on a background thread.

    Returns immediately. Idempotent-ish: a second call while one is running is
    ignored rather than queued, so a re-opened picker cannot stack warmers.
    Every failure is swallowed — this is an optimisation, and an emoji that
    cannot be fetched here will simply be fetched (and fail identically) when
    the picker asks for it directly.
    """
    with _WARM_LOCK:
        if _WARM_STATE["running"]:
            return dict(_WARM_STATE)
        pending = [e for e in dict.fromkeys(emojis) if e and not is_cached(e)]
        if not pending:
            return dict(_WARM_STATE, running=False, done=0, total=0)
        _WARM_STATE.update(running=True, done=0, total=len(pending))
        state = dict(_WARM_STATE)

    def _run() -> None:
        # Plain DAEMON threads over a shared cursor, deliberately not a
        # ThreadPoolExecutor. `concurrent.futures.thread` registers an atexit
        # hook that JOINS its workers, and its workers are not daemon — so
        # quitting the app mid-prewarm blocks the interpreter until every
        # in-flight request finishes or hits the 10s socket timeout. That is a
        # background cache optimisation holding up a user-initiated quit, and on
        # macOS a packaged .app that does not go away promptly gets the spinning
        # beachball and an "not responding" report. Daemon threads are simply
        # abandoned at exit, which is the correct fate for this work: anything
        # unfetched is fetched on demand later, exactly as if prewarm never ran.
        cursor = [0]
        cur_lock = threading.Lock()

        def _worker() -> None:
            while True:
                with cur_lock:
                    i = cursor[0]
                    if i >= len(pending):
                        return
                    cursor[0] = i + 1
                _warm_one(pending[i])
                with _WARM_LOCK:
                    _WARM_STATE["done"] += 1

        try:
            threads = [
                threading.Thread(target=_worker, name=f"emoji-warm-{n}", daemon=True)
                for n in range(min(_WARM_WORKERS, len(pending)))
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            with _WARM_LOCK:
                _WARM_STATE["running"] = False

    threading.Thread(target=_run, name="emoji-prewarm", daemon=True).start()
    return state


def _warm_one(emoji: str) -> None:
    try:
        fetch_emoji_png(emoji)
    except Exception:
        pass
