"""Output-dimension parity: every rendered frame must have EVEN width AND height.

Regression cover for the 4:5 / "IG 4:5" render failure. H.264/yuv420p subsamples
chroma 2x2, so an odd dimension is unencodable — but the damage starts earlier in
the filtergraph, because ffmpeg's `pad` floors its target to the chroma multiple
while rounding its input up. `render_preview`'s short-edge math produced an ODD
675 for a 1080x1350 canvas, and the graph then aborted four different ways
depending on the timeline's shape (pad-smaller-than-input, concat filler
mismatch, mask alphamerge mismatch, encoder parity).

Two things here are deliberate and load-bearing:

1. The shapes are parametrized, not just "one clip". Each shape reaches the odd
   height through a DIFFERENT filter node, and three of the five passed even
   while 4:5 was broken — a single-shape test would have stayed green.
2. `force_libx264` runs the same matrix with the hardware ladder emptied. On
   macOS `h264_videotoolbox` silently writes 674 for an odd request, so the
   encoder-parity leg is INVISIBLE on the dev platform and only surfaced to a
   Windows tester (where the ladder falls through to libx264, which hard-rejects
   odd dimensions). Without this parameter a Mac-only run keeps hiding it.
"""
from __future__ import annotations
import json
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.edl.schema import Canvas, Clip, empty_edl
from video_ai_editor.render import compositor
from video_ai_editor.render import render_preview

# The four canvases the toolbar can produce (TopBar.tsx aspect buttons +
# the "IG 4:5" preset) and dispatch._RATIOS.
CANVASES = [
    pytest.param(1080, 1920, id="9x16"),
    pytest.param(1920, 1080, id="16x9"),
    pytest.param(1080, 1080, id="1x1"),
    pytest.param(1080, 1350, id="4x5"),      # the regression
]


def _mk(path: Path, w: int, h: int, duration: float = 1.5) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=30:duration={duration}",
         "-f", "lavfi", "-i", f"sine=f=440:duration={duration}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(path)],
        check=True, capture_output=True,
    )
    return path


def _dims(path: Path) -> tuple[int, int]:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    s = json.loads(proc.stdout)["streams"][0]
    return int(s["width"]), int(s["height"])


def _edl(cw: int, ch: int, shape: str, land: Path, port: Path):
    edl = empty_edl(Canvas(w=cw, h=ch, fps=30))
    v1 = edl.get_track("v1")
    if shape == "landscape":
        v1.clips.append(Clip(src=str(land), in_=0.0, out=1.5, start=0.0))
    elif shape == "portrait":
        # A source TALLER in aspect than the canvas is what makes `pad` see an
        # input bigger than its floored target.
        v1.clips.append(Clip(src=str(port), in_=0.0, out=1.5, start=0.0))
    elif shape == "mixed":
        v1.clips.append(Clip(src=str(land), in_=0.0, out=1.5, start=0.0))
        v1.clips.append(Clip(src=str(port), in_=0.0, out=1.5, start=1.5))
    elif shape == "leading_gap":
        # A gap makes the compositor emit a `color=` filler, which does NOT
        # round its size the way `pad` does -> concat link mismatch.
        v1.clips.append(Clip(src=str(land), in_=0.0, out=1.5, start=1.0))
    elif shape == "music_only":
        # No v1 clip at all: the filler reaches the encoder unpadded.
        edl.get_track("music").clips.append(
            Clip(src=str(land), in_=0.0, out=1.5, start=0.0))
    else:  # pragma: no cover
        raise AssertionError(shape)
    edl.recompute_duration()
    return edl


@pytest.fixture(scope="module")
def sources(tmp_path_factory) -> tuple[Path, Path]:
    d = tmp_path_factory.mktemp("parity_src")
    return _mk(d / "land.mp4", 640, 360), _mk(d / "port.mp4", 360, 640)


@pytest.mark.parametrize("shape", ["landscape", "portrait", "mixed",
                                   "leading_gap", "music_only"])
@pytest.mark.parametrize("cw,ch", CANVASES)
def test_preview_dimensions_are_even(tmp_path, sources, cw, ch, shape):
    land, port = sources
    res = render_preview(_edl(cw, ch, shape, land, port), tmp_path, height=540)
    w, h = _dims(res.path)
    assert w % 2 == 0 and h % 2 == 0, (
        f"{cw}x{ch} / {shape} rendered {w}x{h}; H.264 yuv420p needs even dims")


@pytest.mark.parametrize("shape", ["portrait", "leading_gap", "music_only"])
def test_preview_dimensions_are_even_without_hw_encoder(
        tmp_path, sources, monkeypatch, shape):
    """Same assertion with the HW ladder emptied, i.e. the Windows path.

    h264_videotoolbox silently accepts an odd height and writes 674; libx264
    fails with "height not divisible by 2". That asymmetry is exactly why this
    shipped broken to a Windows tester while every Mac run looked fine.
    """
    monkeypatch.setattr(compositor, "_HW_ENCODER_ORDER", [])
    compositor._usable_encoder.cache_clear()
    land, port = sources
    res = render_preview(_edl(1080, 1350, shape, land, port), tmp_path, height=540)
    w, h = _dims(res.path)
    assert (w, h) == (540, 674), f"expected 540x674 under libx264, got {w}x{h}"


@pytest.mark.parametrize("cw,ch,expect", [
    (1080, 1920, (540, 960)),
    (1920, 1080, (960, 540)),
    (1080, 1080, (540, 540)),
])
def test_working_presets_keep_their_exact_dimensions(tmp_path, sources, cw, ch, expect):
    """The parity floor must be the IDENTITY for presets that already worked.

    Guards against "fixing" 4:5 by changing the dimensions of 9:16 / 16:9 / 1:1,
    which would silently invalidate every cached preview and shift every baked
    text/sticker overlay.
    """
    land, port = sources
    res = render_preview(_edl(cw, ch, "landscape", land, port), tmp_path, height=540)
    assert _dims(res.path) == expect
