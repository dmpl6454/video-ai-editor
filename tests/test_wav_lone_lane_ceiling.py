"""Final sweep 4 (render-audio): A LONE MAIN TRACK PUSHED PAST FULL SCALE
STILL HOLDS THE CEILING IN A WAV EXPORT.

With Loudness Off the export's true-peak limiter used to run only where
lanes were MIXED (`audio_mix._export_master`); a lone v1 lane was "left
exactly as is". Before 0.8.0 the Inspector could not push a clip past +6 dB,
so a single lane rarely reached full scale. Now one slider reaches +20 dB:
a −3 dBFS clip at +12 dB exported to WAV (24-bit PCM, no AAC delivery
hold) came out with every sample of the whole clip at full scale — audible
digital clipping. mp4 and m4a were held by `delivery_peak.hold`.

The rule now: the audio-only export measures the RAW mix's true peak once
(the same audio-only ebur128 pass Loudness On already runs) and, when it is
over `EXPORT_TRUE_PEAK_DBTP`, masters the lone lane through the limiter
(`export_ceiling_scope`). A lane under the ceiling still goes out exactly
as it was: no limiter, no resample.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.edl.schema import EDL, Canvas, Clip, Track
from video_ai_editor.render import audio_mix, compositor, render_export

W, H, FPS = 320, 180, 30
#: The source tone's amplitude on BOTH channels: 0.7 = −3.1 dBFS (a mono
#: `aevalsrc` upmixed to stereo would land at −6.1: swresample spreads a
#: centre channel at −3 dB).
SRC_AMP = 0.7
SRC_PEAK_DBFS = -3.1


def _run(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, capture_output=True)


@pytest.fixture(scope="module")
def loud_clip(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("lonefx")
    p = d / "loud.mov"
    tone = f"{SRC_AMP}*sin(2*PI*200*t)"
    _run(["-f", "lavfi", "-i", f"color=c=red:s={W}x{H}:d=6:r={FPS}",
          "-f", "lavfi", "-i", f"aevalsrc={tone}|{tone}:c=stereo:s=48000:d=6",
          "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
          "-c:a", "pcm_s16le", str(p)])
    return p


def _lone_edl(src: Path, *, gain_db: float, lufs: float | None = None) -> EDL:
    edl = EDL(canvas=Canvas(w=W, h=H, fps=FPS, loudness_lufs=lufs), tracks=[
        Track(id="v1", type="video", clips=[Clip(id="a", src=str(src), in_=0, out=4, start=0)])])
    edl.get_track("v1").clips[0].audio.gain_db = gain_db
    edl.recompute_duration()
    return edl


def _peaks(p: Path) -> tuple[float, float]:
    """(true peak dBTP, sample peak dBFS) of the delivered file, as ebur128
    reads the stereo stream (a mono downmix would inflate correlated
    channels by sqrt 2)."""
    err = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(p), "-vn",
                          "-af", "ebur128=peak=true+sample:framelog=quiet", "-f", "null", "-"],
                         capture_output=True, text=True, check=True).stderr
    tail = err[err.rfind("Summary:"):]
    tp = float(re.search(r"True peak:\s*\n\s*Peak:\s+(-?[\d.]+|-inf)", tail).group(1))
    sp = float(re.search(r"Sample peak:\s*\n\s*Peak:\s+(-?[\d.]+|-inf)", tail).group(1))
    return tp, sp


def _full_scale_samples(p: Path) -> int:
    """How many delivered samples sit at full scale (|x| >= 0.999)."""
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-vn", "-ac", "2",
                          "-ar", "48000", "-f", "s16le", "-"],
                         check=True, capture_output=True).stdout
    import array
    a = array.array("h")
    a.frombytes(out)
    return sum(1 for v in a if abs(v) >= int(0.999 * 32767))


# ------------------------------------------------------------ the graph

def test_the_ceiling_scope_masters_a_lone_lane_through_the_limiter(loud_clip):
    edl = _lone_edl(loud_clip, gain_db=12.0)
    # outside the scope a lone v1 track is left exactly as is (unchanged rule)
    chain, _inputs, label = audio_mix.build_audio_mix(
        edl, main_audio_label="[aout]", first_input_index=3)
    assert chain == "" and label == "[aout]"
    with audio_mix.export_ceiling_scope():
        chain, _inputs, label = audio_mix.build_audio_mix(
            edl, main_audio_label="[aout]", first_input_index=3)
    assert "alimiter" in chain and label != "[aout]", chain
    # the measuring pass still sees the raw lane inside it
    with audio_mix.export_ceiling_scope(), audio_mix.export_measure_scope():
        chain, _inputs, label = audio_mix.build_audio_mix(
            edl, main_audio_label="[aout]", first_input_index=3)
    assert chain == "" and label == "[aout]"


def test_the_ceiling_scope_changes_nothing_with_a_target(loud_clip):
    edl = _lone_edl(loud_clip, gain_db=12.0, lufs=-14.0)
    plain = audio_mix.build_audio_mix(edl, main_audio_label="[aout]", first_input_index=3)
    with audio_mix.export_ceiling_scope():
        scoped = audio_mix.build_audio_mix(edl, main_audio_label="[aout]", first_input_index=3)
    assert scoped == plain and "alimiter" in plain[0]


# --------------------------------------------------------- the decision

def test_the_raw_mix_true_peak_is_measured_without_a_target(tmp_path, loud_clip):
    over = compositor._raw_mix_true_peak(_lone_edl(loud_clip, gain_db=12.0), fps=FPS,
                                         cache_dir=tmp_path / "c1")
    under = compositor._raw_mix_true_peak(_lone_edl(loud_clip, gain_db=0.0), fps=FPS,
                                          cache_dir=tmp_path / "c2")
    assert over is not None and over > audio_mix.EXPORT_TRUE_PEAK_DBTP, over
    # the lane as it is, nowhere near the ceiling: the source's own peak
    src_tp, _src_sp = _peaks(loud_clip)
    assert under is not None and under == pytest.approx(src_tp, abs=0.3), (under, src_tp)
    assert under == pytest.approx(SRC_PEAK_DBFS, abs=0.4), under


# ------------------------------------------------------------ the file

def test_a_lone_main_track_pushed_over_full_scale_holds_the_ceiling_in_a_wav(tmp_path, loud_clip):
    out = render_export(_lone_edl(loud_clip, gain_db=12.0), tmp_path, container="wav").path
    tp, sp = _peaks(out)
    assert sp < -0.5, f"sample peak {sp} dBFS: the +12 dB lane hard-clips"
    assert tp <= audio_mix.EXPORT_TP_LIMIT_DB + 0.3, f"true peak {tp} dBTP"
    assert _full_scale_samples(out) == 0
    # and the limiter engaged on real signal rather than muting it
    assert sp > -6.0, sp


def test_a_lone_main_track_under_the_ceiling_goes_out_as_it_is(tmp_path, loud_clip, monkeypatch):
    calls: list[int] = []
    real = audio_mix.true_peak_limiter

    def spy() -> str:
        calls.append(1)
        return real()
    monkeypatch.setattr(audio_mix, "true_peak_limiter", spy)
    out = render_export(_lone_edl(loud_clip, gain_db=0.0), tmp_path, container="wav").path
    assert calls == [], "the limiter was inserted on a lane under the ceiling"
    _tp, sp = _peaks(out)
    _src_tp, src_sp = _peaks(loud_clip)
    assert sp == pytest.approx(src_sp, abs=0.1), (sp, src_sp)
    assert sp == pytest.approx(SRC_PEAK_DBFS, abs=0.2), sp
