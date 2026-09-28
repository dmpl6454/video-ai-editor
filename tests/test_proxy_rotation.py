"""A source with a display rotation gets an upright, unsquashed proxy (wave E,
the E1a follow-up).

Phones store an upright clip as a landscape stream plus a 90° (or 270°)
display matrix; ffmpeg autorotates every decode, so the proxy encode receives
PORTRAIT frames. `probe_source` read the stored landscape size, so the proxy
was scaled to it: a portrait picture squashed into landscape (and the index's
w/h, which the engine fits by, was the wrong shape). It now reads the
rotation side data / `rotate` tag (`render/sar.display_size`) and the proxy
RECIPE_VERSION is 3 (4 since gate RX), so no squashed proxy is looked up again.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from video_ai_editor.ingest import proxy as P
from video_ai_editor.ingest.proxy_queue import ProxyManager
from video_ai_editor.render import sar

from proxy_fixtures import decode_gray, make_barcode_master, y_psnr


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch) -> Path:
    from video_ai_editor import storage as _storage
    wd = tmp_path / "wd"
    wd.mkdir()
    monkeypatch.setattr(_storage, "WORKDIR", wd)
    return wd


@pytest.fixture
def manager():
    m = ProxyManager()
    yield m
    m.shutdown()
    m.wait_idle(10)


def _rotated(src: Path, dst: Path, degrees: int) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-display_rotation", str(degrees), "-i", str(src),
                    "-c", "copy", str(dst)], check=True)
    return dst


def _autorotated_gray(src: Path, w: int, h: int) -> np.ndarray:
    out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(src), "-vf", f"scale={w}:{h}",
                          "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"], capture_output=True, check=True)
    return np.frombuffer(out.stdout, dtype=np.uint8).reshape(-1, h, w)


def test_display_size_reads_the_rotation():
    assert sar.display_size({"width": 1920, "height": 1080}) == (1920, 1080)
    rot = {"width": 1920, "height": 1080, "side_data_list": [{"rotation": -90}]}
    assert sar.display_size(rot) == (1080, 1920)
    assert sar.display_size({"width": 1920, "height": 1080, "tags": {"rotate": "270"}}) == (1080, 1920)
    assert sar.display_size({"width": 1920, "height": 1080, "tags": {"rotate": "180"}}) == (1920, 1080)
    # an anamorphic quarter turn: the sides swap and the SAR inverts — the
    # 1080x1440 frame at 3:4 displays 9:16 (the height kept, like the export)
    hdv = {"width": 1440, "height": 1080, "sample_aspect_ratio": "4:3", "side_data_list": [{"rotation": 90}]}
    assert sar.display_size(hdv) == (810, 1440)


@pytest.mark.parametrize("degrees,want", [(90, (360, 640)), (-90, (360, 640)), (180, (640, 360))])
def test_a_rotated_master_gets_an_upright_proxy(workdir, manager, tmp_path, degrees, want):
    master = make_barcode_master(tmp_path / "m.mp4", frames=12, w=640, h=360, audio=False)
    src = _rotated(master, tmp_path / f"rot{degrees}.mp4", degrees)
    info = P.probe_source(src)
    assert (info.width, info.height) == want
    assert info.extra.get("rotation", 0) == degrees % 360
    key = manager.ensure(src)
    assert manager.wait_idle(120), "proxy build did not finish"
    idx = P.read_index(key)
    assert (idx["w"], idx["h"]) == want
    info = P.load_source(key)
    samples: list[bytes] = []
    for n in range(info.spans):
        samples.extend(P.unpack_span(P.span_path(key, n).read_bytes())[1])
    got = decode_gray(P.avcc_of(P.init_path(key).read_bytes()), samples, *want)
    ref = _autorotated_gray(src, *want)
    assert len(got) == len(ref) == 12
    worst = min(y_psnr(a, b) for a, b in zip(got, ref))
    assert worst > 30.0, worst


def test_the_recipe_version_moved_past_the_squashed_proxies():
    assert P.RECIPE_VERSION >= 3 and P.recipe()["version"] == P.RECIPE_VERSION
