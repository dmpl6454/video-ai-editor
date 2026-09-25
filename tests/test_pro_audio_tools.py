"""QA-086 + QA-039 residual: the pro audio tools, each driven through
dispatch and measured on a real EXPORT.

  * gain above +6 dB (the Properties slider stopped at +6; set_volume took +24);
  * track solo — while any track is soloed only soloed tracks are heard;
  * detach audio — the picture clip goes silent, its sound moves to an audio
    lane, identical until you move it (J/L cuts);
  * volume keyframes (add_keyframe prop="audio.gain_db") on v1 and on a music
    lane, and across a split;
  * varispeed (set_speed keep_pitch=false): every click lands on its flash to
    the sample, where atempo's WSOLA moves transients by up to ±12 ms.
"""
from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from timing_fixtures import av_offsets_ms, make_clap  # noqa: E402

from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.edl import EDLStore  # noqa: E402
from video_ai_editor.edl.schema import Canvas, empty_edl  # noqa: E402
from video_ai_editor.render import render_export  # noqa: E402

FPS = 30
SR = 48000


def _run(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True, capture_output=True)


def _clip_with_tone(p: Path, *, freq: int, amp: float, dur: float) -> Path:
    _run(["-f", "lavfi", "-i", f"color=c=gray:s=320x180:d={dur}:r={FPS}",
          "-f", "lavfi", "-i", f"aevalsrc={amp}*sin(2*PI*{freq}*t):s={SR}:d={dur}:c=stereo",
          "-shortest", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
          "-c:a", "pcm_s16le", str(p)])
    return p


def _tone(p: Path, *, freq: int, amp: float, dur: float) -> Path:
    _run(["-f", "lavfi", "-i", f"aevalsrc={amp}*sin(2*PI*{freq}*t):s={SR}:d={dur}:c=stereo", str(p)])
    return p


def _store(sd: Path) -> EDLStore:
    sd.mkdir(parents=True, exist_ok=True)
    e = empty_edl()
    e.canvas = Canvas(w=320, h=180, fps=FPS)
    e.canvas.loudness_lufs = None          # measure the mix itself, not loudnorm's ride
    (sd / "edl.json").write_text(e.model_dump_json())
    return EDLStore(sd)


def _pcm(p: Path) -> np.ndarray:
    # An explicit mean of the channels: `-ac 1` sums L+R at −3 dB each, which
    # reads an identical-channel tone 3 dB hot.
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-map", "0:a:0",
                          "-af", "pan=mono|c0=0.5*c0+0.5*c1",
                          "-ar", str(SR), "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).astype(np.float64)


def _band_db(a: np.ndarray, freq: float, t0: float, t1: float) -> float:
    """Level (dBFS of a sine's amplitude) of `freq` in [t0, t1) by projection."""
    seg = a[int(t0 * SR):int(t1 * SR)]
    n = np.arange(len(seg)) / SR
    c = np.dot(seg, np.cos(2 * np.pi * freq * n)) * 2 / len(seg)
    s = np.dot(seg, np.sin(2 * np.pi * freq * n)) * 2 / len(seg)
    amp = math.hypot(c, s)
    return 20 * math.log10(amp) if amp > 1e-9 else -180.0


def _v1(store):
    return store.edl.get_track("v1").clips


# ------------------------------------------------------------- gain range

def test_gain_above_6db_reaches_the_export(tmp_path):
    s = _store(tmp_path / "s")
    src = _clip_with_tone(tmp_path / "quiet.mov", freq=440, amp=0.03, dur=3.0)   # −30.5 dBFS
    dispatch(s, "add_clip", {"track": "v1", "src": str(src), "in": 0, "out": 3, "start": 0})
    dispatch(s, "set_volume", {"target": _v1(s)[0].id, "db": 18.0})
    a = _pcm(render_export(s.edl, s.dir).path)
    assert _band_db(a, 440, 0.5, 2.5) == pytest.approx(20 * math.log10(0.03) + 18.0, abs=0.5)


# ------------------------------------------------------------------- solo

def _three_lane_store(tmp_path):
    s = _store(tmp_path / "s")
    v = _clip_with_tone(tmp_path / "v.mov", freq=440, amp=0.2, dur=4.0)
    m = _tone(tmp_path / "m.wav", freq=220, amp=0.2, dur=4.0)
    vo = _tone(tmp_path / "vo.wav", freq=1000, amp=0.2, dur=4.0)
    dispatch(s, "add_clip", {"track": "v1", "src": str(v), "in": 0, "out": 4, "start": 0})
    dispatch(s, "add_music", {"src": str(m), "start": 0, "in": 0, "out": 4, "duck": False, "volume_db": 0})
    dispatch(s, "add_clip", {"track": "vo", "src": str(vo), "in": 0, "out": 4, "start": 0})
    return s


def test_solo_hears_only_the_soloed_lane(tmp_path):
    s = _three_lane_store(tmp_path)
    r = dispatch(s, "set_track_solo", {"track": "vo"})
    assert r["solo"] is True and r["soloed"] == ["vo"]
    a = _pcm(render_export(s.edl, s.dir).path)
    assert _band_db(a, 1000, 0.5, 3.5) > -20.0              # the soloed voiceover
    assert _band_db(a, 440, 0.5, 3.5) < -80.0               # v1 sound, soloed out
    assert _band_db(a, 220, 0.5, 3.5) < -80.0               # music, soloed out
    # a second solo adds to the set; unsoloing both brings everything back
    dispatch(s, "set_track_solo", {"track": "music", "solo": True})
    a = _pcm(render_export(s.edl, s.dir).path)
    assert _band_db(a, 220, 0.5, 3.5) > -20.0 and _band_db(a, 440, 0.5, 3.5) < -80.0
    dispatch(s, "set_track_solo", {"track": "vo", "solo": False})
    dispatch(s, "set_track_solo", {"track": "music", "solo": False})
    a = _pcm(render_export(s.edl, s.dir).path)
    assert min(_band_db(a, f, 0.5, 3.5) for f in (220, 440, 1000)) > -20.0


def test_solo_is_refused_on_a_lane_without_sound(tmp_path):
    s = _store(tmp_path / "s")
    with pytest.raises(ValueError, match="no sound"):
        dispatch(s, "set_track_solo", {"track": "captions"})


# ------------------------------------------------------------ detach audio

def test_detach_audio_then_slip_it_for_an_l_cut(tmp_path):
    s = _store(tmp_path / "s")
    clap = make_clap(tmp_path / "clap.mp4", seconds=8, fps=FPS)
    dispatch(s, "add_clip", {"track": "v1", "src": str(clap), "in": 0, "out": 8, "start": 0})
    before = av_offsets_ms(render_export(s.edl, s.dir).path)

    r = dispatch(s, "detach_audio", {"clip_id": _v1(s)[0].id})
    lane = s.edl.get_track(r["track"])
    assert lane.type == "audio" and _v1(s)[0].audio.mute is True
    out = render_export(s.edl, s.dir).path
    after = av_offsets_ms(out)
    # identical until moved: every flash keeps its click, nothing doubled
    assert len(after) == len(before) >= 6
    assert max(abs(x) for x in after) <= 1.0, after
    peak = float(np.max(np.abs(_pcm(out))))
    assert peak < 0.9, f"picture sound and detached sound both playing: peak {peak}"

    # L-cut: the sound runs 0.3 s behind the picture
    dispatch(s, "move_clip", {"clip_id": r["audio_clip_id"], "new_start": 0.3})
    shifted = av_offsets_ms(render_export(s.edl, s.dir).path)
    assert len(shifted) >= 6 and all(abs(x - 300.0) <= 1.5 for x in shifted), shifted


def test_detach_audio_refuses_silent_or_retimed_clips(tmp_path):
    s = _store(tmp_path / "s")
    clap = make_clap(tmp_path / "clap.mp4", seconds=4, fps=FPS)
    dispatch(s, "add_clip", {"track": "v1", "src": str(clap), "in": 0, "out": 4, "start": 0})
    dispatch(s, "set_speed", {"clip_id": _v1(s)[0].id, "factor": 2.0})
    with pytest.raises(ValueError, match="1x"):
        dispatch(s, "detach_audio", {"clip_id": _v1(s)[0].id})


# ---------------------------------------------------------- volume keyframes

def test_volume_keyframes_ramp_the_v1_sound(tmp_path):
    s = _store(tmp_path / "s")
    src = _clip_with_tone(tmp_path / "t.mov", freq=440, amp=0.5, dur=4.0)
    dispatch(s, "add_clip", {"track": "v1", "src": str(src), "in": 0, "out": 4, "start": 0})
    cid = _v1(s)[0].id
    dispatch(s, "add_keyframe", {"clip_id": cid, "prop": "audio.gain_db", "time": 1.0, "value": -24.0})
    dispatch(s, "add_keyframe", {"clip_id": cid, "prop": "audio.gain_db", "time": 3.0, "value": 0.0})
    a = _pcm(render_export(s.edl, s.dir).path)
    ref = 20 * math.log10(0.5)
    assert _band_db(a, 440, 0.2, 0.9) - ref == pytest.approx(-24.0, abs=1.0)   # held before key 1
    assert _band_db(a, 440, 1.95, 2.05) - ref == pytest.approx(-12.0, abs=1.0)  # linear in dB
    assert _band_db(a, 440, 3.2, 3.9) - ref == pytest.approx(0.0, abs=1.0)      # held after key 2


def test_volume_keyframes_on_a_music_lane_are_in_clip_time(tmp_path):
    s = _store(tmp_path / "s")
    pic = _clip_with_tone(tmp_path / "p.mov", freq=440, amp=0.0, dur=8.0)
    bed = _tone(tmp_path / "bed.wav", freq=220, amp=0.5, dur=4.0)
    dispatch(s, "add_clip", {"track": "v1", "src": str(pic), "in": 0, "out": 8, "start": 0})
    dispatch(s, "add_music", {"src": str(bed), "start": 3.0, "in": 0, "out": 4, "duck": False, "volume_db": 0})
    mid = s.edl.get_track("music").clips[0].id
    dispatch(s, "add_keyframe", {"clip_id": mid, "prop": "audio.gain_db", "time": 0.0, "value": -30.0})
    dispatch(s, "add_keyframe", {"clip_id": mid, "prop": "audio.gain_db", "time": 2.0, "value": 0.0})
    a = _pcm(render_export(s.edl, s.dir).path)
    ref = 20 * math.log10(0.5)
    # clip-local 0.5 s = timeline 3.5 s; clip-local 3 s = timeline 6 s
    assert _band_db(a, 220, 3.45, 3.55) - ref == pytest.approx(-22.5, abs=1.5)
    assert _band_db(a, 220, 5.5, 6.5) - ref == pytest.approx(0.0, abs=1.0)


def test_a_split_keeps_the_volume_curve(tmp_path):
    s = _store(tmp_path / "s")
    src = _clip_with_tone(tmp_path / "t.mov", freq=440, amp=0.5, dur=4.0)
    dispatch(s, "add_clip", {"track": "v1", "src": str(src), "in": 0, "out": 4, "start": 0})
    cid = _v1(s)[0].id
    dispatch(s, "add_keyframe", {"clip_id": cid, "prop": "audio.gain_db", "time": 0.0, "value": -24.0})
    dispatch(s, "add_keyframe", {"clip_id": cid, "prop": "audio.gain_db", "time": 4.0, "value": 0.0})
    dispatch(s, "split_at", {"time": 2.0})
    a = _pcm(render_export(s.edl, s.dir).path)
    ref = 20 * math.log10(0.5)
    assert _band_db(a, 440, 2.95, 3.05) - ref == pytest.approx(-6.0, abs=1.0)
    assert _band_db(a, 440, 0.95, 1.05) - ref == pytest.approx(-18.0, abs=1.0)


def test_removing_the_last_volume_key_keeps_the_level(tmp_path):
    s = _store(tmp_path / "s")
    src = _clip_with_tone(tmp_path / "t.mov", freq=440, amp=0.5, dur=2.0)
    dispatch(s, "add_clip", {"track": "v1", "src": str(src), "in": 0, "out": 2, "start": 0})
    cid = _v1(s)[0].id
    dispatch(s, "add_keyframe", {"clip_id": cid, "prop": "audio.gain_db", "time": 1.0, "value": -9.0})
    dispatch(s, "remove_keyframe", {"clip_id": cid, "prop": "audio.gain_db", "time": 1.0})
    c = _v1(s)[0]
    assert c.audio.gain_env is None and c.audio.gain_db == pytest.approx(-9.0)


# ------------------------------------------------ varispeed (QA-039 residual)

@pytest.mark.parametrize("speed", [0.5, 1.5, 2.0])
def test_varispeed_puts_every_click_on_its_flash(tmp_path, speed):
    s = _store(tmp_path / f"v{speed}")
    clap = make_clap(tmp_path / "clap.mp4", seconds=16, fps=FPS)
    dispatch(s, "add_clip", {"track": "v1", "src": str(clap), "in": 0, "out": 16, "start": 0})
    dispatch(s, "split_at", {"time": 6.0})
    dispatch(s, "set_speed", {"clip_id": _v1(s)[1].id, "factor": speed, "keep_pitch": False})
    assert _v1(s)[1].audio.keep_pitch is False
    offs = av_offsets_ms(render_export(s.edl, s.dir).path)
    assert len(offs) >= 10, offs
    # sample-exact sound against a frame-exact picture: within 1 ms everywhere
    # (atempo's WSOLA: up to ±12 ms, one click in five at −17 ms at 0.5x)
    assert max(abs(o) for o in offs) <= 1.0, [round(o, 2) for o in offs]
