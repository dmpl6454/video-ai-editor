"""Emoji sticker artwork: source, resolution, cache namespacing, fallback.

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


def _webp_bytes(size: int = 256) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (size, size), (255, 200, 0, 255)).save(buf, format="WEBP")
    return buf.getvalue()


def _png_bytes(size: int = 72) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (size, size), (255, 0, 0, 255)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """Point both cache tiers at tmp_path so nothing touches the real cache."""
    root = tmp_path / "emoji"
    monkeypatch.setattr(E, "_EMOJI_CACHE_ROOT", root)
    monkeypatch.setattr(E, "EMOJI_CACHE", root / "fluent3d")
    return root


def test_prefers_fluent_3d_and_converts_webp_to_png(cache, monkeypatch):
    """Fluent 3D is the point of the whole exercise: shaded/dimensional and
    256px, where Twemoji is deliberately flat line-art at 72px (a sticker is
    22% of the canvas's longer edge = 422px at 1080x1920, so 72px was a ~6x
    upscale — blurry AND visibly '2D')."""
    seen: list[str] = []

    def fake_dl(url: str):
        seen.append(url)
        return _webp_bytes() if "fluent-emoji-3d" in url else None

    monkeypatch.setattr(E, "_download", fake_dl)
    out = E.fetch_emoji_png("\U0001F60E")
    assert out is not None and out.exists()
    assert any("fluent-emoji-3d" in u for u in seen)
    assert not any("twemoji" in u for u in seen), "Twemoji is the fallback, not the default"

    # Stored as PNG (one format downstream: the Pillow bake, _png_is_valid and
    # the browser <img> all expect it) at Fluent's native resolution.
    with Image.open(out) as im:
        assert im.format == "PNG"
        assert im.size == (256, 256)
        assert im.mode == "RGBA", "alpha must survive the WebP->PNG conversion"


def test_tries_both_vs16_spellings_against_fluent(cache, monkeypatch):
    """Twemoji strips VS16 from every filename; Fluent keeps it for emoji whose
    base codepoint is a legacy dingbat — a heart is `2764-fe0f` there and plain
    `2764` 404s. Guessing one spelling silently loses those."""
    tried: list[str] = []

    # VS16-preserving spelling wins when it exists, and short-circuits.
    def fake_keep(url: str):
        tried.append(url.rsplit("/", 1)[-1])
        return _webp_bytes() if url.endswith("2764-fe0f.webp") else None

    monkeypatch.setattr(E, "_download", fake_keep)
    assert E.fetch_emoji_png("❤️") is not None
    assert tried[0] == "2764-fe0f.webp", f"VS16 spelling must be tried first, got {tried}"

    # And when only the STRIPPED spelling exists, that one is reached too —
    # this is the direction a one-spelling implementation silently loses.
    tried.clear()
    E.EMOJI_CACHE.mkdir(parents=True, exist_ok=True)
    for f in E.EMOJI_CACHE.glob("*.png"):
        f.unlink()

    def fake_strip(url: str):
        tried.append(url.rsplit("/", 1)[-1])
        return _webp_bytes() if url.endswith("/2764.webp") else None

    monkeypatch.setattr(E, "_download", fake_strip)
    assert E.fetch_emoji_png("❤️") is not None
    assert tried == ["2764-fe0f.webp", "2764.webp"], tried


def test_falls_back_to_twemoji_when_fluent_lacks_the_emoji(cache, monkeypatch):
    """Fluent's coverage is broad but not total. A flat sticker beats a failed
    add_sticker."""
    monkeypatch.setattr(E, "_download",
                        lambda url: _png_bytes() if "twemoji" in url else None)
    out = E.fetch_emoji_png("\U0001F60E")
    assert out is not None
    with Image.open(out) as im:
        assert im.size == (72, 72)


def test_falls_back_to_a_legacy_cached_file_when_offline(cache, monkeypatch):
    """Offline, an install that already has flat art for this emoji must keep
    showing it rather than failing outright because the 3D upgrade can't
    reach the network."""
    cache.mkdir(parents=True, exist_ok=True)
    legacy = cache / "1f60e.png"
    legacy.write_bytes(_png_bytes())

    monkeypatch.setattr(E, "_download", lambda url: None)
    assert E.fetch_emoji_png("\U0001F60E") == legacy


def test_cache_is_style_namespaced_so_old_flat_art_cannot_shadow_the_new(cache, monkeypatch):
    """Both styles name the file `<codepoint>.png`. Sharing one directory
    meant every existing install would keep serving its cached Twemoji
    forever and never see the 3D artwork — the upgrade would look like it had
    silently done nothing."""
    assert E.EMOJI_CACHE != E._EMOJI_CACHE_ROOT
    assert E.EMOJI_CACHE.parent == E._EMOJI_CACHE_ROOT

    cache.mkdir(parents=True, exist_ok=True)
    (cache / "1f60e.png").write_bytes(_png_bytes())  # legacy flat art present

    monkeypatch.setattr(E, "_download",
                        lambda url: _webp_bytes() if "fluent" in url else None)
    out = E.fetch_emoji_png("\U0001F60E")
    with Image.open(out) as im:
        assert im.size == (256, 256), "legacy flat art shadowed the 3D fetch"


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


def test_webp_decode_failure_degrades_to_twemoji(cache, monkeypatch):
    """Fluent ships WebP, so this path needs Pillow's WebP plugin. A frozen
    build that shipped the module without its codec would otherwise raise
    where nothing catches it — the exact shape of the packaged-app caption
    bug (module present, its asset absent -> bare HTTP 500). Degrade to flat
    Twemoji instead: worse art beats a broken button.
    """
    def fake_dl(url: str):
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
        assert im.size == (72, 72), "expected the Twemoji fallback"
