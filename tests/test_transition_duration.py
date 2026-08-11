"""A transition SHORTENS the rendered timeline, and the EDL has to say so.

`xfade` plays both clips at once for its duration, so each transition the
renderer applies removes that much from the output — compositor.py has always
known this (`cur_dur = cur_dur + seg_dur[i] - tdur`) and the EDL never did. An
8s timeline split at 2/4/6 with three 0.5s transitions renders 6.5s while the
transport counted to 8.00, so playback stopped with a dead tail and no
explanation: "the 8 sec video got stopped at 7 sec".

The rule has to match the renderer's applicability test exactly — counting a
transition it will not apply is the same defect pointing the other way.
"""
from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl.schema import Clip, TextClip, Transition
from video_ai_editor.edl.snapshot import EDLStore


def _store(tmp_path: Path, spans=((0.0, 2.0), (2.0, 2.0), (4.0, 2.0), (6.0, 2.0))) -> EDLStore:
    s = EDLStore(tmp_path)
    v1 = s.edl.get_track("v1")
    for i, (start, dur) in enumerate(spans):
        v1.clips.append(Clip(id=f"c{i}", src=f"/x/{i}.mp4", in_=0.0, out=dur, start=start))
    s.edl.recompute_duration()
    return s


def test_a_transition_shortens_the_reported_timeline(tmp_path):
    s = _store(tmp_path)
    assert s.edl.duration == pytest.approx(8.0)

    s.edl.get_track("v1").transitions.append(Transition(at=2.0, type="fade", duration=0.5))
    s.edl.recompute_duration()
    assert s.edl.duration == pytest.approx(7.5)


def test_every_applied_transition_costs_its_own_duration(tmp_path):
    """The reported case: 3 seams, 0.5s each -> 8.0s of clips renders 6.5s."""
    s = _store(tmp_path)
    for at in (2.0, 4.0, 6.0):
        s.edl.get_track("v1").transitions.append(Transition(at=at, duration=0.5))
    s.edl.recompute_duration()
    assert s.edl.duration == pytest.approx(6.5)
    assert s.edl.transition_overlap() == pytest.approx(1.5)


def test_removing_the_transition_gives_the_time_back(tmp_path):
    s = _store(tmp_path)
    t = s.edl.get_track("v1")
    t.transitions.append(Transition(at=4.0, duration=0.75))
    s.edl.recompute_duration()
    assert s.edl.duration == pytest.approx(7.25)
    t.transitions.clear()
    s.edl.recompute_duration()
    assert s.edl.duration == pytest.approx(8.0)


# --------------------------------------- only what the RENDERER actually applies

def test_a_transition_across_a_GAP_costs_nothing(tmp_path):
    """A gap becomes a black filler segment and the renderer leaves the seam a
    hard cut (a cross-fade across black is meaningless). Charging for it would
    report a timeline shorter than the file."""
    s = _store(tmp_path, spans=((0.0, 2.0), (5.0, 2.0)))
    s.edl.get_track("v1").transitions.append(Transition(at=2.0, duration=0.5))
    s.edl.recompute_duration()
    assert s.edl.transition_overlap() == pytest.approx(0.0)
    assert s.edl.duration == pytest.approx(7.0)


def test_a_transition_matching_no_seam_costs_nothing(tmp_path):
    s = _store(tmp_path)
    s.edl.get_track("v1").transitions.append(Transition(at=3.3, duration=0.5))
    s.edl.recompute_duration()
    assert s.edl.duration == pytest.approx(8.0)


def test_the_seam_match_uses_the_renderer_s_own_tolerance(tmp_path):
    """compositor.py matches `|tr.at - boundary| < 0.05`; anything else would
    charge for a transition that never renders, or miss one that does."""
    s = _store(tmp_path)
    t = s.edl.get_track("v1")
    t.transitions.append(Transition(at=2.04, duration=0.5))
    s.edl.recompute_duration()
    assert s.edl.duration == pytest.approx(7.5), "within tolerance -> applied"
    t.transitions[0] = Transition(at=2.06, duration=0.5)
    s.edl.recompute_duration()
    assert s.edl.duration == pytest.approx(8.0), "outside tolerance -> not applied"


def test_a_stack_at_one_cut_is_charged_once(tmp_path):
    """add_transition replaces at a seam, but a legacy EDL can still hold a
    stack; the renderer keys by segment index and applies exactly one."""
    s = _store(tmp_path)
    t = s.edl.get_track("v1")
    t.transitions.append(Transition(at=2.0, duration=0.5))
    t.transitions.append(Transition(at=2.0, duration=0.5))
    s.edl.recompute_duration()
    assert s.edl.duration == pytest.approx(7.5)


def test_the_overlap_can_never_exceed_the_shorter_clip(tmp_path):
    """xfade cannot overlap further than a clip is long — an over-long
    transition must not drive the reported duration below zero or past the
    content."""
    s = _store(tmp_path, spans=((0.0, 1.0), (1.0, 1.0)))
    s.edl.get_track("v1").transitions.append(Transition(at=1.0, duration=30.0))
    s.edl.recompute_duration()
    assert s.edl.transition_overlap() == pytest.approx(1.0)
    assert s.edl.duration == pytest.approx(1.0)


def test_a_speed_changed_clip_uses_its_TIMELINE_footprint(tmp_path):
    """The seam is at the effective end, not the source end."""
    s = _store(tmp_path, spans=((0.0, 4.0), (2.0, 2.0)))
    v1 = s.edl.get_track("v1")
    v1.clips[0].speed = 2.0                    # 4s of source -> 2s of timeline
    v1.transitions.append(Transition(at=2.0, duration=0.5))
    s.edl.recompute_duration()
    assert s.edl.duration == pytest.approx(3.5)


def test_an_overlay_past_the_end_still_holds_the_timeline_open(tmp_path):
    """recompute_duration is a max over every track; the subtraction applies to
    the whole timeline because the output IS the v1 assembly."""
    s = _store(tmp_path)
    s.edl.get_track("v1").transitions.append(Transition(at=2.0, duration=0.5))
    s.edl.get_track("tx_super").clips.append(TextClip(id="t0", text="X", start=9.0, end=12.0))
    s.edl.recompute_duration()
    assert s.edl.duration == pytest.approx(11.5)


def test_no_transitions_means_no_change_at_all(tmp_path):
    """The overwhelming majority of timelines. This must be byte-identical to
    the old behaviour or every existing project's duration shifts."""
    s = _store(tmp_path)
    assert s.edl.transition_overlap() == 0.0
    assert s.edl.duration == pytest.approx(8.0)


def test_pip_transitions_are_not_charged(tmp_path):
    """Transitions render on the v1 assembly only; render/pip.py never looks at
    them, so a v2 transition costs nothing."""
    s = _store(tmp_path)
    v2 = s.edl.get_track("v2")
    v2.clips.append(Clip(id="p0", src="/x/p.mp4", in_=0, out=2, start=0.0))
    v2.clips.append(Clip(id="p1", src="/x/q.mp4", in_=0, out=2, start=2.0))
    v2.transitions.append(Transition(at=2.0, duration=0.5))
    s.edl.recompute_duration()
    assert s.edl.duration == pytest.approx(8.0)


# ------------------------------------------- v1 is ASSEMBLED, not just laid out

def test_overlapping_v1_clips_report_the_length_they_RENDER(tmp_path):
    """`_v1_segments` walks v1 with `cursor = max(cursor, start) + duration`,
    so overlapping clips are still emitted one after the other and the file is
    LONGER than the geometric max. Measured against the real renderer: two 4s
    clips overlapping by 2s reported 6.0s and rendered 8.0s.

    Found while verifying the transition fix — same defect class (the app
    claiming a length it does not deliver), different cause. Only add_clip can
    still create it; move_clip snaps to the first free gap.
    """
    s = _store(tmp_path, spans=((0.0, 4.0), (2.0, 4.0)))
    assert s.edl.duration == pytest.approx(8.0)


def test_the_packing_rule_changes_nothing_for_a_normal_timeline(tmp_path):
    """For non-overlapping clips the cursor IS the geometric max, so no
    existing project's duration moves."""
    for spans in (((0.0, 2.0), (2.0, 2.0)),          # butted
                  ((0.0, 2.0), (5.0, 2.0)),          # gapped
                  ((1.5, 2.0), (3.5, 2.0))):         # leading offset
        s = _store(tmp_path / str(hash(spans)), spans=spans)
        assert s.edl.duration == pytest.approx(
            max(st + d for st, d in spans))


def test_overlap_and_a_transition_are_both_accounted(tmp_path):
    s = _store(tmp_path, spans=((0.0, 4.0), (2.0, 4.0)))
    s.edl.get_track("v1").transitions.append(Transition(at=4.0, duration=0.5))
    s.edl.recompute_duration()
    # Packed 8.0s, and the seam IS adjacent (an overlap inserts no filler), so
    # the renderer applies the transition: 7.5s. Verified against ffmpeg.
    assert s.edl.duration == pytest.approx(7.5)


def test_a_pip_lane_is_NOT_packed(tmp_path):
    """v2 clips are overlaid at absolute times, so their extent is the plain
    maximum — packing them would invent length that never renders."""
    s = _store(tmp_path, spans=((0.0, 2.0),))
    v2 = s.edl.get_track("v2")
    v2.clips.append(Clip(id="p0", src="/x/p.mp4", in_=0, out=4, start=0.0))
    v2.clips.append(Clip(id="p1", src="/x/q.mp4", in_=0, out=4, start=2.0))
    s.edl.recompute_duration()
    assert s.edl.duration == pytest.approx(6.0)


# ------------------------------------------------------------ through dispatch

def test_add_and_remove_transition_move_the_duration(tmp_path):
    s = _store(tmp_path)
    s.commit("seed", {}, "seed")
    dispatch(s, "add_transition", {"track": "v1", "at": 2.0,
                                   "type": "fade", "duration": 0.5})
    assert s.edl.duration == pytest.approx(7.5)
    dispatch(s, "remove_transition", {"track": "v1", "at": 2.0})
    assert s.edl.duration == pytest.approx(8.0)
