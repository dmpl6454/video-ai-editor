"""Speed curves and freeze frames as EDITS (wave D, lane S2): `set_speed`
with a curve or a preset, `freeze_frame`, and the cuts (`split_at`,
`cut_range`, `trim_clip`) on a curve clip or a freeze.

Frame claims are measured on REAL bar-coded sources (`frame_map_golden_lib`:
every frame carries its own index) through the program map
(`render/frame_map.build_program_map`), the model the export goldens pin
frame for frame (`tests/test_frame_map_golden.py`); the export itself is
decoded in `test_speed_freeze_export.py` and the UI pass.
"""
from __future__ import annotations

import json
import sys
from fractions import Fraction
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import frame_map_golden_lib as G  # noqa: E402

from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.edl import EDLStore  # noqa: E402
from video_ai_editor.edl import speed_curve as SC  # noqa: E402
from video_ai_editor.edl import speed_presets as SP  # noqa: E402
from video_ai_editor.edl import timebase as tb  # noqa: E402
from video_ai_editor.edl.schema import Canvas, Clip, Sticker, TextClip, Track, Transition  # noqa: E402
from video_ai_editor.render.compositor import clip_frames  # noqa: E402
from video_ai_editor.render.frame_map import build_program_map  # noqa: E402

FPS = 30


@pytest.fixture(scope="module")
def sources(tmp_path_factory) -> dict[str, tuple[str, G.SourceInfo]]:
    d = tmp_path_factory.mktemp("bars")
    out = {}
    for key, sid, rate in (("s30", 1, Fraction(30)), ("s25", 2, Fraction(25))):
        p = G.make_bar_source(d / f"{key}.mp4", G.SourceSpec(key=key, sid=sid, rate=rate, seconds=8.0))
        out[key] = (str(p), G.probe_source(p))
    return out


def _store(tmp_path: Path, src: str, *, second: str | None = None) -> EDLStore:
    s = EDLStore(tmp_path / "sess")
    s.edl.canvas = Canvas(w=320, h=180, fps=FPS)
    v1 = s.edl.get_track("v1")
    v1.clips = [Clip(src=src, in_=0.5, out=6.5, start=0.0, id="c_a")]
    if second:
        v1.clips.append(Clip(src=second, in_=0.0, out=2.0, start=6.0, id="c_b"))
    s.edl.recompute_duration()
    s.commit("init", {}, "init")
    return s


def _map(store: EDLStore, sources) -> list[int]:
    return build_program_map(store.edl, {v[0]: v[1] for v in sources.values()}).frame


# --------------------------------------------------------------- set_speed

def test_preset_is_stored_as_its_curve_with_its_name_and_footprint(tmp_path, sources):
    s = _store(tmp_path, sources["s30"][0], second=sources["s30"][0])
    r = dispatch(s, "set_speed", {"clip_id": "c_a", "preset": "Hero"})
    a, b = s.edl.get_track("v1").clips
    assert a.speed["name"] == "hero"
    assert a.speed["curve"] == SC.CURVE_PRESETS["hero"]
    want = 6.0 / SC.mean_speed([tuple(p) for p in SC.CURVE_PRESETS["hero"]])
    assert a.effective_duration == pytest.approx(want)
    assert r["duration"] == pytest.approx(want)
    # The next clip ripples to the curve's footprint (on the frame grid).
    assert b.start == pytest.approx(tb.quantize(want, FPS))
    assert "Hero curve" in r["summary"]


@pytest.mark.parametrize("spelling", ["jump_cut", "Jump Cut", "jump-cut", "JUMP_CUT"])
def test_preset_spellings(tmp_path, sources, spelling):
    s = _store(tmp_path, sources["s30"][0])
    dispatch(s, "set_speed", {"clip_id": "c_a", "preset": spelling})
    assert s.edl.get_clip("c_a")[1].speed["name"] == "jump_cut"


def test_custom_curve_is_named_by_its_points_not_by_the_caller(tmp_path, sources):
    s = _store(tmp_path, sources["s30"][0])
    hero = SP.PRESET_BY_ID["hero"].points
    dispatch(s, "set_speed", {"clip_id": "c_a", "curve": hero})
    assert s.edl.get_clip("c_a")[1].speed["name"] == "hero"
    dragged = [list(p) for p in hero]
    dragged[2][1] = 0.4
    dispatch(s, "set_speed", {"clip_id": "c_a", "curve": dragged})
    c = s.edl.get_clip("c_a")[1]
    assert c.speed["name"] == "custom" and c.speed["curve"][2] == [0.42, 0.4]


def test_factor_one_clears_a_curve(tmp_path, sources):
    s = _store(tmp_path, sources["s30"][0])
    dispatch(s, "set_speed", {"clip_id": "c_a", "preset": "bullet"})
    dispatch(s, "set_speed", {"clip_id": "c_a", "factor": 1})
    c = s.edl.get_clip("c_a")[1]
    assert c.speed == 1.0 and c.effective_duration == pytest.approx(6.0)


@pytest.mark.parametrize("args, needle", [
    ({}, "needs a factor, a curve or a preset"),
    ({"factor": 2, "preset": "hero"}, "one of factor, curve or preset"),
    ({"preset": "warp"}, "unknown speed preset"),
    ({"curve": [[0, 1]]}, "2-32 points"),
    ({"curve": [[0, 1], [1, 11]]}, "outside 0.1-10x"),
    ({"curve": [[0, 1], [1.5, 2]]}, "outside 0-1"),
    ({"curve": [[0, 1], [0, 2]]}, "two curve points"),
    ({"curve": [[0, 1], [1, "fast"]]}, "must be a number"),
    ({"curve": [[0, 1], [1, float("nan")]]}, "finite"),
    ({"curve": [[x / 40, 1] for x in range(41)]}, "2-32 points"),
])
def test_set_speed_refuses_malformed_speed(tmp_path, sources, args, needle):
    s = _store(tmp_path, sources["s30"][0])
    before = s.edl.to_json()
    with pytest.raises(ValueError, match=needle):
        dispatch(s, "set_speed", {"clip_id": "c_a", **args})
    assert s.edl.to_json() == before


def test_curve_refused_on_a_pip_and_on_a_freeze(tmp_path, sources):
    s = _store(tmp_path, sources["s30"][0])
    s.edl.get_track("v2").clips.append(Clip(src=sources["s30"][0], in_=0, out=2, start=1, id="c_p"))
    with pytest.raises(ValueError, match="main video track"):
        dispatch(s, "set_speed", {"clip_id": "c_p", "preset": "hero"})
    dispatch(s, "freeze_frame", {"time": 1.0, "duration": 1.0})
    still = next(c for c in s.edl.get_track("v1").clips if c.freeze)
    with pytest.raises(ValueError, match="freeze frame"):
        dispatch(s, "set_speed", {"clip_id": still.id, "factor": 2})


def test_set_speed_curve_is_one_undo_step(tmp_path, sources):
    s = _store(tmp_path, sources["s30"][0], second=sources["s30"][0])
    before = s.edl.to_json()
    dispatch(s, "set_speed", {"clip_id": "c_a", "preset": "montage", "keep_pitch": False})
    assert s.edl.get_clip("c_a")[1].audio.keep_pitch is False
    dispatch(s, "undo", {})
    assert s.edl.to_json() == before


# --------------------------------------------------------------- freeze_frame

CASES = [  # (source, speed args, playhead seconds)
    ("s30", None, 2.0),
    ("s25", None, 2.1),
    ("s30", {"factor": 2.0}, 1.0),
    ("s25", {"factor": 1.5}, 1.3),
    ("s30", {"preset": "hero"}, 2.0),
    ("s25", {"preset": "montage"}, 1.7),
    ("s30", {"preset": "flash_in"}, 0.5),
]


@pytest.mark.parametrize("key, speed, t", CASES)
def test_freeze_holds_the_frame_under_the_playhead(tmp_path, sources, key, speed, t):
    """The still is the frame the program map (= the export) shows at the
    playhead BEFORE the edit, on every rate and speed shape, and it is held
    for exactly the requested frames."""
    s = _store(tmp_path, sources[key][0])
    if speed:
        dispatch(s, "set_speed", {"clip_id": "c_a", **speed})
    before = _map(s, sources)
    k = tb.frame_of(t, FPS)
    r = dispatch(s, "freeze_frame", {"time": t, "duration": 1.0})
    after = _map(s, sources)
    n = tb.frame_of(1.0, FPS)
    assert r["at"] == pytest.approx(tb.quantize(t, FPS))
    assert after[:k] == before[:k]
    assert after[k:k + n] == [before[k]] * n
    assert len(after) == len(before) + n


@pytest.mark.parametrize("key, speed, t", [c for c in CASES if c[0] == "s30"])
def test_freeze_continues_from_the_playhead_frame_at_the_project_rate(tmp_path, sources, key, speed, t):
    """Same-rate sources: after the hold the clip resumes on the very frame
    it froze, so the whole map is the old one with a hold spliced in."""
    s = _store(tmp_path, sources[key][0])
    if speed:
        dispatch(s, "set_speed", {"clip_id": "c_a", **speed})
    before = _map(s, sources)
    k, n = tb.frame_of(t, FPS), tb.frame_of(1.0, FPS)
    dispatch(s, "freeze_frame", {"time": t, "duration": 1.0})
    assert _map(s, sources) == before[:k] + [before[k]] * n + before[k:]


def test_freeze_default_is_three_seconds_and_ripples_everything_after(tmp_path, sources):
    src = sources["s30"][0]
    s = _store(tmp_path, src, second=src)
    s.edl.get_track("v1").transitions = [Transition(at=6.0, type="fade", duration=0.5)]
    s.edl.get_track("tx_super").clips.append(TextClip(text="late", start=4.0, end=5.0, id="t_late"))
    s.edl.get_track("tx_super").clips.append(TextClip(text="early", start=0.5, end=1.5, id="t_early"))
    s.edl.get_track("stickers").clips.append(Sticker(src="x.png", start=7.0, end=7.5, id="s_1"))
    s.commit("setup", {}, "setup")
    dispatch(s, "freeze_frame", {"time": 2.0})
    v1 = s.edl.get_track("v1").clips
    assert [round(c.start, 6) for c in v1] == [0.0, 2.0, 5.0, 9.0]
    still = v1[1]
    assert still.freeze == 3.0 and still.speed is None and still.reverse is False
    assert still.audio.fade_in == 0 and still.audio.fade_out == 0
    assert v1[3].id == "c_b"
    assert s.edl.get_track("v1").transitions[0].at == pytest.approx(9.0)
    assert s.edl.get_clip("t_late")[1].start == pytest.approx(7.0)
    assert s.edl.get_clip("t_early")[1].start == pytest.approx(0.5)
    assert s.edl.get_clip("s_1")[1].start == pytest.approx(10.0)


def test_freeze_is_one_undo_step(tmp_path, sources):
    s = _store(tmp_path, sources["s30"][0])
    before = s.edl.to_json()
    ops_before = len(json.loads((s.dir / "ops.json").read_text())["ops"])
    dispatch(s, "freeze_frame", {"time": 2.5, "duration": 2.0})
    ops = json.loads((s.dir / "ops.json").read_text())["ops"]
    assert len(ops) == ops_before + 1 and ops[-1]["tool"] == "freeze_frame"
    dispatch(s, "undo", {})
    assert s.edl.to_json() == before


def test_freeze_of_a_selected_clip_and_its_refusals(tmp_path, sources):
    src = sources["s30"][0]
    s = _store(tmp_path, src, second=src)
    with pytest.raises(ValueError, match="not over clip c_a"):
        dispatch(s, "freeze_frame", {"clip_id": "c_a", "time": 7.0})
    with pytest.raises(ValueError, match="no clip on the main video track"):
        dispatch(s, "freeze_frame", {"time": 30.0})
    with pytest.raises(ValueError, match="needs the playhead"):
        dispatch(s, "freeze_frame", {})
    with pytest.raises(ValueError, match="between 0.1 and 60"):
        dispatch(s, "freeze_frame", {"time": 1.0, "duration": 0})
    # clip_id without time: its first frame, inserted before it.
    before = _map(s, {"s30": sources["s30"]})
    dispatch(s, "freeze_frame", {"clip_id": "c_b", "duration": 0.5})
    after = _map(s, {"s30": sources["s30"]})
    assert after[180:195] == [before[180]] * 15 and after[195:] == before[180:]


def test_freeze_on_a_freeze_holds_longer(tmp_path, sources):
    s = _store(tmp_path, sources["s30"][0])
    r = dispatch(s, "freeze_frame", {"time": 2.0, "duration": 1.0})
    frame = s.edl.get_clip(r["clip_id"])[1].in_
    r2 = dispatch(s, "freeze_frame", {"time": 2.5, "duration": 1.5})
    assert r2["clip_id"] == r["clip_id"]
    still = s.edl.get_clip(r["clip_id"])[1]
    assert still.freeze == pytest.approx(2.5) and still.in_ == frame
    assert len(s.edl.get_track("v1").clips) == 3


def test_freeze_refused_on_a_locked_main_lane(tmp_path, sources):
    s = _store(tmp_path, sources["s30"][0])
    dispatch(s, "set_track_locked", {"track": "v1", "locked": True})
    with pytest.raises(ValueError, match="locked"):
        dispatch(s, "freeze_frame", {"time": 1.0})


def test_freeze_pins_a_keyframed_pose(tmp_path, sources):
    s = _store(tmp_path, sources["s30"][0])
    dispatch(s, "add_keyframe", {"clip_id": "c_a", "props": ["scale"], "time": 0.0, "values": {"scale": 1.0}})
    dispatch(s, "add_keyframe", {"clip_id": "c_a", "props": ["scale"], "time": 4.0, "values": {"scale": 3.0}})
    r = dispatch(s, "freeze_frame", {"time": 2.0, "duration": 1.0})
    assert s.edl.get_clip(r["clip_id"])[1].transform.scale == pytest.approx(2.0)


# --------------------------------------------------------------- cuts on retimed clips

def test_split_a_curve_keeps_each_part_and_the_total_length(tmp_path, sources):
    s = _store(tmp_path, sources["s25"][0])
    dispatch(s, "set_speed", {"clip_id": "c_a", "preset": "montage"})
    whole = s.edl.get_clip("c_a")[1]
    n_before = clip_frames(whole, FPS)
    pts = whole.speed_curve
    r = dispatch(s, "split_at", {"time": 1.7})
    left = s.edl.get_clip("c_a")[1]
    right = s.edl.get_clip(r["halves"]["c_a"])[1]
    assert left.speed["name"] == right.speed["name"] == "custom"
    assert clip_frames(left, FPS) + clip_frames(right, FPS) == n_before
    assert left.effective_duration == pytest.approx(1.7, abs=1e-6)
    # Each half plays its part of the ramp: the speed just before and just
    # after the cut is the whole curve's speed there.
    cm = SC.curve_map(pts, whole.duration)
    at = SC.speed_at(cm, 1.7)
    assert SC.speed_at(SC.curve_map(left.speed_curve, left.duration), 1.7 - 1e-9) == pytest.approx(at, rel=1e-6)
    assert right.speed_curve[0][1] == pytest.approx(at, rel=1e-6)
    assert left.out == right.in_


def test_split_a_freeze_into_two_holds(tmp_path, sources):
    s = _store(tmp_path, sources["s30"][0])
    r = dispatch(s, "freeze_frame", {"time": 2.0, "duration": 2.0})
    r2 = dispatch(s, "split_at", {"time": 2.5})
    a = s.edl.get_clip(r["clip_id"])[1]
    b = s.edl.get_clip(r2["halves"][r["clip_id"]])[1]
    assert (a.freeze, b.freeze) == (pytest.approx(0.5), pytest.approx(1.5))
    assert a.in_ == b.in_


def test_cut_range_through_a_curve_and_a_freeze(tmp_path, sources):
    s = _store(tmp_path, sources["s30"][0])
    dispatch(s, "set_speed", {"clip_id": "c_a", "preset": "hero"})
    D = s.edl.get_clip("c_a")[1].effective_duration
    dispatch(s, "cut_range", {"track": "v1", "start": 1.0, "end": 2.0})
    left, right = s.edl.get_track("v1").clips
    assert left.effective_duration == pytest.approx(1.0, abs=1e-6)
    assert right.effective_duration == pytest.approx(D - 2.0, abs=1e-6)
    r = dispatch(s, "freeze_frame", {"time": 0.5, "duration": 2.0})
    frame = s.edl.get_clip(r["clip_id"])[1].in_
    dispatch(s, "cut_range", {"track": "v1", "start": 1.0, "end": 2.0})
    holds = [c for c in s.edl.get_track("v1").clips if c.freeze is not None]
    # The cut goes through the still: two holds of the same frame, 1 s left.
    assert sum(c.freeze for c in holds) == pytest.approx(1.0)
    assert {c.in_ for c in holds} == {frame}


def test_timeline_edge_drag_on_a_freeze_changes_its_hold_not_its_frame(tmp_path, sources):
    """The timeline trims in source seconds scaled by speed_factor (= one
    frame / hold); the handler turns that back into a hold."""
    src = sources["s30"][0]
    s = _store(tmp_path, src, second=src)
    r = dispatch(s, "freeze_frame", {"time": 2.0, "duration": 3.0})
    still = s.edl.get_clip(r["clip_id"])[1]
    frame = still.in_
    sf = still.speed_factor
    # Drag the right edge 1 s left: out -= 1 s × speed_factor.
    dispatch(s, "trim_clip", {"clip_id": still.id, "out": still.out - 1.0 * sf})
    still = s.edl.get_clip(r["clip_id"])[1]
    assert still.freeze == pytest.approx(2.0) and still.in_ == frame
    assert s.edl.get_clip("c_b")[1].start == pytest.approx(8.0)


def test_retimed_clips_stay_off_audio_lanes(tmp_path, sources):
    s = _store(tmp_path, sources["s30"][0])
    # A curve whose MEAN is exactly 1 is still not a 1x clip.
    dispatch(s, "set_speed", {"clip_id": "c_a", "curve": [[0, 0.5], [0.5, 1.5], [1, 0.5]]})
    assert s.edl.get_clip("c_a")[1].speed_factor == pytest.approx(1.0)
    with pytest.raises(ValueError, match="speed"):
        dispatch(s, "move_clip", {"clip_id": "c_a", "new_start": 0, "new_track": "a1"})
    with pytest.raises(ValueError, match="1x clip"):
        dispatch(s, "detach_audio", {"clip_id": "c_a"})
