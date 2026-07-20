"""Track-level v1 mute and per-clip PIP audio props must be audible.

Both were dead controls: `set_track_muted('v1')` was read nowhere in the
render pipeline, and V2/PIP clips' own gain/fade/mute were ignored by both
the main render and the remux fast path (only aresample+adelay ran).
Parametrized over both render paths — the remux path answers when the
video-only cache is warm, the full path when it isn't.
"""
from __future__ import annotations
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.edl.schema import EDL, Track, Clip, Canvas
from video_ai_editor.render import render_preview


def _mk_video(path: Path, *, freq: int = 440, duration: float = 2.0,
              color: str = "blue"):
    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi", "-i", f"color=c={color}:s=320x180:d={duration}:r=30",
         "-f", "lavfi", "-i", f"sine=f={freq}:duration={duration}",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True, capture_output=True,
    )


def _mean_volume(path: Path) -> float:
    proc = subprocess.run(
        ["ffmpeg", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", proc.stderr)
    assert m, f"volumedetect produced no mean_volume:\n{proc.stderr[-1500:]}"
    return float(m.group(1))


@pytest.mark.parametrize("keep_video_cache", [True, False],
                         ids=["remux-path", "full-render-path"])
def test_v1_track_mute_silences_preview(tmp_path: Path, keep_video_cache: bool):
    src = tmp_path / "src.mp4"
    _mk_video(src)
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(src=str(src), in_=0, out=2, start=0, id="c1"),
        ]),
    ])
    edl.recompute_duration()

    loud = _mean_volume(render_preview(edl, tmp_path, height=180).path)

    edl.get_track("v1").muted = True
    if not keep_video_cache:
        shutil.rmtree(tmp_path / "cache" / "videos", ignore_errors=True)
    quiet = _mean_volume(render_preview(edl, tmp_path, height=180).path)

    assert quiet <= loud - 25, f"v1 track mute inaudible: {loud=} {quiet=}"


@pytest.mark.parametrize("keep_video_cache", [True, False],
                         ids=["remux-path", "full-render-path"])
def test_pip_clip_gain_is_honored(tmp_path: Path, keep_video_cache: bool):
    v1_src = tmp_path / "v1.mp4"
    pip_src = tmp_path / "pip.mp4"
    _mk_video(v1_src, freq=440)
    _mk_video(pip_src, freq=880, color="red")

    v1_clip = Clip(src=str(v1_src), in_=0, out=2, start=0, id="c1")
    v1_clip.audio.gain_db = -50.0  # near-silent: PIP dominates the mix
    pip_clip = Clip(src=str(pip_src), in_=0, out=2, start=0, id="p1")
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[v1_clip]),
        Track(id="v2", type="video", z=1, clips=[pip_clip]),
    ])
    edl.recompute_duration()

    loud = _mean_volume(render_preview(edl, tmp_path, height=180).path)

    pip_clip.audio.gain_db = -50.0
    if not keep_video_cache:
        shutil.rmtree(tmp_path / "cache" / "videos", ignore_errors=True)
    quiet = _mean_volume(render_preview(edl, tmp_path, height=180).path)

    assert quiet <= loud - 25, f"PIP clip gain inaudible: {loud=} {quiet=}"
