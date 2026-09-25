"""Wave-A lane A4 regressions: edit operations at the dispatch boundary.

QA-020 duplicate/paste ids, QA-021 bulk_delete overlay ripple, QA-022 one-frame
nudge on the magnetic main lane, QA-023 track lock, QA-041 argument bounds.
Every test drives the real `dispatch()` (or the real FastAPI app) — the same
single mutation path the UI, Claude and MCP use.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Clip, TextClip


def _store(tmp_path: Path, spans=((0.0, 10.0), (10.0, 10.0), (20.0, 20.0))) -> EDLStore:
    """v1 laid out like the QA repro: red/blue/fc30 at 0-10, 10-20, 20-40."""
    s = EDLStore(tmp_path)
    v1 = s.edl.get_track("v1")
    for i, (start, dur) in enumerate(spans):
        v1.clips.append(Clip(id=f"c_{i}", src=f"/x/{i}.mp4", in_=0.0, out=dur, start=start))
    s.commit("seed", {}, "seed")
    return s


def _v1_ids(s: EDLStore) -> list[str]:
    return [c.id for c in s.edl.get_track("v1").clips]


# ---------------------------------------------------------------- QA-020

def test_duplicating_the_same_clip_twice_gives_every_copy_a_unique_id(tmp_path):
    s = _store(tmp_path)
    a = dispatch(s, "duplicate_clip", {"clip_id": "c_1"})["new_clip_id"]
    b = dispatch(s, "duplicate_clip", {"clip_id": "c_1"})["new_clip_id"]
    assert a != b
    ids = [c.id for t in s.edl.tracks for c in t.clips]
    assert len(ids) == len(set(ids)), f"duplicate ids on the timeline: {ids}"
    # Targeting the SECOND copy edits the second copy, not the first.
    dispatch(s, "set_volume", {"target": b, "db": -6.0})
    assert s.edl.get_clip(b)[1].audio.gain_db == -6.0
    assert s.edl.get_clip(a)[1].audio.gain_db == 0.0
    dispatch(s, "ripple_delete", {"clip_id": b})
    assert s.edl.get_clip(a) is not None and s.edl.get_clip(b) is None


def test_bulk_duplicate_twice_never_repeats_an_id(tmp_path):
    s = _store(tmp_path)
    n1 = dispatch(s, "bulk_duplicate", {"clip_ids": ["c_0", "c_1"]})["new_ids"]
    n2 = dispatch(s, "bulk_duplicate", {"clip_ids": ["c_0", "c_1"]})["new_ids"]
    assert not set(n1) & set(n2)
    ids = [c.id for t in s.edl.tracks for c in t.clips]
    assert len(ids) == len(set(ids)), ids


def test_a_main_lane_copy_lands_directly_after_its_original(tmp_path):
    """Cmd+D on red of red/blue/fc30 used to give red, blue, COPY, fc30 (the
    copy tied blue's start and the stable sort kept blue first)."""
    s = _store(tmp_path)
    dispatch(s, "add_text", {"text": "over blue", "start": 12.0, "end": 13.0})
    new = dispatch(s, "duplicate_clip", {"clip_id": "c_0"})["new_clip_id"]
    assert _v1_ids(s) == ["c_0", new, "c_1", "c_2"]
    assert [c.start for c in s.edl.get_track("v1").clips] == pytest.approx([0, 10, 20, 30])
    t = next(c for tr in s.edl.tracks for c in tr.clips if isinstance(c, TextClip))
    assert (t.start, t.end) == pytest.approx((22.0, 23.0)), "the title stays over blue"


def test_bulk_duplicate_puts_each_copy_after_its_original(tmp_path):
    s = _store(tmp_path)
    new = dispatch(s, "bulk_duplicate", {"clip_ids": ["c_0", "c_1"]})["new_ids"]
    assert _v1_ids(s) == ["c_0", new[0], "c_1", new[1], "c_2"]


def _overlay(s: EDLStore, cid: str):
    return s.edl.get_clip(cid)[1]


def test_text_duplicate_and_paste_give_fresh_ids_after_the_original(tmp_path):
    """Cmd+D / Cmd+C Cmd+V on a TITLE was a 400 ("duplicate_clip only supports
    media clips") and multi-select duplicate skipped it silently. Every copy
    now gets its own id, lands right after its original on the same lane
    (never on top of another title there), and shares nothing mutable."""
    s = _store(tmp_path)
    tid = dispatch(s, "add_text", {"text": "HELLO", "start": 2.0, "end": 4.0})["id"]
    blocker = dispatch(s, "add_text", {"text": "later", "start": 4.5, "end": 5.5})["id"]
    a = dispatch(s, "duplicate_clip", {"clip_id": tid})["new_clip_id"]
    b = dispatch(s, "duplicate_clip", {"clip_id": tid})["new_clip_id"]
    ids = [c.id for t in s.edl.tracks for c in t.clips]
    assert len(ids) == len(set(ids)) and a.startswith("t_") and b.startswith("t_")
    spans = sorted((round(c.start, 3), round(c.end, 3))
                   for c in s.edl.get_clip(tid)[0].clips)
    # 2-4 original, 4.5-5.5 untouched, copies in the first free 2 s after 4.0
    assert spans == [(2.0, 4.0), (4.5, 5.5), (5.5, 7.5), (7.5, 9.5)]
    assert _overlay(s, a).text == "HELLO"
    dispatch(s, "set_clip_transform", {"clip_id": a, "x": 100})
    assert _overlay(s, tid).transform.x != 100
    assert _overlay(s, blocker).start == 4.5


def test_duplicating_a_cue_in_a_packed_lane_is_refused_not_teleported(tmp_path):
    """A caption lane is packed end to end; the first free span after cue 5
    is past the last caption, 35 s later — the QA-022 teleport in another
    lane. The copy is refused with the reason and nothing changes."""
    s = _store(tmp_path)
    for i in range(40):
        dispatch(s, "add_text", {"text": f"cue {i}", "start": float(i), "end": i + 1.0,
                                 "role": "caption"})
    track, cue = s.edl.get_clip(next(c.id for t in s.edl.tracks for c in t.clips
                                     if isinstance(c, TextClip) and c.text == "cue 5"))
    before = s.edl.to_json()
    with pytest.raises(ValueError, match="No room"):
        dispatch(s, "duplicate_clip", {"clip_id": cue.id})
    assert s.edl.to_json() == before


def test_sticker_duplicate_and_bulk_duplicate_cover_overlays(tmp_path):
    s = _store(tmp_path)
    st = dispatch(s, "add_sticker", {"emoji": "🔥", "start": 1.0, "end": 2.0})["sticker_id"]
    tid = dispatch(s, "add_text", {"text": "T", "start": 3.0, "end": 4.0})["id"]
    one = dispatch(s, "duplicate_clip", {"clip_id": st})["new_clip_id"]
    assert one.startswith("st_") and (_overlay(s, one).start, _overlay(s, one).end) == (2.0, 3.0)
    res = dispatch(s, "bulk_duplicate", {"clip_ids": [st, tid, "c_0"]})
    assert res["duplicated"] == 3 and len(res["new_ids"]) == 3
    ids = [c.id for t in s.edl.tracks for c in t.clips]
    assert len(ids) == len(set(ids)), ids


# ---------------------------------------------------------------- QA-021

def test_bulk_delete_on_v1_ripples_text_like_single_deletes(tmp_path):
    s = _store(tmp_path)
    dispatch(s, "add_text", {"text": "HELLO", "start": 22.0, "end": 24.0})
    dispatch(s, "bulk_delete", {"clip_ids": ["c_0", "c_1"]})
    t = next(c for tr in s.edl.tracks for c in tr.clips if isinstance(c, TextClip))
    assert (t.start, t.end) == pytest.approx((2.0, 4.0))
    assert s.edl.duration == pytest.approx(20.0), "no text stranded past the end"


def test_bulk_delete_matches_sequential_ripple_delete(tmp_path):
    """Non-adjacent removals, deliberately passed in timeline order: the result
    must equal the same clips removed one at a time."""
    spans = ((0.0, 4.0), (4.0, 4.0), (8.0, 4.0), (12.0, 4.0))
    a = _store(tmp_path / "a", spans)
    b = _store(tmp_path / "b", spans)
    for s in (a, b):
        dispatch(s, "add_text", {"text": "one", "start": 5.0, "end": 6.0, "role": "super"})
        dispatch(s, "add_sticker", {"emoji": "🔥", "start": 13.0, "end": 14.0})
    dispatch(a, "bulk_delete", {"clip_ids": ["c_0", "c_2"]})
    dispatch(b, "ripple_delete", {"clip_id": "c_2"})
    dispatch(b, "ripple_delete", {"clip_id": "c_0"})

    def overlays(s):
        return sorted((c.start, c.end) for tr in s.edl.tracks
                      if tr.type in ("text", "sticker") for c in tr.clips)
    assert overlays(a) == pytest.approx(overlays(b))
    assert overlays(a) == pytest.approx([(1.0, 2.0), (5.0, 6.0)])
    assert a.edl.duration == pytest.approx(b.edl.duration)


def test_bulk_delete_of_every_v1_clip_drops_orphaned_overlays(tmp_path):
    s = _store(tmp_path)
    dispatch(s, "add_text", {"text": "x", "start": 22.0, "end": 24.0})
    dispatch(s, "bulk_delete", {"clip_ids": ["c_0", "c_1", "c_2"]})
    assert s.edl.duration == 0.0


# ---------------------------------------------------------------- QA-022

def test_one_frame_nudge_into_a_neighbour_is_refused_not_teleported(tmp_path):
    s = _store(tmp_path)
    before = [(c.id, c.start) for c in s.edl.get_track("v1").clips]
    with pytest.raises(ValueError, match="overlap"):
        dispatch(s, "move_clip", {"clip_id": "c_1", "new_start": 10.0 + 1 / 30})
    with pytest.raises(ValueError, match="overlap"):
        dispatch(s, "move_clip", {"clip_id": "c_1", "new_start": 10.0 - 1 / 30})
    assert [(c.id, c.start) for c in s.edl.get_track("v1").clips] == before
    assert s.edl.duration == pytest.approx(40.0)


def test_nudge_into_real_free_space_on_v1_still_moves_exactly(tmp_path):
    s = _store(tmp_path, spans=((0.0, 10.0), (12.0, 5.0)))
    dispatch(s, "move_clip", {"clip_id": "c_1", "new_start": 11.0})
    assert s.edl.get_clip("c_1")[1].start == pytest.approx(11.0)


def test_nudge_over_http_is_a_400_with_the_reason(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import video_ai_editor.main as m
    s = _store(tmp_path / "s_nudge0001")
    monkeypatch.setattr(m, "_store", lambda sid: s)
    r = TestClient(m.app).post("/api/sessions/s_nudge0001/dispatch",
                               json={"tool": "move_clip",
                                     "args": {"clip_id": "c_0", "new_start": 1 / 30}})
    assert r.status_code == 400, r.text
    assert "overlap" in r.text


# ---------------------------------------------------------------- QA-023

def _lock(s, track="v1", locked=True):
    dispatch(s, "set_track_locked", {"track": track, "locked": locked})


@pytest.mark.parametrize("tool,args", [
    ("ripple_delete", {"clip_id": "c_0"}),
    ("split_at", {"time": 5.0}),
    ("split_at", {"track": "v1", "time": 5.0}),
    ("trim_clip", {"clip_id": "c_0", "out": 5.0}),
    ("move_clip", {"clip_id": "c_2", "new_start": 50.0}),
    ("cut_range", {"track": "v1", "start": 1.0, "end": 2.0}),
    ("bulk_delete", {"clip_ids": ["c_0"]}),
    ("duplicate_clip", {"clip_id": "c_0"}),
    ("set_speed", {"clip_id": "c_0", "factor": 2.0}),
    ("color_grade", {"brightness": 0.2}),
    ("add_effect", {"clip_id": "c_0", "type": "vignette"}),
    ("set_volume", {"target": "v1", "db": -3.0}),
    ("add_transition", {"at": 10.0}),
])
def test_locked_main_track_refuses_every_mutation(tmp_path, tool, args):
    s = _store(tmp_path)
    _lock(s)
    before = s.edl.to_json()
    with pytest.raises(ValueError, match="locked"):
        dispatch(s, tool, dict(args))
    assert s.edl.to_json() == before, "the refused edit must leave nothing behind"
    assert s.ops.last().tool == "set_track_locked", "and must not be logged"


@pytest.mark.parametrize("tool,args,action", [
    ("ripple_delete", {"clip_id": "c_0"}, "ripple delete"),
    ("add_clip", {"track": "v1", "src": "/x/9.mp4", "in": 0.0, "out": 2.0, "start": 0.0}, "add clip"),
    ("set_clip_transform", {"clip_id": "c_0", "scale": 1.5}, "set clip transform"),
])
def test_locked_refusal_reads_as_a_sentence_the_toast_can_show(tmp_path, tool, args, action):
    """The refusal is shown verbatim as the UI toast. It used to splice the
    tool name into a verb slot — "unlock it to add clip its clips." — for
    every tool whose name is not a verb phrase."""
    s = _store(tmp_path)
    _lock(s)
    with pytest.raises(ValueError) as e:
        dispatch(s, tool, dict(args))
    assert str(e.value) == (f"Track 'Main video' (v1) is locked — unlock it to edit "
                            f"its clips ({action} was not applied).")


def test_unlocking_restores_editing_and_other_lanes_stay_editable(tmp_path):
    s = _store(tmp_path)
    _lock(s)
    dispatch(s, "add_text", {"text": "fine", "start": 1.0, "end": 2.0})  # text lane unlocked
    _lock(s, locked=False)
    dispatch(s, "ripple_delete", {"clip_id": "c_0"})
    assert _v1_ids(s) == ["c_1", "c_2"]


def test_locked_overlay_lane_is_not_shifted_by_a_main_lane_ripple(tmp_path):
    s = _store(tmp_path)
    dispatch(s, "add_text", {"text": "pinned", "start": 22.0, "end": 24.0})
    text_track = next(t for t in s.edl.tracks if any(isinstance(c, TextClip) for c in t.clips))
    _lock(s, text_track.id)
    dispatch(s, "ripple_delete", {"clip_id": "c_0"})
    t = text_track.clips[0]
    assert (t.start, t.end) == (22.0, 24.0)
    with pytest.raises(ValueError, match="locked"):
        dispatch(s, "move_clip", {"clip_id": t.id, "new_start": 1.0})


def test_lock_is_enforced_over_http(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import video_ai_editor.main as m
    s = _store(tmp_path / "s_lock000001")
    monkeypatch.setattr(m, "_store", lambda sid: s)
    c = TestClient(m.app)
    assert c.post("/api/sessions/s_lock000001/dispatch",
                  json={"tool": "set_track_locked", "args": {"track": "v1", "locked": True}}).status_code == 200
    r = c.post("/api/sessions/s_lock000001/dispatch",
               json={"tool": "ripple_delete", "args": {"clip_id": "c_0"}})
    assert r.status_code == 400 and "locked" in r.text
    assert _v1_ids(s) == ["c_0", "c_1", "c_2"]


# ---------------------------------------------------------------- QA-041

@pytest.mark.parametrize("tool,args", [
    ("move_clip", {"clip_id": "c_2", "new_start": 1e12}),
    ("move_clip", {"clip_id": "c_2", "new_start": 100000}),
    ("set_volume", {"target": "c_0", "db": 1e6}),
    ("color_grade", {"clip_id": "c_0", "brightness": 1e9}),
    ("add_marker", {"time": 1e300}),
    ("add_text", {"text": "x", "start": 5.0, "end": 2.0}),
    ("add_super_text", {"text": "x", "start": 5.0, "end": 5.0}),
    ("set_speed", {"clip_id": "c_0", "factor": 1e6}),
    ("add_transition", {"at": 1e9}),
    ("split_at", {"time": 1e12}),
    ("add_clip", {"track": "v1", "src": "/x/a.mp4", "in": 3.0, "out": 1.0, "start": 0.0}),
    ("apply_export_preset", {}),
])
def test_absurd_arguments_are_rejected_before_they_reach_the_timeline(tmp_path, tool, args):
    s = _store(tmp_path)
    before = s.edl.to_json()
    with pytest.raises(ValueError):
        dispatch(s, tool, dict(args))
    assert s.edl.to_json() == before
    assert s.edl.duration == pytest.approx(40.0)


def test_bounds_are_advertised_in_the_tool_schema():
    from video_ai_editor.agent.tools import ALL_TOOLS, TIMELINE_MAX_SECONDS
    by = {t["name"]: t["input_schema"]["properties"] for t in ALL_TOOLS}
    assert by["move_clip"]["new_start"]["maximum"] == TIMELINE_MAX_SECONDS
    # Times are upper-bounded only: handlers CLAMP a negative time to 0.
    assert "minimum" not in by["move_clip"]["new_start"]
    assert by["set_volume"]["db"]["maximum"] <= 24
    assert by["color_grade"]["brightness"] == {"type": "number", "minimum": -1.0, "maximum": 1.0} \
        or (by["color_grade"]["brightness"]["minimum"], by["color_grade"]["brightness"]["maximum"]) == (-1.0, 1.0)


def test_ordinary_values_still_pass(tmp_path):
    s = _store(tmp_path)
    dispatch(s, "set_volume", {"target": "c_0", "db": -12})
    dispatch(s, "color_grade", {"clip_id": "c_0", "brightness": 0.1, "contrast": 1.2})
    dispatch(s, "add_marker", {"time": 12.5})
    dispatch(s, "set_speed", {"clip_id": "c_0", "factor": "2"})
    dispatch(s, "apply_export_preset", {"name": "tiktok"})


def test_absurd_value_is_a_clean_400_over_http(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import video_ai_editor.main as m
    s = _store(tmp_path / "s_bound00001")
    monkeypatch.setattr(m, "_store", lambda sid: s)
    r = TestClient(m.app).post("/api/sessions/s_bound00001/dispatch",
                               json={"tool": "move_clip",
                                     "args": {"clip_id": "c_2", "new_start": 100000}})
    assert r.status_code == 400, r.text
    assert "new_start" in r.text


# ---------------------------------------------------------------- QA-027 (preset fps)

@pytest.mark.parametrize("project_fps,expect", [
    (120.0, 30.0),      # no platform takes 120 fps: conform to the preset's rate
    (240.0, 30.0),
    (25.0, 25.0),       # a real delivery rate is kept (QA-009: no pulldown judder)
    (30000 / 1001, 30000 / 1001),
    (60.0, 60.0),
])
def test_platform_preset_sets_fps_only_when_the_project_rate_is_undeliverable(
        tmp_path, project_fps, expect):
    s = _store(tmp_path)
    dispatch(s, "set_canvas", {"fps": project_fps})
    dispatch(s, "apply_export_preset", {"name": "tiktok"})
    assert s.edl.canvas.fps == pytest.approx(expect)
