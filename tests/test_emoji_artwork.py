"""Emoji sticker artwork: source, resolution, filename spelling, cache
namespacing, fallback.

The artwork a sticker uses has to be IDENTICAL in the preview and the export,
and identical on macOS and Windows — a user reported the sticker changing
appearance the moment they let go of a drag, and the same project must not
look different for a Mac collaborator. That rules out the OS emoji font
(Apple Color Emoji vs Segoe UI Emoji) and makes this a fetched-artwork
problem, which is what these tests pin.

Network is stubbed throughout: these assert the SELECTION LOGIC (which source,
which spelling, what fallback, where it caches), never that GitHub/jsDelivr is
reachable — a CI box with no egress must still run them.
"""
from __future__ import annotations
import io
from pathlib import Path

import pytest
from PIL import Image

from video_ai_editor.ai import emoji as E

# Native sizes of the sets, so a test can tell which one answered from the
# bytes alone.
APPLE_PX, NOTO_PX, FLUENT_PX, TWEMOJI_PX = 160, 512, 256, 72


def _webp_bytes(size: int = FLUENT_PX) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (size, size), (255, 200, 0, 255)).save(buf, format="WEBP")
    return buf.getvalue()


def _png_bytes(size: int = TWEMOJI_PX) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (size, size), (255, 0, 0, 255)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """Point every cache tier at tmp_path so nothing touches the real cache.

    Also bridges the primary fetch onto `_download`, which is the patch point
    every test below already uses. The primary now goes through `_download_ex`
    (bytes PLUS why it failed — see its docstring), and without this bridge a
    test stubbing only `_download` would let the Apple request escape to the
    real network. `absent` is the right reason for a deliberate stub-returns-
    None: it means "the server answered and hasn't got it", which is what a
    test asserting a fallback is describing.
    """
    root = tmp_path / "emoji"
    monkeypatch.setattr(E, "_EMOJI_CACHE_ROOT", root)
    monkeypatch.setattr(E, "EMOJI_CACHE", root / "apple2")

    real_ex = E._download_ex

    def bridge(url: str):
        dl = E._download
        # `_download` delegates to `_download_ex`, so bridging unconditionally
        # would recurse for the tests that exercise the transport itself. Defer
        # to the real primitive unless a test has actually replaced `_download`
        # (a stub is defined in THIS module, the real one in emoji.py).
        if getattr(dl, "__module__", None) == E.__name__:
            return real_ex(url)
        return (dl(url), "absent")

    monkeypatch.setattr(E, "_download_ex", bridge)
    return root


def test_prefers_apple_ios_artwork(cache, monkeypatch):
    """Apple is the current style — the actual iOS artwork, chosen deliberately
    over the openly-licensed sets (see the module docstring). The others stay
    wired up as fallbacks, so a passing fetch must be observable as APPLE
    specifically; otherwise a silently degraded chain still looks green."""
    seen: list[str] = []

    def fake_dl(url: str):
        seen.append(url)
        return _png_bytes(APPLE_PX) if "img-apple-160" in url else None

    monkeypatch.setattr(E, "_download", fake_dl)
    out = E.fetch_emoji_png("\U0001F60E")
    assert out is not None and out.exists()
    assert any("img-apple-160" in u for u in seen)
    assert not any("noto" in u or "fluent" in u or "twemoji" in u for u in seen), \
        "the other sets are fallbacks, not the default"

    # Stored as PNG (one format downstream: the Pillow bake, _png_is_valid and
    # the browser <img> all expect it) at Apple's native resolution. 160 is
    # Apple's own ceiling — an sbix bitmap font topping out at a 160 strike —
    # not a limit of this mirror, so this number cannot be improved.
    with Image.open(out) as im:
        assert im.format == "PNG"
        assert im.size == (APPLE_PX, APPLE_PX)
        assert im.mode == "RGBA", "alpha must survive"


def test_apple_filename_spelling(cache, monkeypatch):
    """Apple pads to 4 hex digits and keeps VS16 for legacy dingbats. Getting
    either wrong does NOT error — the fetch 404s, falls through to Noto, and
    that one emoji silently renders in the previous style while everything
    around it is right."""
    tried: list[str] = []
    monkeypatch.setattr(E, "_download", lambda url: tried.append(url) or None)

    E.fetch_emoji_png("❤️")           # VS16 kept; already 4 digits
    assert tried[0].endswith("/2764-fe0f.png"), tried[0]

    tried.clear()
    E.fetch_emoji_png("#️⃣")           # keycap: padded, VS16 kept
    assert tried[0].endswith("/0023-fe0f-20e3.png"), tried[0]

    tried.clear()
    E.fetch_emoji_png("\U0001F469‍\U0001F4BB")   # ZWJ sequence
    assert tried[0].endswith("/1f469-200d-1f4bb.png"), tried[0]


def test_each_source_gets_its_own_spelling(cache, monkeypatch):
    """Four sources, three spelling axes, no two alike. Reusing one source's
    stem for another 404s that fallback — turning a safety net into a single
    point of failure, invisibly. Noto is the odd one out (`emoji_u` prefix and
    `_` separator), and Fluent/Twemoji are unpadded."""
    tried: list[str] = []
    monkeypatch.setattr(E, "_download",
                        lambda url: tried.append(url) or (
                            _png_bytes() if "twemoji" in url else None))

    assert E.fetch_emoji_png("#️⃣") is not None
    by = lambda frag: [u for u in tried if frag in u]  # noqa: E731
    assert by("img-apple-160") and by("noto-emoji") and by("fluent") and by("twemoji"), \
        f"not every source was tried: {tried}"
    for url in by("img-apple-160"):
        assert "emoji_u" not in url and "0023" in url
    for url in by("noto-emoji"):
        assert "emoji_u0023_20e3" in url
    for url in by("fluent") + by("twemoji"):
        assert "emoji_u" not in url, f"Noto prefix leaked into {url}"
        assert "0023" not in url, f"padding leaked into {url}"


def test_falls_back_to_noto_for_the_three_apple_lacks(cache, monkeypatch):
    """Apple answers 3778 of the 3781 RGI emoji. The three it misses — ♀️ ♂️ ⚕️
    — come from Noto. Tiny, but a missing sticker is a hard failure while an
    off-style one is barely noticeable, so the hop has to work."""
    monkeypatch.setattr(E, "_download",
                        lambda url: _png_bytes(NOTO_PX) if "noto-emoji" in url else None)
    out = E.fetch_emoji_png("♀️")
    assert out is not None
    with Image.open(out) as im:
        assert im.size == (NOTO_PX, NOTO_PX)


def test_falls_back_to_fluent_and_re_encodes_its_webp(cache, monkeypatch):
    """Fluent's role is now narrow — Apple covers the flags it used to supply —
    but it stays in the chain for anything newer than the pinned tags. The WebP
    it ships still has to reach disk as a PNG, since everything downstream
    assumes one format."""
    monkeypatch.setattr(E, "_download",
                        lambda url: _webp_bytes() if "fluent" in url else None)
    out = E.fetch_emoji_png("\U0001F1EE\U0001F1F3")
    assert out is not None
    with Image.open(out) as im:
        assert im.format == "PNG", "WebP must be re-encoded on the way in"
        assert im.size == (FLUENT_PX, FLUENT_PX)


def test_falls_back_to_twemoji_when_nothing_else_covers_the_emoji(cache, monkeypatch):
    """Broadest coverage of anything in the chain, so it anchors it. A flat
    sticker beats a failed add_sticker."""
    monkeypatch.setattr(E, "_download",
                        lambda url: _png_bytes() if "twemoji" in url else None)
    out = E.fetch_emoji_png("\U0001F60E")
    assert out is not None
    with Image.open(out) as im:
        assert im.size == (TWEMOJI_PX, TWEMOJI_PX)


@pytest.mark.parametrize("style_dir, px", [
    ("noto", NOTO_PX), ("apple", APPLE_PX), ("fluent3d", FLUENT_PX), (".", TWEMOJI_PX)])
def test_falls_back_to_a_previous_style_cache_when_offline(cache, monkeypatch, style_dir, px):
    """Offline, an install that already has art for this emoji in ANY earlier
    style must keep showing it rather than failing outright because the style
    switch can't reach the network. Every prior namespace counts — `noto/`,
    the first-round `apple/`, `fluent3d/`, and the bare root of the flat-Twemoji
    era."""
    d = (cache / style_dir).resolve()
    d.mkdir(parents=True, exist_ok=True)
    legacy = d / "1f60e.png"
    legacy.write_bytes(_png_bytes(px))

    monkeypatch.setattr(E, "_download", lambda url: None)
    assert E.fetch_emoji_png("\U0001F60E") == legacy


def test_prior_style_caches_are_searched_newest_first(cache, monkeypatch):
    """An install that has been through every style holds four copies. Serving
    the oldest would silently downgrade someone from 512px Noto to 72px flat
    Twemoji — a visible regression from a fallback meant to be a safety net."""
    for style, px in (("noto", NOTO_PX), ("apple", APPLE_PX),
                      ("fluent3d", FLUENT_PX), (".", TWEMOJI_PX)):
        d = (cache / style).resolve()
        d.mkdir(parents=True, exist_ok=True)
        (d / "1f60e.png").write_bytes(_png_bytes(px))

    monkeypatch.setattr(E, "_download", lambda url: None)
    with Image.open(E.fetch_emoji_png("\U0001F60E")) as im:
        assert im.size == (NOTO_PX, NOTO_PX), "expected the most recent prior style"


def test_cache_is_style_namespaced_so_older_art_cannot_shadow_the_new(cache, monkeypatch):
    """Every style names the file `<codepoint>.png`. Sharing one directory
    means every existing install keeps serving whatever it cached first and
    never sees the new artwork — the switch looks like it silently did nothing.

    This switch needed a namespace even though an earlier round already used
    `apple/`: that round's chain had no Noto, so its entries for the emoji Apple
    lacks came from a different source. A namespace identifies the whole chain,
    not just its first link."""
    assert E.EMOJI_CACHE != E._EMOJI_CACHE_ROOT
    assert E.EMOJI_CACHE.parent == E._EMOJI_CACHE_ROOT
    assert E.EMOJI_CACHE.name not in E._PRIOR_STYLE_CACHES, \
        "the live namespace must not also be listed as a prior style"

    for style, px in (("noto", NOTO_PX), ("apple", APPLE_PX),
                      ("fluent3d", FLUENT_PX), (".", TWEMOJI_PX)):
        d = (cache / style).resolve()
        d.mkdir(parents=True, exist_ok=True)
        (d / "1f60e.png").write_bytes(_png_bytes(px))

    monkeypatch.setattr(E, "_download",
                        lambda url: _png_bytes(APPLE_PX) if "img-apple-160" in url else None)
    out = E.fetch_emoji_png("\U0001F60E")
    with Image.open(out) as im:
        assert im.size == (APPLE_PX, APPLE_PX), "older cached art shadowed the Apple fetch"


def test_no_network_and_no_cache_returns_none_rather_than_raising(cache, monkeypatch):
    """add_sticker turns None into a clean ValueError -> 400. An exception
    escaping here would be a 500."""
    monkeypatch.setattr(E, "_download", lambda url: None)
    assert E.fetch_emoji_png("\U0001F60E") is None


def test_artwork_source_is_platform_independent():
    """The whole reason this is fetched rather than drawn with a system font:
    a Mac and a Windows user opening the same project must get the same
    pixels, and the export must match both. Nothing here may branch on OS.
    """
    import ast

    tree = ast.parse(Path(E.__file__).read_text(encoding="utf-8"))
    # AST, not raw text: the module's own docstring names the OS emoji fonts
    # precisely to explain why they are NOT used, and a substring scan would
    # flag that prose as a violation.
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and id(node) not in docstrings):
            for font in ("Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji"):
                assert font not in node.value, (
                    f"{font!r} used as a value — a system font makes the artwork "
                    "differ between macOS and Windows, and from the export")
    for banned in ("IS_WINDOWS", "IS_MAC"):
        assert banned not in names, (
            f"{banned} branch here would give Mac and Windows different pixels")


def test_the_proprietary_primary_is_a_documented_choice():
    """Apple's emoji are Apple's copyright and this app now ships them as the
    primary artwork. That was chosen deliberately, with the trade named — and
    the danger with a deliberate exception is that it stops looking like one.
    A later reader finds a proprietary CDN in a file whose other three sources
    are Apache/MIT/CC-BY, assumes it drifted in, and either rips it out or
    quietly copies the pattern somewhere it was never weighed.

    So the gate is inverted rather than deleted: using Apple is allowed, using
    it SILENTLY is not. The module must state the licence position in prose that
    travels with the code.
    """
    src = Path(E.__file__).read_text(encoding="utf-8")
    assert "img-apple-160" in E.APPLE_BASE, "Apple is expected to be the primary"

    doc = E.__doc__ or ""
    for phrase in ("LICENCE", "copyright", "redistribution"):
        assert phrase in doc, (
            f"the module docstring no longer explains the licence trade "
            f"(missing {phrase!r}) — an undocumented proprietary dependency is "
            f"the failure mode, not the dependency itself")
    # The openly-licensed sets stay wired up, so reverting stays a one-constant
    # change rather than a re-implementation.
    assert "googlefonts/noto-emoji" in E.NOTO_BASE      # Apache-2.0 / OFL
    assert "fluent-emoji-3d" in E.FLUENT3D_BASE          # MIT
    assert "twemoji" in E.TWEMOJI_BASE                   # CC-BY 4.0
    assert "_PRIOR_STYLE_CACHES" in src


def test_every_source_is_pinned_to_a_release_tag():
    """A jsDelivr `gh` URL needs a ref, and a BRANCH ref can move under us —
    the artwork every cached sticker was fetched with would change silently.
    Pin tags; the fallbacks cover anything newer than the snapshots."""
    for base in (E.APPLE_BASE, E.NOTO_BASE):
        assert "@main/" not in base and "@master/" not in base, base
    assert "@16.0.0/" in E.APPLE_BASE, E.APPLE_BASE
    assert "@v2.047/" in E.NOTO_BASE, E.NOTO_BASE


def test_webp_decode_failure_degrades_to_twemoji(cache, monkeypatch):
    """The Fluent fallback ships WebP, so that path needs Pillow's WebP plugin.
    A frozen build that shipped the module without its codec would otherwise
    raise where nothing catches it — the exact shape of the packaged-app
    caption bug (module present, its asset absent -> bare HTTP 500). Degrade
    to flat Twemoji instead: worse art beats a broken button.
    """
    def fake_dl(url: str):
        if "img-apple-160" in url or "noto-emoji" in url:
            return None            # force the chain down to the WebP source
        return _webp_bytes() if "fluent" in url else _png_bytes()

    monkeypatch.setattr(E, "_download", fake_dl)

    import PIL.Image as PILImage
    real_open = PILImage.open

    def boom(fp, *a, **kw):
        data = fp.getvalue() if hasattr(fp, "getvalue") else b""
        if data[:4] == b"RIFF":          # a WebP we're being asked to decode
            raise OSError("cannot identify image file (no WEBP plugin)")
        return real_open(fp, *a, **kw)

    monkeypatch.setattr(PILImage, "open", boom)

    out = E.fetch_emoji_png("\U0001F60E")
    assert out is not None, "a WebP decode failure must not kill the sticker"
    with Image.open(out) as im:
        assert im.size == (TWEMOJI_PX, TWEMOJI_PX), "expected the Twemoji fallback"


def test_prewarm_skips_what_is_already_cached(cache, monkeypatch):
    """Warming exists because a cold picker trickles artwork in over seconds.
    It must not re-fetch what is already on disk — a second picker open would
    otherwise repeat the entire download."""
    E.EMOJI_CACHE.mkdir(parents=True, exist_ok=True)
    (E.EMOJI_CACHE / "1f60e.png").write_bytes(_png_bytes(APPLE_PX))

    fetched: list[str] = []
    monkeypatch.setattr(E, "fetch_emoji_png", lambda e: fetched.append(e))

    st = E.prewarm(["\U0001F60E", "\U0001F525"])
    assert st["total"] == 1, "the cached emoji should not be queued"
    _wait_for_warm()
    assert fetched == ["\U0001F525"]


def test_prewarm_never_stacks_concurrent_runs(cache, monkeypatch):
    """A re-opened picker asks again. Queueing a second pool on top of a live
    one would multiply the network traffic this is supposed to reduce."""
    import threading as _t
    gate = _t.Event()
    monkeypatch.setattr(E, "fetch_emoji_png", lambda e: gate.wait(5))

    first = E.prewarm(["\U0001F525", "\U0001F600"])
    assert first["running"] is True
    second = E.prewarm(["\U0001F389"])
    assert second["total"] == first["total"], "a second run was started"
    gate.set()
    _wait_for_warm()


def test_prewarm_survives_a_source_that_raises(cache, monkeypatch):
    """It is an optimisation. An exception escaping the pool would kill warming
    for every later emoji and surface as a dead background thread."""
    def boom(e):
        raise RuntimeError("network gone")
    monkeypatch.setattr(E, "fetch_emoji_png", boom)

    E.prewarm(["\U0001F525", "\U0001F600"])
    _wait_for_warm()
    assert E.warm_state()["running"] is False


def test_prewarm_workers_are_all_daemon_threads(cache, monkeypatch):
    """Quitting the app must not wait on a background cache fill.

    `concurrent.futures.ThreadPoolExecutor` — the obvious way to write this —
    registers an atexit hook that JOINS its non-daemon workers, so a quit landing
    mid-prewarm blocks the interpreter until every in-flight request finishes or
    hits its 10s socket timeout. On a packaged macOS .app that is a beachball and
    an "application not responding" report, caused by work the user never asked
    for and which costs nothing to abandon (anything unfetched is simply fetched
    on demand later).

    Asserted on the live threads rather than by reading the source, so switching
    back to an executor fails here instead of passing quietly.
    """
    import threading as _th
    seen: list[bool] = []
    started = _th.Event()

    def slow(e):
        seen.append(_th.current_thread().daemon)
        started.set()

    monkeypatch.setattr(E, "fetch_emoji_png", slow)
    E.prewarm(["\U0001F525", "\U0001F600", "\U0001F389"])
    assert started.wait(5.0), "prewarm never ran a worker"
    _wait_for_warm()
    assert seen and all(seen), f"non-daemon prewarm worker(s): {seen}"


def _wait_for_warm(timeout: float = 5.0) -> None:
    import time as _time
    end = _time.time() + timeout
    while _time.time() < end:
        if not E.warm_state()["running"]:
            return
        _time.sleep(0.02)
    raise AssertionError("prewarm never finished")


def test_download_recovers_when_a_kept_alive_connection_has_died(cache, monkeypatch):
    """Connections are pooled per thread and held for the life of the process,
    so the server WILL close an idle one before we reuse it. That surfaces only
    after the app has sat open for a while — the hardest kind of bug to catch by
    hand, and a plain crash if the retry is missing.

    The first attempt must fail, the connection be dropped, and a fresh one
    succeed — without falling all the way through to the urllib path, which
    would silently give up the 11x that pooling exists for.
    """
    import http.client

    calls = {"n": 0}

    class DeadThenAlive:
        def __init__(self, *a, **kw):
            pass

        def request(self, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise http.client.RemoteDisconnected("closed by peer")

        def getresponse(self):
            class R:
                status = 200
                def read(self_inner):
                    return b"PNGDATA"
                def getheader(self_inner, _n):
                    return None
            return R()

        def close(self):
            pass

    monkeypatch.setattr(E, "_CONNS", __import__("threading").local())
    monkeypatch.setattr(http.client, "HTTPSConnection", DeadThenAlive)
    monkeypatch.setattr(E.urllib.request, "urlopen",
                        lambda *a, **kw: pytest.fail("fell back to urllib instead of retrying"))

    assert E._download("https://example.test/x.png") == b"PNGDATA"
    assert calls["n"] == 2, "expected exactly one retry on a fresh connection"


def test_download_returns_none_on_404_without_a_second_attempt(cache, monkeypatch):
    """A 404 is an answer, not a transport failure. Retrying it — or falling
    back to urllib — would double the cost of every miss, and the chain is
    BUILT on misses: each of the four sources is tried in turn."""
    import http.client

    calls = {"n": 0}

    class NotFound:
        def __init__(self, *a, **kw):
            pass
        def request(self, *a, **kw):
            calls["n"] += 1
        def getresponse(self):
            class R:
                status = 404
                def read(self_inner):
                    return b""
                def getheader(self_inner, _n):
                    return None
            return R()
        def close(self):
            pass

    monkeypatch.setattr(E, "_CONNS", __import__("threading").local())
    monkeypatch.setattr(http.client, "HTTPSConnection", NotFound)
    monkeypatch.setattr(E.urllib.request, "urlopen",
                        lambda *a, **kw: pytest.fail("a 404 must not fall back to urllib"))

    assert E._download("https://example.test/x.png") is None
    assert calls["n"] == 1, "a 404 was retried"


# --- fallback provenance ------------------------------------------------------
#
# The chain caches whatever it gets, so a failure of the PRIMARY source is
# written to disk and believed forever. That is how two ordinary skin-tone
# emoji (1f44b-1f3fe, 1f91a-1f3fe — both present in the Apple set, verified
# against the live CDN) sat in a real cache as 512px Noto art: one hiccup
# during a warm run, then never re-checked. These pin the re-check.

def _apple_only(url: str) -> bytes | None:
    return _png_bytes(APPLE_PX) if E.APPLE_BASE in url else None


def test_a_transient_primary_failure_is_retried_not_believed_forever(cache, monkeypatch):
    # Round 1: the primary is UNREACHABLE (transport died), so Noto answers.
    monkeypatch.setattr(E, "_download_ex",
                        lambda url: (None, "unreachable") if E.APPLE_BASE in url
                        else (_png_bytes(NOTO_PX), "ok"))
    monkeypatch.setattr(E, "_download",
                        lambda url: _png_bytes(NOTO_PX) if E.NOTO_BASE in url else None)
    p = E.fetch_emoji_png("\U0001F44B\U0001F3FE")
    assert Image.open(p).size == (NOTO_PX, NOTO_PX)
    assert E._fallback_state(p) == E._FALLBACK_RETRY

    # Round 2: the primary is reachable again. The cached fallback must NOT
    # short-circuit the fetch — this is the whole defect.
    monkeypatch.setattr(E, "_download_ex", lambda url: (_apple_only(url), "ok"))
    monkeypatch.setattr(E, "_download", _apple_only)
    p2 = E.fetch_emoji_png("\U0001F44B\U0001F3FE")
    assert Image.open(p2).size == (APPLE_PX, APPLE_PX)
    assert E._fallback_state(p2) is None, "primary art must carry no marker"


def test_an_emoji_the_primary_really_lacks_stops_being_re_checked(cache, monkeypatch):
    """♀️ ♂️ ⚕️ are genuinely absent from Apple. Re-requesting them on every
    fetch would be pure waste, so a confirmed miss is recorded as final."""
    calls = []

    def dl_ex(url):
        calls.append(url)
        return (None, "absent") if E.APPLE_BASE in url else (_png_bytes(NOTO_PX), "ok")

    monkeypatch.setattr(E, "_download_ex", dl_ex)
    monkeypatch.setattr(E, "_download",
                        lambda url: _png_bytes(NOTO_PX) if E.NOTO_BASE in url else None)
    p = E.fetch_emoji_png("\u2640\ufe0f")
    assert Image.open(p).size == (NOTO_PX, NOTO_PX)
    assert E._fallback_state(p) == E._FALLBACK_FINAL

    calls.clear()
    assert E.fetch_emoji_png("\u2640\ufe0f") == p
    assert not calls, "a settled fallback must be served straight from cache"


def test_offline_keeps_the_fallback_art_it_already_has(cache, monkeypatch):
    """A re-check that can't reach the primary must not lose the art."""
    monkeypatch.setattr(E, "_download_ex",
                        lambda url: (None, "unreachable") if E.APPLE_BASE in url
                        else (_png_bytes(NOTO_PX), "ok"))
    monkeypatch.setattr(E, "_download",
                        lambda url: _png_bytes(NOTO_PX) if E.NOTO_BASE in url else None)
    p = E.fetch_emoji_png("\U0001F44B\U0001F3FE")

    monkeypatch.setattr(E, "_download_ex", lambda url: (None, "unreachable"))
    monkeypatch.setattr(E, "_download", lambda url: None)
    again = E.fetch_emoji_png("\U0001F44B\U0001F3FE")
    assert again == p and Image.open(again).size == (NOTO_PX, NOTO_PX)


def test_audit_style_reports_every_non_primary_entry(cache, monkeypatch):
    E.EMOJI_CACHE.mkdir(parents=True, exist_ok=True)
    (E.EMOJI_CACHE / "1f602.png").write_bytes(_png_bytes(APPLE_PX))
    (E.EMOJI_CACHE / "2640.png").write_bytes(_png_bytes(NOTO_PX))
    rows = E.audit_style()
    assert [r[0] for r in rows] == ["2640"], "only non-Apple-sized art is flagged"
    assert rows[0][2] == "unmarked", "written before markers existed"


def test_emoji_from_seq_round_trips_the_filename_stem():
    for ch in ("\U0001F602", "\U0001F44B\U0001F3FE", "\u2764\ufe0f", "\U0001F636\u200d\U0001F32B\ufe0f"):
        seq = E._codepoints(ch, keep_vs16=False)
        assert E._codepoints(E._emoji_from_seq(seq), keep_vs16=False) == seq
    assert E._emoji_from_seq("not-hex") is None


# --- per-session copies -------------------------------------------------------

def test_session_copies_are_restyled_to_the_current_set(cache, tmp_path, monkeypatch):
    """`add_sticker` copies artwork INTO the session, and that copy used to be
    written once and never revisited — so switching sets updated the shared
    cache while every existing project kept serving its old artwork. Reported
    as a 256px Fluent 3D face sitting beside a 160px Apple one in ONE frame."""
    monkeypatch.setattr(E, "_download_ex", lambda url: (_apple_only(url), "ok"))
    monkeypatch.setattr(E, "_download", _apple_only)
    st = tmp_path / "stickers"
    st.mkdir()
    stale = st / "1f602.png"
    stale.write_bytes(_png_bytes(FLUENT_PX))          # old-style copy
    mine = st / "my-logo.png"
    mine.write_bytes(_png_bytes(FLUENT_PX))           # a user's own PNG

    changed = E.refresh_session_sticker_art(st)

    assert changed == ["1f602"]
    assert Image.open(stale).size == (APPLE_PX, APPLE_PX)
    assert Image.open(mine).size == (FLUENT_PX, FLUENT_PX), \
        "a non-emoji filename is not artwork this module owns"


def test_restyle_is_idempotent_and_survives_a_missing_directory(cache, tmp_path, monkeypatch):
    monkeypatch.setattr(E, "_download_ex", lambda url: (_apple_only(url), "ok"))
    monkeypatch.setattr(E, "_download", _apple_only)
    st = tmp_path / "stickers"
    st.mkdir()
    (st / "1f602.png").write_bytes(_png_bytes(FLUENT_PX))
    assert E.refresh_session_sticker_art(st) == ["1f602"]
    assert E.refresh_session_sticker_art(st) == [], "already current — no rewrite"
    assert E.refresh_session_sticker_art(tmp_path / "nope") == []
