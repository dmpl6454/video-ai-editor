"""Transition overlaps land on the frame grid (REGRESSION after QA-009).

xfade overlaps the PICTURE by whole frames while acrossfade overlaps the SOUND
by the exact seconds. A duration that is not a whole number of frames — the
default 0.5 s at 25 fps is 12.5 frames, or 0.52 s at 30 fps — therefore put the
audio half a frame (20 ms) ahead of the picture from the first transition to
the end of the video. Measured here on a real export: a flash and a click at
every whole second, cut into four shots with three cross-fades.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from timing_fixtures import av_offsets_ms, make_clap
from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl import timebase as tb
from video_ai_editor.edl.schema import Canvas, Transition, empty_edl
from video_ai_editor.render import render_export


def _store(sd: Path, fps) -> EDLStore:
    sd.mkdir(parents=True, exist_ok=True)
    e = empty_edl()
    e.canvas = Canvas(w=320, h=180, fps=fps)
    e.canvas.loudness_lufs = None
    (sd / "edl.json").write_text(e.model_dump_json())
    return EDLStore(sd)


def _cut_with_fades(tmp_path: Path, fps: int, duration: float | None) -> EDLStore:
    clap = make_clap(tmp_path / f"clap_{fps}.mp4", seconds=16, fps=fps)
    s = _store(tmp_path / "s", fps)
    dispatch(s, "add_clip", {"track": "v1", "src": str(clap), "in": 0.0, "out": 16.0,
                             "start": 0.0})
    for t in (3.5, 7.5, 11.5):
        dispatch(s, "split_at", {"time": t})
    clips = sorted(s.edl.get_track("v1").clips, key=lambda c: c.start)
    for c in clips[:-1]:
        args = {"at": c.start + c.effective_duration, "type": "fade"}
        if duration is not None:
            args["duration"] = duration
        dispatch(s, "add_transition", args)
    return s


@pytest.mark.parametrize("fps,duration", [(25, None), (30, 0.52)])
def test_audio_stays_on_the_picture_after_crossfades(tmp_path, fps, duration):
    s = _cut_with_fades(tmp_path, fps, duration)
    for tr in s.edl.get_track("v1").transitions:
        n = tr.duration * fps
        assert abs(n - round(n)) < 1e-6, f"{tr.duration} s is not whole frames at {fps}"
    out = render_export(s.edl, s.dir).path
    offs = av_offsets_ms(out)
    assert len(offs) >= 10, offs
    assert max(abs(o) for o in offs) < 3.0, offs


def test_a_legacy_off_grid_duration_is_rendered_on_the_grid(tmp_path):
    """An EDL saved before this fix still carries 0.5 at 25 fps; the renderer
    must overlap picture and sound by the same whole number of frames."""
    s = _cut_with_fades(tmp_path, 25, None)
    v1 = s.edl.get_track("v1")
    v1.transitions = [Transition(at=t.at, type=t.type, duration=0.5) for t in v1.transitions]
    offs = av_offsets_ms(render_export(s.edl, s.dir).path)
    assert len(offs) >= 10, offs
    assert max(abs(o) for o in offs) < 3.0, offs
    # And the EDL's own clock charges what the renderer does.
    assert s.edl.transition_overlap() == pytest.approx(3 * tb.quantize(0.5, 25), abs=1e-9)
