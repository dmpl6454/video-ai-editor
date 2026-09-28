"""A sound lane (music, voiceover, any audio track) keeps its FULL length
across main-track transitions (final QA, round 3).

THE DEFECT: every non-v1 lane is placed on the render clock
(`render/clock.py`), and `render_window(start, end)` shrinks a window by
every seam inside it. That is right for a picture (A's tail and B's head are
the same output frames), but a voiceover is not a picture: shrinking its
window CUT ITS TAIL. A 7 s voiceover typed at 4.0 s over a dissolve and a
clock wipe lost its last 0.9 s of speech in the export, while the Inspector
said Start 04:00, Duration 06:03 but Speed > Duration 7.00 s. In CapCut a
transition overlaps the main-track clips only; audio tracks keep their
length.

THE RULE NOW: a sound-lane clip starts at `render_time(start)` and plays its
whole `effective_duration` (`clock.sound_window`); `EDL.recompute_duration`
counts that end, so a bed that now outlives the picture is not cut by the
file's end either. Measured from the samples of real renders.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from video_ai_editor.edl.schema import EDL, Canvas, Clip, Track, Transition, sound_lane
from video_ai_editor.render import audio_mix, clock, render_export

W, H, FPS = 320, 180, 30
TONE_HZ = 1000
HOP = 0.05
#: The VO "speech": a tone that stops 0.2 s before the file ends (as the
#: finder's voiceover.wav, whose speech stops at 6.65 of 7.0 s).
VO_LEN, VO_SPEECH = 3.5, 3.3


def _run(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, capture_output=True)


@pytest.fixture(scope="module")
def fx(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("slfx")
    for name, colour in (("a", "red"), ("b", "blue"), ("c", "green"), ("d", "white")):
        _run(["-f", "lavfi", "-i", f"color=c={colour}:s={W}x{H}:d=2:r={FPS}",
              "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=2",
              "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(d / f"{name}.mp4")])
    _run(["-f", "lavfi", "-i",
          f"aevalsrc=0.3*sin(2*PI*{TONE_HZ}*t)*lt(t\\,{VO_SPEECH}):s=48000:d={VO_LEN}",
          "-c:a", "pcm_s16le", str(d / "vo.wav")])
    return {p.stem: p for p in d.iterdir()}


def _edl(fx, *, lane: str = "vo", start: float = 1.0, length: float = VO_LEN) -> EDL:
    """Four 2 s v1 clips, a 0.5 s dissolve at 2.0 and a 0.4 s one at 4.0
    (render length 7.1 s), and ONE sound clip on `lane`."""
    edl = EDL(canvas=Canvas(w=W, h=H, fps=FPS, loudness_lufs=None), tracks=[
        Track(id="v1", type="video", z=0, clips=[
            Clip(id=n, src=str(fx[n]), in_=0, out=2, start=2.0 * i)
            for i, n in enumerate("abcd")],
            transitions=[Transition(at=2.0, type="fade", duration=0.5),
                         Transition(at=4.0, type="fade", duration=0.4)]),
        Track(id="music", type="music", z=0),
        Track(id="vo", type="vo", z=0),
        Track(id="a1", type="audio", z=0),
    ])
    edl.get_track(lane).clips.append(
        Clip(id="s", src=str(fx["vo"]), in_=0, out=length, start=start))
    edl.recompute_duration()
    return edl


def _tone_window(path: Path) -> tuple[float, float] | None:
    n = int(round(48000 * HOP))
    af = (f"aresample=48000,bandpass=f={TONE_HZ}:width_type=h:w=40,"
          f"asetnsamples=n={n}:p=0,astats=metadata=1:reset=1,"
          "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-")
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
                           "-vn", "-af", af, "-f", "null", "-"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-400:]
    on: list[float] = []
    t = None
    for line in proc.stdout.splitlines():
        if "pts_time:" in line:
            t = float(line.split("pts_time:")[1].split()[0])
        elif "RMS_level=" in line and t is not None:
            v = line.split("RMS_level=")[1].strip()
            if v != "-inf" and float(v) > -30.0:
                on.append(t)
            t = None
    return (min(on), max(on) + HOP) if on else None


def test_sound_lanes_are_music_vo_and_audio_tracks():
    assert sound_lane(Track(id="music", type="music"))
    assert sound_lane(Track(id="vo", type="vo"))
    assert sound_lane(Track(id="a1", type="audio"))
    assert not sound_lane(Track(id="v2", type="video"))
    assert not sound_lane(Track(id="tx", type="text"))


def test_the_sound_window_starts_on_the_render_clock_and_keeps_its_length(fx):
    edl = _edl(fx)
    seams = clock.seam_table(edl)
    # the picture rule shrinks a straddling window by both seams (0.9 s) ...
    assert clock.render_window(seams, 1.0, 1.0 + VO_LEN) == pytest.approx((1.0, 3.6))
    # ... a sound lane does not: it starts where its start plays, whole
    assert clock.sound_window(seams, 1.0, VO_LEN) == pytest.approx((1.0, 4.5))
    # a start after a seam is pulled by it, as every lane is
    assert clock.sound_window(seams, 3.0, VO_LEN) == pytest.approx((2.5, 6.0))
    # a clip starting inside a consumed span is still heard, never dropped
    assert clock.sound_window(seams, 1.8, 0.2) == pytest.approx((1.8, 2.0))
    placed = audio_mix._on_render_clock(edl.get_track("vo").clips, seams)
    assert [w for _c, w in placed] == [pytest.approx((1.0, 4.5))]


@pytest.mark.parametrize("lane", ["vo", "music", "a1"])
def test_a_voiceover_across_two_transitions_exports_its_last_words(tmp_path, fx, lane):
    """The finder's case, scaled down: the lane clip spans both seams and its
    'speech' (the tone) must be heard for all of its 3.3 s, from render 1.0
    to 4.3 — not cut at 3.6 by the 0.9 s the two dissolves consume."""
    edl = _edl(fx, lane=lane)
    out = render_export(edl, tmp_path, height=H).path
    heard = _tone_window(out)
    assert heard is not None
    assert heard[0] == pytest.approx(1.0, abs=HOP + 0.01)
    assert heard[1] == pytest.approx(1.0 + VO_SPEECH, abs=HOP + 0.01), (
        f"{lane}: speech heard {heard}, expected to run to {1.0 + VO_SPEECH:.2f} s")


def test_a_bed_that_outlives_the_picture_sets_the_timeline_length(fx):
    """Layout 8 s of v1 renders 7.1 s. `edl.duration` (the transport, and the
    length the compositor pads the picture to) must reach the end of a sound
    clip that now plays whole: an 8 s bed at layout 1.0 plays render 1.0-9.0,
    where the old rule stopped the timeline at 9.0 - 0.9 = 8.1 and cut it."""
    assert _edl(fx, lane="music", start=1.0, length=VO_LEN).duration == pytest.approx(7.1)
    assert _edl(fx, lane="music", start=5.0, length=VO_LEN).duration == pytest.approx(4.1 + VO_LEN)
    assert _edl(fx, lane="vo", start=1.0, length=8.0).duration == pytest.approx(9.0)


# ------------------------------------------------ runs of abutting clips

def test_a_run_of_abutting_clips_moves_as_one_block(fx):
    """A voiceover split at 2.5 (across the 2.0 seam), or a looped bed, is a
    RUN: every piece is pulled by the overlap before the run's first clip, so
    the pieces stay back to back — pulling the second piece by its own start
    (0.5 s) while the first plays whole would double 0.5 s of sound."""
    from video_ai_editor.edl.schema import sound_pulls
    edl = _edl(fx)
    vo = edl.get_track("vo")
    whole = vo.clips[0]
    vo.clips = [Clip(id="p1", src=whole.src, in_=0, out=1.5, start=1.0),
                Clip(id="p2", src=whole.src, in_=1.5, out=VO_LEN, start=2.5),
                Clip(id="late", src=whole.src, in_=0, out=0.5, start=6.0)]
    seams = clock.seam_table(edl)
    assert sound_pulls(vo.clips, seams) == {"p1": 0.0, "p2": 0.0, "late": pytest.approx(0.9)}
    wins = clock.sound_windows(vo.clips, seams)
    assert wins["p1"] == pytest.approx((1.0, 2.5)) and wins["p2"] == pytest.approx((2.5, 4.5))
    assert wins["late"] == pytest.approx((5.1, 5.6))


def test_a_split_voiceover_across_a_seam_sounds_like_the_unsplit_one(tmp_path, fx):
    whole = _edl(fx)
    split = _edl(fx)
    vo = split.get_track("vo")
    src = vo.clips[0].src
    vo.clips = [Clip(id="p1", src=src, in_=0, out=1.5, start=1.0),
                Clip(id="p2", src=src, in_=1.5, out=VO_LEN, start=2.5)]
    split.recompute_duration()
    a = _tone_window(render_export(whole, tmp_path / "whole", height=H).path)
    b = _tone_window(render_export(split, tmp_path / "split", height=H).path)
    assert a is not None and b is not None
    assert b == pytest.approx(a, abs=HOP + 0.01)


def test_a_default_bed_and_fit_to_video_end_with_the_picture(tmp_path, fx):
    """`add_music` with no `out` and `fit_music_to_video` size the bed to the
    picture's RENDER end (7.1 s here), not its layout end (8.0 s): a bed
    that plays whole across the transitions and was sized to the layout ran
    0.9 s past the picture, into a black tail."""
    from video_ai_editor.agent.dispatch import dispatch
    from video_ai_editor.edl import EDLStore
    store = EDLStore(tmp_path / "s")
    store.edl = _edl(fx, lane="a1", length=0.5)
    long_bed = tmp_path / "bed.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=300:d=20", str(long_bed)],
                   check=True, capture_output=True)
    dispatch(store, "add_music", {"src": str(long_bed), "start": 0.0})
    [bed] = store.edl.get_track("music").clips
    assert store.edl.render_video_end() == pytest.approx(7.1)
    assert bed.effective_duration == pytest.approx(7.1, abs=0.01)
    assert store.edl.duration == pytest.approx(7.1, abs=0.01), "no tail past the picture"
    bed.out = 12.0
    store.edl.recompute_duration()
    assert store.edl.duration == pytest.approx(12.0)
    dispatch(store, "fit_music_to_video", {})
    [bed] = store.edl.get_track("music").clips
    assert bed.effective_duration == pytest.approx(7.1, abs=0.01)
    assert store.edl.duration == pytest.approx(7.1, abs=0.01)
