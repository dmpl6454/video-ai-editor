"""QA-029: music must duck under the Voiceover and Main-audio lanes too.

The sidechain key used to be built from v1 (+ folded PiP) audio only, so the
textbook case — music bed under a voiceover — never ducked: the VO and the
a1/audio lanes were mixed AFTER sidechaincompress and never reached its key.

Real renders, measured: a silent picture, a 220 Hz bed with ducking on, and a
1 kHz "speech" tone on the lane under test at [2,4) and [7,9). The 220 Hz band
is measured (bandpass) inside the speech windows and in the gaps; ducking
means the bed is clearly quieter while the speech plays.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.edl.schema import EDL, Canvas, Clip, MusicDuck, Track
from video_ai_editor.render import render_preview

DUR = 11.0
SPEECH = [(2.0, 4.0), (7.0, 9.0)]
GAPS = [(0.3, 1.7), (4.6, 6.6), (9.6, 10.6)]


def _run(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True, capture_output=True)


def _silent_picture(p: Path) -> None:
    _run(["-f", "lavfi", "-i", f"color=c=gray:s=320x180:d={DUR}:r=30",
          "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={DUR}",
          "-shortest", "-pix_fmt", "yuv420p", "-c:a", "aac", str(p)])


def _tone(p: Path, freq: int, dur: float, amp: float) -> None:
    # aevalsrc, not `sine`: lavfi's sine is fixed at 1/8 full scale, far below
    # speech level, so a "speech" key built from it barely crosses the
    # compressor threshold and would prove nothing either way.
    _run(["-f", "lavfi", "-i",
          f"aevalsrc={amp}*sin(2*PI*{freq}*t):s=48000:d={dur}:c=stereo", str(p)])


def _band_db(path: Path, a: float, b: float) -> float:
    proc = subprocess.run(
        ["ffmpeg", "-v", "info", "-ss", f"{a}", "-t", f"{b - a}", "-i", str(path),
         "-af", "bandpass=f=220:width_type=h:w=60,volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", proc.stderr)
    assert m, proc.stderr[-800:]
    return float(m.group(1))


def _edl(tmp: Path, lane_id: str, lane_type: str) -> EDL:
    pic, bed, speech = tmp / "pic.mp4", tmp / "bed.wav", tmp / "speech.wav"
    _silent_picture(pic)
    _tone(bed, 220, DUR, amp=0.2)   # about -17 dBFS rms
    _tone(speech, 1000, 2.0, amp=0.3)  # about -13 dBFS rms, speech-like
    lane = [Clip(id=f"s{i}", src=str(speech), in_=0.0, out=2.0, start=s)
            for i, (s, _e) in enumerate(SPEECH)]
    tracks = [
        Track(id="v1", type="video", clips=[
            Clip(id="c1", src=str(pic), in_=0.0, out=DUR, start=0.0)]),
        Track(id="music", type="music", duck=MusicDuck(), clips=[
            Clip(id="m1", src=str(bed), in_=0.0, out=DUR, start=0.0)]),
    ]
    tracks.append(Track(id=lane_id, type=lane_type, clips=lane))
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=tracks)
    edl.recompute_duration()
    return edl


@pytest.mark.parametrize("lane_id,lane_type", [("vo", "vo"), ("a1", "audio")])
def test_music_ducks_under_a_non_v1_speech_lane(tmp_path: Path, lane_id, lane_type):
    out = render_preview(_edl(tmp_path, lane_id, lane_type), tmp_path, height=180).path
    speech = [_band_db(out, a + 0.3, b - 0.1) for a, b in SPEECH]
    gaps = [_band_db(out, a, b) for a, b in GAPS]
    # The bed must sit clearly lower whenever the lane is speaking.
    assert max(speech) <= min(gaps) - 6.0, f"no duck on {lane_id}: {speech=} {gaps=}"


def test_duck_off_leaves_the_bed_level_under_vo(tmp_path: Path):
    edl = _edl(tmp_path, "vo", "vo")
    edl.get_track("music").duck = None
    out = render_preview(edl, tmp_path, height=180).path
    speech = [_band_db(out, a + 0.3, b - 0.1) for a, b in SPEECH]
    gaps = [_band_db(out, a, b) for a, b in GAPS]
    assert abs(max(speech) - min(gaps)) < 2.0, f"{speech=} {gaps=}"


def test_music_ducks_under_vo_in_the_export(tmp_path: Path):
    """Same measurement through the EXPORT path (loudnorm + limiter after the
    mix): the delivered file must duck the bed under the voiceover too."""
    from video_ai_editor.render import render_export
    out = render_export(_edl(tmp_path, "vo", "vo"), tmp_path, height=180).path
    speech = [_band_db(out, a + 0.3, b - 0.1) for a, b in SPEECH]
    gaps = [_band_db(out, a, b) for a, b in GAPS]
    assert max(speech) <= min(gaps) - 6.0, f"no duck in export: {speech=} {gaps=}"
