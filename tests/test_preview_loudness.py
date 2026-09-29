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


# ---------------------------------------------------------------------------
# Final QA (render-audio): a first render whose pass-1 gain is stale must not
# leave the −1 dBFS limiter's squash, taken at the WRONG gain, in the file.

def _sine_clip(p: Path, *, freq: int, dur: float) -> Path:
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", f"color=c=gray:s=320x180:d={dur}:r=30",
                    "-f", "lavfi", "-i", f"aevalsrc=0.8*sin(2*PI*{freq}*t):s=48000:d={dur}:c=stereo",
                    "-shortest", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "pcm_s16le", str(p)], check=True, capture_output=True)
    return p


def _overlap_edl(a: Path, b: Path) -> EDL:
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[Clip(id="a", src=str(a), in_=0.0, out=6.0, start=0.0)]),
        Track(id="v2", type="video", z=1, clips=[Clip(id="b", src=str(b), in_=0.0, out=3.0, start=1.5)]),
    ])
    edl.canvas.loudness_lufs = -16.0
    edl.recompute_duration()
    return edl


def _window_stats(path: Path, t0: float, t1: float) -> tuple[float, float]:
    """(RMS dBFS, sample peak) of `path`'s audio between t0 and t1."""
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ss", str(t0), "-t", str(t1 - t0),
                          "-map", "0:a", "-ac", "1", "-ar", "48000", "-f", "f32le", "-"],
                         check=True, capture_output=True).stdout
    x = np.frombuffer(raw, dtype=np.float32).astype(np.float64)
    assert x.size > 0
    return 20 * np.log10(np.sqrt(np.mean(x * x)) + 1e-12), float(np.max(np.abs(x)))


def test_a_stale_first_pass_gain_does_not_squash_the_loud_overlap(tmp_path: Path):
    a = _sine_clip(tmp_path / "loudA.mov", freq=440, dur=6.0)
    b = _sine_clip(tmp_path / "loudB.mov", freq=330, dur=3.0)
    fresh_dir, seeded_dir = tmp_path / "fresh", tmp_path / "seeded"
    fresh_dir.mkdir()
    seeded_dir.mkdir()
    edl = _overlap_edl(a, b)

    fresh = render_preview(edl, fresh_dir, height=180).path       # first render of a session
    state = PL._read_state(fresh_dir)
    pre, gain = state["pre_gain_lufs"], state["gains"][PL.audio_key(edl)]

    # The reference: the same sound rendered in ONE pass at the recorded gain.
    PL._remember(seeded_dir, pre)
    one_pass = render_preview(edl, seeded_dir, height=180).path
    assert PL._read_state(seeded_dir)["gains"][PL.audio_key(edl)] == pytest.approx(gain, abs=0.05)

    rms_f, peak_f = _window_stats(fresh, 2.0, 4.0)                # the overlap
    rms_o, peak_o = _window_stats(one_pass, 2.0, 4.0)
    assert abs(rms_f - rms_o) <= 0.25, f"overlap {rms_f:.2f} dB vs one-pass {rms_o:.2f} dB"
    assert peak_f == pytest.approx(peak_o, rel=0.03), (peak_f, peak_o)
    out_f, _ = _window_stats(fresh, 5.0, 5.9)                      # outside it
    out_o, _ = _window_stats(one_pass, 5.0, 5.9)
    assert abs(out_f - out_o) <= 0.25, (out_f, out_o)


def test_a_first_pass_the_limiter_never_touched_is_regained_not_rerendered(tmp_path: Path, monkeypatch):
    """Quiet dialogue: pass 1 stayed under −1 dBFS, so the cheap in-place
    re-gain is exact and no second audio render is paid."""
    from video_ai_editor.render import compositor
    calls: list[str] = []
    real_regain, real_remux = PL.regain, compositor._remux_with_new_audio
    monkeypatch.setattr(PL, "regain", lambda p, d: (calls.append("regain"), real_regain(p, d)))
    monkeypatch.setattr(compositor, "_remux_with_new_audio",
                        lambda *a, **k: (calls.append("remux"), real_remux(*a, **k)))
    edl = _edl(_talk(tmp_path / "talk.mov", peak=0.022), lufs=-16.0)
    out = render_preview(edl, tmp_path, height=180).path
    assert calls == ["regain"], calls
    assert abs(integrated_loudness(out) - (-16.0)) <= 1.5
