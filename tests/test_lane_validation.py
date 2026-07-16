"""agent/dispatch.py — a media Clip can't be placed on a non-media-family
track (issues 41/42/43, "anything can be placed anywhere").

Regression coverage: add_clip/move_clip previously resolved a target track
by id with zero type checking. A video Clip dropped/dragged onto e.g. the
captions or stickers track landed there successfully at the data-model
level, but every render path (collect_text_clips, collect_stickers,
build_pip_overlay_chain, ...) only looks at its OWN track type — so the clip
silently vanished from all output with no error anywhere ("errors / does
nothing"). Sticker/TextClip moves between compatible tracks of their own
kind (e.g. tx_super -> tx_hook) must remain unrestricted.
"""
from __future__ import annotations
import tempfile
from pathlib import Path

import pytest

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Canvas, Track, Clip, TextClip, Transform
from video_ai_editor.agent.dispatch import dispatch


def _store_with_tracks(*extra_tracks: Track) -> EDLStore:
    tmp = tempfile.mkdtemp()
    edl = EDL(
        canvas=Canvas(w=1080, h=1920, fps=30),
        tracks=[Track(id="v1", type="video", clips=[]), *extra_tracks],
    )
    edl.recompute_duration()
    (Path(tmp) / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(Path(tmp))


def _src(store: EDLStore) -> str:
    return str(Path(store.dir) / "nonexistent" / "x.mp4")


def test_add_clip_to_a_captions_track_is_rejected():
    store = _store_with_tracks(Track(id="captions", type="captions", clips=[]))
    with pytest.raises(ValueError, match="captions.*can't hold a media clip"):
        dispatch(store, "add_clip", {
            "track": "captions", "src": _src(store), "in": 0.0, "out": 5.0, "start": 0.0,
        })


def test_add_clip_to_a_stickers_track_is_rejected():
    store = _store_with_tracks(Track(id="stickers", type="sticker", clips=[]))
    with pytest.raises(ValueError, match="can't hold a media clip"):
        dispatch(store, "add_clip", {
            "track": "stickers", "src": _src(store), "in": 0.0, "out": 5.0, "start": 0.0,
        })


def test_add_clip_to_video_audio_music_vo_tracks_all_succeed():
    store = _store_with_tracks(
        Track(id="a1", type="audio", clips=[]),
        Track(id="music", type="music", clips=[]),
        Track(id="vo", type="vo", clips=[]),
        Track(id="v2", type="video", clips=[]),
    )
    for track_id in ["v1", "v2", "a1", "music", "vo"]:
        r = dispatch(store, "add_clip", {
            "track": track_id, "src": _src(store), "in": 0.0, "out": 5.0, "start": 0.0,
        })
        assert "clip_id" in r


def test_move_clip_to_a_captions_track_is_rejected():
    store = _store_with_tracks(Track(id="captions", type="captions", clips=[]))
    r = dispatch(store, "add_clip", {
        "track": "v1", "src": _src(store), "in": 0.0, "out": 5.0, "start": 0.0,
    })
    cid = r["clip_id"]
    with pytest.raises(ValueError, match="can't hold a media clip"):
        dispatch(store, "move_clip", {"clip_id": cid, "new_track": "captions", "new_start": 2.0})
    # The clip must still be exactly where it was — a rejected move is a
    # no-op, not a partial move.
    assert store.edl.get_track("v1").clips[0].id == cid
    assert store.edl.get_track("captions").clips == []


def test_move_clip_between_video_tracks_still_works():
    store = _store_with_tracks(Track(id="v2", type="video", z=1, clips=[]))
    r = dispatch(store, "add_clip", {
        "track": "v1", "src": _src(store), "in": 0.0, "out": 5.0, "start": 0.0,
    })
    cid = r["clip_id"]
    dispatch(store, "move_clip", {"clip_id": cid, "new_track": "v2", "new_start": 3.0})
    assert store.edl.get_track("v1").clips == []
    assert store.edl.get_track("v2").clips[0].id == cid


def test_move_text_clip_between_text_tracks_is_unrestricted():
    """A TextClip moving between two text-type tracks (e.g. tx_super to
    tx_hook) is a legitimate, common operation and must NOT be blocked by
    the media-lane check that only applies to media Clips."""
    store = _store_with_tracks(
        Track(id="tx_super", type="text", clips=[
            TextClip(id="t1", text="HI", start=0.0, end=2.0,
                      transform=Transform(x=540, y=960), role="super"),
        ]),
        Track(id="tx_hook", type="text", clips=[]),
    )
    dispatch(store, "move_clip", {"clip_id": "t1", "new_track": "tx_hook", "new_start": 1.0})
    assert store.edl.get_track("tx_super").clips == []
    assert store.edl.get_track("tx_hook").clips[0].id == "t1"
