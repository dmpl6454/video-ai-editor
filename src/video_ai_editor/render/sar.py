"""Non-square-pixel (anamorphic) sources: the ONE place their display size
is decided (Wave D3, lane E1a).

A PAL DV frame is stored 720x576 but DISPLAYED 768x576 (sample aspect 16:15)
or 1024x576 (64:45); HDV stores 1440x1080 and displays 1920x1080 (4:3).
ffmpeg's `scale=W:H:force_original_aspect_ratio=…` sizes from the STORED
width, so the export fitted such a clip as a 5:4 (or 4:3) picture: squeezed.
Ingest's `normalize` squares the pixels of every upload, but a clip can still
reference a raw file (an agent/MCP `add_clip` path, an AI output, a project
from before normalize squared them), and the preview engine has always drawn
the DISPLAY shape (its proxies are square-pixel at the displayed width).

The rule here is the proxy's (`ingest/proxy.probe_source`): displayed width =
the stored width x SAR, rounded to the nearest even number; the height is
kept. The export fits that size (`compositor._build_clip_video_chain`), the
overlays square it first (`pip.py`), the reversed intermediate keeps the SAR
(`reverse.py`), and the engine's `SourceInfo` w/h is the same number — so
every renderer places the same picture, integer for integer.

Square-pixel and unknown-SAR sources (`1:1`, `0:1`, `N/A`) are None: their
filter text is untouched, which is what keeps every existing render
byte-identical.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from fractions import Fraction

from .. import platformutil as _pu
from . import cancel as _cancel

_log = logging.getLogger(__name__)


def parse_sar(text: str | None) -> Fraction | None:
    """ffprobe's `sample_aspect_ratio` (``16:15``, ``16/15``); None for a
    square, unknown or malformed value."""
    if not text:
        return None
    raw = str(text).strip().replace(":", "/")
    try:
        a, b = raw.split("/", 1)
        num, den = int(a), int(b)
    except ValueError:
        return None
    if num <= 0 or den <= 0 or num == den:
        return None
    return Fraction(num, den)


def display_width(width: int, sar: Fraction | None) -> int:
    """The DISPLAYED width of a `width`-wide frame with sample aspect `sar`
    (even, nearest; `width` itself for square pixels) — the proxy's rule."""
    if sar is None or sar <= 0 or sar == 1:
        return int(width)
    return int(round(int(width) * Fraction(sar) / 2)) * 2


@dataclass(frozen=True)
class Anamorphic:
    """A non-square-pixel source as the filter graph sees its frames (after
    ffmpeg's autorotate): stored `width` x `height`, sample aspect `sar`,
    displayed `display_w` x `height`."""
    width: int
    height: int
    sar: Fraction

    @property
    def display_w(self) -> int:
        return display_width(self.width, self.sar)

    @property
    def display_h(self) -> int:
        return self.height

    @property
    def sar_text(self) -> str:
        return f"{self.sar.numerator}/{self.sar.denominator}"


def _rotation(stream: dict) -> int:
    rot = 0
    for sd in stream.get("side_data_list") or []:
        if isinstance(sd, dict) and "rotation" in sd:
            try:
                rot = int(round(float(sd["rotation"])))
            except (TypeError, ValueError):
                pass
    if not rot:
        try:
            rot = int(round(float((stream.get("tags") or {}).get("rotate") or 0)))
        except (TypeError, ValueError):
            rot = 0
    return rot % 360


def anamorphic_from_stream(stream: dict) -> Anamorphic | None:
    """From one ffprobe video stream (width, height, sample_aspect_ratio,
    side_data rotation): the frame the filter graph receives. A quarter
    turn (autorotate's transpose) swaps the sides and inverts the SAR."""
    sar = parse_sar(stream.get("sample_aspect_ratio"))
    if sar is None:
        return None
    w, h = int(stream.get("width") or 0), int(stream.get("height") or 0)
    if w <= 0 or h <= 0:
        return None
    if _rotation(stream) in (90, 270):
        w, h, sar = h, w, 1 / sar
    return Anamorphic(width=w, height=h, sar=sar)


def display_size(stream: dict) -> tuple[int, int]:
    """The DISPLAYED `(width, height)` of one ffprobe video stream: the frame
    ffmpeg's autorotate hands every decode (a quarter turn of the rotation
    side data / `rotate` tag swaps the sides and inverts the SAR), at square
    pixels (`display_width`). What the proxy is sized to (wave E: a phone
    clip shot upright, stored 1920x1080 with a 90° display matrix, was a
    1280x720 proxy of a 1080x1920 picture — squashed)."""
    w, h = int(stream.get("width") or 0), int(stream.get("height") or 0)
    sar = parse_sar(stream.get("sample_aspect_ratio"))
    if _rotation(stream) in (90, 270):
        w, h, sar = h, w, (1 / sar if sar is not None else None)
    return display_width(w, sar), h


def rotation_of(stream: dict) -> int:
    """The stream's display rotation in degrees (0, 90, 180 or 270)."""
    return _rotation(stream)


#: Answers of `_probe`, keyed on the file's identity: only a probe that RAN
#: is remembered (review RD3 — a timeout cached as None made an anamorphic
#: source render squeezed for the rest of the process).
_PROBES: "OrderedDict[tuple[str, int, int], Anamorphic | None]" = OrderedDict()
_PROBES_LOCK = threading.Lock()
_PROBES_MAX = 512


def _probe(src: str, mtime_ns: int, size: int) -> Anamorphic | None:
    key = (src, mtime_ns, size)
    with _PROBES_LOCK:
        if key in _PROBES:
            _PROBES.move_to_end(key)
            return _PROBES[key]
    try:
        # At the render's priority (niced under priority=low, like every
        # other probe a render makes; outside any cancel scope, so a
        # superseding edit cannot kill it into a wrong answer).
        out = _cancel.run_prioritised(
            [_pu.FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,sample_aspect_ratio:"
                              "stream_side_data=rotation:stream_tags=rotate",
             "-of", "json", src],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        streams = json.loads(out.stdout or "{}").get("streams") or []
    except Exception as e:  # noqa: BLE001 — unknown this time: square, and asked again next render
        _log.warning("sar probe of %s failed (%s); treating it as square for this render", src, e)
        return None
    ans = anamorphic_from_stream(streams[0]) if streams else None
    with _PROBES_LOCK:
        _PROBES[key] = ans
        while len(_PROBES) > _PROBES_MAX:
            _PROBES.popitem(last=False)
    return ans


_probe.cache_clear = _PROBES.clear   # type: ignore[attr-defined]  (the lru_cache spelling tests use)


def source_anamorphic(src: str | os.PathLike | None) -> Anamorphic | None:
    """The anamorphic facts of `src`'s first video stream, or None when its
    pixels are square (or it cannot be probed). Cached on path+mtime+size."""
    if not src:
        return None
    try:
        st = os.stat(src)
    except OSError:
        return None
    return _probe(os.fspath(src), st.st_mtime_ns, st.st_size)


def square_pixels_filter(src: str | os.PathLike | None) -> str:
    """Filters (trailing comma) that resample an anamorphic source to square
    pixels at its displayed size, or "" for a square-pixel one. For chains
    that size a picture from `iw`/`ih` downstream (the PIP's `h=-1` and its
    cover scale), where one explicit fit is not available."""
    ana = source_anamorphic(src)
    if ana is None:
        return ""
    return f"scale={ana.display_w}:{ana.display_h},setsar=1,"


def rescale_near(a: int, b: int, c: int) -> int:
    """`av_rescale(a, b, c)` for positive values: a·b/c, nearest, ties away
    from zero — what `force_original_aspect_ratio` computes (geometry.ts
    `rescaleNear`)."""
    return (2 * a * b + c) // (2 * c)


def fit_dims(src_w: int, src_h: int, w: int, h: int, mode: str) -> tuple[int, int]:
    """`scale=w:h:force_original_aspect_ratio=decrease|increase` of a
    `src_w` x `src_h` SQUARE-pixel frame — ffmpeg's integers, as geometry.ts
    `fitDims` mirrors them — so an anamorphic source can be scaled to the
    size its DISPLAYED shape would get, in one resample."""
    tmp_w = rescale_near(h, src_w, src_h)
    tmp_h = rescale_near(w, src_h, src_w)
    if mode == "decrease":
        return min(tmp_w, w), min(tmp_h, h)
    return max(tmp_w, w), max(tmp_h, h)


__all__ = ["Anamorphic", "anamorphic_from_stream", "display_width", "fit_dims",
           "parse_sar", "rescale_near", "source_anamorphic", "square_pixels_filter"]
