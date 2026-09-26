"""Sound placement of the program map against the REAL audio graph (§6 R9).

Sources carry a SAMPLE COUNTER as PCM (left = low 15 bits, right = high 15
bits of the sample index), so every decoded output sample says exactly which
source sample it is. Timelines are rendered through ``_audio_only_graph`` —
the graph the preview remux, the export's loudness pass and the audio-only
export all use, "the render's sound, sample for sample" — without mastering,
and compared with ``frame_map.audio_placements``:

* 1x and reversed clips: every output sample outside an ``acrossfade`` equals
  the model's source sample (exact);
* varispeed clips: within one sample of the nominal ``src0 + i · rate``;
* gaps and the tail past a short source: silence;
* the total length equals the model's assembly sum.
"""
from __future__ import annotations

import random
import subprocess
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from video_ai_editor.edl import timebase as tb
from video_ai_editor.edl.schema import Canvas, Clip, Transition, empty_edl
from video_ai_editor.render import compositor
from video_ai_editor.render.frame_map import (
    SourceInfo, audio_placements, audio_total_samples, build_program_map,
)
from video_ai_editor.render.reverse import with_reversed_sources

SILENCE = -1
SOURCE_SECONDS = 14


def _counter_source(path: Path, rate: Fraction, seconds: float = SOURCE_SECONDS) -> Path:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
         f"color=c=gray:s=64x36:r={tb.ffmpeg_rate(rate)}:d={seconds}",
         "-f", "lavfi", "-i",
         ("aevalsrc=exprs='(mod(n\\,32768)-16384)/32768|"
          f"(mod(floor(n/32768)\\,32768)-16384)/32768':s=48000:d={seconds}"),
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "pcm_s16le", "-shortest", str(path)],
        check=True, capture_output=True)
    return path


def _decode(raw: bytes) -> np.ndarray:
    a = np.frombuffer(raw, dtype=np.float32).reshape(-1, 2)
    lo = np.round(a[:, 0] * 32768 + 16384).astype(np.int64)
    hi = np.round(a[:, 1] * 32768 + 16384).astype(np.int64)
    n = hi * 32768 + lo
    n[(np.abs(a[:, 0]) < 1e-9) & (np.abs(a[:, 1]) < 1e-9)] = SILENCE
    return n


@pytest.fixture(scope="module")
def sources(tmp_path_factory) -> dict[Fraction, str]:
    d = tmp_path_factory.mktemp("counters")
    return {r: str(_counter_source(d / f"c{r.numerator}_{r.denominator}.mov", r))
            for r in (Fraction(30), Fraction(30000, 1001), Fraction(25), Fraction(24000, 1001))}


def _render(edl, fps, cache: Path) -> np.ndarray:
    sub = with_reversed_sources(edl, cache, fps)
    inputs, fc, label = compositor._audio_only_graph(sub, fps=fps, first_input=0,
                                                     apply_loudnorm=False)
    raw = subprocess.run(["ffmpeg", "-v", "error", *inputs, "-filter_complex", fc, "-map", label,
                          "-f", "f32le", "-ac", "2", "-ar", "48000", "-"],
                         check=True, capture_output=True).stdout
    return _decode(raw)


def _timeline(rate: Fraction, src: str, rng: random.Random, *, with_seams: bool) -> tuple:
    fps = tb.fps_float(rate) if rate.denominator != 1 else int(rate)
    e = empty_edl(Canvas(w=64, h=36, fps=fps))
    e.canvas.loudness_lufs = None
    v1 = e.get_track("v1")
    cursor = 0
    trans = []
    for i in range(9):
        kind = rng.choice(["exact", "exact", "exact", "offgrid", "reverse", "vari", "gap"])
        if kind == "gap":
            cursor += rng.randint(1, 9)
            continue
        fin = Fraction(rng.randint(0, int(9 * rate)))
        if kind == "offgrid":
            fin += rng.choice([Fraction(3, 10), -Fraction(3, 10), Fraction(1, 2), -Fraction(1, 2),
                               Fraction(1, 7)])
        span = rng.uniform(0.2, 1.3) if kind != "reverse" else rng.choice([0.5, 4.6])
        speed = rng.choice([0.5, 2.0, 1.5]) if kind == "vari" else None
        c = Clip(src=src, start=tb.time_of(cursor, fps), speed=speed,
                 reverse=kind == "reverse", id=f"a{i}")
        c.in_ = max(0.0, float(fin / rate))
        c.out = c.in_ + span
        c.audio.keep_pitch = False
        v1.clips.append(c)
        cursor += max(1, tb.frame_of(span / (speed or 1.0), fps))
        if with_seams and rng.random() < 0.5:
            trans.append((c.start + c.effective_duration, rng.choice([0.2, 0.37, 0.5])))
    # One clip past the source's end: the tail is silence (apad).
    tail = Clip(src=src, start=tb.time_of(cursor, fps), id="tail")
    tail.in_, tail.out = SOURCE_SECONDS - 0.3, SOURCE_SECONDS + 0.4
    v1.clips.append(tail)
    for at, d in trans:
        v1.transitions.append(Transition(at=at, duration=d))
    e.recompute_duration()
    return e, fps


def _check(n: np.ndarray, edl, fps, info: SourceInfo, src: str) -> list[str]:
    errs = []
    pm = build_program_map(edl, {src: info})
    total = audio_total_samples(edl, pm)
    if len(n) != total:
        errs.append(f"length: rendered {len(n)}, model {total}")
    src_samples = SOURCE_SECONDS * 48000
    covered = np.zeros(len(n), dtype=bool)
    for a in audio_placements(edl, pm, sources={src: info}):
        lo, hi = a.out0 + a.fade_in, a.out0 + a.n - a.fade_out
        covered[max(0, a.out0):a.out0 + a.n] = True
        if a.mode in ("exact", "reverse"):
            exp = np.full(a.n, SILENCE, dtype=np.int64)
            for off, cnt, first, d in a.runs:
                exp[off:off + cnt] = first + d * np.arange(cnt)
            exp[(exp < 0) | (exp >= src_samples)] = SILENCE
            got = n[a.out0:a.out0 + a.n]
            sl = slice(a.fade_in, a.n - a.fade_out)
            bad = np.nonzero(exp[sl] != got[sl])[0]
            if len(bad):
                k = bad[0] + a.fade_in
                errs.append(f"clip {pm.clips[a.clip].id} {a.mode}: out {a.out0 + k} "
                            f"model {exp[k]} rendered {got[k]} ({len(bad)} samples)")
        elif a.mode == "varispeed":
            i = np.arange(a.fade_in, a.n - a.fade_out)
            got = n[a.out0 + i]
            nominal = a.src0 + i * a.rate
            # A resampled counter is only readable away from the low word's
            # wrap (the resampler rings across the 32767 → 0 jump for a few
            # dozen input samples).
            low = np.mod(nominal, 32768)
            ok = (got != SILENCE) & (low > 64) & (low < 32768 - 64)
            dev = np.abs(got[ok] - nominal[ok])
            if len(dev) and dev.max() > 1.0:
                errs.append(f"clip {pm.clips[a.clip].id} varispeed: max deviation {dev.max():.1f}")
        del lo, hi
    gaps = n[~covered]
    if len(gaps) and (gaps != SILENCE).any():
        errs.append(f"{int((gaps != SILENCE).sum())} gap samples are not silence")
    return errs


RATES = [Fraction(30), Fraction(30000, 1001), Fraction(25), Fraction(24000, 1001)]


@pytest.mark.parametrize("rate", RATES, ids=lambda r: str(float(r))[:6])
@pytest.mark.parametrize("seed", [1, 2])
def test_audio_placement_is_sample_exact(rate, seed, sources, tmp_path):
    rng = random.Random(seed * 1000 + rate.numerator)
    src = sources[rate if seed == 1 else RATES[(RATES.index(rate) + 1) % len(RATES)]]
    edl, fps = _timeline(rate, src, rng, with_seams=seed == 2)
    info = SourceInfo.cfr(Fraction(30), 1, width=64, height=36)   # audio ignores the video fields
    n = _render(edl, fps, tmp_path / "cache")
    errs = _check(n, edl, fps, info, src)
    assert errs == [], "\n".join(errs[:10])


def test_the_two_rounding_start_rule_is_needed(sources, tmp_path):
    """round(in · 48 kHz) is off by one where the seek's and the pre-roll's
    roundings do not cancel (in = 319 frames at 29.97: t·48k = 510910.4, the
    chain plays 510911)."""
    rate = Fraction(30000, 1001)
    fps = tb.fps_float(rate)
    e = empty_edl(Canvas(w=64, h=36, fps=fps))
    e.canvas.loudness_lufs = None
    c = Clip(src=sources[rate], start=0.0, id="x")
    c.in_ = tb.time_of(319, fps)
    c.out = c.in_ + 0.2
    e.get_track("v1").clips.append(c)
    e.recompute_duration()
    n = _render(e, fps, tmp_path / "cache")
    assert round(Fraction(c.in_) * 48000) == 510910
    assert n[0] == 510911
    pm = build_program_map(e, {sources[rate]: SourceInfo.cfr(rate, 1)})
    assert audio_placements(e, pm)[0].src0 == 510911
