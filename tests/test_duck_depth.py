"""QA-079: the duck dips the bed by `MusicDuck.to_db`, for quiet talkers too.

The ducker used to be a fixed sidechain compressor (threshold 0.05, ratio 8):
to_db −6 / −18 / −40 rendered byte-identical beds (a 12 dB dip whatever was
asked), and a talker peaking at −33 dBFS never crossed the threshold at all.

Real renders, measured: a silent picture, a 220 Hz bed with ducking on, and a
syllable-rate 3 kHz "speech" signal on v1 at [2,4) and [7,9). The 220 Hz band
is measured inside the speech windows and in the gaps; the dip must be the
requested depth (±1.5 dB), at a normal AND at a quiet speech level.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.edl.schema import EDL, Canvas, Clip, MusicDuck, Track
from video_ai_editor.render import render_export, render_preview

DUR = 11.0
SPEECH = [(2.0, 4.0), (7.0, 9.0)]
GAPS = [(0.3, 1.6), (5.6, 6.6), (10.4, 10.9)]


def _run(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True, capture_output=True)


def _picture_with_speech(p: Path, peak: float) -> None:
    # Syllable-rate AM on a 3 kHz carrier (far from the 220 Hz bed, so the
    # band measurement below reads the bed alone even under loud speech), only inside the SPEECH windows,
    # over −70 dBFS room noise — the envelope a talker actually has.
    gate = "+".join(f"between(t,{a},{b})" for a, b in SPEECH)
    expr = f"{peak}*sin(2*PI*3000*t)*(0.5+0.5*sin(2*PI*4*t))*({gate})"
    _run(["-f", "lavfi", "-i", f"color=c=gray:s=320x180:d={DUR}:r=30",
          "-f", "lavfi", "-i", f"aevalsrc='{expr}':s=48000:d={DUR}:c=stereo",
          "-f", "lavfi", "-i", f"anoisesrc=a=0.0003:d={DUR}:r=48000",
          "-filter_complex", "[1:a][2:a]amix=inputs=2:normalize=0:duration=first[a]",
          "-map", "0:v", "-map", "[a]", "-shortest", "-pix_fmt", "yuv420p",
          "-c:a", "pcm_s16le", str(p)])


def _bed(p: Path) -> None:
    _run(["-f", "lavfi", "-i", f"aevalsrc=0.2*sin(2*PI*220*t):s=48000:d={DUR}:c=stereo", str(p)])


def _band_db(path: Path, a: float, b: float) -> float:
    proc = subprocess.run(
        ["ffmpeg", "-v", "info", "-ss", f"{a}", "-t", f"{b - a}", "-i", str(path),
         "-af", "bandpass=f=220:width_type=h:w=60,bandpass=f=220:width_type=h:w=60,volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", proc.stderr)
    assert m, proc.stderr[-800:]
    return float(m.group(1))


def _edl(tmp: Path, *, peak: float, to_db: float) -> EDL:
    pic, bed = tmp / f"pic_{peak}.mov", tmp / "bed.wav"
    if not pic.exists():
        _picture_with_speech(pic, peak)
    if not bed.exists():
        _bed(bed)
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[Clip(id="c1", src=str(pic), in_=0.0, out=DUR, start=0.0)]),
        Track(id="music", type="music", duck=MusicDuck(to_db=to_db), clips=[
            Clip(id="m1", src=str(bed), in_=0.0, out=DUR, start=0.0)]),
    ])
    edl.canvas.loudness_lufs = None   # measure the mix, not loudnorm's ride
    edl.recompute_duration()
    return edl


def _dips(out: Path) -> tuple[list[float], list[float]]:
    speech = [_band_db(out, a + 0.3, b - 0.1) for a, b in SPEECH]
    gaps = [_band_db(out, a, b) for a, b in GAPS]
    return speech, gaps


@pytest.mark.parametrize("peak", [0.5, 0.022])          # −6 dBFS and −33 dBFS peaks
@pytest.mark.parametrize("to_db", [-6.0, -18.0, -40.0])
def test_duck_depth_is_to_db_at_any_speech_level(tmp_path: Path, peak: float, to_db: float):
    out = render_preview(_edl(tmp_path, peak=peak, to_db=to_db), tmp_path, height=180).path
    speech, gaps = _dips(out)
    ref = sum(gaps) / len(gaps)
    for s in speech:
        assert abs((s - ref) - to_db) <= 1.5, f"{peak=} {to_db=}: {speech=} {gaps=}"
    # the bed is back at full level in every gap (no stuck duck)
    assert max(gaps) - min(gaps) < 1.0, gaps


#: anoisesrc seeds. The room tone used to be random per run, and ~25 % of runs
#: ducked the first half-second (dynaudnorm's boundary mode inflates the first
#: ~300 ms of a room-tone key ~70x past the activity threshold) — so the test
#: was flaky. Fixed seeds make it deterministic; several of them cover the
#: noise realisations that used to trip it (seeds 3, 4, 6 and 7 did before the
#: absolute-level gate in duck_gain_chain).
ROOM_TONE_SEEDS = [1, 2, 3, 4, 5, 6, 7, 8]


@pytest.mark.parametrize("seed", ROOM_TONE_SEEDS)
def test_quiet_room_tone_alone_does_not_duck(tmp_path: Path, seed: int):
    """A key that is only room tone must not pull the bed down — including
    the first second, where the normalised key is inflated."""
    pic, bed = tmp_path / "tone_only.mov", tmp_path / "bed.wav"
    _run(["-f", "lavfi", "-i", f"color=c=gray:s=320x180:d={DUR}:r=30",
          "-f", "lavfi", "-i", f"anoisesrc=a=0.001:d={DUR}:r=48000:seed={seed}",
          "-shortest", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", str(pic)])
    _bed(bed)
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[Clip(id="c1", src=str(pic), in_=0.0, out=DUR, start=0.0)]),
        Track(id="music", type="music", duck=MusicDuck(to_db=-18), clips=[
            Clip(id="m1", src=str(bed), in_=0.0, out=DUR, start=0.0)]),
    ])
    edl.canvas.loudness_lufs = None
    edl.recompute_duration()
    out = render_preview(edl, tmp_path, height=180).path
    levels = [_band_db(out, a, a + 1.0) for a in (0.5, 3.0, 6.0, 9.0)]
    assert max(levels) - min(levels) < 1.0, levels
    head = [_band_db(out, a, a + 0.25) for a in (0.0, 0.25, 0.5, 0.75)]
    assert max(levels) - min(head) < 1.0, (head, levels)


def test_duck_depth_in_the_export(tmp_path: Path):
    """The delivered file (export path, loudnorm on) ducks by the requested
    depth too — loudnorm rides slowly, so the in-window dip survives."""
    edl = _edl(tmp_path, peak=0.022, to_db=-18.0)
    edl.canvas.loudness_lufs = -16.0
    out = render_export(edl, tmp_path, height=180).path
    speech, gaps = _dips(out)
    ref = sum(gaps) / len(gaps)
    assert all(s - ref <= -12.0 for s in speech), f"{speech=} {gaps=}"


# ------------------------------------------------ the prompt's duck check

def _verify_ctx(store, *, render: bool):
    import sys
    from types import SimpleNamespace
    sys.path.insert(0, str(Path(__file__).parent))
    import prompt_fixtures as F
    from video_ai_editor.agent.prompt import verify as V
    exec_result = SimpleNamespace(edl_before=store.edl.model_copy(deep=True), duration_before=store.edl.duration,
                                  steps=[], new_sessions=[], results_for=lambda t: [], outcomes_for=lambda t: [])
    return V.VerifyCtx(store=store, plan=F.plan_of(), exec_result=exec_result,
                       facts_before=F.facts_for(store), render_allowed=render)


def _duck_check(ctx, to_db):
    from video_ai_editor.agent.prompt import verify as V
    from video_ai_editor.agent.prompt.schema import CHECK_SPECS, Postcondition
    spec = CHECK_SPECS["music_ducked"]
    return V.run_check(ctx, Postcondition(check="music_ducked", args={"to_db": to_db, "enabled": True},
                                          human="music ducks under speech", needs_render=spec.needs_render,
                                          headline=spec.headline))


def _duck_store(tmp_path: Path, *, silent: bool):
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    import prompt_fixtures as F
    from video_ai_editor.agent.dispatch import dispatch
    src = None
    if silent:
        # The transcript says someone talks; the audio is silence — the bed
        # can never duck, so the check must not say it did.
        d = tmp_path / "uploads" / "mute"
        d.mkdir(parents=True)
        src = d / "mute.normalized.mp4"
        _run(["-f", "lavfi", "-i", "color=c=blue:s=320x180:d=12:r=30",
              "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=12",
              "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
              "-c:a", "aac", "-shortest", str(src)])
    store = F.make_store(tmp_path, src=src)
    bed = F.music_bed(tmp_path, dur=12.0)
    dispatch(store, "add_music", {"src": str(bed), "start": 0.0, "volume_db": -14, "duck": True})
    dispatch(store, "set_duck", {"track": "music", "enabled": True, "to_db": -18})
    return store


def test_prompt_duck_check_measures_the_rendered_dip(tmp_path: Path):
    store = _duck_store(tmp_path, silent=False)
    r = _duck_check(_verify_ctx(store, render=True), -12)
    assert r.passed is True, r.as_dict()
    assert r.measured == pytest.approx(-18.0, abs=1.5), r.as_dict()
    assert "rendered dip" in (r.detail or "")


def test_prompt_duck_check_fails_when_no_dip_was_rendered(tmp_path: Path):
    """QA-079: the check read `to_db` back from the EDL and passed a −18 dB
    duck that never happened. Speech the key cannot hear → no dip → fail."""
    store = _duck_store(tmp_path, silent=True)
    r = _duck_check(_verify_ctx(store, render=True), -12)
    assert r.passed is False, r.as_dict()
    assert r.measured is not None and r.measured > -3.0, r.as_dict()
