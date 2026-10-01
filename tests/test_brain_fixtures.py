"""EB1-A: the Editor Brain fixtures carry the truth they claim (EB1_BRIEF.md,
"Lane EB1-A — Fixtures and truth").

Every assertion here is a MEASUREMENT of the built media (decoded PCM,
ffprobe'd frames, whisper tokens, click times) against the truth JSON the
builder wrote — never a read-back of the builder's own bookkeeping. The
fixtures are Piper + macOS `say` + numpy + ffmpeg, egress-guarded, and skip
cleanly when a voice is missing (CI has no Piper cache).

Ordering of the module: the pins on the EXISTING benchmark first (they need
no build), then the talking-head fixture, then the two-camera podcast.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
from pathlib import Path

import numpy as np
import pytest

from benchmark import media as MEDIA
from benchmark import narration as NARR
from benchmark.harness import EgressGuard
from timing_fixtures import audio_samples, flash_onsets

pytestmark = pytest.mark.skipif(
    not NARR.piper_voice_available(NARR.EN_VOICE) if hasattr(NARR, "piper_voice_available") else False,
    reason="Piper en_US-amy-medium is not cached (the fixtures never download a voice)")

# --- pins on the EXISTING benchmark (recorded 2026-09-29 at 8271261) ----------
#: `media_key()` of the shipped bench media set — the narration, the scene
#: videos and the bed live under CACHE_ROOT/<this>.
BENCH_MEDIA_KEY = "8515fa4411c9"
#: sha256 of `narration_en.json` / `narration_en.wav` in that set.
BENCH_NARRATION_JSON_SHA = "9ebd43700493359f6d87eb738f2739565a233eab7c3348c600f36ac513aa70c5"
BENCH_NARRATION_WAV_SHA = "1b8013f4e4f8afe56a6e0d22bda8fa9f255a6b11da7fd54c6a8e230a98a5242a"
#: sha256(json.dumps(SCRIPT_EN))[:16] — the script is part of the media key.
BENCH_SCRIPT_SHA16 = "b6aef7ccf39ce6d0"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_existing_narration_unchanged():
    """The bench narration's inputs and outputs are exactly what they were
    before this lane: same key, same script, same constants, same bytes."""
    assert callable(getattr(NARR, "synthesize_script", None)), "EB1-A adds synthesize_script"
    assert MEDIA.media_key() == BENCH_MEDIA_KEY
    assert hashlib.sha256(json.dumps(NARR.SCRIPT_EN).encode()).hexdigest()[:16] == BENCH_SCRIPT_SHA16
    assert (NARR.GAP_S, NARR.PAUSE_S, NARR.PART_PAD_S, NARR.VOICED_THRESHOLD) == (0.12, 2.0, 0.04, 0.02)
    assert NARR.EN_VOICE == "en_US-amy-medium" and MEDIA.MEDIA_VERSION == 5
    assert len(NARR.PLANTED_FILLERS) == 9 and set(NARR.PLANTED_FILLERS) == {"um", "umm"}
    root = MEDIA.CACHE_ROOT / BENCH_MEDIA_KEY
    if not (root / "narration_en.json").exists():
        pytest.skip("the bench media set is not built on this machine; the key and script pins ran")
    assert _sha(root / "narration_en.json") == BENCH_NARRATION_JSON_SHA
    assert _sha(root / "narration_en.wav") == BENCH_NARRATION_WAV_SHA
    # The refactor keeps `synthesize_narration` a thin caller of the new
    # `synthesize_script`: the cached truth still parses and reads the same.
    nar = NARR.load_narration(root, lang="en")
    assert nar is not None and len(nar.fillers) == 9 and len(nar.pauses) == 7 and len(nar.sentences) == 12
    assert [u.text for u in nar.utterances] == [t for _k, t in NARR.SCRIPT_EN]


def _legacy_synthesize_narration_en(out_dir: Path) -> "NARR.Narration":
    """The English branch of `synthesize_narration` exactly as it was at
    8271261, before `synthesize_script` existed (copied verbatim, only the
    Hindi branch dropped) — the reference the refactor is compared to."""
    script = NARR.SCRIPT_EN
    parts_dir = out_dir / "parts_en"
    voice = NARR._piper_voice(NARR.EN_VOICE)
    synth = lambda text, dst: NARR._synth_piper(voice, text, dst)  # noqa: E731
    sr = int(voice.config.sample_rate)
    chunks: list[np.ndarray] = []
    utterances = []
    cursor = 0.0
    for i, (kind, text) in enumerate(script):
        if kind in ("pause", "gap"):
            seconds = NARR.PAUSE_S if kind == "pause" else NARR.GAP_S
            n = int(round(seconds * sr))
            chunks.append(np.zeros(n, dtype=np.float32))
            utterances.append(NARR.Utterance(i, kind, "", cursor, cursor + n / sr, None, None))
            cursor += n / sr
            continue
        pieces = NARR._sentences(text) if kind == "speech" else [NARR.FILLER_SPOKEN[text]]
        joined: list[np.ndarray] = []
        for j, piece in enumerate(pieces):
            raw, part_sr = synth(piece, parts_dir / f"{i:03d}_{j}.wav")
            if part_sr != sr:
                raise RuntimeError(f"voice sample rate changed mid-script: {part_sr} != {sr}")
            if joined:
                joined.append(np.zeros(int(round(NARR.GAP_S * sr)), dtype=np.float32))
            joined.append(NARR._trim_to_voiced(raw, sr))
        pcm = np.concatenate(joined)
        bounds = NARR._voiced_bounds(pcm, sr)
        start = cursor
        end = cursor + len(pcm) / sr
        utterances.append(NARR.Utterance(
            i, kind, text, start, end,
            None if bounds is None else start + bounds[0],
            None if bounds is None else start + bounds[1]))
        chunks.append(pcm)
        cursor = end
    pcm_all = np.concatenate(chunks)
    wav = out_dir / "narration_en.wav"
    NARR._write_wav(wav, pcm_all, sr)
    narration = NARR.Narration(lang="en", voice=NARR.EN_VOICE, wav=str(wav), sample_rate=sr,
                               duration=len(pcm_all) / sr, utterances=tuple(utterances))
    (out_dir / "narration_en.json").write_text(narration.to_json(), encoding="utf-8")
    return narration


def test_synthesize_narration_refactor_is_byte_equal_to_the_original(tmp_path: Path, monkeypatch):
    """The refactor (`synthesize_narration` -> `synthesize_script`) runs the
    SAME code path for the bench's own script: with a deterministic stand-in
    voice, the new function and the verbatim pre-EB1 function write the same
    narration_en.wav and narration_en.json bytes. (The cached-bytes pin above
    cannot see a drift in the code; Piper itself is not byte-stable.)"""
    from types import SimpleNamespace

    def fake_piper_voice(_name):
        return SimpleNamespace(config=SimpleNamespace(sample_rate=22050))

    def fake_synth(_voice, text, dst):
        sr = 22050
        seed = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
        n = int(sr * (0.25 + 0.05 * len(text.split())))
        t = np.arange(n) / sr
        env = np.minimum(1.0, np.minimum(t, t[-1] - t) * 20.0)
        x = 0.4 * env * np.sin(2 * np.pi * (140 + seed % 90) * t)
        pcm = np.concatenate([np.zeros(int(0.06 * sr)), x, np.zeros(int(0.05 * sr))]).astype(np.float32)
        dst.parent.mkdir(parents=True, exist_ok=True)
        NARR._write_wav(dst, pcm, sr)
        return pcm, sr

    monkeypatch.setattr(NARR, "_piper_voice", fake_piper_voice)
    monkeypatch.setattr(NARR, "_synth_piper", fake_synth)
    new_dir, old_dir = tmp_path / "new", tmp_path / "old"
    new_dir.mkdir()
    old_dir.mkdir()
    new = NARR.synthesize_narration(new_dir, lang="en")
    old = _legacy_synthesize_narration_en(old_dir)
    assert len(new.utterances) == len(NARR.SCRIPT_EN) == len(old.utterances)
    assert _sha(new_dir / "narration_en.wav") == _sha(old_dir / "narration_en.wav")
    a = json.loads((new_dir / "narration_en.json").read_text())
    b = json.loads((old_dir / "narration_en.json").read_text())
    a["wav"] = b["wav"] = ""
    assert a == b


def _legacy_synthesize_narration_hi(out_dir: Path, backend: str) -> "NARR.Narration":
    """The Hindi branch of `synthesize_narration` exactly as it was at HEAD (copied verbatim, only the
    English branch and the no-voice raise dropped): the reference the refactor is compared to (SC-10)."""
    script = NARR.SCRIPT_HI
    parts_dir = out_dir / "parts_hi"
    if backend == "piper":
        voice = NARR._piper_voice(NARR.HI_VOICE)
        voice_name = NARR.HI_VOICE
        synth = lambda text, dst: NARR._synth_piper(voice, text, dst)  # noqa: E731
        sr = int(voice.config.sample_rate)
    else:
        voice_name = f"say:{NARR.HI_SAY_VOICE}"
        sr = 22050
        synth = lambda text, dst: NARR._synth_say(text, dst, voice=NARR.HI_SAY_VOICE, sr=sr)  # noqa: E731
    chunks: list[np.ndarray] = []
    utterances = []
    cursor = 0.0
    for i, (kind, text) in enumerate(script):
        if kind in ("pause", "gap"):
            seconds = NARR.PAUSE_S if kind == "pause" else NARR.GAP_S
            n = int(round(seconds * sr))
            chunks.append(np.zeros(n, dtype=np.float32))
            utterances.append(NARR.Utterance(i, kind, "", cursor, cursor + n / sr, None, None))
            cursor += n / sr
            continue
        pieces = NARR._sentences(text) if kind == "speech" else [NARR.FILLER_SPOKEN[text]]
        joined: list[np.ndarray] = []
        for j, piece in enumerate(pieces):
            raw, part_sr = synth(piece, parts_dir / f"{i:03d}_{j}.wav")
            if part_sr != sr:
                raise RuntimeError(f"voice sample rate changed mid-script: {part_sr} != {sr}")
            if joined:
                joined.append(np.zeros(int(round(NARR.GAP_S * sr)), dtype=np.float32))
            joined.append(NARR._trim_to_voiced(raw, sr))
        pcm = np.concatenate(joined)
        bounds = NARR._voiced_bounds(pcm, sr)
        start = cursor
        end = cursor + len(pcm) / sr
        utterances.append(NARR.Utterance(
            i, kind, text, start, end,
            None if bounds is None else start + bounds[0],
            None if bounds is None else start + bounds[1]))
        chunks.append(pcm)
        cursor = end
    pcm_all = np.concatenate(chunks)
    wav = out_dir / "narration_hi.wav"
    NARR._write_wav(wav, pcm_all, sr)
    narration = NARR.Narration(lang="hi", voice=voice_name, wav=str(wav), sample_rate=sr,
                               duration=len(pcm_all) / sr, utterances=tuple(utterances))
    (out_dir / "narration_hi.json").write_text(narration.to_json(), encoding="utf-8")
    return narration


@pytest.mark.parametrize("backend", ["say", "piper"])
def test_hindi_narration_refactor_is_byte_equal_to_the_original(tmp_path: Path, monkeypatch, backend):
    """SC-10: the Hindi branch (Piper hi_IN or macOS `say`) runs through `synthesize_script` now; with a
    deterministic stand-in voice it writes the same narration_hi.wav / .json bytes as the verbatim HEAD code."""
    from types import SimpleNamespace

    def fake(text, dst):
        sr = 22050
        seed = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
        n = int(sr * (0.25 + 0.03 * len(text)))
        t = np.arange(n) / sr
        env = np.minimum(1.0, np.minimum(t, t[-1] - t) * 20.0)
        x = 0.4 * env * np.sin(2 * np.pi * (120 + seed % 110) * t)
        pcm = np.concatenate([np.zeros(int(0.06 * sr)), x, np.zeros(int(0.05 * sr))]).astype(np.float32)
        dst.parent.mkdir(parents=True, exist_ok=True)
        NARR._write_wav(dst, pcm, sr)
        return pcm, sr

    monkeypatch.setattr(NARR, "hindi_backend", lambda: backend)
    monkeypatch.setattr(NARR, "_piper_voice", lambda _n: SimpleNamespace(config=SimpleNamespace(sample_rate=22050)))
    monkeypatch.setattr(NARR, "_synth_piper", lambda _v, text, dst: fake(text, dst))
    monkeypatch.setattr(NARR, "_synth_say", lambda text, dst, voice, sr: fake(text, dst))
    new_dir, old_dir = tmp_path / "new", tmp_path / "old"
    new_dir.mkdir()
    old_dir.mkdir()
    new = NARR.synthesize_narration(new_dir, lang="hi")
    old = _legacy_synthesize_narration_hi(old_dir, backend)
    assert len(new.utterances) == len(NARR.SCRIPT_HI) == len(old.utterances)
    assert new.voice == old.voice
    assert _sha(new_dir / "narration_hi.wav") == _sha(old_dir / "narration_hi.wav")
    a = json.loads((new_dir / "narration_hi.json").read_text())
    b = json.loads((old_dir / "narration_hi.json").read_text())
    a["wav"] = b["wav"] = ""
    assert a == b


# --- the fixtures -------------------------------------------------------------

@pytest.fixture(scope="module")
def fx():
    from brain_fixtures import build_brain_fixtures
    with EgressGuard():
        return build_brain_fixtures()


def _rms_db(pcm: np.ndarray) -> float:
    ms = float(np.mean(np.square(pcm))) if pcm.size else 0.0
    return 10 * math.log10(ms) if ms > 1e-24 else -240.0


def _pcm(path: Path, rate: int = 48000) -> np.ndarray:
    return np.asarray(audio_samples(path, rate=rate), dtype=np.float32)


def _span(pcm: np.ndarray, t0: float, t1: float, rate: int = 48000) -> np.ndarray:
    return pcm[int(round(t0 * rate)):int(round(t1 * rate))]


# --- talking head ---------------------------------------------------------------

def test_th_truth_shape(fx):
    th = fx.th
    t = th.truth
    assert len(t.sentences) == 14 and len(t.fillers_lexical) == 6 and len(t.fillers_acoustic) == 3
    assert len([p for p in t.pauses if p.kind == "planted"]) == 5
    assert all(abs((p.t1 - p.t0) - 1.5) < 1e-6 for p in t.pauses if p.kind == "planted")
    assert sum(1 for s in t.sentences if s.is_question) == 1
    assert t.sentences[-1].text.endswith("thanks for watching.")
    assert t.retake.dup != t.retake.of
    by_id = {s.id: s for s in t.sentences}
    assert by_id[t.retake.dup].text == by_id[t.retake.of].text
    assert by_id[t.quotable].features["strong_number"] and by_id[t.quotable].features["superlative"]
    assert by_id[t.throwaway].features["weak_number"] and not by_id[t.throwaway].features["strong_number"]
    assert t.emphasis.clause_start is not None
    e = by_id[t.emphasis.sent]
    assert e.t0 < t.emphasis.clause_start < e.t1 and "," in e.text
    assert Path(th.video_16x9).exists() and Path(th.video_9x16).exists()
    assert 60 <= t.duration <= 95
    # utterances tile the file with no holes
    for a, b in zip(t.utts, t.utts[1:]):
        assert abs(a.end - b.start) < 1e-6, (a, b)


def test_th_emphasis_sentence_is_6db_louder_and_15pct_slower(fx):
    """Measured from the wav: the emphasised sentence is +6 dB over its plain
    twin (synthesised alongside, not placed in the narration) and 15 % longer,
    and stands out from every other sentence."""
    t = fx.th.truth
    pcm = _pcm(Path(fx.th.speech_wav))
    by_id = {s.id: s for s in t.sentences}
    e = by_id[t.emphasis.sent]
    emph_db = _rms_db(_span(pcm, e.t0, e.t1))
    others = [_rms_db(_span(pcm, s.t0, s.t1)) for s in t.sentences if s.id != e.id]
    assert 5.5 <= emph_db - t.emphasis.plain_rms_db <= 6.5, (emph_db, t.emphasis.plain_rms_db)
    assert emph_db - max(others) >= 3.5, (emph_db, max(others))
    ratio = (e.t1 - e.t0) / t.emphasis.plain_duration
    assert 1.10 <= ratio <= 1.20, ratio
    # spec §3.4 `stretch` = speaker median wpm / scene wpm ≥ 1.1 for this sentence
    wpm = lambda s: 60.0 * len(s.text.split()) / (s.t1 - s.t0)  # noqa: E731
    median_wpm = float(np.median([wpm(s) for s in t.sentences if s.id != e.id]))
    assert median_wpm / wpm(e) >= 1.1, (median_wpm, wpm(e))


def _whisper_small_backends() -> list[str]:
    """Every whisper `small` backend usable OFFLINE on this machine:
    whisper.cpp when a ggml `small` is cached (the upload path's backend),
    faster-whisper from the offline HF cache. Nothing here downloads."""
    from video_ai_editor.ingest import transcribe as T
    out = []
    if T._whisper_cpp_available() and T._whisper_cpp_model_path("small").exists():
        out.append("whisper_cpp")
    hub = Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface") / "hub"
    if (hub / "models--Systran--faster-whisper-small").exists():
        out.append("faster_whisper")
    return out


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


@pytest.mark.parametrize("backend", ["whisper_cpp", "faster_whisper"])
@pytest.mark.parametrize("media", ["audio_wav", "video_16x9"])
def test_th_truth_has_three_acoustic_fillers_whisper_drops(fx, backend, media):
    """whisper-small over the TH audio — the wav AND the 16:9 render's own
    soundtrack, which is what the app ingests — yields no token over any of
    the three `uh` islands (nor any word covering half of one: the §3.4
    detector's overlap rule), while the lexical `um`s are heard. whisper.cpp,
    the upload path's backend, leaves not even a partial overlap. So the
    acoustic truth is by TIME and only the detector can find it."""
    if backend not in _whisper_small_backends():
        pytest.skip(f"no offline whisper `small` for {backend}; nothing is downloaded")
    from video_ai_editor.ingest import transcribe as T
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    with EgressGuard():
        tr = T.transcribe(Path(getattr(fx.th, media)), language="en", model_size="small", backend=backend)
    words = [(w.start, w.end, w.word.strip().lower().strip(".,!?")) for w in tr.words]
    t = fx.th.truth
    utts = {u.id: u for u in t.utts}
    for uid in t.fillers_acoustic:
        u = utts[uid]
        v0, v1 = u.voiced_start, u.voiced_end
        assert 0.2 <= v1 - v0 <= 0.4, (uid, v1 - v0)
        hits = [w for w in words if _overlap(w[0], w[1], v0, v1) >= 0.5 * (v1 - v0)]
        assert not hits, f"whisper heard {hits} over acoustic filler {uid} {v0:.2f}-{v1:.2f}"
        assert not [w for w in words if w[2] in ("uh", "uhh", "uhm", "ah", "huh") and _overlap(w[0], w[1], v0 - 0.3, v1 + 0.3) > 0]
        if backend == "whisper_cpp":
            touch = [w for w in words if _overlap(w[0], w[1], v0, v1) > 0]
            assert not touch, f"whisper.cpp put {touch} over acoustic filler {uid} {v0:.2f}-{v1:.2f}"
    heard = 0
    for uid in t.fillers_lexical:
        u = utts[uid]
        v0, v1 = u.voiced_start, u.voiced_end
        if any(w[2] in ("um", "umm", "uhm", "hmm") and _overlap(w[0], w[1], v0 - 0.3, v1 + 0.3) > 0 for w in words):
            heard += 1
    assert heard >= 5, f"whisper heard only {heard}/6 lexical fillers: {words}"


def test_th_clicks_meet_flashes_on_both_renders(fx):
    """Every planted pause boundary carries a click and a one-frame flash at
    the same instant in both the 16:9 and the 9:16 render (≤ 5 ms)."""
    from brain_fixtures import av_offsets_ms_strict, click_times_strict
    t = fx.th.truth
    for video in (fx.th.video_16x9, fx.th.video_9x16):
        clicks = click_times_strict(Path(video))
        assert len(clicks) == len(t.clicks) == 10, (video, clicks)
        assert max(abs(a - b) for a, b in zip(clicks, t.clicks)) <= 0.002
        offs = av_offsets_ms_strict(Path(video))
        assert len(offs) == 10 and max(abs(o) for o in offs) <= 5.0, offs
        flashes = flash_onsets(Path(video))
        assert len(flashes) == 10


def test_th_bar_code_frames_decode(fx):
    """The picture is a readable bar code: frame n of the 16:9 render carries
    `code_of(1, n)` (and the 9:16 render `code_of(2, n)`), so a consumer can
    prove which source frame a timeline span shows."""
    from frame_map_golden_lib import code_of, measure
    for sid, video in ((1, fx.th.video_16x9), (2, fx.th.video_9x16)):
        m = measure(Path(video))
        n = len(m["top"])
        assert n >= 60 * fx.th.truth.fps
        for i in (0, 1, 7, n // 2, n - 2, n - 1):
            assert m["top"][i] == m["bot"][i] == code_of(sid, i), (sid, i, m["top"][i])


# --- two-camera podcast ---------------------------------------------------------

@pytest.fixture(scope="module")
def p2(fx):
    if fx.p2 is None:
        pytest.skip(fx.p2_skip_reason or "P2 fixture unavailable")
    return fx.p2


def test_p2_truth_shape(p2):
    t = p2.truth
    assert len(t.turns) == 40
    assert len(t.backchannels) == 4 and all(t.turn(b).t1 - t.turn(b).t0 < 0.6 for b in t.backchannels)
    fill = [u for u in t.utts if u.id in t.fillers_lexical]
    assert sorted(u.speaker for u in fill) == ["S1", "S1", "S2", "S2"]
    assert t.false_start.kept_sent in {s.id for s in t.sentences}
    # the overlapping turn's start is snapped to the 20 fps grid (± half a frame)
    assert abs((t.overlap.t1 - t.overlap.t0) - 0.8) <= 0.026
    assert len(t.qa_pairs) >= 10
    pauses = {p.kind: [] for p in t.pauses}
    for p in t.pauses:
        pauses[p.kind].append(round(p.t1 - p.t0, 2))
    assert 2.4 in pauses["in_turn"] and pauses["turn_boundary"].count(1.8) == 2
    prot = [p for p in t.pauses if p.protection == "emotion"]
    assert len(prot) == 1 and abs((prot[0].t1 - prot[0].t0) - 1.0) < 1e-6
    assert t.offsets == {"recorder": 0.0, "cam_a": 0.35, "cam_b": -0.2}
    assert t.own_mic == {"cam_a": "S1", "cam_b": "S2"}
    assert 120 <= t.duration <= 204 and t.fps == 20
    assert all(s.speaker in ("S1", "S2") for s in t.sentences)
    assert [tr.expected_angle for tr in t.turns if tr.kind == "backchannel"] == [None] * 4


def test_p2_offsets_and_clicks(p2):
    """The planted file offsets are recoverable from the click tracks, and on
    each raw angle the clicks meet the flashes within 5 ms."""
    from brain_fixtures import av_offsets_ms_strict, click_times_strict
    t = p2.truth
    rec = click_times_strict(Path(p2.recorder_wav))
    assert len(rec) == len(t.clicks) == 40
    assert max(abs(a - b) for a, b in zip(rec, t.clicks)) <= 0.002
    for name, path in (("cam_a", p2.cam_a), ("cam_b", p2.cam_b)):
        cl = click_times_strict(Path(path))
        assert len(cl) == 40, (name, len(cl))
        deltas = [c - r for c, r in zip(cl, rec)]
        assert max(abs(d - t.offsets[name]) for d in deltas) <= 0.002, (name, deltas[:5])
        offs = av_offsets_ms_strict(Path(path))
        assert len(offs) == 40 and max(abs(o) for o in offs) <= 5.0, (name, offs)


def test_p2_own_mic_gain(p2):
    """Each camera's microphone hears its own speaker ≥ 5 dB louder than the
    other one, measured over the truth turns in that file's own clock."""
    t = p2.truth
    for name, path in (("cam_a", p2.cam_a), ("cam_b", p2.cam_b)):
        pcm = _pcm(Path(path))
        off = t.offsets[name]
        own = t.own_mic[name]
        by_spk: dict[str, list[np.ndarray]] = {"S1": [], "S2": []}
        for tr in t.turns:
            if tr.kind == "backchannel" or (t.overlap.t0 < tr.t1 and tr.t0 < t.overlap.t1):
                continue
            by_spk[tr.speaker].append(_span(pcm, tr.t0 + off + 0.05, tr.t1 + off - 0.05))
        own_db = _rms_db(np.concatenate(by_spk[own]))
        other = "S2" if own == "S1" else "S1"
        other_db = _rms_db(np.concatenate(by_spk[other]))
        assert own_db - other_db >= 5.0, (name, own_db, other_db)
        assert own_db < -14.0, (name, own_db)          # camera mics are 12 dB under the recorder


def test_p2_recorder_is_the_clean_reference(p2):
    t = p2.truth
    rec = _pcm(Path(p2.recorder_wav))
    cam = _pcm(Path(p2.cam_a))
    tr = next(x for x in t.turns if x.speaker == "S1" and x.kind == "turn")
    rec_db = _rms_db(_span(rec, tr.t0 + 0.05, tr.t1 - 0.05))
    cam_db = _rms_db(_span(cam, tr.t0 + 0.35 + 0.05, tr.t1 + 0.35 - 0.05))
    assert 10.0 <= rec_db - cam_db <= 14.0, (rec_db, cam_db)
    gap = next(p for p in t.pauses if p.kind == "turn_boundary")
    floor = _rms_db(_span(cam, gap.t0 + 0.35 + 0.3, gap.t1 + 0.35 - 0.3))
    assert -60 <= floor <= -50, floor                  # room noise −55 dBFS
    assert _rms_db(_span(rec, gap.t0 + 0.3, gap.t1 - 0.3)) < -90  # digital silence on the recorder


def test_p2_bar_codes_and_pulses(p2):
    from frame_map_golden_lib import code_of, measure
    t = p2.truth
    for sid, video in ((1, p2.cam_a), (2, p2.cam_b)):
        m = measure(Path(video))
        n = len(m["top"])
        assert 2400 <= n < 4096
        for i in (0, 1, 5, n // 3, n - 1):
            assert m["top"][i] == m["bot"][i] == code_of(sid, i), (sid, i)
    assert t.sids == {"cam_a": 1, "cam_b": 2}


# --- determinism ----------------------------------------------------------------

def _tree_digest(root: Path) -> dict[str, str]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name != "build_seconds.txt":   # wall-clock, not content
            data = p.read_bytes()
            if p.suffix == ".json":
                data = data.replace(str(root).encode(), b"<root>")
            out[str(p.relative_to(root))] = hashlib.sha256(data).hexdigest()
    return out


def test_builds_are_byte_equal_twice(fx, tmp_path: Path):
    """A second cold build in another root produces the same bytes for every
    wav, mp4 and JSON (paths normalised) — Piper is run deterministically."""
    from brain_fixtures import build_brain_fixtures
    other = tmp_path / "b"
    with EgressGuard():
        again = build_brain_fixtures(root=other, force=True)
    a = _tree_digest(Path(fx.root))
    b = _tree_digest(Path(again.root))
    assert set(a) == set(b), set(a) ^ set(b)
    diff = [k for k in a if a[k] != b[k]]
    assert not diff, diff
    shutil.rmtree(other, ignore_errors=True)


def test_build_time_and_manifest(fx):
    secs = float((Path(fx.root) / "build_seconds.txt").read_text().strip())
    assert secs <= 60.0, secs
    man = json.loads((Path(fx.root) / "brain_fixtures.json").read_text())
    assert man["key"] == Path(fx.root).name and "th" in man


# --- the hand-built golden graphs -------------------------------------------------

GOLDEN_DIR = Path(__file__).parent / "goldens" / "brain" / "graphs"
GOLDEN_SRC_KEY = re.compile(r"^src_[0-9a-f]{24}$")


def _golden(name: str) -> dict:
    return json.loads((GOLDEN_DIR / name).read_text(encoding="utf-8"))


def _validate_envelope(env: dict):
    """Every part of the `{graph, layers, scenes, angles}` envelope against
    lane C's frozen models (`brain.schema`), the way `brain.store` would."""
    from video_ai_editor.brain import schema as S
    from video_ai_editor.brain.store import LAYER_MODELS
    graph = S.Graph.model_validate(env["graph"])
    layers = {k: LAYER_MODELS[k].model_validate(v) for k, v in env["layers"].items()}
    scenes = S.Scenes.model_validate(env["scenes"])
    angles = S.Angles.model_validate(env["angles"])
    ids = S.graph_ids(graph, layers, scenes=scenes, angles=angles)
    return graph, layers, scenes, angles, ids


@pytest.mark.parametrize("name", ["talking_head.json", "two_cam_podcast.json"])
def test_golden_graphs_validate_against_brain_schema(fx, name):
    """Both golden graphs validate against C's models (extra="forbid",
    monotone 4-decimal times, id references), carry `src_` + 24-hex keys that
    are the CONTENT keys of the built files, and name every sentence/scene
    the truth has."""
    if name.startswith("two_cam") and fx.p2 is None:
        pytest.skip(fx.p2_skip_reason or "P2 fixture unavailable")
    env = _golden(name)
    graph, layers, scenes, angles, ids = _validate_envelope(env)
    files = ([fx.th.video_16x9] if name.startswith("talking") else [fx.p2.recorder_wav, fx.p2.cam_a, fx.p2.cam_b])
    keys = {s.key for s in graph.sources}
    assert keys == {"src_" + hashlib.sha256(Path(f).read_bytes()).hexdigest()[:24] for f in files}
    assert all(GOLDEN_SRC_KEY.match(k) for k in keys | {m.src_key for m in angles.members} | {graph.reference})
    assert graph.reference in keys and angles.reference == graph.reference
    truth = fx.th.truth if name.startswith("talking") else fx.p2.truth
    assert {s.id for s in truth.sentences} <= ids, sorted({s.id for s in truth.sentences} - ids)[:5]
    assert len(layers["speech"].sentences) == len(truth.sentences)


@pytest.mark.parametrize("name", ["talking_head.json", "two_cam_podcast.json"])
def test_golden_graphs_are_current_with_the_fixture(fx, name):
    """The committed goldens are exactly what `gen_brain_fixture_graphs.py`
    writes for the fixture as it builds now (a stale golden fails here, so
    the fixture and the graph never drift apart)."""
    if name.startswith("two_cam") and fx.p2 is None:
        pytest.skip(fx.p2_skip_reason or "P2 fixture unavailable")
    from gen_brain_fixture_graphs import generate
    assert (GOLDEN_DIR / name).read_text(encoding="utf-8") == generate(fx)[name]
