"""Regression test for the 'audio doesn't play with video on the timeline' bug.

The renderer used to apply `loudnorm` on the preview path. loudnorm operates
internally at 192 kHz; the AAC encoder downsampled to 96 kHz, which Safari
(and a couple of Chromium configurations on macOS) silently dropped on
playback inside an mp4 container. The fix:
  1. Skip loudnorm on the preview path entirely — only export needs LUFS.
  2. Force `aresample=48000` after every loudnorm so even export comes out at
     the canonical sample rate the AAC encoder is happiest with.

This test asserts that after a video + music import, the rendered preview is
plain 48 kHz stereo AAC.
"""
from __future__ import annotations
import json
import subprocess
import tempfile
from pathlib import Path

import pytest


def _ffprobe_audio_stream(p: Path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_streams", "-of", "json", str(p)],
        capture_output=True, text=True, check=True,
    )
    streams = json.loads(proc.stdout).get("streams", [])
    return streams[0] if streams else {}


def _measure_volume_db(p: Path) -> float | None:
    proc = subprocess.run(
        ["ffmpeg", "-i", str(p), "-af", "volumedetect",
         "-vn", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    for line in proc.stderr.splitlines():
        if "mean_volume" in line:
            try:
                return float(line.split("mean_volume:")[1].strip().split()[0])
            except Exception:
                return None
    return None


def test_preview_audio_is_48k_after_music_import(tmp_path: Path):
    from video_ai_editor.edl import EDLStore
    from video_ai_editor.edl.schema import EDL, Track, Clip, Canvas
    from video_ai_editor.agent.dispatch import dispatch
    from video_ai_editor.render import render_preview

    # Build a 4-second video with audio
    src = tmp_path / "src.mp4"
    keyed = tmp_path / "k.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "color=c=blue:s=320x180:d=4:r=30",
        "-pix_fmt", "yuv420p", str(keyed),
    ], check=True, capture_output=True)
    subprocess.run([
        "ffmpeg", "-y", "-i", str(keyed),
        "-f", "lavfi", "-i", "sine=f=440:duration=4",
        "-c:v", "copy", "-c:a", "aac", "-shortest", str(src),
    ], check=True, capture_output=True)

    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video",
              clips=[Clip(src=str(src), in_=0, out=4, start=0, id="c1")])
    ])
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)

    # Import an audio file as music
    music = tmp_path / "music.mp3"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=f=200:duration=8",
        "-c:a", "mp3", str(music),
    ], check=True, capture_output=True)
    dispatch(store, "add_music", {
        "src": str(music), "start": 0.0, "in": 0.0, "out": 4.0,
        "duck": True, "volume_db": -12.0,
    })

    r = render_preview(store.edl, tmp_path, height=180)
    assert r.path.exists() and r.path.stat().st_size > 0

    audio = _ffprobe_audio_stream(r.path)
    assert audio.get("codec_name") == "aac", \
        f"expected AAC audio, got {audio.get('codec_name')}"
    assert int(audio.get("sample_rate", 0)) == 48000, \
        f"preview audio must be 48 kHz; got {audio.get('sample_rate')}"
    assert int(audio.get("channels", 0)) == 2, \
        f"preview audio must be stereo; got {audio.get('channels')}"

    vol = _measure_volume_db(r.path)
    assert vol is not None, "no volume measurement returned"
    assert vol > -60, f"preview audio is silent (mean_volume={vol} dB)"
