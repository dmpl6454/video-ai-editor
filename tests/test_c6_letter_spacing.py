"""QA-078 (wave C remainder): letter spacing, end to end.

Letter spacing needs per-glyph advances in BOTH renderers, and naive tracking
breaks Devanagari / Arabic joining. Rule 8 of the shared text layout model:
`style.letter_spacing` canvas px (× the transform scale) after every grapheme
of a simple-script run and every emoji, BETWEEN units only; a complex-script
run is never spaced. The fixture's `block.letter_spacing` pins the arithmetic
and the unit split for this side and for lib/textLayout.ts; the renders below
measure real ink.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import TextStyle
from video_ai_editor.render import shaping
from video_ai_editor.render import text_overlay as T

FIXTURE = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "lib"
           / "__fixtures__" / "text_layout_cases.json")
LS = json.loads(FIXTURE.read_text(encoding="utf-8"))["block"]["letter_spacing"]


def test_rule_8_arithmetic_and_units_match_the_contract():
    assert list(T.LETTER_SPACING_RANGE) == LS["RANGE"]
    for c in LS["width"]:
        assert T.tracked_width(c["advances"], c["tracked"], c["spacing"]) == pytest.approx(c["width"])
    for c in LS["units"]:
        assert [[u, t] for u, t in T._simple_clusters(c["chunk"])] == c["units"], c["chunk"]


def _ink(img) -> tuple[int, int]:
    a = np.asarray(img.getchannel("A")) > 128
    xs = np.nonzero(a.any(axis=0))[0]
    return int(xs.min()), int(xs.max())


def _render(text: str, spacing: float, **kw):
    return T.render_text_png(text, "watermark", 1920, 1080, fill=(255, 0, 0, 255), size=100.0,
                             stroke_w=0.0, anchor_y=540.0, letter_spacing=spacing, **kw)


def _cluster_advances(text: str) -> float:
    """Sum of each grapheme shaped ALONE at the render's font and size."""
    font = shaping.ShapedFont(T._font_path(T.ROLE_STYLES["watermark"]["font"]), 100)
    return sum(font._shape(cl, False, "Latn")[1] for cl in shaping.grapheme_clusters(text))


@pytest.mark.skipif(not shaping.available(), reason="HarfBuzz shaper not installed")
def test_latin_grows_by_exactly_the_gaps_times_the_spacing():
    text = "HOLIDAY"
    x0, x1 = _ink(_render(text, 0.0))
    s0, s1 = _ink(_render(text, 20.0))
    # Units are shaped alone, so the tracked line is (sum of lone advances)
    # + 6 gaps x 20, and it stays centred on the anchor.
    loose = _cluster_advances(text) + 6 * 20.0
    whole = shaping.ShapedFont(T._font_path(T.ROLE_STYLES["watermark"]["font"]), 100).width(
        text, T._tokenize_emoji, 100)
    assert (s1 - s0) - (x1 - x0) == pytest.approx(loose - whole, abs=2.0)
    assert (s0 + s1) / 2 == pytest.approx(960, abs=1.5)
    t0, t1 = _ink(_render(text, -8.0))
    assert (t1 - t0) - (x1 - x0) == pytest.approx(_cluster_advances(text) - 6 * 8.0 - whole, abs=2.0)


@pytest.mark.skipif(not shaping.available(), reason="HarfBuzz shaper not installed")
def test_devanagari_is_never_pulled_apart():
    a = np.asarray(_render("नमस्ते दुनिया", 0.0).getchannel("A"))
    b = np.asarray(_render("नमस्ते दुनिया", 30.0).getchannel("A"))
    assert np.array_equal(a, b), "a complex-script run must render identically at any spacing"


@pytest.mark.skipif(not shaping.available(), reason="HarfBuzz shaper not installed")
def test_mixed_line_spaces_only_its_latin_and_emoji():
    base = _ink(_render("Hi 👋 नमस्ते AV", 0.0))
    spaced = _ink(_render("Hi 👋 नमस्ते AV", 10.0))
    grow = (spaced[1] - spaced[0]) - (base[1] - base[0])
    # Tracked units H, i, ' ', 👋, A are followed by a gap (V is last; the
    # Devanagari run carries none): 5 x 10 px, give or take lost kerning.
    assert 45 <= grow <= 60, grow


def test_transform_scale_multiplies_the_spacing():
    a = _ink(_render("HHHH", 10.0, scale=1.0))
    b = _ink(_render("HHHH", 10.0, scale=2.0))
    assert (b[1] - b[0]) == pytest.approx(2 * (a[1] - a[0]), abs=4)


def test_schema_clamps_and_add_text_takes_it(tmp_path):
    assert TextStyle(letter_spacing=500).letter_spacing == 100.0
    assert TextStyle(letter_spacing=-99).letter_spacing == -20.0
    s = EDLStore(tmp_path / "s")
    tid = dispatch(s, "add_text", {"text": "TRACKED", "start": 0, "end": 2, "letter_spacing": 12})["id"]
    clip = s.edl.get_clip(tid)[1]
    assert clip.style.letter_spacing == 12.0
    dispatch(s, "set_property", {"clip_id": tid, "path": "style.letter_spacing", "value": 4})
    assert s.edl.get_clip(tid)[1].style.letter_spacing == 4.0
    blk = T.resolve_block_overrides(s.edl.get_clip(tid)[1])
    assert blk["letter_spacing"] == 4.0 and "lsp4.000" in T.block_key(blk)
    blk0 = {**blk, "letter_spacing": 0.0}
    assert "lsp" not in T.block_key(blk0)        # unspaced clips keep their cached PNGs


def test_letter_spacing_reaches_the_exported_frame(tmp_path):
    """Through the real text PNG cache the compositor overlays."""
    s = EDLStore(tmp_path / "s")
    t0 = dispatch(s, "add_text", {"text": "SPACED", "start": 0, "end": 2})["id"]
    t1 = dispatch(s, "add_text", {"text": "SPACED", "start": 0, "end": 2, "letter_spacing": 24,
                                  "allow_stack": True})["id"]
    pngs = {c.id: p for c, _role, p in T.cache_text_pngs(s.edl, tmp_path / "cache")}
    from PIL import Image
    w0 = _ink(Image.open(pngs[t0]))
    w1 = _ink(Image.open(pngs[t1]))
    assert (w1[1] - w1[0]) - (w0[1] - w0[0]) >= 5 * 24 - 10
