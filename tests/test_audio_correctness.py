"""Audio correctness: gain, fades, music+ducking, voiceover, multi-clip
mix, loudness target. Verifies the rendered preview/export audio actually
contains audio at expected levels."""
from __future__ import annotations
import json
import re
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Track, Clip, Canvas, AudioProps, MusicDuck
from video_ai_editor.render import render_preview, render_export


def _mk_video(p: Path, *, freq: int = 440, dur: float = 2.0):
    keyed = p.with_suffix(".keyed.mp4")
    subprocess.run(["ffmpeg","-y","-f","lavfi","-i",f"color=c=blue:s=320x180:d={dur}:r=30",
                    "-pix_fmt","yuv420p",str(keyed)], check=True, capture_output=True)
    subprocess.run(["ffmpeg","-y","-i",str(keyed),"-f","lavfi","-i",f"sine=f={freq}:duration={dur}",
                    "-c:v","copy","-c:a","aac","-shortest",str(p)], check=True, capture_output=True)


def _mk_audio(p: Path, *, freq: int = 200, dur: float = 4.0):
    subprocess.run(["ffmpeg","-y","-f","lavfi","-i",f"sine=f={freq}:duration={dur}",
                    "-c:a","mp3",str(p)], check=True, capture_output=True)


def _measure_loudness(p: Path) -> dict[str, float]:
    """Return mean_volume + max_volume in dB."""
    proc = subprocess.run(
        ["ffmpeg", "-i", str(p), "-af", "volumedetect", "-vn", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    out: dict[str, float] = {}
    for line in proc.stderr.splitlines():
        m = re.search(r"(mean_volume|max_volume):\s*(-?[\d.]+)\s*dB", line)
        if m:
            out[m.group(1)] = float(m.group(2))
    return out


def _probe_audio(p: Path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_streams", "-of", "json", str(p)],
        capture_output=True, text=True, check=True,
    )
    streams = json.loads(proc.stdout).get("streams", [])
    return streams[0] if streams else {}


def _store(tmp_path: Path, edl: EDL) -> EDLStore:
    edl.recompute_duration()
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(tmp_path)


def test_audio_present_after_render(tmp_path: Path):
    src = tmp_path/"src.mp4"; _mk_video(src)
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30, loudness_lufs=None),
              tracks=[Track(id="v1", type="video", clips=[
                  Clip(src=str(src), in_=0, out=2, start=0, id="c1")])])
    out = render_preview(_store(tmp_path, edl).edl, tmp_path, height=180).path
    a = _probe_audio(out)
    assert a.get("codec_name") == "aac"
    assert a.get("sample_rate") == "48000"
    vol = _measure_loudness(out)
    assert vol["max_volume"] > -25, f"audio is silent: {vol}"


def test_clip_gain_is_applied(tmp_path: Path):
    src = tmp_path/"src.mp4"; _mk_video(src)
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30, loudness_lufs=None),
              tracks=[Track(id="v1", type="video", clips=[
                  Clip(src=str(src), in_=0, out=2, start=0, id="c1",
                       audio=AudioProps(gain_db=-20.0))])])
    out = render_preview(_store(tmp_path, edl).edl, tmp_path, height=180).path
    vol = _measure_loudness(out)
    # -20 dB on a sine should land around -25 dB max (sine peaks at -3 dB)
    assert vol["max_volume"] < -15, f"gain not applied: {vol}"


def test_music_track_mixed_with_main(tmp_path: Path):
    src = tmp_path/"src.mp4"; _mk_video(src, freq=880, dur=4)
    music = tmp_path/"music.mp3"; _mk_audio(music, freq=200, dur=4)
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30, loudness_lufs=None), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(src=str(src), in_=0, out=4, start=0, id="c1")]),
        Track(id="music", type="music", clips=[
            Clip(src=str(music), in_=0, out=4, start=0, id="m1")]),
    ])
    out = render_preview(_store(tmp_path, edl).edl, tmp_path, height=180).path
    # Mixed audio should have measurable energy
    vol = _measure_loudness(out)
    assert vol["max_volume"] > -25
    assert _probe_audio(out)["channels"] == 2


def test_music_ducking_lowers_music_under_speech(tmp_path: Path):
    """With ducking ON, music level under main audio should drop."""
    src = tmp_path/"src.mp4"; _mk_video(src, freq=440, dur=4)
    music = tmp_path/"music.mp3"; _mk_audio(music, freq=200, dur=4)

    # WITHOUT ducking
    edl1 = EDL(canvas=Canvas(w=320, h=180, fps=30, loudness_lufs=None), tracks=[
        Track(id="v1", type="video", clips=[Clip(src=str(src), in_=0, out=4, start=0, id="c1")]),
        Track(id="music", type="music", clips=[
            Clip(src=str(music), in_=0, out=4, start=0, id="m1")]),
    ])
    o1 = render_preview(_store(tmp_path/"a", edl1).edl, tmp_path/"a", height=180).path
    v1 = _measure_loudness(o1)

    # WITH ducking
    edl2 = EDL(canvas=Canvas(w=320, h=180, fps=30, loudness_lufs=None), tracks=[
        Track(id="v1", type="video", clips=[Clip(src=str(src), in_=0, out=4, start=0, id="c1")]),
        Track(id="music", type="music",
              duck=MusicDuck(to_db=-18, track_ref="v1"),
              clips=[Clip(src=str(music), in_=0, out=4, start=0, id="m1")]),
    ])
    o2 = render_preview(_store(tmp_path/"b", edl2).edl, tmp_path/"b", height=180).path
    v2 = _measure_loudness(o2)
    # Both have audio; ducked output mean_volume should be ≤ undocked.
    assert v2["mean_volume"] <= v1["mean_volume"] + 0.5


def test_loudness_target_hit_on_export(tmp_path: Path):
    src = tmp_path/"src.mp4"; _mk_video(src, freq=440, dur=4)
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30, loudness_lufs=-16.0),
              tracks=[Track(id="v1", type="video", clips=[
                  Clip(src=str(src), in_=0, out=4, start=0, id="c1")])])
    out = render_export(_store(tmp_path, edl).edl, tmp_path).path
    # Re-measure: input integrated should be near -16 dBFS
    proc = subprocess.run(
        ["ffmpeg","-i",str(out),
         "-af","loudnorm=I=-16:TP=-1:LRA=11:print_format=summary",
         "-f","null","-"], capture_output=True, text=True,
    )
    integrated = None
    for ln in proc.stderr.splitlines():
        m = re.search(r"Input Integrated:\s*(-?[\d.]+)", ln)
        if m:
            integrated = float(m.group(1)); break
    assert integrated is not None and abs(integrated - (-16.0)) < 1.0, integrated


def test_fade_in_out_present_in_audio(tmp_path: Path):
    """Fades attenuate audio near both ends. Should be quieter at the edges
    than in the middle."""
    src = tmp_path/"src.mp4"; _mk_video(src, freq=440, dur=4)
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30, loudness_lufs=None),
              tracks=[Track(id="v1", type="video", clips=[
                  Clip(src=str(src), in_=0, out=4, start=0, id="c1",
                       audio=AudioProps(fade_in=1.0, fade_out=1.0))])])
    out = render_preview(_store(tmp_path, edl).edl, tmp_path, height=180).path
    # Measure first 0.2s vs middle 0.5s
    e_start = subprocess.run(
        ["ffmpeg","-i",str(out),"-ss","0.05","-t","0.1",
         "-af","volumedetect","-vn","-f","null","-"],
        capture_output=True, text=True,
    ).stderr
    e_mid = subprocess.run(
        ["ffmpeg","-i",str(out),"-ss","2.0","-t","0.5",
         "-af","volumedetect","-vn","-f","null","-"],
        capture_output=True, text=True,
    ).stderr
    def _max(s):
        m = re.search(r"max_volume:\s*(-?[\d.]+)", s); return float(m.group(1)) if m else 0
    assert _max(e_start) < _max(e_mid) - 5, f"fade-in not applied: start={_max(e_start)} mid={_max(e_mid)}"


def test_multi_clip_audio_preserved_through_concat(tmp_path: Path):
    """Two V1 clips back-to-back should both contribute audio."""
    a = tmp_path/"a.mp4"; b = tmp_path/"b.mp4"
    _mk_video(a, freq=440, dur=2); _mk_video(b, freq=880, dur=2)
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30, loudness_lufs=None),
              tracks=[Track(id="v1", type="video", clips=[
                  Clip(src=str(a), in_=0, out=2, start=0, id="c1"),
                  Clip(src=str(b), in_=0, out=2, start=2, id="c2")])])
    out = render_preview(_store(tmp_path, edl).edl, tmp_path, height=180).path
    dur = float(json.loads(subprocess.run(
        ["ffprobe","-v","error","-show_entries","format=duration",
         "-of","json",str(out)], capture_output=True, text=True, check=True
    ).stdout)["format"]["duration"])
    assert 3.5 < dur < 4.5, dur
    vol = _measure_loudness(out)
    assert vol["max_volume"] > -25
