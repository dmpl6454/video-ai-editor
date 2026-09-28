"""Final QA round 2 (prompt-assistant-backend): main-lane edits keep what
belongs to a picture WITH that picture.

* A clip's DETACHED sound (detach_audio) stayed at its old time when an
  earlier main-track clip was trimmed, deleted, slowed, frozen, duplicated or
  dragged, so the export played it up to 2 s away from its picture. The sound
  now records `linked_to` and keeps its offset from the picture across every
  main-lane edit (an L/J-cut offset included).
* A PIP (a media clip on an overlay video lane) stayed put while the text
  over the same picture followed the ripple — the PIP now follows too.
* move_clip (the Timeline drag, close_gap) left v1 transitions at fixed
  times: they landed on the wrong cut or on no seam at all.
* History printed every added clip as "(0.00–5.00)" — the SOURCE range —
  whatever its place on the timeline.
* 25 fps: typed half-frame times (3.5 / 4.5 s) round apart, so appending at
  the previous clip's typed end split it and left a one-frame sliver of the
  wrong shot at the end.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Canvas, Clip, TextClip, Track, Transition

W, H, FPS = 160, 90, 30


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> str:
    dst = tmp_path_factory.mktemp("media") / "tone.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:d=12:r={FPS}",
                    "-f", "lavfi", "-i", "sine=f=440:d=12",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(dst)],
                   check=True, capture_output=True)
    return str(dst)


def _store(tmp: Path, src: str, *, durs=(2.0, 4.0), fps=FPS, extra=()) -> EDLStore:
    clips, t = [], 0.0
    for i, d in enumerate(durs):
        clips.append(Clip(id="abcd"[i], src=src, in_=0.0, out=d, start=t))
        t += d
    edl = EDL(canvas=Canvas(w=W, h=H, fps=fps, loudness_lufs=None), tracks=[
        Track(id="v1", type="video", clips=clips),
        Track(id="v2", type="video", z=1),
        Track(id="tx", type="text", z=3),
        Track(id="a1", type="audio", z=0),
        *extra,
    ])
    edl.recompute_duration()
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(tmp)


def _clip(store: EDLStore, cid: str):
    res = store.edl.get_clip(cid)
    assert res is not None, cid
    return res[1]


def _v1(store: EDLStore) -> list[Clip]:
    return sorted((c for c in store.edl.get_track("v1").clips if isinstance(c, Clip)),
                  key=lambda c: c.start)


# ----------------------------------------------------------- History summary

def test_add_clip_summary_names_its_timeline_place_not_the_source_range(tmp_path, media):
    store = _store(tmp_path / "s", media, durs=())
    summaries = [dispatch(store, "add_clip", {"track": "v1", "src": media, "in": 0, "out": 5,
                                              "start": 5.0 * i})["summary"] for i in range(4)]
    for i, s in enumerate(summaries):
        assert f"at {5.0 * i:.2f}s" in s, s
        assert "(0.00–5.00)" not in s, s
    assert len(set(s.split(" ", 3)[3] for s in summaries)) == 4, summaries


# ------------------------------------------------------ detached sound follows

_EDITS_BEFORE_B = {
    "trim A to 0-1": ("trim_clip", {"clip_id": "a", "in": 0, "out": 1}),
    "ripple delete A": ("ripple_delete", {"clip_id": "a"}),
    "slow A to 0.5x": ("set_speed", {"clip_id": "a", "factor": 0.5}),
    "freeze 1 s in A": ("freeze_frame", {"time": 1.0, "duration": 1.0}),
    "duplicate A": ("duplicate_clip", {"clip_id": "a"}),
    "drag B to the front": ("move_clip", {"clip_id": "b", "new_start": 0.0, "close_gap": True}),
}


@pytest.fixture
def has_audio(monkeypatch):
    from video_ai_editor.render import compositor
    monkeypatch.setattr(compositor, "source_has_audio", lambda _src: True)


@pytest.mark.parametrize("name", list(_EDITS_BEFORE_B))
def test_detached_sound_stays_with_its_picture(tmp_path, media, has_audio, name):
    store = _store(tmp_path / "s", media)
    r = dispatch(store, "detach_audio", {"clip_id": "b"})
    sound = _clip(store, r["audio_clip_id"])
    assert sound.linked_to == "b"
    tool, args = _EDITS_BEFORE_B[name]
    dispatch(store, tool, dict(args))
    b = _clip(store, "b")
    sound = _clip(store, r["audio_clip_id"])
    assert sound.start == pytest.approx(b.start, abs=1e-6), (name, b.start, sound.start)
    # one edit, one undo step: undo puts both back
    dispatch(store, "undo", {})
    assert _clip(store, "b").start == pytest.approx(2.0)
    assert _clip(store, r["audio_clip_id"]).start == pytest.approx(2.0)


def test_an_l_cut_offset_survives_an_earlier_trim(tmp_path, media, has_audio):
    store = _store(tmp_path / "s", media)
    sid = dispatch(store, "detach_audio", {"clip_id": "b"})["audio_clip_id"]
    dispatch(store, "move_clip", {"clip_id": sid, "new_start": 2.3})    # sound 0.3 s late
    assert _clip(store, sid).start == pytest.approx(2.3)                # not pulled back
    dispatch(store, "trim_clip", {"clip_id": "a", "in": 0, "out": 1})
    assert _clip(store, "b").start == pytest.approx(1.0)
    assert _clip(store, sid).start == pytest.approx(1.3)


def test_a_detached_sound_keeps_its_own_trim(tmp_path, media, has_audio):
    store = _store(tmp_path / "s", media)
    sid = dispatch(store, "detach_audio", {"clip_id": "b"})["audio_clip_id"]
    dispatch(store, "trim_clip", {"clip_id": sid, "in": 0.5, "out": 4})
    s = _clip(store, sid)
    assert s.in_ == pytest.approx(0.5) and s.start == pytest.approx(2.0)


# --------------------------------------------------------------- PIP follows

@pytest.mark.parametrize("name", [n for n in _EDITS_BEFORE_B if n != "drag B to the front"])
def test_a_pip_moves_with_the_text_over_the_same_picture(tmp_path, media, name):
    store = _store(tmp_path / "s", media)
    store.edl.get_track("v2").clips.append(Clip(id="p", src=media, in_=0.0, out=1.0, start=3.0))
    store.edl.get_track("tx").clips.append(TextClip(id="t_1", text="hi", start=3.0, end=4.0))
    store.commit("seed", {}, "seed")
    tool, args = _EDITS_BEFORE_B[name]
    dispatch(store, tool, dict(args))
    text, pip = _clip(store, "t_1"), _clip(store, "p")
    assert text.start != pytest.approx(3.0), name                 # the ripple did move it
    assert pip.start == pytest.approx(text.start, abs=1e-6), (name, text.start, pip.start)


def test_a_locked_pip_lane_does_not_follow(tmp_path, media):
    store = _store(tmp_path / "s", media)
    v2 = store.edl.get_track("v2")
    v2.clips.append(Clip(id="p", src=media, in_=0.0, out=1.0, start=3.0))
    v2.locked = True
    store.commit("seed", {}, "seed")
    dispatch(store, "trim_clip", {"clip_id": "a", "in": 0, "out": 1})
    assert _clip(store, "p").start == pytest.approx(3.0)


def test_moving_a_pip_itself_is_not_undone(tmp_path, media):
    store = _store(tmp_path / "s", media)
    store.edl.get_track("v2").clips.append(Clip(id="p", src=media, in_=0.0, out=1.0, start=3.0))
    store.commit("seed", {}, "seed")
    dispatch(store, "move_clip", {"clip_id": "p", "new_start": 0.5})
    assert _clip(store, "p").start == pytest.approx(0.5)


# ------------------------------------------------- move_clip keeps transitions

def _four(tmp: Path, media: str) -> EDLStore:
    store = _store(tmp, media, durs=(2.0, 2.0, 2.0, 2.0))
    store.edl.get_track("v1").transitions = [
        Transition(at=2.0, type="fade", duration=0.5),
        Transition(at=4.0, type="wipeleft", duration=0.5),
        Transition(at=6.0, type="slideleft", duration=0.5)]
    store.commit("seed", {}, "seed")
    return store


def _pairs(store: EDLStore) -> set[tuple[str, str, str]]:
    """(before, after, type) for every transition — each must sit on a seam."""
    v1 = _v1(store)
    out = set()
    for tr in store.edl.get_track("v1").transitions:
        pair = next(((a.id, b.id) for a, b in zip(v1, v1[1:]) if abs(b.start - tr.at) < 1e-3), None)
        assert pair is not None, f"{tr.type} @ {tr.at} sits on no seam"
        out.add((*pair, tr.type))
    return out


def test_dragging_d_to_the_front_keeps_transitions_on_their_pairs(tmp_path, media):
    store = _four(tmp_path / "s", media)
    dispatch(store, "move_clip", {"clip_id": "d", "new_start": 0.0, "close_gap": True})
    assert [c.id for c in _v1(store)] == ["d", "a", "b", "c"]
    # a|b and b|c are still neighbours; c|d is gone
    assert _pairs(store) == {("a", "b", "fade"), ("b", "c", "wipeleft")}


def test_dragging_b_off_the_main_track_drops_its_seams(tmp_path, media):
    store = _four(tmp_path / "s", media)
    dispatch(store, "move_clip", {"clip_id": "b", "new_track": "v2", "new_start": 9.0,
                                  "close_gap": True})
    assert [c.id for c in _v1(store)] == ["a", "c", "d"]
    assert _pairs(store) == {("c", "d", "slideleft")}


# --------------------------------------------- 25 fps typed half-frame seams

def test_appending_at_a_typed_half_frame_end_leaves_no_sliver(tmp_path, media):
    store = _store(tmp_path / "s", media, durs=(), fps=25)
    dispatch(store, "add_clip", {"track": "v1", "src": media, "in": 0, "out": 3.5, "start": 0})
    dispatch(store, "add_clip", {"track": "v1", "src": media, "in": 5, "out": 6, "start": 3.5})
    r = dispatch(store, "add_clip", {"track": "v1", "src": media, "in": 9, "out": 11, "start": 4.5})
    assert "splits" not in r["summary"], r["summary"]
    v1 = _v1(store)
    assert [round(c.in_, 2) for c in v1] == [0, 5, 9], [(c.start, c.in_, c.out) for c in v1]
    assert v1[-1].out == pytest.approx(11.0)
    assert all(c.effective_duration > 0.5 for c in v1), [(c.start, c.in_, c.out) for c in v1]
