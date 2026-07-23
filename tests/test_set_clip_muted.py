"""set_clip_muted — a real clip-level mute that preserves gain_db.

Regression for tester issue 9: Properties.tsx's "Mute clip" checkbox used
`set_volume {db: -60}` as a mute proxy, which (a) never set `audio.mute` so
the checkbox never rendered checked, (b) clobbered the user's gain trim, and
(c) made unmuting via the checkbox impossible (every click re-sent -60).

The renderer already honors `audio.mute` on every path (volume=0 in both the
V1 clip chain and audio_mix._audio_clip_filter), so the fix is purely a
dispatch tool that flips the flag — WITHOUT touching gain_db, so a user's
-6 dB trim survives a mute/unmute cycle.

Also covers tester issue 10's backend defense-in-depth: color_grade /
set_clip_transform / add_effect raise ValueError when pointed at a clip on an
audio lane (music/vo/audio), where those edits are silent render no-ops.
"""
from __future__ import annotations
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Track, Clip, Canvas
from video_ai_editor.agent.dispatch import dispatch


# --- helpers (mean_volume pattern from tests/test_track_and_pip_audio.py) ---

def _mk_video(path: Path, *, freq: int = 440, duration: float = 2.0,
              color: str = "blue"):
    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi", "-i", f"color=c={color}:s=320x180:d={duration}:r=30",
         "-f", "lavfi", "-i", f"sine=f={freq}:duration={duration}",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True, capture_output=True,
    )


def _mk_audio(path: Path, *, freq: int = 200, duration: float = 2.0):
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"sine=f={freq}:duration={duration}",
         "-c:a", "aac", str(path)],
        check=True, capture_output=True,
    )


def _mean_volume(path: Path) -> float:
    proc = subprocess.run(
        ["ffmpeg", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", proc.stderr)
    assert m, f"volumedetect produced no mean_volume:\n{proc.stderr[-1500:]}"
    return float(m.group(1))


def _store(tmp_path: Path, *, with_music: bool = False) -> EDLStore:
    src = tmp_path / "src.mp4"
    _mk_video(src)
    tracks = [
        Track(id="v1", type="video", clips=[
            Clip(src=str(src), in_=0, out=2, start=0, id="c1"),
        ]),
    ]
    if with_music:
        music = tmp_path / "music.m4a"
        _mk_audio(music)
        tracks.append(Track(id="music", type="music", clips=[
            Clip(src=str(music), in_=0, out=2, start=0, id="m1"),
        ]))
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=tracks)
    edl.recompute_duration()
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(tmp_path)


# ------------------------------- unit tests --------------------------------

def test_set_clip_muted_sets_edl_mute_flag(tmp_path: Path):
    store = _store(tmp_path)
    res = dispatch(store, "set_clip_muted", {"clip_id": "c1", "muted": True})
    _, c = store.edl.get_clip("c1")
    assert c.audio.mute is True
    assert res["muted"] is True
    # Persisted: a fresh store sees the flag.
    fresh = EDLStore(tmp_path)
    _, c2 = fresh.edl.get_clip("c1")
    assert c2.audio.mute is True


def test_set_clip_muted_toggle_semantics_when_muted_omitted(tmp_path: Path):
    store = _store(tmp_path)
    _, c = store.edl.get_clip("c1")
    assert c.audio.mute is False  # schema default

    r1 = dispatch(store, "set_clip_muted", {"clip_id": "c1"})
    assert c.audio.mute is True and r1["muted"] is True

    r2 = dispatch(store, "set_clip_muted", {"clip_id": "c1"})
    assert c.audio.mute is False and r2["muted"] is False


def test_unmute_preserves_gain_db(tmp_path: Path):
    """The whole reason the -60 dB set_volume proxy was wrong: a user's
    gain trim must survive a mute/unmute cycle untouched."""
    store = _store(tmp_path)
    dispatch(store, "set_volume", {"target": "c1", "db": -6.0})
    _, c = store.edl.get_clip("c1")
    assert c.audio.gain_db == pytest.approx(-6.0)

    dispatch(store, "set_clip_muted", {"clip_id": "c1", "muted": True})
    assert c.audio.mute is True
    assert c.audio.gain_db == pytest.approx(-6.0), "mute must not touch gain"

    dispatch(store, "set_clip_muted", {"clip_id": "c1", "muted": False})
    assert c.audio.mute is False
    assert c.audio.gain_db == pytest.approx(-6.0), "unmute must not touch gain"


def test_set_clip_muted_unknown_clip_raises(tmp_path: Path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="not found"):
        dispatch(store, "set_clip_muted", {"clip_id": "nope"})


def test_set_clip_muted_rejects_text_clip(tmp_path: Path):
    store = _store(tmp_path)
    dispatch(store, "add_super_text", {"text": "HI", "start": 0.0, "end": 1.0})
    tid = next(
        c.id for t in store.edl.tracks for c in t.clips
        if not isinstance(c, Clip)
    )
    with pytest.raises(ValueError, match="media clip"):
        dispatch(store, "set_clip_muted", {"clip_id": tid})


def test_set_clip_muted_commits_an_op(tmp_path: Path):
    store = _store(tmp_path)
    dispatch(store, "set_clip_muted", {"clip_id": "c1", "muted": True})
    assert store.ops.last().tool == "set_clip_muted"


# --------------------------- integration render ----------------------------

@pytest.mark.parametrize("keep_video_cache", [True, False],
                         ids=["remux-path", "full-render-path"])
def test_muted_v1_clip_is_silent_but_music_still_audible(
        tmp_path: Path, keep_video_cache: bool):
    """v1 clip muted + music track present → music survives the mix, the v1
    tone drops out. Guards the actual render behavior, not just the flag."""
    from video_ai_editor.render import render_preview

    store = _store(tmp_path, with_music=True)
    loud = _mean_volume(render_preview(store.edl, tmp_path, height=180).path)

    dispatch(store, "set_clip_muted", {"clip_id": "c1", "muted": True})
    if not keep_video_cache:
        shutil.rmtree(tmp_path / "cache" / "videos", ignore_errors=True)
    after = _mean_volume(render_preview(store.edl, tmp_path, height=180).path)

    # Music (200 Hz sine) is still in the mix, so the output is NOT silent —
    # it just lost the v1 tone. Assert quieter-than-before but far from the
    # noise floor (a fully-silent file volumedetects around -91 dB).
    assert after <= loud - 2, f"v1 clip mute inaudible: {loud=} {after=}"
    assert after > -60, f"music vanished too — whole mix went silent: {after=}"

    # And with NO music, muting the only clip drives the mix to the floor.
    music_track = store.edl.get_track("music")
    music_track.muted = True
    shutil.rmtree(tmp_path / "cache" / "videos", ignore_errors=True)
    silent = _mean_volume(render_preview(store.edl, tmp_path, height=180).path)
    assert silent <= loud - 25, f"muted-only-clip mix not silent: {silent=}"


# ------------------- issue 10: audio-lane guard (backend) -------------------

@pytest.mark.parametrize("tool,args", [
    ("color_grade", {"brightness": 0.1}),
    ("set_clip_transform", {"scale": 1.5}),
    ("add_effect", {"type": "blur", "params": {"radius": 4}}),
])
def test_video_only_tools_reject_audio_lane_clips(tmp_path: Path, tool, args):
    """Silent no-op → loud error: the audio render path ignores effects and
    transform entirely, so pointing these tools at a music/vo clip must fail
    instead of committing dead data."""
    store = _store(tmp_path, with_music=True)
    with pytest.raises(ValueError, match="audio lane"):
        dispatch(store, tool, {"clip_id": "m1", **args})
    # Nothing committed to the music clip.
    _, m = store.edl.get_clip("m1")
    assert not m.effects
    assert m.transform.scale == 1.0


def test_video_only_tools_still_work_on_video_clips(tmp_path: Path):
    store = _store(tmp_path, with_music=True)
    dispatch(store, "color_grade", {"clip_id": "c1", "brightness": 0.1})
    dispatch(store, "set_clip_transform", {"clip_id": "c1", "scale": 1.2})
    dispatch(store, "add_effect", {"clip_id": "c1", "type": "blur",
                                   "params": {"radius": 4}})
    _, c = store.edl.get_clip("c1")
    assert len(c.effects) == 2  # color + blur
    assert c.transform.scale == pytest.approx(1.2)


def test_color_grade_v1_wide_no_clip_id_still_works(tmp_path: Path):
    """color_grade with NO clip_id targets every v1 clip — the guard must not
    break the track-wide path (music clips aren't in its target set anyway)."""
    store = _store(tmp_path, with_music=True)
    res = dispatch(store, "color_grade", {"brightness": 0.05})
    assert res["applied_to"] == ["c1"]
    _, m = store.edl.get_clip("m1")
    assert not m.effects
