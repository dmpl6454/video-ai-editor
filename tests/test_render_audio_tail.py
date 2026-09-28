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
"""
from __future__ import annotations

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


@pytest.mark.parametrize("rate", NTSC, ids=["2997", "5994"])
def test_an_ntsc_export_carries_every_planned_sample(tmp_path, counter, rate):
    e, fps = _edl(counter, rate, -16.0)
    plan = planned_frames(e, fps)
    out = render_export(e, tmp_path, height=36).path
    assert _frames(out) == plan
    assert _samples(out) >= tb.samples_for_frames(plan, fps)


@pytest.mark.parametrize("rate", NTSC, ids=["2997", "5994"])
def test_an_ntsc_single_pass_preview_carries_every_planned_sample(tmp_path, counter, rate):
    e, fps = _edl(counter, rate, None)
    plan = planned_frames(e, fps)
    out = compositor._render(e, tmp_path / "p.mp4", height=36, fps=fps, preview=True,
                             cache_dir=tmp_path / "cache", chunked=False)
    assert _frames(out) == plan
    assert _samples(out) >= tb.samples_for_frames(plan, fps)


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
