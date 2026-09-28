"""A title with emoji exported offline keeps its emoji, and a later export
online does not reuse the offline PNG (final QA, round 3).

THE DEFECT: `text_overlay._emoji_image` only asked `fetch_emoji_png`, which
needs the network. Offline it returned None and the emoji was left out as a
blank gap — silently — although `ai.emoji.local_emoji_png` draws the same
Apple artwork without the network (what `add_sticker` already uses). And the
text PNG cache key did not record that, so the degraded PNG was reused by
every later export, online too. The `/api/emoji/{seq}.png` route had the
same gap: a 404 offline, so the preview and the picker showed blanks.

No test here opens a connection: both resolvers are replaced.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from video_ai_editor.ai import emoji as E
from video_ai_editor.edl.schema import EDL, Canvas, TextClip, TextStyle, Track
from video_ai_editor.render import text_overlay as T

W, H = 640, 360
RED, GREEN = (255, 0, 0, 255), (0, 255, 0, 255)


def _tile(path: Path, rgba) -> Path:
    Image.new("RGBA", (160, 160), rgba).save(path)
    return path


@pytest.fixture()
def art(tmp_path, monkeypatch):
    """`net` stands for the downloaded artwork (green), `local` for the tile
    drawn from the installed font (red). `state["online"]` flips the network."""
    net, local = _tile(tmp_path / "net.png", GREEN), _tile(tmp_path / "local.png", RED)
    state = {"online": False, "fetches": 0}

    def fetch(emoji):
        state["fetches"] += 1
        return net if state["online"] else None
    monkeypatch.setattr(E, "fetch_emoji_png", fetch)
    monkeypatch.setattr(E, "local_emoji_png", lambda emoji: local)
    return state


def _edl(text: str = "Hot \U0001F525 deal", **kw) -> EDL:
    clip = TextClip(id="t", text=text, start=0, end=3, role="hook",
                    style=TextStyle(color="#FFFFFF", stroke_w=0, shadow=None), **kw)
    edl = EDL(canvas=Canvas(w=W, h=H, fps=30), tracks=[Track(id="tx", type="text", z=10, clips=[clip])])
    edl.recompute_duration()
    return edl


def _count(png: Path, rgb) -> int:
    a = np.asarray(Image.open(png).convert("RGBA")).astype(int)
    return int(((np.abs(a[:, :, :3] - np.array(rgb[:3])).max(axis=2) < 40) & (a[:, :, 3] > 200)).sum())


def test_offline_the_emoji_is_drawn_from_the_local_tile(tmp_path, art):
    [(_c, _role, png)] = T.cache_text_pngs(_edl(), tmp_path / "cache")
    assert _count(png, RED) > 200, "the emoji was left out offline"


def test_a_degraded_png_is_not_reused_once_the_network_is_back(tmp_path, art):
    cache = tmp_path / "cache"
    [(_c, _r, offline)] = T.cache_text_pngs(_edl(), cache)
    assert _count(offline, RED) > 200
    art["online"] = True
    [(_c, _r, online)] = T.cache_text_pngs(_edl(), cache)
    assert _count(online, GREEN) > 200, "the online export reused the offline PNG"
    assert _count(online, RED) == 0
    # ...and once resolved it IS cached: no further fetch for the same title
    n = art["fetches"]
    [(_c, _r, again)] = T.cache_text_pngs(_edl(), cache)
    assert again == online and art["fetches"] == n


def test_the_same_holds_for_animated_text(tmp_path, art):
    """Keyframed text renders through `cache_xform_text_pngs`."""
    from video_ai_editor.edl.schema import Transform
    edl = _edl(transform=Transform(x=W / 2, y=H / 2, rotation={"keyframes": [[0.0, 0.0], [2.0, 45.0]]}))
    [item] = T.cache_xform_text_pngs(edl, tmp_path / "cache")
    assert _count(item["png"], RED) > 100
    art["online"] = True
    [item2] = T.cache_xform_text_pngs(edl, tmp_path / "cache")
    assert _count(item2["png"], GREEN) > 100 and _count(item2["png"], RED) == 0


def test_the_emoji_route_serves_the_local_tile_offline(tmp_path, art, monkeypatch):
    from fastapi.testclient import TestClient
    from video_ai_editor import main
    client = TestClient(main.app)
    r = client.get("/api/emoji/1f525.png")
    assert r.status_code == 200
    assert "immutable" not in r.headers.get("cache-control", ""), "a stand-in must not be cached for good"
    art["online"] = True
    r = client.get("/api/emoji/1f525.png")
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
