"""Speed CURVES and FREEZE frames, rendered (Wave D, lane S1).

A clip's `speed` may be a curve ``{"curve": [[x, r], ...]}`` (x over the
clip's output, piecewise-linear speed) and a clip may be a freeze
(`Clip.freeze`: one held frame, silence). Everything here is measured from
real ffmpeg output — decoded frames, decoded samples, ffprobe — never from
the model the render is supposed to agree with:

* the schema: normalisation, footprint = the curve's integral on the frame
  grid, a freeze is a still, the JSON of an EDL without the new fields is
  byte-for-byte what it was, and a .vae save/open keeps both;
* the picture: the `setpts` the compositor prints is evaluated by ffmpeg to
  the SAME ticks `speed_curve.out_seconds` computes (three time bases), and
  a render's frame count is the plan's;
* the sound: a click track follows the curve (varispeed within 1.5 ms,
  pitch-kept within 12 ms — a frame is 33 ms), a tone has no click at the
  curve's piece joins, and a freeze is digital silence between sounding
  neighbours, on the export and on the chunked preview.

(Frame SELECTION of curves and freezes against decoded bar-coded renders at
five rates lives in the frame-map goldens: tests/test_frame_map_golden.py,
group ``speed``.)
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from timing_fixtures import decode_frame_numbers, make_frame_counter  # noqa: E402

from video_ai_editor import platformutil as _pu  # noqa: E402
from video_ai_editor.edl import speed_curve as sc  # noqa: E402
from video_ai_editor.edl import timebase as tb  # noqa: E402
from video_ai_editor.edl.schema import EDL, RENDER_BEHAVIOR_VERSION, Canvas, Clip, empty_edl  # noqa: E402
from video_ai_editor.render import compositor, render_preview, speed_audio  # noqa: E402
from video_ai_editor.render.chunks import fingerprint_clip  # noqa: E402
from video_ai_editor.render.frame_map import (  # noqa: E402
    SourceInfo, build_program_map, freeze_frame, freeze_in_for,
)

SR = 48000
GOLDENS = Path(__file__).resolve().parent / "goldens" / "frame_map"


def _ff(args: list[str]) -> None:
    subprocess.run([_pu.FFMPEG, "-y", "-v", "error", *args], check=True, capture_output=True)


# ---------------------------------------------------------------- schema

def test_a_curve_is_normalised_on_validation():
    c = Clip(src="a.mp4", out=4.0, speed={"curve": [[1.2, 30], [0.5, 0.01], [0.5, 2.0]],
                                          "name": "  my ramp ", "junk": 1})
    # sorted, clamped (x to [0,1], r to 0.1-10), last of a duplicated x wins,
    # the start pinned flat, the name kept and everything else dropped.
    assert c.speed == {"curve": [[0.0, 2.0], [0.5, 2.0], [1.0, 10.0]], "name": "my ramp"}
    assert Clip(src="a", out=1, speed={"curve": [[0.3, 2]]}).speed == {"curve": [[0.0, 2.0], [0.3, 2.0], [1.0, 2.0]]}
    # no usable curve = what it always rendered as: 1x
    assert Clip(src="a", out=1, speed={"curve": []}).speed is None
    assert Clip(src="a", out=1, speed={"points": [[0, 2]]}).speed is None
    with pytest.raises(ValueError):
        Clip(src="a", out=1, speed={"curve": [[0, float("nan")], [1, 1]]})
    # assignment validates too (the dispatch handlers assign)
    c.speed = {"curve": [[0, 0.5], [1, 0.5]]}
    assert c.speed == {"curve": [[0.0, 0.5], [1.0, 0.5]]}
    assert len(Clip(src="a", out=1, speed={"curve": [[i / 99, 1 + i % 3] for i in range(100)]}).speed["curve"]) \
        == sc.MAX_CURVE_POINTS


@pytest.mark.parametrize("name", sorted(sc.CURVE_PRESETS))
def test_the_footprint_is_the_curves_integral(name):
    pts = sc.CURVE_PRESETS[name]
    c = Clip(src="a", **{"in": 2.0}, out=8.0, speed={"curve": pts})
    mean = sc.mean_speed([tuple(p) for p in pts])
    assert c.effective_duration == pytest.approx(6.0 / mean, rel=1e-12)
    assert c.speed_factor == pytest.approx(mean)
    # the integral, numerically: ∫ dt over the output = S when the speed is
    # integrated along it
    cm = sc.curve_map(c.speed_curve, c.duration)
    ts = np.linspace(0, cm.D, 200001)
    rs = np.array([sc.speed_at(cm, t) for t in ts[::100]])
    assert np.trapezoid(rs, ts[::100]) == pytest.approx(6.0, rel=1e-4)
    # the frame count goes through the timebase like any speed
    for fps in (Fraction(24000, 1001), 25, Fraction(30000, 1001), 30, Fraction(60000, 1001)):
        assert compositor.clip_frames(c, fps) == max(1, tb.frame_of(c.effective_duration, fps))
    # source ↔ timeline offsets are each other's inverse and exact at the ends
    assert c.source_offset_at(c.effective_duration) == pytest.approx(6.0, abs=1e-9)
    for s in (0.0, 0.7, 2.5, 5.99):
        assert c.source_offset_at(c.timeline_offset_at(s)) == pytest.approx(s, abs=1e-9)


def test_split_curve_keeps_the_footprint_and_the_shape():
    pts = [tuple(p) for p in sc.CURVE_PRESETS["bullet"]]
    S, u = 6.0, 0.37
    frac = sc.integral_fraction(pts, u)
    left, right = sc.split_curve(pts, u)
    a = Clip(src="a", out=S * frac, speed={"curve": left})
    b = Clip(src="a", **{"in": S * frac}, out=S, speed={"curve": right})
    whole = Clip(src="a", out=S, speed={"curve": [list(p) for p in pts]})
    assert a.effective_duration == pytest.approx(u * whole.effective_duration, rel=1e-9)
    assert a.effective_duration + b.effective_duration == pytest.approx(whole.effective_duration, rel=1e-9)


def test_a_freeze_is_a_still_and_unset_is_omitted():
    c = Clip(src="a", **{"in": 1.0}, out=1.04, speed=2.0, reverse=True, freeze=3.0)
    assert (c.speed, c.reverse, c.effective_duration) == (None, False, 3.0)
    c.speed = 1.5                         # a still has no speed, on assignment too
    assert c.speed is None and c.source_offset_at(2.0) == 0.0
    assert Clip(src="a", out=1, freeze=0).freeze is None
    assert Clip(src="a", out=1, freeze=10 ** 9).freeze == 6 * 3600.0
    with pytest.raises(ValueError):
        Clip(src="a", out=1, freeze=float("inf"))
    # unset → not in the JSON at all; set → round-trips
    assert "freeze" not in json.loads(Clip(src="a", out=1).model_dump_json(by_alias=True))
    assert json.loads(c.model_dump_json(by_alias=True))["freeze"] == 3.0


def test_an_edl_without_the_new_fields_serialises_byte_for_byte_as_before():
    """Every golden EDL was written before curves rendered and before
    `freeze` existed (the rates group aside, whose curve row's `speed` was
    already a dict): loading and dumping it gives back the same JSON, so its
    identity changes only by the RENDER_BEHAVIOR_VERSION salt."""
    n = 0
    for p in sorted(GOLDENS.glob("*.json")):
        for case in json.loads(p.read_text())["cases"]:
            d = case["edl"]
            again = EDL.model_validate(d).model_dump(by_alias=True, mode="json")
            assert again == d, case["name"]
            n += 1
    assert n > 100
    assert RENDER_BEHAVIOR_VERSION == 18


_RATES = [Fraction(24000, 1001), 24, 25, Fraction(30000, 1001), 30, 50, Fraction(60000, 1001), 60]


def test_freeze_in_for_reaches_every_frame_a_playhead_can_show():
    """A freeze-frame op (lane S2) turns the frame under the playhead into
    an `in`: for every (source, project) rate pair and every frame a 1x
    chain can show, `freeze_in_for` finds an `in` whose freeze holds it."""
    for S in _RATES:
        src = SourceInfo.cfr(S, 900)
        for R in _RATES:
            shown = set(build_program_map(_edl([Clip(src="s", out=29.0, id="x")], fps=float(R)),
                                          {"s": src}, fps=R).frame)
            for f in sorted(shown)[:40] + [450, 899]:
                assert freeze_frame(src, in_=freeze_in_for(src, f, R), fps=R) == f, (S, R, f)


def test_the_chunk_key_moves_with_a_freeze_and_only_then(tmp_path):
    kw = dict(canvas_w=320, canvas_h=180, fps=30, encoder_args=["x"])
    a = Clip(src=str(tmp_path / "x.mp4"), out=1.0)
    b = a.model_copy(deep=True)
    assert fingerprint_clip(a, **kw) == fingerprint_clip(b, **kw)
    b.freeze = 2.0
    assert fingerprint_clip(a, **kw) != fingerprint_clip(b, **kw)


def test_a_vae_round_trip_keeps_curves_and_freezes(tmp_path, monkeypatch):
    from video_ai_editor import storage as _storage, storage_project as _sp
    from video_ai_editor.edl import EDLStore
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path / "wd")
    monkeypatch.setattr(_sp, "session_dir", lambda sid: tmp_path / "wd" / sid)
    sd = tmp_path / "wd" / "s1"
    sd.mkdir(parents=True)
    src = tmp_path / "src.mp4"
    _ff(["-f", "lavfi", "-i", "color=c=blue:s=320x180:d=3:r=30", "-f", "lavfi", "-i",
         "sine=f=440:duration=3", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src)])
    e = empty_edl(Canvas(w=320, h=180, fps=30))
    curve = {"curve": sc.CURVE_PRESETS["hero"], "name": "hero"}
    e.get_track("v1").clips += [
        Clip(src=str(src), out=2.0, start=0.0, speed=curve, id="cv"),
        Clip(src=str(src), **{"in": 1.0}, out=1.0 + 1 / 30, start=2.0, freeze=1.5, id="fz"),
    ]
    e.recompute_duration()
    (sd / "edl.json").write_text(e.model_dump_json(by_alias=True))
    before = EDLStore(sd).edl
    dst = tmp_path / "p.vae"
    _sp.save_project("s1", dst)
    new = EDLStore(tmp_path / "wd" / _sp.load_project(dst)).edl
    cv, fz = (new.get_clip("cv")[1], new.get_clip("fz")[1])
    assert cv.speed == before.get_clip("cv")[1].speed and cv.speed["name"] == "hero"
    assert fz.freeze == 1.5 and fz.effective_duration == 1.5
    assert new.duration == pytest.approx(before.duration)
    # load_project's validation ran the same model: a reload is stable
    assert EDL.model_validate_json(new.to_json()).to_json() == new.to_json()


# ---------------------------------------------------------------- the picture's setpts

@pytest.mark.parametrize("rate,timescale", [("30000/1001", None), ("25", "90000"), ("30", None)])
def test_ffmpeg_evaluates_the_curve_setpts_to_the_models_ticks(tmp_path, rate, timescale):
    """`setpts_expr` run by ffmpeg (showinfo pts after it) == the model's
    double-exact evaluation, frame for frame, on three stream time bases."""
    src = tmp_path / "s.mp4"
    args = ["-f", "lavfi", "-i", f"testsrc2=s=64x36:r={rate}", "-frames:v", "240",
            "-c:v", "libx264", "-g", "30"]
    if timescale:
        args += ["-video_track_timescale", timescale]
    _ff(args + [str(src)])
    r = Fraction(rate)
    for name in ("ramp_up", "bullet", "montage"):
        cm = sc.curve_map([tuple(p) for p in sc.CURVE_PRESETS[name]], 5.3)
        vf = f"setpts=PTS-STARTPTS,setpts={sc.setpts_expr(cm)},showinfo"
        p = subprocess.run([_pu.FFMPEG, "-v", "info", "-i", str(src), "-vf", vf, "-f", "null", "-"],
                           capture_output=True, text=True)
        assert p.returncode == 0, p.stderr[-500:]
        got = [int(x) for x in re.findall(r"pts:\s*(-?\d+)", p.stderr)]
        m = re.search(r"config in time_base: (\d+)/(\d+)", p.stderr)
        tbase = Fraction(int(m.group(1)), int(m.group(2)))
        TB = tbase.numerator / tbase.denominator
        step = (1 / r) / tbase
        want = [int(sc.out_seconds(cm, float(round(i * step)) * TB) / TB) for i in range(len(got))]
        assert len(got) == 240 and got == want, name


# ---------------------------------------------------------------- sound helpers

def _click_source(path: Path, *, every: float = 0.25, seconds: float = 10.0) -> Path:
    """Black picture + a 4 ms 1 kHz click every `every` seconds (PCM, so the
    source itself adds no codec smear)."""
    _ff(["-f", "lavfi", "-i", f"color=c=black:s=160x90:r=30:d={seconds}", "-f", "lavfi", "-i",
         f"aevalsrc='if(lt(mod(t\\,{every})\\,0.004)\\,0.8*sin(2*PI*1000*t)\\,0)'"
         f":s=48000:d={seconds}:c=stereo",
         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "pcm_s16le",
         str(path)])
    return path


def _tone_source(path: Path, *, seconds: float = 10.0) -> Path:
    # float PCM: 16-bit quantisation noise would dominate the curvature bound
    _ff(["-f", "lavfi", "-i", f"color=c=gray:s=160x90:r=30:d={seconds}", "-f", "lavfi", "-i",
         f"aevalsrc='0.5*sin(2*PI*440*t)':s=48000:d={seconds}:c=stereo",
         "-pix_fmt", "yuv420p", "-c:v", "libx264",
         "-preset", "ultrafast", "-c:a", "pcm_f32le", str(path)])
    return path


def _edl(clips: list[Clip], fps=30) -> EDL:
    e = empty_edl(Canvas(w=160, h=90, fps=fps))
    e.canvas.loudness_lufs = None
    e.get_track("v1").clips += clips
    e.recompute_duration()
    return e


def _sound_of(edl: EDL, dst: Path, fps=30) -> np.ndarray:
    """The timeline's sound as PCM (mono f32) through the RENDER's own audio
    graph (`_audio_only_graph`: the same per-clip chains as preview and
    export), with no codec in the way."""
    speed_audio.prepare(edl, dst.parent / "cache", fps)
    inputs, fc, label = compositor._audio_only_graph(edl, fps=fps, first_input=0,
                                                     apply_loudnorm=False)
    _ff([*inputs, "-filter_complex", fc, "-map", label, "-c:a", "pcm_f32le", str(dst)])
    return _decode(dst)


def _decode(path: Path) -> np.ndarray:
    raw = subprocess.run([_pu.FFMPEG, "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1",
                          "-ar", str(SR), "-f", "f32le", "-"], check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def _onsets(a: np.ndarray, *, thresh: float = 0.2, dead_s: float = 0.08) -> list[float]:
    hits = np.flatnonzero(np.abs(a) > thresh)
    out: list[float] = []
    last = -10 ** 9
    for i in hits:
        if i - last > dead_s * SR:
            out.append(i / SR)
        last = i
    return out


_RAMP = {"curve": [[0.0, 0.5], [0.45, 2.2], [0.7, 1.0], [1.0, 0.6]]}


def _expected_clicks(clip: Clip, every: float) -> list[float]:
    k0 = int(np.ceil(clip.in_ / every - 1e-9))
    ts = [k * every for k in range(k0, int(clip.out / every) + 1) if clip.in_ <= k * every < clip.out]
    return [clip.start + clip.timeline_offset_at(t - clip.in_) for t in ts]


# ---------------------------------------------------------------- sound

_SLOW = {"curve": [[0.0, 0.1], [0.5, 0.1], [1.0, 1.0]]}


@pytest.mark.parametrize("curve,out,keep_pitch,tol_ms", [
    (_RAMP, 6.5, False, 0.5), (_RAMP, 6.5, True, 8.0),
    (_SLOW, 2.4, False, 0.5), (_SLOW, 2.4, True, 12.0),
])
def test_the_sound_follows_the_curve(tmp_path, curve, out, keep_pitch, tol_ms):
    """Every click lands where the picture's curve puts its source instant:
    varispeed to a fraction of a millisecond, pitch-kept WSOLA within 8 ms
    up to 2.2x and 12 ms at 0.1x (a 59.94 frame is 16.7 ms; constant-speed
    atempo is allowed 20). Measured quiet: 0.06 / 6.2 ms and 0.3 / 9.6 ms."""
    src = _click_source(tmp_path / "clicks.mov")
    clip = Clip(src=str(src), **{"in": 0.5}, out=out, start=0.0, speed=curve, id="c")
    clip.audio.keep_pitch = keep_pitch
    got = _onsets(_sound_of(_edl([clip]), tmp_path / "out.wav"))
    want = _expected_clicks(clip, 0.25)
    # Every click found is a click the curve places, within the bound...
    err = [min(((g - w) * 1000 for w in want), key=abs) for g in got]
    assert max(abs(e) for e in err) <= tol_ms, [round(e, 2) for e in err]
    # ...and every click is found — except, pitch kept, where the curve runs
    # faster than 1.8x: WSOLA (like atempo) may skip a 4 ms transient there.
    cm = sc.curve_map(clip.speed_curve, clip.duration)
    need = [w for w in want if not keep_pitch or sc.speed_at(cm, w - clip.start) <= 1.8]
    missing = [w for w in need if min(abs(g - w) for g in got) * 1000 > tol_ms]
    assert not missing and len(need) >= len(want) * 2 // 3, (missing, len(need), len(want))
    # The test discriminates: a linear (mean-speed) mapping misses by frames.
    lin = [clip.start + (w_src - clip.in_) / clip.speed_factor
           for w_src in [k * 0.25 for k in range(2, 2 + len(want))]]
    assert max(abs(a - b) for a, b in zip(want, lin)) > 1 / 30


def test_a_curve_on_a_music_lane_follows_it(tmp_path):
    """The audio lanes share the rule (QA-086): a curve on a music clip is
    read from its intermediate, placed at its start and cut to its
    footprint — every click where the curve puts it, nothing after the end."""
    bed = tmp_path / "bed.wav"
    _ff(["-f", "lavfi", "-i", "aevalsrc='if(lt(mod(t\\,0.25)\\,0.004)\\,0.8*sin(2*PI*1000*t)\\,0)'"
         ":s=48000:d=8:c=stereo", "-c:a", "pcm_s16le", str(bed)])
    pic = tmp_path / "black.mov"
    _ff(["-f", "lavfi", "-i", "color=c=black:s=160x90:d=12:r=30", "-f", "lavfi", "-i",
         "anullsrc=r=48000:cl=stereo", "-t", "12", "-pix_fmt", "yuv420p", "-c:v", "libx264",
         "-preset", "ultrafast", "-c:a", "pcm_s16le", str(pic)])
    e = _edl([Clip(src=str(pic), out=12.0, id="v")])
    m = Clip(src=str(bed), **{"in": 0.6}, out=6.1, start=1.0, speed=_RAMP, id="mu")
    m.audio.keep_pitch = False
    e.get_track("music").clips.append(m)
    e.recompute_duration()
    a = _sound_of(e, tmp_path / "mix.wav")
    got = _onsets(a)
    want = _expected_clicks(m, 0.25)
    assert len(got) == len(want), (got, want)
    assert max(abs(g - w) for g, w in zip(got, want)) * 1000 <= 1.0
    end = m.start + m.effective_duration
    assert np.abs(a[int((end + 0.01) * SR):int((end + 1.0) * SR)]).max() < 1e-4


@pytest.mark.parametrize("keep_pitch", [False, True])
def test_a_tone_has_no_click_at_the_curves_piece_joins(tmp_path, keep_pitch):
    """A 440 Hz tone through the hero curve (four joins): the sample-to-
    sample curvature never exceeds what a clean sinusoid at the local
    frequency has — a click is a discontinuity, which this bound catches
    from half a percent of the tone's level."""
    src = _tone_source(tmp_path / "tone.mov")
    clip = Clip(src=str(src), **{"in": 1.0}, out=7.0, start=0.0,
                speed={"curve": sc.CURVE_PRESETS["hero"]}, id="t")
    clip.audio.keep_pitch = keep_pitch
    y = _sound_of(_edl([clip]), tmp_path / "tone.wav")
    n = int(clip.effective_duration * SR)
    body = y[int(0.03 * SR):n - int(0.03 * SR)]
    d2 = np.abs(np.diff(body, 2))
    amp = float(np.sqrt(2 * np.mean(body ** 2)))          # the tone's peak (0.5)
    rmax = 1.0 if keep_pitch else max(p[1] for p in clip.speed_curve)
    bound = amp * (2 * np.pi * 440 * rmax / SR) ** 2
    assert d2.max() <= 1.25 * bound, (float(d2.max()), bound)
    # A step of half a percent of the tone's peak (a click at a join) would
    # already break that bound: its curvature is the step itself.
    assert 0.005 * amp > 1.25 * bound
    # and it is a real tone all the way through (no dropout at a join)
    win = int(0.02 * SR)
    rms = np.sqrt(np.convolve(body ** 2, np.ones(win) / win, mode="valid"))
    assert rms.min() > 0.8 * np.median(rms), (float(rms.min()), float(np.median(rms)))


def test_a_freeze_is_silent_between_sounding_neighbours_on_export_and_preview(tmp_path):
    """A (tone) | freeze of A's last frame for 1 s | B (tone): the render holds
    the frame the model says for exactly the plan's frames, and the freeze's
    sound is digital silence — on the single-pass export AND on the chunked
    preview."""
    counter = make_frame_counter(tmp_path / "fc.mp4", frames=300, fps=30)
    info = SourceInfo.cfr(30, 300)
    a = Clip(src=str(counter), **{"in": 1.0}, out=2.0, start=0.0, id="a")
    held = build_program_map(_edl([a]), {str(counter): info}).frame[-1]
    fin = freeze_in_for(info, held, 30)
    fz = Clip(src=str(counter), **{"in": fin}, out=fin + 1 / 30, start=1.0, freeze=1.0, id="fz")
    b = Clip(src=str(counter), **{"in": 5.0}, out=6.0, start=2.0, id="b")
    edl = _edl([a, fz, b])
    assert freeze_frame(info, in_=fz.in_, fps=30) == held == 59
    exp = compositor._render(edl, tmp_path / "export.mp4", height=90, fps=30, preview=False,
                             cache_dir=tmp_path / "cache", chunked=False)
    prev = render_preview(edl, tmp_path / "sess").path
    for label, path in (("export", exp), ("preview", prev)):
        frames = decode_frame_numbers(path)
        assert frames == list(range(30, 60)) + [59] * 30 + list(range(150, 180)), label
        s = _decode(path)
        assert np.sqrt(np.mean(s[int(1.05 * SR):int(1.95 * SR)] ** 2)) < 1e-4, label   # < −80 dBFS
        assert np.sqrt(np.mean(s[int(0.2 * SR):int(0.9 * SR)] ** 2)) > 0.05, label
        assert np.sqrt(np.mean(s[int(2.1 * SR):int(2.9 * SR)] ** 2)) > 0.05, label


def test_a_curve_clip_renders_its_plan_frames_and_samples(tmp_path):
    """The rendered file is exactly the plan long: clip_frames frames and
    samples_for_frames(clip_frames) samples (29.97, an off-grid footprint),
    through export and preview, with the curve's sound in it."""
    src = _click_source(tmp_path / "clicks.mov")
    clip = Clip(src=str(src), **{"in": 0.3}, out=5.3, start=0.0,
                speed={"curve": sc.CURVE_PRESETS["montage"]}, id="m")
    # Varispeed keeps every 4 ms click even at 3x (pitch-kept WSOLA, like
    # atempo, may skip a transient above 2x — that is time-stretching).
    clip.audio.keep_pitch = False
    fps = Fraction(30000, 1001)
    edl = _edl([clip], fps=tb.fps_float(fps))
    n = compositor.clip_frames(clip, fps)
    assert n == tb.frame_of(clip.effective_duration, fps)
    exp = compositor._render(edl, tmp_path / "e.mp4", height=90, fps=edl.canvas.fps,
                             preview=False, cache_dir=tmp_path / "cache", chunked=False)
    prev = render_preview(edl, tmp_path / "sess").path
    for path in (exp, prev):
        out = subprocess.run([_pu.FFPROBE, "-v", "error", "-count_frames", "-select_streams", "v:0",
                              "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, check=True).stdout
        assert int(out.strip()) == n
        s = _decode(path)
        assert abs(len(s) - tb.samples_for_frames(n, fps)) <= 2048   # AAC's framing
        got = _onsets(s, dead_s=0.05)
        want = _expected_clicks(clip, 0.25)
        assert len(got) == len(want), (got, want)
        # AAC (the delivered file) smears a 4 ms burst by well under 2 ms
        assert max(abs(g - w) for g, w in zip(got, want)) < 0.002


def test_the_intermediate_is_cached_and_keyed_on_what_it_holds(tmp_path):
    src = _click_source(tmp_path / "c.mov", seconds=3)
    c = Clip(src=str(src), out=2.0, speed={"curve": sc.CURVE_PRESETS["ramp_up"]})
    p1 = speed_audio.ensure(c, 30, tmp_path / "cache")
    assert p1.parent == tmp_path / "cache" / "speed_audio" and p1.name.startswith("sa_")
    ino = p1.stat().st_ino
    assert speed_audio.ensure(c, 30, tmp_path / "cache").stat().st_ino == ino     # a hit
    c2 = c.model_copy(deep=True)
    c2.audio.keep_pitch = False
    assert speed_audio.key(c2, 30) != speed_audio.key(c, 30)
    assert speed_audio.key(c, 25) != speed_audio.key(c, 30)
    # the graph names it through amovie, a filter SOURCE (input indices untouched)
    chain = compositor._build_clip_audio_chain(c, input_label="[3:a]", label_out="[a3]", fps=30)
    assert chain.startswith("amovie=filename=") and "[3:a]" not in chain
    # a render cache like the reversed intermediates: counted and evictable
    from video_ai_editor.render import cache_budget
    assert p1 in {Path(e.path) for e in cache_budget.entries(tmp_path)}


def test_positions_are_the_curves_integral():
    cm = sc.curve_map([tuple(p) for p in sc.CURVE_PRESETS["bullet"]], 4.0)
    n = int(cm.D * SR) + 10
    pos, rate = speed_audio.positions(cm, n)
    for j in (0, 1, 777, n // 3, n // 2, n - 11, n - 1):
        assert pos[j] == pytest.approx(sc.source_seconds(cm, j / SR) * SR, abs=1e-6)
        assert rate[j] == pytest.approx(sc.speed_at(cm, j / SR), abs=1e-9)
    assert np.all(np.diff(pos) > 0)
