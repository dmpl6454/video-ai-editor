"""R9's ONE audio start rule, measured (INSTANT_PREVIEW_SPEC §6 R9; wave E gate,
lane X1).

A clip's sound starts on source sample ``S(in) = timebase.edit_sample(in)``:
the NEAREST sample to ``in`` as ffmpeg reads it. That is the output side's own
rule — frame ``k`` starts on ``samples_for_frames(k)``, the nearest sample to
its start — so the sample nearest a source frame's start plays on the sample
nearest the output frame's start, and a click on a source frame's first sample
(``samples_for_frames(f, Rs)``, how this editor's own renders lay sound out)
lands on the first sample of the output frame that clip starts on.

The rule it replaces was TWO roundings, ``S(in − pre) + round(pre · 48 kHz)``
for a half-frame pre-roll cut by ``atrim=start=`` in seconds: one sample late
about a third of the time at 29.97, dropping that frame's own click from the
clip (in = frame 319: 510911 for 510910). Review RE's in-anchored seek moved
the v1 chain to ``S(in)`` and left the program map, the client and the
reversed / speed-curve intermediates on the old rule — the gate's P1-A1
failure (client 152633, server 152632).

* ``test_click_per_frame_av_at_every_standard_rate``: click-per-frame and
  sample-counter sources at 30 and 29.97, cut on the source grid, off it and
  on the project grid, at all nine standard project rates, through
  ``_audio_only_graph`` (the audio-only export, the preview remux, the
  export's loudness pass): every clip starts on ``S(in)`` = the model's
  ``src0``, every click lands where the model puts it, and every click's
  error against the continuous source clock is within the start rule's own
  half sample (the two-roundings rule reached 0.6 at 29.97 and dropped a
  source-grid cut's first click);
* ``test_every_chain_starts_a_clip_on_S_in``: the same start through the PiP
  fold, the music and voice-over lanes, the speed-curve intermediate, and the
  real renders — the export's single pass, the single-pass preview and the
  CHUNKED preview — with their AAC swapped for a lossless codec so every
  sample can be read.
"""
from __future__ import annotations

import subprocess
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from video_ai_editor.edl import timebase as tb
from video_ai_editor.edl.schema import Canvas, Clip, empty_edl
from video_ai_editor.render import compositor
from video_ai_editor.render.frame_map import (
    SourceInfo, audio_placements, build_program_map, ffmpeg_us, rescale,
)

SR = 48000
SECONDS = 16
SILENCE = -1
SRC_RATES = (Fraction(30), Fraction(30000, 1001))
COUNTER = ("aevalsrc=exprs='(mod(n\\,32768)-16384)/32768|"
           "(mod(floor(n/32768)\\,32768)-16384)/32768':s=48000:d={d}")


def _source(path: Path, rate: Fraction, audio: str) -> str:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"color=c=gray:s=64x36:r={tb.ffmpeg_rate(rate)}:d={SECONDS}",
                    "-f", "lavfi", "-i", audio, "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "pcm_s16le", "-shortest", str(path)], check=True, capture_output=True)
    return str(path)


def _click_expr(rate: Fraction) -> str:
    """A 0.5 click on every source frame's first sample, samples_for_frames(f)
    (tests/wk/audio_fixture.click_source)."""
    num, den = rate.numerator, rate.denominator
    e = f"0.5*eq(n\\,round(round(n*{num}/({SR}*{den}))*{SR}*{den}/{num}))"
    return f"aevalsrc=exprs='{e}|{e}':s={SR}:d={SECONDS}"


@pytest.fixture(scope="module")
def sources(tmp_path_factory) -> dict[tuple[str, Fraction], str]:
    d = tmp_path_factory.mktemp("start-rule")
    out = {}
    for r in SRC_RATES:
        tag = f"{r.numerator}_{r.denominator}"
        out[("click", r)] = _source(d / f"click{tag}.mov", r, _click_expr(r))
        out[("counter", r)] = _source(d / f"counter{tag}.mov", r, COUNTER.format(d=SECONDS))
    return out


def _counter(a: np.ndarray) -> np.ndarray:
    """(n, 2) float or int16-scaled → source sample index per row."""
    lo = np.round(a[:, 0].astype(np.float64) * 32768 + 16384).astype(np.int64)
    hi = np.round(a[:, 1].astype(np.float64) * 32768 + 16384).astype(np.int64)
    n = hi * 32768 + lo
    n[(np.abs(a[:, 0]) < 1e-9) & (np.abs(a[:, 1]) < 1e-9)] = SILENCE
    return n


def _render(edl, fps, cache: Path) -> np.ndarray:
    from video_ai_editor.render.reverse import with_reversed_sources
    from video_ai_editor.render.speed_audio import prepare
    sub = with_reversed_sources(edl, cache, fps)
    prepare(sub, cache, fps)
    inputs, fc, label = compositor._audio_only_graph(sub, fps=fps, first_input=0, apply_loudnorm=False)
    raw = subprocess.run(["ffmpeg", "-v", "error", *inputs, "-filter_complex", fc, "-map", label,
                          "-f", "f32le", "-ac", "2", "-ar", str(SR), "-"],
                         check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32).reshape(-1, 2)


def _two_roundings(t: float, fps) -> int:
    """The rule R9 replaced: a half-project-frame pre-roll seek, then
    `atrim=start=<pre>` in seconds — ``S(seek) + round(pre · 48 kHz)``."""
    pre = tb.seek_preroll(t, fps)
    seek = max(0.0, float(t) - pre)
    us = Fraction(1, 1_000_000)
    return ((rescale(ffmpeg_us(seek), us, Fraction(1, SR)) if seek > 0 else 0)
            + (rescale(ffmpeg_us(pre), us, Fraction(1, SR)) if pre > 1e-9 else 0))


def _sweep_edl(rate: Fraction, src_rate: Fraction, src: str):
    """Four 1x v1 clips with gaps: in on the source grid, off it, on the
    project grid, and within the file's first 0.5 s (no input seek)."""
    fps = tb.fps_float(rate) if rate.denominator != 1 else int(rate)
    e = empty_edl(Canvas(w=64, h=36, fps=fps))
    e.canvas.loudness_lufs = None
    ins = {
        "src_grid": Fraction(319) / src_rate,
        "off_grid": (Fraction(211) + Fraction(37, 100)) / src_rate,
        "proj_grid": Fraction(tb.frame_of(7.3, fps)) / rate,
        "head": Fraction(7) / src_rate,
    }
    cursor = 5
    for name, fin in ins.items():
        c = Clip(src=src, start=tb.time_of(cursor, fps), id=name)
        c.in_ = float(fin)
        c.out = c.in_ + 0.4
        c.audio.keep_pitch = False
        e.get_track("v1").clips.append(c)
        cursor += tb.frame_of(0.4, fps) + 3
    e.recompute_duration()
    return e, fps


@pytest.mark.parametrize("rate", tb.STANDARD_RATES, ids=lambda r: f"{float(r):.3f}")
def test_click_per_frame_av_at_every_standard_rate(rate, sources, tmp_path):
    report = {}
    old_rule_drops_a_click = False
    for src_rate in SRC_RATES:
        e, fps = _sweep_edl(rate, src_rate, sources[("click", src_rate)])
        info = SourceInfo.cfr(src_rate, SECONDS * 30, width=64, height=36)
        pm = build_program_map(e, {sources[("click", src_rate)]: info})
        places = audio_placements(e, pm, sources={sources[("click", src_rate)]: info})
        assert len(places) == 4
        clicks_all = np.nonzero(np.abs(_render(e, fps, tmp_path / "c")[:, 0]) > 0.25)[0]
        src_clicks = np.array([tb.samples_for_frames(f, src_rate) for f in range(int(SECONDS * src_rate) + 1)])
        worst = worst_old = Fraction(0)
        for a in places:
            c = pm.clips[a.clip]
            s_in = tb.edit_sample(c.in_)
            assert a.src0 == s_in, (c.id, a.src0, s_in)
            got = clicks_all[(clicks_all >= a.out0) & (clicks_all < a.out0 + a.n)]
            mine = src_clicks[(src_clicks >= s_in) & (src_clicks < s_in + a.n)]
            assert list(got) == list(a.out0 + mine - s_in), (c.id, list(got[:3]), list(a.out0 + mine[:3] - s_in))
            # A/V of the START: source instant c_f/48000 must play
            # (c_f/48000 − in) after the clip's first sample; the error of
            # every click, in samples — the start rule's own rounding, within
            # half a sample of the printed `in` (+ its µs print, 0.024).
            # (Where that first sample sits on the program, `out0`, is the
            # assembly's per-segment `samples_for_frames` sum, shared by the
            # server and both models — X1 notDone.)
            ideal = [a.out0 + int(cf) - Fraction(c.in_) * SR for cf in mine]
            err = [int(j) - x for j, x in zip(got, ideal)]
            assert max(abs(x) for x in err) < Fraction(53, 100), (c.id, [float(x) for x in err[:4]])
            worst = max(worst, *(abs(x) for x in err))
            shift = _two_roundings(c.in_, fps) - s_in
            worst_old = max(worst_old, *(abs(x - shift) for x in err))
            if c.id == "src_grid":
                # the clip's first video frame's own click IS its first sample
                assert got[0] == a.out0, (c.id, got[:2], a.out0)
                old_rule_drops_a_click |= shift != 0
        # the counter: every sample of every clip is the model's source sample
        e2, _ = _sweep_edl(rate, src_rate, sources[("counter", src_rate)])
        n = _counter(_render(e2, fps, tmp_path / "n"))
        pm2 = build_program_map(e2, {sources[("counter", src_rate)]: info})
        for a in audio_placements(e2, pm2):
            assert n[a.out0] == tb.edit_sample(pm2.clips[a.clip].in_) == a.src0
            assert np.array_equal(n[a.out0:a.out0 + a.n], a.src0 + np.arange(a.n)), pm2.clips[a.clip].id
        report[str(float(src_rate))[:5]] = (round(float(worst), 3), round(float(worst_old), 3))
    print(f"R={float(rate):.3f} max |A/V error| in samples (S(in), two roundings):", report)
    if rate == Fraction(30000, 1001):
        # The sweep discriminates: at 29.97 the old rule starts a source-grid
        # cut one sample late, past its first frame's click.
        assert old_rule_drops_a_click


# ------------------------------------------------------------ every chain

IN_FRAME = 319
R2997 = Fraction(30000, 1001)


def _edl2997():
    fps = tb.fps_float(R2997)
    e = empty_edl(Canvas(w=64, h=36, fps=fps))
    e.canvas.loudness_lufs = None
    return e, fps


def _clip(src: str, cid: str, start_frames: int, fps, **kw) -> Clip:
    c = Clip(src=src, start=tb.time_of(start_frames, fps), id=cid, **kw)
    c.in_ = tb.time_of(IN_FRAME, fps)
    c.out = c.in_ + 0.5
    return c


def _clicks_from(x: np.ndarray, at: int) -> list[int]:
    return [int(j) for j in np.nonzero(np.abs(x[:, 0]) > 0.2)[0] if j >= at][:3]


def test_every_chain_starts_a_clip_on_S_in(sources, tmp_path, monkeypatch):
    s_in = 510910
    fps = tb.fps_float(R2997)
    assert tb.edit_sample(tb.time_of(IN_FRAME, fps)) == s_in != _two_roundings(tb.time_of(IN_FRAME, fps), fps)
    click, counter = sources[("click", R2997)], sources[("counter", R2997)]
    frame = tb.samples_for_frames(IN_FRAME + 1, R2997) - tb.samples_for_frames(IN_FRAME, R2997)
    base = [0, frame]          # the first frame's own click, then the next frame's

    # the PiP fold (v1 silent under it) and the music / voice-over / audio lanes:
    # the clip's first sample is its first frame's click
    for track in ("v2", "music", "vo", "a1"):
        e, fps = _edl2997()
        under = _clip(click, "under", 0, fps)
        under.in_, under.out, under.audio.mute = 0.0, 2.0, True
        e.get_track("v1").clips.append(under)
        e.get_track(track).clips.append(_clip(click, f"on_{track}", 30, fps))
        e.recompute_duration()
        at = tb.samples_for_frames(30, fps) if track == "v2" else int(round(tb.time_of(30, fps) * 1000)) * 48
        got = _clicks_from(_render(e, fps, tmp_path / track), at - 5)
        assert [g - at for g in got[:2]] == base, (track, got, at)

    # the speed-curve intermediate decodes from S(in)
    from video_ai_editor.render import speed_audio
    c = _clip(counter, "cv", 0, fps, speed={"curve": [[0.0, 0.5], [1.0, 2.0]]})
    first = _counter(speed_audio._decode(c, fps).T[:4])
    assert list(first) == [s_in, s_in + 1, s_in + 2, s_in + 3]

    # the real renders, AAC swapped for ALAC (lossless in mp4, no priming):
    # the export's single pass, the single-pass preview and the chunked preview
    alac = ["-c:a", "alac", "-ar", str(SR), "-ac", "2"]
    monkeypatch.setattr(compositor, "_AAC_OUT", list(alac))
    monkeypatch.setattr(compositor, "_AAC_DELIVERY_OUT", list(alac))
    monkeypatch.setattr(compositor, "_preview_aac_out", lambda: list(alac))
    e, fps = _edl2997()
    e.get_track("v1").clips.append(_clip(counter, "x", 0, fps))
    e.recompute_duration()

    def first_samples(path: Path) -> list[int]:
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "2", "-"],
                             capture_output=True, check=True).stdout
        a = np.frombuffer(raw, dtype="<i2").reshape(-1, 2).astype(np.float64) / 32768
        return list(_counter(a)[:3])

    want = [s_in, s_in + 1, s_in + 2]
    for preview in (False, True):
        out = compositor._render(e, tmp_path / f"single{int(preview)}.mp4", height=36, fps=fps,
                                 preview=preview, cache_dir=tmp_path / f"cache{int(preview)}", chunked=False)
        assert first_samples(out) == want, ("export" if not preview else "preview", first_samples(out))
    from video_ai_editor.render import render_preview
    sess = tmp_path / "sess"
    sess.mkdir()
    res = render_preview(e, sess)
    chunks = sorted((sess / "cache" / "chunks").glob("chunk_*.mp4"))
    assert chunks, "the preview did not take the chunked path"
    assert first_samples(chunks[0]) == want
    assert first_samples(res.path) == want
