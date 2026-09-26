"""Proxy-shaped bar-coded test sources for the WK acceptance suites.

Each source is encoded with the normative proxy recipe of
INSTANT_PREVIEW_SPEC §5.1 (x264 all-intra, ``stitchable=1``, High@4.1,
8-bit 4:2:0, BT.709 limited tags, fragmented) and split into the §5.1
on-disk layout::

    <dir>/<name>/index.json   recipe, src_rate {num,den}, frames, w, h, span_frames
    <dir>/<name>/init.mp4     ftyp + moov (the avcC)
    <dir>/<name>/v/NNNN.bin   span packs: u32 first, u32 count, u32 sizes[count],
                              then AVCC samples; integers big-endian

so the pages read exactly what ``/api/proxies/{key}/…`` will serve, and the
real proxies can replace these without touching a test.

Frame identity is burned into every frame (§13): a bar of 15 cells, each
``BAR_CELL`` px wide, on rows 0..39 — bits 0..10 are the source frame index,
bits 11..14 the source id (1..15; never 0, so a real frame always has a
white cell and an all-black read means "no picture").
"""
from __future__ import annotations

import hashlib
import json
import struct
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

BAR_CELL = 48
FRAME_BITS = 11
SRC_BITS = 4
BAR_BITS = FRAME_BITS + SRC_BITS
BAR_ROW = 20  # the row the readers sample (the bar covers rows 0..39)
RECIPE = "wk-fixture-2 (spec 5.1: x264 keyint=1 stitchable=1 high@4.1 crf23 bt709 tv)"


@dataclass(frozen=True)
class SourceSpec:
    name: str
    src_id: int
    w: int
    h: int
    frames: int
    pattern: str = "testsrc2"
    rate: Fraction = Fraction(30)


def bar_code(src_id: int, frame: int) -> int:
    return (src_id << FRAME_BITS) | frame


def _run(cmd: list[str]) -> bytes:
    return subprocess.run(cmd, check=True, capture_output=True).stdout


def validate(spec: SourceSpec) -> None:
    if not 1 <= spec.src_id < 2 ** SRC_BITS:
        raise ValueError(f"src_id {spec.src_id} does not fit {SRC_BITS} bits")
    if spec.frames > 2 ** FRAME_BITS:
        raise ValueError(f"{spec.frames} frames do not fit {FRAME_BITS} bits")
    if spec.w < BAR_BITS * BAR_CELL:
        raise ValueError(f"{spec.w} px is narrower than the {BAR_BITS * BAR_CELL} px bar")


# The normative proxy recipe (§5.1) as x264 output options.
PROXY_CODEC_ARGS = (
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-profile:v", "high", "-level:v", "4.1",
    "-x264-params", "keyint=1:min-keyint=1:scenecut=0:bframes=0:stitchable=1",
    "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
    "-f", "mp4", "-movflags", "+empty_moov+default_base_moof+frag_keyframe",
)
# What ingest's normalised masters look like: x264 defaults (keyint 250,
# B-frames, so an mp4 edit list), progressive mp4.
MASTER_CODEC_ARGS = ("-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-movflags", "+faststart")


def encode(spec: SourceSpec, out: Path, codec_args: tuple[str, ...] = PROXY_CODEC_ARGS) -> None:
    """Encode ``spec``'s bar-coded frames to ``out`` (the proxy recipe by default)."""
    validate(spec)
    rate = f"{spec.rate.numerator}/{spec.rate.denominator}"
    dur = float(spec.frames / spec.rate) + 1
    base = spec.src_id << FRAME_BITS
    bar = (f"nullsrc=s={spec.w}x40:r={rate}:d={dur},format=gray,"
           f"geq=lum='if(bitand(floor(({base}+N)/pow(2\\,floor(X/{BAR_CELL})))\\,1)"
           f"*lt(X\\,{BAR_BITS * BAR_CELL})\\,235\\,16)'")
    _run([
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"{spec.pattern}=s={spec.w}x{spec.h}:r={rate}:d={dur}",
        "-f", "lavfi", "-i", bar,
        # setparams: ffmpeg 8.1 DROPS the -color_primaries/-color_trc output
        # options when the frames carry "unknown" (measured: ffprobe reports
        # primaries/transfer unknown), so the tags are set on the frames
        # themselves. The §5.1 recipe needs the same fix.
        "-filter_complex", "[1:v]format=yuv420p[b];[0:v]format=yuv420p[m];[m][b]overlay=0:0:shortest=1,format=yuv420p,"
                           "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv",
        "-frames:v", str(spec.frames), "-fps_mode", "passthrough", "-an",
        *codec_args, str(out),
    ])


def encode_master(spec: SourceSpec, out: Path) -> Path:
    """A bar-coded long-GOP master for the real proxy pipeline to ingest."""
    out.parent.mkdir(parents=True, exist_ok=True)
    encode(spec, out, MASTER_CODEC_ARGS)
    return out


# ------------------------------------------------------------ fMP4 splitting

def _boxes(buf: bytes, start: int, end: int):
    at = start
    while at + 8 <= end:
        size, typ = struct.unpack(">I4s", buf[at:at + 8])
        header = 8
        if size == 1:
            size = struct.unpack(">Q", buf[at + 8:at + 16])[0]
            header = 16
        elif size == 0:
            size = end - at
        if size < header or at + size > end:
            raise ValueError(f"bad box at {at}")
        yield typ.decode("latin-1"), at, at + header, at + size
        at += size


def _child(buf: bytes, body: int, end: int, typ: str):
    for t, s, b, e in _boxes(buf, body, end):
        if t == typ:
            return s, b, e
    return None


def split_fragmented(data: bytes) -> tuple[bytes, list[bytes]]:
    """(init segment, samples in decode order) of a fragmented mp4."""
    init_end = 0
    samples: list[bytes] = []
    for typ, start, body, end in _boxes(data, 0, len(data)):
        if typ in ("ftyp", "moov"):
            init_end = end
        elif typ == "moof":
            for ttyp, _ts, tbody, tend in _boxes(data, body, end):
                if ttyp != "traf":
                    continue
                tfhd = _child(data, tbody, tend, "tfhd")
                trun = _child(data, tbody, tend, "trun")
                if tfhd is None or trun is None:
                    raise ValueError("traf without tfhd/trun")
                tf_flags = struct.unpack(">I", data[tfhd[1]:tfhd[1] + 4])[0] & 0xFFFFFF
                if tf_flags & 0x1:
                    raise ValueError("base-data-offset tfhd is not supported")
                default_size = None
                o = tfhd[1] + 8
                for bit, width in ((0x2, 4), (0x8, 4), (0x10, 4), (0x20, 4)):
                    if tf_flags & bit:
                        if bit == 0x10:
                            default_size = struct.unpack(">I", data[o:o + 4])[0]
                        o += width
                vf = struct.unpack(">I", data[trun[1]:trun[1] + 4])[0]
                flags = vf & 0xFFFFFF
                count = struct.unpack(">I", data[trun[1] + 4:trun[1] + 8])[0]
                o = trun[1] + 8
                data_offset = 0
                if flags & 0x1:
                    data_offset = struct.unpack(">i", data[o:o + 4])[0]
                    o += 4
                if flags & 0x4:
                    o += 4
                pos = start + data_offset  # default-base-is-moof
                for _ in range(count):
                    size = default_size
                    if flags & 0x100:
                        o += 4
                    if flags & 0x200:
                        size = struct.unpack(">I", data[o:o + 4])[0]
                        o += 4
                    if flags & 0x400:
                        o += 4
                    if flags & 0x800:
                        o += 4
                    if size is None:
                        raise ValueError("trun without sample sizes")
                    samples.append(data[pos:pos + size])
                    pos += size
    if not init_end:
        raise ValueError("no init segment")
    return data[:init_end], samples


def span_pack(first: int, samples: list[bytes]) -> bytes:
    head = struct.pack(f">II{len(samples)}I", first, len(samples), *(len(s) for s in samples))
    return head + b"".join(samples)


def parse_span_pack(buf: bytes) -> tuple[int, list[bytes]]:
    first, count = struct.unpack(">II", buf[:8])
    sizes = struct.unpack(f">{count}I", buf[8:8 + 4 * count])
    at = 8 + 4 * count
    out = []
    for s in sizes:
        out.append(buf[at:at + s])
        at += s
    if at != len(buf):
        raise ValueError("trailing bytes in span pack")
    return first, out


def write_proxy_dir(spec: SourceSpec, root: Path) -> Path:
    """Encode ``spec`` and lay it out as a §5.1 proxy directory."""
    validate(spec)
    d = root / spec.name
    (d / "v").mkdir(parents=True, exist_ok=True)
    raw = d / "source_frag.mp4"
    encode(spec, raw)
    init, samples = split_fragmented(raw.read_bytes())
    if len(samples) != spec.frames:
        raise AssertionError(f"{spec.name}: {len(samples)} samples, expected {spec.frames}")
    span = round(2 * spec.rate)
    spans = []
    for n, first in enumerate(range(0, len(samples), span)):
        (d / "v" / f"{n:04d}.bin").write_bytes(span_pack(first, samples[first:first + span]))
        spans.append("ready")
    (d / "init.mp4").write_bytes(init)
    index = {
        "recipe": RECIPE,
        "src_rate": {"num": spec.rate.numerator, "den": spec.rate.denominator},
        "frames": len(samples), "w": spec.w, "h": spec.h,
        "span_frames": span, "spans": spans,
        "src_id": spec.src_id,
    }
    (d / "index.json").write_text(json.dumps(index))
    return d


def read_bar(frame_gray_rows: bytes, width: int) -> int:
    """The bar code of one decoded frame given its first 40 gray rows."""
    row = frame_gray_rows[BAR_ROW * width:(BAR_ROW + 1) * width]
    n = 0
    for bit in range(BAR_BITS):
        if row[bit * BAR_CELL + BAR_CELL // 2] > 128:
            n |= 1 << bit
    return n


def decode_bars(path: Path, width: int) -> list[int]:
    """Bar codes of every frame of ``path``, read from ffmpeg's decode."""
    raw = _run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-fps_mode", "passthrough",
                "-vf", f"crop={width}:40:0:0", "-f", "rawvideo", "-pix_fmt", "gray", "-"])
    per = width * 40
    return [read_bar(raw[i:i + per], width) for i in range(0, len(raw), per)]


# The standard WK fixture set: two landscape sources that share one size
# class (so one avcC) and one portrait source (a different size class).
WK_SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec("A", 1, 1280, 720, 240, "testsrc2"),
    SourceSpec("B", 2, 1280, 720, 240, "testsrc"),
    SourceSpec("C", 3, 720, 1280, 120, "testsrc2"),
)


def build_wk_fixtures(root: Path, sources: tuple[SourceSpec, ...] = WK_SOURCES) -> dict[str, Path]:
    """Build (or reuse, keyed on the specs and recipe) the fixture proxies."""
    key = hashlib.sha256(repr((RECIPE, sources)).encode()).hexdigest()[:12]
    base = root / f"wk-proxies-{key}"
    done = base / "READY"
    if not done.exists():
        for spec in sources:
            write_proxy_dir(spec, base)
        done.write_text("ok")
    return {spec.name: base / spec.name for spec in sources}
