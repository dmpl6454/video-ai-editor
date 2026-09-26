"""Keep-pitch speed clips must export (the atempo timestamp bug, wave D2).

Found by the wave D1 parity lane and reproduced on 0.7.3: on a 29.97 project,
three keep-pitch clips in a row (0.3x, 0.8x, 0.8x) made the single-pass export
fail outright with ffmpeg's "Invalid data found when processing input" /
"Conversion failed!". Every rate-matrix golden timeline at 29.97 failed the same
way with keep pitch on. Keep pitch is the default for a speed change, and the
single pass is what an ordinary export uses (and every timeline with
transitions), so a user who slowed a few clips could not export at all.

Cause: ffmpeg 8.1's `adelay` + `atempo` (the centring lag in front of each WSOLA
stage, `audio_mix.ATEMPO_LAG`) emits frames whose pts derive from
AV_NOPTS_VALUE, and the v1 `concat` rejects them. The chain now restamps after
every atempo stage (`asetpts=N/SR/TB`): WSOLA output is contiguous, so the
restamp changes no sample, only the timestamps concat reads.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from video_ai_editor import platformutil as _pu
from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl import timebase as tb
from video_ai_editor.edl.schema import Canvas, empty_edl
from video_ai_editor.render import audio_mix, render_export

SR = 48000


def _ff(args: list[str]) -> None:
    subprocess.run([_pu.FFMPEG, "-y", "-v", "error", *args], check=True, capture_output=True)


@pytest.fixture(scope="module")
def tone_src(tmp_path_factory) -> Path:
    """30 fps picture with a continuous 440 Hz tone (every import has sound)."""
    p = tmp_path_factory.mktemp("kp") / "tone30.mp4"
    _ff(["-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=20", "-f", "lavfi", "-i",
         "sine=f=440:sample_rate=48000:duration=20", "-pix_fmt", "yuv420p", "-c:v", "libx264",
         "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(p)])
    return p


def _store(sd: Path, fps) -> EDLStore:
    sd.mkdir(parents=True, exist_ok=True)
    e = empty_edl()
    e.canvas = Canvas(w=320, h=180, fps=fps)
    e.canvas.loudness_lufs = None
    (sd / "edl.json").write_text(e.model_dump_json())
    return EDLStore(sd)


def _streams(p: Path) -> dict[str, dict]:
    out = subprocess.run([_pu.FFPROBE, "-v", "error", "-count_frames", "-show_streams", "-of", "json",
                          str(p)], capture_output=True, text=True, check=True).stdout
    return {s["codec_type"]: s for s in json.loads(out)["streams"]}


# (project fps, [(source in, source seconds, speed)]). The first row is the
# measured minimal failing timeline; the others are what an editor does: slow
# a few shots of a 29.97 / 23.976 / 59.94 phone or camera project.
CASES = [
    (29.97, [(13.6, 0.30, 0.3), (14.4, 0.33, 0.8), (14.4, 0.33, 0.8)]),
    (29.97, [(0.0, 2.0, 0.5), (3.0, 1.5, 0.8), (5.0, 1.5, 0.8), (7.0, 2.0, 1.5)]),
    (23.976, [(1.0, 0.30, 0.3), (2.0, 0.33, 0.8), (3.0, 0.33, 0.8)]),
    (59.94, [(1.0, 0.30, 0.3), (2.0, 0.33, 0.8), (3.0, 0.33, 0.8)]),
]


@pytest.mark.parametrize("fps,rows", CASES, ids=[f"{c[0]}-{i}" for i, c in enumerate(CASES)])
def test_keep_pitch_speed_clips_export(tmp_path, tone_src, fps, rows):
    s = _store(tmp_path / "s", fps)
    cursor = 0.0
    for in_, dur, speed in rows:
        dispatch(s, "add_clip", {"track": "v1", "src": str(tone_src), "in": in_, "out": in_ + dur,
                                 "start": cursor})
        cid = s.edl.get_track("v1").clips[-1].id
        # keep_pitch left at its default (True): the path every user is on
        dispatch(s, "set_speed", {"clip_id": cid, "factor": speed})
        clip = s.edl.get_clip(cid)[1]
        assert clip.audio.keep_pitch is True
        cursor = float(clip.start) + clip.effective_duration
    out = render_export(s.edl, s.dir).path
    st = _streams(out)
    assert "audio" in st and "video" in st, st.keys()
    want_frames = tb.frame_of(s.edl.duration, fps)
    assert abs(int(st["video"]["nb_read_frames"]) - want_frames) <= 1, (st["video"]["nb_read_frames"], want_frames)
    # the sound runs the whole programme, not cut off at the first retimed clip
    assert float(st["audio"]["duration"]) == pytest.approx(s.edl.duration, abs=0.05)


def test_every_atempo_stage_is_restamped():
    """The rule itself, so a later edit of `speed_filters` cannot drop it: each
    atempo stage (0.3x needs two) is followed by a restamp."""
    from video_ai_editor.edl.schema import Clip
    c = Clip(src="x.mp4", start=0.0, speed=0.3, **{"in": 0.0, "out": 1.0})
    chain = audio_mix.speed_filters(c).lstrip(",").split(",")
    stages = [i for i, f in enumerate(chain) if f.startswith("atempo=")]
    assert len(stages) == 2, chain
    for i in stages:
        assert chain[i + 1] == "asetpts=N/SR/TB", chain
    c.audio.keep_pitch = False
    assert "asetpts" not in audio_mix.speed_filters(c)
