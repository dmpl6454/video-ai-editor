"""The `audio` layer: the 100 Hz envelope every cut edge is placed by, VAD,
silences, loudness, noise floor, clipping and the per-angle own-mic energy.

VAD is the logic of `ingest/transcribe._voicing` (10 ms RMS frames, threshold
`max(floor + 8, ref − 35)` from the 10th/95th percentiles, no threshold when
the file has < 12 dB of dynamics) re-implemented here — the private is not
imported. Silences follow `ai/diarize._heuristic_diarize`'s ffmpeg thresholds
(≥ 0.4 s below −35 dB) on the same frames, so the layer needs no second pass.
"""
from __future__ import annotations

import base64
import os
import re
import subprocess

import numpy as np

from ... import platformutil as _pu
from . import ANALYSIS_VERSION
from .pcm import SR, frame_view, read_pcm

HZ = 100
FRAME_S = 1.0 / HZ
VAD_GAP_S = 0.1          # transcribe._VAD_GAP_S: a shorter dip does not end a run
VAD_MIN_RUN_S = 0.03     # transcribe._VAD_MIN_RUN_S: shorter runs are clicks/breaths
VAD_MIN_DYNAMICS_DB = 12.0
SILENCE_DB = -35.0
SILENCE_MIN_S = 0.4
ENV_FLOOR_DB = -120.0
CLIP_LEVEL = 0.999
PARAMS = {"hz": HZ, "analysis_version": ANALYSIS_VERSION}


def envelope_db(pcm: np.ndarray, sr: int = SR) -> np.ndarray:
    """RMS per 10 ms frame in dBFS (float64), floored at ENV_FLOOR_DB."""
    n = max(1, int(round(sr * FRAME_S)))
    frames = frame_view(pcm, n, n)
    if len(frames) == 0:
        return np.zeros(0, dtype=np.float64)
    rms = np.sqrt(np.mean(np.square(frames.astype(np.float64)), axis=1))
    return np.maximum(20.0 * np.log10(rms + 1e-9), ENV_FLOOR_DB)


def vad_threshold(env: np.ndarray) -> float | None:
    if len(env) < 10:
        return None
    ref, floor = float(np.percentile(env, 95)), float(np.percentile(env, 10))
    if ref - floor < VAD_MIN_DYNAMICS_DB:
        return None
    return max(floor + 8.0, ref - 35.0)


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """[a, b) index runs of True."""
    if len(mask) == 0:
        return []
    d = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1)
    return list(zip(starts.tolist(), ends.tolist()))


def vad_runs(env: np.ndarray, threshold: float | None) -> list[tuple[float, float]]:
    """Voiced spans in seconds: frames above `threshold`, dips shorter than
    VAD_GAP_S bridged, runs shorter than VAD_MIN_RUN_S dropped."""
    if threshold is None or len(env) == 0:
        return []
    mask = env > threshold
    gap = int(round(VAD_GAP_S / FRAME_S))
    merged: list[list[int]] = []
    for a, b in _runs(mask):
        if merged and a - merged[-1][1] < gap:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    min_run = int(round(VAD_MIN_RUN_S / FRAME_S))
    return [(round(a * FRAME_S, 3), round(b * FRAME_S, 3)) for a, b in merged if b - a >= min_run]


def silences(env: np.ndarray) -> list[tuple[float, float]]:
    min_frames = int(round(SILENCE_MIN_S / FRAME_S))
    return [(round(a * FRAME_S, 3), round(b * FRAME_S, 3)) for a, b in _runs(env < SILENCE_DB)
            if b - a >= min_frames]


def encode_env(env: np.ndarray) -> str:
    q = np.clip(np.rint(env), -128, 0).astype(np.int8)
    return base64.b64encode(q.tobytes()).decode("ascii")


def decode_env(layer_or_b64: dict | str) -> np.ndarray:
    raw = layer_or_b64["env_10ms"] if isinstance(layer_or_b64, dict) else layer_or_b64
    return np.frombuffer(base64.b64decode(raw), dtype=np.int8).astype(np.float64)


_EBUR_I = re.compile(r"^\s*I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", re.M)


def loudness_i(path: str | os.PathLike) -> float | None:
    """Integrated loudness by ffmpeg `ebur128`; None when it cannot be read."""
    proc = subprocess.run([_pu.FFMPEG, "-nostdin", "-hide_banner", "-i", str(path), "-vn", "-af",
                           "ebur128=framelog=quiet", "-f", "null", "-"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          **_pu.SUBPROCESS_FLAGS)
    m = _EBUR_I.findall(proc.stderr)
    if not m:
        return None
    try:
        return round(float(m[-1]), 1)
    except ValueError:
        return None


def clipping_spans(pcm: np.ndarray, sr: int = SR, *, min_s: float = 0.02) -> list[tuple[float, float]]:
    hot = np.abs(pcm) >= CLIP_LEVEL
    n = max(1, int(round(sr * FRAME_S)))
    frames = frame_view(hot.astype(np.int8), n, n)
    if len(frames) == 0:
        return []
    frac = frames.mean(axis=1)
    return [(round(a * FRAME_S, 3), round(b * FRAME_S, 3)) for a, b in _runs(frac > 0.02)
            if (b - a) * FRAME_S >= min_s]


def build_audio_layer(path: str | os.PathLike, *, pcm: np.ndarray | None = None) -> dict:
    """The audio layer of `path` (reference seconds = its own seconds), in
    lane C's `schema.AudioLayer` shape — the frame count and the VAD
    threshold are derived from `env_10ms` (`n_frames`, `vad_threshold`)
    rather than stored, so the file carries nothing the model forbids."""
    pcm = read_pcm(path) if pcm is None else pcm
    env = envelope_db(pcm)
    thr = vad_threshold(env)
    vad = vad_runs(env, thr)
    sil = silences(env)
    speech_frames = np.zeros(len(env), dtype=bool)
    for a, b in vad:
        speech_frames[int(a * HZ): int(b * HZ)] = True
    quiet = env[~speech_frames] if (~speech_frames).any() else env
    return {
        "params": dict(PARAMS), "hz": HZ, "env_10ms": encode_env(env),
        "vad": [[a, b] for a, b in vad],
        "silences": [{"id": f"sil_{i + 1:04d}", "t0": a, "t1": b} for i, (a, b) in enumerate(sil)],
        "loudness_i": loudness_i(path),
        "noise_floor_db": round(float(np.percentile(quiet, 10)), 1) if len(quiet) else None,
        "clipping": [[a, b] for a, b in clipping_spans(pcm)],
        "own_mic_energy": {},
        "events": [],
    }


def n_frames(layer: dict) -> int:
    """Frames of a layer's envelope (4 base64 chars carry 3 frames)."""
    raw = layer.get("env_10ms") or ""
    return (len(raw) * 3) // 4 - raw.count("=")


def speech_mask(layer: dict) -> np.ndarray:
    """Boolean speech frames of a layer (its `vad` spans at `hz`)."""
    n = n_frames(layer)
    m = np.zeros(n, dtype=bool)
    for a, b in layer.get("vad") or []:
        m[int(a * HZ): int(b * HZ)] = True
    return m


def aligned_env(angle_layer: dict, offset_s: float, n_ref_frames: int) -> np.ndarray:
    """The angle's envelope on the REFERENCE frame grid: reference frame `i`
    (t = i / hz) reads angle frame `round((t + offset_s) · hz)`; frames the
    angle does not cover read ENV_FLOOR_DB. (`angle_t = ref_t + offset_s`.)"""
    env = decode_env(angle_layer)
    idx = np.arange(n_ref_frames) + int(round(offset_s * HZ))
    out = np.full(n_ref_frames, ENV_FLOOR_DB, dtype=np.float64)
    ok = (idx >= 0) & (idx < len(env))
    out[ok] = env[idx[ok]]
    return out


def attach_own_mic_energy(ref_layer: dict, angles: dict[str, tuple[dict, float]]) -> dict:
    """A copy of the reference's audio layer with `own_mic_energy[src_key]` =
    each angle's envelope aligned onto the reference frames (whole dBFS per
    frame, the list shape of spec §3.2)."""
    n = n_frames(ref_layer)
    energy = {key: np.clip(np.rint(aligned_env(layer, off, n)), -128, 0).astype(int).tolist()
              for key, (layer, off) in sorted(angles.items())}
    return {**ref_layer, "own_mic_energy": energy}


__all__ = ["HZ", "FRAME_S", "PARAMS", "envelope_db", "vad_threshold", "vad_runs", "silences", "encode_env",
           "decode_env", "loudness_i", "clipping_spans", "build_audio_layer", "n_frames", "speech_mask",
           "aligned_env", "attach_own_mic_energy"]
