"""BrandKit end_card / palette / font — the three fields apply_brand_kit wrote
into the EDL that nothing read. They are now (a) validated loudly at the tool
boundary and (b) materialised into the clips the kit creates: end_card becomes
a full-canvas sticker under the end-card text, palette[0] becomes the
end-card text color, font becomes the created clips' TextStyle font.
"""
from __future__ import annotations
from pathlib import Path

import pytest
from PIL import Image

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Clip, Sticker
from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.show.brand_kit import ENDCARD_STICKER_ID


@pytest.fixture
def store(tmp_path: Path) -> EDLStore:
    s = EDLStore(tmp_path / "sess")
    s.edl.get_track("v1").clips.append(Clip(src=str(tmp_path / "x.mp4"), in_=0, out=10, start=0))
    s.edl.recompute_duration()
    return s


@pytest.fixture
def end_card_png(tmp_path: Path) -> Path:
    p = tmp_path / "endcard.png"
    Image.new("RGB", (320, 568), (20, 30, 40)).save(p)
    return p


FULL_KIT = {"handle": "@brand", "hashtags": ["#a"], "palette": ["ffcc00", "#112233"],
            "font": "BebasNeue-Regular"}


def test_fields_materialise_into_clips(store: EDLStore, end_card_png: Path):
    dispatch(store, "apply_brand_kit", {**FULL_KIT, "end_card": str(end_card_png)})
    ec = store.edl.get_track("tx_endcard").clips[0]
    assert ec.style.color == "#ffcc00"          # palette[0], normalised to #-form
    assert ec.style.font == "BebasNeue-Regular"  # brand font
    wm = store.edl.get_track("tx_watermark").clips[0]
    assert wm.style.font == "BebasNeue-Regular"
    stick = [c for c in store.edl.get_track("stickers").clips
             if isinstance(c, Sticker) and c.id == ENDCARD_STICKER_ID]
    assert len(stick) == 1
    assert stick[0].src == str(end_card_png)
    assert stick[0].end == store.edl.duration
    # full-canvas sizing: 22% × scale spans the long edge
    assert abs(float(stick[0].transform.scale) * 0.22 - 1.0) < 0.01


def test_reapply_is_idempotent(store: EDLStore, end_card_png: Path):
    dispatch(store, "apply_brand_kit", {**FULL_KIT, "end_card": str(end_card_png)})
    dispatch(store, "apply_brand_kit", {**FULL_KIT, "end_card": str(end_card_png)})
    stickers = [c for c in store.edl.get_track("stickers").clips
                if c.id == ENDCARD_STICKER_ID]
    assert len(stickers) == 1
    assert len(store.edl.get_track("tx_endcard").clips) == 1


def test_endcard_sticker_sits_under_endcard_text(store: EDLStore, end_card_png: Path):
    """Track z: stickers (12) < end-card text (15) → the overlay compositor
    must place the image before the text in the graph."""
    dispatch(store, "apply_brand_kit", {**FULL_KIT, "end_card": str(end_card_png)})
    stickers_z = store.edl.get_track("stickers").z
    endcard_z = store.edl.get_track("tx_endcard").z
    assert stickers_z < endcard_z


@pytest.mark.parametrize("bad,match", [
    ({"font": "ComicSans"}, "not a bundled font"),
    ({"end_card": "/nonexistent/nope.png"}, "readable image"),
    ({"palette": ["red"]}, "hex colors"),
    ({"palette": ["#12345"]}, "hex colors"),
])
def test_invalid_values_rejected_loudly(store: EDLStore, bad: dict, match: str):
    with pytest.raises(ValueError, match=match):
        dispatch(store, "apply_brand_kit", bad)


def test_plain_kit_unchanged(store: EDLStore):
    """A handle-and-hashtags-only kit (the only part that ever worked) still
    behaves exactly as before: no sticker, sentinel styles."""
    dispatch(store, "apply_brand_kit", {"handle": "@x", "hashtags": ["#y"]})
    assert not any(c.id == ENDCARD_STICKER_ID
                   for c in store.edl.get_track("stickers").clips)
    ec = store.edl.get_track("tx_endcard").clips[0]
    assert ec.style.color == "#FFFFFF"       # sentinel → role style
    assert ec.style.font == "Inter-Black"    # sentinel → role style
