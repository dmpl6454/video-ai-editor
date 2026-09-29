"""Preview proxies: the source frames the instant-preview engine plays (wave D).

Spec: docs/design/INSTANT_PREVIEW_SPEC.md §3.1, §5.1, §6 R5, §9.3.

WHAT A PROXY IS. For every source the client may draw, the server keeps an
ALL-INTRA, STITCHABLE, 8-bit 4:2:0, BT.709-tagged H.264 copy at the SOURCE's
own frame rate (``edl.timebase.source_rate``), short edge 720, plus the
source's audio decoded by ffmpeg into 48 kHz stereo FLAC chunks. Every frame
is an IDR and every same-size proxy of one recipe shares its SPS/PPS, so the
client can write any proxy frame at any output position of its own fMP4
stream: a cut, a duplicate, a reverse or a speed change is only a choice of
which sample goes where. Audio is ffmpeg's own decode — the PCM the export
decodes — so neither the AAC priming samples nor the mp4 edit list can shift
it.

LAYOUT. ``WORKDIR/proxies/<key>/``, ``key = sha256(realpath, size, mtime_ns,
recipe)[:24]`` — cached on FILE IDENTITY, like render/chunks.py:

    index.json      static facts (recipe, src_rate, frames, w, h, codec, ...)
    source.json     the probe (stream facts + the master pts table)
    refs.json       session ids that asked for this proxy (route access check)
    init.mp4        ftyp + moov: the avcC every span of this proxy shares
    v/NNNN.bin      span pack: u32be first, u32be count, u32be sizes[count],
                    then `count` AVCC samples (4-byte NAL lengths), frame
                    `first + i` of the master in presentation order
    a/NNNN.flac     48 kHz stereo 24-bit FLAC, exactly AUDIO_CHUNK_SAMPLES
                    samples (the last chunk may be short). A chunk whose
                    decode goes past full scale is stored divided by a power
                    of two (exact in float); index.json audio.chunk_gain
                    maps "n" -> that power of two, which the client
                    multiplies back, and the chunk route repeats it as
                    X-Audio-Gain

FRAME IDENTITY (R5). Proxy frame ``i`` is master frame ``i`` in presentation
order after the edit list: ffmpeg applies the edit list when it decodes, and
the encode passes every decoded frame through (``-fps_mode passthrough``).
Asserted: an encode of frames ``[f0, f1)`` must yield exactly ``f1 - f0``
samples, and the full count must equal the master's pts table. A mismatch
fails the proxy (the client then uses the degraded tier, spec §7).

NOT HERE: scheduling, niceness, cancellation and the eager/on-demand queue
live in ``ingest/proxy_queue.py``; the HTTP surface is
``api/preview_routes.py``. The masters themselves are never changed (N5).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import struct
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Callable, Iterator

from .. import platformutil as _pu
from ..edl import timebase as _tb

# ---- recipe (normative, spec §5.1) ------------------------------------------

#: Bump when anything below changes what a proxy's bytes are: the key hashes it,
#: so old proxies are simply never looked up again (and age out of the LRU).
#: 2: levels/matrix converted to BT.709 limited (not relabelled), square pixels
#:    (setsar=1, SAR parsed), absolute seeks, power-of-two audio headroom.
#: 3: sized to the AUTOROTATED picture (wave E): a source with a 90/270°
#:    display matrix (a phone clip shot upright) was scaled to its stored
#:    landscape size — the proxy squashed a portrait picture.
#: 4: the sound is on the FILE clock (wave E gate RX): an audio stream that
#:    starts after the file does is led by silence, so proxy sample S(t) is
#:    file time t, as every render chain reads it (`AUDIO_FILTER`).
RECIPE_VERSION = 4
#: Short edge of the proxy picture. Sources smaller than this are NOT upscaled
#: (a 360p source makes a 360p proxy): upscaling adds bytes, not detail.
SHORT_EDGE = 720
SPAN_SECONDS = 2
AUDIO_RATE = 48000
AUDIO_CHANNELS = 2
AUDIO_CHUNK_SAMPLES = 5 * AUDIO_RATE          # 240000 samples = 5 s
#: The proxy's decode of a master's sound. First `aresample=async=1:first_pts=0`
#: at the source's own rate — the head of every render chain (compositor,
#: pip, audio_mix, reverse, speed_audio) — so a stream that starts after the
#: file (audio start_time > format start_time: camera and screen-recorder
#: MOV/MP4) is padded with silence and proxy sample S(t) is file time t,
#: which the client indexes by `edit_sample(in)` (spec R9). Without it the
#: client played such a source 100 ms off the server on every sample (gate RX,
#: tests/test_proxy_audio_clock.py). Then the 48 kHz stereo float conversion.
AUDIO_FILTER = ("aresample=async=1:first_pts=0,"
                f"aresample={AUDIO_RATE},aformat=sample_fmts=flt:channel_layouts=stereo")
#: MSE timescale the client writes in (spec R1). The init's mdhd carries it so
#: the init segment can be passed through untouched.
TIMESCALE = 240000
X264_PARAMS = "keyint=1:min-keyint=1:scenecut=0:bframes=0:stitchable=1"
PROFILE = "high"
LEVEL = "4.1"
PRESET = "veryfast"
ENCODE_THREADS = 2
#: An encode whose ffmpeg left before every frame was out — killed under
#: memory pressure, a pipe error, a crash; never a cancel — is started again
#: from its first missing span this many times, waiting ENCODE_BACKOFF_S,
#: then twice that, ... between attempts (``encode_spans``). Only after the
#: last one is the proxy marked failed, with the exit code and ffmpeg's last
#: words as the reason (Final QA r4: the early EOF raised out of the job, the
#: span was never built until something asked for it again).
ENCODE_RETRIES = 3
ENCODE_BACKOFF_S = 0.25

_log = logging.getLogger("video_ai_editor")


#: Spec §15 open decision 1, settled by measurement (2026-09-26, the 82
#: workdir masters = 42.3 source-minutes, 2 threads, nice 10, busy machine):
#:   crf 23: video 40.0 MB/source-min (66/82 sources <= 40), 11.5x realtime
#:   crf 24: video 35.9 MB/source-min (67/82 sources <= 40), 12.3x realtime
#: crf 23 sits exactly ON the 40 MB target; 24 meets it with margin for one
#: crf step of an all-intra PREVIEW picture. FLAC audio adds ~8 MB/min.
DEFAULT_CRF = 24


def crf() -> int:
    """Proxy crf (VAI_PROXY_CRF overrides DEFAULT_CRF)."""
    try:
        return max(0, min(51, int(os.environ.get("VAI_PROXY_CRF", str(DEFAULT_CRF)))))
    except ValueError:
        return DEFAULT_CRF


def recipe() -> dict:
    return {"version": RECIPE_VERSION, "codec": "h264", "intra": True,
            "stitchable": True, "pix_fmt": "yuv420p", "color": "bt709-tv",
            "short_edge": SHORT_EDGE, "crf": crf(), "preset": PRESET,
            "profile": PROFILE, "level": LEVEL, "timescale": TIMESCALE,
            "span_seconds": SPAN_SECONDS, "audio_rate": AUDIO_RATE,
            "audio_chunk_samples": AUDIO_CHUNK_SAMPLES}


def _recipe_tag() -> str:
    r = recipe()
    return f"v{r['version']}/crf{r['crf']}/se{r['short_edge']}/{r['preset']}"


PACK_FORMAT = ("u32be first, u32be count, u32be sizes[count], then count AVCC "
               "samples (4-byte big-endian NAL lengths)")

KEY_LEN = 24


class ProxyError(RuntimeError):
    """A proxy could not be built as specified (count mismatch, bad output)."""


class EncodeInterrupted(ProxyError):
    """ffmpeg's output ended before frames [f0, f1) were all out and nobody
    cancelled it (a non-zero exit, or the pipe closed in the middle of a
    box). Retryable: ``encode_spans`` starts again at ``resume_frame``."""

    def __init__(self, f0: int, f1: int, done: int, returncode: int | None, stderr_tail: str):
        self.f0, self.f1, self.frames_done = f0, f1, done
        self.returncode = returncode
        self.stderr_tail = stderr_tail
        super().__init__(f"proxy encode of frames [{f0}, {f1}) stopped early at frame {f0 + done} "
                         f"(ffmpeg exit {returncode}): {stderr_tail or 'no message'}")


# ---- where proxies live -----------------------------------------------------

def proxies_root() -> Path:
    """``WORKDIR/proxies`` — read per call so tests (and a relocated workdir)
    that monkeypatch ``storage.WORKDIR`` are honoured."""
    from .. import storage as _storage
    return Path(_storage.WORKDIR) / "proxies"


def is_valid_key(key: str) -> bool:
    return (isinstance(key, str) and len(key) == KEY_LEN
            and all(c in "0123456789abcdef" for c in key))


def proxy_dir(key: str) -> Path:
    if not is_valid_key(key):
        raise ValueError("invalid proxy key")
    return proxies_root() / key


def proxy_key(src: str | os.PathLike) -> str:
    """The proxy key of the file at ``src`` as it is NOW (realpath, size,
    mtime_ns, recipe). Raises OSError when the file is missing."""
    real = os.path.realpath(os.fspath(src))
    st = os.stat(real)
    ident = f"{real}\0{st.st_size}\0{st.st_mtime_ns}\0{_recipe_tag()}"
    return hashlib.sha256(ident.encode("utf-8", "surrogatepass")).hexdigest()[:KEY_LEN]


def span_path(key: str, n: int) -> Path:
    return proxy_dir(key) / "v" / f"{int(n):04d}.bin"


def chunk_path(key: str, n: int) -> Path:
    return proxy_dir(key) / "a" / f"{int(n):04d}.flac"


def _write_atomic(dst: Path, data: bytes) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dst.parent), prefix=".", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        _pu.replace_with_retry(tmp, dst)
    except BaseException:
        _pu.unlink_with_retry(tmp)
        raise


def _write_json(dst: Path, obj) -> None:
    _write_atomic(dst, json.dumps(obj, indent=1, sort_keys=True).encode("utf-8"))


def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---- probe: stream facts + the master pts table -----------------------------

@dataclass
class SourceInfo:
    key: str
    src: str                      # realpath
    width: int
    height: int
    pix_fmt: str
    rate: Fraction                # timebase.source_rate of the stream
    time_base: Fraction
    pts: list[int]                # decoded frames, presentation order
    keyframes: list[int]          # indices into `pts` of sync samples
    has_audio: bool
    audio_start: float = 0.0      # audio stream start_time − video start_time (s)
    extra: dict = field(default_factory=dict)

    @property
    def frames(self) -> int:
        return len(self.pts)

    @property
    def span_frames(self) -> int:
        return max(1, int(round(SPAN_SECONDS * self.rate)))

    @property
    def spans(self) -> int:
        return -(-self.frames // self.span_frames) if self.frames else 0

    @property
    def has_video(self) -> bool:
        return bool(self.pts)

    def pts_time(self, i: int) -> float:
        return float(self.pts[i] * self.time_base)

    def proxy_size(self) -> tuple[int, int]:
        if not self.has_video:
            return 0, 0
        return proxy_dimensions(self.width, self.height)

    def to_json(self) -> dict:
        return {"key": self.key, "src": self.src, "width": self.width,
                "height": self.height, "pix_fmt": self.pix_fmt,
                "rate": [self.rate.numerator, self.rate.denominator],
                "time_base": [self.time_base.numerator, self.time_base.denominator],
                "pts": self.pts, "keyframes": self.keyframes,
                "has_audio": self.has_audio, "audio_start": self.audio_start,
                "extra": self.extra}

    @classmethod
    def from_json(cls, d: dict) -> "SourceInfo":
        return cls(key=d["key"], src=d["src"], width=int(d["width"]),
                   height=int(d["height"]), pix_fmt=str(d.get("pix_fmt") or ""),
                   rate=Fraction(*d["rate"]), time_base=Fraction(*d["time_base"]),
                   pts=[int(x) for x in d["pts"]],
                   keyframes=[int(x) for x in d["keyframes"]],
                   has_audio=bool(d.get("has_audio")),
                   audio_start=float(d.get("audio_start") or 0.0),
                   extra=dict(d.get("extra") or {}))


def proxy_dimensions(width: int, height: int) -> tuple[int, int]:
    """(w, h) of the proxy: short edge min(source, SHORT_EDGE), both even."""
    w, h = max(2, int(width)), max(2, int(height))
    short = min(w, h)
    target = min(SHORT_EDGE, short)
    target -= target % 2
    if short == w:
        pw = target
        ph = int(round(h * target / w / 2.0)) * 2
    else:
        ph = target
        pw = int(round(w * target / h / 2.0)) * 2
    return max(2, pw), max(2, ph)


def _ffprobe_json(args: list[str]) -> dict:
    out = subprocess.run([_pu.FFPROBE, "-v", "error", *args, "-of", "json"],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace", check=True, **_pu.SUBPROCESS_FLAGS)
    return json.loads(out.stdout or "{}")


def _frac(text: str | None) -> Fraction | None:
    """ffprobe's ratios: ``30000/1001`` (rates, time bases) AND ``4:3``
    (sample_aspect_ratio uses a colon). ``0/0``, ``0:1``, ``N/A`` -> None."""
    if not text or text in ("0/0", "N/A"):
        return None
    text = text.replace(":", "/")
    try:
        if "/" in text:
            a, b = text.split("/", 1)
            return Fraction(int(a), int(b)) if int(b) and int(a) else None
        return Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None


def _packet_table(src: str) -> tuple[list[int], list[int]]:
    """(pts in presentation order, keyframe indices) of video stream 0, from
    the demuxer's packets — no decode. Packets the demuxer marks DISCARD (the
    pre-roll an mp4 edit list hides) are not frames the decoder outputs."""
    out = subprocess.run(
        [_pu.FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "packet=pts,dts,flags", "-of", "csv=p=0", src],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=True, **_pu.SUBPROCESS_FLAGS)
    rows: list[tuple[int, bool]] = []
    for line in out.stdout.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 3:
            continue
        pts_s, dts_s, flags = parts[0], parts[1], parts[2]
        if "D" in flags:
            continue
        ts = pts_s if pts_s not in ("", "N/A") else dts_s
        try:
            t = int(ts)
        except ValueError:
            continue
        rows.append((t, "K" in flags))
    rows.sort(key=lambda r: r[0])
    return [r[0] for r in rows], [i for i, r in enumerate(rows) if r[1]]


def probe_source(src: str | os.PathLike, key: str | None = None) -> SourceInfo:
    """Stream facts and the pts table of ``src`` (one ffprobe for streams,
    one packet scan). An audio-only source (a music bed, a voice-over) is a
    proxy with no frames and only FLAC chunks. Raises ProxyError when there
    is neither."""
    real = os.path.realpath(os.fspath(src))
    key = key or proxy_key(real)
    data = _ffprobe_json(["-show_entries",
                          "stream=index,codec_type,width,height,pix_fmt,r_frame_rate,"
                          "avg_frame_rate,time_base,start_time,sample_aspect_ratio,"
                          "color_range,color_space"
                          ":stream_side_data=rotation:stream_tags=rotate",
                          real])
    streams = data.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if v is None and a is None:
        raise ProxyError("no audio or video stream")
    if v is None:
        return SourceInfo(key=key, src=real, width=0, height=0, pix_fmt="",
                          rate=_tb.DEFAULT_RATE, time_base=Fraction(1, AUDIO_RATE),
                          pts=[], keyframes=[], has_audio=True,
                          extra={"audio_only": True})
    # The DISPLAYED size (render/sar.py: the export fits the same number):
    # square pixels, and the picture ffmpeg's autorotate decodes — every
    # decode the proxy encode makes is autorotated, so a quarter-turn
    # rotation swaps the sides (wave E; E1a notDone).
    from ..render.sar import display_size, rotation_of
    width, height = display_size(v)
    rotation = rotation_of(v)
    rate = _tb.source_rate(v.get("avg_frame_rate"), v.get("r_frame_rate"))
    tb = _frac(v.get("time_base")) or Fraction(1, 90000)
    pts, keys = _packet_table(real)
    if not pts:
        raise ProxyError("video stream has no frames")
    audio_start = 0.0
    if a is not None:
        try:
            audio_start = float(a.get("start_time") or 0.0) - float(v.get("start_time") or 0.0)
        except (TypeError, ValueError):
            audio_start = 0.0
    pix_fmt = str(v.get("pix_fmt") or "")
    # Colour facts the proxy's scale step converts FROM (spec R12): a full-
    # range or BT.601/BT.2020 master keeps its own levels through export, so
    # its BT.709-limited proxy must convert, not relabel.
    rng = str(v.get("color_range") or "")
    colour = {"color_range": "pc" if rng == "pc" or pix_fmt.startswith("yuvj") else
              (rng if rng in ("tv",) else "unknown"),
              "color_space": str(v.get("color_space") or "unknown")}
    if rotation:
        colour["rotation"] = rotation
    return SourceInfo(key=key, src=real, width=width, height=height,
                      pix_fmt=pix_fmt, rate=rate, time_base=tb,
                      pts=pts, keyframes=keys or [0], has_audio=a is not None,
                      audio_start=round(audio_start, 6), extra=colour)


def load_source(key: str) -> SourceInfo | None:
    d = _read_json(proxy_dir(key) / "source.json")
    try:
        return SourceInfo.from_json(d) if isinstance(d, dict) else None
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def save_source(info: SourceInfo) -> None:
    _write_json(proxy_dir(info.key) / "source.json", info.to_json())


def pts_digest(pts: list[int]) -> str:
    return hashlib.sha256(",".join(map(str, pts)).encode()).hexdigest()[:16]


# ---- index.json ---------------------------------------------------------------

def static_index(info: SourceInfo, *, avcc: bytes | None = None) -> dict:
    w, h = info.proxy_size()
    idx = {
        "version": 1, "key": info.key, "recipe": recipe(),
        "src_rate": {"num": info.rate.numerator, "den": info.rate.denominator},
        "frames": info.frames, "w": w, "h": h, "has_video": info.has_video,
        "src_w": info.width, "src_h": info.height, "src_pix_fmt": info.pix_fmt,
        "span_frames": info.span_frames, "spans": info.spans,
        "pack_format": PACK_FORMAT,
        "pts_digest": pts_digest(info.pts),
        "audio": audio_layout(info),
    }
    if avcc is not None:
        idx["codec"] = codec_string(avcc)
        # The init CLASS key: the avcC bytes as lowercase hex. One definition,
        # shared with the client (fmp4Writer.parseInitSegment().initKey), so
        # the index and the writer can never name one class two ways.
        idx["init_key"] = avcc.hex()
    return idx


def audio_layout(info: SourceInfo, samples: int | None = None,
                 gains: dict[str, float] | None = None,
                 peaks: list[float] | None = None) -> dict:
    """``chunk_gain`` is sparse ("n" -> the LINEAR power of two chunk n was
    divided by; absent = 1); ``gain_db`` is the largest of them in dB, for
    display (0.0: nothing to undo). The client multiplies by the linear
    value, never by a rounded dB figure, so the undo is exact.
    ``chunk_peak`` (once the sound is built) is every chunk's max |x| of the
    float decode, rounded UP to 1e-6: the client bounds the pre-limiter peak
    with it and calls a range whose bound tops the ceiling APPROX (the
    browser's limiter is not `alimiter`; gate RX finding 3)."""
    gains = dict(gains or {})
    out = {"rate": AUDIO_RATE, "channels": AUDIO_CHANNELS,
           "chunk_samples": AUDIO_CHUNK_SAMPLES, "silent": not info.has_audio,
           "start_s": info.audio_start, "samples": samples,
           "chunks": (-(-samples // AUDIO_CHUNK_SAMPLES) if samples else 0)
           if samples is not None else None,
           "gain_db": _db(max(gains.values(), default=1.0)), "chunk_gain": gains}
    if peaks is not None:
        out["chunk_peak"] = list(peaks)
    return out


def peak_up(peak: float) -> float:
    """`peak` rounded UP to 1e-6 (an upper bound that survives JSON)."""
    if not peak > 0.0 or peak != peak:
        return 0.0
    return math.ceil(peak * 1e6) / 1e6


def _db(linear: float) -> float:
    return round(20.0 * math.log10(linear), 6) if linear > 0 else 0.0


def headroom(peak: float) -> float:
    """The divisor for a chunk whose float peak is `peak`: the smallest power
    of two that brings it to <= full scale (1.0 when it already is), so
    dividing and multiplying back are exact in float and only the 24-bit
    rounding step (2^-23 x divisor) separates the chunk from ffmpeg's decode."""
    if not peak > 1.0 or peak != peak or peak == math.inf:
        return 1.0
    exp = 0
    while 2.0 ** exp < peak:
        exp += 1
    return 2.0 ** exp


def chunk_gain(key: str, n: int) -> float:
    """The linear gain chunk `n` of `key` must be multiplied by (1.0 normally)."""
    a = (read_index(key) or {}).get("audio") or {}
    try:
        return float((a.get("chunk_gain") or {}).get(str(int(n)), 1.0))
    except (TypeError, ValueError):
        return 1.0


def read_index(key: str) -> dict | None:
    d = _read_json(proxy_dir(key) / "index.json")
    return d if isinstance(d, dict) else None


def write_index(key: str, idx: dict) -> None:
    _write_json(proxy_dir(key) / "index.json", idx)


def live_index(key: str) -> dict | None:
    """index.json merged with what is on disk right now: per-span and
    per-chunk readiness and the overall state (spec §5.1 "span states")."""
    idx = read_index(key)
    if idx is None:
        return None
    d = proxy_dir(key)
    spans = int(idx.get("spans") or 0)
    ready = [(d / "v" / f"{n:04d}.bin").is_file() for n in range(spans)]
    audio = dict(idx.get("audio") or {})
    chunks = audio.get("chunks")
    if isinstance(chunks, int) and chunks > 0:
        audio["ready"] = [(d / "a" / f"{n:04d}.flac").is_file() for n in range(chunks)]
    out = {**idx, "span_ready": ready, "audio": audio}
    if idx.get("failed"):
        out["state"] = "failed"
    elif ((spans == 0 or (all(ready) and (d / "init.mp4").is_file()))
          and (audio.get("silent") or (chunks is not None and all(audio.get("ready") or [True])))):
        out["state"] = "ready"
    else:
        out["state"] = "partial" if any(ready) else "pending"
    return out


def mark_failed(key: str, message: str) -> None:
    idx = read_index(key) or {"version": 1, "key": key}
    idx["failed"] = True
    idx["error"] = str(message)[:500]
    write_index(key, idx)


def add_ref(key: str, sid: str) -> None:
    """Record that session `sid` uses this proxy (route access, spec §5.2)."""
    p = proxy_dir(key) / "refs.json"
    with _REFS_LOCK:
        refs = _read_json(p)
        refs = [r for r in refs if isinstance(r, str)] if isinstance(refs, list) else []
        if sid not in refs:
            _write_json(p, sorted({*refs, sid}))


def refs(key: str) -> list[str]:
    r = _read_json(proxy_dir(key) / "refs.json")
    return [x for x in r if isinstance(x, str)] if isinstance(r, list) else []


_REFS_LOCK = threading.Lock()


# ---- ISO-BMFF plumbing ----------------------------------------------------------

def _boxes(buf: bytes, off: int = 0, end: int | None = None) -> Iterator[tuple[str, int, int, int]]:
    """(type, box_offset, header_len, box_size) of each box in buf[off:end]."""
    end = len(buf) if end is None else end
    while off + 8 <= end:
        size, typ = struct.unpack(">I4s", buf[off:off + 8])
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", buf[off + 8:off + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - off
        if size < hdr:
            return
        yield typ.decode("latin1"), off, hdr, size
        off += size


_CONTAINERS = {"moov", "trak", "mdia", "minf", "stbl", "moof", "traf", "mvex", "edts", "dinf"}


def find_box(buf: bytes, path: list[str], off: int = 0, end: int | None = None
             ) -> tuple[int, int, int] | None:
    """(offset, header_len, size) of the first box at `path`."""
    head, *rest = path
    for typ, o, hdr, size in _boxes(buf, off, end):
        if typ != head:
            continue
        if not rest:
            return o, hdr, size
        inner = o + hdr
        if typ == "stsd":
            inner += 8
        elif typ in ("avc1", "avc3"):
            inner += 78
        found = find_box(buf, rest, inner, o + size)
        if found:
            return found
    return None


def avcc_of(init: bytes) -> bytes:
    loc = find_box(init, ["moov", "trak", "mdia", "minf", "stbl", "stsd", "avc1", "avcC"])
    if loc is None:
        raise ProxyError("init segment has no avcC")
    o, hdr, size = loc
    return init[o + hdr:o + size]


def mdhd_timescale(init: bytes) -> int:
    loc = find_box(init, ["moov", "trak", "mdia", "mdhd"])
    if loc is None:
        raise ProxyError("init segment has no mdhd")
    o, hdr, _ = loc
    ver = init[o + hdr]
    return struct.unpack(">I", init[o + hdr + (20 if ver == 1 else 12):][:4])[0]


def codec_string(avcc: bytes) -> str:
    """RFC 6381 codecs string, e.g. ``avc1.640029`` (High, level 4.1)."""
    return "avc1." + avcc[1:4].hex().upper()


def _trun_sizes(moof: bytes) -> list[int]:
    """Sample sizes of every trun in one moof (tfhd default size fallback)."""
    sizes: list[int] = []
    for typ, o, hdr, size in _boxes(moof):
        if typ != "moof":
            continue
        for t2, o2, h2, s2 in _boxes(moof, o + hdr, o + size):
            if t2 != "traf":
                continue
            default_size = 0
            for t3, o3, h3, _s3 in _boxes(moof, o2 + h2, o2 + s2):
                body = o3 + h3
                flags = int.from_bytes(moof[body + 1:body + 4], "big")
                if t3 == "tfhd":
                    p = body + 8
                    if flags & 0x01:
                        p += 8
                    if flags & 0x02:
                        p += 4
                    if flags & 0x08:
                        p += 4
                    if flags & 0x10:
                        default_size = struct.unpack(">I", moof[p:p + 4])[0]
                elif t3 == "trun":
                    count = struct.unpack(">I", moof[body + 4:body + 8])[0]
                    p = body + 8
                    if flags & 0x01:
                        p += 4
                    if flags & 0x04:
                        p += 4
                    for _ in range(count):
                        if flags & 0x100:
                            p += 4
                        if flags & 0x200:
                            sizes.append(struct.unpack(">I", moof[p:p + 4])[0])
                            p += 4
                        else:
                            sizes.append(default_size)
                        if flags & 0x400:
                            p += 4
                        if flags & 0x800:
                            p += 4
    return sizes


class FragmentReader:
    """Incremental parser for ffmpeg's fragmented-mp4 stdout: the init
    segment once, then every sample's bytes in decode (= presentation, all
    intra) order."""

    def __init__(self, read: Callable[[int], bytes]):
        self._read = read
        self.init = b""
        self._pending_sizes: list[int] = []
        #: The stream ended in the MIDDLE of a box (the pipe closed under a
        #: killed or crashed ffmpeg), not between two boxes.
        self.truncated = False

    def _exact(self, n: int) -> bytes:
        parts, left = [], n
        while left > 0:
            b = self._read(left)
            if not b:
                raise EOFError
            parts.append(b)
            left -= len(b)
        return b"".join(parts)

    def samples(self) -> Iterator[bytes]:
        init_parts: list[bytes] = []
        while True:
            try:
                head = self._exact(8)
            except EOFError:
                return
            size, typ_b = struct.unpack(">I4s", head)
            typ = typ_b.decode("latin1")
            try:
                if size == 1:
                    large = self._exact(8)
                    size = struct.unpack(">Q", large)[0]
                    head += large
                if size < len(head):
                    raise ProxyError(f"corrupt box {typ!r} in encoder output")
                body = self._exact(size - len(head))
            except EOFError:
                # the pipe closed inside this box: the samples before it are
                # good, the caller decides what the short count means
                self.truncated = True
                return
            if typ in ("ftyp", "moov"):
                init_parts.append(head + body)
                if typ == "moov":
                    self.init = b"".join(init_parts)
            elif typ == "moof":
                self._pending_sizes = _trun_sizes(head + body)
            elif typ == "mdat":
                off = 0
                for s in self._pending_sizes:
                    yield body[off:off + s]
                    off += s
                if off != len(body):
                    raise ProxyError("mdat size does not match its trun")
                self._pending_sizes = []


def pack_span(first: int, samples: list[bytes]) -> bytes:
    head = struct.pack(f">II{len(samples)}I", first, len(samples), *(len(s) for s in samples))
    return head + b"".join(samples)


def unpack_span(data: bytes) -> tuple[int, list[bytes]]:
    first, count = struct.unpack(">II", data[:8])
    sizes = struct.unpack(f">{count}I", data[8:8 + 4 * count])
    off = 8 + 4 * count
    out = []
    for s in sizes:
        out.append(data[off:off + s])
        off += s
    if off != len(data):
        raise ProxyError("span pack length mismatch")
    return first, out


# ---- video encode --------------------------------------------------------------

#: ffprobe color_space -> the swscale matrix it was encoded with. Anything
#: else (bt709, unknown, an untagged source) is read as BT.709.
_SWS_MATRIX = {"smpte170m": "bt601", "bt470bg": "bt601", "bt2020nc": "bt2020",
               "bt2020c": "bt2020", "smpte240m": "smpte240m", "fcc": "fcc"}


def scale_filter(info: SourceInfo, w: int, h: int) -> str:
    """The proxy's scale step. A source tagged full range or with a non-709
    matrix is CONVERTED to BT.709 limited here (the later setparams only
    labels); an untagged / BT.709-limited source gets the plain scale, so its
    pixels are exactly what they were before the conversion existed."""
    rng = "pc" if info.extra.get("color_range") == "pc" else "tv"
    mtx = _SWS_MATRIX.get(str(info.extra.get("color_space") or ""), "bt709")
    base = f"scale={w}:{h}:flags=bicubic"
    if rng == "tv" and mtx == "bt709":
        return base
    return (f"{base}:in_range={rng}:out_range=tv"
            f":in_color_matrix={mtx}:out_color_matrix=bt709")


def encode_args(info: SourceInfo, f0: int, f1: int) -> list[str]:
    """The ffmpeg argv that encodes master frames [f0, f1) (spec §5.1)."""
    if not (0 <= f0 < f1 <= info.frames):
        raise ValueError(f"bad frame range [{f0}, {f1}) of {info.frames}")
    w, h = info.proxy_size()
    frame_s = 1.0 / float(info.rate)
    pre: list[str] = []
    vf: list[str] = []
    if f0 > 0:
        # The nearest sync sample at or before f0; ffmpeg seeks the demuxer
        # there (-noaccurate_seek: no trimming of its own) and -copyts keeps
        # every frame's own timestamp so `trim` can select exactly by pts.
        # The pts table is ABSOLUTE; without -seek_timestamp ffmpeg adds the
        # container's start_time to -ss and lands past the span on any source
        # that does not start at 0 (a camera MTS, output_ts_offset).
        kf = max((k for k in info.keyframes if info.pts[k] <= info.pts[f0]), default=0)
        pre = ["-noaccurate_seek", "-seek_timestamp", "1",
               "-ss", f"{info.pts_time(kf) + frame_s / 4.0:.6f}"]
    if f0 > 0 or f1 < info.frames:
        # Cut at the MIDPOINT between neighbouring frames' pts: exact for any
        # frame spacing (a VFR AI output as much as a CFR master), where a
        # fixed half-frame margin could take in a neighbour of an uneven gap.
        trim = [f"start={(info.pts_time(f0 - 1) + info.pts_time(f0)) / 2:.6f}"] if f0 > 0 else []
        if f1 < info.frames:
            trim.append(f"end={(info.pts_time(f1 - 1) + info.pts_time(f1)) / 2:.6f}")
        vf.append("trim=" + ":".join(trim))
        vf.append("setpts=PTS-STARTPTS")
    vf.append(scale_filter(info, w, h))
    # Square pixels always: an anamorphic master was sized to its DISPLAYED
    # width above, and one SAR for every proxy keeps same-size SPSs equal.
    vf.append("setsar=1")
    vf.append("format=yuv420p")
    # Tag the FRAMES: ffmpeg 8 hands libx264 the frames' own colour
    # properties, and `-color_primaries/-color_trc` alone left the SPS VUI
    # saying "unknown" for both (measured; only matrix + range came through).
    vf.append("setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv")
    return [_pu.FFMPEG, "-nostdin", "-hide_banner", "-v", "error",
            "-threads", str(ENCODE_THREADS), *pre, "-copyts", "-i", info.src,
            "-map", "0:v:0", "-an", "-sn", "-dn", "-map_metadata", "-1",
            "-vf", ",".join(vf), "-fps_mode", "passthrough",
            "-c:v", "libx264", "-preset", PRESET, "-crf", str(crf()),
            "-profile:v", PROFILE, "-level:v", LEVEL,
            "-x264-params", X264_PARAMS, "-threads", str(ENCODE_THREADS),
            "-colorspace", "bt709", "-color_primaries", "bt709",
            "-color_trc", "bt709", "-color_range", "tv",
            "-video_track_timescale", str(TIMESCALE),
            "-f", "mp4", "-movflags", "+empty_moov+default_base_moof+frag_keyframe",
            "pipe:1"]


def _spawn(argv: list[str], low_priority: bool) -> subprocess.Popen:
    # Windows: a launcher's child (the real ffmpeg behind a Chocolatey shim)
    # must die with it, or a cancelled build goes on writing spans (CI round
    # 3: 15/15 spans after the cancel), so the process is in its kill job
    # before it runs. On POSIX this is `subprocess.Popen`. Every caller ends
    # with `_pu.release_process_tree`.
    if low_priority:
        return _pu.popen_in_tree(_pu.low_priority_argv(argv), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 **_pu.LOW_PRIORITY_SUBPROCESS_FLAGS)
    return _pu.popen_in_tree(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, **_pu.SUBPROCESS_FLAGS)


class _EventLike:  # pragma: no cover - typing aid only
    def is_set(self) -> bool: ...


class Cancelled(Exception):
    """The job's cancel event was set (superseded, paused for export, shutdown)."""


def _watch(proc: subprocess.Popen, cancel: "threading.Event | _EventLike | None",
           stop: threading.Event) -> None:
    while not stop.wait(0.05):
        if cancel is not None and cancel.is_set():
            try:
                _pu.kill_process_tree(proc)
            except OSError:
                pass
            return


def _drain(stream, sink: list[bytes]) -> None:
    for chunk in iter(lambda: stream.read(4096), b""):
        if sum(len(c) for c in sink) < 16384:
            sink.append(chunk)


def run_encode(info: SourceInfo, f0: int, f1: int, *,
               on_init: Callable[[bytes], None] | None = None,
               on_span: Callable[[int, list[bytes]], None],
               cancel: "threading.Event | _EventLike | None" = None,
               low_priority: bool = True) -> int:
    """Encode frames [f0, f1) (f0 span-aligned) and hand each finished span
    to ``on_span(n, samples)`` as soon as its frames are out. Returns the
    sample count, which is asserted to be ``f1 - f0``."""
    S = info.span_frames
    if f0 % S:
        raise ValueError("f0 must be span aligned")
    proc = _spawn(encode_args(info, f0, f1), low_priority)
    err: list[bytes] = []
    stop = threading.Event()
    watcher = threading.Thread(target=_watch, args=(proc, cancel, stop), daemon=True)
    drainer = threading.Thread(target=_drain, args=(proc.stderr, err), daemon=True)
    watcher.start()
    drainer.start()
    count = 0
    buf: list[bytes] = []
    n = f0 // S
    overflow = False
    try:
        reader = FragmentReader(proc.stdout.read)
        init_seen = False
        for sample in reader.samples():
            if not init_seen:
                init_seen = True
                if on_init is not None:
                    on_init(reader.init)
            if f0 + count >= f1:
                overflow = True
                _pu.kill_process_tree(proc)   # it is blocked writing the extra frames
                break
            buf.append(sample)
            count += 1
            if len(buf) == S:
                on_span(n, buf)
                n += 1
                buf = []
        if buf and not overflow and f0 + count == f1:
            on_span(n, buf)
        proc.wait()
    finally:
        stop.set()
        if proc.poll() is None:
            _pu.kill_process_tree(proc)
            proc.wait()
        watcher.join(timeout=1)         # its kill is over before the job handle closes
        _pu.release_process_tree(proc)
        drainer.join(timeout=2)
    if cancel is not None and cancel.is_set():
        raise Cancelled()
    if overflow:
        raise ProxyError(f"proxy encode of frames [{f0}, {f1}) produced more than "
                         f"{f1 - f0} frames (frame identity would break)")
    tail = b"".join(err).decode("utf-8", "replace")[-400:].strip()
    if count < f1 - f0 and (proc.returncode != 0 or reader.truncated):
        # ffmpeg left early (killed, crashed, the pipe broke): not a fact
        # about the source, so the caller may try again from where it stopped
        raise EncodeInterrupted(f0, f1, count, proc.returncode, tail)
    if proc.returncode != 0:
        raise ProxyError(f"proxy encode failed ({proc.returncode}): {tail}")
    if count != f1 - f0:
        raise ProxyError(f"proxy encode of frames [{f0}, {f1}) produced {count} "
                         f"frames (frame identity would break)")
    return count


def _backoff_wait(cancel: "threading.Event | _EventLike | None", seconds: float) -> None:
    """Sleep ``seconds`` between two attempts, leaving at once on a cancel."""
    deadline = time.monotonic() + seconds
    while True:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        left = deadline - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(0.05, left))


def encode_spans(info: SourceInfo, f0: int, f1: int, *,
                 on_init: Callable[[bytes], None] | None = None,
                 on_span: Callable[[int, list[bytes]], None],
                 cancel: "threading.Event | _EventLike | None" = None,
                 low_priority: bool = True,
                 retries: int = ENCODE_RETRIES) -> int:
    """``run_encode`` that survives an ffmpeg leaving early: an
    :class:`EncodeInterrupted` (the pipe closed mid-box, a non-zero exit with
    frames still owed — never a cancel) is logged with its exit code and
    stderr tail, and the encode starts again at the first span it did not hand
    out, ``retries`` times with backoff. Then it is a :class:`ProxyError`
    naming the attempts, the exit code and ffmpeg's last words — what
    ``mark_failed`` stores and the proxy route hands the UI."""
    S = info.span_frames
    start = f0
    total = 0
    attempt = 1
    while True:
        handed = [start]

        def hand(n: int, samples: list[bytes], _handed=handed) -> None:
            on_span(n, samples)
            _handed[0] = (n + 1) * S

        try:
            total += run_encode(info, start, f1, on_init=on_init, on_span=hand,
                                cancel=cancel, low_priority=low_priority)
            return total
        except EncodeInterrupted as e:
            done = min(handed[0], f1) - start
            total += done
            start = min(handed[0], f1)
            if attempt > retries:
                raise ProxyError(f"proxy encode stopped early {attempt} times, last at frame "
                                 f"{e.f0 + e.frames_done} of [{f0}, {f1}) (ffmpeg exit "
                                 f"{e.returncode}): {e.stderr_tail or 'no message'}") from e
            delay = ENCODE_BACKOFF_S * 2 ** (attempt - 1)
            _log.warning("proxy encode of %s frames [%d, %d) stopped early at frame %d (ffmpeg exit %s): "
                         "%s; retry %d/%d from frame %d in %.2f s",
                         info.key, e.f0, e.f1, e.f0 + e.frames_done, e.returncode,
                         e.stderr_tail or "no message", attempt, retries, start, delay)
            attempt += 1
            _backoff_wait(cancel, delay)


def init_path(key: str) -> Path:
    return proxy_dir(key) / "init.mp4"


def accept_init(info: SourceInfo, init: bytes) -> None:
    """Keep the first init segment; every later encode must match its avcC
    (stitchability is what lets the client mix spans of two encodes)."""
    p = init_path(info.key)
    avcc = avcc_of(init)
    if mdhd_timescale(init) != TIMESCALE:
        raise ProxyError("proxy init segment has the wrong timescale")
    if find_box(init, ["moov", "trak", "edts"]) is not None:
        raise ProxyError("proxy init segment carries an edit list")
    with _INIT_LOCK:
        if p.is_file():
            if avcc_of(p.read_bytes()) != avcc:
                raise ProxyError("proxy encodes disagree on SPS/PPS (not stitchable)")
            return
        _write_atomic(p, init)
        idx = read_index(info.key) or static_index(info)
        full = static_index(info, avcc=avcc)
        idx.update({"codec": full["codec"], "init_key": full["init_key"]})
        write_index(info.key, idx)


_INIT_LOCK = threading.Lock()


def write_span(key: str, n: int, first: int, samples: list[bytes]) -> Path:
    p = span_path(key, n)
    if not p.is_file():
        _write_atomic(p, pack_span(first, samples))
    return p


# ---- audio ------------------------------------------------------------------------

def build_audio(info: SourceInfo, *, cancel: "threading.Event | _EventLike | None" = None,
                low_priority: bool = True) -> dict:
    """Decode the master's first audio stream with ffmpeg to 48 kHz stereo
    float, slice it into exact AUDIO_CHUNK_SAMPLES chunks and store each as
    24-bit FLAC. Returns the audio layout (also written into index.json)."""
    import numpy as np
    import soundfile as sf

    key = info.key
    d = proxy_dir(key) / "a"
    if not info.has_audio:
        layout = audio_layout(info, samples=0)
        _merge_index(info, {"audio": layout})
        return layout
    argv = [_pu.FFMPEG, "-nostdin", "-hide_banner", "-v", "error", "-i", info.src,
            "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-af", AUDIO_FILTER,
            "-f", "f32le", "pipe:1"]
    proc = _spawn(argv, low_priority)
    err: list[bytes] = []
    stop = threading.Event()
    watcher = threading.Thread(target=_watch, args=(proc, cancel, stop), daemon=True)
    drainer = threading.Thread(target=_drain, args=(proc.stderr, err), daemon=True)
    watcher.start()
    drainer.start()
    frame_bytes = 4 * AUDIO_CHANNELS
    want = AUDIO_CHUNK_SAMPLES * frame_bytes
    total = 0
    n = 0
    peaks: list[float] = []
    gains: dict[str, float] = dict(((read_index(key) or {}).get("audio") or {})
                                   .get("chunk_gain") or {})
    d.mkdir(parents=True, exist_ok=True)
    try:
        while True:
            parts, got = [], 0
            while got < want:
                b = proc.stdout.read(want - got)
                if not b:
                    break
                parts.append(b)
                got += len(b)
            got -= got % frame_bytes
            if got == 0:
                break
            pcm = np.frombuffer(b"".join(parts)[:got], dtype="<f4").reshape(-1, AUDIO_CHANNELS)
            peak = float(np.max(np.abs(pcm))) if pcm.size else 0.0
            peaks.append(peak_up(peak))
            dst = d / f"{n:04d}.flac"
            if not dst.is_file():
                # The export mixes the UNCLIPPED float decode; 24-bit PCM
                # would hard-clip an over. Store it with power-of-two headroom
                # and publish the gain BEFORE the chunk becomes servable.
                div = headroom(peak)
                if div != 1.0:
                    pcm = pcm / np.float32(div)
                    gains[str(n)] = div
                    _merge_audio(info, {"chunk_gain": dict(gains),
                                        "gain_db": _db(max(gains.values()))})
                fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".", suffix=".flac")
                os.close(fd)
                try:
                    sf.write(tmp, pcm, AUDIO_RATE, subtype="PCM_24", format="FLAC")
                    _pu.replace_with_retry(tmp, dst)
                except BaseException:
                    _pu.unlink_with_retry(tmp)
                    raise
            total += pcm.shape[0]
            n += 1
            if got < want:
                break
        proc.wait()
    finally:
        stop.set()
        if proc.poll() is None:
            _pu.kill_process_tree(proc)
            proc.wait()
        watcher.join(timeout=1)         # its kill is over before the job handle closes
        _pu.release_process_tree(proc)
        drainer.join(timeout=2)
    if cancel is not None and cancel.is_set():
        raise Cancelled()
    if proc.returncode != 0:
        tail = b"".join(err).decode("utf-8", "replace")[-400:]
        raise ProxyError(f"audio decode failed ({proc.returncode}): {tail}")
    layout = audio_layout(info, samples=total, gains=gains, peaks=peaks)
    _merge_index(info, {"audio": layout})
    return layout


def _merge_index(info: SourceInfo, patch: dict) -> None:
    with _INIT_LOCK:
        idx = read_index(info.key) or static_index(info)
        idx.update(patch)
        write_index(info.key, idx)


def _merge_audio(info: SourceInfo, patch: dict) -> None:
    with _INIT_LOCK:
        idx = read_index(info.key) or static_index(info)
        idx["audio"] = {**(idx.get("audio") or audio_layout(info)), **patch}
        write_index(info.key, idx)


def audio_done(key: str) -> bool:
    idx = read_index(key) or {}
    a = idx.get("audio") or {}
    if a.get("silent"):
        return a.get("samples") is not None
    chunks = a.get("chunks")
    if not isinstance(chunks, int) or a.get("samples") is None:
        return False
    return all(chunk_path(key, n).is_file() for n in range(chunks))


def missing_spans(key: str, info: SourceInfo) -> list[int]:
    return [n for n in range(info.spans) if not span_path(key, n).is_file()]


def span_range(info: SourceInfo, n: int) -> tuple[int, int]:
    S = info.span_frames
    return n * S, min(info.frames, (n + 1) * S)


def media_facts(info: SourceInfo | None) -> dict:
    """The /media row fields spec §5.2 adds (fps, frames, pix_fmt, has_audio)."""
    if info is None:
        return {}
    return {"fps": {"num": info.rate.numerator, "den": info.rate.denominator},
            "frames": info.frames, "pix_fmt": info.pix_fmt,
            "has_audio": info.has_audio}


__all__ = [
    "RECIPE_VERSION", "SHORT_EDGE", "SPAN_SECONDS", "AUDIO_RATE", "AUDIO_CHUNK_SAMPLES", "AUDIO_FILTER",
    "TIMESCALE", "ProxyError", "EncodeInterrupted", "Cancelled", "SourceInfo", "proxies_root", "proxy_dir",
    "proxy_key", "is_valid_key", "probe_source", "load_source", "save_source",
    "static_index", "live_index", "read_index", "write_index", "mark_failed",
    "add_ref", "refs", "run_encode", "encode_spans", "ENCODE_RETRIES", "ENCODE_BACKOFF_S",
    "accept_init", "write_span", "build_audio",
    "audio_done", "missing_spans", "span_range", "span_path", "chunk_path",
    "init_path", "pack_span", "unpack_span", "avcc_of", "codec_string",
    "proxy_dimensions", "media_facts", "recipe", "crf", "encode_args",
    "FragmentReader", "mdhd_timescale", "find_box", "headroom", "chunk_gain",
    "scale_filter",
]

