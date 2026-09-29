"""Final QA run 3 (P3, K1's notDone): overlays FOLLOW the main-lane picture
they sit over through the remaining edit tools.

K1 made a PIP, title, sticker or caption keep its offset into the v1 clip
under it through ripple_delete / trim / speed / freeze / duplicate / move /
reorder / bulk ops / paste (`dispatch._follow_main_lane`). `cut_range` — and
so remove_silences / remove_fillers, which loop it — still only applied its
own interval remap: a layer over a picture AFTER a v1 gap moved by the cut
length while the repack moved its picture by the cut plus the gap, a layer
parked past v1's end slid left by the cut, and one over a gap slid too.
`set_property` moving a v1 clip left every layer over it behind.

Every scenario uses the same v1 (m0 [0,8), m1 [8,18), a gap [18,20), m2
[20,24)) with layers over the edited clip, over a later clip, over the gap
and past v1's end, on every overlay kind (PIP, text, sticker, caption).
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Canvas, Clip, Sticker, TextClip, Track

# id -> (lane, start, end)  (a PIP's end is start + its 1 s source range)
LAYERS: dict[str, tuple[str, float, float]] = {
    "p_over": ("v2", 5.0, 6.0),       # PIP over m0 (the clip most tools edit)
    "p_next": ("v2", 10.0, 11.0),     # PIP over m1
    "p_gap": ("v2", 19.0, 20.0),      # PIP over the v1 gap
    "p_past": ("v2", 30.0, 31.0),     # PIP past v1's end
    "t_over": ("tx", 5.0, 6.0),
    "t_next": ("tx", 10.0, 11.0),
    "t_gap": ("tx", 18.5, 19.5),
    "t_after": ("tx", 21.0, 22.0),    # over m2, after the gap
    "t_past": ("tx", 30.0, 31.0),
    "s_next": ("st", 10.0, 11.0),
    "s_after": ("st", 21.0, 22.0),
    "s_past": ("st", 30.0, 31.0),
    "c_next": ("cap", 10.5, 11.5),
    "c_after": ("cap", 21.5, 22.5),
}


def _store(tmp: Path | None = None) -> EDLStore:
    tmp = Path(tmp or tempfile.mkdtemp())
    media = tmp / "missing"            # never probed successfully
    v1 = [Clip(id="m0", src=str(media / "a.mp4"), in_=0.0, out=8.0, start=0.0),
          Clip(id="m1", src=str(media / "b.mp4"), in_=0.0, out=10.0, start=8.0),
          Clip(id="m2", src=str(media / "c.mp4"), in_=0.0, out=4.0, start=20.0)]
    lanes: dict[str, list] = {"v2": [], "tx": [], "st": [], "cap": []}
    for lid, (lane, s, e) in LAYERS.items():
        if lane == "v2":
            lanes[lane].append(Clip(id=lid, src=str(media / "pip.mp4"), in_=0.0, out=e - s, start=s))
        elif lane == "st":
            lanes[lane].append(Sticker(id=lid, src="x.png", start=s, end=e))
        elif lane == "cap":
            lanes[lane].append(TextClip(id=lid, text=lid, start=s, end=e, role="caption"))
        else:
            lanes[lane].append(TextClip(id=lid, text=lid, start=s, end=e))
    edl = EDL(canvas=Canvas(w=1920, h=1080, fps=30), tracks=[
        Track(id="v1", type="video", clips=v1),
        Track(id="v2", type="video", z=5, clips=lanes["v2"]),
        Track(id="tx", type="text", z=6, clips=lanes["tx"]),
        Track(id="st", type="sticker", z=7, clips=lanes["st"]),
        Track(id="cap", type="captions", z=8, clips=lanes["cap"]),
    ])
    edl.recompute_duration()
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(tmp)


def _v1(store: EDLStore) -> list[tuple[float, float]]:
    return sorted((round(c.start, 6), round(c.start + c.effective_duration, 6))
                  for c in store.edl.get_track("v1").clips if isinstance(c, Clip))


def _layers(store: EDLStore) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for lane in ("v2", "tx", "st", "cap"):
        for c in store.edl.get_track(lane).clips:
            end = c.start + c.effective_duration if isinstance(c, Clip) else c.end
            out[c.id] = (round(c.start, 6), round(end, 6))
    return out


def _moved(**shifts: float) -> dict[str, tuple[float, float]]:
    """Every layer where it was, except `id=new_start` (same length)."""
    out = {lid: (s, e) for lid, (_lane, s, e) in LAYERS.items()}
    for lid, ns in shifts.items():
        s, e = out[lid]
        out[lid] = (ns, round(ns + (e - s), 6))
    return out


#: The layout every cut of timeline [2, 4) out of m0 leaves: m0 [0,2), its
#: tail piece [2,6), m1 [6,16), the gap closed by the magnetic repack, m2
#: [16,20) — m2 moved 4 s (the cut AND the gap), m1 2 s.
CUT_2_4_V1 = [(0.0, 2.0), (2.0, 6.0), (6.0, 16.0), (16.0, 20.0)]
CUT_2_4_LAYERS = _moved(
    p_over=3.0, t_over=3.0,                              # over m0: minus the cut
    p_next=8.0, t_next=8.0, s_next=8.0, c_next=8.5,      # over m1: its 2 s move
    t_after=17.0, s_after=17.0, c_after=17.5,            # over m2: its 4 s move
    # p_gap / t_gap (over the gap) and *_past keep their time
)


# ------------------------------------------------------------------ cut_range

def test_cut_range_layers_follow_their_pictures_across_a_gap_and_past_the_end():
    store = _store()
    dispatch(store, "cut_range", {"track": "v1", "start": 2.0, "end": 4.0})
    assert _v1(store) == CUT_2_4_V1
    assert _layers(store) == CUT_2_4_LAYERS


def test_cut_range_and_its_layer_follow_are_one_undo_step():
    store = _store()
    before = _layers(store)
    dispatch(store, "cut_range", {"track": "v1", "start": 2.0, "end": 4.0})
    dispatch(store, "undo", {})
    assert _layers(store) == before
    assert _v1(store) == [(0.0, 8.0), (8.0, 18.0), (20.0, 24.0)]


def test_cut_range_on_a_gapless_v1_agrees_with_the_interval_remap():
    """With no gap and nothing past the end the follow pass has nothing to
    correct: every layer lands exactly where the interval remap puts it
    (removed [2, 4): after 4 -> minus 2, inside -> 2)."""
    store = _store()
    edl = store.edl
    edl.get_track("v1").clips[2].start = 18.0          # close the gap: m2 [18, 22)
    for lane in ("v2", "tx", "st", "cap"):             # nothing past the end / over a gap
        t = edl.get_track(lane)
        t.clips = [c for c in t.clips if c.id not in ("p_gap", "t_gap", "p_past", "t_past", "s_past")]
    edl.get_clip("t_after")[1].start, edl.get_clip("t_after")[1].end = 19.0, 20.0
    (store.dir / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(store.dir)
    dispatch(store, "cut_range", {"track": "v1", "start": 2.0, "end": 4.0})
    got = _layers(store)
    assert got["t_over"] == (3.0, 4.0) and got["p_over"] == (3.0, 4.0)
    assert got["t_next"] == (8.0, 9.0) and got["c_next"] == (8.5, 9.5)
    assert got["t_after"] == (17.0, 18.0) and got["s_after"] == (19.0, 20.0)


def test_cut_range_a_title_over_the_cut_keeps_the_interval_remap():
    """The per-tool remap stays authoritative over the EDITED picture: a
    title straddling the cut's end shrinks to what is left of it."""
    store = _store()
    t = store.edl.get_clip("t_over")[1]
    t.start, t.end = 3.0, 6.0                          # [3, 6) over m0, cut [2, 4)
    dispatch(store, "cut_range", {"track": "v1", "start": 2.0, "end": 4.0})
    assert _layers(store)["t_over"] == (2.0, 4.0)


# -------------------------------------------------------------------- split_at

def test_split_at_moves_no_layer():
    store = _store()
    dispatch(store, "split_at", {"track": "v1", "time": 4.0})
    assert _v1(store) == [(0.0, 4.0), (4.0, 8.0), (8.0, 18.0), (20.0, 24.0)]
    assert _layers(store) == _moved()


# ---------------------------------------------------- remove_silences / fillers

def _stub_silence_in(monkeypatch, src_name: str, stderr: str) -> None:
    """silencedetect reports `stderr` for `src_name`'s slice and nothing for
    any other clip; every other subprocess runs for real."""
    class _Proc:
        returncode, stdout = 0, ""

        def __init__(self, err: str):
            self.stderr = err

    real_run = subprocess.run

    def fake_run(argv, *a, **kw):
        line = " ".join(map(str, argv))
        if "silencedetect" in line:
            return _Proc(stderr if src_name in line else "")
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_remove_silences_layers_follow_their_pictures(monkeypatch):
    store = _store()
    _stub_silence_in(monkeypatch, "a.mp4", "silence_start: 2.0\nsilence_end: 4.0\n")
    r = dispatch(store, "remove_silences", {"keep_pad": 0.0, "min_dur": 0.5})
    assert r["cuts"] == 1
    assert _v1(store) == CUT_2_4_V1
    assert _layers(store) == CUT_2_4_LAYERS
    dispatch(store, "undo", {})                        # one undo step
    assert _layers(store) == _moved()


def test_remove_silences_with_two_cuts_across_the_gap(monkeypatch):
    """Two cuts in ONE pass: the first repack closes the gap, so the second
    is mapped against the packed lane — every cut's follow is exact."""
    store = _store()
    _stub_silence_in(monkeypatch, "b.mp4", "silence_start: 1.0\nsilence_end: 2.0\n"
                                           "silence_start: 6.0\nsilence_end: 7.0\n")
    r = dispatch(store, "remove_silences", {"keep_pad": 0.0, "min_dur": 0.5})
    assert r["cuts"] == 2
    # m1 [8,18) loses source [1,2) and [6,7): 8 s long; the gap closes.
    assert _v1(store) == [(0.0, 8.0), (8.0, 9.0), (9.0, 13.0), (13.0, 16.0), (16.0, 20.0)]
    got = _layers(store)
    assert got["t_next"] == (9.0, 10.0)                # source 2 of m1 -> 8 + 1
    assert got["c_next"] == (9.5, 10.5)
    assert got["t_after"] == (17.0, 18.0)              # m2 moved 4 s (2 cut + 2 gap)
    assert got["c_after"] == (17.5, 18.5)
    assert got["t_gap"] == (18.5, 19.5) and got["p_gap"] == (19.0, 20.0)
    assert got["t_past"] == (30.0, 31.0) and got["p_past"] == (30.0, 31.0)
    assert got["t_over"] == (5.0, 6.0)                 # over m0, before both cuts


def test_remove_fillers_layers_follow_their_pictures():
    store = _store()
    a = store.edl.get_clip("m0")[1].src
    (store.dir / "transcript.json").write_text(
        '{"language": "en", "duration": 8.0, "segments": [{"id": 0, "start": 0.5, '
        '"end": 6.0, "text": "so um yes", "words": ['
        '{"word": "so", "start": 0.5, "end": 1.0, "prob": 1.0},'
        '{"word": "um", "start": 2.0, "end": 4.0, "prob": 1.0},'
        '{"word": "yes", "start": 5.0, "end": 6.0, "prob": 1.0}]}]}', encoding="utf-8")
    assert a.endswith("a.mp4")
    r = dispatch(store, "remove_fillers", {"words": ["um"], "pad": 0.0})
    assert r["cuts"] == 1
    assert _v1(store) == CUT_2_4_V1
    assert _layers(store) == CUT_2_4_LAYERS


# ---------------------------------------------------------------- set_property

def test_set_property_moving_a_v1_clip_carries_what_sits_over_it():
    store = _store()
    dispatch(store, "set_property", {"clip_id": "m1", "path": "start", "value": 9.0})
    assert _v1(store) == [(0.0, 8.0), (9.0, 19.0), (20.0, 24.0)]
    # Over m1: +1. Over the gap (now under m1's tail), past the end, over
    # m0 and over m2: where they were.
    assert _layers(store) == _moved(p_next=11.0, t_next=11.0, s_next=11.0, c_next=11.5)
    dispatch(store, "undo", {})                        # one undo step
    assert _layers(store) == _moved()


def test_set_property_retiming_a_v1_clip_is_set_speed():
    """Final sweep 3: a v1 speed through set_property keeps the main lane
    magnetic, exactly like set_speed (the Alt-drag test below). It used to
    leave m0 [0,4) and m1 at 8: a 4 s black hole in the export."""
    store = _store()
    dispatch(store, "set_property", {"clip_id": "m0", "path": "speed", "value": 2.0})
    assert _v1(store) == [(0.0, 4.0), (4.0, 14.0), (14.0, 18.0)]
    got = _layers(store)
    assert got["t_over"] == (2.5, 3.0) and got["p_over"][0] == 2.5   # retimed with m0
    assert got["t_next"] == (6.0, 7.0) and got["c_next"] == (6.5, 7.5)
    assert got["t_after"] == (15.0, 16.0) and got["s_after"] == (15.0, 16.0)
    assert got["t_gap"] == (18.5, 19.5) and got["p_gap"] == (19.0, 20.0)
    assert got["t_past"] == (30.0, 31.0) and got["p_past"] == (30.0, 31.0)
    dispatch(store, "undo", {})                        # one undo step
    assert _layers(store) == _moved()
    assert _v1(store) == [(0.0, 8.0), (8.0, 18.0), (20.0, 24.0)]


def test_set_property_on_a_layer_is_that_layers_own_edit():
    store = _store()
    dispatch(store, "set_property", {"clip_id": "t_next", "path": "start", "value": 10.5})
    assert _layers(store) == {**_moved(), "t_next": (10.5, 11.0)}


# ------------------------------------------------------------- set_clip_timing

def test_set_clip_timing_moves_only_the_named_layer():
    store = _store()
    dispatch(store, "set_clip_timing", {"clip_id": "t_next", "start": 12.0, "end": 13.0})
    assert _layers(store) == _moved(t_next=12.0)
    assert _v1(store) == [(0.0, 8.0), (8.0, 18.0), (20.0, 24.0)]
    with pytest.raises(ValueError):                    # never a v1 picture
        dispatch(store, "set_clip_timing", {"clip_id": "m1", "start": 9.0})


# ------------------------------------------------ the Timeline's edge-drag trims
# Exactly what Timeline.tsx sends on release (trim-l / trim-r / Alt-drag).

def test_edge_drag_right_edge_in_carries_every_later_layer():
    store = _store()
    dispatch(store, "trim_clip", {"clip_id": "m0", "out": 6.0})
    assert _v1(store) == [(0.0, 6.0), (6.0, 16.0), (16.0, 20.0)]
    assert _layers(store) == _moved(
        p_next=8.0, t_next=8.0, s_next=8.0, c_next=8.5,
        t_after=17.0, s_after=17.0, c_after=17.5)


def test_edge_drag_left_edge_of_a_later_clip():
    store = _store()
    dispatch(store, "trim_clip", {"clip_id": "m1", "in": 2.0})
    assert _v1(store) == [(0.0, 8.0), (8.0, 16.0), (16.0, 20.0)]
    # Over m1's kept part: minus the 2 s head; over m2: its 4 s move.
    assert _layers(store) == _moved(
        p_next=8.0, t_next=8.0, s_next=8.0, c_next=8.5,
        t_after=17.0, s_after=17.0, c_after=17.5)


def test_edge_drag_extending_a_clip_pushes_later_layers_and_keeps_the_gap_one():
    store = _store()
    dispatch(store, "trim_clip", {"clip_id": "m0", "out": 10.0})
    # m0 +2 s; m1 [10,20); the gap is closed, so m2 stays at 20.
    assert _v1(store) == [(0.0, 10.0), (10.0, 20.0), (20.0, 24.0)]
    assert _layers(store) == _moved(p_next=12.0, t_next=12.0, s_next=12.0, c_next=12.5)


def test_edge_drag_with_alt_retimes_and_carries_layers():
    store = _store()
    dispatch(store, "set_speed", {"clip_id": "m0", "factor": 2.0})
    assert _v1(store) == [(0.0, 4.0), (4.0, 14.0), (14.0, 18.0)]
    got = _layers(store)
    assert got["t_over"] == (2.5, 3.0) and got["p_over"][0] == 2.5   # retimed with m0
    assert got["t_next"] == (6.0, 7.0) and got["c_next"] == (6.5, 7.5)
    assert got["t_after"] == (15.0, 16.0) and got["s_after"] == (15.0, 16.0)
    assert got["t_gap"] == (18.5, 19.5) and got["p_gap"] == (19.0, 20.0)
    assert got["t_past"] == (30.0, 31.0) and got["p_past"] == (30.0, 31.0)


# -------------------------------------------------------------- add_clip (v1)

def test_add_clip_inserting_on_v1_moves_only_the_layers_over_moved_pictures(tmp_path):
    """Final sweep 3: Insert at playhead / a timeline drop (add_clip on v1 at
    4.0 for 2 s). The gap after m1 absorbs the push, so m2 does not move and
    neither do the layers over it, over the gap or past v1's end. They all
    used to move +2 s (`_shift_overlays_after` on everything after 4.0)."""
    ins = tmp_path / "ins.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=s=320x180:r=30:d=4", "-pix_fmt", "yuv420p", str(ins)], check=True)
    store = _store(tmp_path / "s")
    dispatch(store, "add_clip", {"track": "v1", "src": str(ins), "in": 0, "out": 2, "start": 4.0})
    assert _v1(store) == [(0.0, 4.0), (4.0, 6.0), (6.0, 10.0), (10.0, 20.0), (20.0, 24.0)]
    assert _layers(store) == _moved(
        p_over=7.0, t_over=7.0,                            # m0's tail moved +2
        p_next=12.0, t_next=12.0, s_next=12.0, c_next=12.5,  # m1 moved +2
        # t_after/s_after/c_after (m2 did not move), the gap and *_past keep theirs
    )
