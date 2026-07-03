"""Audio waveform peaks for the timeline.

We pipe mono int16 PCM out of ffmpeg, then bucket-max-abs into N peaks per
second. Cached to JSON next to the source file so re-fetching is instant.
"""
from __future__ import annotations
import hashlib
import json
import struct
import subprocess
from pathlib import Path

from .. import platformutil as _pu

DEFAULT_PEAKS_PER_SEC = 50
MAX_PEAKS = 4000  # Cap total peaks regardless of source length so JSON stays small.


def _key(src: Path, peaks_per_sec: int) -> str:
    return hashlib.sha256(f"{src}|{peaks_per_sec}".encode()).hexdigest()[:14]


def _effective_pps(duration_hint: float, requested: int) -> int:
    """If a long source would exceed MAX_PEAKS at `requested` density, scale down."""
    if duration_hint <= 0:
        return requested
    max_pps = max(2, int(MAX_PEAKS / max(1.0, duration_hint)))
    return min(requested, max_pps)


def waveform_peaks(src: Path, cache_dir: Path,
                   *, peaks_per_sec: int = DEFAULT_PEAKS_PER_SEC) -> dict:
    """Return {peaks: [-1..1 floats], peaks_per_sec, duration} for a media file.

    Long sources are auto-downsampled (peaks_per_sec lowered) so total peaks ≤
    MAX_PEAKS — keeps JSON small and the canvas redraw cheap.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Probe duration first so we can clamp peaks_per_sec for long sources
    try:
        from ..ingest.probe import probe
        dur_hint = probe(src).duration
    except Exception:
        dur_hint = 0.0
    peaks_per_sec = _effective_pps(dur_hint, peaks_per_sec)
    cache_path = cache_dir / f"wave_{_key(src, peaks_per_sec)}.json"
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text())
        except Exception:
            pass

    sr = peaks_per_sec * 200  # 200 samples per peak; gives clean RMS-ish bars
    proc = subprocess.run(
        [_pu.FFMPEG, "-v", "error", "-i", str(src),
         "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
        capture_output=True,
    )
    if proc.returncode != 0:
        # Source has no audio; return zeros
        return {"peaks": [], "peaks_per_sec": peaks_per_sec, "duration": 0.0}

    raw = proc.stdout
    n_samples = len(raw) // 2
    if n_samples == 0:
        return {"peaks": [], "peaks_per_sec": peaks_per_sec, "duration": 0.0}

    # Bucket samples into peaks (max abs) per chunk of `bucket` samples
    bucket = max(1, sr // peaks_per_sec)
    n_buckets = n_samples // bucket
    peaks: list[float] = []
    # Read int16 efficiently in chunks
    for i in range(n_buckets):
        start = i * bucket * 2
        end = start + bucket * 2
        chunk = raw[start:end]
        # Find max abs across the chunk (struct.unpack_from is fast enough here)
        n = len(chunk) // 2
        if not n:
            peaks.append(0.0)
            continue
        ints = struct.unpack(f"<{n}h", chunk)
        m = max(abs(v) for v in ints)
        peaks.append(m / 32768.0)

    duration = n_samples / sr
    out = {"peaks": peaks, "peaks_per_sec": peaks_per_sec, "duration": duration}
    cache_path.write_text(json.dumps(out))
    return out
