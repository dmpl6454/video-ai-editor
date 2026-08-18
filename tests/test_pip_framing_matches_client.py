"""The PIP framing contract, measured against ffmpeg rather than derived.

Since the preview stopped baking a PIP's picture (pip.py's `preview` branch), the
browser draws it — so `pipDrawGeom` in frontend/src/lib/pipDraw.ts decides which
part of the source lands in the shape for the PREVIEW, and this file's filtergraph
decides it for the EXPORT. Two implementations, no shared code, and a divergence
is invisible until somebody exports.

Box SIZE is pinned by test_pip_overlay.py against the emitted filter string. This
pins the harder half — the SOURCE RECT after cover-scale, framing zoom and pan —
by rendering through real ffmpeg and reading back which source region survived.

Method: the source encodes its own coordinates (R = x/W, G = y/H), so the output's
own pixels say where they came from. That removes every assumption about how
ffmpeg rounds `force_original_aspect_ratio=increase` and clamps `crop` — the two
places a hand-derived mirror would drift.
"""
import re
import shutil
import subprocess

import numpy as np
import pytest
from PIL import Image

from video_ai_editor.edl.schema import EDL, Canvas, Clip, Framing, Mask, Track
from video_ai_editor.render.pip import build_pip_overlay_chain

# This file renders through REAL ffmpeg, so it must degrade to a skip where
# there is none rather than erroring — the same guard test_audio_on_video_lane
# and test_sticker_z_order use. It matters beyond a bare dev box: the macOS CI
# job installs plain `ffmpeg` (not ffmpeg-full), and the house rule everywhere
# is that a missing external toolchain skips cleanly instead of failing the run.
FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not on PATH")

SW, SH = 640, 360

# Sampling the OUTPUT's edge columns/rows reads pixels that interpolation has
# already blended, so each endpoint lands a pixel or two inside the true rect;
# ffmpeg's even-dimension snapping adds a little more. Measured worst case across
# these cases was 3.2px on a 640px-wide source (0.5%). 6px is comfortably inside
# "the same crop" and far outside the ~100px errors a real sign or aspect bug gives.
TOL = 6.0


@pytest.fixture(scope="module")
def ramp(tmp_path_factory):
    a = np.zeros((SH, SW, 3), np.uint8)
    a[:, :, 0] = (np.arange(SW) / (SW - 1) * 255).astype(np.uint8)[None, :]
    a[:, :, 1] = (np.arange(SH) / (SH - 1) * 255).astype(np.uint8)[:, None]
    p = tmp_path_factory.mktemp("pipframe") / "ramp.png"
    Image.fromarray(a).save(p)
    return p


def _emitted(ramp, **kw) -> str:
    c = Clip(src=str(ramp), in_=0, out=1, start=0.0, id="p")
    for k, v in kw.items():
        setattr(c, k, v)
    edl = EDL(canvas=Canvas(w=1080, h=1920, fps=30), tracks=[
        Track(id="v1", type="video", clips=[]),
        Track(id="v2", type="video", z=1, clips=[c])])
    chain, _, _, _ = build_pip_overlay_chain(
        edl, source_label="[v]", out_label="[o]", first_input_index=1,
        out_w=1080, out_h=1920)
    m = re.search(r"\[1:v\](scale=[^,]+,crop=[^\[]+)\[pip0\]", chain)
    assert m, f"no scale+crop emitted:\n{chain}"
    return m.group(1)


def _measure(ramp, tmp_path, filt):
    out = tmp_path / "out.png"
    # Resolved path, not the bare name: config's PATH augmentation makes the
    # bare form work today, but the rest of the codebase invokes ffmpeg through
    # a resolved binary and a test that finds a DIFFERENT ffmpeg than the app
    # would is measuring the wrong thing.
    subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(ramp), "-vf", filt,
                    "-frames:v", "1", str(out)], check=True)
    a = np.array(Image.open(out).convert("RGB")).astype(float)
    h, w, _ = a.shape
    x0 = a[h // 2, 0, 0] / 255 * (SW - 1)
    x1 = a[h // 2, -1, 0] / 255 * (SW - 1)
    y0 = a[0, w // 2, 1] / 255 * (SH - 1)
    y1 = a[-1, w // 2, 1] / 255 * (SH - 1)
    return (x0, y0, x1 - x0, y1 - y0), (w, h)


def _pip_draw_geom(box_w, box_h, zoom=1.0, fx=0.0, fy=0.0):
    """Mirror of lib/pipDraw.ts::pipDrawGeom.

    Kept in step by overlay.test.ts, which asserts the REAL TypeScript function
    against this same case table — so this transcription cannot quietly drift
    from the code it stands in for.
    """
    zoom = max(1.0, zoom)
    fx = max(-1.0, min(1.0, fx))
    fy = max(-1.0, min(1.0, fy))
    box_aspect = box_w / max(1e-6, box_h)
    sw, sh = float(SW), SW / box_aspect
    if sh > SH:
        sh, sw = float(SH), SH * box_aspect
    sw /= zoom
    sh /= zoom
    mx, my = (SW - sw) / 2, (SH - sh) / 2
    return (mx + fx * mx, my + fy * my, sw, sh)


CASES = [
    ("circle", {"mask": Mask(type="circle")}),
    ("cover", {"fit": "cover"}),
    ("circle_zoom2", {"mask": Mask(type="circle"), "framing": Framing(zoom=2.0)}),
    # Pan in BOTH directions: an inverted sign swaps these two and nothing else
    # would notice — the picture would simply move the wrong way under the drag.
    ("circle_zoom2_pan_right",
     {"mask": Mask(type="circle"), "framing": Framing(zoom=2.0, x=1.0)}),
    ("circle_zoom2_pan_left",
     {"mask": Mask(type="circle"), "framing": Framing(zoom=2.0, x=-1.0)}),
    ("cover_zoom15_pan_up", {"fit": "cover", "framing": Framing(zoom=1.5, y=-1.0)}),
]


@pytest.mark.parametrize("name,kw", CASES, ids=[c[0] for c in CASES])
def test_the_baked_source_rect_matches_pip_draw_geom(ramp, tmp_path, name, kw):
    got, (bw, bh) = _measure(ramp, tmp_path, _emitted(ramp, **kw))
    fr = kw.get("framing")
    want = _pip_draw_geom(bw, bh,
                          zoom=getattr(fr, "zoom", 1.0) or 1.0,
                          fx=getattr(fr, "x", 0.0) or 0.0,
                          fy=getattr(fr, "y", 0.0) or 0.0)
    for label, g, w in zip(("sx", "sy", "sw", "sh"), got, want):
        assert abs(g - w) <= TOL, (
            f"{name}: {label} baked={g:.1f} client={w:.1f} "
            f"(all baked={got}, client={want})")


def test_panning_actually_moves_the_window_and_in_the_right_direction(ramp, tmp_path):
    """A tolerance test alone would pass if BOTH sides were inverted together, and
    would also pass if pan were a no-op. This pins the absolute behaviour: right
    pan samples the right of the source, left pan the left, and they differ a lot."""
    left, _ = _measure(ramp, tmp_path, _emitted(
        ramp, mask=Mask(type="circle"), framing=Framing(zoom=2.0, x=-1.0)))
    right, _ = _measure(ramp, tmp_path, _emitted(
        ramp, mask=Mask(type="circle"), framing=Framing(zoom=2.0, x=1.0)))
    centre, _ = _measure(ramp, tmp_path, _emitted(
        ramp, mask=Mask(type="circle"), framing=Framing(zoom=2.0)))
    assert left[0] < centre[0] < right[0], (left[0], centre[0], right[0])
    assert right[0] - left[0] > 400, "pan barely moved the window"
