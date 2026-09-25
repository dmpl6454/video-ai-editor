"""QA-003: exported Devanagari must be spelled right and Arabic joined, RTL.

The export rasteriser drew with Pillow's BASIC layout (no libraqm/fribidi in
the wheel): one glyph per codepoint in logical order. 'नमस्ते' exported with a
visible halant, 'दुनिया' as 'दुनयिा', and Arabic as isolated letters left to
right, ~30% wider. Text now goes through HarfBuzz + FreeType + python-bidi
(render/shaping.py) whenever it needs shaping.

GOLDEN RASTERS. `tests/fixtures/shaping_golden/*.png` were rendered by
HarfBuzz's own reference tool, independent of this code:
    hb-view --font-size=100 --variations=wght=700 --margin=16 \
            --background=#FFFFFF --foreground=#000000 -O png \
            --text-file=<utf-8 text> fonts/<NotoSans…-VF.ttf>
(hb-view 14.2.0, cairo rasteriser). Each test compares the ink of OUR render
with the golden ink: same box size within a few px, and IoU of the two masks.
The unshaped Pillow layout scores IoU 0.49 (Devanagari) / 0.26 (Arabic)
against the same goldens; a correct shaper scores > 0.9.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from video_ai_editor.config import FONTS_DIR
from video_ai_editor.edl.schema import TextClip, TextStyle, Transform
from video_ai_editor.render import render_export, shaping
from video_ai_editor.render.text_overlay import _tokenize_emoji, render_text_png

sys.path.insert(0, str(Path(__file__).parent))
from overlay_render_helpers import base_edl, frame_at, mask_of  # noqa: E402

GOLD = Path(__file__).parent / "fixtures" / "shaping_golden"
CASES = [
    ("नमस्ते दुनिया", "hi_namaste_duniya.png"),     # conjunct स्त + pre-base i-matra
    ("مرحبا بالعالم", "ar_marhaban_bilalam.png"),  # joined, right-to-left
]


def _crop(mask: np.ndarray) -> np.ndarray:
    ys, xs = np.nonzero(mask)
    assert len(xs), "no ink"
    return mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def _golden(name: str) -> np.ndarray:
    return _crop(np.asarray(Image.open(GOLD / name).convert("L")) < 128)


def _iou_vs_golden(ours: np.ndarray, gold: np.ndarray) -> float:
    g = np.asarray(Image.fromarray(gold.astype(np.uint8) * 255)
                   .resize((ours.shape[1], ours.shape[0]), Image.BILINEAR)) > 127
    return float((ours & g).sum() / (ours | g).sum())


@pytest.mark.parametrize("text,golden", CASES)
def test_rasteriser_matches_the_harfbuzz_golden(text: str, golden: str):
    img = render_text_png(text, "watermark", 1920, 1080, fill=(0, 0, 0, 255),
                          size=100.0, stroke_w=0.0, anchor_y=540.0)
    ours = _crop(np.asarray(img.getchannel("A")) > 100)
    gold = _golden(golden)
    # Same footprint: the unshaped Arabic was 701 px wide against 507.
    assert abs(ours.shape[1] - gold.shape[1]) <= 4, (ours.shape, gold.shape)
    assert abs(ours.shape[0] - gold.shape[0]) <= 4, (ours.shape, gold.shape)
    assert _iou_vs_golden(ours, gold) > 0.88


@pytest.mark.parametrize("text,golden", CASES)
def test_exported_frame_matches_the_harfbuzz_golden(tmp_path: Path, text: str, golden: str):
    # 4 s, not 2: a digitally SILENT timeline shorter than loudnorm's 3 s
    # window makes the export's loudnorm emit NaN and the AAC encoder abort —
    # a separate, pre-existing export defect this test is not about.
    edl = base_edl(tmp_path, 1920, 1080, 4.0, color="white")
    edl.get_track("tx").clips.append(TextClip(
        id="t1", text=text, start=0.0, end=4.0, role="label",
        style=TextStyle(color="#FF0000", size=100, stroke_w=0),
        transform=Transform(x=960, y=540)))
    edl.recompute_duration()
    out = render_export(edl, tmp_path, height=1080).path
    ours = _crop(mask_of(frame_at(out, 1.0, 30), (255, 0, 0), 150))
    gold = _golden(golden)
    assert abs(ours.shape[1] - gold.shape[1]) <= 6, (ours.shape, gold.shape)
    assert _iou_vs_golden(ours, gold) > 0.8


def test_mixed_direction_line_orders_runs_visually():
    f = shaping.ShapedFont(FONTS_DIR / "NotoSansArabic-VF.ttf", 64, 700)
    runs = f.runs("مرحبا 2026", _tokenize_emoji, 64)
    # RTL paragraph: the number sits to the LEFT of the Arabic word.
    assert [r.text.strip() for r in runs if r.text.strip()] == ["2026", "مرحبا"]


def test_devanagari_is_shaped_not_one_glyph_per_codepoint():
    f = shaping.ShapedFont(FONTS_DIR / "NotoSansDevanagari-VF.ttf", 64)
    (run,) = f.runs("नमस्ते", _tokenize_emoji, 64)
    # 6 codepoints; the स्त conjunct and the e-matra fold into fewer glyphs,
    # and no standalone virama (halant) glyph survives.
    assert len(run.glyphs) < 6
    virama = f.face.get_char_index("्")
    assert virama not in {g[0] for g in run.glyphs}


def test_latin_text_never_touches_the_shaper():
    assert not shaping.needs_shaping("HELLO world 123 🔥")
    assert shaping.needs_shaping("नमस्ते") and shaping.needs_shaping("مرحبا")


def test_missing_shaper_fails_loudly_instead_of_misspelling(monkeypatch):
    monkeypatch.setattr(shaping, "_IMPORT_ERROR", ImportError("uharfbuzz"))
    with pytest.raises(shaping.ShapingUnavailable, match="misspelled"):
        render_text_png("नमस्ते", "caption", 1080, 1920)
    # ...while Latin text still renders without it.
    assert render_text_png("HELLO", "caption", 1080, 1920).getbbox() is not None
