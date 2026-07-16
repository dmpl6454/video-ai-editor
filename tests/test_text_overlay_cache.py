"""render/text_overlay.py — overlay PNG cache validity + atomic writes.

Regression coverage for a real production bug: a torn/0-byte cached overlay
PNG (from a killed render or a race between two renders computing the same
content-hash path) used to be silently reused via a bare `exists()` check,
then handed to ffmpeg as `-i`, which fails with "Invalid data found when
processing input" and aborts the whole preview render. That failure then
surfaced to users as a "music upload failed" / "upload failed" toast even
though their media was fine — see docs/superpowers/plans/
2026-07-10-editor-issues-verification-and-fixes.md (R2).
"""
from __future__ import annotations
from pathlib import Path

from PIL import Image

from video_ai_editor.edl.schema import EDL, Canvas, Track, TextClip, Sticker, Transform
from video_ai_editor.render.text_overlay import (
    cache_text_pngs,
    cache_sticker_pngs,
    cache_animated_sticker_pngs,
    build_overlay_chain,
    _png_is_valid,
    _save_png_atomic,
)


def _edl_with_text(canvas_w=320, canvas_h=180) -> EDL:
    return EDL(
        canvas=Canvas(w=canvas_w, h=canvas_h, fps=30),
        tracks=[
            Track(id="v1", type="video", clips=[]),
            Track(id="text", type="text", z=10, clips=[
                TextClip(id="t1", text="HELLO", start=0.0, end=2.0,
                         transform=Transform(x=160, y=40), role="super"),
            ]),
        ],
    )


def _edl_with_sticker(sticker_src: Path, canvas_w=320, canvas_h=180,
                       keyframed_x: bool = False) -> EDL:
    tx = Transform(x=160, y=90)
    if keyframed_x:
        tx.x = {"keyframes": [[0.0, 50.0], [2.0, 250.0]]}
    return EDL(
        canvas=Canvas(w=canvas_w, h=canvas_h, fps=30),
        tracks=[
            Track(id="v1", type="video", clips=[]),
            Track(id="stickers", type="sticker", z=11, clips=[
                Sticker(id="s1", src=str(sticker_src), start=0.0, end=2.0, transform=tx),
            ]),
        ],
    )


def _make_sticker_png(path: Path) -> Path:
    Image.new("RGBA", (64, 64), (255, 0, 0, 255)).save(path)
    return path


# ---------- _png_is_valid ----------

def test_png_is_valid_rejects_missing_file(tmp_path: Path):
    assert _png_is_valid(tmp_path / "nope.png") is False


def test_png_is_valid_rejects_zero_byte_file(tmp_path: Path):
    p = tmp_path / "torn.png"
    p.write_bytes(b"")
    assert _png_is_valid(p) is False


def test_png_is_valid_rejects_truncated_file(tmp_path: Path):
    # A real PNG header/IHDR but cut off before the data — simulates a render
    # killed mid-write, which is exactly what leaves a "torn" cache file.
    real = tmp_path / "real.png"
    Image.new("RGBA", (64, 64), (0, 255, 0, 255)).save(real)
    truncated = tmp_path / "truncated.png"
    truncated.write_bytes(real.read_bytes()[:40])
    assert _png_is_valid(truncated) is False


def test_png_is_valid_accepts_a_real_png(tmp_path: Path):
    p = tmp_path / "good.png"
    Image.new("RGBA", (32, 32), (0, 0, 255, 255)).save(p)
    assert _png_is_valid(p) is True


# ---------- _save_png_atomic ----------

def test_save_png_atomic_leaves_no_temp_file_behind(tmp_path: Path):
    dst = tmp_path / "out.png"
    img = Image.new("RGBA", (16, 16), (1, 2, 3, 255))
    _save_png_atomic(img, dst)
    assert dst.exists()
    assert _png_is_valid(dst)
    leftovers = list(tmp_path.glob(".*.tmp"))
    assert leftovers == [], f"temp file(s) left behind: {leftovers}"


# ---------- cache_text_pngs self-heals a torn cache entry ----------

def test_cache_text_pngs_regenerates_a_zero_byte_cache_file(tmp_path: Path):
    edl = _edl_with_text()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    # First pass: populate the cache normally.
    paired = cache_text_pngs(edl, cache_dir)
    assert len(paired) == 1
    _, _, png_path = paired[0]
    assert _png_is_valid(png_path)

    # Simulate a killed render: truncate the cached PNG to 0 bytes, exactly
    # like an interrupted `img.save()` used to leave behind pre-fix.
    png_path.write_bytes(b"")
    assert png_path.exists() and png_path.stat().st_size == 0

    # Re-running the cache pass must detect the torn file and rebuild it,
    # not hand ffmpeg a 0-byte PNG.
    paired2 = cache_text_pngs(edl, cache_dir)
    _, _, png_path2 = paired2[0]
    assert png_path2 == png_path  # same content-hash path
    assert _png_is_valid(png_path2)
    assert png_path2.stat().st_size > 0


def test_cache_sticker_pngs_regenerates_a_corrupt_cache_file(tmp_path: Path):
    sticker_src = _make_sticker_png(tmp_path / "sticker.png")
    edl = _edl_with_sticker(sticker_src)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    out = cache_sticker_pngs(edl, cache_dir)
    assert len(out) == 1
    _, dst = out[0]
    assert _png_is_valid(dst)
    good_bytes = dst.read_bytes()
    assert len(good_bytes) > 0

    # Truncate to simulate the exact ground-truth bug: a torn st_*.png that
    # ffmpeg -i then rejects with "Invalid data found when processing input".
    dst.write_bytes(good_bytes[:10])
    assert not _png_is_valid(dst)

    out2 = cache_sticker_pngs(edl, cache_dir)
    _, dst2 = out2[0]
    assert dst2 == dst
    assert _png_is_valid(dst2)


def test_cache_animated_sticker_pngs_regenerates_a_corrupt_cache_file(tmp_path: Path):
    sticker_src = _make_sticker_png(tmp_path / "sticker.png")
    edl = _edl_with_sticker(sticker_src, keyframed_x=True)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    out = cache_animated_sticker_pngs(edl, cache_dir)
    assert len(out) == 1
    _, dst, size = out[0]
    assert _png_is_valid(dst)
    assert size[0] > 0 and size[1] > 0

    dst.write_bytes(b"")  # torn cache file
    out2 = cache_animated_sticker_pngs(edl, cache_dir)
    assert len(out2) == 1  # must still find/rebuild it, not silently drop the sticker
    _, dst2, size2 = out2[0]
    assert dst2 == dst
    assert _png_is_valid(dst2)
    assert size2 == size


def test_cache_text_pngs_does_not_rewrite_a_valid_cached_file(tmp_path: Path):
    """A valid cache hit should be left untouched (no gratuitous re-render)."""
    edl = _edl_with_text()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    paired = cache_text_pngs(edl, cache_dir)
    _, _, png_path = paired[0]
    mtime_before = png_path.stat().st_mtime_ns

    paired2 = cache_text_pngs(edl, cache_dir)
    _, _, png_path2 = paired2[0]
    assert png_path2 == png_path
    assert png_path2.stat().st_mtime_ns == mtime_before


# ---------- build_overlay_chain: preview skips TEXT, keeps STICKERS (issue 40) ----------
#
# The browser's TextLayer already draws every text/captions clip live, with no
# ffmpeg round-trip. Baking text server-side too — as the code used to do
# unconditionally — doubled it up in the PREVIEW at different sizing/position
# math (server: canvas-sized PNG scaled to output; client: on-screen box
# size), producing "big AND small captions simultaneously". Stickers have no
# client-side pixel renderer (StickerLayer only draws selection handles), so
# they must still be baked in preview — only text is preview-skipped.

def test_build_overlay_chain_skips_text_in_preview_mode(tmp_path: Path):
    edl = _edl_with_text()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    filter_str, extra_inputs, _ = build_overlay_chain(
        edl, cache_dir, source_label="[v]", out_label="[vout]",
        first_input_index=5, out_w=320, out_h=180, preview=True,
    )
    assert filter_str == ""
    assert extra_inputs == []


def test_build_overlay_chain_bakes_text_when_not_preview(tmp_path: Path):
    edl = _edl_with_text()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    filter_str, extra_inputs, _ = build_overlay_chain(
        edl, cache_dir, source_label="[v]", out_label="[vout]",
        first_input_index=5, out_w=320, out_h=180, preview=False,
    )
    assert filter_str != ""
    assert len(extra_inputs) > 0


def test_build_overlay_chain_still_bakes_stickers_in_preview_mode(tmp_path: Path):
    sticker_src = tmp_path / "sticker.png"
    Image.new("RGBA", (64, 64), (255, 0, 0, 255)).save(sticker_src)
    edl = _edl_with_sticker(sticker_src)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    filter_str, extra_inputs, _ = build_overlay_chain(
        edl, cache_dir, source_label="[v]", out_label="[vout]",
        first_input_index=5, out_w=320, out_h=180, preview=True,
    )
    # Stickers are NOT text — preview must still bake them (no client-side
    # sticker pixel renderer exists), unlike the text-skip case above.
    assert filter_str != ""
    assert len(extra_inputs) > 0


def test_build_overlay_chain_preview_default_still_bakes_everything(tmp_path: Path):
    """`preview` defaults to False so existing callers that don't pass it
    (if any) keep the pre-fix baking behavior — only compositor.py's preview
    render path opts in explicitly."""
    edl = _edl_with_text()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    filter_str, _, _ = build_overlay_chain(
        edl, cache_dir, source_label="[v]", out_label="[vout]",
        first_input_index=5, out_w=320, out_h=180,
    )
    assert filter_str != ""
