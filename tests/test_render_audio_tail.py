"""The picture cap never cuts the sound's tail (wave E gate RX, finding 1).

Wave E (item 24) held the picture to its plan with the OUTPUT option
`-frames:v <plan>`. When the video stream reaches that count ffmpeg closes the
output before the AAC encoder has flushed its last packet, so at NTSC rates
the delivered audio ended ~10 ms before the picture (measured: a 10-clip v1
timeline at 29.97 decoded 389 samples short of `samples_for_frames(plan)`,
194 at 59.94; the audio-only remux path, which has no `-frames:v`, kept its
tail — two preview paths of one EDL differed in length). The cap is now a
`trim=end_frame=<plan>` inside the filtergraph; the sound is already bounded
by `apad`+`atrim=end_sample`, so it arrives whole.

CI round 3: the two 29.97 tests passed on ffmpeg <= 8 only because its AAC
decode returns whole 1024-sample frames (197632 for a plan of 196997). The
graph itself fed the encoder 196996 — the v1 sound is each segment's frames in
samples, rounded per segment, and this timeline's ten roundings sum one under
the plan's — and ffmpeg 9 (macOS 9.0.1, Windows 9.0.2) decodes exactly what was
fed. The render now pads the delivered sound to the plan in samples
(`compositor._sound_covers_plan`); the `fed` tests below count what the graph
hands the encoder, which no decoder's end trimming can hide.
"""
from __future__ import annotations

import json
import struct
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from video_ai_editor.edl import timebase as tb  # noqa: E402
from video_ai_editor.edl.schema import AudioProps, Canvas, Clip, empty_edl  # noqa: E402
from video_ai_editor.render import compositor, render_export  # noqa: E402
from video_ai_editor.render.frame_map import planned_frames  # noqa: E402

from wk import audio_fixture as fx  # noqa: E402

# (in seconds, frames): on- and off-grid cuts, a head trim, a long tail clip.
CUTS = [(10.64, 17), (0.3, 13), (7.07, 11), (2.5, 9), (3.3, 10),
        (4.46, 12), (0.1, 8), (5.0, 14), (6.0, 10), (8.2, 19)]
NTSC = [Fraction(30000, 1001), Fraction(60000, 1001)]


@pytest.fixture(scope="module")
def counter(tmp_path_factory) -> str:
    d = tmp_path_factory.mktemp("tail")
    return str(fx.counter_source(d / "c2997.mov", Fraction(30000, 1001), 12.0))


def _edl(src: str, rate: Fraction, lufs):
    fps = tb.fps_float(rate)
    e = empty_edl(Canvas(w=64, h=36, fps=fps))
    e.canvas.loudness_lufs = lufs
    v1 = e.get_track("v1")
    cur = 0
    for i, (in_, n) in enumerate(CUTS):
        c = Clip(src=src, start=tb.time_of(cur, fps), id=f"c{i}")
        c.in_, c.out = in_, in_ + tb.time_of(n, fps)
        c.audio = AudioProps()
        v1.clips.append(c)
        cur += n
    e.recompute_duration()
    return e, fps


def _samples(path: Path) -> int:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0",
                          "-f", "f32le", "-ac", "1", "-"], check=True, capture_output=True).stdout
    return len(np.frombuffer(raw, dtype="<f4"))


def _frames(path: Path) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                          "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)],
                         check=True, capture_output=True, text=True).stdout
    return int(out.strip().splitlines()[0].rstrip(","))


def _edit_lists(path: Path) -> list[list[tuple[int, int]]]:
    """Every `elst` box's (segment_duration, media_time) entries, in file
    order (one box per track that has one)."""
    data = path.read_bytes()
    out, at = [], data.find(b"elst")
    while at >= 0:
        version, count = data[at + 4], struct.unpack(">I", data[at + 8:at + 12])[0]
        fmt, step = (">Qq", 20) if version == 1 else (">Ii", 12)
        out.append([struct.unpack(fmt, data[at + 12 + k * step:at + 12 + k * step + step - 4])
                    for k in range(min(count, 8))])
        at = data.find(b"elst", at + 4)
    return out


def _tail_report(path: Path, plan: int, fps) -> str:
    """What a short tail needs to be diagnosed from a CI log alone: planned
    and decoded samples, each stream's start and length, the container's
    duration and the edit lists."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,time_base,start_pts,duration_ts,nb_frames:format=duration",
         "-of", "json", str(path)], capture_output=True, text=True).stdout
    try:
        info = json.loads(probe)
    except ValueError:
        info = {"ffprobe": probe[-400:]}
    version = subprocess.run(["ffmpeg", "-version"], capture_output=True,
                             text=True).stdout.splitlines()[:1]
    return (f"planned {tb.samples_for_frames(plan, fps)} samples for {plan} frames, "
            f"decoded {_samples(path)}; container {info.get('format')}; "
            f"streams {info.get('streams')}; edit lists {_edit_lists(path)}; {version}")


def _spy_graph_renders(monkeypatch) -> list[list[str]]:
    seen: list[list[str]] = []
    real = compositor._cancel.run

    def spy(args, *a, **k):
        if "-filter_complex" in args:
            seen.append([str(x) for x in args])
        return real(args, *a, **k)
    monkeypatch.setattr(compositor._cancel, "run", spy)
    return seen


def _fed_samples(args: list[str]) -> int:
    """Samples the render's graph hands its audio encoder: the same inputs
    and graph, the mapped sound written as raw PCM instead of AAC."""
    if args[2:4] == ["-f", "concat"]:
        # The chunk assembly's picture input is a list file it deletes when
        # done; any picture keeps the sound inputs' indices.
        i = args.index("-i")
        args = [*args[:2], "-f", "lavfi", "-i", "color=s=16x16:r=1:d=1", *args[i + 2:]]
    at = args.index("-filter_complex")
    maps = [args[i + 1] for i, a in enumerate(args) if a == "-map"]
    assert len(maps) == 2, maps
    raw = subprocess.run(
        [*args[:at + 2], "-map", maps[0], "-f", "null", "-",
         "-map", maps[1], "-f", "f32le", "-ac", "1", "pipe:1"],
        check=True, capture_output=True).stdout
    return len(raw) // 4


@pytest.mark.parametrize("rate", NTSC, ids=["2997", "5994"])
def test_an_ntsc_export_carries_every_planned_sample(tmp_path, counter, rate):
    e, fps = _edl(counter, rate, -16.0)
    plan = planned_frames(e, fps)
    out = render_export(e, tmp_path, height=36).path
    assert _frames(out) == plan
    assert _samples(out) >= tb.samples_for_frames(plan, fps), _tail_report(out, plan, fps)


@pytest.mark.parametrize("rate", NTSC, ids=["2997", "5994"])
def test_an_ntsc_single_pass_preview_carries_every_planned_sample(tmp_path, counter, rate):
    e, fps = _edl(counter, rate, None)
    plan = planned_frames(e, fps)
    out = compositor._render(e, tmp_path / "p.mp4", height=36, fps=fps, preview=True,
                             cache_dir=tmp_path / "cache", chunked=False)
    assert _frames(out) == plan
    assert _samples(out) >= tb.samples_for_frames(plan, fps), _tail_report(out, plan, fps)


RATES = {"24": Fraction(24), "25": Fraction(25), "2997": NTSC[0],
         "30": Fraction(30), "5994": NTSC[1]}


@pytest.mark.parametrize("chunked", [False, True], ids=["single-pass", "chunks"])
@pytest.mark.parametrize("rate", list(RATES.values()), ids=list(RATES))
def test_the_graph_feeds_the_encoder_every_planned_sample(tmp_path, counter, monkeypatch,
                                                          rate, chunked):
    """Version-independent: counts the PCM the graph produces, not what a
    decoder makes of the AAC (ffmpeg <= 8 returns whole frames and hid a
    graph one sample short at 29.97)."""
    e, fps = _edl(counter, rate, None)
    plan = planned_frames(e, fps)
    seen = _spy_graph_renders(monkeypatch)
    compositor._render(e, tmp_path / "p.mp4", height=36, fps=fps, preview=True,
                       cache_dir=tmp_path / "cache", chunked=chunked)
    assert seen, "a graph render ran"
    assert _fed_samples(seen[-1]) >= tb.samples_for_frames(plan, fps)


def test_the_tail_pad_adds_silence_and_moves_no_sample(tmp_path, counter, monkeypatch):
    """The pad is `apad` on the delivered sound: the graph without it yields
    the same PCM, the padded tail is digital silence."""
    e, fps = _edl(counter, NTSC[0], None)
    seen = _spy_graph_renders(monkeypatch)
    compositor._render(e, tmp_path / "p.mp4", height=36, fps=fps, preview=True,
                       cache_dir=tmp_path / "cache", chunked=False)
    args = seen[-1]
    at = args.index("-filter_complex")
    fc = args[at + 1]
    n = tb.samples_for_frames(planned_frames(e, fps), fps)
    assert fc.endswith(f"[aout]apad=whole_len={n}[acap]"), fc[-120:]

    def pcm(graph: str, label: str) -> np.ndarray:
        raw = subprocess.run(
            [*args[:at], "-filter_complex", graph, "-map", "[vcap]", "-f", "null", "-",
             "-map", label, "-f", "f32le", "pipe:1"], check=True, capture_output=True).stdout
        return np.frombuffer(raw, dtype="<f4").reshape(-1, 2)
    padded = pcm(fc, "[acap]")
    bare = pcm(fc[:fc.rindex(";")], "[aout]")
    assert len(bare) == 196996 and len(padded) == n == 196997
    assert np.array_equal(padded[:len(bare)], bare)
    assert not padded[len(bare):].any()


def test_the_picture_is_capped_inside_the_graph_not_by_an_output_option(tmp_path, counter, monkeypatch):
    """The cap must not be `-frames:v` (it closes the file before the audio
    encoder flushes); it is a `trim=end_frame=<plan>` on the mapped picture."""
    e, fps = _edl(counter, NTSC[0], None)
    seen: list[list[str]] = []
    real = compositor._cancel.run

    def spy(args, *a, **k):
        if "-filter_complex" in args:
            seen.append(list(args))
        return real(args, *a, **k)
    monkeypatch.setattr(compositor._cancel, "run", spy)
    compositor._render(e, tmp_path / "p.mp4", height=36, fps=fps, preview=True,
                       cache_dir=tmp_path / "cache", chunked=False)
    assert seen, "the single-pass render ran"
    args = seen[-1]
    assert "-frames:v" not in args
    fc = args[args.index("-filter_complex") + 1]
    assert f"trim=end_frame={planned_frames(e, fps)}" in fc


def test_the_audio_only_remux_feeds_every_planned_sample(tmp_path, counter, monkeypatch):
    """The preview's audio-only path muxes fresh sound onto the cached picture:
    the same pad, so the two preview paths of one EDL stay the same length."""
    e, fps = _edl(counter, NTSC[0], None)
    plan = planned_frames(e, fps)
    full = compositor._render(e, tmp_path / "p.mp4", height=36, fps=fps, preview=True,
                              cache_dir=tmp_path / "cache", chunked=False)
    picture = tmp_path / "picture.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(full), "-an", "-c:v", "copy",
                    str(picture)], check=True)
    seen = _spy_graph_renders(monkeypatch)
    out = tmp_path / "remux.mp4"
    compositor._remux_with_new_audio(e, picture, out, fps=fps, cache_dir=tmp_path / "cache")
    assert _fed_samples(seen[-1]) >= tb.samples_for_frames(plan, fps)
    assert _frames(out) == plan
    # the failure report of the delivery tests reads a real file
    report = _tail_report(out, plan, fps)
    assert "planned 196997 samples for 123 frames" in report and "edit lists [[" in report, report
