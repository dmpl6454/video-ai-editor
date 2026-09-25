"""Wave-B review: mixed-script text shapes per script run, with a per-run face.

The export picked ONE face per clip by dominant script and split runs only by
bidi level, shaping each with `guess_segment_properties()` (script from the
first strong character). So a Latin-dominant clip drew its Devanagari line in
Inter (.notdef boxes), and "Hello नमस्ते दुनिया" was shaped as Latin ('दुनयिा',
the i-matra after its consonant). The browser preview falls back per
character, so it showed the right text.
"""
from __future__ import annotations

import numpy as np
import pytest

from video_ai_editor.config import FONTS_DIR
from video_ai_editor.render import shaping
from video_ai_editor.render.text_overlay import render_text_png

pytestmark = pytest.mark.skipif(not shaping.available(), reason="shaper not installed")

DEVA = FONTS_DIR / "NotoSansDevanagari-VF.ttf"


def _tok(s):
    return [("text", s)] if s else []


def _gids(runs, script=None):
    return [g[0] for r in runs if r.kind == "text" and (script is None or r.script == script)
            for g in r.glyphs]


def _fallback(size):
    def get(script):
        return shaping.ShapedFont(DEVA, size, 700) if script == "Deva" else None
    return get


def test_devanagari_after_a_latin_word_is_shaped_as_devanagari():
    """'Hello दुनिया': the Devanagari run must carry the glyphs a Devanagari-
    only shaping produces (ि reordered BEFORE न), not a Latin shaping of it."""
    ref = shaping.ShapedFont(DEVA, 64, 700)
    want = _gids(ref.runs("दुनिया", _tok, 10))
    f = shaping.ShapedFont(DEVA, 64, 700)          # Devanagari-dominant clip
    got = _gids(f.runs("Hello दुनिया", _tok, 10), "Deva")
    assert got == want
    # …which a Latin shaping of the same characters does NOT produce (the bug:
    # the matra stays after its consonant).
    bad = [g[0] for g in ref._shape("दुनिया", False, "Latn")[0]]
    assert bad != want


def test_latin_dominant_clip_falls_back_for_its_devanagari_line():
    inter = FONTS_DIR / "Inter-Bold.ttf"
    f = shaping.ShapedFont(inter, 64, None, fallback=_fallback(64))
    runs = f.runs("नमस्ते दुनिया", _tok, 10)
    assert 0 not in _gids(runs), "a .notdef glyph was drawn"
    latin = f.runs("left aligned line", _tok, 10)
    assert all(r.font is None for r in latin), "Latin must keep the clip's own face"


def _ink_rows(img):
    a = np.asarray(img.getchannel("A")) > 0
    return a


def test_export_png_of_a_latin_dominant_clip_draws_the_devanagari_line_like_the_devanagari_render():
    """Pixel check on the exported text PNG: the Devanagari line of a Latin-
    dominant two-line caption matches the same line rendered on its own (as a
    Devanagari clip, Noto at the role weight) — no boxes, same shaping."""
    two = render_text_png("नमस्ते दुनिया\nleft aligned line", "caption", 1080, 1920, shadow=False)
    one = render_text_png("नमस्ते दुनिया", "caption", 1080, 1920, shadow=False)
    a2, a1 = _ink_rows(two), _ink_rows(one)
    ys, xs = np.nonzero(a1)
    h = ys.max() - ys.min() + 1
    w = xs.max() - xs.min() + 1
    ys2, xs2 = np.nonzero(a2)
    top = ys2.min()
    # the first line's ink: same columns as the single-line render
    crop2 = a2[top:top + h, xs.min():xs.min() + w]
    crop1 = a1[ys.min():ys.min() + h, xs.min():xs.min() + w]
    assert crop2.shape == crop1.shape
    assert (crop2 != crop1).mean() < 0.01, (crop2 != crop1).mean()
