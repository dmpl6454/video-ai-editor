"""index.json records every proxy audio chunk's PEAK (wave E gate RX, finding 3).

The client's limiter (a DynamicsCompressor) is not the export's `alimiter`:
below the ceiling both are transparent and the offline mix matches the server
sample for sample, above it they differ (|Δ| up to 0.25, 0.64 dB per 50 ms
block on over-ceiling material) — yet the plan called the range EXACT. The
plan now bounds the pre-limiter peak from each source chunk's recorded peak
(`audio.chunk_peak`, the max |x| of ffmpeg's float decode, rounded UP to
1e-6) and marks the master range APPROX where the bound tops the ceiling
(frontend/src/lib/preview/audio/limiting.ts).
"""
from __future__ import annotations

import subprocess

import numpy as np

import sys
from pathlib import Path

# The house way to reach tests/wk helpers (see test_render_audio_tail.py):
# `from tests.wk import …` only works when the repo root is on sys.path,
# which `python -m pytest` gives and CI's `uv run pytest` does not.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from wk import audio_fixture as fx  # noqa: E402

CS = 240000


def test_every_chunk_records_its_peak_upper_bound(tmp_path):
    src = tmp_path / "hot.mov"
    # 12 s = three chunks: hot (over full scale), then 0.5, then 0.25.
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    ("aevalsrc='if(lt(t,5),1.45,if(lt(t,10),0.5,0.25))*sin(2*PI*220*t)|"
                     "if(lt(t,5),1.2,if(lt(t,10),0.4,0.2))*sin(2*PI*330*t)':s=48000:d=12"),
                    "-c:a", "pcm_f32le", str(src)], check=True, capture_output=True)
    wd = tmp_path / "wd"
    wd.mkdir()
    key, idx, _info = fx.build_proxy_audio(src, wd)
    a = idx["audio"]
    master = fx.decode_master_f32(src)
    assert a["chunks"] == 3 and len(a["chunk_peak"]) == 3
    for n, peak in enumerate(a["chunk_peak"]):
        true = float(np.max(np.abs(master[n * CS:(n + 1) * CS])))
        assert true <= peak <= true + 1e-6, (n, peak, true)
    assert a["chunk_peak"][0] > 1.4 and a["chunk_gain"] == {"0": 2.0}


def test_a_silent_master_records_no_peaks(tmp_path):
    src = tmp_path / "mute.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=64x36:r=30:d=1",
                    "-c:v", "libx264", "-preset", "ultrafast", str(src)], check=True, capture_output=True)
    wd = tmp_path / "wd"
    wd.mkdir()
    _key, idx, _info = fx.build_proxy_audio(src, wd)
    assert idx["audio"]["silent"] is True and not idx["audio"].get("chunk_peak")
