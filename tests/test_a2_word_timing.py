"""QA-011: whisper.cpp word timings are real, not spread evenly per segment.

Speech is synthesised with macOS `say` into a known layout — lead-in silence,
three sentences, a filler, and 2.2 s pauses — and transcribed through the real
whisper-cli backend. Every assertion is measured against the voiced audio of
that file, so captions (which are built from these word times) are checked to
land on speech rather than on the pause before it.

Skips when `say`, whisper-cli or a ggml model is not installed (CI).
"""
from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from video_ai_editor.ingest import transcribe as T

SR = 16000
PAUSE = 2.2
SENTENCES = [
    "The first thing is the size. It fits in a jacket pocket.",
    "The second thing is the screen. It flips out to the side.",
    "Um, the third thing is the battery, which lasts all day.",
]


def _model() -> str | None:
    for name in ("small", "base", "tiny"):
        if T._whisper_cpp_model_path(name).exists():
            return name
    return None


pytestmark = pytest.mark.skipif(
    shutil.which("say") is None or not T._whisper_cpp_available() or _model() is None,
    reason="needs macOS `say`, whisper-cli and a ggml model")


def _say(text: str, dst: Path) -> np.ndarray:
    aiff = dst.with_suffix(".aiff")
    subprocess.run(["say", "-v", "Samantha", "-o", str(aiff), text], check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-i", str(aiff), "-ac", "1", "-ar", str(SR),
                    "-c:a", "pcm_s16le", str(dst)], check=True, capture_output=True)
    with wave.open(str(dst), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768


def _voiced_span(x: np.ndarray) -> tuple[float, float]:
    n = int(0.01 * SR)
    rms = np.sqrt(np.mean(np.square(x[: len(x) // n * n].reshape(-1, n)), axis=1))
    idx = np.nonzero(20 * np.log10(rms + 1e-9) > -45)[0]
    return idx[0] * 0.01, (idx[-1] + 1) * 0.01


@pytest.fixture(scope="module")
def narration(tmp_path_factory):
    d = tmp_path_factory.mktemp("say")
    parts, speech = [np.zeros(int(1.0 * SR), np.float32)], []
    t = 1.0
    for i, text in enumerate(SENTENCES):
        x = _say(text, d / f"s{i}.wav")
        v0, v1 = _voiced_span(x)
        speech.append((t + v0, t + v1))
        parts.append(x)
        t += len(x) / SR
        parts.append(np.zeros(int(PAUSE * SR), np.float32))
        t += PAUSE
    audio = np.concatenate(parts)
    wav = d / "narration.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    tx = T.transcribe(wav, model_size=_model(), backend="whisper_cpp", language="en")
    return tx, speech


def test_words_after_a_pause_start_on_the_speech_not_in_the_silence(narration):
    tx, speech = narration
    words = [w for s in tx.segments for w in s.words]
    assert words, "no words transcribed"
    for (on, _off), label in zip(speech[1:], ("second", "third")):
        first = min((w for w in words if w.start >= on - 1.0), key=lambda w: w.start)
        assert abs(first.start - on) < 0.2, (label, first, on)


def test_no_word_sits_in_a_pause(narration):
    tx, speech = narration
    pauses = [(speech[i][1], speech[i + 1][0]) for i in range(len(speech) - 1)]
    for s in tx.segments:
        for w in s.words:
            for p0, p1 in pauses:
                inside = max(0.0, min(w.end, p1) - max(w.start, p0))
                assert inside < 0.2, (w, (p0, p1))


def test_segments_start_on_their_first_word(narration):
    """A caption cue built from a segment must not come up before the voice."""
    tx, speech = narration
    for s in tx.segments:
        if s.words:
            assert s.start == pytest.approx(s.words[0].start, abs=1e-6)
    onsets = [on for on, _ in speech]
    for s in tx.segments:
        # every segment start is at (or just before) voiced audio, never deep in a pause
        assert any(-0.25 <= s.start - on for on in onsets)
        assert not any(p0 + 0.2 < s.start < p1 - 0.2
                       for p0, p1 in [(speech[i][1], speech[i + 1][0]) for i in range(len(speech) - 1)])


def test_word_durations_are_measured_not_uniform(narration):
    tx, _ = narration
    for s in tx.segments:
        if len(s.words) >= 4:
            durs = np.array([w.end - w.start for w in s.words])
            assert durs.std() > 0.02, [(w.word, w.start, w.end) for w in s.words]
