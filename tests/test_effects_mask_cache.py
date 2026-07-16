"""render/effects.py — mask PNG cache validity + atomic writes.

Same class of bug as text_overlay.py's overlay cache (see
test_text_overlay_cache.py and R2 in docs/superpowers/plans/
2026-07-10-editor-issues-verification-and-fixes.md): compositor.py and
chunks.py both cache a per-clip mask PNG keyed by content hash and used to
guard reuse with a bare `exists()` check. A torn/0-byte mask file would be
fed to ffmpeg's alphamerge as `-i` and fail the same way a corrupt sticker
PNG does.
"""
from __future__ import annotations
from pathlib import Path

from video_ai_editor.edl.schema import Mask
from video_ai_editor.render.effects import render_mask_png, mask_png_is_valid


def test_mask_png_is_valid_rejects_zero_byte_file(tmp_path: Path):
    p = tmp_path / "mask.png"
    p.write_bytes(b"")
    assert mask_png_is_valid(p) is False


def test_mask_png_is_valid_rejects_missing_file(tmp_path: Path):
    assert mask_png_is_valid(tmp_path / "nope.png") is False


def test_render_mask_png_produces_a_valid_file(tmp_path: Path):
    mask = Mask(type="circle", feather=4.0, position=(160.0, 90.0))
    dst = tmp_path / "mask.png"
    out = render_mask_png(mask, 320, 180, dst)
    assert out == dst
    assert mask_png_is_valid(dst)


def test_render_mask_png_overwrites_a_torn_cache_file(tmp_path: Path):
    mask = Mask(type="rectangle", feather=0.0, position=(160.0, 90.0))
    dst = tmp_path / "mask.png"
    render_mask_png(mask, 320, 180, dst)
    good_bytes = dst.read_bytes()
    assert len(good_bytes) > 0

    dst.write_bytes(good_bytes[:5])  # simulate a killed render
    assert not mask_png_is_valid(dst)

    render_mask_png(mask, 320, 180, dst)  # caller re-renders on an invalid guard
    assert mask_png_is_valid(dst)
    assert dst.stat().st_size == len(good_bytes)


def test_render_mask_png_leaves_no_temp_file_behind(tmp_path: Path):
    mask = Mask(type="linear", feather=8.0, position=(160.0, 0.0))
    dst = tmp_path / "mask.png"
    render_mask_png(mask, 320, 180, dst)
    leftovers = list(tmp_path.glob(".*.tmp"))
    assert leftovers == [], f"temp file(s) left behind: {leftovers}"
