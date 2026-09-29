"""An export at another frame rate ends on the last clip frame, with no
black, silent filler frame after it (final QA, run 2).

THE DEFECT: under a rate scope (`compositor.v1_rate_scope`) the resampled v1
spans end at `round(project_frames * r/R)` (Python's round: ties to even),
but the plan's tail was `frame_of(total_duration, r)` — the float seconds
rounded on the render grid. The two disagree on half-frame lengths: a 30 fps
clip of 111 frames (3.7 s) spans round(92.5) = 92 frames at 25 fps while the
tail asked for frame_of(3.7, 25) = 93, and the extra frame became a black
gap segment with ~40 ms of anullsrc silence at the end of the file.

THE RULE NOW: under a rate scope the plan's end is the project-grid end
resampled with the same rounding the spans use.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from video_ai_editor.edl import timebase as tb
from video_ai_editor.edl.schema import EDL, Canvas, Clip, Track
from video_ai_editor.render import compositor
from video_ai_editor.render.frame_map import planned_frames

W, H = 320, 180


def _edl(src: str, frames: int, project_fps: int = 30) -> EDL:
    edl = EDL(canvas=Canvas(w=W, h=H, fps=project_fps, loudness_lufs=None), tracks=[
        Track(id="v1", type="video", z=0, clips=[
            Clip(id="c", src=src, in_=0.0, out=frames / project_fps, start=0.0)])])
    edl.recompute_duration()
    return edl


def _plan(edl: EDL, fps: int):
    v1 = edl.get_track("v1")
    with compositor.v1_rate_scope(edl, fps):
        return compositor._v1_frame_plan(list(v1.clips), edl.duration, fps)


def test_the_finders_case_plans_no_trailing_gap():
    edl = _edl("/nonexistent/t30.mp4", 111)                  # in 0, out 3.7
    assert _plan(edl, 25) == [("clip", 0, 92)]
    assert planned_frames(edl, 25) == 92


@pytest.mark.parametrize("project_fps, fps", [(30, 25), (60, 25), (30, 24), (25, 30)])
def test_no_single_clip_length_plans_a_trailing_gap_at_another_rate(project_fps, fps):
    bad = [n for n in range(1, 1500)
           if any(k == "gap" for k, _i, _n in _plan(_edl("/nonexistent/s.mp4", n, project_fps), fps))]
    assert bad == [], f"{project_fps}->{fps}: {len(bad)} lengths end on a black gap, e.g. {bad[:5]}"


def test_two_clips_with_a_gap_between_keep_the_gap_but_no_tail():
    """A real gap in the layout is still filled; only the tail is fixed."""
    edl = _edl("/nonexistent/s.mp4", 111)
    edl.get_track("v1").clips.append(Clip(id="d", src="/nonexistent/s.mp4", in_=0.0, out=3.7, start=5.0))
    edl.recompute_duration()
    plan = _plan(edl, 25)
    assert [k for k, _i, _n in plan] == ["clip", "gap", "clip"]


@pytest.fixture(scope="module")
def src30(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("rst") / "t30.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"color=c=white:s={W}x{H}:d=5:r=30",
                    "-f", "lavfi", "-i", "sine=f=440:sample_rate=48000:d=5",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(p)],
                   check=True, capture_output=True)
    return p


def test_the_25fps_export_has_no_black_last_frame_or_silent_tail(tmp_path, src30):
    edl = _edl(str(src30), 111)
    out = compositor.render_export(edl, tmp_path, fps=25, height=H).path
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-map", "0:v:0",
                          "-vf", "scale=8:8,format=gray", "-f", "rawvideo", "-"],
                         check=True, capture_output=True).stdout
    frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 64)
    assert len(frames) == 92
    black = [i for i, f in enumerate(frames) if f.mean() < 40]
    assert black == [], f"black frames {black}"
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-map", "0:a:0", "-ac", "1",
                          "-ar", "48000", "-f", "f32le", "-"], check=True, capture_output=True).stdout
    a = np.frombuffer(pcm, dtype=np.float32)
    end = int(round(tb.time_of(92, 25) * 48000))
    last = a[end - 480:end]                                   # the final 10 ms of the picture
    assert len(last) == 480 and float(np.sqrt(np.mean(last ** 2))) > 0.01, "silent tail"
