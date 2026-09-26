"""Fixtures for the instant-preview SOUND suites (INSTANT_PREVIEW_SPEC §13
P1-A1, P1-A2; tests/wk/test_wk_audio.py).

Everything is real: sources are synthesized by ffmpeg, their proxy sound is
built by ``ingest/proxy.build_audio`` (the FLAC chunks the app serves), and
the reference is the server's own ``compositor._audio_only_graph`` render —
the preview's sound, sample for sample — of the same EDL.

Sources:

* **counter** — PCM whose every sample SAYS which sample it is (left = low
  15 bits, right = high 15 bits of the index), so a client render can be
  decoded back to "output sample p plays source sample s" exactly;
* **click** — one full-scale-ish sample at the first sample of every source
  frame (`samples_for_frames(i)`), nothing else;
* **tone** — AAC (as ingest normalises every import): a different tone on
  each channel plus a short tick every 0.5 s, for the mix parity case.
"""
from __future__ import annotations

import contextlib
import json
import subprocess
from fractions import Fraction
from pathlib import Path

import numpy as np

from video_ai_editor.edl import timebase as tb
from video_ai_editor.edl.schema import (
    AudioProps, Canvas, Clip, Keyframe, MusicDuck, Transition, empty_edl,
)

SR = 48000
SILENCE = -1


# ------------------------------------------------------------------ sources

def _run(argv: list[str]) -> None:
    subprocess.run(argv, check=True, capture_output=True)


def counter_source(path: Path, rate: Fraction, seconds: float) -> Path:
    if not path.exists():
        _run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
              f"color=c=gray:s=64x36:r={tb.ffmpeg_rate(rate)}:d={seconds}",
              "-f", "lavfi", "-i",
              ("aevalsrc=exprs='(mod(n\\,32768)-16384)/32768|"
               f"(mod(floor(n/32768)\\,32768)-16384)/32768':s={SR}:d={seconds}"),
              "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "pcm_s16le", "-shortest", str(path)])
    return path


def click_source(path: Path, rate: Fraction, seconds: float) -> Path:
    """A 0.5 click at every source frame's first sample, samples_for_frames(i)
    (no rate here makes that a tie, so ffmpeg's round() agrees), silence
    elsewhere: n is a click when round(round(n·rate/48000)·48000/rate) == n."""
    if not path.exists():
        num, den = rate.numerator, rate.denominator
        expr = (f"0.5*eq(n\\,round(round(n*{num}/({SR}*{den}))*{SR}*{den}/{num}))")
        _run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
              f"color=c=gray:s=64x36:r={tb.ffmpeg_rate(rate)}:d={seconds}",
              "-f", "lavfi", "-i", f"aevalsrc=exprs='{expr}|{expr}':s={SR}:d={seconds}",
              "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "pcm_s16le", "-shortest", str(path)])
    return path


def tone_source(path: Path, seconds: float, lf: float, rf: float, *, video: bool = True,
                amp: float = 0.2) -> Path:
    """AAC in mp4 (the import format): lf Hz left, rf Hz right, a 2 ms tick
    every 0.5 s on both (alignment for the cross-correlation)."""
    if not path.exists():
        tick = "0.3*lt(mod(t\\,0.5)\\,0.002)*sin(2*PI*3000*t)"
        a = [ "-f", "lavfi", "-i",
              f"aevalsrc=exprs='{amp}*sin(2*PI*{lf}*t)+{tick}|{amp}*sin(2*PI*{rf}*t)+{tick}':s={SR}:d={seconds}"]
        v = ["-f", "lavfi", "-i", f"color=c=gray:s=64x36:r=30:d={seconds}"] if video else []
        _run(["ffmpeg", "-v", "error", "-y", *v, *a,
              *(["-c:v", "libx264", "-preset", "ultrafast"] if video else []),
              "-c:a", "aac", "-b:a", "192k", "-shortest", str(path)])
    return path


def lossless_twin(src: Path, dst: Path) -> Path:
    """`src` with its sound decoded once, from the start, to float PCM (the
    picture copied): the samples the proxy FLAC holds, in a container the
    export can seek sample-exactly. The server render of a timeline over
    twins is the reference WITHOUT ffmpeg's AAC seek artefact (the first
    ~650 samples after an input `-ss` into AAC decode wrong)."""
    if not dst.exists():
        _run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0", "-c:v", "copy",
              "-c:a", "pcm_f32le", str(dst)])
    return dst


def with_paths(edl, paths: dict[str, str]):
    out = edl.model_copy(deep=True)
    for t in out.tracks:
        for c in t.clips:
            if isinstance(c, Clip) and c.src in paths:
                c.src = paths[c.src]
    return out


def decode_counter(x: np.ndarray) -> np.ndarray:
    """(n, 2) float → source sample index per row (SILENCE where silent)."""
    lo = np.round(x[:, 0].astype(np.float64) * 32768 + 16384).astype(np.int64)
    hi = np.round(x[:, 1].astype(np.float64) * 32768 + 16384).astype(np.int64)
    n = hi * 32768 + lo
    n[(np.abs(x[:, 0]) < 1e-9) & (np.abs(x[:, 1]) < 1e-9)] = SILENCE
    return n


# ------------------------------------------------------------------ proxies

@contextlib.contextmanager
def workdir(wd: Path):
    from video_ai_editor import storage as _storage
    old = _storage.WORKDIR
    _storage.WORKDIR = wd
    try:
        yield wd
    finally:
        _storage.WORKDIR = old


def build_proxy_audio(src: Path, wd: Path) -> tuple[str, dict, dict]:
    """(key, index.json, frame_map SourceInfo json) — the FLAC chunks built by
    the real `proxy.build_audio`."""
    from video_ai_editor.ingest import proxy as P
    from video_ai_editor.render.frame_map import SourceInfo
    with workdir(wd):
        key = P.proxy_key(src)
        info = P.probe_source(src, key)
        P.save_source(info)
        P.build_audio(info, low_priority=False)
        idx = P.read_index(key)
    fm = (SourceInfo.from_proxy(info) if info.pts else SourceInfo.cfr(Fraction(30), 1, width=64, height=36))
    return key, idx, fm.to_json()


def decode_master_f32(src: Path) -> np.ndarray:
    """ffmpeg's decode of the master exactly as build_audio does it."""
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(src), "-map", "0:a:0", "-vn",
                          "-af", f"aresample={SR},aformat=sample_fmts=flt:channel_layouts=stereo",
                          "-f", "f32le", "pipe:1"], check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype="<f4").reshape(-1, 2)


def decode_chunks_f32(wd: Path, key: str, idx: dict) -> np.ndarray:
    """ffmpeg's decode of the FLAC chunks, multiplied back by their gains."""
    a = idx["audio"]
    parts = []
    for n in range(a["chunks"]):
        path = wd / "proxies" / key / "a" / f"{n:04d}.flac"
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "2", "-"],
                             check=True, capture_output=True).stdout
        x = np.frombuffer(raw, dtype="<f4").reshape(-1, 2) * np.float32(a["chunk_gain"].get(str(n), 1.0))
        parts.append(x)
    return np.concatenate(parts)


# ------------------------------------------------------------------ server render

def server_render(edl, fps, cache: Path) -> np.ndarray:
    """The preview's sound of `edl` (the `_remux_with_new_audio` graph:
    solo applied, reversed sources substituted, no mastering), (n, 2) f32."""
    from video_ai_editor.render import compositor
    from video_ai_editor.render.audio_mix import apply_solo
    from video_ai_editor.render.reverse import with_reversed_sources
    sub = with_reversed_sources(apply_solo(edl), cache, fps)
    inputs, fc, label = compositor._audio_only_graph(sub, fps=fps, first_input=0, apply_loudnorm=False)
    raw = subprocess.run(["ffmpeg", "-v", "error", *inputs, "-filter_complex", fc, "-map", label,
                          "-f", "f32le", "-ac", "2", "-ar", str(SR), "-"],
                         check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype="<f4").reshape(-1, 2)


def edl_json(edl) -> dict:
    return edl.model_dump(by_alias=True, mode="json")


def write_case(root: Path, name: str, edl, sources: dict[str, tuple[str, dict]], rng: tuple[int, int],
               loudness_gain_db: float | None = None) -> Path:
    d = root / "cases"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.json"
    path.write_text(json.dumps({
        "name": name, "edl": edl_json(edl), "range": list(rng), "loudness_gain_db": loudness_gain_db,
        "sources": {src: {"key": key, "info": info} for src, (key, info) in sources.items()},
    }))
    return path


# ------------------------------------------------------------------ EDLs

def _clip(src: str, cid: str, start: float, in_: float, out: float, **kw) -> Clip:
    audio = kw.pop("audio", None)
    c = Clip(src=src, start=start, id=cid, **kw)
    c.in_, c.out = in_, out
    if audio:
        c.audio = AudioProps(**audio)
    return c


def placement_edl(counter: str, click: str, rate: Fraction):
    """P1-A1: cuts on and off the frame grid, a gap, a reversed clip, the
    click source on grid — no gains, no seams: every sample is exact."""
    fps = int(rate) if rate.denominator == 1 else tb.fps_float(rate)
    e = empty_edl(Canvas(w=64, h=36, fps=fps))
    e.canvas.loudness_lufs = None
    v1 = e.get_track("v1")
    cursor = 0

    def add(src, cid, in_, dur_frames, **kw):
        nonlocal cursor
        c = _clip(src, cid, tb.time_of(cursor, fps), in_, in_ + tb.time_of(dur_frames, fps), **kw)
        v1.clips.append(c)
        cursor += dur_frames

    add(counter, "c0", tb.time_of(10, fps), 20)
    add(click, "k0", tb.time_of(3, fps), 17)
    add(counter, "c1", tb.time_of(95, fps) + 0.3 / float(rate), 13)          # off grid (+0.3 frame)
    add(counter, "c2", tb.time_of(40, fps) - 0.5 / float(rate), 11)          # off grid (−0.5 frame)
    cursor += 7                                                              # a gap
    add(counter, "r0", tb.time_of(20, fps), 23, reverse=True)
    add(click, "k1", tb.time_of(50, fps), 29)
    add(counter, "c3", 1.0, 16)
    e.recompute_duration()
    return e, fps


def ripple_delete(edl, clip_id: str, fps):
    """`edl` with v1 clip `clip_id` removed and every later v1 clip moved
    left by its length (what a ripple delete dispatches)."""
    out = edl.model_copy(deep=True)
    v1 = out.get_track("v1")
    gone = next(c for c in v1.clips if c.id == clip_id)
    width = gone.effective_duration
    v1.clips = [c for c in v1.clips if c.id != clip_id]
    for c in v1.clips:
        if c.start > gone.start:
            c.start = tb.quantize(c.start - width, fps)
    out.recompute_duration()
    return out


def mix_edl(tone_a: str, tone_b: str, bed: str, voice: str, *, solo: str | None = None,
            duck: bool = False, loudness: float | None = None):
    """P1-A2: cuts, gain, gain_env, fades, mute, channel modes, a seam
    (acrossfade), a varispeed clip, a music bed with fades and gain, a
    voice-over and an audio-lane clip."""
    e = empty_edl(Canvas(w=64, h=36, fps=30))
    e.canvas.loudness_lufs = loudness
    v1 = e.get_track("v1")
    v1.clips += [
        _clip(tone_a, "a", 0.0, 0.5, 1.7, audio={"gain_db": -3.5, "fade_in": 0.3}),
        _clip(tone_b, "b", 1.2, 2.0, 2.9, audio={"channels": "left",
              "gain_env": Keyframe(keyframes=[(0.0, 0.0), (0.9, -9.0)], interp="linear")}),
        _clip(tone_a, "c", 2.1, 1.0, 2.2, audio={"channels": "right", "fade_out": 0.4, "gain_db": 2.25}),
        _clip(tone_b, "m", 3.3, 0.0, 0.5, audio={"mute": True}),
        _clip(tone_a, "d", 3.8, 3.0, 4.0, audio={"channels": "mono",
              "gain_env": Keyframe(keyframes=[(0.1, -6.0), (0.6, 0.0)], interp="ease-in-out")}),
        _clip(tone_b, "v", 4.8, 1.0, 2.0, speed=2.0, audio={"keep_pitch": False}),   # varispeed: APPROX
        _clip(tone_a, "e", 5.3, 4.0, 5.3, audio={"fade_in": 0.2, "fade_out": 0.2}),
    ]
    v1.transitions.append(Transition(at=1.2 + 0.9, duration=0.3))            # b→c acrossfade
    music = e.get_track("music")
    music.clips.append(_clip(bed, "bed", 0.4, 0.0, 5.5, audio={"gain_db": -8.0, "fade_in": 0.5, "fade_out": 0.7}))
    if duck:
        music.duck = MusicDuck(to_db=-12.0)
    e.get_track("vo").clips.append(_clip(voice, "vo1", 2.4, 0.2, 1.4, audio={"gain_db": -4.0}))
    e.get_track("a1").clips.append(_clip(voice, "a1c", 5.4, 3.0, 4.0, audio={"channels": "mono", "fade_out": 0.25}))
    if solo:
        e.get_track(solo).solo = True
    e.recompute_duration()
    return e, 30


def curve_edl(tone_a: str, tone_b: str, bed: str):
    """Speed curves (lane S1) and a freeze, APPROX in the preview: a
    varispeed curve and a pitch-kept curve on v1 (alone: a pitch-kept tone
    under another would beat differently from the varispeed preview), a
    freeze, a varispeed-curved bed."""
    e = empty_edl(Canvas(w=64, h=36, fps=30))
    e.canvas.loudness_lufs = None
    v1 = e.get_track("v1")
    ramp = {"curve": [[0.0, 0.5], [1.0, 2.0]]}
    v1.clips += [
        _clip(tone_a, "n", 0.0, 0.2, 1.2, audio={"gain_db": -2.0}),
        _clip(tone_b, "cv", 1.0, 1.0, 3.0, speed=ramp, audio={"keep_pitch": False}),   # 1.6 s
        _clip(tone_a, "fz", 2.6, 2.0, 2.2, freeze=0.8),
        _clip(tone_a, "cp", 3.4, 3.0, 4.5, speed={"curve": [[0.0, 1.5], [0.5, 0.75], [1.0, 1.5]]}),
    ]
    e.get_track("music").clips.append(_clip(bed, "mc", 0.5, 0.0, 3.0, speed=ramp,
                                            audio={"gain_db": -6.0, "keep_pitch": False}))
    e.recompute_duration()
    return e, 30
