"""Final QA: a v1 transition belongs to the SEAM between two clips, not to a
time on the ruler.

`_ripple_close_gap` repacks the main lane after a delete / trim / cut / move /
duplicate / speed change, and it never touched `track.transitions`. So:

  * deleting the FIRST of four 2 s clips (dissolve @2, fade @4) left the
    transitions at 2 and 4: the dissolve that belonged to the deleted clip
    slid onto city->mountains and the fade onto mountains->vertical;
  * trimming clip 2 to 1.5 s moved the seams to 2 / 3.5 / 5.5 while the fade
    stayed at 4 — on no seam, so it was dropped silently (a hard cut in the
    export where the fade had been).

A transition now travels with its seam (the pair of clips either side of it),
and is dropped when that seam no longer exists (one of its clips was deleted,
or the two are no longer neighbours).

The magnetic-insert half (add_clip landing inside a main-track clip) is
pinned at the bottom: it used to overlap two main-track clips, so the
timeline showed one edit and the export played another.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Canvas, Clip, Track, Transition
from video_ai_editor.render import render_export

from test_transition_overlay_sync import _frames, _lavfi  # noqa: E402  (pure helpers)

W, H, FPS = 320, 180, 30


def _store(srcs: list[str] | None = None, *, dur: float = 2.0,
           transitions: list[Transition] | None = None) -> EDLStore:
    tmp = tempfile.mkdtemp()
    names = ["a", "b", "c", "d"]
    srcs = srcs or [str(Path(tmp) / "missing" / f"{n}.mp4") for n in names]
    clips = [Clip(id=n, src=s, in_=0.0, out=dur, start=i * dur)
             for i, (n, s) in enumerate(zip(names, srcs))]
    edl = EDL(canvas=Canvas(w=W, h=H, fps=FPS, loudness_lufs=None), tracks=[
        Track(id="v1", type="video", clips=clips, transitions=list(transitions or [
            Transition(at=2.0, type="dissolve", duration=0.5),
            Transition(at=4.0, type="fade", duration=0.5)])),
        Track(id="v2", type="video", z=1),
        Track(id="music", type="music", z=0),
    ])
    edl.recompute_duration()
    (Path(tmp) / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(Path(tmp))


def _seams(store: EDLStore) -> list[tuple[str, str, float]]:
    """Every v1 neighbour pair with its seam time."""
    v1 = sorted(store.edl.get_track("v1").clips, key=lambda c: c.start)
    return [(a.id, b.id, round(b.start, 4)) for a, b in zip(v1, v1[1:])]


def _transitions(store: EDLStore) -> list[tuple[float, str]]:
    return sorted((round(t.at, 4), t.type) for t in store.edl.get_track("v1").transitions)


# ------------------------------------------------------------------ EDL level

def test_delete_first_clip_drops_its_transition_and_keeps_the_fade_on_b_to_c():
    store = _store()
    dispatch(store, "ripple_delete", {"clip_id": "a"})
    assert _seams(store) == [("b", "c", 2.0), ("c", "d", 4.0)]
    # The dissolve belonged to a->b (a is gone); the fade stays on b->c.
    assert _transitions(store) == [(2.0, "fade")]


def test_delete_middle_clip_drops_both_of_its_transitions():
    store = _store()
    dispatch(store, "ripple_delete", {"clip_id": "b"})
    assert _seams(store) == [("a", "c", 2.0), ("c", "d", 4.0)]
    assert _transitions(store) == []


def test_trim_a_middle_clip_moves_the_fade_to_the_new_seam():
    store = _store()
    dispatch(store, "trim_clip", {"clip_id": "b", "in": 0.0, "out": 1.5})
    assert _seams(store) == [("a", "b", 2.0), ("b", "c", 3.5), ("c", "d", 5.5)]
    assert _transitions(store) == [(2.0, "dissolve"), (3.5, "fade")]
    # ...and the fade still renders: it is charged in the timeline length.
    assert store.edl.duration == pytest.approx(7.5 - 1.0)


def test_head_trim_of_the_first_clip_shifts_both_transitions():
    store = _store()
    dispatch(store, "trim_clip", {"clip_id": "a", "in": 0.5, "out": 2.0})
    assert _transitions(store) == [(1.5, "dissolve"), (3.5, "fade")]


def test_cut_range_inside_a_clip_keeps_the_transitions_on_their_seams():
    store = _store()
    # Removes 0.5 s from the middle of b (b is split around the cut): the
    # fade on b's TAIL (b->c) follows the right-hand piece.
    dispatch(store, "cut_range", {"track": "v1", "start": 2.5, "end": 3.0})
    assert _transitions(store) == [(2.0, "dissolve"), (3.5, "fade")]


def test_cut_range_that_removes_a_whole_clip_drops_its_transitions():
    store = _store()
    dispatch(store, "cut_range", {"track": "v1", "start": 2.0, "end": 4.0})
    assert _transitions(store) == []


def test_reorder_drops_transitions_whose_neighbours_changed():
    store = _store(transitions=[Transition(at=2.0, type="dissolve", duration=0.5),
                                Transition(at=6.0, type="wipeleft", duration=0.5)])
    # a b c d -> b a c d: a->b no longer exists as a pair (b->a is a new seam);
    # c->d is untouched.
    dispatch(store, "reorder_clips", {"track": "v1", "order": ["b", "a", "c", "d"]})
    assert _transitions(store) == [(6.0, "wipeleft")]


def test_speed_change_carries_later_transitions():
    store = _store()
    dispatch(store, "set_speed", {"clip_id": "a", "factor": 2.0})
    # a now plays 1 s: seams at 1, 3, 5.
    assert _transitions(store) == [(1.0, "dissolve"), (3.0, "fade")]


def test_duplicate_keeps_the_outgoing_transition_after_the_copy():
    store = _store()
    out = dispatch(store, "duplicate_clip", {"clip_id": "b"})
    new = out["new_clip_id"]
    assert _seams(store) == [("a", "b", 2.0), ("b", new, 4.0), (new, "c", 6.0), ("c", "d", 8.0)]
    # b's tail (b->c fade) now sits after the copy, on copy->c.
    assert _transitions(store) == [(2.0, "dissolve"), (6.0, "fade")]


def test_ripple_delete_on_a_pip_lane_does_not_touch_v1_transitions():
    store = _store()
    store.edl.get_track("v2").clips.append(
        Clip(id="p", src=store.edl.get_track("v1").clips[0].src, in_=0, out=1, start=1.0))
    dispatch(store, "ripple_delete", {"clip_id": "p"})
    assert _transitions(store) == [(2.0, "dissolve"), (4.0, "fade")]


# ---------------------------------------------------------------- decoded export

@pytest.fixture(scope="module")
def colours(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("seam_follow")
    for name in ("red", "blue", "lime", "yellow"):
        _lavfi(d / f"{name}.mp4", video=f"color=c={name}",
               audio="anullsrc=r=48000:cl=stereo", seconds=2)
    return {p.stem: p for p in d.iterdir()}


def _is(rgb, name: str) -> bool:
    r, g, b = rgb
    return {"red": r > 200 and g < 40 and b < 40,
            "blue": b > 200 and r < 40 and g < 40,
            "lime": g > 200 and r < 40 and b < 40,
            "yellow": r > 200 and g > 200 and b < 40}[name]


def _blended_between(frames, lo: float, hi: float, a: str, b: str) -> int:
    """How many frames in [lo, hi) are neither pure `a` nor pure `b`."""
    return sum(1 for t, rgb in frames if lo <= t < hi and not _is(rgb, a) and not _is(rgb, b))


def _export(store: EDLStore, where: Path) -> Path:
    return render_export(store.edl, where, height=H).path


def test_export_after_deleting_the_first_clip_fades_blue_to_lime(tmp_path, colours):
    srcs = [str(colours[n]) for n in ("red", "blue", "lime", "yellow")]
    store = _store(srcs, transitions=[Transition(at=2.0, type="dissolve", duration=0.5),
                                      Transition(at=4.0, type="fade", duration=0.5)])
    dispatch(store, "ripple_delete", {"clip_id": "a"})
    frames = _frames(_export(store, tmp_path))
    # blue 0-2, lime 2-4 (the fade eats 0.5 s: seam renders around 1.5-2.0),
    # yellow after — a HARD cut from lime to yellow.
    assert _blended_between(frames, 1.4, 2.1, "blue", "lime") >= 5
    assert _blended_between(frames, 3.0, 4.0, "lime", "yellow") == 0


def test_export_after_trimming_the_middle_clip_still_fades_blue_to_lime(tmp_path, colours):
    srcs = [str(colours[n]) for n in ("red", "blue", "lime", "yellow")]
    store = _store(srcs, transitions=[Transition(at=2.0, type="dissolve", duration=0.5),
                                      Transition(at=4.0, type="fade", duration=0.5)])
    dispatch(store, "trim_clip", {"clip_id": "b", "in": 0.0, "out": 1.5})
    path = _export(store, tmp_path)
    frames = _frames(path)
    # Render clock: dissolve at 2 eats 0.5 (seam renders 1.5-2.0), blue then
    # plays to render 3.0, where the fade (another 0.5) blends into lime.
    assert _blended_between(frames, 1.4, 2.1, "red", "blue") >= 5
    assert _blended_between(frames, 2.4, 3.1, "blue", "lime") >= 5
    assert frames[-1][0] == pytest.approx(store.edl.duration - 1 / FPS, abs=2 / FPS)


# ------------------------------------------------------------ magnetic insert

def _mag_store(tmp: Path, main: Path) -> EDLStore:
    edl = EDL(canvas=Canvas(w=W, h=H, fps=FPS, loudness_lufs=None), tracks=[
        Track(id="v1", type="video", clips=[Clip(id="m", src=str(main), in_=0, out=20, start=0)]),
        Track(id="v2", type="video", z=1),
        Track(id="music", type="music", z=0),
    ])
    edl.recompute_duration()
    (tmp / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(tmp)


@pytest.fixture(scope="module")
def long_media(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("mag")
    _lavfi(d / "main20.mp4", video="color=c=red", audio="anullsrc=r=48000:cl=stereo", seconds=20)
    _lavfi(d / "r12.mp4", video="color=c=blue", audio="anullsrc=r=48000:cl=stereo", seconds=12)
    return {p.stem: p for p in d.iterdir()}


def test_add_clip_inside_a_main_clip_splits_it_and_inserts(tmp_path, long_media):
    store = _mag_store(tmp_path, long_media["main20"])
    out = dispatch(store, "add_clip", {"track": "v1", "src": str(long_media["r12"]),
                                       "in": 0, "out": 12, "start": 4})
    v1 = sorted(store.edl.get_track("v1").clips, key=lambda c: c.start)
    spans = [(round(c.start, 3), round(c.start + c.effective_duration, 3)) for c in v1]
    assert spans == [(0.0, 4.0), (4.0, 16.0), (16.0, 32.0)]
    assert v1[1].id == out["clip_id"]
    # The two halves of the main clip play its source continuously.
    assert (v1[0].in_, v1[0].out) == (0.0, 4.0) and (v1[2].in_, v1[2].out) == (4.0, 20.0)
    assert store.edl.duration == pytest.approx(32.0)


def test_add_clip_inside_a_main_clip_exports_in_timeline_order(tmp_path, long_media):
    store = _mag_store(tmp_path, long_media["main20"])
    dispatch(store, "add_clip", {"track": "v1", "src": str(long_media["r12"]),
                                 "in": 0, "out": 12, "start": 4})
    frames = _frames(render_export(store.edl, tmp_path / "out", height=H).path)
    at = {round(t): rgb for t, rgb in frames}
    assert _is(at[2], "red") and _is(at[5], "blue") and _is(at[10], "blue")
    assert _is(at[17], "red") and _is(at[30], "red")


def test_add_clip_on_a_seam_inserts_there_and_shifts_later_clips_and_transitions():
    store = _store()
    src = store.edl.get_track("v1").clips[0].src
    new = dispatch(store, "add_clip", {"track": "v1", "src": src, "in": 0, "out": 1,
                                       "start": 4})["clip_id"]
    assert _seams(store) == [("a", "b", 2.0), ("b", new, 4.0), (new, "c", 5.0), ("c", "d", 7.0)]
    # b->c's fade no longer has a seam (a clip now sits between them); a->b
    # keeps its dissolve.
    assert _transitions(store) == [(2.0, "dissolve")]
    assert store.edl.get_clip("d")[1].start == pytest.approx(7.0)


def test_add_clip_past_the_end_of_the_main_lane_still_appends():
    store = _store(transitions=[])
    src = store.edl.get_track("v1").clips[0].src
    out = dispatch(store, "add_clip", {"track": "v1", "src": src, "in": 0, "out": 1, "start": 8})
    assert store.edl.get_clip(out["clip_id"])[1].start == pytest.approx(8.0)
    assert [c.start for c in store.edl.get_track("v1").clips] == [0.0, 2.0, 4.0, 6.0, 8.0]
