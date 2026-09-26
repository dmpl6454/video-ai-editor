"""ffprobe wrapper — read duration, streams, codec, fps, etc."""
from __future__ import annotations
import json
import os
import subprocess
import threading
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar
from pydantic import BaseModel

from .. import platformutil as _pu


class ProbeStream(BaseModel):
    index: int
    codec_type: str
    codec_name: str | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    avg_frame_rate: str | None = None
    # The container's nominal base rate. Together with avg_frame_rate it is how
    # ingest tells a VFR phone clip (nominal 30, average 29.98) from a 29.97
    # one — see edl/timebase.source_rate.
    r_frame_rate: str | None = None
    sample_rate: int | None = None
    channels: int | None = None


class ProbeResult(BaseModel):
    duration: float
    format_name: str
    bit_rate: int | None = None
    streams: list[ProbeStream] = []

    @property
    def video(self) -> ProbeStream | None:
        return next((s for s in self.streams if s.codec_type == "video"), None)

    @property
    def audio(self) -> ProbeStream | None:
        return next((s for s in self.streams if s.codec_type == "audio"), None)

    @property
    def fps(self) -> float | None:
        v = self.video
        if v and v.avg_frame_rate and "/" in v.avg_frame_rate:
            num, den = v.avg_frame_rate.split("/")
            try:
                d = float(den)
                return float(num) / d if d else None
            except Exception:
                return None
        return None


# ---------------------------------------------------------------- the cache
#
# Every committed edit that validates against its source (trim_clip clamps to
# the picture's length: probe() + video_frame_extent()) ran ffprobe twice,
# ~25 ms each — most of a client-mode trim's 80 ms edit-to-picture budget
# (instant preview §11.1). The answers only change when the file does, so they
# are kept per file IDENTITY: (realpath, size, mtime_ns). A failed probe is
# never kept (the next call asks again), and probe() hands every caller its
# own copy, so no caller can change another's answer.

_CACHE_MAX = 512
_cache: OrderedDict[tuple, object] = OrderedDict()
_cache_lock = threading.Lock()
_T = TypeVar("_T")


def clear_cache() -> None:
    """Forget every cached answer (tests)."""
    with _cache_lock:
        _cache.clear()


def _identity(path: Path) -> tuple | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (os.path.realpath(path), st.st_size, st.st_mtime_ns)


def _cached(kind: str, path: Path, compute: Callable[[], _T], keep: Callable[[_T], bool]) -> _T:
    ident = _identity(path)
    if ident is None:
        return compute()
    key = (kind, *ident)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]  # type: ignore[return-value]
    value = compute()
    if keep(value):
        with _cache_lock:
            _cache[key] = value
            while len(_cache) > _CACHE_MAX:
                _cache.popitem(last=False)
    return value


def probe(path: Path) -> ProbeResult:
    """ffprobe's format and streams of ``path`` (cached per file identity;
    raises like ffprobe on a file it cannot read)."""
    return _cached("probe", path, lambda: _probe(path), lambda r: True).model_copy(deep=True)


def _probe(path: Path) -> ProbeResult:
    out = subprocess.run(
        [
            _pu.FFPROBE, "-v", "error",
            "-show_format", "-show_streams",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
        **_pu.SUBPROCESS_FLAGS,
    )
    data = json.loads(out.stdout)
    fmt = data.get("format", {})
    streams = [
        ProbeStream(
            index=s.get("index", 0),
            codec_type=s.get("codec_type", ""),
            codec_name=s.get("codec_name"),
            width=s.get("width"),
            height=s.get("height"),
            duration=float(s["duration"]) if s.get("duration") else None,
            avg_frame_rate=s.get("avg_frame_rate"),
            r_frame_rate=s.get("r_frame_rate"),
            sample_rate=int(s["sample_rate"]) if s.get("sample_rate") else None,
            channels=s.get("channels"),
        )
        for s in data.get("streams", [])
    ]
    return ProbeResult(
        duration=float(fmt.get("duration", 0.0)),
        format_name=fmt.get("format_name", ""),
        bit_rate=int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
        streams=streams,
    )


def video_frame_extent(path: Path) -> float | None:
    """Cached per file identity; see ``_video_frame_extent``."""
    return _cached("extent", path, lambda: _video_frame_extent(path), lambda v: v is not None)


def _video_frame_extent(path: Path) -> float | None:
    """Seconds of PICTURE in ``path``'s first video stream, frame-exact:
    ``nb_frames / avg_frame_rate`` when the container counts its frames,
    else the video stream's own duration. None when there is no video.

    QA-002: an import used ``format.duration`` as the clip's ``out``, and for
    an AAC-normalised mp4 that is the AUDIO's padded length — a 600-frame,
    20.000 s picture came in as out=20.01, and appended clips drifted 40 ms
    by the ninth. Callers floor this to the project grid with
    ``edl.timebase.floor_to_frame``.
    """
    try:
        out = subprocess.run(
            [_pu.FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=nb_frames,avg_frame_rate,r_frame_rate,duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, **_pu.SUBPROCESS_FLAGS,
        )
        streams = json.loads(out.stdout or "{}").get("streams") or []
    except Exception:
        return None
    if not streams:
        return None
    s = streams[0]

    def _rate(txt: str | None) -> float | None:
        try:
            num, den = str(txt).split("/")
            r = float(num) / float(den)
            return r if r > 0 else None
        except Exception:
            return None

    rate = _rate(s.get("avg_frame_rate")) or _rate(s.get("r_frame_rate"))
    try:
        n = int(s.get("nb_frames") or 0)
    except (TypeError, ValueError):
        n = 0
    if n > 0 and rate:
        return n / rate
    try:
        d = float(s.get("duration") or 0.0)
    except (TypeError, ValueError):
        d = 0.0
    return d if d > 0 else None
