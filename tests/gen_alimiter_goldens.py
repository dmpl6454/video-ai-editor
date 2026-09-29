"""Regenerate the golden the preview's alimiter port is pinned to (P2 limiter
tail, 0.8.0 final QA), from REAL ffmpeg.

    .venv/bin/python tests/gen_alimiter_goldens.py [--check]

``tests/goldens/alimiter_cases.json``: ffmpeg's ``alimiter`` output, as
float32, for three deterministic stereo inputs the TS side synthesizes
identically (``golden_input``: a xorshift32 stream of int16 steps times an
integer envelope, every sample an exact multiple of 2^-13) through the two
limiters the server's preview runs (``audio_mix``: the mix limiter
``alimiter=limit=0.97:latency=1`` and the loudness stage's
``alimiter=limit=0.891251:level=0:latency=1``). ``frontend/src/lib/preview/
audio/alimiter.test.ts`` holds ``ALimiterCore`` to it BIT FOR BIT.

``--check`` regenerates in memory and exits non-zero when the checked-in file
differs (an ffmpeg whose alimiter changed must be ported again).
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "goldens" / "alimiter_cases.json"
SR = 48000
N = 6000
LIMITERS = (
    {"filter": "alimiter=limit=0.97:latency=1", "limit": 0.97, "auto_level": True},
    {"filter": "alimiter=limit=0.891251:level=0:latency=1", "limit": 0.891251, "auto_level": False},
)


def _xorshift(seed: int):
    x = seed & 0xFFFFFFFF
    while True:
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        yield (x >> 16) - 32768          # int in [-32768, 32767]


def _env(kind: str, ch: int, i: int) -> int:
    """Integer amplitude A: the sample is trunc(u·A/64)·2^-13, |x| ≤ A/16."""
    if kind == "step":
        return 5 if i < 1000 else 32 if i < 3000 else 8          # 0.31 → 2.0 → 0.5
    if kind == "clicks":
        return 48 if i % 480 < 24 else 8                          # 3.0 bursts over 0.5
    # "skew": one side hot in the middle, the other hot at the ends
    hot = 1500 <= i < 3500
    return (40 if hot else 6) if ch == 0 else (6 if hot else 20)


def golden_input(kind: str, seed: int, n: int = N) -> np.ndarray:
    """(n, 2) float32 (alimiter.test.ts `goldenInput` computes the same)."""
    g = _xorshift(seed)
    out = np.zeros((n, 2), dtype=np.float64)
    for i in range(n):
        for ch in range(2):
            u = next(g)
            out[i, ch] = int(u * _env(kind, ch, i) / 64) * 2.0 ** -13
    return out.astype(np.float32)


INPUTS = (("step", 1), ("clicks", 2), ("skew", 3))


def run(filt: str, x: np.ndarray) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", "pipe:0",
                          "-af", filt, "-f", "f32le", "-ac", "2", "-ar", str(SR), "-"],
                         input=x.tobytes(), check=True, capture_output=True).stdout
    y = np.frombuffer(raw, dtype="<f4").reshape(-1, 2)
    assert len(y) == len(x), (len(y), len(x))
    return y


def build() -> dict:
    version = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, check=True).stdout.split()[2]
    cases = []
    for kind, seed in INPUTS:
        x = golden_input(kind, seed)
        for lim in LIMITERS:
            y = run(lim["filter"], x)
            cases.append({"input": kind, "seed": seed, **lim,
                          "peak_in": float(np.abs(x).max()),
                          "out_f32le_b64": base64.b64encode(np.ascontiguousarray(y, dtype="<f4").tobytes()).decode()})
    return {"ffmpeg": version, "sample_rate": SR, "n": N, "cases": cases}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    doc = build()
    text = json.dumps(doc, indent=1) + "\n"
    if ap.parse_args().check:
        old = json.loads(GOLDEN.read_text())
        same = [c["out_f32le_b64"] for c in old["cases"]] == [c["out_f32le_b64"] for c in doc["cases"]]
        print("alimiter golden:", "up to date" if same else "DIFFERS (ffmpeg's alimiter changed: re-port alimiter.ts)")
        return 0 if same else 1
    GOLDEN.write_text(text)
    print(f"wrote {GOLDEN} ({len(text) // 1024} KiB, ffmpeg {doc['ffmpeg']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
