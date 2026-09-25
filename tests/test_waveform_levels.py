"""QA-081: /waveform peaks state the true sample peak.

Pre-fix the peaks were taken from a decode at peaks_per_sec × 200 Hz, fewer
for long files, mono-averaged: a 3 kHz tone read 0.021 at 30 s and 0.0 at
200 s, an 8 kHz tone read 0.0 (7.9 kHz here: at exactly 8 kHz a 48 kHz
decode samples every 60°, so the true SAMPLE peak is 0.866 × amplitude), a left-only tone half its level, and the
bundled preset music answered 403. Each case here is a known-amplitude lavfi
tone fetched through the real endpoint.
"""
from __future__ import annotations

import statistics
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor.main import app


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    from video_ai_editor import storage as _storage, main as _main
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    _main._STORES.clear()
    return TestClient(app)


def _tone(p: Path, *, freq: int, dur: float, left_only: bool = False, amp: float = 0.5) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    right = "0" if left_only else f"{amp}*sin(2*PI*{freq}*t)"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    f"aevalsrc={amp}*sin(2*PI*{freq}*t)|{right}:s=48000:d={dur}:c=stereo",
                    "-c:a", "pcm_s16le", str(p)], check=True, capture_output=True)
    return p


def _peaks(client, sid, src: Path, pps: int = 50) -> dict:
    r = client.get(f"/api/sessions/{sid}/waveform", params={"src": str(src), "peaks_per_sec": pps})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("freq,dur", [(1000, 30.0), (3000, 30.0), (3000, 200.0), (7900, 30.0)])
def test_peaks_read_the_tone_amplitude_at_any_frequency_and_length(client, tmp_path, freq, dur):
    sid = client.post("/api/sessions").json()["id"]
    src = _tone(tmp_path / sid / "uploads" / f"t{freq}_{int(dur)}.wav", freq=freq, dur=dur)
    d = _peaks(client, sid, src)
    assert abs(len(d["peaks"]) / d["peaks_per_sec"] - dur) < 0.1, (len(d["peaks"]), d["peaks_per_sec"])
    assert statistics.median(d["peaks"]) == pytest.approx(0.5, abs=0.02), (freq, dur, d["peaks"][:5])
    assert d["duration"] == pytest.approx(dur, abs=0.05)


def test_a_left_only_tone_reads_its_full_level(client, tmp_path):
    sid = client.post("/api/sessions").json()["id"]
    src = _tone(tmp_path / sid / "uploads" / "left.wav", freq=440, dur=10.0, left_only=True)
    assert statistics.median(_peaks(client, sid, src)["peaks"]) == pytest.approx(0.5, abs=0.02)


def test_a_long_source_keeps_a_useful_density_and_true_peaks(client, tmp_path):
    """A 12-minute source used to drop to 5 peaks/s decoded at 1 kHz."""
    sid = client.post("/api/sessions").json()["id"]
    src = _tone(tmp_path / sid / "uploads" / "long.wav", freq=2500, dur=720.0, amp=0.75)
    d = _peaks(client, sid, src)
    assert d["peaks_per_sec"] >= 50
    assert max(d["peaks"]) == pytest.approx(0.75, abs=0.02)
    assert 48000 % d["peaks_per_sec"] == 0


def test_bundled_preset_music_is_drawable(client, tmp_path, monkeypatch):
    from video_ai_editor import config
    presets = tmp_path / "bundled_presets"
    bed = _tone(presets / "music" / "bed.wav", freq=220, dur=5.0)
    monkeypatch.setattr(config, "PRESETS_DIR", presets)
    sid = client.post("/api/sessions").json()["id"]
    assert statistics.median(_peaks(client, sid, bed)["peaks"]) == pytest.approx(0.5, abs=0.02)


def test_a_file_outside_everything_is_still_refused(client, tmp_path):
    sid = client.post("/api/sessions").json()["id"]
    src = _tone(tmp_path / "elsewhere" / "x.wav", freq=440, dur=2.0)
    r = client.get(f"/api/sessions/{sid}/waveform", params={"src": str(src)})
    assert r.status_code == 403
