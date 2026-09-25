"""QA-015: preview and export share ONE text layout model.

Text landed 20-50 px apart between the browser preview (TextLayer) and the
export (render_text_png), and the export outline was ~2x heavier: the export
drew (size+8)-px line boxes from Pillow's ascender top with a full-width
outward stroke, the preview centred an em box with a half-visible canvas
stroke, and the two used different role anchors.

`frontend/src/lib/__fixtures__/text_layout_cases.json` is the contract; this
file pins the export side to it (constants, role table, anchors, line
centres) AND measures real rasterised pixels against the model. The frontend
side is pinned to the same fixture by `lib/textLayout.test.ts`.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from video_ai_editor.render import text_overlay as T

FIXTURE = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "lib"
           / "__fixtures__" / "text_layout_cases.json")
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_constants_match_the_contract():
    assert T.LINE_HEIGHT_RATIO == CASES["LINE_HEIGHT_RATIO"]
    assert list(T.SHADOW_OFFSET) == CASES["SHADOW_OFFSET"]
    assert T.SHADOW_ALPHA == CASES["SHADOW_ALPHA"]
    assert T.WRAP_WIDTH_RATIO == CASES["WRAP_WIDTH_RATIO"]
    assert T.EMOJI_BOX_RATIO == CASES["EMOJI_BOX_RATIO"]
    assert T.EMOJI_INK_RATIO == CASES["EMOJI_INK_RATIO"]


@pytest.mark.parametrize("role", sorted(CASES["roles"]))
def test_role_table_matches_the_contract(role: str):
    want, got = CASES["roles"][role], T.ROLE_STYLES[role]
    assert got["size"] == want["size"] and got["stroke_w"] == want["stroke_w"]
    assert list(got["fill"]) == want["fill"] and list(got["stroke"]) == want["stroke"]
    assert bool(got["shadow"]) == want["shadow"]
    assert bool(got.get("upper", False)) == want["upper"]
    assert T.SCRIPT_FONT_WEIGHT[role] == want["script_weight"]


def test_role_anchors_and_line_centres_match_the_contract():
    for a in CASES["anchors"]:
        assert T._y_for_role(a["role"], None, a["h"], a["w"]) == pytest.approx(a["y"])
    for ln in CASES["lines"]:
        assert T.line_centers(ln["anchor_y"], ln["n"], ln["size"]) == pytest.approx(ln["centers"])


def _alpha(img) -> np.ndarray:
    return np.asarray(img.getchannel("A"))


def _rows(mask: np.ndarray) -> tuple[int, int]:
    ys = np.nonzero(mask.any(axis=1))[0]
    return int(ys.min()), int(ys.max())


def test_cap_band_is_centred_on_the_anchor():
    """Rule 3: the 'H' ink (cap band) is centred on the line centre. The old
    ascender-top layout put it 18 px low at size 120 (the QA measurement)."""
    img = T.render_text_png("HHH", "watermark", 1920, 1080, fill=(255, 0, 0, 255),
                            size=120.0, stroke_w=0.0, anchor_y=540.0)
    top, bottom = _rows(_alpha(img) > 128)
    assert abs((top + bottom) / 2 - 540) <= 1.0, (top, bottom)


def test_multi_line_blocks_stack_at_the_shared_line_height():
    img = T.render_text_png("HH\nHH\nHH", "watermark", 1920, 1080,
                            fill=(255, 0, 0, 255), size=100.0, stroke_w=0.0,
                            anchor_y=540.0)
    a = _alpha(img) > 128
    rows = np.nonzero(a.any(axis=1))[0]
    # Split the ink into its three line bands and take each band's middle.
    gaps = np.nonzero(np.diff(rows) > 1)[0]
    bands = np.split(rows, gaps + 1)
    mids = [(b.min() + b.max()) / 2 for b in bands]
    assert mids == pytest.approx(T.line_centers(540, 3, 100), abs=1.0)


def test_outline_extends_stroke_w_outside_the_glyph():
    """Rule 4: `stroke_w` px of outline OUTSIDE the glyph edge — the preview
    strokes at 2 x stroke_w under the fill to show the same band."""
    fill = T.render_text_png("H", "watermark", 1920, 1080, fill=(255, 0, 0, 255),
                             size=200.0, stroke_w=0.0, anchor_y=540.0)
    stroked = T.render_text_png("H", "watermark", 1920, 1080, fill=(255, 0, 0, 255),
                                size=200.0, stroke_w=10.0, anchor_y=540.0)
    f_cols = np.nonzero((_alpha(fill) > 128).any(axis=0))[0]
    s_cols = np.nonzero((_alpha(stroked) > 128).any(axis=0))[0]
    assert (f_cols.min() - s_cols.min()) == pytest.approx(10, abs=1)
    assert (s_cols.max() - f_cols.max()) == pytest.approx(10, abs=1)


def test_role_anchor_is_where_the_role_draws():
    """Rule 1: a role-positioned clip centres on `_y_for_role` — the anchor
    the preview now uses too (it had its own table: label at the TOP)."""
    img = T.render_text_png("HH", "label", 1920, 1080, fill=(255, 0, 0, 255), stroke_w=0.0)
    top, bottom = _rows(_alpha(img) > 200)   # fill only: the shadow is alpha 140
    assert abs((top + bottom) / 2 - T._y_for_role("label", None, 1080, 1920)) <= 1.0
