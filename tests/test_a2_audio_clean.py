"""QA-028: noise reduction must lower the noise floor without costing the
dialogue level or the stereo image, and the platform recipe must not apply it
unasked.

The DSP tests run `ai.denoise.denoise_clip` on real mp4s (numpy-synthesised
speech-like bursts over pink-ish noise, muxed with ffmpeg) and measure the
decoded output.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

sf = pytest.importorskip("soundfile")
pytest.importorskip("noisereduce")

SR = 48000


def _bursts(f0: float, seconds: float, on: float, off: float, phase: float = 0.0) -> np.ndarray:
    """Voiced-speech stand-in: a harmonic tone (f0 + 5 harmonics) gated on/off
    like words and pauses."""
    t = np.arange(int(seconds * SR)) / SR
    sig = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 7))
    period = on + off
    gate = (((t + phase) % period) < on).astype(np.float64)
    # 10 ms ramps so the gating itself is not a click train
    ramp = int(0.01 * SR)
    kernel = np.ones(ramp) / ramp
    gate = np.convolve(gate, kernel, mode="same")
    return 0.25 * sig / np.max(np.abs(sig)) * gate, gate


def _noise(seconds: float, level: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    white = rng.standard_normal(int(seconds * SR))
    # gentle 1/f tilt: cumulative leaky sum, then remove DC
    pink = np.zeros_like(white)
    acc = 0.0
    for i, w in enumerate(white):
        acc = 0.97 * acc + w
        pink[i] = acc
    pink -= pink.mean()
    return level * pink / np.sqrt(np.mean(pink ** 2))


def _mp4(path: Path, stereo: np.ndarray) -> Path:
    wav = path.with_suffix(".wav")
    sf.write(str(wav), stereo.astype(np.float32), SR, subtype="FLOAT")
    dur = stereo.shape[0] / SR
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=gray:s=160x120:r=25:d={dur}",
                    "-i", str(wav), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-b:a", "320k", "-shortest", str(path)], check=True, capture_output=True)
    return path


def _decode(path: Path, tmp: Path) -> np.ndarray:
    wav = tmp / f"dec_{path.stem}.wav"
    subprocess.run(["ffmpeg", "-y", "-i", str(path), "-vn", "-c:a", "pcm_f32le", str(wav)],
                   check=True, capture_output=True)
    data, sr = sf.read(str(wav), always_2d=True)
    assert sr == SR
    return data


def _db(x: np.ndarray) -> float:
    return 20 * np.log10(np.sqrt(np.mean(np.square(x))) + 1e-12)


def _band_db(x: np.ndarray, f: float) -> float:
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freqs = np.fft.rfftfreq(len(x), 1 / SR)
    return 20 * np.log10(spec[(freqs > f - 4) & (freqs < f + 4)].max() + 1e-12)


def test_denoise_keeps_speech_level_and_stereo_and_lowers_the_floor(tmp_path):
    from video_ai_editor.ai.denoise import denoise_clip
    secs = 8.0
    left, gate_l = _bursts(180.0, secs, on=0.7, off=0.35)
    right, gate_r = _bursts(260.0, secs, on=0.7, off=0.35, phase=0.5)
    stereo = np.stack([left + _noise(secs, 0.008, 1), right + _noise(secs, 0.008, 2)], axis=1)
    src = _mp4(tmp_path / "noisy.mp4", stereo)

    out = denoise_clip(src, tmp_path / "cache", strength=0.85)
    a, b = _decode(src, tmp_path), _decode(out, tmp_path)
    assert b.shape[1] == 2, "denoise folded stereo to mono"
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    for ch, gate in ((0, gate_l), (1, gate_r)):
        g = gate[:n]
        voiced, pause = g > 0.99, g < 0.01
        assert abs(_db(b[voiced, ch]) - _db(a[voiced, ch])) < 1.0, \
            f"ch{ch} speech level moved {_db(a[voiced, ch]):.1f} -> {_db(b[voiced, ch]):.1f} dB"
        assert _db(b[pause, ch]) < _db(a[pause, ch]) - 6.0, \
            f"ch{ch} noise floor {_db(a[pause, ch]):.1f} -> {_db(b[pause, ch]):.1f} dB"
    # Each channel keeps ITS voice: the left's 180 Hz does not appear on the
    # right (a mono fold puts both voices at equal level in both channels).
    for ch, own, other in ((0, 180.0, 260.0), (1, 260.0, 180.0)):
        assert _band_db(b[:, ch], own) - _band_db(b[:, ch], other) > 25.0


def test_denoise_leaves_a_signal_with_no_noise_floor_alone(tmp_path):
    """A steady L=440 / R=1000 Hz pair has no quiet frames to learn noise
    from; gating it against itself was the -22 dB / mono failure."""
    from video_ai_editor.ai.denoise import denoise_clip
    t = np.arange(int(4.0 * SR)) / SR
    stereo = np.stack([0.2 * np.sin(2 * np.pi * 440 * t), 0.2 * np.sin(2 * np.pi * 1000 * t)], axis=1)
    src = _mp4(tmp_path / "lr.mp4", stereo)
    out = denoise_clip(src, tmp_path / "cache", strength=0.85)
    a, b = _decode(src, tmp_path), _decode(out, tmp_path)
    assert b.shape[1] == 2
    for ch in (0, 1):
        assert abs(_db(b[:, ch]) - _db(a[:, ch])) < 0.5
    assert _band_db(b[:, 0], 440) - _band_db(b[:, 0], 1000) > 40
    assert _band_db(b[:, 1], 1000) - _band_db(b[:, 1], 440) > 40


def test_platform_recipes_do_not_denoise_unless_asked():
    from video_ai_editor.agent.prompt import planner as P
    from video_ai_editor.agent.prompt.facts import TimelineFacts
    facts = TimelineFacts.minimal()
    for prompt in ("turn this into a tiktok", "make it good for youtube", "make it pop"):
        tools = [s.tool for s in P.plan(prompt, facts).steps]
        assert "noise_reduce" not in tools, (prompt, tools)
    for prompt in ("remove the background noise", "make it pop and remove the background noise",
                   "clean up the audio and export for tiktok"):
        tools = [s.tool for s in P.plan(prompt, facts).steps]
        assert "noise_reduce" in tools, (prompt, tools)
    # "make it pop" still normalises loudness without a platform preset.
    assert "set_loudness_target" in [s.tool for s in P.plan("make it pop", facts).steps]
