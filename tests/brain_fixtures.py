"""Editor Brain fixtures with truth by construction (EB1-A).

Two fixtures, built once per content key under `CACHE_ROOT/brain/<key>/`
(the bench's cache dir, never the repo, never WORKDIR) and read from the
manifest afterwards:

  th/   the talking head — `th_speech.wav` (Piper amy, deterministic, the
        clean speech), `th_audio.wav` (the same plus a click at every planted
        pause boundary; what the videos carry), `th_16x9.mp4` (1920×1080,
        bar-code sid 1) and `th_9x16.mp4` (1080×1920, sid 2) at 30 fps with a
        one-frame flash at every click and a scene-colour strip that changes
        at the five `scene_cuts`; `th_truth.json`.
  p2/   the two-camera podcast — `p2_recorder.wav` (44.1 kHz mono: host +
        guest + a click at every turn start; THE reference), `p2_cam_a.mp4`
        (host close, sid 1, `file_t = ref_t + 0.35`), `p2_cam_b.mp4` (guest
        close, sid 2, `file_t = ref_t − 0.20`), both 320×180 at 20 fps with a
        pulsing strip while their speaker talks and a flash at every click,
        and camera-mic audio: own speaker 0 dB, the other −6 dB, the mix
        −12 dB with a 40 ms reflection at −6 dB and room noise at −55 dBFS,
        clicks undelayed; `p2_cam_{a,b}_mix.wav` are the reference-clock
        mixes before the room treatment; `p2_truth.json`.

Why 20 fps for P2: the planted offsets +0.35 / −0.20 s are whole frames
only at 20 (7 / 4) or 60 fps, and the bar code has 12 frame bits (4 095
frames), which a 3-minute file exceeds at 60. At 20 fps every click, flash
and offset is frame- AND sample-exact (44 100 / 20 = 2 205), so the raw
angles measure `av_offsets_ms == 0` and the click tracks recover the offsets
to the sample. Why 44.1 kHz: 22 050 is not divisible by 20 fps.

Truth field guide: `brain_fixture_truth.py`. Scripts: `brain_fixture_scripts.py`.
Golden graphs derived from this truth: `tests/gen_brain_fixture_graphs.py`
→ `tests/goldens/brain/graphs/{talking_head,two_cam_podcast}.json`.

Build: `build_brain_fixtures()` (≈ 40 s cold on an M4; a manifest read warm),
or `uv run python -m tests.brain_fixtures` — no: `cd tests && uv run python
brain_fixtures.py` prints the paths. P2 is skipped cleanly (`p2 is None`,
`p2_skip_reason`) when macOS `say -v Daniel` is absent; TH needs the cached
Piper voice (`narration.piper_voice_available`). Nothing here downloads.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from benchmark import media as _media
from benchmark import narration as _nar
from benchmark.media import add_clicks, render_barcode_angle
from benchmark.narration import (EN_VOICE, FILLER_SPOKEN, GAP_S, PiperSynth, SaySynth, _read_wav,
                                 _trim_to_voiced, _voiced_bounds, _write_wav, normalize_rms,
                                 piper_voice_available, say_available, synthesize_script)
from brain_fixture_scripts import (P2_EMOTION_PAUSE_S, P2_FALSE_START_GAP_S, P2_FPS, P2_GUEST_BC_SCALE, P2_HOST_BC_RATE,
                                   P2_HOST_RATE, P2_IN_TURN_PAUSE_S, P2_ITEM_GAP_S, P2_LONG_GAP_S, P2_MIC, P2_OFFSETS,
                                   P2_OWN_MIC, P2_SIDS, P2_SPEAKERS, P2_SPEECH_RMS_DB, P2_SR, P2_STRIP_YUV, P2_TURN_GAP_S,
                                   P2_TURNS, TH_EMPH_GAIN_DB, TH_EMPH_SCALE, TH_EMPHASIS_CLAUSES, TH_BASE_DB, TH_FPS,
                                   TH_GAINS, TH_ROLES, TH_SCALES, TH_SCENE_YUV, TH_SCRIPT, TH_SENTENCES, TH_SIDS,
                                   TH_SPEECH_RMS_DB, Item)
from brain_fixture_truth import (Emphasis, FalseStart, Overlap, P2Truth, Pause, Retake, Seam, Sentence, THTruth, Turn,
                                 Utt, approx_words, av_offsets_ms_strict, click_times_strict, from_dict, rms_db,
                                 upsample2)

__all__ = ["BRAIN_FIXTURE_VERSION", "THFixture", "P2Fixture", "BrainFixtures", "fixture_key", "default_root",
           "build_brain_fixtures", "click_times_strict", "av_offsets_ms_strict"]

#: Bump whenever synthesis or truth changes shape; part of the cache key.
BRAIN_FIXTURE_VERSION = 1
HOST_VOICE = "Daniel"
#: Energy-5 pads used for `Seam.removed_est_s` (spec §4.6 / brief constants).
KEEP_PAD_S, TURN_FLOOR_S, PROTECTED_KEEP = 0.15, 0.30, 0.45


@dataclass(frozen=True)
class THFixture:
    root: str
    speech_wav: str
    audio_wav: str
    video_16x9: str
    video_9x16: str
    truth_json: str
    truth: THTruth


@dataclass(frozen=True)
class P2Fixture:
    root: str
    recorder_wav: str
    cam_a: str
    cam_b: str
    cam_a_mix_wav: str
    cam_b_mix_wav: str
    truth_json: str
    truth: P2Truth


@dataclass(frozen=True)
class BrainFixtures:
    root: str
    key: str
    th: THFixture
    p2: P2Fixture | None
    p2_skip_reason: str | None

    @property
    def files(self) -> list[str]:
        out = [self.th.speech_wav, self.th.audio_wav, self.th.video_16x9, self.th.video_9x16, self.th.truth_json]
        if self.p2:
            out += [self.p2.recorder_wav, self.p2.cam_a, self.p2.cam_b, self.p2.truth_json]
        return out


def fixture_key() -> str:
    material = {"v": BRAIN_FIXTURE_VERSION, "th": [TH_SCRIPT, TH_ROLES, TH_GAINS, TH_SCALES, TH_FPS, TH_SIDS, TH_SPEECH_RMS_DB],
                "p2": [[asdict(t) for t in P2_TURNS], P2_FPS, P2_SR, P2_OFFSETS, P2_MIC, P2_SIDS, P2_SPEECH_RMS_DB,
                       P2_TURN_GAP_S, HOST_VOICE, P2_HOST_RATE, P2_HOST_BC_RATE, P2_GUEST_BC_SCALE],
                "voice": EN_VOICE, "gap": GAP_S, "media": _media.MEDIA_VERSION,
                "schwa": [SCHWA_F0_HZ, SCHWA_DROOP_ST, SCHWA_S, SCHWA_FORMANTS]}
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()[:12]


def default_root() -> Path:
    return _media.CACHE_ROOT / "brain"


# --------------------------------------------------------------------------
# talking head
# --------------------------------------------------------------------------

def _frame_ceil(t: float, fps: int) -> float:
    return math.ceil(t * fps - 1e-9) / fps


def _frame_floor(t: float, fps: int) -> float:
    return math.floor(t * fps + 1e-9) / fps


def _th_utts(nar: _nar.Narration) -> list[Utt]:
    sent_ids = {key: f"s_{i + 1:04d}" for i, key in enumerate(TH_SENTENCES)}
    out = []
    for u in nar.utterances:
        role = TH_ROLES.get(u.index)
        if u.kind == "speech":
            kind, sent = "speech", sent_ids[role]
        elif u.kind == "filler":
            kind, sent = ("acoustic_filler" if u.text == "uh" else "filler"), None
        else:
            kind, sent = u.kind, None
        out.append(Utt(f"u_{u.index:04d}", kind, "S1", u.text, u.start, u.end, u.voiced_start, u.voiced_end,
                       sent=sent, gain_db=TH_GAINS.get(u.index, 0.0), length_scale=TH_SCALES.get(u.index, 1.0)))
    return out


def _th_sentences(utts: list[Utt]) -> list[Sentence]:
    out = []
    for i, (key, (kind, text, feats)) in enumerate(TH_SENTENCES.items()):
        sid = f"s_{i + 1:04d}"
        mine = [u for u in utts if u.sent == sid]
        t0, t1 = mine[0].voiced[0], mine[-1].voiced[1]
        clause = mine[1].voiced[0] if len(mine) > 1 else None
        out.append(Sentence(sid, "S1", text, t0, t1, tuple(u.id for u in mine), kind, kind == "question",
                            feats, approx_words(f"w_{i + 1:04d}", text, t0, t1), None, clause))
    return out


def _th_pauses(utts: list[Utt]) -> list[Pause]:
    out = []
    n = 0
    for i, u in enumerate(utts):
        if u.kind not in ("pause", "silence"):
            continue
        role = TH_ROLES.get(int(u.id[2:]))
        kind = role if role in ("planted", "retake_gap") else "island_gap"
        prev = next((x.sent for x in reversed(utts[:i]) if x.sent), None)
        nxt = next((x.sent for x in utts[i + 1:] if x.sent), None)
        n += 1
        out.append(Pause(f"p_{n:04d}", u.start, u.end, kind, None, prev, nxt))
    return out


def _plain_twin(voice: PiperSynth, parts_dir: Path) -> tuple[float, float]:
    """The emphasised sentence rendered plain (no gain, no length scale),
    the same two clauses joined with `GAP_S`: (rms_db over its voiced span,
    voiced duration)."""
    pieces = []
    for j, clause in enumerate(TH_EMPHASIS_CLAUSES):
        raw, sr = voice(clause, parts_dir / f"plain_{j}.wav")
        if pieces:
            pieces.append(np.zeros(int(round(GAP_S * sr)), np.float32))
        pieces.append(normalize_rms(_trim_to_voiced(raw, sr), sr, TH_SPEECH_RMS_DB))
    pcm = np.concatenate(pieces) * float(10 ** (TH_BASE_DB / 20.0))
    b = _voiced_bounds(pcm, sr)
    assert b is not None
    seg = pcm[int(b[0] * sr):int(b[1] * sr)]
    return rms_db(seg), b[1] - b[0]


def _th_truth(nar: _nar.Narration, plain: tuple[float, float]) -> THTruth:
    utts = _th_utts(nar)
    sents = _th_sentences(utts)
    by_kind = {s.kind: s.id for s in sents}
    pauses = _th_pauses(utts)
    clicks = []
    for p in pauses:
        if p.kind == "planted":
            clicks += [round(_frame_ceil(p.t0, TH_FPS), 6), round(_frame_floor(p.t1, TH_FPS), 6)]
    emph = next(s for s in sents if s.kind == "emphasis")
    return THTruth(
        fps=TH_FPS, sample_rate=nar.sample_rate, duration=nar.duration, voice=nar.voice,
        utts=tuple(utts), sentences=tuple(sents), pauses=tuple(pauses),
        fillers_lexical=tuple(u.id for u in utts if u.kind == "filler"),
        fillers_acoustic=tuple(u.id for u in utts if u.kind == "acoustic_filler"),
        quotable=by_kind["quotable"], throwaway=by_kind["throwaway"], question=by_kind["question"],
        closing=by_kind["closing"],
        emphasis=Emphasis(emph.id, float(emph.clause_start), TH_EMPH_GAIN_DB, TH_EMPH_SCALE, plain[0], plain[1]),
        retake=Retake(by_kind["retake"], by_kind["retake_dup"], _nar_silence(utts, "retake_gap")),
        scene_cuts=_media.scene_cuts_for(nar.duration), clicks=tuple(clicks), sids=dict(TH_SIDS))


def _nar_silence(utts: list[Utt], role: str) -> float:
    u = next(x for x in utts if x.kind == "silence" and TH_ROLES.get(int(x.id[2:])) == role)
    return round(u.end - u.start, 4)


def _th_strip(truth: THTruth) -> list[tuple[float, float, tuple[int, int, int]]]:
    bounds = (0.0, *truth.scene_cuts, truth.duration + 1.0)
    return [(bounds[i], bounds[i + 1], TH_SCENE_YUV[i]) for i in range(len(bounds) - 1)]


#: The acoustic-filler island: a formant-synthesised schwa. Measured
#: 2026-09-29 (faster-whisper `small`, offline): whisper transcribes Piper's
#: deterministic "Uh." as "Ah,"/"Uh," in most placements, and where it drops
#: the token it still starts the NEXT word's DTW span on the island, which
#: `transcribe._refine_words` then snaps onto it (the following word is
#: mis-timed over the filler). This schwa — constant f0 118 Hz with a 0.3 st
#: droop, F1/F2/F3 600/1150/2500 Hz, 0.28 s, 40 ms fades — yields no token
#: and no word overlap in every configuration tried, has a pitch range of
#: 0.2 st and a voiced length of 0.25 s: exactly the §3.4 detector's target.
SCHWA_F0_HZ, SCHWA_DROOP_ST, SCHWA_S = 118.0, 0.3, 0.28
SCHWA_FORMANTS = ((600.0, 90.0), (1150.0, 110.0), (2500.0, 160.0))


def _resonate(x: np.ndarray, sr: int, fc: float, bw: float) -> np.ndarray:
    r = math.exp(-math.pi * bw / sr)
    th = 2 * math.pi * fc / sr
    a1, a2 = -2 * r * math.cos(th), r * r
    g = (1 - r) * math.sqrt(1 - 2 * r * math.cos(2 * th) + r * r)
    y = np.zeros(len(x))
    y1 = y2 = 0.0
    for i, v in enumerate(x):
        y0 = g * v - a1 * y1 - a2 * y2
        y[i], y2, y1 = y0, y1, y0
    return y


def _schwa(sr: int = 22050) -> np.ndarray:
    n = int(SCHWA_S * sr)
    t = np.arange(n) / sr
    f = SCHWA_F0_HZ * 2 ** (-SCHWA_DROOP_ST / 12 * t / SCHWA_S)
    phase = 2 * math.pi * np.cumsum(f) / sr
    x = np.zeros(n)
    for k in range(1, int(4000 / SCHWA_F0_HZ)):
        x += np.sin(k * phase) / (k ** 1.6)
    for fc, bw in SCHWA_FORMANTS:
        x = _resonate(x, sr, fc, bw)
    fade = int(0.04 * sr)
    env = np.ones(n)
    env[:fade] = 0.5 - 0.5 * np.cos(math.pi * np.arange(fade) / fade)
    env[-fade:] = env[:fade][::-1]
    x = x * env
    return (x / np.abs(x).max()).astype(np.float32)


class _THVoice:
    """Piper amy (deterministic) for every token except the `uh` islands,
    which are `_schwa` (see above). Same call shape as `PiperSynth`."""

    def __init__(self) -> None:
        self.piper = PiperSynth(EN_VOICE, deterministic=True)
        self.sr, self.name = self.piper.sr, self.piper.name

    def __call__(self, text: str, dst: Path, *, length_scale: float | None = None) -> tuple[np.ndarray, int]:
        if text != FILLER_SPOKEN["uh"]:
            return self.piper(text, dst, length_scale=length_scale)
        dst.parent.mkdir(parents=True, exist_ok=True)
        _write_wav(dst, _schwa(self.sr), self.sr)
        return _read_wav(dst)


def _build_th(base: Path) -> THFixture:
    out = base / "th"
    out.mkdir(parents=True, exist_ok=True)
    voice = _THVoice()
    nar = synthesize_script(TH_SCRIPT, out, voice=voice, gain_db_by_index=TH_GAINS,
                            length_scale_by_index=TH_SCALES, name="th_speech",
                            normalize_rms_db=TH_SPEECH_RMS_DB)
    truth = _th_truth(nar, _plain_twin(voice, out / "parts_plain"))
    pcm, sr = _read_wav(Path(nar.wav))
    if float(np.abs(pcm).max()) > 0.6:
        raise RuntimeError(f"TH speech peaks at {np.abs(pcm).max():.2f}; the click detector needs ≤ 0.6")
    audio = out / "th_audio.wav"
    _write_wav(audio, add_clicks(pcm, sr, truth.clicks), sr)
    strip = _th_strip(truth)
    v16 = render_barcode_angle(Path(nar.wav), out / "th_16x9.mp4", sid=TH_SIDS["16x9"], size=(1920, 1080),
                               offset_s=0.0, fps=TH_FPS, click_times=truth.clicks, strip=strip)
    v9 = render_barcode_angle(Path(nar.wav), out / "th_9x16.mp4", sid=TH_SIDS["9x16"], size=(1080, 1920),
                              offset_s=0.0, fps=TH_FPS, click_times=truth.clicks, strip=strip)
    tj = out / "th_truth.json"
    tj.write_text(json.dumps(asdict(truth), indent=1, ensure_ascii=False), encoding="utf-8")
    return THFixture(str(out), nar.wav, str(audio), str(v16), str(v9), str(tj), truth)


# --------------------------------------------------------------------------
# two-camera podcast
# --------------------------------------------------------------------------

def _item_text(item: Item) -> str:
    return FILLER_SPOKEN[item.text] if item.kind == "f" else item.text


def _item_scale(spk: str, item: Item) -> float | None:
    if item.kind == "bc":
        return P2_GUEST_BC_SCALE.get(item.text) if spk == "S2" else P2_HOST_RATE / P2_HOST_BC_RATE
    return item.length_scale


def _p2_synthesise(parts_dir: Path) -> dict[tuple[int, int], np.ndarray]:
    """Every spoken item, trimmed to its voiced bounds ± PART_PAD, at 22 050
    Hz and the voice's own level (`say` in four threads, Piper serially)."""
    guest = PiperSynth(EN_VOICE, deterministic=True)
    host = SaySynth(HOST_VOICE, rate=P2_HOST_RATE)
    jobs = [(ti, ii, turn.speaker, item) for ti, turn in enumerate(P2_TURNS)
            for ii, item in enumerate(turn.items) if item.kind != "sil"]

    def run(job):
        ti, ii, spk, item = job
        voice = host if spk == "S1" else guest
        raw, sr = voice(_item_text(item), parts_dir / f"t{ti:02d}_{ii}.wav", length_scale=_item_scale(spk, item))
        assert sr == 22050, sr
        return (ti, ii), _trim_to_voiced(raw, sr)

    parts: dict[tuple[int, int], np.ndarray] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for key, pcm in pool.map(run, [j for j in jobs if j[2] == "S1"]):
            parts[key] = pcm
    for job in [j for j in jobs if j[2] == "S2"]:
        key, pcm = run(job)
        parts[key] = pcm
    return parts


def _speaker_gains(parts: dict[tuple[int, int], np.ndarray]) -> dict[str, float]:
    """One gain per speaker so each speaker's median plain sentence sits at
    `P2_SPEECH_RMS_DB` (Daniel and amy come out at different levels)."""
    levels: dict[str, list[float]] = {"S1": [], "S2": []}
    for (ti, ii), pcm in parts.items():
        item = P2_TURNS[ti].items[ii]
        if item.kind == "s" and item.gain_db == 0.0:
            b = _voiced_bounds(pcm, 22050)
            levels[P2_TURNS[ti].speaker].append(rms_db(pcm[int(b[0] * 22050):int(b[1] * 22050)]))
    return {spk: P2_SPEECH_RMS_DB - float(np.median(v)) for spk, v in levels.items()}


@dataclass
class _Placed:
    utts: list[Utt]
    pcm: dict[str, np.ndarray]       # 44.1 kHz per utt id
    pos: dict[str, int]              # first sample
    turn_starts: list[float]
    pauses: list[Pause]
    false_start: FalseStart | None = None
    overlap: Overlap | None = None


def _turn_start(cursor: float, turn, first: bool) -> float:
    if first:
        return 0.5
    if turn.overlap_s:
        return round((cursor - turn.overlap_s) * P2_FPS) / P2_FPS
    if turn.gap_before:
        return _frame_ceil(cursor, P2_FPS) + turn.gap_before
    return _frame_ceil(cursor + P2_TURN_GAP_S, P2_FPS)


def _utt_kind(item: Item) -> str:
    return {"s": "speech", "f": "filler", "bc": "backchannel", "fs": "false_start"}[item.kind]


def _place_p2(parts: dict[tuple[int, int], np.ndarray], gains: dict[str, float]) -> _Placed:
    P = _Placed([], {}, {}, [], [])
    cursor = 0.0
    n_utt = n_sent = n_pause = 0
    for ti, turn in enumerate(P2_TURNS):
        start = _turn_start(cursor, turn, ti == 0)
        tid = f"t_{ti + 1:04d}"
        if turn.gap_before:
            n_pause += 1
            P.pauses.append(Pause(f"p_{n_pause:04d}", round(start - turn.gap_before, 6), start, "turn_boundary"))
        if turn.overlap_s:
            P.overlap = Overlap(start, round(cursor, 6), (f"t_{ti:04d}", tid))
        t = start
        prev_kind = None
        for ii, item in enumerate(turn.items):
            if item.kind == "sil":
                n_pause += 1
                P.pauses.append(Pause(f"p_{n_pause:04d}", t, t + item.seconds, "in_turn", item.protection,
                                      P.utts[-1].sent if P.utts else None))
                t += item.seconds
                prev_kind = "sil"
                continue
            if ii and prev_kind != "sil":
                t += P2_FALSE_START_GAP_S if prev_kind == "fs" else P2_ITEM_GAP_S
            t = round(t, 6)
            pcm = upsample2(parts[(ti, ii)] * float(10 ** ((gains[turn.speaker] + item.gain_db) / 20.0)))
            n_utt += 1
            uid = f"u_{n_utt:04d}"
            if item.kind in ("s", "bc"):
                n_sent += 1
            sid = f"s_{n_sent:04d}" if item.kind in ("s", "bc") else None
            b = _voiced_bounds(pcm, P2_SR)
            end = round(t + len(pcm) / P2_SR, 6)
            P.utts.append(Utt(uid, _utt_kind(item), turn.speaker, item.text, t, end, t + b[0], t + b[1], sid, tid,
                              item.gain_db, item.length_scale or 1.0))
            P.pcm[uid], P.pos[uid] = pcm, int(round(t * P2_SR))
            if item.kind == "fs":
                # t1 = the restarted sentence's voiced onset, filled in once it is placed
                P.false_start = FalseStart(uid, f"s_{n_sent + 1:04d}", t, -1.0, item.text)
            elif P.false_start is not None and P.false_start.t1 < 0 and sid == P.false_start.kept_sent:
                P.false_start = FalseStart(P.false_start.utt, sid, P.false_start.t0, t + b[0], P.false_start.text)
            t, prev_kind = end, item.kind
        P.turn_starts.append(start)
        cursor = t
    return P


def _p2_sentences(P: _Placed) -> list[Sentence]:
    """One Sentence per spoken `s`/`bc` item, paired with its utterance in
    order; an answer turn's first sentence answers the last question of the
    turn before it."""
    out: list[Sentence] = []
    last_question: str | None = None
    for ti, turn in enumerate(P2_TURNS):
        tid = f"t_{ti + 1:04d}"
        utts = [u for u in P.utts if u.turn == tid and u.sent]
        items = [i for i in turn.items if i.kind in ("s", "bc")]
        assert len(utts) == len(items), (tid, len(utts), len(items))
        q_before, first = (last_question if turn.kind == "a" else None), True
        for u, item in zip(utts, items):
            n = int(u.sent[2:])
            answer = q_before if first else None
            out.append(Sentence(u.sent, turn.speaker, item.text, u.voiced[0], u.voiced[1], (u.id,), item.skind,
                                item.skind == "question", dict(item.features),
                                approx_words(f"w_{n:04d}", item.text, u.voiced[0], u.voiced[1]), answer, None))
            first = False
            if item.skind == "question":
                last_question = u.sent
    return out


def _p2_turns(P: _Placed, sents: list[Sentence]) -> list[Turn]:
    out = []
    for ti, turn in enumerate(P2_TURNS):
        tid = f"t_{ti + 1:04d}"
        mine = [u for u in P.utts if u.turn == tid]
        kind = "backchannel" if turn.kind == "bc" else "turn"
        out.append(Turn(tid, turn.speaker, mine[0].voiced[0], mine[-1].voiced[1],
                        tuple(u.sent for u in mine if u.sent), kind,
                        None if kind == "backchannel" else P2_SPEAKERS[turn.speaker]["angle"]))
    return out


def _p2_seams(P: _Placed, utts_by_id: dict[str, Utt]) -> list[Seam]:
    out = []
    for p in P.pauses:
        length = p.t1 - p.t0
        if p.protection:
            removed = length - max(KEEP_PAD_S, PROTECTED_KEEP * length)
        elif p.kind == "turn_boundary":
            removed = length - TURN_FLOOR_S
        else:
            removed = length - 2 * KEEP_PAD_S
        out.append(Seam(p.t0, p.t1, f"pause:{p.kind}", round(removed, 4), removed >= 0.4, bool(p.protection)))
    fs = P.false_start
    assert fs is not None
    removed = (fs.t1 - fs.t0) - KEEP_PAD_S
    out.append(Seam(fs.t0, fs.t1, "false_start", round(removed, 4), removed >= 0.4))
    for u in P.utts:
        if u.kind != "filler":
            continue
        nxt = utts_by_id[f"u_{int(u.id[2:]) + 1:04d}"]
        removed = (nxt.voiced[0] - u.voiced[0]) - KEEP_PAD_S
        out.append(Seam(u.voiced[0], nxt.voiced[0], "filler", round(removed, 4), removed >= 0.4))
    return sorted(out, key=lambda s: s.t0)


def _p2_truth(P: _Placed, duration: float) -> P2Truth:
    sents = _p2_sentences(P)
    turns = _p2_turns(P, sents)
    by_id = {u.id: u for u in P.utts}
    by_kind = {s.kind: s.id for s in sents}
    qa = tuple((s.answer_of, s.id) for s in sents if s.answer_of)
    assert P.overlap is not None and P.false_start is not None
    return P2Truth(
        fps=P2_FPS, sample_rate=P2_SR, duration=duration,
        voices={"S1": f"say:{HOST_VOICE}", "S2": EN_VOICE},
        speakers={k: dict(v) for k, v in P2_SPEAKERS.items()},
        utts=tuple(P.utts), sentences=tuple(sents), turns=tuple(turns), pauses=tuple(P.pauses),
        fillers_lexical=tuple(u.id for u in P.utts if u.kind == "filler"),
        backchannels=tuple(t.id for t in turns if t.kind == "backchannel"),
        false_start=P.false_start, overlap=P.overlap, emotional=by_kind["emotional"],
        quotable=by_kind["quotable"], throwaway=by_kind["throwaway"], qa_pairs=qa,
        offsets=dict(P2_OFFSETS), own_mic=dict(P2_OWN_MIC), mic=dict(P2_MIC),
        clicks=tuple(round(t, 6) for t in P.turn_starts), seams=tuple(_p2_seams(P, by_id)), sids=dict(P2_SIDS))


def _stems(P: _Placed) -> dict[str, np.ndarray]:
    total = max(P.pos[u.id] + len(P.pcm[u.id]) for u in P.utts) + int(1.0 * P2_SR)
    total = int(math.ceil(total / (P2_SR // P2_FPS))) * (P2_SR // P2_FPS)
    stems = {"S1": np.zeros(total, np.float32), "S2": np.zeros(total, np.float32)}
    for u in P.utts:
        i = P.pos[u.id]
        stems[u.speaker][i:i + len(P.pcm[u.id])] += P.pcm[u.id]
    return stems


def _pulses(P: _Placed, spk: str, color) -> list[tuple[float, float, tuple[int, int, int]]]:
    return [(u.voiced[0], u.voiced[1], color) for u in P.utts if u.speaker == spk]


def _render_p2(out: Path, P: _Placed, truth: P2Truth, stems: dict[str, np.ndarray]) -> P2Fixture:
    rec = out / "p2_recorder.wav"
    _write_wav(rec, add_clicks(stems["S1"] + stems["S2"], P2_SR, truth.clicks), P2_SR)
    other = float(10 ** (P2_MIC["other_speaker_db"] / 20.0))
    mixes = {"cam_a": stems["S1"] + other * stems["S2"], "cam_b": stems["S2"] + other * stems["S1"]}
    paths = {}
    for i, (cam, mix) in enumerate(mixes.items()):
        mix_wav = out / f"p2_{cam}_mix.wav"
        _write_wav(mix_wav, mix, P2_SR)
        spk = P2_OWN_MIC[cam]
        paths[cam] = render_barcode_angle(
            mix_wav, out / f"p2_{cam}.mp4", sid=P2_SIDS[cam], size=(320, 180), offset_s=P2_OFFSETS[cam],
            mic_gain_db=P2_MIC["gain_db"], mic_delay_ms=P2_MIC["echo_ms"], click=True, fps=P2_FPS,
            click_times=truth.clicks, strip=_pulses(P, spk, P2_STRIP_YUV[spk]), noise_dbfs=P2_MIC["noise_dbfs"],
            seed=11 + i)
        paths[cam + "_mix"] = mix_wav
    tj = out / "p2_truth.json"
    tj.write_text(json.dumps(asdict(truth), indent=1, ensure_ascii=False), encoding="utf-8")
    return P2Fixture(str(out), str(rec), str(paths["cam_a"]), str(paths["cam_b"]), str(paths["cam_a_mix"]),
                     str(paths["cam_b_mix"]), str(tj), truth)


def _build_p2(base: Path) -> P2Fixture:
    out = base / "p2"
    out.mkdir(parents=True, exist_ok=True)
    parts = _p2_synthesise(out / "parts_p2")
    P = _place_p2(parts, _speaker_gains(parts))
    stems = _stems(P)
    peak = float(np.abs(stems["S1"] + stems["S2"]).max())
    if peak > 0.65:
        raise RuntimeError(f"P2 speech peaks at {peak:.2f}; the click detector needs ≤ 0.65")
    truth = _p2_truth(P, len(stems["S1"]) / P2_SR)
    return _render_p2(out, P, truth, stems)


# --------------------------------------------------------------------------
# the set
# --------------------------------------------------------------------------

def _load(manifest: Path) -> BrainFixtures | None:
    data = json.loads(manifest.read_text(encoding="utf-8"))
    th = data["th"]
    th_fx = THFixture(**{**{k: v for k, v in th.items() if k != "truth"}, "truth": from_dict(THTruth, th["truth"])})
    p2_fx = None
    if data.get("p2"):
        p2 = data["p2"]
        p2_fx = P2Fixture(**{**{k: v for k, v in p2.items() if k != "truth"}, "truth": from_dict(P2Truth, p2["truth"])})
    fx = BrainFixtures(data["root"], data["key"], th_fx, p2_fx, data.get("p2_skip_reason"))
    return fx if all(Path(p).exists() for p in fx.files) else None


def build_brain_fixtures(root: Path | None = None, *, force: bool = False) -> BrainFixtures:
    """Both fixtures under `<root>/<fixture_key()>/` (default root: the bench
    cache's `brain/`), built once and read from `brain_fixtures.json` after."""
    if not piper_voice_available(EN_VOICE):
        raise FileNotFoundError(f"Piper voice {EN_VOICE} is not cached; the fixtures never download one")
    base = (root or default_root()) / fixture_key()
    manifest = base / "brain_fixtures.json"
    if manifest.exists() and not force:
        cached = _load(manifest)
        if cached is not None:
            return cached
    base.mkdir(parents=True, exist_ok=True)
    started = time.time()
    th = _build_th(base)
    p2, why = None, None
    if say_available(HOST_VOICE):
        p2 = _build_p2(base)
    else:
        why = f"macOS `say -v {HOST_VOICE}` is unavailable; the two-camera podcast needs a second voice"
    fx = BrainFixtures(str(base), fixture_key(), th, p2, why)
    manifest.write_text(json.dumps(asdict(fx), indent=1, ensure_ascii=False), encoding="utf-8")
    (base / "build_seconds.txt").write_text(f"{time.time() - started:.1f}\n", encoding="utf-8")
    return fx


if __name__ == "__main__":
    fx = build_brain_fixtures()
    print(json.dumps({"root": fx.root, "key": fx.key, "files": fx.files, "p2_skip_reason": fx.p2_skip_reason}, indent=1))
