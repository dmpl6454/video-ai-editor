"""Truth shapes and measurement helpers for the Editor Brain fixtures (EB1-A).

Every time below is a SOURCE second of the file it belongs to. The talking
head (TH) has one clock. The two-camera podcast (P2) has one reference clock
— the recorder's — and per-file offsets: `file_t = ref_t + offsets[file]`
(positive = that file starts before the recorder and so lags it, spec §2.4).

Field guide (the JSON is `dataclasses.asdict` of these, key order as declared):

  Utt        one synthesised or silent piece on the clock.
             kind ∈ speech | filler | acoustic_filler | backchannel |
                    false_start | pause | gap | silence
             start/end = the wav span (concat offsets); voiced_* = the first /
             last sample above −34 dBFS inside it (None for silences).
             sent = the sentence id it belongs to (a filler has none);
             turn = the turn id (P2). gain_db / length_scale = how it was
             synthesised (0 / 1.0 = the voice's plain rendering).
  Word       approximate word timing inside a sentence: the voiced span split
             in proportion to character count (+1 per token). `approx` is
             always True — neither Piper amy nor `say` exposes alignments —
             so a consumer compares words by TEXT and sentence, never by ±50 ms.
  Sentence   id `s_NNNN`; t0/t1 = voiced span; utts = the Utt ids it spans
             (the emphasised sentence spans two: its clauses are synthesised
             separately so `clause_start` is exact); kind is a planted role:
             statement | question | quotable | throwaway | emphasis | retake |
             retake_dup | closing | contrast | imperative | emotional | answer
             features: len_words, strong_number, weak_number, superlative,
             claim, contrast_words (count), conclusion_marker, imperative,
             anaphora_start, weak_start, comma_clause — hand-annotated from
             the script by the spec §3.4 definitions.
             answer_of = the question sentence this sentence answers (P2).
  Turn       P2: id `t_NNNN`, speaker S1 (host) | S2 (guest), t0/t1 = the
             span from the first utterance's wav start to the last's wav end,
             kind turn | backchannel, expected_angle = the camera whose close
             shows the speaker ("cam_a" for S1, "cam_b" for S2) or None for a
             backchannel (the picture must NOT switch on it).
  Pause      planted silence. kind: planted (TH's five 1.5 s pauses) |
             in_turn | turn_boundary | retake_gap | island_gap;
             protection: None or the §4.6.3 class ("emotion") — a protected
             pause must be KEPT at ≥ 45 % of its length.
  Retake     TH: `of` is the first rendering, `dup` the repeat (the later one
             is the duplicate, spec §3.4 repeat_of), gap_s between them.
  Emphasis   the sentence rendered `gain_db` louder and `length_scale` slower;
             clause_start = the voiced start of the clause after the comma;
             plain_rms_db / plain_duration = the SAME clauses rendered plain
             (synthesised alongside, never placed in the narration) so the
             +6 dB / slower claims are measurable against a twin. `length_scale`
             is Piper's 1.25, which measures +16 % voiced duration for amy (1.15
             measures only +7 %): the brief's "15 % slower" as MEASURED.
  FalseStart P2: the fragment utterance, its span, and the sentence it
             restarts (`kept_sent`).
  Overlap    P2: the span where both speakers talk, and the two turns.
  Seam       P2: a planted stretch a tighten pass removes; removed_est_s is
             the length left after the energy-5 pads (0.15 s each side; the
             0.30 s floor at a turn boundary; 45 % kept when protected);
             angle_change_required = removed_est_s ≥ 0.4 (spec §4.3 rule 1b).

Acoustic fillers: TH's three `uh` islands are `acoustic_filler` Utts — 0.24 s
formant-synthesised schwas, 4 dB under the speech RMS, 0.25 s after the
sentence before and 0.35 s before the next (`brain_fixture_scripts` says why
those gaps). Neither whisper.cpp nor faster-whisper writes a token over them
and whisper.cpp times no word onto them; only an acoustic detector finds them.

Source keys in the golden graphs are `src_` + sha256(file bytes)[:24] (content
keys, stable across machines; C's `SRC_KEY_RE`), never the identity key.

Top level: THTruth / P2Truth (below) plus the file paths in
`brain_fixtures.THFixture` / `P2Fixture`. `clicks` are the click/flash
instants on the REFERENCE clock; every one sits on the `fps` grid.
"""
from __future__ import annotations

import json
import math
import types
import typing
from dataclasses import asdict, dataclass, fields, is_dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class Word:
    id: str
    text: str
    t0: float
    t1: float
    approx: bool = True


@dataclass(frozen=True)
class Utt:
    id: str
    kind: str
    speaker: str
    text: str
    start: float
    end: float
    voiced_start: float | None
    voiced_end: float | None
    sent: str | None = None
    turn: str | None = None
    gain_db: float = 0.0
    length_scale: float = 1.0

    @property
    def voiced(self) -> tuple[float, float]:
        if self.voiced_start is None or self.voiced_end is None:
            return (self.start, self.end)
        return (self.voiced_start, self.voiced_end)


@dataclass(frozen=True)
class Sentence:
    id: str
    speaker: str
    text: str
    t0: float
    t1: float
    utts: tuple[str, ...]
    kind: str
    is_question: bool
    features: dict
    words: tuple[Word, ...]
    answer_of: str | None = None
    clause_start: float | None = None


@dataclass(frozen=True)
class Turn:
    id: str
    speaker: str
    t0: float
    t1: float
    sents: tuple[str, ...]
    kind: str
    expected_angle: str | None


@dataclass(frozen=True)
class Pause:
    id: str
    t0: float
    t1: float
    kind: str
    protection: str | None = None
    after_sent: str | None = None
    before_sent: str | None = None


@dataclass(frozen=True)
class Retake:
    of: str
    dup: str
    gap_s: float


@dataclass(frozen=True)
class Emphasis:
    sent: str
    clause_start: float
    gain_db: float
    length_scale: float
    plain_rms_db: float
    plain_duration: float


@dataclass(frozen=True)
class FalseStart:
    utt: str
    kept_sent: str
    t0: float
    t1: float
    text: str


@dataclass(frozen=True)
class Overlap:
    t0: float
    t1: float
    turns: tuple[str, str]


@dataclass(frozen=True)
class Seam:
    t0: float
    t1: float
    kind: str
    removed_est_s: float
    angle_change_required: bool
    protected: bool = False


@dataclass(frozen=True)
class THTruth:
    fps: int
    sample_rate: int
    duration: float
    voice: str
    utts: tuple[Utt, ...]
    sentences: tuple[Sentence, ...]
    pauses: tuple[Pause, ...]
    fillers_lexical: tuple[str, ...]
    fillers_acoustic: tuple[str, ...]
    quotable: str
    throwaway: str
    question: str
    closing: str
    emphasis: Emphasis
    retake: Retake
    scene_cuts: tuple[float, ...]
    clicks: tuple[float, ...]
    sids: dict

    def sentence(self, sid: str) -> Sentence:
        return next(s for s in self.sentences if s.id == sid)

    def utt(self, uid: str) -> Utt:
        return next(u for u in self.utts if u.id == uid)


@dataclass(frozen=True)
class P2Truth:
    fps: int
    sample_rate: int
    duration: float
    voices: dict
    speakers: dict
    utts: tuple[Utt, ...]
    sentences: tuple[Sentence, ...]
    turns: tuple[Turn, ...]
    pauses: tuple[Pause, ...]
    fillers_lexical: tuple[str, ...]
    backchannels: tuple[str, ...]
    false_start: FalseStart
    overlap: Overlap
    emotional: str
    quotable: str
    throwaway: str
    qa_pairs: tuple[tuple[str, str], ...]
    offsets: dict
    own_mic: dict
    mic: dict
    clicks: tuple[float, ...]
    seams: tuple[Seam, ...]
    sids: dict

    def turn(self, tid: str) -> Turn:
        return next(t for t in self.turns if t.id == tid)

    def sentence(self, sid: str) -> Sentence:
        return next(s for s in self.sentences if s.id == sid)

    def utt(self, uid: str) -> Utt:
        return next(u for u in self.utts if u.id == uid)


# --------------------------------------------------------------------------
# JSON round trip (generic over the dataclasses above)
# --------------------------------------------------------------------------

def to_json(obj) -> str:
    return json.dumps(asdict(obj), indent=1, ensure_ascii=False)


def _union_members(t):
    if isinstance(t, types.UnionType) or typing.get_origin(t) is typing.Union:
        return typing.get_args(t)
    return (t,)


def from_dict(cls, data: dict):
    hints = typing.get_type_hints(cls)
    kw = {}
    for f in fields(cls):
        v = data.get(f.name)
        t = hints[f.name]
        dc = [m for m in _union_members(t) if is_dataclass(m)]
        if dc and isinstance(v, dict):
            v = from_dict(dc[0], v)
        elif typing.get_origin(t) is tuple and v is not None:
            args = typing.get_args(t)
            if args and is_dataclass(args[0]):
                v = tuple(from_dict(args[0], x) for x in v)
            elif args and typing.get_origin(args[0]) is tuple:
                v = tuple(tuple(x) for x in v)
            else:
                v = tuple(v)
        kw[f.name] = v
    return cls(**kw)


def from_json(cls, text: str):
    return from_dict(cls, json.loads(text))


# --------------------------------------------------------------------------
# measurement helpers shared by the builder and the tests
# --------------------------------------------------------------------------

def rms_db(pcm: np.ndarray) -> float:
    ms = float(np.mean(np.square(pcm, dtype=np.float64))) if pcm.size else 0.0
    return 10 * math.log10(ms) if ms > 1e-24 else -240.0


def upsample2(pcm: np.ndarray) -> np.ndarray:
    """Exact ×2 band-limited upsampling (FFT zero-padding), numpy only:
    Piper and `say` speak at 22 050 Hz, the P2 clock is 44 100 Hz so every
    1/20 s frame and every planted offset is a whole number of samples."""
    x = np.asarray(pcm, dtype=np.float64)
    n = len(x)
    spec = np.fft.rfft(x)
    out = np.fft.irfft(spec, 2 * n) * 2.0
    return out.astype(np.float32)


def env_10ms_db(pcm: np.ndarray, sr: int) -> np.ndarray:
    """RMS per 10 ms frame in dBFS (float); the audio layer's `env_10ms`."""
    n = max(1, int(round(0.01 * sr)))
    frames = len(pcm) // n
    if frames == 0:
        return np.zeros(0, np.float32)
    x = np.asarray(pcm[: frames * n], dtype=np.float64).reshape(frames, n)
    ms = np.mean(np.square(x), axis=1)
    return (10 * np.log10(np.maximum(ms, 1e-12))).astype(np.float32)


def env_10ms_int8_b64(pcm: np.ndarray, sr: int) -> str:
    import base64
    db = np.clip(np.round(env_10ms_db(pcm, sr)), -127, 0).astype(np.int8)
    return base64.b64encode(db.tobytes()).decode("ascii")


def click_times_strict(path: Path, *, rate: int = 48000, thresh: float = 0.7) -> list[float]:
    """`timing_fixtures.click_times` at a threshold speech never reaches
    (the fixtures' speech peaks ≤ 0.5, the clicks at 0.95)."""
    from timing_fixtures import click_times
    return click_times(path, rate=rate, thresh=thresh)


def av_offsets_ms_strict(path: Path, *, thresh: float = 0.7) -> list[float]:
    """`timing_fixtures.av_offsets_ms` with the strict click threshold: for
    every flash onset, (nearest click − flash) in ms. Use this, not the
    default-threshold one, on anything that carries speech."""
    from timing_fixtures import flash_onsets
    fl = flash_onsets(path)
    cl = click_times_strict(path, thresh=thresh)
    out = []
    for f in fl:
        if not cl:
            break
        c = min(cl, key=lambda x: abs(x - f))
        if abs(c - f) < 0.4:
            out.append((c - f) * 1000.0)
    return out


def approx_words(prefix: str, text: str, t0: float, t1: float) -> tuple[Word, ...]:
    toks = text.split()
    weights = [len(t.strip(".,!?;:—")) + 1 for t in toks]
    total = float(sum(weights)) or 1.0
    out = []
    t = t0
    for i, (tok, w) in enumerate(zip(toks, weights)):
        d = (t1 - t0) * w / total
        out.append(Word(f"{prefix}_{i:02d}", tok, round(t, 4), round(t + d, 4), True))
        t += d
    return tuple(out)


__all__ = ["Word", "Utt", "Sentence", "Turn", "Pause", "Retake", "Emphasis", "FalseStart", "Overlap", "Seam",
           "THTruth", "P2Truth", "to_json", "from_dict", "from_json", "rms_db", "upsample2", "env_10ms_db",
           "env_10ms_int8_b64", "click_times_strict", "av_offsets_ms_strict", "approx_words"]
