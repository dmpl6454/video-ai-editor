"""Transform.flip_h / flip_v (CapCut Mirror / Flip, wave E lane F4a) in the
export, decoded.

The mirror applies to the PICTURE before its rotation, scale and position:
a mirrored clip rotated 30° still turns clockwise by 30°. The v1 chain is
pinned by the geometry goldens (`tflip_*`, tests/geometry_golden_lib.py, and
geometry.ts to 1 px); this file measures the overlay (PiP) and the sticker
paths, and that an unflipped EDL's filter text is unchanged.
"""
from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
import geometry_golden_lib as G  # noqa: E402
from overlay_render_helpers import base_edl, centroid, frame_rgb, mask_of  # noqa: E402

from video_ai_editor.edl.schema import Clip, Keyframe, Sticker, Track, Transform  # noqa: E402
from video_ai_editor.render import compositor  # noqa: E402

W, H = 1280, 720


@pytest.fixture(scope="module")
def land(tmp_path_factory) -> str:
    d = tmp_path_factory.mktemp("flipsrc")
    return str(G.make_source(d / "land.mp4", G.SOURCES["land"]))


def _render(edl, tmp: Path) -> np.ndarray:
    out = compositor._render(edl, tmp / "x.mp4", height=H, fps=30, preview=False,
                             cache_dir=tmp / "cache", chunked=False)
    return frame_rgb(out, 10)


def _markers(fr: np.ndarray) -> dict:
    return G.measure_frame(fr, gain_only=False)["markers"]


def _pip_edl(tmp: Path, src: str, **tx):
    edl = base_edl(tmp, W, H, 1.0)
    pip = Clip(id="p", src=src, in_=0.0, out=1.0, start=0.0,
               transform=Transform(x=W / 2, y=H / 2, scale=2.0, **tx))
    edl.tracks.append(Track(id="v2", type="video", z=1, clips=[pip]))
    edl.recompute_duration()
    return edl


def _rot(p, c, deg):
    """`p` turned `deg` clockwise on screen (y down) about `c`."""
    a = math.radians(deg)
    x, y = p[0] - c[0], p[1] - c[1]
    return (c[0] + x * math.cos(a) - y * math.sin(a), c[1] + x * math.sin(a) + y * math.cos(a))


def _close(a, b, tol=1.6) -> bool:
    return a is not None and b is not None and math.hypot(a[0] - b[0], a[1] - b[1]) <= tol


def test_a_flipped_overlay_is_mirrored_before_it_turns(tmp_path, land):
    plain = _markers(_render(_pip_edl(tmp_path / "a", land), tmp_path / "a"))
    c = plain["white"]
    assert all(plain[m] is not None for m in ("red", "green", "blue", "yellow"))
    mirror_h = {m: (2 * c[0] - p[0], p[1]) for m, p in plain.items()}
    mirror_v = {m: (p[0], 2 * c[1] - p[1]) for m, p in plain.items()}

    fh = _markers(_render(_pip_edl(tmp_path / "b", land, flip_h=True), tmp_path / "b"))
    assert all(_close(fh[m], mirror_h[m]) for m in mirror_h), (fh, mirror_h)
    fv = _markers(_render(_pip_edl(tmp_path / "c", land, flip_v=True), tmp_path / "c"))
    assert all(_close(fv[m], mirror_v[m]) for m in mirror_v), (fv, mirror_v)

    turned = _markers(_render(_pip_edl(tmp_path / "d", land, flip_h=True, rotation=30.0), tmp_path / "d"))
    first_mirror = {m: _rot(mirror_h[m], c, 30.0) for m in mirror_h}
    assert all(_close(turned[m], first_mirror[m], 2.5) for m in first_mirror), (turned, first_mirror)
    # ... and not the other order (turned, then mirrored: a -30° picture)
    then_mirror = {m: (2 * c[0] - _rot(plain[m], c, 30.0)[0], _rot(plain[m], c, 30.0)[1]) for m in plain}
    assert not _close(turned["red"], then_mirror["red"], 10.0)


def _sticker_png(path: Path) -> Path:
    img = Image.new("RGBA", (200, 100), (0, 0, 0, 0))
    img.paste((255, 0, 0, 255), (0, 0, 100, 100))
    img.paste((0, 0, 255, 255), (100, 0, 200, 100))
    img.save(path)
    return path


@pytest.mark.parametrize("keyed", [False, True], ids=["static", "keyed"])
def test_a_flipped_sticker_is_mirrored(tmp_path, keyed):
    png = _sticker_png(tmp_path / "rb.png")

    def edl_for(tmp: Path, **tx):
        edl = base_edl(tmp, W, H, 1.0)
        x = Keyframe(keyframes=[(0.0, W / 2), (1.0, W / 2 + 1)]) if keyed else W / 2
        st = Sticker(id="s", src=str(png), start=0.0, end=1.0, transform=Transform(x=x, y=H / 2, **tx))
        edl.tracks.append(Track(id="st", type="sticker", z=20, clips=[st]))
        edl.recompute_duration()
        return edl

    def sides(fr):
        r, b = centroid(mask_of(fr, (255, 0, 0))), centroid(mask_of(fr, (0, 0, 255)))
        assert r is not None and b is not None
        return r, b

    r0, b0 = sides(_render(edl_for(tmp_path / "a"), tmp_path / "a"))
    assert r0[0] < b0[0], "red is on the left unflipped"
    r1, b1 = sides(_render(edl_for(tmp_path / "b", flip_h=True), tmp_path / "b"))
    assert r1[0] > b1[0] and abs(r1[0] - b0[0]) < 3 and abs(b1[0] - r0[0]) < 3
    # mirrored, THEN turned 90° clockwise: red (now right) goes to the bottom
    r2, b2 = sides(_render(edl_for(tmp_path / "c", flip_h=True, rotation=90.0), tmp_path / "c"))
    assert r2[1] > b2[1] + 40, (r2, b2)


def test_an_unflipped_edl_emits_the_same_filter_text(tmp_path, land):
    """No flip, no filter: the graph text is what it was (a v1 clip, an
    overlay and a sticker)."""
    edl = _pip_edl(tmp_path / "a", land, rotation=10.0)
    graph = compositor._build_filter_complex(
        compositor._video_clips(edl), W, H, transitions=[], cache_dir=None, fps=30,
        total_duration=edl.duration)[0]
    assert "hflip" not in graph and "vflip" not in graph
    flipped = _pip_edl(tmp_path / "b", land, rotation=10.0)
    flipped.get_track("v1").clips[0].transform.flip_h = True
    g2 = compositor._build_filter_complex(
        compositor._video_clips(flipped), W, H, transitions=[], cache_dir=None, fps=30,
        total_duration=flipped.duration)[0]
    assert g2.count("hflip") == 1 and g2.replace(",hflip", "") == graph

    # review RE: the OVERLAY chain (pip.py) — the docstring always said so,
    # but only the v1 graph was built
    from video_ai_editor.render.pip import build_pip_overlay_chain

    def pip_graph(edl):
        return build_pip_overlay_chain(edl, source_label="[base]", out_label="[out]", first_input_index=1,
                                       out_w=W, out_h=H, fps=30)[0]
    plain = _pip_edl(tmp_path / "c", land, rotation=10.0)
    pg = pip_graph(plain)
    assert "hflip" not in pg and "vflip" not in pg
    plain.get_track("v2").clips[0].transform.flip_h = True
    pg2 = pip_graph(plain)
    assert pg2.count("hflip") == 1 and "[pipf0]" in pg2
    # the flipped graph is the plain one plus exactly the mirror stage
    import re as _re
    stage = _re.search(r"(\[[^\]]+\])hflip\[pipf0\];", pg2)
    assert stage is not None
    assert pg2.replace(stage.group(0), "").replace("[pipf0]", stage.group(1)) == pg

    # and the STICKER chain (text_overlay.py): the PNG is mirrored before it turns
    from PIL import Image
    from video_ai_editor.render.text_overlay import build_overlay_chain
    png = tmp_path / "st.png"
    Image.new("RGBA", (40, 20), (255, 0, 0, 255)).save(png)

    def st_graph(flip: bool):
        edl = base_edl(tmp_path / ("sf" if flip else "sp"), W, H, 1.0)
        edl.tracks.append(Track(id="st", type="sticker", z=20, clips=[
            Sticker(id="s", src=str(png), start=0.0, end=1.0,
                    transform=Transform(x=W / 2, y=H / 2, rotation=10.0, flip_h=flip))]))
        edl.recompute_duration()
        return build_overlay_chain(edl, tmp_path / "cache", source_label="[base]", out_label="[out]",
                                   first_input_index=1, out_w=W, out_h=H)
    sp, sf = st_graph(False), st_graph(True)
    assert "hflip" not in sp[0] and "vflip" not in sp[0]
    # the sticker's mirror is baked into its cached PNG: a DIFFERENT input, the same graph
    assert sf[0] == sp[0] and sf[1] != sp[1]


def test_flip_round_trips_through_the_project_file():
    from video_ai_editor.edl.schema import EDL
    edl = EDL.model_validate({"tracks": [{"id": "v1", "type": "video", "clips": [
        {"id": "c", "src": "a.mp4", "in": 0, "out": 1, "start": 0,
         "transform": {"flip_h": True, "flip_v": False}}]}]})
    again = EDL.model_validate_json(edl.model_dump_json(by_alias=True))
    tx = again.get_track("v1").clips[0].transform
    assert tx.flip_h is True and tx.flip_v is False
    old = EDL.model_validate({"tracks": [{"id": "v1", "type": "video", "clips": [
        {"id": "c", "src": "a.mp4", "in": 0, "out": 1, "start": 0, "transform": {"x": 3}}]}]})
    assert old.get_track("v1").clips[0].transform.flip_h is False
