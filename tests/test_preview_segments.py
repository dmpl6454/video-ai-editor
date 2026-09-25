"""QA-005 (partial): long clips preview from cached picture SEGMENTS.

Before, a v1 clip was one preview chunk, so any edit that changed it — a
split, a trim — re-encoded the WHOLE clip (both halves of a split of a 12-min
clip: all twelve minutes). Now a long, time-invariant clip's picture is
rendered in segments on a grid global to its source file, and an edit
re-encodes only the segments containing a new edge.

Measured on real renders:
  * how many seconds of PICTURE a split re-encodes (spy on the chunk renderer);
  * that the segmented preview is the same video as the whole-clip one —
    same frame count, frames identical to within encoder noise, same sound;
  * that ineligible clips (speed, fades, keyframes, off-grid trims) keep the
    whole-clip path.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.edl import timebase as tb
from video_ai_editor.edl.schema import EDL, Canvas, Clip, Track
from video_ai_editor.render import chunks, render_preview, segments

FPS = 30
DUR = 40.0


@pytest.fixture(scope="module")
def src(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("segsrc") / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc2=s=640x360:r={FPS}:d={DUR}",
         "-f", "lavfi", "-i", f"sine=f=330:duration={DUR}", "-c:v", "libx264",
         "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(p)],
        check=True, capture_output=True)
    return p


def _edl(src: Path, cuts: list[float]) -> EDL:
    bounds = [0.0, *cuts, DUR]
    clips = [Clip(src=str(src), in_=a, out=b, start=a, id=f"c{i}")
             for i, (a, b) in enumerate(zip(bounds, bounds[1:]))]
    e = EDL(canvas=Canvas(w=640, h=360, fps=FPS), tracks=[Track(id="v1", type="video", clips=clips)])
    e.recompute_duration()
    return e


class _ChunkSpy:
    """Seconds of picture / sound each render_clip_to_chunk call encodes."""

    def __init__(self, monkeypatch):
        self.picture_s = 0.0
        self.calls: list[tuple[str, float]] = []
        real = chunks.render_clip_to_chunk

        def spy(c, *a, streams="av", **kw):
            dur = float(c.out) - float(c.in_)
            self.calls.append((streams, dur))
            if "v" in streams:
                self.picture_s += dur
            return real(c, *a, streams=streams, **kw)

        monkeypatch.setattr(chunks, "render_clip_to_chunk", spy)


def _frames(p: Path) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                          "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(p)],
                         capture_output=True, text=True, check=True).stdout
    return int(out.strip())


def _min_psnr(a: Path, b: Path) -> float:
    err = subprocess.run(["ffmpeg", "-v", "info", "-i", str(a), "-i", str(b), "-lavfi",
                          "[0:v][1:v]psnr=stats_file=-", "-f", "null", "-"],
                         capture_output=True, text=True).stdout
    vals = [float(v) if v != "inf" else 99.0 for v in re.findall(r"psnr_avg:(\S+)", err)]
    assert vals, "psnr produced no per-frame stats"
    return min(vals)


def _pcm(p: Path):
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-f", "f32le", "-ac", "1",
                          "-ar", "48000", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


def _sound_vs_source(preview: Path, source: Path, times: list[float]) -> list[tuple[int, float]]:
    """(lag in samples, relative error) of the preview's sound against the
    source's at each time — the timeline here is the source played straight
    through, so a correct preview is the source, sample for sample."""
    import numpy as np
    a, ref = _pcm(preview), _pcm(source)
    out = []
    for t in times:
        i, n = int(t * 48000), 2400
        w = a[i:i + n]
        lag = min(range(-48, 49), key=lambda k: float(np.sum((w - ref[i + k:i + k + n]) ** 2)))
        err = float(np.sqrt(np.mean((w - ref[i + lag:i + lag + n]) ** 2)) / (np.sqrt(np.mean(w ** 2)) + 1e-12))
        out.append((lag, err))
    return out


def test_segment_bounds_tile_the_clip_on_a_source_grid(src):
    c = Clip(src=str(src), in_=1.0, out=38.0, start=0.0)
    spans = segments.segment_bounds(c, FPS, 6.0)
    assert spans[0][0] == 1.0 and spans[-1][1] == 38.0
    assert all(a < b for a, b in spans)
    assert all(x[1] == y[0] for x, y in zip(spans, spans[1:]))
    # inner boundaries sit on the 6 s SOURCE grid, independent of the clip's in
    assert [round(a, 6) for a, _ in spans[1:]] == [6.0, 12.0, 18.0, 24.0, 30.0, 36.0]
    # a split half starting at 17.0 shares every grid boundary after it
    half = Clip(src=str(src), in_=17.0, out=38.0, start=0.0)
    assert [round(a, 6) for a, _ in segments.segment_bounds(half, FPS, 6.0)[1:]] == [18.0, 24.0, 30.0, 36.0]


@pytest.mark.parametrize("change", [
    {"speed": 2.0}, {"video_fade_in": 0.5}, {"reverse": True},
    {"in_": 1.01},                                   # off the frame grid
    {"out": 9.0},                                    # too short to segment
])
def test_ineligible_clips_render_whole(src, change):
    c = Clip(src=str(src), in_=0.0, out=39.0, start=0.0).model_copy(update=change)
    assert segments.segment_bounds(c, FPS, 6.0) is None


def test_keyframed_transform_renders_whole(src):
    c = Clip.model_validate({"src": str(src), "in": 0.0, "out": 39.0, "start": 0.0,
                             "transform": {"scale": {"keyframes": [[0.0, 1.0], [5.0, 1.5]]}}})
    assert segments.segment_bounds(c, FPS, 6.0) is None


def test_split_reencodes_only_the_segment_it_cuts(src, tmp_path, monkeypatch):
    monkeypatch.setattr(segments, "SEGMENT_S", 6.0)
    sess = tmp_path / "seg"
    sess.mkdir()
    render_preview(_edl(src, []), sess)                    # cold
    spy = _ChunkSpy(monkeypatch)
    split_at = tb.quantize(17.0, FPS)
    res = render_preview(_edl(src, [split_at]), sess)
    # Before: both halves re-encoded whole — 40 s of picture for one split.
    assert spy.picture_s <= 6.0 + 1e-6, spy.calls
    assert _frames(res.path) == int(DUR * FPS)


def test_segmented_preview_is_the_same_video(src, tmp_path, monkeypatch):
    # A short clip (whole-clip AAC chunk) next to two segmented ones (FLAC
    # sound): the assembly must treat both kinds of chunk alike.
    edl = _edl(src, [tb.quantize(3.0, FPS), tb.quantize(17.0, FPS)])
    monkeypatch.setattr(segments, "SEGMENT_S", 0.0)
    (tmp_path / "whole").mkdir()
    whole = render_preview(edl, tmp_path / "whole").path
    monkeypatch.setattr(segments, "SEGMENT_S", 6.0)
    (tmp_path / "seg").mkdir()
    spy = _ChunkSpy(monkeypatch)
    seg = render_preview(edl, tmp_path / "seg").path
    assert any(s == "v" for s, _ in spy.calls), "segment path was not taken"
    assert _frames(seg) == _frames(whole) == int(DUR * FPS)
    assert _min_psnr(seg, whole) >= 40.0          # frame-for-frame the same picture
    # The same sound, sample-aligned — measured against the source, which is
    # what this straight-through timeline plays (either side of both cuts).
    for lag, err in _sound_vs_source(seg, src, [1.0, 2.9, 3.1, 10.0, 16.9, 17.1, 25.0, 38.0]):
        assert lag == 0 and err < 0.02, (lag, err)
