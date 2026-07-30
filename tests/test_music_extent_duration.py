"""A music bed must not stretch `edl.duration` past the video (RC-DUR).

The upload path used to size the music clip with

    out = min(p.duration, max(edl.duration, p.duration))

which is the algebraic identity `min(d, max(x, d)) == d` for every x — so it
ALWAYS returned the full song length and its "trim to project duration" comment
described behaviour that never existed. A 29s video plus a 6:13 track therefore
made `edl.duration` 373.71s, and both the transport and the rendered file ran
minutes past the last frame of video. Reported independently against the browser
build and the desktop app as "the playback timer keeps running after the clip
finishes", and it also masked the speed fix (a 2x clip could not shrink a total
that was pinned to the song).

`add_music`'s own `out<=0` default had the same defect by a different route, and
that one is reachable from Claude and from MCP as well as the UI.
"""
from __future__ import annotations
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Canvas, Clip, empty_edl

VIDEO_LEN = 5.0
SONG_LEN = 60.0


def _mk_video(path: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=30:duration={VIDEO_LEN}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True)
    return path


def _mk_song(path: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"sine=f=440:duration={SONG_LEN}",
         "-c:a", "libmp3lame", str(path)],
        check=True, capture_output=True)
    return path


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> tuple[Path, Path]:
    d = tmp_path_factory.mktemp("dur_media")
    return _mk_video(d / "v.mp4"), _mk_song(d / "song.mp3")


def _store(tmp_path: Path, video: Path | None) -> EDLStore:
    edl = empty_edl(Canvas(w=320, h=180, fps=30))
    if video is not None:
        edl.get_track("v1").clips.append(
            Clip(src=str(video), in_=0.0, out=VIDEO_LEN, start=0.0))
    edl.recompute_duration()
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(tmp_path)


def test_music_is_trimmed_to_the_video_extent(tmp_path, media):
    video, song = media
    store = _store(tmp_path, video)
    dispatch(store, "add_music", {"src": str(song)})
    music = store.edl.get_track("music").clips[0]
    assert music.out == pytest.approx(VIDEO_LEN), "music bed should stop with the video"
    assert store.edl.duration == pytest.approx(VIDEO_LEN), (
        "edl.duration drives the transport and the rendered file length")


def test_music_only_timeline_keeps_the_whole_song(tmp_path, media):
    """No video yet (music-first workflow) → trimming to 0 would be wrong."""
    _, song = media
    store = _store(tmp_path, None)
    dispatch(store, "add_music", {"src": str(song)})
    music = store.edl.get_track("music").clips[0]
    assert music.out == pytest.approx(SONG_LEN, abs=0.5)


def test_explicit_out_still_wins(tmp_path, media):
    """A deliberately chosen bed length must not be clamped away."""
    video, song = media
    store = _store(tmp_path, video)
    dispatch(store, "add_music", {"src": str(song), "out": 30.0})
    assert store.edl.get_track("music").clips[0].out == pytest.approx(30.0)


def test_speeding_a_clip_shrinks_the_total(tmp_path, media):
    """P11: with no music pinning it, 2x speed must halve the reported total."""
    video, _ = media
    store = _store(tmp_path, video)
    cid = store.edl.get_track("v1").clips[0].id
    dispatch(store, "set_speed", {"clip_id": cid, "factor": 2.0})
    assert store.edl.duration == pytest.approx(VIDEO_LEN / 2, abs=0.05)


def test_video_extent_ignores_other_tracks(tmp_path, media):
    """`video_extent()` is the V1 length; `duration` is the max over all tracks."""
    video, song = media
    store = _store(tmp_path, video)
    dispatch(store, "add_music", {"src": str(song), "out": 30.0})
    assert store.edl.video_extent() == pytest.approx(VIDEO_LEN)
    assert store.edl.duration == pytest.approx(30.0)


def test_video_extent_is_speed_aware(tmp_path, media):
    video, _ = media
    store = _store(tmp_path, video)
    cid = store.edl.get_track("v1").clips[0].id
    dispatch(store, "set_speed", {"clip_id": cid, "factor": 2.0})
    assert store.edl.video_extent() == pytest.approx(VIDEO_LEN / 2, abs=0.05)
