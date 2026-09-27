"""Speed, curves, freeze and reverse on OVERLAY (PIP, v2+) clips as EDITS
(wave D3, lane E2) — CapCut retimes overlays, and so does this app now.

`set_speed` and `freeze_frame` used to refuse a PIP ("PIP clips render at
native speed"): render/pip.py applied no retime. It now retimes a PIP with
v1's own rule (tests/test_b5_pip_frame_exact.py decodes the renders), so the
edits are allowed, with the overlay-lane rules:

* an overlay lane is NEVER repacked — a PIP sits at an absolute time and v2
  legitimately has gaps; a slow-down that would run into the next clip on
  the same lane pushes that lane's later clips (the audio-lane rule);
* nothing else follows an overlay edit: v1, the other lanes, the
  transitions and the text/sticker overlays stay put;
* a freeze on a PIP splits it and inserts a still of the frame the export
  shows at the playhead, opening only that lane.

Frame claims use real bar-coded sources and the per-clip model
`frame_map.clip_frame_list` (golden-pinned against v1 renders; a PIP picks
frames by the same rule).
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
from video_ai_editor.edl import speed_presets as SP  # noqa: E402
from video_ai_editor.edl import timebase as tb  # noqa: E402
from video_ai_editor.edl.schema import Canvas, Clip, Sticker, TextClip, Transform, Transition  # noqa: E402
from video_ai_editor.render import frame_map as FM  # noqa: E402
from video_ai_editor.render.pip import pip_frames, pip_layout_end  # noqa: E402

FPS = 30


@pytest.fixture(scope="module")
def sources(tmp_path_factory) -> dict[str, tuple[str, G.SourceInfo]]:
    d = tmp_path_factory.mktemp("bars")
    out = {}
    for key, sid, rate in (("s30", 1, Fraction(30)), ("s25", 2, Fraction(25))):
        p = G.make_bar_source(d / f"{key}.mp4", G.SourceSpec(key=key, sid=sid, rate=rate, seconds=8.0))
        out[key] = (str(p), G.probe_source(p))
    return out


def _store(tmp_path: Path, src: str) -> EDLStore:
    """v1: one 8 s clip; v2: two deliberately gapped PIPs at 1.0 and 5.0;
    a transition, a caption and a sticker to prove nothing else moves."""
    s = EDLStore(tmp_path / "sess")
    s.edl.canvas = Canvas(w=320, h=180, fps=FPS)
    s.edl.get_track("v1").clips = [Clip(src=src, in_=0.0, out=4.0, start=0.0, id="c_m1"),
                                   Clip(src=src, in_=4.0, out=8.0, start=4.0, id="c_m2")]
    s.edl.get_track("v1").transitions = [Transition(at=4.0, type="fade", duration=0.5)]
    tf = Transform(x=160, y=90, scale=0.5)
    s.edl.get_track("v2").clips = [
        Clip(src=src, in_=0.5, out=2.5, start=1.0, id="c_p1", transform=tf),
        Clip(src=src, in_=3.0, out=5.0, start=5.0, id="c_p2", transform=tf.model_copy())]
    s.edl.get_track("tx_super").clips.append(TextClip(text="t", start=3.0, end=4.0, id="t_1"))
    s.edl.get_track("stickers").clips.append(Sticker(src="x.png", start=6.0, end=6.5, id="s_1"))
    s.edl.recompute_duration()
    s.commit("init", {}, "init")
    return s


def _rest(s: EDLStore, lane: str = "v2") -> str:
    """Everything but `lane`, serialised — must not change on a PIP edit."""
    d = json.loads(s.edl.to_json())
    d["tracks"] = [t for t in d["tracks"] if t["id"] != lane]
    d.pop("duration", None)
    return json.dumps(d, sort_keys=True)


def _lane_frames(s: EDLStore, info: G.SourceInfo, lane: str = "v2") -> dict[int, int]:
    """Output frame → source frame the PIP lane shows (the export's picks)."""
    out: dict[int, int] = {}
    for c in s.edl.get_track(lane).clips:
        f0, n = pip_frames(c.start, pip_layout_end(c), FPS)
        fl = FM.clip_frame_list(c, info, FPS)
        for j in range(n):
            out[f0 + j] = fl[j]
    return out


# --------------------------------------------------------------- set_speed

@pytest.mark.parametrize("args, footprint", [
    ({"factor": 2.0}, 1.0),
    ({"factor": 0.5}, 4.0),
    ({"preset": "hero"}, None),
    ({"curve": [[0, 1.0], [0.5, 0.25], [1, 1.0]]}, None),
])
def test_set_speed_on_a_pip_retimes_it_in_place(tmp_path, sources, args, footprint):
    s = _store(tmp_path, sources["s30"][0])
    rest = _rest(s)
    r = dispatch(s, "set_speed", {"clip_id": "c_p1", **args})
    p1, p2 = s.edl.get_track("v2").clips
    if footprint is not None:
        assert p1.effective_duration == pytest.approx(footprint)
    assert r["duration"] == pytest.approx(p1.effective_duration)
    assert p1.start == pytest.approx(1.0), "a PIP keeps its placement"
    # The next PIP moves only if the retimed one would run into it.
    want_p2 = max(5.0, tb.quantize(1.0 + p1.effective_duration, FPS)) \
        if 1.0 + p1.effective_duration > 5.0 else 5.0
    assert p2.start == pytest.approx(want_p2, abs=1 / FPS)
    assert p2.start >= 1.0 + p1.effective_duration - 1e-9
    assert _rest(s) == rest, "v1, transitions and the other overlays stay put"
    # (less the v1 cross-fade's 0.5 s, EDL.duration's rule)
    assert s.edl.duration == pytest.approx(max(8.0, p2.start + p2.effective_duration) - 0.5)


def test_a_slow_down_pushes_only_its_own_lane_and_undo_restores(tmp_path, sources):
    s = _store(tmp_path, sources["s30"][0])
    before = s.edl.to_json()
    rest = _rest(s)
    dispatch(s, "set_speed", {"clip_id": "c_p1", "factor": 0.25})   # 2 s → 8 s: 1.0-9.0
    p1, p2 = s.edl.get_track("v2").clips
    assert p1.start == pytest.approx(1.0) and p1.effective_duration == pytest.approx(8.0)
    assert p2.start == pytest.approx(9.0)
    assert _rest(s) == rest
    dispatch(s, "undo", {})
    assert s.edl.to_json() == before


def test_a_speed_up_leaves_the_gap(tmp_path, sources):
    """The old refusal existed because set_speed once REPACKED v2 from t=0
    (8.0/20.0 placements collapsed to 0.0/2.0). A speed-up now leaves the
    lane's gaps exactly where they were."""
    s = _store(tmp_path, sources["s30"][0])
    dispatch(s, "set_speed", {"clip_id": "c_p2", "factor": 4.0})
    p1, p2 = s.edl.get_track("v2").clips
    assert (p1.start, p2.start) == (pytest.approx(1.0), pytest.approx(5.0))
    assert p2.effective_duration == pytest.approx(0.5)


def test_the_retimed_pip_shows_the_frames_of_its_speed(tmp_path, sources):
    """The frame model of the edited lane: 2x picks every other source frame
    from `in` (the export decodes the same — test_b5_pip_frame_exact)."""
    src, info = sources["s30"]
    s = _store(tmp_path, src)
    dispatch(s, "set_speed", {"clip_id": "c_p1", "factor": 2.0})
    lane = _lane_frames(s, info)
    k0 = tb.frame_of(1.0, FPS)
    assert [lane[k0 + j] for j in range(5)] == [15, 17, 19, 21, 23]


def test_keep_pitch_travels_with_a_pip_speed(tmp_path, sources):
    s = _store(tmp_path, sources["s30"][0])
    dispatch(s, "set_speed", {"clip_id": "c_p1", "factor": 1.5, "keep_pitch": False})
    assert s.edl.get_clip("c_p1")[1].audio.keep_pitch is False


def test_set_speed_on_a_locked_overlay_lane_is_refused(tmp_path, sources):
    s = _store(tmp_path, sources["s30"][0])
    dispatch(s, "set_track_locked", {"track": "v2", "locked": True})
    before = s.edl.to_json()
    with pytest.raises(ValueError, match="locked"):
        dispatch(s, "set_speed", {"clip_id": "c_p1", "factor": 2.0})
    assert s.edl.to_json() == before


def test_a_retimed_clip_moves_between_v1_and_a_pip_lane_keeping_its_speed(tmp_path, sources):
    s = _store(tmp_path, sources["s30"][0])
    dispatch(s, "set_speed", {"clip_id": "c_m2", "preset": "montage"})
    dispatch(s, "move_clip", {"clip_id": "c_m2", "new_track": "v2", "new_start": 12.0})
    tr, c = s.edl.get_clip("c_m2")
    assert tr.id == "v2" and c.speed_curve is not None and c.start == pytest.approx(12.0)
    # ...but not onto an audio lane, whose clips have no picture to retime.
    dispatch(s, "set_speed", {"clip_id": "c_p1", "factor": 2.0})
    with pytest.raises(ValueError, match="audio lane"):
        dispatch(s, "move_clip", {"clip_id": "c_p1", "new_track": "music", "new_start": 0.0})


# --------------------------------------------------------------- freeze

@pytest.mark.parametrize("key, speed, t", [
    ("s30", None, 1.5),
    ("s25", None, 1.6),
    ("s30", {"factor": 2.0}, 1.4),
    ("s25", {"preset": "hero"}, 2.0),
])
def test_freeze_on_a_pip_holds_the_frame_under_the_playhead(tmp_path, sources, key, speed, t):
    """The still is the frame the PIP showed at the playhead BEFORE the
    edit, held for exactly the hold; the rest of the PIP resumes after it;
    only this lane moves."""
    src, info = sources[key]
    s = _store(tmp_path, src)
    if speed:
        dispatch(s, "set_speed", {"clip_id": "c_p1", **speed})
    before = _lane_frames(s, info)
    rest = _rest(s)
    p2_before = s.edl.get_clip("c_p2")[1].start
    k, n = tb.frame_of(t, FPS), tb.frame_of(1.0, FPS)
    r = dispatch(s, "freeze_frame", {"clip_id": "c_p1", "time": t, "duration": 1.0})
    after = _lane_frames(s, info)
    assert r["at"] == pytest.approx(tb.quantize(t, FPS))
    still = s.edl.get_clip(r["clip_id"])
    assert still[0].id == "v2" and still[1].freeze == pytest.approx(1.0)
    k0 = tb.frame_of(1.0, FPS)
    assert [after[j] for j in range(k0, k)] == [before[j] for j in range(k0, k)]
    assert [after[j] for j in range(k, k + n)] == [before[k]] * n
    if key == "s30":
        # same-rate source: the PIP resumes on the very frame it froze
        end = max(j for j in before if j < tb.frame_of(p2_before, FPS))
        assert [after[j + n] for j in range(k, end + 1)] == [before[j] for j in range(k, end + 1)]
    assert _rest(s) == rest, "v1, transitions, text and stickers stay put"
    assert s.edl.get_clip("c_p2")[1].start >= p2_before


def test_freeze_on_a_pip_by_track_and_extending_it(tmp_path, sources):
    s = _store(tmp_path, sources["s30"][0])
    r = dispatch(s, "freeze_frame", {"track": "v2", "time": 5.5, "duration": 1.0})
    lane = s.edl.get_track("v2").clips
    assert [round(c.start, 6) for c in lane] == [1.0, 5.0, 5.5, 6.5]
    assert lane[2].id == r["clip_id"] and lane[2].freeze == 1.0
    r2 = dispatch(s, "freeze_frame", {"clip_id": r["clip_id"], "time": 6.0, "duration": 0.5})
    assert r2["clip_id"] == r["clip_id"]
    lane = s.edl.get_track("v2").clips
    assert [round(c.start, 6) for c in lane] == [1.0, 5.0, 5.5, 7.0]
    assert s.edl.get_clip(r["clip_id"])[1].freeze == pytest.approx(1.5)
    with pytest.raises(ValueError, match="overlay lane 'v2'"):
        dispatch(s, "freeze_frame", {"track": "v2", "time": 0.5})
    with pytest.raises(ValueError, match="not a video lane"):
        dispatch(s, "freeze_frame", {"track": "music", "time": 0.5})


def test_freeze_on_a_pip_is_one_undo_step(tmp_path, sources):
    s = _store(tmp_path, sources["s30"][0])
    before = s.edl.to_json()
    ops_before = len(json.loads((s.dir / "ops.json").read_text())["ops"])
    dispatch(s, "freeze_frame", {"clip_id": "c_p2", "time": 6.0, "duration": 2.0})
    ops = json.loads((s.dir / "ops.json").read_text())["ops"]
    assert len(ops) == ops_before + 1 and ops[-1]["tool"] == "freeze_frame"
    dispatch(s, "undo", {})
    assert s.edl.to_json() == before


def test_speed_on_a_frozen_pip_is_refused(tmp_path, sources):
    s = _store(tmp_path, sources["s30"][0])
    r = dispatch(s, "freeze_frame", {"clip_id": "c_p1", "time": 1.5, "duration": 1.0})
    with pytest.raises(ValueError, match="freeze frame"):
        dispatch(s, "set_speed", {"clip_id": r["clip_id"], "factor": 2})


# --------------------------------------------------------------- reverse

def test_reverse_on_a_pip_and_undo(tmp_path, sources):
    src, info = sources["s30"]
    s = _store(tmp_path, src)
    before = s.edl.to_json()
    dispatch(s, "set_clip_reverse", {"clip_id": "c_p1", "reverse": True})
    lane = _lane_frames(s, info)
    k0 = tb.frame_of(1.0, FPS)
    # in 0.5 .. out 2.5 backwards: the first frame is the last forward one
    assert lane[k0] == 74 and lane[k0 + 59] == 15
    assert s.edl.get_clip("c_p1")[1].start == pytest.approx(1.0)
    dispatch(s, "undo", {})
    assert s.edl.to_json() == before


def test_an_overlay_freeze_ignores_a_locked_main_lane_but_not_its_own(tmp_path, sources):
    """Track lock (QA-023) names the lane the op edits: an overlay freeze by
    `clip_id` edits only the overlay lane, so a locked v1 does not refuse
    it — a locked overlay lane does, and nothing is committed."""
    s = _store(tmp_path, sources["s30"][0])
    dispatch(s, "set_track_locked", {"track": "v1", "locked": True})
    r = dispatch(s, "freeze_frame", {"clip_id": "c_p1", "time": 1.5, "duration": 1.0})
    assert s.edl.get_clip(r["clip_id"])[0].id == "v2"
    dispatch(s, "set_track_locked", {"track": "v2", "locked": True})
    before = s.edl.to_json()
    with pytest.raises(ValueError, match="locked"):
        dispatch(s, "freeze_frame", {"clip_id": "c_p2", "time": 5.5, "duration": 1.0})
    assert s.edl.to_json() == before
