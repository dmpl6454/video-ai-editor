"""Volume automation (`AudioProps.gain_env`) survives every edit that removes
the head of a clip (wave E, d2-followups item 25).

The envelope is keyed in clip-local TIMELINE seconds. `split_at` re-based it
(QA-086), but `cut_range` and a head trim (`trim_clip`, the plain path, the
speed-curve path, a reversed clip) kept the whole clip's keys: the piece that
now started later replayed the envelope from its first key — a level jump of
up to the envelope's whole range (-18 dB on the fixture). Each piece now
carries the part of the envelope it plays, re-based to its own 0
(`dispatch._piece_gain_env`, `_piece_kf`: exact for every interpolation).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl import timebase as tb
from video_ai_editor.edl.keyframes import sample
from video_ai_editor.edl.schema import Canvas, Keyframe
from video_ai_editor.render import compositor

INTERPS = ["linear", "ease-in", "ease-out", "ease-in-out", "step", "back-out"]
KEYS = [(0.2, 0.0), (1.1, -18.0), (2.3, -6.0), (3.4, 2.0)]
FPS = 30


def _store(tmp: Path, *, track: str, speed, reverse: bool, interp: str) -> tuple[EDLStore, str]:
    st = EDLStore(tmp)
    st.edl.canvas = Canvas(w=64, h=36, fps=FPS)
    st.edl.canvas.loudness_lufs = None
    st.commit("init", {}, "init")
    if track != "v1":
        dispatch(st, "add_clip", {"track": "v1", "src": "bars.mp4", "in": 0.0, "out": 20.0, "start": 0.0})
    cid = dispatch(st, "add_clip", {"track": track, "src": "bars.mp4", "in": 2.0, "out": 6.0,
                                    "start": 0.0 if track == "v1" else 1.0})["clip_id"]
    if reverse:
        dispatch(st, "set_clip_reverse", {"clip_id": cid, "reverse": True})
    if isinstance(speed, str):
        dispatch(st, "set_speed", {"clip_id": cid, "preset": speed})
    elif speed is not None:
        dispatch(st, "set_speed", {"clip_id": cid, "factor": speed})
    c = st.edl.get_clip(cid)[1]
    c.audio.gain_env = Keyframe(keyframes=KEYS, interp=interp)
    st.commit("env", {}, "env")
    return st, cid


def _levels(st: EDLStore, track: str) -> list[float]:
    """The envelope's dB at every output frame of the lane (clip by clip)."""
    out: list[float] = []
    for c in sorted(st.edl.get_track(track).clips, key=lambda c: c.start):
        env = c.audio.gain_env
        for j in range(compositor.clip_frames(c, FPS)):
            out.append(sample(env, tb.time_of(j, FPS)) if env is not None else 0.0)
    return out


def _bad(got: list[float], want: list[float]) -> list[int]:
    assert len(got) == len(want)
    return [i for i, (a, b) in enumerate(zip(got, want)) if abs(a - b) > 1e-6]


@pytest.mark.parametrize("interp", INTERPS)
@pytest.mark.parametrize("track", ["v1", "v2"])
def test_cut_range_keeps_the_envelope_on_its_frames(tmp_path, interp, track):
    st, cid = _store(tmp_path / "s", track=track, speed=None, reverse=False, interp=interp)
    whole = _levels(st, track)
    k0 = tb.frame_of(st.edl.get_clip(cid)[1].start, FPS)
    for i, (a, b) in enumerate([(20, 35), (0, 14), (80, 120)]):     # a hole, the head, the tail
        s2 = EDLStore(tmp_path / f"c{i}")
        s2.edl = st.edl.model_copy(deep=True)
        dispatch(s2, "cut_range", {"track": track, "start": tb.time_of(k0 + a, FPS),
                                   "end": tb.time_of(k0 + b, FPS)})
        assert _bad(_levels(s2, track), whole[:a] + whole[b:]) == [], (a, b)


@pytest.mark.parametrize("interp", ["linear", "ease-in-out", "step"])
@pytest.mark.parametrize("track,speed,reverse", [("v1", None, False), ("v1", 2.0, False), ("v2", None, False),
                                                 ("v1", None, True), ("v1", "hero", False),
                                                 ("v1", "hero", True), ("v2", "montage", True)],
                         ids=["v1_1x", "v1_2x", "v2_1x", "v1_rev", "v1_hero", "v1_rev_hero", "v2_rev_montage"])
def test_a_head_trim_keeps_the_envelope_on_its_frames(tmp_path, interp, track, speed, reverse):
    """The HEAD of the footprint goes: `in` up on a forward clip, `out` down
    on a reversed one (it plays `out` first)."""
    st, cid = _store(tmp_path / "s", track=track, speed=speed, reverse=reverse, interp=interp)
    whole = _levels(st, track)
    c = st.edl.get_clip(cid)[1]
    head = 24
    src_at = c.source_offset_at(tb.time_of(head, FPS))
    args = {"clip_id": cid, **({"out": c.out - src_at} if reverse else {"in": c.in_ + src_at})}
    if track != "v1":
        args["move_start"] = True
    dispatch(st, "trim_clip", args)
    got = _levels(st, track)
    assert _bad(got, whole[len(whole) - len(got):]) == []
    assert len(whole) - len(got) == head


def test_a_head_trim_that_extends_the_clip_moves_the_keys_later(tmp_path):
    st, cid = _store(tmp_path / "s", track="v1", speed=None, reverse=False, interp="linear")
    whole = _levels(st, "v1")
    dispatch(st, "trim_clip", {"clip_id": cid, "in": 1.0})
    got = _levels(st, "v1")
    assert _bad(got[30:], whole) == []
    assert [k[0] for k in st.edl.get_clip(cid)[1].audio.gain_env.keyframes] == [k[0] + 1.0 for k in KEYS]


def test_a_piece_past_the_last_key_holds_that_level(tmp_path):
    """A piece wholly after the last key is that constant: one key, or no
    automation at all when it is 0 dB."""
    st, cid = _store(tmp_path / "s", track="v1", speed=None, reverse=False, interp="linear")
    dispatch(st, "trim_clip", {"clip_id": cid, "in": 2.0 + 3.6})
    env = st.edl.get_clip(cid)[1].audio.gain_env
    assert env is not None and [tuple(k) for k in env.keyframes] == [(0.0, 2.0)]
