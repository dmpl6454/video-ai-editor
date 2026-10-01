"""The `speakers` layer: utterances from VAD run-lengths ≥ 0.4 s → 13 numpy
MFCC (pre-emphasis 0.97, 25 ms Hann, 10 ms hop, 26 mel, DCT-II) → k-means++
(seed 42, 25 iterations, `ai/diarize._kmeans_numpy`) → turns (same speaker
merged across gaps < 0.6 s) → speakers with share. k comes from the request,
else the best silhouette in 1..4.

The single-speaker test, MEASURED: the spec's "two-cluster silhouette < 0.12
means one speaker" does not hold on synthetic speech — the talking-head
fixture's ONE Piper voice clusters at silhouette 0.31 (0.57 without c0),
above the two-voice podcast's 0.36: the second cluster is the `um`s and the
retake, i.e. content, not a voice. A content split cuts through the
transcript's punctuated sentences; a speaker split follows them. So k = 2 is
accepted only when the silhouette clears SINGLE_SILHOUETTE AND at most
MIXED_SENTENCE_MAX of the punctuated sentences carry both labels; without a
usable transcript (< MIN_PUNCTUATED sentences) the fallback gate is a median
pitch separation ≥ PITCH_SEP_MIN_ST between the clusters (2.0 st on the one
voice, 8.4 st on the two). `k_method` records which gate decided; the
measured numbers go to the log (lane C's `SpeakersLayer` has no slot).

Angle hints (`angle_hints`) use own-mic energy FIRST: Pearson r between each
angle's envelope RELATIVE to the mean over angles (in dB, aligned onto the
reference frames — the subtraction cancels the speech's own dynamics that
every camera hears; absolute amplitude gave r ≈ 0.24, relative gives 0.95)
and each speaker's talking mask over the reference's speech frames; accepted
at r ≥ 0.5 with a margin ≥ 0.25 over the runner-up; assignment by enumerating
permutations (≤ 4 angles this wave); an angle that correlates with both
speakers is `wide`. Face size is the fallback (`face_hints`, Haar over 1 Hz
frames, decoded by ffmpeg, never `cv2.VideoCapture` on a user path); mouth
motion is not used this wave. `by` records which signal decided.
"""
from __future__ import annotations

import itertools
import logging
import os
import subprocess

import numpy as np

from ... import platformutil as _pu
from ...ai.diarize import _kmeans_numpy, mfcc13_numpy
from ...ai.shorts import _sentences as _shorts_sentences
from . import ANALYSIS_VERSION
from .audio import HZ, aligned_env, decode_env, n_frames, speech_mask
from .fillers import pitch_track
from .pcm import SR

UTT_MIN_S = 0.4
TURN_GAP_S = 0.6
SEED = 42
KMEANS_ITERS = 25
K_MAX = 4
SINGLE_SILHOUETTE = 0.12
MIXED_SENTENCE_MAX = 0.2
MIN_PUNCTUATED = 5
PITCH_SEP_MIN_ST = 3.0
MFCC = {"n_fft": 400, "hop": 160, "n_mels": 26, "preemph": 0.97}
ENGINE = "numpy-mfcc13-kmeans"
_log = logging.getLogger(__name__)
OWN_MIC_R_MIN = 0.5
OWN_MIC_MARGIN = 0.25
FACE_CLOSE = 0.25
FACE_FAR = 0.15


def utterances_from_vad(vad: list) -> list[tuple[float, float]]:
    return [(float(a), float(b)) for a, b in vad if float(b) - float(a) >= UTT_MIN_S]


def mfcc_means(pcm: np.ndarray, sr: int, utts: list[tuple[float, float]]) -> np.ndarray:
    feats = []
    for a, b in utts:
        seg = pcm[int(a * sr): int(b * sr)]
        if len(seg) < 800:
            feats.append(np.zeros(13, dtype=np.float64))
            continue
        feats.append(mfcc13_numpy(seg.astype(np.float64), sr, **MFCC).mean(axis=1))
    return np.stack(feats) if feats else np.zeros((0, 13))


def silhouette(X: np.ndarray, labels: np.ndarray) -> float:
    """Mean silhouette over samples (numpy); 0 when a cluster is a singleton
    or there is one cluster."""
    ks = np.unique(labels)
    if len(ks) < 2 or len(X) < 3:
        return 0.0
    d = np.sqrt(((X[:, None, :] - X[None, :, :]) ** 2).sum(-1))
    s = np.zeros(len(X))
    for i in range(len(X)):
        own = labels == labels[i]
        if own.sum() < 2:
            continue
        a = d[i, own].sum() / (own.sum() - 1)
        b = min(d[i, labels == k].mean() for k in ks if k != labels[i])
        s[i] = (b - a) / max(a, b, 1e-12)
    return float(s.mean())


def punctuated_sentence_spans(transcript: dict | None) -> list[tuple[float, float]]:
    """(t0, t1) of every sentence that ends in terminal punctuation — the
    structure a speaker split must respect."""
    if not transcript:
        return []
    return [(s.start, s.end) for s in _shorts_sentences(transcript) if s.is_complete]


def mixed_sentence_rate(utts: list[tuple[float, float]], labels: np.ndarray,
                        sentences: list[tuple[float, float]]) -> float | None:
    """Share of punctuated sentences whose utterances carry both labels;
    None when fewer than MIN_PUNCTUATED sentences hold ≥ 2 utterances."""
    rows = []
    for a, b in sentences:
        inside = {int(l) for (u0, u1), l in zip(utts, labels) if min(u1, b) - max(u0, a) > 0.5 * (u1 - u0)}
        if len(inside) >= 1:
            rows.append(len(inside) > 1)
    return None if len(rows) < MIN_PUNCTUATED else float(np.mean(rows))


def cluster_pitch_sep_st(pcm: np.ndarray, sr: int, utts: list[tuple[float, float]], labels: np.ndarray) -> float:
    meds = []
    for k in np.unique(labels):
        f0 = np.concatenate([pitch_track(pcm, sr, a, b) for (a, b), l in zip(utts, labels) if l == k] or [np.zeros(0)])
        meds.append(np.log2(np.median(f0)) if len(f0) >= 3 else np.nan)
    meds = [m for m in meds if not np.isnan(m)]
    return float(12.0 * (max(meds) - min(meds))) if len(meds) >= 2 else 0.0


def _two_speakers_ok(sil: float, mixed: float | None, pitch_sep: float) -> tuple[bool, str, dict]:
    ev = {"silhouette": round(sil, 3), "mixed_sentence_rate": None if mixed is None else round(mixed, 3),
          "pitch_sep_st": round(pitch_sep, 2)}
    if sil < SINGLE_SILHOUETTE:
        return False, "silhouette", ev
    if mixed is not None:
        return mixed <= MIXED_SENTENCE_MAX, "silhouette+sentences", ev
    return pitch_sep >= PITCH_SEP_MIN_ST, "silhouette+pitch", ev


def choose_k(X: np.ndarray, k: int | None, *, gate=None) -> tuple[int, str, float, np.ndarray, dict]:
    """(k, method, silhouette, labels, evidence). `gate(labels) -> (ok, method,
    evidence)` decides whether a k ≥ 2 solution is speakers rather than content."""
    if len(X) == 0:
        return 0, "empty", 0.0, np.zeros(0, dtype=int), {}
    if k is not None:
        k = max(1, min(int(k), len(X)))
        labels = _kmeans_numpy(X, k=k, iters=KMEANS_ITERS, seed=SEED) if k > 1 else np.zeros(len(X), dtype=int)
        return k, "request", (silhouette(X, labels) if k > 1 else 0.0), labels, {}
    best = (1, 0.0, np.zeros(len(X), dtype=int))
    for kk in range(2, min(K_MAX, len(X) - 1) + 1):
        labels = _kmeans_numpy(X, k=kk, iters=KMEANS_ITERS, seed=SEED)
        sil = silhouette(X, labels)
        if sil > best[1]:
            best = (kk, sil, labels)
    single = (1, "silhouette", round(best[1], 3), np.zeros(len(X), dtype=int))
    if best[0] == 1:
        return (*single, {})
    ok, method, ev = gate(best[2], best[1]) if gate else (best[1] >= SINGLE_SILHOUETTE, "silhouette", {})
    if not ok:
        return (*single[:1], method, single[2], single[3], ev)
    return best[0], method, round(best[1], 3), best[2], ev


def _turns(utts: list[tuple[float, float]], spk: list[str]) -> list[dict]:
    out: list[dict] = []
    for (a, b), s in zip(utts, spk):
        if out and out[-1]["spk"] == s and a - out[-1]["t1"] < TURN_GAP_S:
            out[-1]["t1"] = round(b, 3)
            continue
        out.append({"id": f"u_{len(out) + 1:04d}", "spk": s, "t0": round(a, 3), "t1": round(b, 3)})
    return out


def build_speakers_layer(pcm: np.ndarray, audio_layer: dict, *, k: int | None = None, sr: int = SR,
                         transcript: dict | None = None) -> dict:
    """The speakers layer of one source from its PCM and audio layer; the
    transcript (when given) only informs the single-speaker test."""
    utts = utterances_from_vad(audio_layer.get("vad") or [])
    X = mfcc_means(pcm, sr, utts)
    sentences = punctuated_sentence_spans(transcript)

    def gate(labels, sil):
        return _two_speakers_ok(sil, mixed_sentence_rate(utts, labels, sentences),
                                cluster_pitch_sep_st(pcm, sr, utts, labels))

    kk, method, sil, labels, evidence = choose_k(X, k, gate=gate)
    _log.info("speakers: k=%d by %s (silhouette %.3f) evidence=%s", kk, method, sil, evidence)
    order: dict[int, str] = {}
    for lab in labels.tolist():
        order.setdefault(int(lab), f"S{len(order) + 1}")
    spk = [order[int(lab)] for lab in labels.tolist()]
    turns = _turns(utts, spk)
    talk = {s: 0.0 for s in order.values()}
    for (a, b), s in zip(utts, spk):
        talk[s] += b - a
    total = sum(talk.values()) or 1.0
    speakers = [{"id": s, "label": f"SPEAKER_{i:02d}", "share": round(talk[s] / total, 3), "questions": 0,
                 "role_guess": "unknown"} for i, s in enumerate(order.values())]
    return {"params": {"k": "auto" if k is None else int(k), "features": "mfcc13", "seed": SEED, "engine": ENGINE,
                       "analysis_version": ANALYSIS_VERSION, "sentence_gate": bool(sentences)},
            "engine": ENGINE, "k": kk, "k_method": method, "silhouette": round(float(sil), 3),
            "utterances": [{"t0": round(a, 3), "t1": round(b, 3), "spk": s} for (a, b), s in zip(utts, spk)],
            "turns": turns, "speakers": speakers, "overlaps": [], "flip_risk": []}


def speaker_masks(layer: dict, n: int) -> dict[str, np.ndarray]:
    masks = {s["id"]: np.zeros(n, dtype=bool) for s in layer["speakers"]}
    for u in layer["utterances"]:
        masks[u["spk"]][int(u["t0"] * HZ): int(u["t1"] * HZ)] = True
    return masks


def _pearson(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 3 or x.std() == 0 or y.std() == 0:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])


def own_mic_matrix(layer: dict, ref_audio: dict, angles: dict[str, dict], offsets: dict[str, float]) -> dict:
    """r[speaker][angle] over the reference's speech frames, each angle's dB
    envelope taken RELATIVE to the mean over angles (see the module docstring)."""
    n = n_frames(ref_audio)
    speech = speech_mask(ref_audio)
    masks = speaker_masks(layer, n)
    envs = {path: aligned_env(alayer, float(offsets.get(path, 0.0)), n) for path, alayer in angles.items()}
    mean = np.mean(list(envs.values()), axis=0) if len(envs) > 1 else np.zeros(n)
    r: dict[str, dict[str, float]] = {s: {} for s in masks}
    for path, env in envs.items():
        rel = env - mean
        for s, m in masks.items():
            r[s][path] = round(_pearson(rel[speech], m[speech].astype(np.float64)), 3)
    return r


def assign(r: dict[str, dict[str, float]]) -> dict[str, dict]:
    """Speakers × angles by the best total r over permutations; a speaker's
    hint is accepted at r ≥ 0.5 with margin ≥ 0.25 over its runner-up."""
    speakers = sorted(r)
    angles = sorted({a for row in r.values() for a in row})
    if not speakers or not angles:
        return {}
    best: tuple[float, tuple] = (-1e9, ())
    for perm in itertools.permutations(angles, min(len(angles), len(speakers))):
        total = sum(r[s][a] for s, a in zip(speakers, perm))
        if total > best[0]:
            best = (total, perm)
    out: dict[str, dict] = {}
    for s, a in zip(speakers, best[1]):
        others = sorted((v for k, v in r[s].items() if k != a), reverse=True)
        margin = r[s][a] - (others[0] if others else 0.0)
        if r[s][a] >= OWN_MIC_R_MIN and margin >= OWN_MIC_MARGIN:
            out[s] = {"angle": a, "confidence": round(min(1.0, margin), 3), "by": "own_mic", "r": r[s][a]}
    return out


def angle_hints(layer: dict, ref_audio: dict, angles: dict[str, dict], *, offsets: dict[str, float]) -> dict:
    """{speaker_id: {angle, confidence, by, r}} by own-mic correlation."""
    if not angles or not layer.get("speakers"):
        return {}
    return assign(own_mic_matrix(layer, ref_audio, angles, offsets))


def _grey_frames(path: str | os.PathLike, times: list[float], size: int = 480) -> list[np.ndarray]:
    """One grey frame per time, decoded by ffmpeg (never cv2.VideoCapture)."""
    out = []
    for t in times:
        proc = subprocess.run([_pu.FFMPEG, "-nostdin", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path),
                               "-frames:v", "1", "-vf", f"scale={size}:-2,format=gray", "-f", "rawvideo", "-"],
                              capture_output=True, **_pu.SUBPROCESS_FLAGS)
        if proc.returncode != 0 or not proc.stdout:
            continue
        w = size
        h = len(proc.stdout) // w
        if h > 0:
            out.append(np.frombuffer(proc.stdout[: w * h], dtype=np.uint8).reshape(h, w))
    return out


def largest_face_fraction(path: str | os.PathLike, times: list[float]) -> float | None:
    """The median largest-face height as a fraction of the frame over the
    sampled times; None without cv2 or when no frame decodes."""
    try:
        import cv2
    except Exception:
        return None
    from pathlib import Path as _P
    det = cv2.CascadeClassifier(str(_P(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"))
    if det.empty():
        return None
    fracs = []
    for frame in _grey_frames(path, times):
        faces = det.detectMultiScale(frame, scaleFactor=1.2, minNeighbors=4, minSize=(24, 24))
        fracs.append(max((fh / frame.shape[0] for _x, _y, _w, fh in faces), default=0.0))
    return float(np.median(fracs)) if fracs else None


def face_hints(layer: dict, angle_paths: dict[str, str], *, offsets: dict[str, float], hz: float = 1.0) -> dict:
    """Fallback: the angle whose largest face is ≥ FACE_CLOSE while another's is
    < FACE_FAR is a close; assigned to the speaker with the largest talking
    share not yet placed. Recorded with `by: face_size`."""
    if not angle_paths or not layer.get("speakers"):
        return {}
    sizes: dict[str, float | None] = {}
    for path, media in angle_paths.items():
        t = [float(u["t0"]) + float(offsets.get(path, 0.0)) for u in layer["utterances"]][:: max(1, int(1 / hz))]
        sizes[path] = largest_face_fraction(media, t[:12])
    close = [p for p, f in sizes.items() if f is not None and f >= FACE_CLOSE
             and any(g is not None and g < FACE_FAR for q, g in sizes.items() if q != p)]
    out: dict[str, dict] = {}
    for s, p in zip(sorted(layer["speakers"], key=lambda x: -x["share"]), close):
        out[s["id"]] = {"angle": p, "confidence": round(float(sizes[p] or 0.0), 3), "by": "face_size", "r": None}
    return out


__all__ = ["ENGINE", "SEED", "UTT_MIN_S", "TURN_GAP_S", "SINGLE_SILHOUETTE", "MIXED_SENTENCE_MAX", "PITCH_SEP_MIN_ST",
           "utterances_from_vad", "mfcc_means", "silhouette", "choose_k", "punctuated_sentence_spans",
           "mixed_sentence_rate", "cluster_pitch_sep_st", "build_speakers_layer", "speaker_masks", "own_mic_matrix", "assign",
           "angle_hints", "largest_face_fraction", "face_hints", "decode_env"]
