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
