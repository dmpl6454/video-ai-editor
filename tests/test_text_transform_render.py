"""TextClip transform.x / transform.y / style.size actually render.

These fields were persisted-and-ignored: the Properties inspector commits
`transform.x`, `transform.y`, `style.size` via set_property, but BOTH
renderers (render/text_overlay.py and TextLayer.tsx) were role-anchored —
the server hard-coded horizontal centering and passed a literal
`canvas_h*0.75` where `_y_for_role` already accepted a `transform_y`
parameter, and text size came only from ROLE_STYLES[role]["size"].

Sentinel semantics (mirrors the 2026-07-17 color/font sentinel pattern in
resolve_style_overrides): the schema defaults mean "unset — role
positioning / role size", so every existing project renders unchanged.
  - size sentinel: 96 (TextStyle schema default)
  - x sentinels: 540 (Transform schema default on TextClip) and canvas.w/2
    (every tool's default = the current hard-coded centering — a no-op)
  - y sentinels: 1700 (schema default), canvas.h*0.85 (add_text's no-arg
    default, which never matched what rendered), and the role's own anchor
    (add_super_text/brand_kit write the anchor value itself — a no-op)
  - caption role: transform overrides are ignored entirely (the captions
    block owns caption positioning).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from PIL import Image

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import (
    EDL, Canvas, Clip, Sticker, TextClip, TextStyle, Track, Transform,
)
from video_ai_editor.render.text_overlay import (
    ROLE_STYLES,
    build_overlay_chain,
    cache_text_pngs,
    render_text_png,
    resolve_anchor_overrides,
    resolve_size_override,
)

CW, CH = 320, 180  # small canvas keeps the Pillow renders fast


def _edl(clips: list[TextClip], w: int = CW, h: int = CH) -> EDL:
    return EDL(
        canvas=Canvas(w=w, h=h, fps=30),
        tracks=[
            Track(id="v1", type="video", clips=[]),
            Track(id="text", type="text", z=10, clips=clips),
        ],
    )


def _ink_centroid(img: Image.Image) -> tuple[float, float] | None:
    """Centroid (x, y) of non-transparent pixels, or None if fully clear."""
    alpha = img.split()[-1]
    w, h = img.size
    data = alpha.load()
    sx = sy = n = 0
    for y in range(h):
        for x in range(w):
            if data[x, y] > 8:
                sx += x
                sy += y
                n += 1
    if n == 0:
        return None
    return sx / n, sy / n


def _ink_bbox(img: Image.Image) -> tuple[int, int, int, int] | None:
    return img.split()[-1].getbbox()


# ---------- sentinel resolution (unit) ----------

def test_schema_default_transform_is_unset():
    c = TextClip(text="x", start=0, end=1)  # transform=(540, 1700)
    assert resolve_anchor_overrides(c, "default", 1080, 1920) == (None, None)


def test_center_x_and_add_text_default_y_are_sentinels():
    # add_text without x/y stores (canvas.w/2, canvas.h*0.85) — positional
    # no-intent, must keep rendering at the role anchor exactly as today.
    c = TextClip(text="x", start=0, end=1,
                 transform=Transform(x=CW / 2, y=CH * 0.85))
    assert resolve_anchor_overrides(c, "default", CW, CH) == (None, None)


def test_role_anchor_y_is_a_sentinel():
    # add_super_text(role="hook") stores y = canvas.h*0.5 — hook's own
    # anchor. Writing your own role anchor is a semantic no-op (same
    # principle as the font sentinel's role-font rule).
    c = TextClip(text="x", start=0, end=1, role="hook",
                 transform=Transform(x=CW / 2, y=CH * 0.5))
    assert resolve_anchor_overrides(c, "hook", CW, CH) == (None, None)


def test_custom_x_y_resolve_absolute():
    c = TextClip(text="x", start=0, end=1,
                 transform=Transform(x=200, y=300))
    assert resolve_anchor_overrides(c, "default", 1080, 1920) == (200.0, 300.0)


def test_caption_role_ignores_transform_overrides():
    c = TextClip(text="x", start=0, end=1, role="caption",
                 transform=Transform(x=123, y=45))
    assert resolve_anchor_overrides(c, "caption", CW, CH) == (None, None)


def test_keyframed_x_y_stay_role_positioned():
    # Text x/y keyframes were never animated server-side; keep ignoring
    # them (sentinel) rather than silently baking the last keyframe value.
    c = TextClip(text="x", start=0, end=1)
    c.transform.x = {"keyframes": [[0.0, 10.0], [1.0, 200.0]]}
    c.transform.y = {"keyframes": [[0.0, 10.0], [1.0, 200.0]]}
    ax, ay = resolve_anchor_overrides(c, "default", CW, CH)
    assert ax is None and ay is None


def test_size_sentinel_and_override():
    assert resolve_size_override(TextClip(text="x", start=0, end=1)) is None
    c = TextClip(text="x", start=0, end=1, style=TextStyle(size=48))
    assert resolve_size_override(c) == 48.0


# ---------- PNG cache key ----------

def test_size_changes_png_cache_key(tmp_path: Path):
    """Two clips differing only in style.size must not share a cached PNG —
    without the key change, a size edit silently serves the stale PNG."""
    edl = _edl([
        TextClip(text="SAME", start=0, end=1, role="super"),
        TextClip(text="SAME", start=2, end=3, role="super",
                 style=TextStyle(size=48)),
    ])
    paired = cache_text_pngs(edl, tmp_path / "cache")
    assert len({p.name for _, _, p in paired}) == 2


def test_position_changes_png_cache_key(tmp_path: Path):
    edl = _edl([
        TextClip(text="SAME", start=0, end=1),
        TextClip(text="SAME", start=2, end=3,
                 transform=Transform(x=200, y=100)),
    ])
    paired = cache_text_pngs(edl, tmp_path / "cache")
    assert len({p.name for _, _, p in paired}) == 2


# ---------- pixels in the rendered PNG (fast, no ffmpeg) ----------

def test_custom_size_actually_changes_glyph_height():
    big = render_text_png("HI", "default", CW, CH, size=96.0)
    small = render_text_png("HI", "default", CW, CH, size=32.0)
    bb_big, bb_small = _ink_bbox(big), _ink_bbox(small)
    assert bb_big and bb_small
    h_big = bb_big[3] - bb_big[1]
    h_small = bb_small[3] - bb_small[1]
    assert h_big > h_small * 1.8, (h_big, h_small)


def test_default_size_matches_role_size():
    """size=None must render byte-identically to today's role-sized text."""
    dflt = render_text_png("HI", "super", CW, CH)
    explicit_role = render_text_png("HI", "super", CW, CH,
                                    size=float(ROLE_STYLES["super"]["size"]))
    assert dflt.tobytes() == explicit_role.tobytes()


def test_custom_anchor_moves_ink_near_target():
    img = render_text_png("HI", "default", 640, 360, size=40.0,
                          anchor_x=200.0, anchor_y=300.0)
    cen = _ink_centroid(img)
    assert cen is not None
    # Shadow offsets pull the centroid a few px right/down of the anchor.
    assert abs(cen[0] - 200) < 30, cen
    assert abs(cen[1] - 300) < 30, cen


def test_default_anchor_unmoved_and_distinct_from_custom():
    """The default-positioned clip stays where it always rendered (role
    anchor: centered-x, 0.75h), and does NOT overlap a custom-positioned
    clip parked elsewhere."""
    default_img = render_text_png("HI", "default", 640, 360)
    baseline = render_text_png("HI", "default", 640, 360,
                               anchor_x=None, anchor_y=None)
    assert default_img.tobytes() == baseline.tobytes()
    cen_d = _ink_centroid(default_img)
    assert cen_d is not None
    assert abs(cen_d[0] - 320) < 30      # centered
    assert abs(cen_d[1] - 360 * 0.75) < 30

    custom = render_text_png("HI", "default", 640, 360,
                             anchor_x=120.0, anchor_y=60.0)
    cen_c = _ink_centroid(custom)
    assert cen_c is not None
    # Positions genuinely differ (no overlap of the two anchors).
    assert abs(cen_c[0] - cen_d[0]) > 80
    assert abs(cen_c[1] - cen_d[1]) > 80


def test_caption_pixels_pinned_to_caption_anchor(tmp_path: Path):
    """Caption positioning is owned by the captions block — a stray custom
    transform must not move caption pixels: the cached PNG for a caption
    with a custom transform is byte-identical to a default-transform one."""
    moved_edl = _edl([TextClip(text="hello", start=0, end=1, role="caption",
                               transform=Transform(x=50, y=40))],
                     w=640, h=360)
    plain_edl = _edl([TextClip(text="hello", start=0, end=1, role="caption")],
                     w=640, h=360)
    (_, _, p_moved), = cache_text_pngs(moved_edl, tmp_path / "c1")
    (_, _, p_plain), = cache_text_pngs(plain_edl, tmp_path / "c2")
    assert p_moved.read_bytes() == p_plain.read_bytes()


# ---------- emitted graph (animated path agrees with static) ----------

def test_anim_path_y_compensation_uses_custom_anchor(tmp_path: Path):
    edl = _edl([TextClip(text="HI", start=0, end=2, anim_in="fade",
                         transform=Transform(x=CW / 2, y=40.0))])
    chain, _, _ = build_overlay_chain(
        edl, tmp_path / "cache", source_label="[v]", out_label="[vout]",
        first_input_index=1, out_w=CW, out_h=CH)
    # cy = 40 * (out_h / canvas.h) = 40 → the pop/y compensation term.
    assert "40.00*(1-overlay_h/main_h)" in chain, chain


def test_anim_path_x_compensation_uses_custom_anchor(tmp_path: Path):
    edl = _edl([TextClip(text="HI", start=0, end=2, anim_in="pop",
                         transform=Transform(x=80.0, y=CH * 0.85))])
    chain, _, _ = build_overlay_chain(
        edl, tmp_path / "cache", source_label="[v]", out_label="[vout]",
        first_input_index=1, out_w=CW, out_h=CH)
    assert "80.00*(1-overlay_w/main_w)" in chain, chain


def test_anim_path_default_keeps_centered_x(tmp_path: Path):
    edl = _edl([TextClip(text="HI", start=0, end=2, anim_in="fade",
                         transform=Transform(x=CW / 2, y=CH * 0.85))])
    chain, _, _ = build_overlay_chain(
        edl, tmp_path / "cache", source_label="[v]", out_label="[vout]",
        first_input_index=1, out_w=CW, out_h=CH)
    assert "overlay=x='(main_w-overlay_w)/2'" in chain, chain


# ---------- pixels in a real export render ----------

@pytest.fixture
def store(tmp_path: Path) -> EDLStore:
    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", "color=c=0x202020:s=320x240:d=4:r=30",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
         "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", str(src)],
        check=True)
    s = EDLStore(tmp_path / "sess")
    s.edl.canvas = Canvas(w=320, h=240, fps=30)
    s.edl.get_track("v1").clips.append(Clip(src=str(src), in_=0, out=4, start=0))
    s.edl.recompute_duration()
    return s


def test_transform_and_size_reach_export_pixels(store: EDLStore, tmp_path: Path):
    """Export render (the pipeline that bakes text): a red custom-positioned
    clip lands near its (x, y) scaled to output; a green default clip stays
    at the role anchor; the two clusters do not coincide.

    NOTE: render_preview deliberately does NOT bake text (issue 40 — the
    browser's TextLayer draws it live), so the pixel proof runs through
    render_export, same as test_text_anim_style's pixel test.
    """
    import numpy as np
    from video_ai_editor.render.compositor import render_export

    tr = store.edl.get_track("text") if store.edl.get_track("text") else None
    if tr is None:
        from video_ai_editor.agent.dispatch import ensure_track
        tr = ensure_track(store.edl, "text", "text", z=10)
    # Custom: red, size 28, anchored at canvas (80, 60).
    tr.clips.append(TextClip(text="XX", start=0, end=4,
                             style=TextStyle(color="#FF2020", size=28),
                             transform=Transform(x=80, y=60)))
    # Default-positioned: green, untouched transform/size.
    tr.clips.append(TextClip(text="XX", start=0, end=4,
                             style=TextStyle(color="#20FF20")))

    res = render_export(store.edl, store.dir, height=240)
    frame = tmp_path / "f.png"
    subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y",
                    "-ss", "1.0", "-i", str(res.path), "-frames:v", "1",
                    str(frame)], check=True)
    a = np.asarray(Image.open(frame).convert("RGB")).astype(int)
    oh, ow = a.shape[:2]
    sx, sy = ow / 320, oh / 240

    red = (a[:, :, 0] > 120) & (a[:, :, 0] > a[:, :, 1] + 60) & (a[:, :, 0] > a[:, :, 2] + 60)
    green = (a[:, :, 1] > 120) & (a[:, :, 1] > a[:, :, 0] + 60) & (a[:, :, 1] > a[:, :, 2] + 60)
    assert red.sum() > 20, "custom clip never reached pixels"
    assert green.sum() > 20, "default clip never reached pixels"

    ys, xs = np.nonzero(red)
    red_c = (xs.mean(), ys.mean())
    ys, xs = np.nonzero(green)
    green_c = (xs.mean(), ys.mean())

    # Custom cluster near (80, 60) scaled to output.
    assert abs(red_c[0] - 80 * sx) < 30 * sx, red_c
    assert abs(red_c[1] - 60 * sy) < 30 * sy, red_c
    # Default cluster at the role anchor (centered x, 0.75h).
    assert abs(green_c[0] - 160 * sx) < 30 * sx, green_c
    assert abs(green_c[1] - 240 * 0.75 * sy) < 30 * sy, green_c
    # And the two positions genuinely differ.
    assert abs(red_c[0] - green_c[0]) > 20 or abs(red_c[1] - green_c[1]) > 20
