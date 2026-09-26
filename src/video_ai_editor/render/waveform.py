"""Audio waveform peaks for the timeline.

Each peak is the max |sample| over its bucket, across EVERY channel, of the
source decoded at 48 kHz — the true sample peak, whatever the file's length,
content or channel layout. Cached to JSON under the session cache.

QA-081 — what this used to do, and why each part was wrong:
  * decoded at `peaks_per_sec × 200` Hz (10 kHz at 50 pps, 1 kHz for a
    12-minute file whose density was lowered to fit the cap): everything above
    that Nyquist was filtered out before a single peak was taken — a 3 kHz
    tone read 0.021 at 30 s and 0.0 at 200 s, an 8 kHz tone never showed, and
    a 12-minute narration peaked at 0.503 against a true 0.754;
  * `-ac 1` averaged the channels, so a left-only tone drew at half height.
The density cap stays (the JSON and the redraw stay bounded) but is far
higher, and a lower density only coarsens WHEN a peak is, never how tall it is.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
from pathlib import Path

from .. import platformutil as _pu

DEFAULT_PEAKS_PER_SEC = 50
#: Cap on the peaks returned for one source: 30 min at the default density.
MAX_PEAKS = 90_000
DECODE_RATE = 48_000
#: Bump when the peak math changes, so a cached pre-fix waveform is not served.
_WAVE_VERSION = 3   # 3: per-side peaks (QA-122), keyed on the requested density (QA-132)
#: Densities that divide DECODE_RATE exactly, so `idx = floor(t × pps)` on the
#: client lands on the bucket that holds t, at any length.
_PPS_LADDER = (100, 80, 60, 50, 48, 40, 32, 30, 25, 24, 20, 16, 15, 12, 10, 8, 6, 5, 4, 3, 2, 1)


def _key(src: Path, peaks_per_sec: int) -> str:
    try:
        st = src.stat()
        ident = f"{st.st_size}|{st.st_mtime_ns}"
    except OSError:
        ident = ""
    return hashlib.sha256(f"v{_WAVE_VERSION}|{src}|{ident}|{peaks_per_sec}".encode()).hexdigest()[:16]


def _effective_pps(duration_hint: float, requested: int) -> int:
    """The densest ladder rate ≤ `requested` that keeps a source of
    `duration_hint` seconds within MAX_PEAKS."""
    cap = requested
    if duration_hint > 0:
        cap = min(cap, int(MAX_PEAKS / max(1.0, duration_hint)))
    for pps in _PPS_LADDER:
        if pps <= max(1, cap):
            return pps
    return 1


def _probe_audio(src: Path) -> tuple[int, float]:
    """(channels of the first audio stream, container duration); (0, 0.0)
    when there is no audio stream."""
    try:
        proc = subprocess.run(
            [_pu.FFPROBE, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=channels:format=duration", "-of", "json", str(src)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, **_pu.SUBPROCESS_FLAGS)
        d = json.loads(proc.stdout or "{}")
    except Exception:
        return 0, 0.0
    streams = d.get("streams") or []
    ch = int(streams[0].get("channels") or 0) if streams else 0
    try:
        dur = float((d.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        dur = 0.0
    return ch, dur


def _bucket_peaks(src: Path, channels: int, pps: int
                  ) -> tuple[list[float], list[list[float]], int]:
    """Stream-decode `src` at DECODE_RATE (every channel kept) and reduce it to
    one max-|x| per bucket of DECODE_RATE/pps samples: across every channel,
    and — for a 2+ channel source — per side, L being channel 0 and R channel
    1 (QA-122). Returns (peaks, [peaks_l, peaks_r] or [], samples)."""
    import numpy as np
    bucket = DECODE_RATE // pps
    frame_bytes = 4 * channels
    block = bucket * frame_bytes * 50            # ~50 buckets per read
    proc = subprocess.Popen(
        [_pu.FFMPEG, "-v", "error", "-i", str(src), "-map", "0:a:0", "-vn",
         "-ar", str(DECODE_RATE), "-ac", str(channels), "-f", "f32le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **_pu.SUBPROCESS_FLAGS)
    peaks: list[float] = []
    sides: list[list[float]] = [[], []] if channels >= 2 else []
    carry = b""
    total = 0

    def reduce(a) -> None:            # a: (buckets, samples-per-bucket, channels)
        ab = np.abs(a)
        peaks.extend(ab.max(axis=(1, 2)).tolist())
        for k, side in enumerate(sides):
            side.extend(ab[:, :, k].max(axis=1).tolist())
    try:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(block)
            if not chunk:
                break
            buf = carry + chunk
            whole = len(buf) // (bucket * frame_bytes) * (bucket * frame_bytes)
            carry = buf[whole:]
            if whole:
                reduce(np.frombuffer(buf[:whole], dtype="<f4").reshape(-1, bucket, channels))
                total += whole // frame_bytes
        tail = carry[: len(carry) // frame_bytes * frame_bytes]
        if tail:
            reduce(np.frombuffer(tail, dtype="<f4").reshape(1, -1, channels))
            total += len(tail) // frame_bytes
    finally:
        proc.stdout and proc.stdout.close()
        proc.wait()
    if proc.returncode != 0 and not peaks:
        return [], [], 0
    rnd = lambda xs: [round(min(1.0, p), 4) for p in xs]  # noqa: E731
    return rnd(peaks), [rnd(x) for x in sides], total


def waveform_peaks(src: Path, cache_dir: Path,
                   *, peaks_per_sec: int = DEFAULT_PEAKS_PER_SEC) -> dict:
    """Return {peaks: [0..1 floats], peaks_per_sec, duration} for a media file,
    plus `peaks_l`/`peaks_r` (each side's own peak) for a 2+ channel source —
    what lets the timeline show a one-sided recording (QA-122).

    Long sources get a lower density (a divisor of DECODE_RATE) so the total
    stays ≤ MAX_PEAKS; each peak is still the true sample peak of its span.

    QA-132: a cached waveform is found from the file's identity and the
    REQUESTED density alone, before anything is spawned — the probe that
    picks the effective density used to run first, so every request (the
    timeline asks once per clip per session, plus every reload) paid an
    ffprobe, ~40 ms idle and 300 ms+ under load.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    requested = max(1, int(peaks_per_sec))
    cache_path = cache_dir / f"wave_{_key(src, requested)}.json"
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    channels, dur_hint = _probe_audio(src)
    peaks_per_sec = _effective_pps(dur_hint, requested)
    if channels <= 0:
        # No audio stream: nothing to draw (and nothing to cache — an audio
        # track added to the file later must not be hidden behind this).
        return {"peaks": [], "peaks_per_sec": peaks_per_sec, "duration": 0.0}
    peaks, sides, n_samples = _bucket_peaks(src, channels, peaks_per_sec)
    if not peaks:
        return {"peaks": [], "peaks_per_sec": peaks_per_sec, "duration": 0.0}
    out = {"peaks": peaks, "peaks_per_sec": peaks_per_sec, "duration": n_samples / DECODE_RATE}
    if sides:
        out["peaks_l"], out["peaks_r"] = sides
    tmp = cache_path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    _pu.replace_with_retry(tmp, cache_path)
    return out
