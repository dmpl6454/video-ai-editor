"""The §3.4 delivery and importance INPUTS per sentence — computed from the
speech and audio layers (and the PCM for pitch), never stored in the speech
layer: lane C's `Features` model forbids more than the §3.2 lexical fields,
and the inputs are a pure function of what the layers already hold, so the
semantic layer recomputes them on demand and every run agrees.

  rms_z            the sentence's mean level over its speech frames, z-scored
                   WITHIN its speaker (σ floored at RMS_STD_FLOOR_DB)
  pitch_range_st   autocorrelation pitch range in semitones (10th–90th pct)
  stretch          the speaker's median wpm / the sentence's wpm
  filler_rate      fillers / (words + fillers)
  answer_len_norm  an answer's word count / 40 (0 for a non-answer)
  topic_peak       the sentence with the highest lexical centrality
  repeat           the sentence is the `dup` of a repeat
  false_start      the sentence holds a false-start fragment
  imperative       the sentence's kind
"""
from __future__ import annotations

import numpy as np

from .audio import HZ, decode_env, speech_mask
from .fillers import pitch_range_st, pitch_track

RMS_STD_FLOOR_DB = 1.0
ANSWER_LEN_FULL_WORDS = 40.0


def _levels(sents: list[dict], audio: dict) -> dict[str, float]:
    """Mean dBFS of each sentence over its speech frames (NaN when none)."""
    env = decode_env(audio) if audio.get("env_10ms") else np.zeros(0)
    speech = speech_mask(audio) if len(env) else np.zeros(0, dtype=bool)
    out: dict[str, float] = {}
    for s in sents:
        a, b = int(s["t0"] * HZ), max(int(s["t0"] * HZ) + 1, int(s["t1"] * HZ))
        seg = env[a:b][speech[a:b]] if len(env) else np.zeros(0)
        out[s["id"]] = float(seg.mean()) if len(seg) else float("nan")
    return out


def _z_and_stretch(sents: list[dict], levels: dict[str, float]) -> dict[str, tuple[float, float]]:
    by_spk: dict[str, list[dict]] = {}
    for s in sents:
        by_spk.setdefault(str(s.get("spk")), []).append(s)
    out: dict[str, tuple[float, float]] = {}
    for rows in by_spk.values():
        vals = np.array([levels[s["id"]] for s in rows if not np.isnan(levels[s["id"]])])
        mu = float(vals.mean()) if len(vals) else 0.0
        sd = max(RMS_STD_FLOOR_DB, float(vals.std())) if len(vals) > 1 else RMS_STD_FLOOR_DB
        wpms = [float((s.get("features") or {}).get("wpm") or 0) for s in rows]
        med = float(np.median([w for w in wpms if w > 0])) if any(w > 0 for w in wpms) else 0.0
        for s, wpm in zip(rows, wpms):
            m = levels[s["id"]]
            out[s["id"]] = (0.0 if np.isnan(m) else round((m - mu) / sd, 2),
                            round(med / wpm, 3) if wpm > 0 and med > 0 else 1.0)
    return out


def _topic_peak(sents: list[dict], tokens_of) -> str | None:
    if not sents:
        return None
    from .speech import STOP
    bags = [{t for t in tokens_of(s) if t not in STOP and len(t) > 2} for s in sents]
    df: dict[str, int] = {}
    for bag in bags:
        for t in bag:
            df[t] = df.get(t, 0) + 1
    scores = [sum(df[t] - 1 for t in bag) / max(1, len(bag)) for bag in bags]
    return sents[int(np.argmax(scores))]["id"]


def sentence_inputs(speech: dict, audio: dict, pcm: np.ndarray | None = None, sr: int = 16000) -> dict[str, dict]:
    """{sentence id: inputs} — see the module docstring."""
    from .speech import content_tokens
    sents = speech.get("sentences") or []
    levels = _levels(sents, audio)
    zs = _z_and_stretch(sents, levels)
    flags = speech.get("flags") or {}
    dups = {r["dup"] for r in flags.get("repeats") or []}
    fs = flags.get("false_starts") or []
    peak = _topic_peak(sents, lambda s: content_tokens(s["text"]))
    out: dict[str, dict] = {}
    for s in sents:
        f = s.get("features") or {}
        n_words, n_fill = int(f.get("len_words") or 0), int(f.get("fillers") or 0)
        pitch = 0.0
        if pcm is not None and len(pcm):
            pitch = round(pitch_range_st(pitch_track(pcm, sr, s["t0"], s["t1"])), 2)
        z, stretch = zs[s["id"]]
        out[s["id"]] = {
            "rms_z": z, "pitch_range_st": pitch, "stretch": stretch,
            "filler_rate": round(n_fill / max(1, n_words + n_fill), 3),
            "answer_len_norm": round(min(1.0, n_words / ANSWER_LEN_FULL_WORDS), 3) if s.get("answer_of") else 0.0,
            "topic_peak": s["id"] == peak, "repeat": s["id"] in dups,
            "false_start": any(s["t0"] - 0.05 <= x["t0"] <= s["t1"] + 0.05 for x in fs),
            "imperative": s.get("kind") == "imperative",
        }
    return out


__all__ = ["RMS_STD_FLOOR_DB", "sentence_inputs"]
