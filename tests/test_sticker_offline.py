"""Emoji stickers are added OFFLINE and never touch the network (gate X3).

`add_sticker` used to call `emoji.fetch_emoji_png`, which opens a connection
to cdn.jsdelivr.net: an unguarded network fetch in the middle of an edit op,
and a sticker that could not be added at all without a connection. Dispatch
now resolves artwork from local sources only (`emoji.local_emoji_png`): the
cached pinned set, else a tile drawn from the installed Apple emoji font
(`ai/emoji_local.py`), else a prior-style cache.

Every test here runs behind a socket guard and against empty temp caches, so
"it worked" can never mean "it quietly downloaded".
"""
from __future__ import annotations

import io
import socket
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.ai import emoji as E
from video_ai_editor.ai import emoji_local as L
from video_ai_editor.edl import EDLStore

FIRE = "\U0001F525"
LOCAL = pytest.mark.skipif(not L.available(), reason="no installed Apple emoji font (macOS only)")
APPLE_FONT = Path("/System/Library/Fonts/Apple Color Emoji.ttc")


@pytest.fixture
def no_network(monkeypatch) -> list[str]:
    """Every outbound socket path refuses and is recorded."""
    attempts: list[str] = []

    def refuse(via):
        def _f(*a, **k):
            attempts.append(f"{via}:{a[1] if len(a) > 1 else a[0] if a else ''}")
            raise OSError(f"network disabled in this test ({via})")
        return _f

    monkeypatch.setattr(socket.socket, "connect", refuse("connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", refuse("connect_ex"))
    monkeypatch.setattr(socket, "create_connection", refuse("create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", refuse("getaddrinfo"))
    return attempts


@pytest.fixture
def cold_cache(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "emoji"
    monkeypatch.setattr(E, "_EMOJI_CACHE_ROOT", root)
    monkeypatch.setattr(E, "EMOJI_CACHE", root / "apple2")
    monkeypatch.setattr(E, "LOCAL_CACHE", root / "local")
    return root


def _store(tmp_path: Path) -> EDLStore:
    st = EDLStore(tmp_path / "session")
    st.commit("init", {}, "init")
    return st


def _png(size: int, rgba=(255, 0, 0, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (size, size), rgba).save(buf, format="PNG")
    return buf.getvalue()


def _premultiplied(png: bytes) -> np.ndarray:
    a = np.asarray(Image.open(io.BytesIO(png)).convert("RGBA")).astype(np.int64)
    return np.concatenate([np.round(a[..., :3] * a[..., 3:4] / 255.0), a[..., 3:4]], axis=2)


def test_add_sticker_never_opens_a_socket(tmp_path, cold_cache, no_network):
    """Cold cache, no network: the op must not try the CDN. Where the machine
    can draw the tile it succeeds with a 160x160 tile; where it cannot it
    refuses cleanly — and neither path opens a connection."""
    st = _store(tmp_path)
    if L.available():
        sid = dispatch(st, "add_sticker", {"emoji": FIRE})["sticker_id"]
        src = Path(st.edl.get_clip(sid)[1].src)
        assert src.parent == st.dir / "uploads" / "stickers", "the session owns its copy"
        assert src.read_bytes() == (cold_cache / "local" / "1f525.png").read_bytes()
        with Image.open(src) as im:
            assert im.size == (160, 160)
    else:
        with pytest.raises(ValueError, match="no sticker artwork"):
            dispatch(st, "add_sticker", {"emoji": FIRE})
    assert no_network == [], f"add_sticker reached for the network: {no_network}"
    assert not (cold_cache / "apple2").exists() or not any((cold_cache / "apple2").iterdir()), \
        "the live namespace holds the pinned download only"


def test_add_sticker_uses_the_cached_pinned_art_first(tmp_path, cold_cache, no_network, monkeypatch):
    """The normal path: the picker's swatch request already cached the pinned
    tile, and the sticker is exactly those bytes — nothing is drawn."""
    (cold_cache / "apple2").mkdir(parents=True)
    pinned = _png(160, (1, 2, 3, 255))
    (cold_cache / "apple2" / "1f525.png").write_bytes(pinned)
    monkeypatch.setattr(L, "render_png", lambda e: pytest.fail("drew a tile the cache already had"))
    st = _store(tmp_path)
    sid = dispatch(st, "add_sticker", {"emoji": FIRE})["sticker_id"]
    assert Path(st.edl.get_clip(sid)[1].src).read_bytes() == pinned
    assert no_network == []


def test_offline_with_no_art_anywhere_refuses_cleanly(tmp_path, cold_cache, no_network, monkeypatch):
    monkeypatch.setattr(L, "render_png", lambda e: None)
    st = _store(tmp_path)
    n = len(st.ops.ops)
    with pytest.raises(ValueError, match="no sticker artwork .* on this machine"):
        dispatch(st, "add_sticker", {"emoji": FIRE})
    assert len(st.ops.ops) == n, "a refused add commits nothing"
    assert no_network == []


def test_a_prior_style_cache_is_the_last_resort(tmp_path, cold_cache, no_network, monkeypatch):
    monkeypatch.setattr(L, "render_png", lambda e: None)
    (cold_cache / "noto").mkdir(parents=True)
    old = _png(512)
    (cold_cache / "noto" / "1f525.png").write_bytes(old)
    st = _store(tmp_path)
    sid = dispatch(st, "add_sticker", {"emoji": FIRE})["sticker_id"]
    assert Path(st.edl.get_clip(sid)[1].src).read_bytes() == old
    assert no_network == []


@LOCAL
def test_the_local_tile_is_the_fonts_own_bitmap():
    """The pinned set IS the font's 160 px bitmap strike. Read that bitmap
    straight out of the font file and compare: identical alpha, colour within
    one level (CoreGraphics premultiplies, a PNG stores straight)."""
    fontTools = pytest.importorskip("fontTools.ttLib")
    if not APPLE_FONT.exists():
        pytest.skip("Apple Color Emoji is not at its usual path")
    import struct
    f = fontTools.TTFont(str(APPLE_FONT), fontNumber=0, lazy=True)
    gid = f.getGlyphID(f.getBestCmap()[ord("\U0001F602")])
    base = f.reader.tables["sbix"].offset
    with APPLE_FONT.open("rb") as fh:
        fh.seek(base)
        _v, _fl, n = struct.unpack(">HHI", fh.read(8))
        strikes = struct.unpack(f">{n}I", fh.read(4 * n))
        best = []
        for so in strikes:
            fh.seek(base + so)
            best.append((struct.unpack(">H", fh.read(2))[0], so))
        ppem, so = max(best)
        fh.seek(base + so + 4 + 4 * gid)
        a, b = struct.unpack(">II", fh.read(8))
        fh.seek(base + so + a + 8)
        embedded = fh.read(b - a - 8)
    assert ppem == 160
    got = L.render_png("\U0001F602")
    assert got is not None
    want, have = _premultiplied(embedded), _premultiplied(got)
    assert want.shape == have.shape == (160, 160, 4)
    assert np.array_equal(want[..., 3], have[..., 3]), "the tile is placed exactly"
    assert np.abs(want - have).max() <= 1


@LOCAL
@pytest.mark.parametrize("text, why", [
    ("A", "plain text is drawn by a substitute face"),
    ("♂️", "the font has only an OUTLINE for this sign — a text glyph, not artwork"),
    ("\U0001F1FD\U0001F1FD", "not a flag: two separate tiles side by side"),
    ("", "nothing"),
])
def test_local_drawing_refuses_what_is_not_one_tile_of_artwork(text, why):
    assert L.render_png(text) is None, why


@LOCAL
@pytest.mark.parametrize("emoji", [
    "\U0001F469\U0001F3FD‍\U0001F4BB",          # ZWJ + skin tone
    "\U0001F1EE\U0001F1F3",                           # a flag
    "#️⃣",                                  # a keycap
    "❤️",                                   # a legacy dingbat with VS16
    "\U0001F469\U0001F3FB‍❤️‍\U0001F48B‍\U0001F468\U0001F3FF",  # 10 codepoints
])
def test_local_drawing_joins_sequences_into_one_tile(emoji):
    got = L.render_png(emoji)
    assert got is not None
    with Image.open(io.BytesIO(got)) as im:
        assert im.size == (160, 160) and im.getbbox() is not None


@LOCAL
def test_the_fetch_path_never_draws_with_the_local_font(cold_cache, no_network, monkeypatch):
    """The locally drawn tile is for STICKERS, whose bytes travel with the
    session. `fetch_emoji_png` also feeds emoji inside text, rendered afresh
    on each machine at export — it must stay platform-independent, so an
    offline miss there is still None, never an OS-font drawing."""
    monkeypatch.setattr(L, "render_png", lambda e: pytest.fail("fetch path drew locally"))
    assert E.fetch_emoji_png(FIRE) is None


def test_restyle_never_swaps_a_copy_for_prior_style_art(cold_cache, tmp_path, monkeypatch, no_network):
    """Offline, `fetch_emoji_png` ends at a prior-style cache. A session copy
    drawn locally (Apple artwork) must not be 'restyled' to older Noto art
    just because the project was opened without a connection."""
    (cold_cache / "noto").mkdir(parents=True)
    (cold_cache / "noto" / "1f525.png").write_bytes(_png(512))
    stickers = tmp_path / "stickers"
    stickers.mkdir()
    mine = _png(160, (9, 9, 9, 255))
    (stickers / "1f525.png").write_bytes(mine)
    assert E.refresh_session_sticker_art(stickers) == []
    assert (stickers / "1f525.png").read_bytes() == mine


def test_restyle_still_upgrades_a_local_copy_to_the_pinned_bytes(cold_cache, tmp_path, monkeypatch):
    """...and once the pinned art is reachable, the local copy IS replaced by it."""
    pinned = _png(160, (1, 2, 3, 255))
    monkeypatch.setattr(E, "_download_ex", lambda url: (pinned if "img-apple-160" in url else None, "ok"))
    monkeypatch.setattr(E, "_download", lambda url: None)
    stickers = tmp_path / "stickers"
    stickers.mkdir()
    (stickers / "1f525.png").write_bytes(_png(160, (9, 9, 9, 255)))
    assert E.refresh_session_sticker_art(stickers) == ["1f525"]
    assert (stickers / "1f525.png").read_bytes() == pinned
