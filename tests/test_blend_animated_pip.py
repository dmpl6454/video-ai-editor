"""A picture-in-picture with a blend mode AND a size animation grows in the
export (final QA, round 3).

THE DEFECT: `pip.blend_overlay_parts` converted the element to RGB with a
sized `scale=in_color_matrix=…` filter. That ran AFTER the per-frame animated
`scale=…:eval=frame` stage, and ffmpeg's scale filter keeps the output size it
configured on the first frame — so a Screen / Multiply / Add PiP with Zoom In,
Spin or Bounce (or keyed scale) exported frozen at the animation's first
size, while Normal and both previews animated. Measured on the exported file:
the element's bounding box on a Normal render (the reference) and on each
blend mode's render must be the same box on every frame.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from video_ai_editor.edl.schema import EDL, Canvas, Clip, Track, Transform
from video_ai_editor.render import render_export

W, H, FPS = 320, 180, 30


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True, capture_output=True)


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("bap")
    # a dark grey base and a pure red element: red over 25 % grey differs from
    # the base under every one of the 14 modes
    _ff("-f", "lavfi", "-i", f"color=c=0x404040:s={W}x{H}:r={FPS}:d=2", "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast", str(d / "base.mp4"))
    _ff("-f", "lavfi", "-i", f"color=c=red:s=160x160:r={FPS}:d=2", "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast", str(d / "red.mp4"))
    return {p.stem: p for p in d.iterdir()}


def _edl(media, *, blend: str, variant: str) -> EDL:
    tr = Transform(x=W / 2, y=H / 2, scale=0.5)
    extra: dict = {}
    if variant == "keys":
        tr = Transform(x=W / 2, y=H / 2, scale={"keyframes": [[0.0, 0.2], [1.0, 0.6]]})
    else:
        extra = {"anim_in": variant, "anim_dur": 0.9}
    pip = Clip(id="p", src=str(media["red"]), in_=0, out=1.5, start=0.2, transform=tr,
               blend=blend, **extra)
    edl = EDL(canvas=Canvas(w=W, h=H, fps=FPS, loudness_lufs=None), tracks=[
        Track(id="v1", type="video", z=0, clips=[Clip(id="b", src=str(media["base"]), in_=0, out=2, start=0)]),
        Track(id="v2", type="video", z=1, clips=[pip]),
    ])
    edl.recompute_duration()
    return edl


def _boxes(path: Path) -> list[tuple[int, int] | None]:
    """(height, width) of the pixels that differ from the grey base, per frame."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, H, W, 3).astype(int)
    out: list[tuple[int, int] | None] = []
    for f in frames:
        on = np.abs(f - np.array([64, 64, 64])).max(axis=2) > 40
        ys, xs = np.nonzero(on)
        out.append((int(ys.max() - ys.min() + 1), int(xs.max() - xs.min() + 1)) if len(ys) else None)
    return out


@pytest.fixture(scope="module")
def reference(media, tmp_path_factory) -> dict[str, list]:
    ref: dict[str, list] = {}
    for variant in ("zoom_in", "spin", "bounce", "keys"):
        d = tmp_path_factory.mktemp(f"ref_{variant}")
        ref[variant] = _boxes(render_export(_edl(media, blend="normal", variant=variant), d, height=H).path)
    return ref


@pytest.mark.parametrize("blend", ["screen", "multiply", "add"])
@pytest.mark.parametrize("variant", ["zoom_in", "spin", "bounce", "keys"])
def test_a_blended_pip_animates_its_size_like_normal(tmp_path, media, reference, blend, variant):
    ref = reference[variant]
    sizes = {b[0] for b in ref if b}
    assert len(sizes) > 4, f"the Normal reference does not animate: {sorted(sizes)}"
    got = _boxes(render_export(_edl(media, blend=blend, variant=variant), tmp_path, height=H).path)
    assert len(got) == len(ref)
    bad = [(i, r, g) for i, (r, g) in enumerate(zip(ref, got))
           # 3 px: a turning element's anti-aliased edge crosses the >40
           # threshold a pixel apart between modes; a frozen one is 10-30 px off
           if (r is None) != (g is None) or (r and g and max(abs(r[0] - g[0]), abs(r[1] - g[1])) > 3)]
    assert not bad, f"{blend}/{variant}: {len(bad)} frames differ from Normal, first {bad[:4]}"


def _black_pixels(path: Path) -> list[int]:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, H, W, 3).astype(int)
    return [int((f.max(axis=2) < 30).sum()) for f in frames]


@pytest.mark.parametrize("variant", ["spin", "keyed_rotation"])
def test_a_turning_pip_has_no_black_corners(tmp_path, media, variant):
    """A Spin In (or keyed rotation) on a PiP with no alpha stage before it
    rotated with `c=black@0` on a format WITHOUT alpha, so the export showed
    a black square turning behind the element (the previews draw it
    transparent). Nothing in this fixture is darker than the 25 % grey base
    or the red element."""
    edl = _edl(media, blend="normal", variant="spin")
    if variant == "keyed_rotation":
        c = edl.get_track("v2").clips[0]
        c.anim_in = None
        c.transform = Transform(x=W / 2, y=H / 2, scale=0.5,
                                rotation={"keyframes": [[0.0, 0.0], [1.0, 90.0]]})
    black = _black_pixels(render_export(edl, tmp_path, height=H).path)
    assert max(black) <= 4, f"black pixels per frame: {black}"
