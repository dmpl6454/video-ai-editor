from video_ai_editor.edl import EDL, Track, Clip, empty_edl
from video_ai_editor.edl.schema import Canvas


def test_empty_edl_has_default_tracks():
    edl = empty_edl()
    track_ids = [t.id for t in edl.tracks]
    assert "v1" in track_ids
    assert "a1" in track_ids
    assert "captions" in track_ids


def test_edl_round_trip_preserves_hash():
    edl = empty_edl()
    h1 = edl.hash()
    j = edl.to_json()
    edl2 = EDL.model_validate_json(j)
    assert edl2.hash() == h1


def test_edl_hash_changes_when_clip_added():
    edl = empty_edl()
    h1 = edl.hash()
    edl.tracks[0].clips.append(Clip(src="x.mp4", in_=0.0, out=5.0, start=0.0))
    h2 = edl.hash()
    assert h1 != h2


def test_recompute_duration():
    edl = empty_edl()
    edl.tracks[0].clips.append(Clip(src="a.mp4", in_=0.0, out=3.0, start=0.0))
    edl.tracks[0].clips.append(Clip(src="b.mp4", in_=0.0, out=4.0, start=3.0))
    edl.recompute_duration()
    assert edl.duration == 7.0


def test_canvas_default_is_vertical():
    c = Canvas()
    assert c.w == 1080
    assert c.h == 1920
    assert c.fps == 30


def test_get_clip_returns_track_and_clip():
    edl = empty_edl()
    clip = Clip(src="x.mp4", in_=0.0, out=5.0, start=0.0)
    edl.tracks[0].clips.append(clip)
    result = edl.get_clip(clip.id)
    assert result is not None
    track, found = result
    assert track.id == "v1"
    assert found.id == clip.id
