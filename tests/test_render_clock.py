"""render/clock.py — the layout→render time map every non-v1 lane is placed
through. Pure arithmetic on `EDL.v1_seam_table()`; the seams themselves are
the schema's rule (tests/test_transition_duration.py), so these tests check
the MAP: identity without transitions, one seam, stacked seams, a window
inside a consumed span is dropped (never inverted), the numbers agree with
`transition_overlap()` / `recompute_duration()`, and a table or an EDL can be
passed interchangeably. The renders that prove the map is what the file
shows are in tests/test_transition_overlay_sync.py.
"""
from __future__ import annotations

import pytest

from video_ai_editor.edl.schema import EDL, Canvas, Clip, Track, Transition
from video_ai_editor.render import clock


def _edl(*seams: tuple[float, float, str], clips: tuple[float, ...] = (2.0, 2.0, 2.0),
         gap_before: int | None = None) -> EDL:
    """v1 with clips of the given lengths back to back (a 0.5 s gap before
    clip `gap_before` when given) and a transition per `(at, duration, type)`."""
    v1 = Track(id="v1", type="video")
    cursor = 0.0
    for i, d in enumerate(clips):
        if gap_before == i:
            cursor += 0.5
        v1.clips.append(Clip(id=f"c{i}", src="/x/a.mp4", in_=0.0, out=d, start=cursor))
        cursor += d
    v1.transitions = [Transition(at=at, duration=dur, type=ty) for at, dur, ty in seams]
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[v1])
    edl.recompute_duration()
    return edl


# ------------------------------------------------------------ no transitions

def test_no_transitions_is_the_identity():
    edl = _edl()
    assert clock.seam_table(edl) == []
    for t in (0.0, 1.0, 2.0, 3.99, 6.0, 42.0):
        assert clock.overlap_before(edl, t) == 0.0
        assert clock.render_time(edl, t) == t
    assert clock.render_window(edl, 3.0, 3.5) == (3.0, 3.5)


def test_a_window_with_no_length_is_dropped_even_without_seams():
    assert clock.render_window(_edl(), 3.0, 3.0) is None
    assert clock.render_window(_edl(), 3.0, 2.0) is None


# --------------------------------------------------------------- one seam

def test_one_seam_pulls_everything_at_or_after_it_left():
    edl = _edl((2.0, 0.5, "fade"))
    assert clock.seam_table(edl) == [(2.0, 0.5)]
    # Before the seam: untouched.
    assert clock.render_time(edl, 1.9) == pytest.approx(1.9)
    # AT the seam: clip B's first frame is already inside the crossfade.
    assert clock.render_time(edl, 2.0) == pytest.approx(1.5)
    assert clock.render_time(edl, 3.0) == pytest.approx(2.5)
    assert clock.overlap_before(edl, 2.0) == pytest.approx(0.5)
    assert clock.overlap_before(edl, 1.999) == 0.0


def test_a_window_on_clip_b_moves_and_one_on_clip_a_does_not():
    edl = _edl((2.0, 0.5, "fade"))
    assert clock.render_window(edl, 3.0, 3.5) == pytest.approx((2.5, 3.0))
    assert clock.render_window(edl, 1.6, 1.9) == pytest.approx((1.6, 1.9))


def test_a_window_inside_the_consumed_tail_is_dropped_not_inverted():
    """Clip A's last d seconds, `[s−d, s)`, map to `[s−d, s−d)` — the desktop
    draws such a clip as dropped (`renderWindow(...).dropped`), and the
    renderer must emit nothing for it rather than an inverted gate."""
    edl = _edl((2.0, 0.5, "fade"))
    assert clock.render_window(edl, 1.5, 2.0) is None
    assert clock.render_window(edl, 1.6, 1.9) is not None       # not consumed
    assert clock.render_window(edl, 1.7, 2.0) is None            # inside the tail


def test_a_window_straddling_the_seam_shrinks_by_the_overlap():
    """`[1.7, 2.3)` covers 0.3 s of A's tail and 0.3 s of B's head; on screen
    those share the crossfade window, so the item is visible for 0.1 s."""
    edl = _edl((2.0, 0.5, "fade"))
    assert clock.render_window(edl, 1.7, 2.3) == pytest.approx((1.7, 1.8))


def test_the_head_of_clip_b_shows_during_the_crossfade():
    edl = _edl((2.0, 0.5, "fade"))
    assert clock.render_window(edl, 2.0, 2.5) == pytest.approx((1.5, 2.0))


# ------------------------------------------------------------ stacked seams

def test_stacked_seams_accumulate():
    edl = _edl((2.0, 0.5, "fade"), (4.0, 0.3, "zoomin"))
    assert clock.seam_table(edl) == [(2.0, 0.5), (4.0, pytest.approx(0.3))]
    assert clock.overlap_before(edl, 3.99) == pytest.approx(0.5)
    assert clock.overlap_before(edl, 4.0) == pytest.approx(0.8)
    assert clock.render_time(edl, 5.0) == pytest.approx(4.2)
    assert clock.render_window(edl, 5.0, 5.5) == pytest.approx((4.2, 4.7))
    # Between the two seams only the first counts.
    assert clock.render_window(edl, 3.0, 3.5) == pytest.approx((2.5, 3.0))


def test_the_map_reaches_exactly_the_recomputed_duration():
    """`render_time(layout end)` IS `edl.duration`, and the overlap past the
    last seam IS `transition_overlap()` — one arithmetic, two readers."""
    edl = _edl((2.0, 0.5, "fade"), (4.0, 0.3, "zoomin"))
    layout_end = 6.0
    assert edl.duration == pytest.approx(layout_end - 0.8)
    assert clock.render_time(edl, layout_end) == pytest.approx(edl.duration)
    assert clock.overlap_before(edl, layout_end) == pytest.approx(edl.transition_overlap())


# ------------------------------------------------ the table is the schema's

def test_a_transition_across_a_gap_does_not_move_anything():
    edl = _edl((2.0, 0.5, "fade"), gap_before=1)   # clip B starts at 2.5
    assert edl.transition_overlap() == 0.0
    assert clock.seam_table(edl) == []
    assert clock.render_window(edl, 3.0, 3.5) == (3.0, 3.5)


def test_the_seam_tolerance_is_the_schema_s():
    inside = _edl((2.04, 0.5, "fade"))
    outside = _edl((2.06, 0.5, "fade"))
    assert clock.render_time(inside, 3.0) == pytest.approx(2.5)
    assert clock.render_time(outside, 3.0) == pytest.approx(3.0)


def test_the_overlap_is_clamped_to_the_shorter_clip():
    edl = _edl((2.0, 5.0, "fade"), clips=(2.0, 1.0, 2.0))
    assert clock.seam_table(edl) == [(2.0, 1.0)]
    assert clock.render_time(edl, 3.0) == pytest.approx(2.0)


# ---------------------------------------------- an EDL or a table, same answer

def test_a_precomputed_table_gives_the_same_answers_as_the_edl():
    edl = _edl((2.0, 0.5, "fade"), (4.0, 0.3, "zoomin"))
    table = clock.seam_table(edl)
    for t in (0.0, 1.9, 2.0, 3.0, 4.0, 5.0, 6.0):
        assert clock.render_time(table, t) == clock.render_time(edl, t)
    assert clock.render_window(table, 5.0, 5.5) == clock.render_window(edl, 5.0, 5.5)
    assert clock.render_window(table, 1.5, 2.0) is None


def test_the_helpers_do_not_touch_the_edl():
    edl = _edl((2.0, 0.5, "fade"))
    before = edl.model_dump(mode="json")
    clock.seam_table(edl)
    clock.render_window(edl, 0.0, 9.0)
    assert edl.model_dump(mode="json") == before
