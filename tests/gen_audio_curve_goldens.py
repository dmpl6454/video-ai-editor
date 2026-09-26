"""Regenerate ``tests/goldens/audio_curve_cases.json`` from REAL ffmpeg output.

    .venv/bin/python tests/gen_audio_curve_goldens.py [--check]

The instant-preview mix (``frontend/src/lib/preview/audio/curves.ts``,
INSTANT_PREVIEW_SPEC §6 R10) must shape sound exactly as the export does, so
nothing here trusts a formula. Every gain in the golden is what ffmpeg itself
multiplied a DC signal of 1.0 by:

* ``curves``: ``afade`` in and out for every curve ffmpeg has, sampled at every
  sample of a short fade and on a 1 ms grid of a long one;
* ``emitted``: the ``afade`` filters the export's own chain builders
  (``compositor._audio_props_filters`` for v1 and PiP clips,
  ``audio_mix._audio_clip_filter`` for the music / voice-over / audio lanes)
  emit for a spread of clips — the filter text itself, and where ffmpeg put
  the fade (its first sample and its length);
* ``acrossfade``: the ``acrossfade=d=…`` the v1 assembly emits at real seams
  (``compositor._audio_only_graph``), with the overlap length and both gains;
* ``gain_env``: ``audio_mix.gain_env_filter`` for every interpolation, rendered
  one sample per frame (``asetnsamples=1``) so ``eval=frame`` is evaluated at
  every sample: the curve the expression describes.

``--check`` regenerates in memory and exits non-zero when the checked-in file
differs (tests/test_audio_curve_golden.py runs the same comparison).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from video_ai_editor.edl import timebase as tb  # noqa: E402
from video_ai_editor.edl.schema import (  # noqa: E402
    AudioProps, Canvas, Clip, Keyframe, Transition, empty_edl,
)
from video_ai_editor.render import audio_mix, compositor  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "goldens" / "audio_curve_cases.json"
SR = 48000
VERSION = 1

#: Every afade curve ffmpeg 8 knows (af_afade.c `curve` option).
CURVES = ["tri", "qsin", "esin", "hsin", "log", "ipar", "qua", "cub", "squ", "cbr", "par", "exp",
          "iqsin", "ihsin", "dese", "desi", "losi", "sinc", "isinc", "quat", "quatr", "qsin2",
          "hsin2", "nofade"]
#: Curves also pinned over a long fade on a 1 ms grid.
LONG_CURVES = ["tri", "qsin", "esin", "hsin", "exp"]


def _ffmpeg_version() -> str:
    out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, check=True).stdout
    return out.split("\n", 1)[0].split(" ")[2]


def _dc(filt: str, seconds: float) -> np.ndarray:
    """ffmpeg's output for a mono DC 1.0 through `filt`, as doubles."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"aevalsrc=1:s={SR}:d={seconds:.6f}",
         "-af", f"aformat=sample_fmts=dbl,{filt}", "-f", "f64le", "-"],
        check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float64)


def _pairs(g: np.ndarray, idx) -> list[list[float]]:
    return [[int(i), round(float(g[i]), 9)] for i in idx if 0 <= i < len(g)]


def _grid(start: int, rng: int, step: int, pad: int = 3) -> list[int]:
    """Every `step`-th sample of [start, start + rng], plus every sample within
    `pad` of either edge."""
    idx = set(range(start, start + rng + 1, step))
    for e in (start, start + rng):
        idx.update(range(e - pad, e + pad + 1))
    return sorted(i for i in idx if i >= 0)


# ---------------------------------------------------------------- afade curves

def curve_cases() -> list[dict]:
    cases = []
    for curve in CURVES:
        for kind in ("in", "out"):
            for st, d, step in (("0.010", "0.005", 1), ("0.002", "0.500", 48)):
                if step != 1 and curve not in LONG_CURVES:
                    continue
                filt = f"afade=t={kind}:st={st}:d={d}:curve={curve}"
                start, rng = round(float(st) * SR), round(float(d) * SR)
                g = _dc(filt, float(st) + float(d) + 0.01)
                cases.append({"filter": filt, "type": kind, "curve": curve, "st": st, "d": d,
                              "start": start, "range": rng, "len": len(g),
                              "samples": _pairs(g, _grid(start, rng, step))})
    return cases


# ---------------------------------------------------------------- emitted afades

_AFADE = re.compile(r"afade=t=(in|out):st=([0-9.]+):d=([0-9.]+)")


def _measure_fade(g: np.ndarray, kind: str) -> tuple[int, int]:
    """(first sample, length) of the ramp ffmpeg applied to a DC signal."""
    if kind == "in":
        start = int(np.argmax(g > 0)) - 1
        end = start + 1 + int(np.argmax(g[start + 1:] >= 1.0))
    else:
        start = int(np.argmax(g < 1.0)) - 1
        end = start + 1 + int(np.argmax(g[start + 1:] <= 0.0))
    return start, end - start


def _clip(**kw) -> Clip:
    audio = AudioProps(**{k: kw.pop(k) for k in ("fade_in", "fade_out", "gain_db", "mute") if k in kw})
    c = Clip(src="/dev/null", id=kw.pop("id", "c"), **{k: v for k, v in kw.items() if k not in ("in_", "out")})
    c.in_, c.out = kw.get("in_", 0.0), kw.get("out", 1.0)
    c.audio = audio
    return c


#: (lane, fade_in, fade_out, in, out, speed, render window) — v1/PiP rows use
#: the clip's own chain (fades on clip-local time), lane rows the lane chain
#: (fades on the render clock, from the window).
EMITTED_ROWS = [
    ("v1", 0.5, 0.5, 0.0, 2.0, None, None),
    ("v1", 0.25, 0.0, 1.0, 2.2345, None, None),
    ("v1", 0.0, 0.3333, 0.4, 1.7777, None, None),
    ("v1", 0.1235, 0.0625, 0.0, 1.0, None, None),     # %.3f ties: 0.1235 and 0.0625
    ("v1", 0.2, 0.4, 0.0, 3.0, 2.0, None),            # effective_duration 1.5
    ("v1", 0.2, 0.3, 0.5, 1.5, 0.5, None),            # effective_duration 2.0
    ("v1", 0.0004, 0.0015, 0.0, 1.0, None, None),     # below / at the 1 ms threshold
    ("lane", 0.5, 0.5, 0.0, 4.0, None, (1.0, 5.0)),
    ("lane", 0.2, 0.3, 2.0, 3.5, None, (0.7335, 2.2335)),
    ("lane", 0.25, 0.25, 0.0, 2.0, None, (3.2105, 4.9)),   # window shortened by a seam
    ("lane", 0.1, 0.0625, 1.0, 2.0, None, (0.0625, 1.0625)),
]


def emitted_cases() -> list[dict]:
    cases = []
    for lane, fin, fout, in_, out, speed, win in EMITTED_ROWS:
        c = _clip(fade_in=fin, fade_out=fout, in_=in_, out=out, speed=speed)
        if lane == "v1":
            chain = compositor._audio_props_filters(c)
        else:
            chain = audio_mix._audio_clip_filter("[0:a]", c, "[o]", window=win)
        fades = []
        for kind, st, d in _AFADE.findall(chain):
            g = _dc(f"afade=t={kind}:st={st}:d={d}", float(st) + float(d) + 0.05)
            start, rng = _measure_fade(g, kind)
            fades.append({"type": kind, "st": st, "d": d, "start": start, "range": rng})
        cases.append({"lane": lane, "fade_in": fin, "fade_out": fout, "in": in_, "out": out,
                      "speed": speed, "effective_duration": c.effective_duration,
                      "window": list(win) if win else None, "fades": fades})
    return cases


# ---------------------------------------------------------------- acrossfade

_XFADE = re.compile(r"acrossfade=d=([0-9.]+)")


def _crossfade(d: str, la: float = 1.0, lb: float = 1.0) -> tuple[np.ndarray, np.ndarray, int]:
    def run(a: int, b: int) -> np.ndarray:
        fc = ("[0:a]aformat=sample_fmts=dbl[x];[1:a]aformat=sample_fmts=dbl[y];"
              f"[x][y]acrossfade=d={d}")
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"aevalsrc={a}:s={SR}:d={la}",
             "-f", "lavfi", "-i", f"aevalsrc={b}:s={SR}:d={lb}", "-filter_complex", fc,
             "-f", "f64le", "-"], check=True, capture_output=True).stdout
        return np.frombuffer(raw, dtype=np.float64)
    g0, g1 = run(1, 0), run(0, 1)
    return g0, g1, int(round(la * SR)) + int(round(lb * SR)) - len(g0)


def _seam_costs() -> list[tuple[float, float, str]]:
    """(fps, seam cost seconds, emitted d text) for real v1 seams."""
    rows = []
    for fps, durs in ((30, (0.5, 0.37, 1.0)), (tb.fps_float(tb.rate_of(29.97)), (0.5, 0.2)),
                      (25, (0.5, 0.33)), (tb.fps_float(tb.rate_of(23.976)), (0.4,))):
        for d in durs:
            e = empty_edl(Canvas(w=64, h=36, fps=fps))
            v1 = e.get_track("v1")
            a = Clip(src="/dev/null", start=0.0, id="a")
            a.in_, a.out = 0.0, 2.0
            b = Clip(src="/dev/null", start=2.0, id="b")
            b.in_, b.out = 0.0, 2.0
            v1.clips += [a, b]
            v1.transitions.append(Transition(at=2.0, duration=d))
            e.recompute_duration()
            _inputs, fc, _label = compositor._audio_only_graph(e, fps=fps, first_input=0,
                                                               apply_loudnorm=False)
            emitted = _XFADE.findall(fc)
            (seam, cost), = e.v1_seam_table()
            rows.append((fps, cost, emitted[0]))
    return rows


def acrossfade_cases() -> list[dict]:
    cases = []
    for fps, cost, d in _seam_costs():
        g0, g1, m = _crossfade(d)
        s = SR - m                                    # first overlapped output sample
        idx = [s + i for i in _grid(0, m - 1, 48)]
        cases.append({"fps": fps, "cost": cost, "d": d, "m": m, "first": s,
                      "g0": _pairs(g0, idx), "g1": _pairs(g1, idx)})
    for d in ("0.001000", "0.000500"):                 # tiny overlaps: every sample
        g0, g1, m = _crossfade(d)
        s = SR - m
        idx = list(range(s - 2, s + m + 2))
        cases.append({"fps": None, "cost": float(d), "d": d, "m": m, "first": s,
                      "g0": _pairs(g0, idx), "g1": _pairs(g1, idx)})
    return cases


# ---------------------------------------------------------------- gain_env

ENVS = [
    ("linear", [[0.0, 0.0], [0.5, -6.0], [1.0, 3.0]]),
    ("linear", [[0.12345, -3.0], [0.67891, -12.34567]]),
    ("ease-in", [[0.1, 0.0], [0.9, -9.0]]),
    ("ease-out", [[0.1, 0.0], [0.9, -9.0]]),
    ("ease-in-out", [[0.0, -12.0], [0.8, 0.0], [1.2, -3.0]]),
    ("back-out", [[0.2, 0.0], [1.0, -6.0]]),
    ("step", [[0.0, 0.0], [0.4, -6.0], [0.8, -2.0]]),
    ("bounce", [[0.0, -6.0], [1.0, 0.0]]),
    ("linear", [[0.3, -4.5]]),
]


def gain_env_cases() -> list[dict]:
    cases = []
    for interp, kfs in ENVS:
        audio = AudioProps(gain_env=Keyframe(keyframes=[tuple(k) for k in kfs], interp=interp))
        filt = audio_mix.gain_env_filter(audio)
        g = _dc(f"asetnsamples=n=1,{filt}", 1.3)
        idx = sorted(set(range(0, len(g), 48)) | {len(g) - 1})
        cases.append({"interp": interp, "keyframes": kfs, "filter": filt,
                      "samples": _pairs(g, idx)})
    return cases


def generate() -> dict:
    return {"version": VERSION, "sample_rate": SR, "ffmpeg": _ffmpeg_version(),
            "generator": "tests/gen_audio_curve_goldens.py",
            "curves": curve_cases(), "emitted": emitted_cases(),
            "acrossfade": acrossfade_cases(), "gain_env": gain_env_cases()}


def dumps(d: dict) -> str:
    return json.dumps(d, separators=(",", ":"), sort_keys=True) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="compare with the checked-in golden")
    ns = ap.parse_args()
    text = dumps(generate())
    if ns.check:
        same = GOLDEN.is_file() and GOLDEN.read_text() == text
        print("golden up to date" if same else f"golden differs: {GOLDEN}")
        return 0 if same else 1
    GOLDEN.write_text(text)
    print(f"wrote {GOLDEN} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
