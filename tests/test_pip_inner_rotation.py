"""Rotation of the picture INSIDE a PIP's shape (`Framing.rotation`).

Distinct from `Transform.rotation`, which turns the whole element — shape and
all — on the canvas. Inner rotation spins the footage while the circle stays a
circle sitting where it was, and the two compose.

The trap this pins is the CROP: rotating in place cuts corners, so unless the
covered source is grown first the rotation drags black wedges into the shape —
the same information-loss the v1 chain accepts (there the canvas IS the frame)
and a PIP must not.
"""
from __future__ import annotations
import math
import re

import pytest

from video_ai_editor.edl.schema import EDL, Clip, Framing, Track, Canvas
from video_ai_editor.render.pip import build_pip_overlay_chain


def _edl(framing: Framing | None = None, *, canvas=(1920, 1080)) -> EDL:
    e = EDL(canvas=Canvas(w=canvas[0], h=canvas[1]))
    e.tracks = [
        Track(id="v1", type="video", clips=[]),
        Track(id="v2", type="video", clips=[
            # fit='cover' is what gives the element a BOX; without one pip.py
            # keeps the source's own aspect and there is nothing to frame
            # inside (the panel says exactly this before offering the sliders).
            Clip(id="p1", src="/tmp/a.mp4", start=0.0, **{"in": 0.0}, out=2.0,
                 fit="cover", framing=framing),
        ]),
    ]
    return e


def _chain(framing: Framing | None) -> str:
    chain, _inputs, _label, _audio = build_pip_overlay_chain(
        _edl(framing), source_label="[base]", out_label="[out]",
        first_input_index=1, out_w=1920, out_h=1080)
    return chain


def _cover_size(chain: str) -> tuple[int, int]:
    m = re.search(r"scale=(\d+):(\d+):force_original_aspect_ratio=increase", chain)
    assert m, chain
    return int(m.group(1)), int(m.group(2))


def _crop_size(chain: str) -> tuple[int, int]:
    m = re.search(r"crop=(\d+):(\d+):", chain)
    assert m, chain
    return int(m.group(1)), int(m.group(2))


def test_no_rotation_emits_no_rotate_and_covers_exactly_the_box():
    chain = _chain(None)
    inner = chain.split("crop=")[0]
    assert "rotate=" not in inner, "an unrotated PIP must not pay for a rotate"
    assert _cover_size(chain) == _crop_size(chain)


def test_rotation_emits_a_rotate_before_the_crop():
    chain = _chain(Framing(rotation=30))
    inner = chain.split("crop=")[0]
    assert "rotate=" in inner, \
        "the picture must be turned BEFORE the shape cuts it, or the shape turns too"
    rad = re.search(r"rotate=([-0-9.]+):c=black@0", inner)
    assert rad and abs(float(rad.group(1)) - math.radians(30)) < 1e-4


def test_the_cover_is_grown_so_no_black_corner_reaches_the_shape():
    """A w x h window inside a rectangle rotated by t needs that rectangle to be
    at least w|cos|+h|sin| by w|sin|+h|cos|. Without the growth the crop sees
    the rotate's transparent corners."""
    plain_w, plain_h = _cover_size(_chain(None))
    for deg in (15, 30, 45, 90, -30, 175):
        chain = _chain(Framing(rotation=deg))
        cw, ch = _cover_size(chain)
        bw, bh = _crop_size(chain)
        r = math.radians(deg)
        need_w = bw * abs(math.cos(r)) + bh * abs(math.sin(r))
        need_h = bw * abs(math.sin(r)) + bh * abs(math.cos(r))
        assert cw + 1 >= need_w, f"{deg}°: cover {cw} < needed {need_w:.1f}"
        assert ch + 1 >= need_h, f"{deg}°: cover {ch} < needed {need_h:.1f}"
        assert (cw, ch) >= (plain_w, plain_h)


def test_zoom_and_rotation_compose_rather_than_replace():
    a = _cover_size(_chain(Framing(zoom=2.0)))
    b = _cover_size(_chain(Framing(zoom=2.0, rotation=45)))
    assert b[0] > a[0] and b[1] > a[1], \
        "rotating must widen the cover ON TOP of the zoom, not instead of it"


def test_cover_dimensions_stay_even():
    # Odd sizes are the chroma-parity trap the rest of the graph snaps for.
    for deg in (7, 23, 41, 67, 89):
        cw, ch = _cover_size(_chain(Framing(rotation=deg)))
        assert cw % 2 == 0 and ch % 2 == 0, f"{deg}° gave {cw}x{ch}"


def test_the_shape_mask_is_applied_after_the_inner_rotation():
    """Order is the whole point: mask AFTER means the circle stays a circle and
    only the picture turns. Mask first would rotate the shape as well."""
    chain = _chain(Framing(rotation=30))
    e = _edl(Framing(rotation=30))
    from video_ai_editor.edl.schema import Mask
    e.tracks[1].clips[0].mask = Mask(type="circle")
    chain, _i, _l, _a = build_pip_overlay_chain(
        e, source_label="[base]", out_label="[out]", first_input_index=1,
        out_w=1920, out_h=1080)
    assert "geq=" in chain
    assert chain.index("rotate=") < chain.index("geq="), \
        "the picture must turn before the shape is cut, not after"


class TestFramingModel:
    def test_defaults_to_no_rotation(self):
        assert Framing().rotation == 0.0

    def test_clamps_to_a_half_turn_each_way(self):
        assert Framing(rotation=500).rotation == 180.0
        assert Framing(rotation=-500).rotation == -180.0

    def test_rejects_non_finite(self):
        with pytest.raises(ValueError):
            Framing(rotation=float("nan"))

    def test_clamps_on_ASSIGNMENT_too(self):
        # _EDLModel sets validate_assignment; a handler mutating in place must
        # not be able to slip a bad value past the model.
        f = Framing()
        f.rotation = 900
        assert f.rotation == 180.0
