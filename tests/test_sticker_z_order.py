"""Sticker z-order (issue 6): per-clip `z` overrides insertion order.

Two layers under test:

1. `set_clip_z` tool semantics — int / 'front' / 'back' resolution against the
   sibling stickers on the same track.

2. The renderer actually honors clip z: two overlapping solid-color stickers
   composite with the higher-z one on top, verified by pixel-sampling a
   rendered preview frame at the overlap center. The default (both z=0) pins
   the current behavior: later `start` wins (stable sort by start → later
   sticker overlays composite after, i.e. on top).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Canvas, Clip, EDL, Sticker, Track, Transform
from video_ai_editor.agent.dispatch import dispatch

FFMPEG = shutil.which("ffmpeg")


# ---------------------------------------------------------------------------
# helpers

def _solid_png(path: Path, color: tuple[int, int, int]) -> Path:
    """A 64x64 fully-opaque solid-color PNG (a sticker with no transparency)."""
    Image.new("RGBA", (64, 64), (*color, 255)).save(path)
    return path


def _store_with_two_stickers(tmp_path: Path) -> tuple[EDLStore, str, str]:
    """Session: 4s blue lavfi video on v1 + red & green stickers at the SAME
    canvas position (fully overlapping), red starting later than green.
    Returns (store, green_id, red_id)."""
    src = tmp_path / "bg.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", "color=c=black:s=320x568:d=4:r=30",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
         "-shortest", "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True,
    )
    green_png = _solid_png(tmp_path / "green.png", (0, 200, 0))
    red_png = _solid_png(tmp_path / "red.png", (220, 0, 0))
    cw, ch = 320, 568
    edl = EDL(
        canvas=Canvas(w=cw, h=ch, fps=30),
        tracks=[
            Track(id="v1", type="video", clips=[
                Clip(id="c1", src=str(src), in_=0.0, out=4.0, start=0.0),
            ]),
            Track(id="stickers", type="sticker", z=12, clips=[
                # Same center → total overlap. Green starts first, red later:
                # with equal z the stable start-sort puts red on top today.
                Sticker(id="sg", src=str(green_png), start=0.0, end=4.0,
                        transform=Transform(x=cw / 2, y=ch / 2, scale=1.0)),
                Sticker(id="sr", src=str(red_png), start=0.5, end=4.0,
                        transform=Transform(x=cw / 2, y=ch / 2, scale=1.0)),
            ]),
        ],
    )
    edl.recompute_duration()
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)
    return store, "sg", "sr"


def _center_pixel(video: Path, at_s: float, w: int, h: int) -> tuple[int, int, int]:
    """Decode one frame at `at_s` and return the RGB of the exact center pixel
    (both stickers are centered on the canvas, so this is the overlap center)."""
    frame = video.parent / f"frame_{at_s:.2f}.png"
    subprocess.run(
        ["ffmpeg", "-y", "-ss", str(at_s), "-i", str(video),
         "-frames:v", "1", str(frame)],
        check=True, capture_output=True,
    )
    with Image.open(frame) as im:
        rgb = im.convert("RGB")
        return rgb.getpixel((rgb.width // 2, rgb.height // 2))


def _is_red(px: tuple[int, int, int]) -> bool:
    r, g, b = px
    return r > 140 and g < 90 and b < 90


def _is_green(px: tuple[int, int, int]) -> bool:
    r, g, b = px
    return g > 120 and r < 90 and b < 90


# ---------------------------------------------------------------------------
# 1) set_clip_z tool semantics

def test_set_clip_z_int_front_back(tmp_path):
    store, gid, rid = _store_with_two_stickers(tmp_path)

    # Default: field exists and is 0 (legacy order preserved).
    _, g = store.edl.get_clip(gid)
    _, r = store.edl.get_clip(rid)
    assert g.z == 0 and r.z == 0

    # Explicit int.
    res = dispatch(store, "set_clip_z", {"clip_id": gid, "z": 5})
    _, g = store.edl.get_clip(gid)
    assert g.z == 5
    assert res.get("z") == 5

    # 'front' = max(existing sibling z) + 1  → red above green's 5.
    dispatch(store, "set_clip_z", {"clip_id": rid, "z": "front"})
    _, r = store.edl.get_clip(rid)
    assert r.z == 6

    # 'back' = min(existing sibling z) - 1 → green below red's 6... siblings
    # are now {green:5, red:6}; back puts green at 4? No — min is green's own
    # 5 vs red's 6 → min=5, so back → 4.
    dispatch(store, "set_clip_z", {"clip_id": gid, "z": "back"})
    _, g = store.edl.get_clip(gid)
    assert g.z == 4

    # Rejects non-sticker/overlay-less targets cleanly.
    with pytest.raises(ValueError):
        dispatch(store, "set_clip_z", {"clip_id": "nope"})


def test_set_clip_z_roundtrips_through_serialization(tmp_path):
    store, gid, _rid = _store_with_two_stickers(tmp_path)
    dispatch(store, "set_clip_z", {"clip_id": gid, "z": 3})
    # Reload from disk — z must survive the JSON round trip.
    store2 = EDLStore(tmp_path)
    _, g = store2.edl.get_clip(gid)
    assert g.z == 3


# ---------------------------------------------------------------------------
# 2) rendered-frame pixel proof

@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not available")
def test_default_order_later_start_wins_then_z_overrides(tmp_path):
    from video_ai_editor.render import render_preview

    store, gid, rid = _store_with_two_stickers(tmp_path)

    # --- Default (both z=0): red (later start) composites on top.
    pv = render_preview(store.edl, tmp_path)
    px = _center_pixel(pv.path, 2.0, 320, 568)
    assert _is_red(px), f"expected red on top by default, got {px}"

    # --- set_clip_z back on red → green must now be on top. The z change is
    # part of the model, so edl.hash() changes and the render cache re-renders.
    dispatch(store, "set_clip_z", {"clip_id": rid, "z": "back"})
    pv2 = render_preview(store.edl, tmp_path)
    px2 = _center_pixel(pv2.path, 2.0, 320, 568)
    assert _is_green(px2), f"expected green on top after red sent to back, got {px2}"
