"""The acoustic filler detector (spec §3.4) and the pitch helpers the speech
layer shares.

An `uh` the transcript missed is a voiced island of 0.15–0.6 s (≥ 3 voiced
pitch frames — a breath is not an `uh`) with a flat pitch (autocorrelation
pitch range < 2 semitones), low spectral flux and no CONTENT word overlapping
≥ 50 % of it. `confidence = 0.4·flatness + 0.3·(1 − flux_norm) + 0.3·(1 −
overlap)`; a lexical filler that also passes gets 1.0.

"Low flux", MEASURED: flux is the mean frame-to-frame difference of the
26-band log-mel spectrum over the island's interior 20 ms frames, compared
with the same statistic over every equal-length window of speech in the
file. The planted `uh`s sit at the 6th–17th percentile of those windows and
the breathiest one at the 32nd, so the gate is "below the median window" and
the confidence term reads `flux_norm = (flux − p20) / (median − p20)` clipped
to 0..1 — the spec's 20th percentile is where the term is 1.0, not the gate
(a per-frame 20th percentile is the steady-vowel floor no whole island
reaches). Flatness = 1 − range / FLAT_FULL_ST (4 st: a sentence word).

MEASURED deviation from the spec's letter ("no transcript word overlapping
≥ 50 %"): whisper.cpp small — the backend the upload path uses on this Mac —
never drops a 0.2–0.4 s Piper `uh`; it writes `Ha!`, `Huh.`, `Ah,`, a lone
`A`/`The`, or lets a neighbour's token span bleed across the 0.12 s gap onto
the island (tests/brain_analysis_fixtures.py records the table). A token in
NON_LEXICAL or FUNCTION therefore does not block the island — it is what the
transcript wrote over a non-word. A content token ("Right.", "Okay.") still
blocks. The speech layer marks such a NON_LEXICAL word `filler` when an
island covers it (`speech._mark_fillers`).

Shape: lane C's `schema.AcousticFiller` — `{id, t0, t1, confidence,
evidence: {name: float}}`; `evidence.lexical` is 1.0 when the island is also
a lexical filler the transcript heard (then `confidence` is 1.0).
"""
from __future__ import annotations

import re

import numpy as np

from ...ai.diarize import mel_filterbank
from .pcm import frame_view

ISLAND_MIN_S = 0.15
ISLAND_MAX_S = 0.6
PITCH_FLAT_ST = 2.0
FLAT_FULL_ST = 4.0
MIN_PITCH_FRAMES = 3
FLUX_PERCENTILE = 20
FLUX_BANDS = 26
OVERLAP_MAX = 0.5
REMOVE_CONFIDENCE = 0.7
PITCH_FRAME_S = 0.04
PITCH_HOP_S = 0.01
F0_MIN, F0_MAX = 60.0, 400.0
VOICING_MIN = 0.5
FLUX_FRAME_S = 0.02

#: dispatch._DEFAULT_FILLERS, mirrored (importing dispatch here would pull the editor in).
LEXICAL_FILLERS = frozenset({"um", "uh", "umm", "uhh", "erm", "hmm"})
#: whisper.cpp's other spellings of a hesitation (measured: `Hum,` for a
#: Piper `umm`); never a content word when shorter than HESITATION_MAX_S.
#: Backchannel spellings (`mm`, `mhm`, `mm-hm`) are NOT here — a listener's
#: "mm-hm" is a turn, not a filler.
HESITATION_SPELLINGS = frozenset({"hum", "uhm", "hmmm", "ahh", "uhhh", "ohh", "umh", "ummm"})
HESITATION_MAX_S = 0.7
#: The transcript's spellings of a non-word (never a content word).
NON_LEXICAL = frozenset({"ha", "huh", "ah", "oh", "hm", "hum", "mm", "mhm", "mmhm", "eh", "er", "uhm", "hmmm",
                         "ahh", "ohh", "uhhh", "ooh", "aah"} | LEXICAL_FILLERS)
#: Function tokens whisper drops onto an island between two sentences.
FUNCTION = frozenset({"a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "so", "i"})
_STRIP = re.compile(r"[^\w']+", re.UNICODE)


def norm_token(text: str) -> str:
    return _STRIP.sub("", str(text)).lower().replace("-", "")


def pitch_track(pcm: np.ndarray, sr: int, t0: float, t1: float) -> np.ndarray:
    """f0 (Hz) per 40 ms frame (hop 10 ms) inside [t0, t1) by autocorrelation
    in 60–400 Hz; only frames whose normalised peak ≥ VOICING_MIN are kept."""
    seg = pcm[int(t0 * sr): int(t1 * sr)].astype(np.float64)
    n = int(PITCH_FRAME_S * sr)
    hop = int(PITCH_HOP_S * sr)
    frames = frame_view(seg, n, hop)
    if len(frames) == 0:
        return np.zeros(0)
    frames = frames - frames.mean(axis=1, keepdims=True)
    frames = frames * np.hanning(n)
    spec = np.fft.rfft(frames, 2 * n, axis=1)
    ac = np.fft.irfft(np.abs(spec) ** 2, 2 * n, axis=1)[:, :n]
    lo, hi = int(sr / F0_MAX), int(sr / F0_MIN)
    r0 = ac[:, 0] + 1e-12
    window = ac[:, lo:hi + 1] / r0[:, None]
    best = np.argmax(window, axis=1)
    strength = window[np.arange(len(window)), best]
    f0 = sr / (best + lo)
    return f0[strength >= VOICING_MIN]


def pitch_range_st(f0: np.ndarray) -> float:
    """Robust range in semitones: the 10th–90th percentile spread."""
    if len(f0) < 3:
        return 0.0
    lo, hi = np.percentile(f0, [10, 90])
    return float(12.0 * np.log2(max(hi, 1e-6) / max(lo, 1e-6)))


def spectral_flux(pcm: np.ndarray, sr: int) -> np.ndarray:
    """Mean frame-to-frame difference of the level-normalised 26-band log-mel
    spectrum per 20 ms frame (band shape, not level, so loudness is not change)."""
    n = int(FLUX_FRAME_S * sr)
    frames = frame_view(pcm.astype(np.float64), n, n)
    if len(frames) < 2:
        return np.zeros(len(frames))
    power = np.abs(np.fft.rfft(frames * np.hanning(n), axis=1)) ** 2
    mel = np.log10(1e-9 + power @ mel_filterbank(sr, n, FLUX_BANDS).T)
    mel = mel - mel.mean(axis=1, keepdims=True)
    flux = np.zeros(len(frames))
    flux[1:] = np.abs(np.diff(mel, axis=0)).mean(axis=1)
    return flux


def window_flux_population(flux: np.ndarray, vad: list, island_frames: int) -> np.ndarray:
    """Mean flux of every window of `island_frames` interior frames of speech."""
    out = []
    for a, b in vad:
        i0, i1 = int(float(a) / FLUX_FRAME_S) + 1, int(float(b) / FLUX_FRAME_S) - 1
        for s in range(i0, i1 - island_frames + 1, max(1, island_frames // 4)):
            out.append(float(flux[s: s + island_frames].mean()))
    return np.array(out) if out else np.zeros(0)


def _islands(vad: list) -> list[tuple[float, float]]:
    return [(float(a), float(b)) for a, b in vad if ISLAND_MIN_S <= float(b) - float(a) <= ISLAND_MAX_S]


def _overlap_of(words: list[dict], a: float, b: float) -> tuple[float, bool]:
    """(content-word overlap fraction of the island, whether the token that
    covers it most is a lexical filler covering ≥ 50 %)."""
    content = 0.0
    best: tuple[float, str] = (0.0, "")
    for w in words:
        ov = max(0.0, min(b, float(w["t1"])) - max(a, float(w["t0"])))
        if ov <= 0:
            continue
        tok = norm_token(w["text"])
        frac = ov / (b - a)
        if tok and tok not in NON_LEXICAL and tok not in FUNCTION:
            content += frac
        if frac > best[0]:
            best = (frac, tok)
    lexical = best[0] >= 0.5 and (best[1] in LEXICAL_FILLERS or best[1] in HESITATION_SPELLINGS)
    return min(1.0, content), lexical


def acoustic_fillers(pcm: np.ndarray, sr: int, *, vad: list, words: list[dict]) -> list[dict]:
    """[{id, t0, t1, confidence, evidence{pitch_range_st, flux, flux_percentile,
    word_overlap, lexical}}] for every VAD island that passes the three
    tests; `words` are `{t0, t1, text}` in the same clock."""
    islands = _islands(vad)
    if not islands:
        return []
    flux = spectral_flux(pcm, sr)
    typical = int(round(float(np.median([b - a for a, b in islands])) / FLUX_FRAME_S)) - 2
    pop = window_flux_population(flux, vad, max(3, typical))
    p20 = float(np.percentile(pop, FLUX_PERCENTILE)) if len(pop) else 0.0
    median = float(np.median(pop)) if len(pop) else 0.0
    out: list[dict] = []
    for a, b in islands:
        content_ov, lexical = _overlap_of(words, a, b)
        if content_ov >= OVERLAP_MAX:
            continue
        f0 = pitch_track(pcm, sr, a, b)
        rng = pitch_range_st(f0)
        if len(f0) < MIN_PITCH_FRAMES or rng >= PITCH_FLAT_ST:
            continue
        fl = flux[int(a / FLUX_FRAME_S) + 1: max(int(a / FLUX_FRAME_S) + 2, int(b / FLUX_FRAME_S) - 1)]
        island_flux = float(fl.mean()) if len(fl) else 0.0
        if island_flux > median:
            continue
        flatness = max(0.0, 1.0 - rng / FLAT_FULL_ST)
        flux_norm = min(1.0, max(0.0, (island_flux - p20) / max(1e-9, median - p20)))
        conf = 0.4 * flatness + 0.3 * (1.0 - flux_norm) + 0.3 * (1.0 - content_ov)
        out.append({"id": f"af_{len(out) + 1:04d}", "t0": round(a, 3), "t1": round(b, 3),
                    "confidence": 1.0 if lexical else round(min(1.0, conf), 3),
                    "evidence": {"pitch_range_st": round(rng, 2), "flux": round(island_flux, 4),
                                 "flux_percentile": round(float((pop < island_flux).mean()), 2) if len(pop) else 0.0,
                                 "word_overlap": round(content_ov, 2), "lexical": 1.0 if lexical else 0.0}})
    return out


__all__ = ["ISLAND_MIN_S", "ISLAND_MAX_S", "PITCH_FLAT_ST", "REMOVE_CONFIDENCE", "LEXICAL_FILLERS", "NON_LEXICAL",
           "HESITATION_SPELLINGS", "HESITATION_MAX_S", "FUNCTION", "norm_token", "pitch_track", "pitch_range_st", "spectral_flux", "window_flux_population",
           "acoustic_fillers"]
