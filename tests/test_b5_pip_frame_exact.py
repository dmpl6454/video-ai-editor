"""QA-002, the PIP half: picture-in-picture clips cut and placed by frame count.

v1 has cut by frame count since wave A; PIPs were still trimmed with float
`-ss/-t %.3f` and placed with `-itsoffset %.3f`. Measured on the old code with
a frame-counter PIP filling the canvas:

* a start off the frame grid (any legacy EDL) → frame 30 black and the whole
  PIP one frame late (61 of 91 frames wrong);
* a 29.97 project → one stray source frame at the tail;
* `-itsoffset` also broke input seeking: ffmpeg kept every frame from the
  keyframe before `-ss` and counted `-t` from there, so a PIP trimmed deep
  into a long GOP lost that much of its tail, frozen on one frame.

Every assertion decodes the exported frames (timing_fixtures) and compares
them with the frame the EDL says belongs there.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from timing_fixtures import av_offsets_ms, decode_frame_numbers, make_clap, make_frame_counter
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl import timebase as tb
from video_ai_editor.edl.schema import Canvas, Clip, Transform, empty_edl
from video_ai_editor.render import render_export

NTSC = 30000 / 1001


def _counter(path: Path, rate: str, seconds: float, gop: int) -> Path:
    lum = "if(mod(floor(N/pow(2\\,floor(X/32)))\\,2)\\,235\\,16)"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"color=c=black:s=320x180:r={rate}:d={seconds}",
         "-f", "lavfi", "-i", f"sine=f=440:sample_rate=48000:duration={seconds}",
         "-vf", f"format=gray,geq=lum='{lum}',format=yuv420p", "-c:v", "libx264",
         "-qp", "0", "-preset", "ultrafast", "-g", str(gop), "-c:a", "aac", "-shortest",
         str(path)], check=True, capture_output=True)
    return path


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("pip_media")
    return {
        "fc30": make_frame_counter(d / "fc30.mp4", frames=600, fps=30),
        "fc2997": _counter(d / "fc2997.mp4", "30000/1001", 20.02, 30),
        # One keyframe every 10 s: a trim deep into it is a real camera file.
        "longgop": _counter(d / "longgop.mp4", "30", 20.0, 300),
        "clap": make_clap(d / "clap.mp4", seconds=10, fps=30),
    }


def _export(sd: Path, fps, clips: list[tuple[float, float, float]], src: Path) -> Path:
    """A PIP-only timeline (v1 empty → black base) whose PIP fills the canvas,
    so decode_frame_numbers reads the PIP's own counter."""
    sd.mkdir(parents=True, exist_ok=True)
    e = empty_edl()
    e.canvas = Canvas(w=320, h=180, fps=fps)
    e.canvas.loudness_lufs = None
    for i, o, st in clips:
        e.get_track("v2").clips.append(Clip(
            src=str(src), in_=i, out=o, start=st,
            transform=Transform(x=160, y=90, scale=2.86)))
    e.recompute_duration()
    (sd / "edl.json").write_text(e.model_dump_json())
    s = EDLStore(sd)
    return render_export(s.edl, s.dir).path


def _expected(n_frames: int, fps, clips) -> list[int]:
    """The source frame the EDL puts at every output frame (0 = black)."""
    out = []
    for k in range(n_frames):
        val = 0
        for i, o, st in clips:
            f0, f1 = tb.frame_of(st, fps), tb.frame_of(st + (o - i), fps)
            if f0 <= k < f1:
                val = tb.frame_of(i, fps) + (k - f0)
        out.append(val)
    return out


def _mismatches(path: Path, fps, clips) -> list[tuple[int, int, int]]:
    got = decode_frame_numbers(path)
    exp = _expected(len(got), fps, clips)
    assert len(got) == tb.frame_of(max(st + o - i for i, o, st in clips), fps)
    return [(k, g, x) for k, (g, x) in enumerate(zip(got, exp)) if g != x]


def test_pip_split_on_the_grid_is_continuous(tmp_path, media):
    clips = [(0.0, 5.0, 1.0), (5.0, 10.0, 6.0)]
    assert _mismatches(_export(tmp_path / "s", 30, clips, media["fc30"]), 30, clips) == []


def test_pip_with_a_legacy_off_grid_start_lands_on_its_frame(tmp_path, media):
    """start=1.0125 is frame 30 (quantize) — the PIP must show its first
    frame there, not black then everything a frame late."""
    clips = [(1.0, 3.0, 1.0125)]
    assert _mismatches(_export(tmp_path / "s", 30, clips, media["fc30"]), 30, clips) == []


def test_pip_split_on_a_29_97_project_has_no_stray_or_early_frame(tmp_path, media):
    f = NTSC
    clips = [(0.0, tb.time_of(150, f), tb.time_of(30, f)),
             (tb.time_of(150, f), tb.time_of(300, f), tb.time_of(180, f))]
    assert _mismatches(_export(tmp_path / "s", f, clips, media["fc2997"]), f, clips) == []


def test_pip_trimmed_deep_into_a_long_gop_keeps_its_tail(tmp_path, media):
    """in_=7 s with the only earlier keyframe at 0 s: all 90 frames play."""
    clips = [(7.0, 10.0, 1.0)]
    assert _mismatches(_export(tmp_path / "s", 30, clips, media["longgop"]), 30, clips) == []


def test_pip_sound_stays_on_its_picture(tmp_path, media):
    """A clap PIP at an off-grid start: every click within 2 ms of its flash
    (the picture is frame-exact; the sound is delayed in samples to match)."""
    clips = [(0.5, 8.5, 1.0125)]
    out = _export(tmp_path / "s", 30, clips, media["clap"])
    offs = av_offsets_ms(out)
    assert len(offs) >= 7, offs
    assert max(abs(o) for o in offs) < 2.0, offs


# ---------------------------------------------------------------------------
# Wave D3, lane E2: speed, speed curves, freeze and reverse on a PIP.
#
# `render/pip.py` used to ignore `speed` entirely (and place the window by the
# SOURCE length), so an overlay could only play at 1x. It now retimes a PIP
# with v1's own rule (`pip.pip_retime`, between the rebase and the grid), so
# the frame a PIP shows at every output slot must be the frame the program
# map's per-clip model (`frame_map.clip_frame_list`, pinned against real v1
# renders by tests/goldens/frame_map) names for that clip. Every assertion
# decodes bar-coded frames from a real render.

import sys as _sys
from fractions import Fraction

_sys.path.insert(0, str(Path(__file__).parent))
import frame_map_golden_lib as G  # noqa: E402

from video_ai_editor.edl import speed_presets as SP  # noqa: E402
from video_ai_editor.render import compositor, frame_map as FM  # noqa: E402
from video_ai_editor.render.pip import pip_frames, pip_layout_end  # noqa: E402

#: One PIP of each kind, played in turn on v2 (in, speed fields).
_KINDS: list[dict] = [
    {"speed": 2.0},
    {"speed": 0.5},
    {"speed": 1.5, "keep_pitch": True},
    {"preset": "hero"},
    {"preset": "montage"},
    {"freeze": 0.8},
    {"reverse": True},
    {"reverse": True, "speed": 1.5},
    # a curve trimmed near the file's start: no input seek at all, the
    # file's own clock (speed_curve.curve_seek → 0)
    {"preset": "bullet", "in": 0.2},
    {"reverse": True, "preset": "montage"},
]


@pytest.fixture(scope="module")
def bars(tmp_path_factory):
    d = tmp_path_factory.mktemp("pip_bars")
    out = {}
    for key, sid, rate in (("s30", 1, Fraction(30)), ("s25", 2, Fraction(25))):
        p = G.make_bar_source(d / f"{key}.mp4", G.SourceSpec(key=key, sid=sid, rate=rate, seconds=6.0))
        out[key] = (str(p), sid, G.probe_source(p))
    return out


def _speed_pip_edl(bars, fps) -> tuple:
    """A black base (v1 empty) and every kind in `_KINDS` on v2, one after
    another at off-zero starts, alternating a 30 fps and a 25 fps source so
    the grid both keeps and drops source frames. Each PIP fills the canvas."""
    e = empty_edl(Canvas(w=G.W, h=G.H, fps=fps))
    e.canvas.loudness_lufs = None
    t = tb.quantize(0.4, fps)
    placed = []
    for i, k in enumerate(_KINDS):
        src, sid, info = bars["s30" if i % 2 == 0 else "s25"]
        speed = SP.resolve_speed(preset=k["preset"]) if "preset" in k else k.get("speed")
        c = Clip(src=src, start=t, id=f"p{i}", speed=speed, reverse=k.get("reverse", False),
                 transform=Transform(x=G.W / 2, y=G.H / 2, scale=2.86))
        c.in_ = k.get("in", 0.5 + 0.13 * i)
        c.out = c.in_ + 2.0
        c.audio.keep_pitch = bool(k.get("keep_pitch", False))
        if "freeze" in k:
            c.out = c.in_ + tb.frame_duration(fps)
            c.freeze = k["freeze"]
        e.get_track("v2").clips.append(c)
        placed.append((c, sid, info))
        t = tb.quantize(pip_layout_end(c) + 0.2, fps)
    e.recompute_duration()
    return e, placed


def _speed_pip_expected(e, placed, fps) -> list[int]:
    exp = [0] * tb.frame_of(e.duration, fps)
    for c, sid, info in placed:
        f0, n = pip_frames(c.start, pip_layout_end(c), fps)
        frames = FM.clip_frame_list(c, info, fps)     # v1's model of this clip
        assert len(frames) >= n
        for j in range(n):
            exp[f0 + j] = G.code_of(sid, frames[j])
    return exp


def _decoded_vs_model(path: Path, exp: list[int]) -> list[str]:
    m = G.measure(path)
    return G.compare({"top": m["top"], "bot": m["top"], "p": m["p"]},
                     {"top": exp, "bot": exp, "p": None}, check_p=False)


@pytest.mark.parametrize("rate", ["25", "30000/1001", "30"])
def test_every_speed_kind_on_a_pip_shows_the_frames_v1_would(tmp_path, bars, rate):
    """2x, 0.5x, 1.5x, the Hero and Montage curves, a freeze, a reverse and
    a reverse at 1.5x — each on a PIP, rendered in one export and decoded:
    every output frame is the frame v1's frame-selection model names, the
    PIP occupies exactly its timeline footprint (effective_duration), and
    the export is exactly as long as the EDL says."""
    fps = G.fps_value(Fraction(rate))
    e, placed = _speed_pip_edl(bars, fps)
    exp = _speed_pip_expected(e, placed, fps)
    out = compositor._render(e, tmp_path / "pip.mp4", height=G.H, fps=fps, preview=False,
                             cache_dir=tmp_path / "cache", chunked=False)
    errs = _decoded_vs_model(out, exp)
    assert errs == [], "PIP export vs v1 model:\n" + "\n".join(errs[:16])
    # The test discriminates: at 1x (the old PIP) the frames differ.
    one_x = [G.code_of(sid, f) for c, sid, info in placed
             for f in FM.clip_frame_list(c.model_copy(update={"speed": None, "freeze": None}),
                                         info, fps)[:3]]
    assert one_x != [x for x in exp if x][:len(one_x)]


def test_a_retimed_pip_through_the_real_export_entry_point(tmp_path, bars):
    """The same timeline through `render_export` (the session export path,
    chunk caches and all) at 29.97: identical frames."""
    fps = NTSC
    e, placed = _speed_pip_edl(bars, fps)
    exp = _speed_pip_expected(e, placed, fps)
    sd = tmp_path / "sess"
    sd.mkdir()
    (sd / "edl.json").write_text(e.model_dump_json())
    s = EDLStore(sd)
    out = render_export(s.edl, s.dir).path
    errs = _decoded_vs_model(out, exp)
    assert errs == [], "\n".join(errs[:16])


# ---- sound: a click track on a retimed PIP ----------------------------------

import numpy as _np  # noqa: E402

from video_ai_editor import platformutil as _pu  # noqa: E402
from video_ai_editor.render import speed_audio as _speed_audio  # noqa: E402
from video_ai_editor.render.audio_mix import KEEP_PITCH_MAX_OFFSET_MS  # noqa: E402
from video_ai_editor.render.reverse import with_reversed_sources  # noqa: E402

_SR = 48000
_EVERY = 0.25


def _click_source(path: Path, seconds: float = 8.0) -> Path:
    """Black picture + a 4 ms 1 kHz click every 0.25 s (PCM: no codec smear)."""
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=black:s=160x90:r=30:d={seconds}",
         "-f", "lavfi", "-i",
         f"aevalsrc='if(lt(mod(t\\,{_EVERY})\\,0.004)\\,0.8*sin(2*PI*1000*t)\\,0)':s=48000:d={seconds}:c=stereo",
         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "pcm_s16le",
         str(path)], check=True, capture_output=True)
    return path


def _pip_sound(e, tmp: Path, fps) -> _np.ndarray:
    """The timeline's sound through the RENDER's own audio graph (the PIP
    fold of `_audio_only_graph`, which the preview remux and the export's
    loudness pass share), decoded to mono f32 with no codec in the way."""
    e = with_reversed_sources(e, tmp / "cache", fps)
    _speed_audio.prepare(e, tmp / "cache", fps)
    inputs, fc, label = compositor._audio_only_graph(e, fps=fps, first_input=0, apply_loudnorm=False)
    dst = tmp / "sound.wav"
    subprocess.run([_pu.FFMPEG, "-y", "-v", "error", *inputs, "-filter_complex", fc, "-map", label,
                    "-c:a", "pcm_f32le", str(dst)], check=True, capture_output=True)
    return _decode_mono(dst)


def _decode_mono(path: Path) -> _np.ndarray:
    raw = subprocess.run([_pu.FFMPEG, "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1",
                          "-ar", str(_SR), "-f", "f32le", "-"], check=True, capture_output=True).stdout
    return _np.frombuffer(raw, dtype=_np.float32)


def _onsets(a: _np.ndarray, *, thresh: float = 0.2, dead_s: float = 0.08) -> list[float]:
    out: list[float] = []
    last = -10 ** 9
    for i in _np.flatnonzero(_np.abs(a) > thresh):
        if i - last > dead_s * _SR:
            out.append(i / _SR)
        last = i
    return out


def _pip_end(c: Clip, fps) -> float:
    """Where a PIP's sound stops: the end of its last FRAME (its sound is
    cut to exactly its frames' samples, v1's rule), not the float footprint."""
    f0, n = pip_frames(c.start, pip_layout_end(c), fps)
    return tb.time_of(f0 + n, fps)


def _expected_clicks(c: Clip, fps) -> list[float]:
    """Where each source click must sound: the PIP's snapped start plus the
    clip-local TIMELINE instant its source time maps to (the picture's own
    map — `timeline_offset_at`, a curve's integral), for every click that
    starts inside the PIP's frames. Reversed: mirrored."""
    t0 = tb.time_of(tb.frame_of(c.start, fps), fps)
    end = _pip_end(c, fps)
    ks = range(int(_np.ceil(c.in_ / _EVERY - 1e-9)), int((c.out + 0.2) / _EVERY) + 1)
    src = [k * _EVERY for k in ks]
    if c.reverse:
        # The intermediate holds frame_of(out - in) frames of the range,
        # backwards: source instant s lands at (span - (s - in)), and a
        # reversed 4 ms click's onset is its old END.
        span = tb.time_of(tb.frame_of(c.out - c.in_, fps), fps)
        # A click cut by the range's end starts the reversed sound at t0.
        at = [t0 + max(0.0, span - (x - c.in_) - 0.004) / c.speed_factor
              for x in src if x - c.in_ < span]
    else:
        at = [t0 + c.timeline_offset_at(x - c.in_) for x in src]
    # A click mapped right onto the last frame's end may be heard just
    # inside it (atempo's jitter): candidates run a little past the end, the
    # caller only REQUIRES those well inside.
    return sorted(t for t in at if t0 - 1e-6 <= t < end + 0.025)


_RAMP = {"curve": [[0.0, 0.5], [0.45, 2.2], [0.7, 1.0], [1.0, 0.6]]}


@pytest.mark.parametrize("fps", [25, NTSC, 30])
@pytest.mark.parametrize("speed,keep_pitch,reverse,tol_ms", [
    (2.0, False, False, 0.5),
    (0.5, False, False, 0.5),
    (1.5, True, False, KEEP_PITCH_MAX_OFFSET_MS),
    (_RAMP, False, False, 0.5),
    (_RAMP, True, False, 12.0),
    (None, False, True, 0.5),
    (1.5, False, True, 0.5),
])
def test_a_retimed_pips_clicks_land_where_its_picture_puts_them(tmp_path, fps, speed, keep_pitch,
                                                                  reverse, tol_ms):
    """Every click of a PIP's sound lands at the timeline instant its
    picture's map puts that source instant — the v1 speed rules on an
    overlay: varispeed and a reverse to a fraction of a ms, atempo within
    the keep-pitch bound, a curve's WSOLA within 12 ms — and nothing
    sounds past the PIP's footprint."""
    src = _click_source(tmp_path / "clicks.mov")
    e = empty_edl(Canvas(w=160, h=90, fps=fps))
    e.canvas.loudness_lufs = None
    c = Clip(src=str(src), start=tb.quantize(0.6, fps), id="p", speed=speed, reverse=reverse,
             transform=Transform(x=80, y=45, scale=1.0))
    # in=1.1: off the click grid, so a mirrored (reversed) grid is distinct.
    c.in_, c.out = 1.1, 5.0
    c.audio.keep_pitch = keep_pitch
    e.get_track("v2").clips.append(c)
    e.recompute_duration()
    a = _pip_sound(e, tmp_path, fps)
    got = _onsets(a)
    want = _expected_clicks(c, fps)
    end = _pip_end(c, fps)
    err = [min(((g - w) * 1000 for w in want), key=abs) for g in got]
    assert got and max(abs(x) for x in err) <= tol_ms, [round(x, 2) for x in err]
    # Every click well inside the PIP is heard (WSOLA may skip a 4 ms
    # transient above ~1.8x).
    need = [w for w in want if w < end - tol_ms / 1000 - 0.005]
    if keep_pitch and isinstance(speed, dict):
        from video_ai_editor.edl import speed_curve as sc
        cm = sc.curve_map(c.speed_curve, c.duration)
        need = [w for w in need if sc.speed_at(cm, w - c.start) <= 1.8]
    missing = [w for w in need if min(abs(g - w) for g in got) * 1000 > tol_ms]
    assert len(need) >= 8 and not missing, missing
    # Silence past the PIP's frames (a 1x-sound PIP would still be clicking).
    assert _np.abs(a[int((end + 0.005) * _SR):]).max(initial=0.0) < 1e-4
    # The test discriminates: the old 1x forward sound is somewhere else.
    t0 = tb.time_of(tb.frame_of(c.start, fps), fps)
    one_x = [t0 + (k * _EVERY - c.in_) for k in range(5, 40)
             if c.in_ <= k * _EVERY and t0 + (k * _EVERY - c.in_) < end - 0.03]
    assert any(min(abs(g - w) for g in got) > 0.01 for w in one_x) or len(one_x) != len(need)


def test_a_frozen_pip_is_silent_and_its_neighbours_sound(tmp_path):
    src = _click_source(tmp_path / "clicks.mov")
    e = empty_edl(Canvas(w=160, h=90, fps=30))
    e.canvas.loudness_lufs = None
    v2 = e.get_track("v2")
    v2.clips.append(Clip(src=str(src), start=0.0, id="a", **{"in": 1.0}, out=2.0))
    frz = Clip(src=str(src), start=1.0, id="f", **{"in": 2.0}, out=2.0 + 1 / 30)
    frz.freeze = 1.0
    v2.clips.append(frz)
    v2.clips.append(Clip(src=str(src), start=2.0, id="b", **{"in": 3.0}, out=4.0))
    e.recompute_duration()
    a = _pip_sound(e, tmp_path, 30)
    got = _onsets(a)
    assert [round(g, 3) for g in got] == [0.0, 0.25, 0.5, 0.75, 2.0, 2.25, 2.5, 2.75]


def test_a_retimed_pip_exports_its_sound_with_its_picture(tmp_path, media):
    """The delivered file (AAC, the real export): a clap PIP at 2x
    varispeed and one at 0.5x keep-pitch — every click within the speed
    rule's bound of its flash (a 1x-sound PIP drifts by whole seconds)."""
    fps = 30
    e = empty_edl(Canvas(w=320, h=180, fps=fps))
    e.canvas.loudness_lufs = None
    fast = Clip(src=str(media["clap"]), start=0.5, id="fast", speed=2.0,
                transform=Transform(x=160, y=90, scale=2.86), **{"in": 1.0}, out=6.0)
    fast.audio.keep_pitch = False
    slow = Clip(src=str(media["clap"]), start=4.0, id="slow", speed=0.5,
                transform=Transform(x=160, y=90, scale=2.86), **{"in": 6.5}, out=9.5)
    e.get_track("v2").clips += [fast, slow]
    e.recompute_duration()
    sd = tmp_path / "s"
    sd.mkdir()
    (sd / "edl.json").write_text(e.model_dump_json())
    s = EDLStore(sd)
    out = render_export(s.edl, s.dir).path
    offs = av_offsets_ms(out)
    # 5 flashes in the fast one (source 1-5: at 2x from an even frame the
    # grid keeps every flash frame), 3 in the slow one (source 7, 8, 9).
    assert len(offs) >= 8, offs
    assert max(abs(o) for o in offs) < KEEP_PITCH_MAX_OFFSET_MS + 2.0, offs
