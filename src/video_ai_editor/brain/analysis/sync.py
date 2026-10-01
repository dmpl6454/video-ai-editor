"""Per-file sync offsets: numpy FFT cross-correlation at 16 kHz against the
reference, three 20 s anchors (start, middle, end; one more every 5 min), the
file offset is their median. A port of `ai/multicam._audio_offset`'s intent
without librosa.

Sign: `other_t = ref_t + offset_s` — a file whose content plays LATER in its
own clock (it started recording earlier) has a positive offset; that is the
number `apply_camera_plan` adds to `in_` (`offsets[angle] − offsets[src]`).

Confidence, MEASURED rather than assumed: the spec's "peak-to-median ratio / 6"
alone cannot mark a dead angle, because the peak of the cross-correlation of
two independent noises over a ±5 s search (160 000 lags) sits ≈ 4.7 σ above
zero while the median of |xc| is 0.67 σ — a ratio ≈ 7, "verified". So the
ratio is combined with anchor agreement: when the anchors' offsets spread more
than DRIFT_MAX_MS the member is `unverified` (confidence scaled by 0.3), and
its offset is reported as 0.0 with the anchor table kept for the card.
"""
from __future__ import annotations

import os

import numpy as np

from .pcm import SR, read_pcm

ANCHOR_S = 20.0
ANCHOR_EVERY_S = 300.0
MIN_ANCHORS = 3
SEARCH_S = 5.0
UNVERIFIED_BELOW = 0.5
DRIFT_MAX_MS = 40.0
DISAGREE_FACTOR = 0.3
ENGINE = "fft_xcorr"


def _xcorr(ref: np.ndarray, other: np.ndarray, sr: int, search_s: float) -> tuple[int, float]:
    """(best lag in samples, peak-to-median ratio) of c[L] = Σ ref[t]·other[t+L]
    over |L| ≤ search_s·sr. Circular correlation on a 2× zero-padded FFT."""
    n = len(ref)
    n2 = 1 << int(np.ceil(np.log2(max(2, 2 * n))))
    r = np.fft.rfft(ref.astype(np.float64) - float(ref.mean()), n2)
    o = np.fft.rfft(other.astype(np.float64) - float(other.mean()), n2)
    xc = np.fft.irfft(np.conj(r) * o, n2)
    max_lag = min(int(search_s * sr), n - 1)
    lags = np.concatenate([np.arange(0, max_lag + 1), np.arange(-max_lag, 0)])
    vals = np.concatenate([xc[: max_lag + 1], xc[n2 - max_lag:]])
    i = int(np.argmax(vals))
    med = float(np.median(np.abs(vals))) + 1e-12
    return int(lags[i]), float(vals[i]) / med


def _anchor_starts(overlap_s: float, anchor_s: float) -> list[float]:
    usable = overlap_s - anchor_s - SEARCH_S
    if usable <= 0:
        return [0.0]
    count = max(MIN_ANCHORS, int(usable // ANCHOR_EVERY_S) + 1)
    return [SEARCH_S + usable * i / (count - 1) for i in range(count)]


def estimate_offset(ref: np.ndarray, other: np.ndarray, *, sr: int = SR, anchor_s: float = ANCHOR_S,
                    search_s: float = SEARCH_S) -> dict:
    """The offset of `other` against `ref` (both PCM at `sr`), with anchors."""
    overlap = min(len(ref), len(other)) / sr
    anchor_s = min(anchor_s, max(1.0, overlap / 2))
    anchors = []
    for s in _anchor_starts(overlap, anchor_s):
        a0 = int(s * sr)
        a1 = min(len(ref), len(other), a0 + int(anchor_s * sr))
        if a1 - a0 < sr // 2:
            continue
        lag, ratio = _xcorr(ref[a0:a1], other[a0:a1], sr, search_s)
        anchors.append({"ref_s": round(s, 3), "offset_s": round(lag / sr, 4), "ratio": round(ratio, 2)})
    if not anchors:
        return {"offset_s": 0.0, "confidence": 0.0, "unverified": True, "anchors": [], "anchors_max_dev_ms": 0.0,
                "engine": ENGINE}
    offs = np.array([a["offset_s"] for a in anchors])
    median = float(np.median(offs))
    dev_ms = float(np.max(np.abs(offs - median))) * 1000.0
    confidence = min(1.0, float(np.median([a["ratio"] for a in anchors])) / 6.0)
    if dev_ms > DRIFT_MAX_MS:
        confidence *= DISAGREE_FACTOR
    unverified = confidence < UNVERIFIED_BELOW
    return {"offset_s": 0.0 if unverified else round(median, 4), "confidence": round(confidence, 3),
            "unverified": unverified, "anchors": anchors, "anchors_max_dev_ms": round(dev_ms, 1),
            "engine": ENGINE}


def sync_member(ref_path: str | os.PathLike, other_path: str | os.PathLike, *,
                ref_pcm: np.ndarray | None = None, other_pcm: np.ndarray | None = None) -> dict:
    """`estimate_offset` over two files (decoded once each when not given)."""
    ref = read_pcm(ref_path) if ref_pcm is None else ref_pcm
    other = read_pcm(other_path) if other_pcm is None else other_pcm
    if len(ref) == 0 or len(other) == 0:
        return {"offset_s": 0.0, "confidence": 0.0, "unverified": True, "anchors": [], "anchors_max_dev_ms": 0.0,
                "engine": ENGINE}
    return estimate_offset(ref, other)


__all__ = ["ENGINE", "ANCHOR_S", "SEARCH_S", "UNVERIFIED_BELOW", "DRIFT_MAX_MS", "estimate_offset", "sync_member"]
