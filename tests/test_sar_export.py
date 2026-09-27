"""Anamorphic sources are fitted by their DISPLAYED shape on every render path
(Wave D3, lane E1a; render/sar.py).

A 720x576 SAR 16:15 PAL frame displays 768x576 (4:3); ffmpeg's
`force_original_aspect_ratio` sized it from the STORED 720 and the export drew
a 5:4 picture (450 px wide in a 640x360 canvas instead of 480). The preview
engine has always drawn the display shape (square-pixel proxies at the
displayed width), so preview and export disagreed. Every path is checked here
on decoded frames of synthesized masters (flat grey, five markers, see
geometry_golden_lib): the single-pass export, the chunked render, the server
preview, a reversed clip, a PIP overlay, and an export at another size. A
square-pixel phone clip is the control: its filter text is unchanged.
"""
from __future__ import annotations

import shutil
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

import geometry_golden_lib as lib
from video_ai_editor.edl.schema import Canvas, Clip, empty_edl
from video_ai_editor.render import compositor
from video_ai_editor.render.sar import (anamorphic_from_stream, display_width, fit_dims,
                                        parse_sar, source_anamorphic, square_pixels_filter)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")

W, H = 640, 360
PHONE = lib.Src("phone", 1080, 1920, Fraction(30), 30, Fraction(1, 1))
#: key → (source spec, displayed w x h)
MASTERS = {
    "pal43": (lib.SOURCES["pal43"], (768, 576)),
    "pal169": (lib.SOURCES["pal169"], (1024, 576)),
    "hdv": (lib.SOURCES["hdv"], (1920, 1080)),
    "phone": (PHONE, (1080, 1920)),
}


@pytest.fixture(scope="module")
def masters(tmp_path_factory) -> dict[str, str]:
    root = tmp_path_factory.mktemp("sar-src")
    return {k: str(lib.make_source(root / f"{k}.mp4", spec)) for k, (spec, _d) in MASTERS.items()}


def _contain_bbox(disp: tuple[int, int], cw: int = W, ch: int = H) -> list[int]:
    """Where `contain` puts a displayed w x h picture: ffmpeg's integers
    (fit_dims), the pad offset and width on the chroma grid."""
    pw, ph = fit_dims(disp[0], disp[1], cw, ch, "decrease")
    ox, oy = int((cw - pw) / 2) // 2 * 2, int((ch - ph) / 2) // 2 * 2
    return [ox, oy, ox + pw // 2 * 2, oy + ph // 2 * 2]


def _edl(src: str, *, canvas=(W, H), fit="contain", reverse=False, pip=False, out=1.0):
    e = empty_edl(Canvas(w=canvas[0], h=canvas[1], fps=30))
    e.canvas.loudness_lufs = None
    c = Clip(src=src, id="c0")
    c.in_, c.out, c.fit = 0.0, out, fit
    c.reverse = reverse
    if pip:
        c.transform.x, c.transform.y, c.transform.scale = canvas[0] / 2, canvas[1] / 2, 1.0
        e.get_track("v2").clips.append(c)
    else:
        e.get_track("v1").clips.append(c)
    e.recompute_duration()
    return e


def _frames(path: Path, w: int, h: int) -> tuple[np.ndarray, np.ndarray]:
    return lib.decode_rgb(path, w, h), lib.decode_y(path, w, h)


def _bboxes(path: Path, w: int = W, h: int = H) -> list[list[int] | None]:
    rgb, ys = _frames(path, w, h)
    return [lib.measure_frame(rgb[i], gain_only=False, luma=ys[i])["bbox"] for i in range(len(rgb))]


# ------------------------------------------------------------------ the rule

def test_display_size_rule_is_the_proxy_rule():
    assert display_width(720, Fraction(16, 15)) == 768
    assert display_width(720, Fraction(64, 45)) == 1024
    assert display_width(1440, Fraction(4, 3)) == 1920
    assert display_width(720, Fraction(32, 27)) == 854       # 853.33 → nearest even
    assert display_width(1920, None) == 1920
    for square in ("1:1", "0:1", "N/A", "", None, "16:0"):
        assert parse_sar(square) is None
    assert parse_sar("16:15") == Fraction(16, 15)
    # a quarter turn (autorotate's transpose) swaps the sides and inverts the SAR
    a = anamorphic_from_stream({"width": 1440, "height": 1080, "sample_aspect_ratio": "4:3",
                                "side_data_list": [{"rotation": -90}]})
    assert (a.width, a.height, a.sar) == (1080, 1440, Fraction(3, 4))
    assert (a.display_w, a.display_h) == (810, 1440)


def test_probe_reads_the_masters(masters):
    for k, (spec, disp) in MASTERS.items():
        a = source_anamorphic(masters[k])
        if spec.sar == 1:
            assert a is None, k
            assert square_pixels_filter(masters[k]) == ""
        else:
            assert (a.width, a.height, a.sar) == (spec.w, spec.h, spec.sar), k
            assert (a.display_w, a.display_h) == disp, k


def test_square_pixel_chain_text_is_unchanged(masters):
    """The control: a SAR 1:1 phone clip gets the historical fit text, so
    every square-pixel render stays byte-identical."""
    c = Clip(src=masters["phone"], id="p")
    c.out = 1.0
    chain = compositor._build_clip_video_chain(c, input_label="[0:v]", label_out="[v]",
                                               canvas_w=W, canvas_h=H, fps=30)
    assert f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad=" in chain
    c.src = masters["pal43"]
    chain = compositor._build_clip_video_chain(c, input_label="[0:v]", label_out="[v]",
                                               canvas_w=W, canvas_h=H, fps=30)
    assert "force_original_aspect_ratio" not in chain and "scale=480:360,pad=" in chain


# -------------------------------------------------------------- every path

@pytest.mark.parametrize("key", list(MASTERS))
def test_export_fits_the_displayed_shape_frame_by_frame(key, masters, tmp_path):
    disp = MASTERS[key][1]
    out = compositor._render(_edl(masters[key]), tmp_path / "o.mp4", height=H, fps=30, preview=False,
                             cache_dir=tmp_path / "cache", chunked=False)
    want = _contain_bbox(disp)
    got = _bboxes(out)
    assert len(got) == 30
    assert all(b == want for b in got), (key, want, got[:3])
    if key == "pal43":
        assert want == [80, 0, 560, 360]            # the squeezed 5:4 picture was [95, 0, 545, 360]


def test_portrait_canvas_and_markers(masters, tmp_path):
    """PAL 16:9 in a 9:16 canvas: 360 x 202.5 → 203 rows; the red marker
    (source 0.25, 0.25) lands a quarter into the DISPLAYED picture."""
    out = compositor._render(_edl(masters["pal169"], canvas=(360, 640)), tmp_path / "o.mp4", height=640,
                             fps=30, preview=False, cache_dir=tmp_path / "cache", chunked=False)
    rgb, ys = _frames(out, 360, 640)
    m = lib.measure_frame(rgb[5], gain_only=False, luma=ys[5])
    assert m["bbox"] == _contain_bbox((1024, 576), 360, 640) == [0, 218, 360, 420]
    assert m["markers"]["red"] == pytest.approx([90, 218 + 203 / 4], abs=1.0)
    assert m["markers"]["white"] == pytest.approx([180, 319.5], abs=1.0)


@pytest.mark.parametrize("key", ["pal43", "hdv"])
def test_cover_fills_by_the_displayed_shape(key, masters, tmp_path):
    """cover crops the DISPLAYED picture: HDV (16:9 displayed) fills a 16:9
    canvas with nothing cut, so its markers sit at the quarter points."""
    out = compositor._render(_edl(masters[key], fit="cover"), tmp_path / "o.mp4", height=H, fps=30,
                             preview=False, cache_dir=tmp_path / "cache", chunked=False)
    rgb, ys = _frames(out, W, H)
    m = lib.measure_frame(rgb[3], gain_only=False, luma=ys[3])
    assert m["bbox"] == [0, 0, W, H]
    cw, ch = fit_dims(*MASTERS[key][1], W, H, "increase")
    oy = (ch - H) / 2
    assert m["markers"]["red"][0] == pytest.approx(0.25 * cw - (cw - W) / 2, abs=1.0)
    assert m["markers"]["red"][1] == pytest.approx(0.25 * ch - oy, abs=1.5)


def test_chunked_render_and_server_preview_agree(masters, tmp_path):
    want = _contain_bbox((768, 576))
    chunked = compositor._render(_edl(masters["pal43"]), tmp_path / "c.mp4", height=H, fps=30, preview=False,
                                 cache_dir=tmp_path / "cache", chunked=True)
    assert all(b == want for b in _bboxes(chunked))
    res = compositor.render_preview(_edl(masters["pal43"]), tmp_path / "sess", height=H)
    assert all(b == want for b in _bboxes(Path(res.path)))


def test_reversed_anamorphic_clip_keeps_its_shape(masters, tmp_path):
    """The reversed intermediate keeps the source SAR (render/reverse.py
    squared it to 1:1 without resampling: the reverse exported squeezed)."""
    out = compositor._render(_edl(masters["pal43"], reverse=True), tmp_path / "r.mp4", height=H, fps=30,
                             preview=False, cache_dir=tmp_path / "cache", chunked=False)
    assert all(b == [80, 0, 560, 360] for b in _bboxes(out))


def test_export_at_another_size_scales_the_same_shape(masters, tmp_path):
    out = compositor._render(_edl(masters["pal43"]), tmp_path / "o.mp4", height=720, fps=30, preview=False,
                             cache_dir=tmp_path / "cache", chunked=False)
    assert _bboxes(out, 1280, 720)[0] == [160, 0, 1120, 720]


@pytest.mark.parametrize("key", ["pal43", "hdv"])
def test_pip_overlay_has_the_displayed_aspect(key, masters, tmp_path):
    """A PIP is sized by its width with `h=-1`: from the STORED shape a PAL
    4:3 PIP came out 5:4 and an HDV one 4:3 (the UI box, <video>.videoWidth,
    is the displayed shape)."""
    out = compositor._render(_edl(masters[key], pip=True), tmp_path / "p.mp4", height=H, fps=30, preview=False,
                             cache_dir=tmp_path / "cache", chunked=False)
    bb = _bboxes(out)[10]
    assert bb is not None
    w, h = bb[2] - bb[0], bb[3] - bb[1]
    dw, dh = MASTERS[key][1]
    assert w / h == pytest.approx(dw / dh, abs=0.02), (key, bb)
