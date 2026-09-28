"""Untagged and BT.709-tagged clips on one main track keep their colours
(final QA, round 3).

THE DEFECT: ffmpeg 8 negotiates colour tags across `concat` / `xfade`
inputs, so the FIRST clip's tag decided the lane and every other clip was
converted to it: after a BT.709-tagged clip an untagged clip was converted
BT.601 -> BT.709 (red 81,90,240 exported 62,102,239), and after an untagged
clip a tagged one went the other way — in the export only; the preview and
the proxies read untagged as BT.709 (INSTANT_PREVIEW_SPEC R12) and kept
them. A Canvas Blur background on the tagged clip triggered it too.

THE RULE NOW: every main-track segment is BT.709 limited before assembly —
an untagged or BT.709-limited clip is only LABELLED (its bytes pass), any
other matrix or full range is converted — so there is nothing to negotiate.
Measured on the raw Y'CbCr the export decodes to, against the source's own.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from video_ai_editor.edl.schema import EDL, Canvas, CanvasBackground, Clip, Track
from video_ai_editor.render import render_export

W, H, FPS = 320, 180, 30
TAG = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv"]


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True, capture_output=True)


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("colfx")
    # an untagged red clip (ffmpeg defaults: no colour tags) ...
    _ff("-f", "lavfi", "-i", f"color=c=red:s={W}x{H}:r={FPS}:d=1", "-f", "lavfi",
        "-i", "anullsrc=r=48000:cl=stereo:d=1", "-shortest", "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast", "-c:a", "aac", str(d / "untagged.mp4"))
    # ... and a BT.709-tagged green one, 4:3 so a Canvas background shows
    _ff("-f", "lavfi", "-i", f"color=c=0x20C040:s=240x180:r={FPS}:d=1,setparams=colorspace=bt709:range=tv"
        ":color_primaries=bt709:color_trc=bt709", "-f", "lavfi",
        "-i", "anullsrc=r=48000:cl=stereo:d=1", "-shortest", "-pix_fmt", "yuv420p", *TAG,
        "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast", "-c:a", "aac", str(d / "tagged.mp4"))
    return {p.stem: p for p in d.iterdir()}


def _yuv_means(path: Path, w: int, h: int, box: tuple[int, int, int, int]) -> list[tuple[float, float, float]]:
    """Mean raw (Y, Cb, Cr) inside `box` (x0, y0, x1, y1, luma pixels) of
    every frame — the decoded bytes, no colour conversion."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo",
                          "-pix_fmt", "yuv420p", "-"], capture_output=True, check=True).stdout
    fs = w * h * 3 // 2
    out = []
    x0, y0, x1, y1 = box
    for k in range(len(raw) // fs):
        f = np.frombuffer(raw[k * fs:(k + 1) * fs], np.uint8)
        Y = f[:w * h].reshape(h, w)
        U = f[w * h:w * h + w * h // 4].reshape(h // 2, w // 2)
        V = f[w * h + w * h // 4:].reshape(h // 2, w // 2)
        out.append((float(Y[y0:y1, x0:x1].mean()), float(U[y0 // 2:y1 // 2, x0 // 2:x1 // 2].mean()),
                    float(V[y0 // 2:y1 // 2, x0 // 2:x1 // 2].mean())))
    return out


def _source(media, name: str) -> tuple[float, float, float]:
    w = 240 if name == "tagged" else W
    return _yuv_means(media[name], w, H, (w // 2 - 20, 70, w // 2 + 20, 110))[5]


def _export(media, tmp: Path, order: list[str], *, blur: bool = False) -> list[tuple[float, float, float]]:
    clips = []
    for i, name in enumerate(order):
        c = Clip(id=f"c{i}", src=str(media[name]), in_=0, out=1, start=float(i))
        if blur and name == "tagged":
            c.canvas_bg = CanvasBackground(type="blur", blur=2)
        clips.append(c)
    edl = EDL(canvas=Canvas(w=W, h=H, fps=FPS, loudness_lufs=None),
              tracks=[Track(id="v1", type="video", z=0, clips=clips)])
    edl.recompute_duration()
    return _yuv_means(render_export(edl, tmp, height=H).path, W, H, (W // 2 - 20, 70, W // 2 + 20, 110))


@pytest.mark.parametrize("order,blur", [(["tagged", "untagged"], False),
                                        (["untagged", "tagged"], False),
                                        (["untagged", "tagged"], True)])
def test_each_clip_keeps_its_own_colours_whatever_comes_first(tmp_path, media, order, blur):
    got = _export(media, tmp_path, order, blur=blur)
    for i, name in enumerate(order):
        want = _source(media, name)
        seen = got[i * FPS + FPS // 2]
        assert max(abs(a - b) for a, b in zip(seen, want)) <= 3.0, (
            f"{name} as clip {i + 1} of {order}{' (blur bg)' if blur else ''}: "
            f"exported Y'CbCr {tuple(round(v) for v in seen)}, source {tuple(round(v) for v in want)}")


def test_the_export_is_tagged_bt709(tmp_path, media):
    clips = [Clip(id="u", src=str(media["untagged"]), in_=0, out=1, start=0)]
    edl = EDL(canvas=Canvas(w=W, h=H, fps=FPS, loudness_lufs=None),
              tracks=[Track(id="v1", type="video", z=0, clips=clips)])
    edl.recompute_duration()
    out = render_export(edl, tmp_path, height=H).path
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=color_space", "-of", "csv=p=0", str(out)],
                           capture_output=True, text=True, check=True).stdout.strip()
    assert probe == "bt709"


@pytest.mark.parametrize("base,pip", [("tagged", "untagged"), ("untagged", "tagged")])
def test_a_picture_in_picture_keeps_its_colours_over_either_base(tmp_path, media, base, pip):
    """The base is BT.709 now, and `overlay` negotiates tags too: a PiP video
    element is labelled / converted like a main-track segment, so an untagged
    PiP over a tagged clip (or the reverse) keeps its own Y'CbCr."""
    from video_ai_editor.edl.schema import Transform
    v1 = Clip(id="b", src=str(media[base]), in_=0, out=1, start=0)
    p = Clip(id="p", src=str(media[pip]), in_=0, out=1, start=0, fit="cover",
             transform=Transform(x=W / 2, y=H / 2, scale=1 / 0.35))
    edl = EDL(canvas=Canvas(w=W, h=H, fps=FPS, loudness_lufs=None),
              tracks=[Track(id="v1", type="video", z=0, clips=[v1]), Track(id="v2", type="video", z=1, clips=[p])])
    edl.recompute_duration()
    got = _yuv_means(render_export(edl, tmp_path, height=H).path, W, H, (W // 2 - 20, 70, W // 2 + 20, 110))
    want = _source(media, pip)
    assert max(abs(a - b) for a, b in zip(got[FPS // 2], want)) <= 3.0, (got[FPS // 2], want)
