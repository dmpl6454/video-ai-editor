"""Regenerate the voice-effect table the browser reads and the golden its DSP
is pinned to, from the ONE table (`edl/voice_effects.py`) and REAL ffmpeg.

    .venv/bin/python tests/gen_voice_fx_goldens.py [--check]

* ``frontend/src/lib/voice/voiceFxTable.ts`` — ``voice_effects.payload()``
  as a TS constant (the preview engine reads it synchronously; the Inspector
  reads the same payload from ``GET /api/voice/presets``);
* ``tests/goldens/voice_fx_cases.json`` — what ffmpeg does with each preset's
  own filter text (``audio_mix.voice_filters``) to a deterministic stereo
  input the TS side synthesizes identically (``golden_input``):
  - every preset but the pitch shifters and the reverb, at intensity 1 and
    0.5: output windows, sample for sample (voiceFx.test.ts: within 2e-5);
  - the pitch presets: per-50 ms spectral centroid and RMS of the output
    (the preview's granular shifter is APPROX: voiceFx.test.ts holds it to
    the tolerances support.ts VOICE_FX_PARITY states);
  - the Hall impulse response ``aevalsrc`` synthesizes, windows of both
    channels (voiceFx.ts ``reverbIr`` must match it).

``--check`` regenerates in memory and exits non-zero when either checked-in
file differs (tests/test_voice_effects.py runs the same comparison).
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from video_ai_editor.edl import voice_effects as V  # noqa: E402
from video_ai_editor.render import audio_mix  # noqa: E402

TABLE_TS = ROOT / "frontend" / "src" / "lib" / "voice" / "voiceFxTable.ts"
GOLDEN = ROOT / "tests" / "goldens" / "voice_fx_cases.json"
SR = 48000
N = 48000
WINDOWS = ((0, 192), (12090, 12282), (24100, 24292), (36050, 36242))
PITCHED = ("chipmunk", "deep", "monster")
BLOCK = 2400


def golden_input(n: int = N) -> np.ndarray:
    """(n, 2) float32: a harmonic voice-like tone each side (140 Hz with 20
    harmonics left, 180 Hz with 16 right, 1/k amplitudes), a syllable
    envelope, and an impulse at sample 100 (voiceFx.test.ts `goldenInput`
    computes the same numbers in double and rounds them to float32)."""
    t = np.arange(n, dtype=np.float64) / SR
    env = 0.35 + 0.65 * (np.sin(2 * np.pi * 3.0 * t) > -0.3)
    left = env * sum((0.3 / k) * np.sin(2 * np.pi * 140 * k * t + 0.3 * k) for k in range(1, 21))
    right = env * sum((0.25 / k) * np.sin(2 * np.pi * 180 * k * t + 0.3 * k) for k in range(1, 17))
    left[100] += 0.4
    right[100] += 0.4
    return np.stack([left, right], axis=1).astype(np.float32)


def _ffmpeg_version() -> str:
    out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, check=True).stdout
    return out.split("\n", 1)[0].split(" ")[2]


def run_chain(frag: str, x: np.ndarray) -> np.ndarray:
    """ffmpeg's output of the voice fragment over `x` (f32 stereo in and out)."""
    chain = f"[0:a]anull{frag}[o]"
    raw = subprocess.run(["ffmpeg", "-v", "error", "-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", "pipe:0",
                          "-filter_complex", chain, "-map", "[o]", "-f", "f32le", "-ac", "2", "-ar", str(SR), "-"],
                         input=x.tobytes(), check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype="<f4").reshape(-1, 2)


class _Audio:
    def __init__(self, effect: str, intensity: float):
        self.voice_effect, self.voice_intensity = effect, intensity


def _r(v: float) -> float:
    return float(f"{float(v):.8g}")


def _blocks(y: np.ndarray) -> dict:
    """Per-50 ms spectral centroid (Hz) and RMS (dB) of the left channel."""
    cents, rms = [], []
    f = np.fft.rfftfreq(BLOCK, 1 / SR)
    win = np.hanning(BLOCK)
    for i in range(0, len(y) - BLOCK + 1, BLOCK):
        b = y[i:i + BLOCK, 0].astype(np.float64)
        s = np.abs(np.fft.rfft(b * win)) ** 2
        cents.append(_r(float((s * f).sum() / max(s.sum(), 1e-30))))
        rms.append(_r(10 * math.log10(float(np.mean(b * b)) + 1e-20)))
    return {"centroid": cents, "rms_db": rms}


def generate() -> dict:
    x = golden_input()
    cases = []
    for p in V.PRESETS:
        kinds = {s.kind for s in p.stages}
        for inten in (1.0, 0.5):
            frag = audio_mix.voice_filters(_Audio(p.id, inten), "g")
            y = run_chain(frag, x)
            case = {"effect": p.id, "intensity": inten, "filter": frag, "length": int(len(y))}
            if "reverb" in kinds:
                continue                                # the IR is pinned below
            if "pitch" in kinds:
                case["blocks"] = _blocks(y)
            else:
                case["windows"] = [{"start": a, "L": [_r(v) for v in y[a:b, 0]], "R": [_r(v) for v in y[a:b, 1]]}
                                   for a, b in WINDOWS]
            cases.append(case)
    rv = next(s for s in V.PRESET_BY_ID["reverb"].stages if s.kind == "reverb").params
    k = V.reverb_constants(rv)
    irs = "|".join(audio_mix._reverb_ir_expr(k, c) for c in (0, 1))
    raw = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                          f"aevalsrc=exprs='{irs}':s=48000:d={V.REVERB_IR_SECONDS:g}",
                          "-f", "f32le", "-ac", "2", "-"], check=True, capture_output=True).stdout
    ir = np.frombuffer(raw, dtype="<f4").reshape(-1, 2)
    ir_windows = [{"start": a, "L": [_r(v) for v in ir[a:a + 64, 0]], "R": [_r(v) for v in ir[a:a + 64, 1]]}
                  for a in (0, 1180, 50000, 95000)]
    return {
        "version": 1, "ffmpeg": _ffmpeg_version(), "sample_rate": SR, "n": N, "block": BLOCK,
        "input": {"f": "golden_input", "impulse_at": 100, "left": [140, 20, 0.3], "right": [180, 16, 0.25]}, "cases": cases,
        "reverb_ir": {"params": rv, "samples": int(len(ir)), "windows": ir_windows},
    }


def table_ts() -> str:
    body = json.dumps(V.payload(), indent=2)
    return ("// GENERATED by tests/gen_voice_fx_goldens.py from edl/voice_effects.py —\n"
            "// do not edit (tests/test_voice_effects.py pins it equal to the table).\n"
            f"export const VOICE_FX_TABLE = {body} as const\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    doc = generate()
    ts = table_ts()
    if a.check:
        ok = GOLDEN.exists() and json.loads(GOLDEN.read_text()) == json.loads(json.dumps(doc))
        ok = ok and TABLE_TS.exists() and TABLE_TS.read_text() == ts
        print("voice fx goldens are " + ("current" if ok else "STALE"))
        return 0 if ok else 1
    GOLDEN.write_text(json.dumps(doc, separators=(",", ":")) + "\n")
    TABLE_TS.parent.mkdir(parents=True, exist_ok=True)
    TABLE_TS.write_text(ts)
    print(f"wrote {GOLDEN} ({GOLDEN.stat().st_size} bytes) and {TABLE_TS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
