"""QA-082: the preview plays at the export's loudness.

Export normalises to canvas.loudness_lufs with loudnorm; the preview skipped
it (Safari and 96 kHz AAC), so quiet dialogue monitored at −36.6 LUFS against
a −15.7 LUFS export. Measured here with ffmpeg's ebur128 on real renders of
both paths.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from video_ai_editor.edl.schema import EDL, Canvas, Clip, MusicDuck, Track
from video_ai_editor.render import preview_loudness as PL
from video_ai_editor.render import render_export, render_preview
from video_ai_editor.render.verify_render import integrated_loudness

DUR = 12.0


def _talk(p: Path, *, peak: float) -> Path:
    # Syllable-rate speech-like bursts with pauses, over room tone.
    expr = (f"{peak}*sin(2*PI*300*t)*sin(2*PI*1200*t+sin(2*PI*5*t))"
            f"*(0.5+0.5*sin(2*PI*3.3*t))*gt(sin(2*PI*0.4*t)\\,-0.3)")
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", f"color=c=gray:s=320x180:d={DUR}:r=30",
                    "-f", "lavfi", "-i", f"aevalsrc='{expr}':s=48000:d={DUR}:c=stereo",
                    "-shortest", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "pcm_s16le", str(p)], check=True, capture_output=True)
    return p


def _edl(src: Path, *, lufs: float, music: Path | None = None) -> EDL:
    tracks = [Track(id="v1", type="video", clips=[Clip(id="c1", src=str(src), in_=0.0, out=DUR, start=0.0)])]
    if music is not None:
        tracks.append(Track(id="music", type="music", duck=MusicDuck(), clips=[
            Clip(id="m1", src=str(music), in_=0.0, out=DUR, start=0.0)]))
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=tracks)
    edl.canvas.loudness_lufs = lufs
    edl.recompute_duration()
    return edl


def _bed(p: Path) -> Path:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    f"aevalsrc=0.05*sin(2*PI*220*t)*(0.7+0.3*sin(2*PI*0.5*t)):s=48000:d={DUR}:c=stereo",
                    str(p)], check=True, capture_output=True)
    return p


# −33 dBFS dialogue at the −16 default; a scene with a ducked bed at the
# YouTube −14 preset (QA: preview −17.7 vs export −14.1).
@pytest.mark.parametrize("peak,lufs,music", [(0.022, -16.0, False), (0.15, -14.0, True)])
def test_preview_is_within_1_5_lu_of_the_export(tmp_path: Path, peak: float, lufs: float, music: bool):
    edl = _edl(_talk(tmp_path / "talk.mov", peak=peak), lufs=lufs,
               music=_bed(tmp_path / "bed.wav") if music else None)
    exported = integrated_loudness(render_export(edl, tmp_path, height=180).path)
    previewed = integrated_loudness(render_preview(edl, tmp_path, height=180).path)
    assert exported is not None and previewed is not None
    assert abs(previewed - exported) <= 1.5, f"{previewed=} {exported=}"
    assert abs(previewed - lufs) <= 1.5, previewed


def test_a_follow_up_edit_is_matched_in_graph_without_a_second_encode(tmp_path: Path, monkeypatch):
    """Steady state: the session's last measurement sets the gain up front,
    so an ordinary edit renders at the target with no re-gain pass."""
    src = _talk(tmp_path / "talk.mov", peak=0.022)
    edl = _edl(src, lufs=-16.0)
    render_preview(edl, tmp_path, height=180)                 # first render: measures + re-gains

    calls: list[float] = []
    real = PL.regain
    monkeypatch.setattr(PL, "regain", lambda p, d: (calls.append(d), real(p, d)))
    edl2 = edl.model_copy(deep=True)
    edl2.get_track("v1").clips[0].out = DUR - 1.0              # a trim
    edl2.recompute_duration()
    out = render_preview(edl2, tmp_path, height=180).path
    assert calls == [], f"steady-state edit paid a re-gain: {calls}"
    assert abs(integrated_loudness(out) - (-16.0)) <= 1.5


def test_preview_with_music_and_no_target_is_untouched(tmp_path: Path):
    """No loudness target → the preview mix is left exactly as it was."""
    src = _talk(tmp_path / "talk.mov", peak=0.022)
    edl = _edl(src, lufs=-16.0)
    edl.canvas.loudness_lufs = None
    out = render_preview(edl, tmp_path, height=180).path
    assert integrated_loudness(out) < -30.0
    assert not (tmp_path / "cache" / "preview_loudness.json").exists()
