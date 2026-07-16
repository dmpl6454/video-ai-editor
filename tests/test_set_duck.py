"""agent/dispatch.py — set_duck toggles ONLY the duck flag (issues 38/39).

Before this tool existed, the only way to flip music ducking was
MediaBin.tsx's checkbox re-adding the music clip via add_music(duck=...) then
ripple_delete-ing the original — which re-probed the FULL source duration and
reset start/in/out to 0/0/full-length, discarding any trim or repositioning
the user had already done ("clicking Duck expands the split video"). The
ripple_delete + re-add could also land on a clip id the panel's closure
hadn't captured, so a second toggle would silently target a stale/missing
clip and appear to do nothing.
"""
from __future__ import annotations
import tempfile
from pathlib import Path

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Canvas, Track, Clip, AudioProps
from video_ai_editor.agent.dispatch import dispatch


def _store_with_trimmed_music_clip() -> EDLStore:
    tmp = tempfile.mkdtemp()
    src = str(Path(tmp) / "nonexistent" / "music.mp3")
    edl = EDL(
        canvas=Canvas(w=1080, h=1920, fps=30),
        tracks=[
            Track(id="v1", type="video", clips=[]),
            Track(id="music", type="music", clips=[
                # Deliberately trimmed + repositioned — NOT the default
                # start=0/in=0/out=full-duration an add_music() re-probe
                # would reset it to.
                Clip(src=src, in_=5.0, out=20.0, start=8.0, id="m1",
                     audio=AudioProps(gain_db=-9.0)),
            ]),
        ],
    )
    edl.recompute_duration()
    (Path(tmp) / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(Path(tmp))


def _music_track(store: EDLStore):
    return store.edl.get_track("music")


def test_set_duck_enables_without_touching_clip_position():
    store = _store_with_trimmed_music_clip()
    dispatch(store, "set_duck", {"track": "music", "enabled": True})
    track = _music_track(store)
    assert track.duck is not None
    assert track.duck.to_db == -18.0
    clip = track.clips[0]
    assert clip.id == "m1"
    assert clip.start == 8.0 and clip.in_ == 5.0 and clip.out == 20.0
    assert clip.audio.gain_db == -9.0


def test_set_duck_disables_without_touching_clip_position():
    store = _store_with_trimmed_music_clip()
    dispatch(store, "set_duck", {"track": "music", "enabled": True})
    dispatch(store, "set_duck", {"track": "music", "enabled": False})
    track = _music_track(store)
    assert track.duck is None
    clip = track.clips[0]
    assert clip.id == "m1"
    assert clip.start == 8.0 and clip.in_ == 5.0 and clip.out == 20.0


def test_set_duck_toggles_when_enabled_is_omitted():
    store = _store_with_trimmed_music_clip()
    assert _music_track(store).duck is None
    dispatch(store, "set_duck", {"track": "music"})
    assert _music_track(store).duck is not None
    dispatch(store, "set_duck", {"track": "music"})
    assert _music_track(store).duck is None


def test_set_duck_repeated_toggles_never_lose_the_clip():
    """The exact regression: toggling on/off/on/off repeatedly must not
    silently no-op or duplicate/remove the clip — issue 39's 'clicking again
    does nothing, stays expanded'."""
    store = _store_with_trimmed_music_clip()
    for _ in range(4):
        dispatch(store, "set_duck", {"track": "music"})
    track = _music_track(store)
    assert len(track.clips) == 1
    assert track.clips[0].id == "m1"
    assert track.clips[0].start == 8.0


def test_set_duck_custom_to_db_and_track_ref():
    store = _store_with_trimmed_music_clip()
    dispatch(store, "set_duck", {"track": "music", "enabled": True, "to_db": -24.0, "track_ref": "vo"})
    duck = _music_track(store).duck
    assert duck.to_db == -24.0
    assert duck.track_ref == "vo"


def test_set_duck_missing_track_raises():
    store = _store_with_trimmed_music_clip()
    import pytest
    with pytest.raises(ValueError, match="not found"):
        dispatch(store, "set_duck", {"track": "does_not_exist"})
