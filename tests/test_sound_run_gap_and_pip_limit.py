"""Final sweep 3, round 2 (render-audio): two export-truth findings.

1. A LAID GAP BETWEEN TWO SOUND RUNS NEVER BECOMES AN OVERLAP.
   v1 A|B|C (4 s each) with a 0.5 s fade at 4.0; the voiceover lane holds
   line 1 at [1.0, 4.8) and line 2 laid 0.2 s after it. Line 1 ends inside
   v1, so it plays whole (render 1.0-4.8); line 2 opens a new run, pulled by
   the whole 0.5 s overlap before it (render 4.5). The two lines talked over
   each other for 0.3 s where the timeline shows a 0.2 s gap. Now an
   UNLINKED run starts no earlier than the previous unlinked run on its lane
   ends (`schema.sound_render_windows`): the gap shrinks at most to zero.
   A detached sound (`linked_to`) keeps following its picture (J/L overlap).

2. A PIP'S SOUND FOLDED INTO v1 COUNTS AS A MIX.
   With Loudness Off, the export's true-peak limiter runs only where lanes
   were mixed. A PIP's sound is folded into the main label BEFORE
   `build_audio_mix`, which saw one input and left the master empty: a
   0.9-amplitude tone on v1 and on a PIP summed past full scale and a WAV
   export (no AAC delivery hold) hard-clipped 42 % of its samples.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.edl.schema import (EDL, Canvas, Clip, Track, Transform, Transition,
                                        sound_pull_at, sound_pulls, sound_render_windows)
from video_ai_editor.render import audio_mix, clock, render_export

W, H, FPS = 320, 180, 30
F1, F2 = 700, 2500
HOP = 0.01


def _run(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, capture_output=True)


@pytest.fixture(scope="module")
def fx(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("gapfx")
    for name, colour in (("a", "red"), ("b", "blue"), ("c", "green")):
        _run(["-f", "lavfi", "-i", f"color=c={colour}:s={W}x{H}:d=4:r={FPS}",
              "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=4",
              "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
              "-c:a", "aac", "-shortest", str(d / f"{name}.mp4")])
    for name, hz in (("l1", F1), ("l2", F2)):
        _run(["-f", "lavfi", "-i", f"aevalsrc=0.3*sin(2*PI*{hz}*t):s=48000:d=5",
              "-ac", "2", "-c:a", "pcm_s16le", str(d / f"{name}.wav")])
    for name, colour in (("loud", "gray"), ("pip", "cyan")):
        _run(["-f", "lavfi", "-i", f"color=c={colour}:s={W}x{H}:d=3:r={FPS}",
              "-f", "lavfi", "-i", "aevalsrc=0.9*sin(2*PI*220*t):s=48000:d=3",
              "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
              "-c:a", "pcm_s16le", "-ac", "2", "-shortest", str(d / f"{name}.mov")])
    return {p.stem: p for p in d.iterdir()}


# ----------------------------------------------------------- 1. run gaps

def _vo_edl(fx, gap: float, *, linked: bool = False) -> EDL:
    v1 = [Clip(id=n, src=str(fx[n]), in_=0, out=4, start=4.0 * i) for i, n in enumerate("abc")]
    for c in v1:
        c.audio.mute = True
    l1 = Clip(id="L1", src=str(fx["l1"]), in_=0, out=3.8, start=1.0)
    l2 = Clip(id="L2", src=str(fx["l2"]), in_=0, out=3.0, start=4.8 + gap)
    if linked:
        l1.linked_to, l2.linked_to = "a", "b"
    edl = EDL(canvas=Canvas(w=W, h=H, fps=FPS, loudness_lufs=None), tracks=[
        Track(id="v1", type="video", clips=v1,
              transitions=[Transition(at=4.0, type="fade", duration=0.5)]),
        Track(id="vo", type="vo", clips=[l1, l2])])
    edl.recompute_duration()
    return edl


def _windows(edl: EDL) -> dict[str, tuple[float, float]]:
    return sound_render_windows(edl.get_track("vo").clips, clock.seam_table(edl),
                                edl.video_extent())


def test_abutting_lines_stay_back_to_back(fx):
    w = _windows(_vo_edl(fx, 0.0))
    assert w["L1"] == pytest.approx((1.0, 4.8))
    assert w["L2"] == pytest.approx((4.8, 7.8))


@pytest.mark.parametrize("gap", [0.05, 0.2, 0.45])
def test_a_laid_gap_shorter_than_the_overlap_never_overlaps(fx, gap):
    """The finder's case: line 2's run is pulled by the 0.5 s overlap before
    it, line 1 plays whole — so line 2 must start no earlier than 4.8."""
    edl = _vo_edl(fx, gap)
    w = _windows(edl)
    assert w["L1"] == pytest.approx((1.0, 4.8))
    assert w["L2"][0] >= w["L1"][1] - 1e-9, w
    assert w["L2"] == pytest.approx((4.8, 7.8))       # whole length, gap shrunk to 0
    seams, vend = clock.seam_table(edl), edl.video_extent()
    assert sound_pulls(edl.get_track("vo").clips, seams, vend)["L2"] == pytest.approx(gap)
    # a NEW clip laid there would get the same pull (`EDL.sound_cover`)
    lane = [c for c in edl.get_track("vo").clips if c.id == "L1"]
    assert sound_pull_at(lane, seams, 4.8 + gap, vend) == pytest.approx(gap)


def test_a_gap_wider_than_the_overlap_keeps_what_is_left(fx):
    w = _windows(_vo_edl(fx, 0.8))            # laid at 5.6, pulled 0.5 -> 5.1
    assert w["L2"] == pytest.approx((5.1, 8.1))


def test_detached_sounds_keep_their_j_l_overlap(fx):
    """A detached sound follows its own picture: line 2 as b's sound plays
    under b from render 4.5 even though line 1 (a's) is still sounding."""
    w = _windows(_vo_edl(fx, 0.2, linked=True))
    assert w["L2"] == pytest.approx((4.5, 7.5))


def test_a_run_after_one_cut_at_the_programme_end_keeps_its_gap(fx):
    """A run laid past v1's end is cut where its layout end maps; the next
    run, laid after it, is not pushed further by the uncut length."""
    edl = _vo_edl(fx, 0.0)
    vo = edl.get_track("vo")
    # line 1 at 3.0 (pull 0, before the seam) laid to 13.0, past v1's end
    # 12.0: cut where 13.0 maps, 12.5 (uncut it would run to 13.0)
    vo.clips = [Clip(id="L1", src=str(fx["l1"]), in_=0, out=10.0, start=3.0),
                Clip(id="L2", src=str(fx["l2"]), in_=0, out=1.0, start=13.2)]
    w = _windows(edl)
    assert w["L1"] == pytest.approx((3.0, 12.5))
    assert w["L2"] == pytest.approx((12.7, 13.7))     # its 0.2 s gap, not pushed to 13.0


def _tone_span(path: Path, hz: int) -> tuple[float, float] | None:
    n = int(round(48000 * HOP))
    af = (f"aresample=48000,bandpass=f={hz}:width_type=h:w=60,"
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


def test_the_export_never_plays_two_lines_at_once(tmp_path, fx):
    out = render_export(_vo_edl(fx, 0.2), tmp_path, height=H).path
    a, b = _tone_span(out, F1), _tone_span(out, F2)
    assert a is not None and b is not None
    assert a == pytest.approx((1.0, 4.8), abs=0.03)
    # line 2 starts where line 1 stops (bandpass ringing: a hop or two)
    assert b[0] >= a[1] - 0.03, f"line 1 heard {a}, line 2 heard {b}: they overlap"
    assert b[1] == pytest.approx(7.8, abs=0.03)


# ----------------------------------------------------- 2. PIP fold limit

def _pip_edl(fx) -> EDL:
    edl = EDL(canvas=Canvas(w=W, h=H, fps=FPS, loudness_lufs=None), tracks=[
        Track(id="v1", type="video", clips=[Clip(id="L", src=str(fx["loud"]), in_=0, out=3, start=0)]),
        Track(id="v2", type="video", z=5, clips=[
            Clip(id="P", src=str(fx["pip"]), in_=0, out=3, start=0, transform=Transform(scale=0.3))])])
    edl.recompute_duration()
    return edl


def test_a_folded_main_label_gets_the_limiter(fx):
    edl = _pip_edl(fx)
    chain, _inputs, label = audio_mix.build_audio_mix(
        edl, main_audio_label="[a_with_pip]", first_input_index=3, main_is_mix=True)
    assert "alimiter" in chain and label != "[a_with_pip]", chain
    # a lone v1 track is still left exactly as is
    chain, _inputs, label = audio_mix.build_audio_mix(
        edl, main_audio_label="[aout]", first_input_index=3)
    assert chain == "" and label == "[aout]"


def _peaks(p: Path) -> tuple[float, float]:
    err = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(p), "-vn",
                          "-af", "ebur128=peak=true+sample:framelog=quiet", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    tail = err[err.rfind("Summary:"):]
    tp = float(re.search(r"True peak:\s*\n\s*Peak:\s+(-?[\d.]+|-inf)", tail).group(1))
    sp = float(re.search(r"Sample peak:\s*\n\s*Peak:\s+(-?[\d.]+|-inf)", tail).group(1))
    return tp, sp


def test_a_wav_export_of_v1_plus_a_loud_pip_does_not_clip(tmp_path, fx):
    out = render_export(_pip_edl(fx), tmp_path, height=H, container="wav").path
    tp, sp = _peaks(out)
    assert sp < -0.5, f"sample peak {sp} dBFS: the PIP + v1 sum hard-clips"
    assert tp <= audio_mix.EXPORT_TP_LIMIT_DB + 0.3, f"true peak {tp} dBTP"
