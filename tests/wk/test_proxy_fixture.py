"""The proxy-shaped fixture sources the WK suites read (no WebKit needed):
real x264 all-intra output, measured with ffprobe and an ffmpeg decode."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from .proxy_fixture import (
    BAR_BITS, BAR_CELL, SourceSpec, bar_code, decode_bars, parse_span_pack, span_pack, split_fragmented,
    write_proxy_dir,
)

pytestmark = pytest.mark.skipif(subprocess.run(["which", "ffmpeg"], capture_output=True).returncode != 0,
                                reason="ffmpeg not on PATH")

LAND_A = SourceSpec("LA", 5, 720, 400, 45, "testsrc2")
LAND_B = SourceSpec("LB", 6, 720, 400, 30, "testsrc")
PORT = SourceSpec("PT", 7, 720, 1280, 20, "testsrc2")


@pytest.fixture(scope="module")
def proxies(tmp_path_factory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("fixture-proxies")
    return {s.name: write_proxy_dir(s, root) for s in (LAND_A, LAND_B, PORT)}


def _packets(path: Path) -> list[tuple[int, str]]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=size,flags",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout
    return [(int(s), f) for s, f in (line.split(",")[:2] for line in out.split())]


def _stream(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=profile,level,pix_fmt,color_space,color_primaries,color_transfer,color_range,width,height",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)["streams"][0]


def test_every_frame_is_an_idr_and_the_split_matches_ffprobe(proxies):
    d = proxies["LA"]
    packets = _packets(d / "source_frag.mp4")
    assert len(packets) == LAND_A.frames
    assert all(f.startswith("K") for _, f in packets)
    init, samples = split_fragmented((d / "source_frag.mp4").read_bytes())
    assert [len(s) for s in samples] == [s for s, _ in packets]
    assert init == (d / "init.mp4").read_bytes()


def test_recipe_tags_high_41_8bit_420_bt709_limited(proxies):
    s = _stream(proxies["LA"] / "source_frag.mp4")
    assert (s["profile"], s["level"], s["pix_fmt"]) == ("High", 41, "yuv420p")
    assert (s["color_space"], s["color_primaries"], s["color_transfer"], s["color_range"]) == ("bt709", "bt709", "bt709", "tv")


def test_same_size_sources_share_one_init_and_other_sizes_do_not(proxies):
    # stitchable=1 + fixed recipe: one size class = one avcC = one init (§3.1).
    assert (proxies["LA"] / "init.mp4").read_bytes() == (proxies["LB"] / "init.mp4").read_bytes()
    assert (proxies["LA"] / "init.mp4").read_bytes() != (proxies["PT"] / "init.mp4").read_bytes()


def test_span_packs_cover_every_frame_in_order(proxies):
    d = proxies["LA"]
    index = json.loads((d / "index.json").read_text())
    assert index["span_frames"] == 60 and index["frames"] == 45 and (index["w"], index["h"]) == (720, 400)
    _, samples = split_fragmented((d / "source_frag.mp4").read_bytes())
    got: list[bytes] = []
    for n in range(len(index["spans"])):
        first, pack = parse_span_pack((d / "v" / f"{n:04d}.bin").read_bytes())
        assert first == len(got)
        got += pack
    assert got == samples


def test_span_pack_is_big_endian_and_round_trips():
    blob = span_pack(7, [b"\x00\x00\x00\x01e", b"xy"])
    assert blob[:16] == bytes([0, 0, 0, 7, 0, 0, 0, 2, 0, 0, 0, 5, 0, 0, 0, 2])
    assert parse_span_pack(blob) == (7, [b"\x00\x00\x00\x01e", b"xy"])
    with pytest.raises(ValueError):
        parse_span_pack(blob + b"!")


def test_the_bar_decodes_to_source_id_and_frame(proxies):
    for spec in (LAND_A, PORT):
        bars = decode_bars(proxies[spec.name] / "source_frag.mp4", spec.w)
        assert bars == [bar_code(spec.src_id, i) for i in range(spec.frames)]


def test_spec_validation():
    with pytest.raises(ValueError, match="does not fit"):
        write_proxy_dir(SourceSpec("X", 16, 720, 400, 5), Path("/nonexistent"))
    with pytest.raises(ValueError, match="narrower"):
        write_proxy_dir(SourceSpec("X", 1, BAR_BITS * BAR_CELL - 2, 400, 5), Path("/nonexistent"))
