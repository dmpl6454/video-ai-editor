"""`apply_camera_plan` — the Editor Brain's angle-swap tool (EB1-B, spec §5.4 row 2).

Per switch: `split_at` at the live times of `at_src` / `until_src` on the v1
piece(s) whose source span contains them, then swap the piece's `src` to the
angle and shift `in`/`out` by `offsets[angle] − offsets[src]`, keeping
transform, effects, audio, keyframes and fades. Never clears v1. One commit.

Bar-coded sources (`frame_map_golden_lib`): every decoded frame of a render
names its angle and frame index, so "the swapped span shows angle B at
`t + offset`" is measured on pixels, not on the EDL alone.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain_tool_fixtures as BT  # noqa: E402
import frame_map_golden_lib as bars  # noqa: E402

from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.agent.prompt import changes as C  # noqa: E402
from video_ai_editor.edl.schema import Keyframe  # noqa: E402
from video_ai_editor.render import render_export  # noqa: E402


FPS = 30
A_S, B_S, SHORT_S = 8.0, 8.0, 4.0


@pytest.fixture(autouse=True)
def _posture():
    with BT.restriction_off():
        yield


@pytest.fixture(scope="module")
def angles(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("angles")
    return {"A": BT.make_bar_angle(d / "camA.mp4", sid=1, seconds=A_S),
            "B": BT.make_bar_angle(d / "camB.mp4", sid=2, seconds=B_S),
            "short": BT.make_bar_angle(d / "camShort.mp4", sid=3, seconds=SHORT_S)}


def _store(tmp_path: Path, angles) -> tuple:
    store = BT.session(tmp_path)
    dispatch(store, "add_clip", {"track": "v1", "src": str(angles["A"]), "in": 0, "out": A_S, "start": 0})
    return store, str(BT.v1_pieces(store)[0].src)


def _switch(src, at, until, angle) -> dict:
    return {"src": src, "at_src": at, "until_src": until, "angle_src": str(angle)}


def _spans(store) -> list[tuple[str, float, float, float]]:
    return [(Path(s).name, i, o, st) for s, i, o, st in BT.layout(store)]


# --------------------------------------------------------------- structure

def test_never_clears_v1(tmp_path, angles):
    store, a = _store(tmp_path, angles)
    res = dispatch(store, "apply_camera_plan", {"switches": [_switch(a, 2.0, 4.0, angles["B"])]})
    assert res["switches"] == 1 and res["pieces"] == 1
    assert _spans(store) == [("camA.mp4", 0.0, 2.0, 0.0), ("camB.mp4", 2.0, 4.0, 2.0),
                             ("camA.mp4", 4.0, 8.0, 4.0)]
    assert store.edl.video_extent() == pytest.approx(A_S)


def test_swapped_piece_keeps_transform_effects_audio_keyframes(tmp_path, angles):
    store, a = _store(tmp_path, angles)
    cid = BT.v1_pieces(store)[0].id
    dispatch(store, "set_clip_transform", {"clip_id": cid, "scale": 1.3, "x": 12})
    dispatch(store, "add_keyframe", {"clip_id": cid, "prop": "scale", "time": 3.0, "value": 1.6})
    dispatch(store, "add_effect", {"clip_id": cid, "type": "blur", "params": {"radius": 4}})
    dispatch(store, "add_fade", {"clip_id": cid, "in_s": 0.2, "out_s": 0.3})
    dispatch(store, "set_volume", {"target": cid, "db": -6.0})
    dispatch(store, "set_clip_muted", {"clip_id": cid, "muted": True})
    dispatch(store, "set_video_fade", {"clip_id": cid, "in_s": 0.5})
    # The same tree split by hand at 2 and 4: what the middle piece must carry.
    twin = BT.session(tmp_path, name="twin")
    twin.edl = store.edl.model_copy(deep=True)
    dispatch(twin, "split_at", {"track": "v1", "time": 2.0})
    dispatch(twin, "split_at", {"track": "v1", "time": 4.0})
    dispatch(store, "apply_camera_plan", {"switches": [_switch(a, 2.0, 4.0, angles["B"])]})
    mine, theirs = BT.v1_pieces(store)[1], BT.v1_pieces(twin)[1]
    assert Path(mine.src).name == "camB.mp4" and Path(theirs.src).name == "camA.mp4"
    assert isinstance(mine.transform.scale, Keyframe)
    assert mine.transform.model_dump() == theirs.transform.model_dump()
    assert [e.model_dump() for e in mine.effects] == [e.model_dump() for e in theirs.effects]
    assert mine.audio.model_dump() == theirs.audio.model_dump()
    assert mine.audio.mute is True and mine.audio.gain_db == -6.0
    assert (mine.video_fade_in, mine.video_fade_out) == (theirs.video_fade_in, theirs.video_fade_out)
    assert (mine.in_, mine.out, mine.start) == (theirs.in_, theirs.out, theirs.start)


# ------------------------------------------------------------- the offsets

def _codes(path: Path) -> list[int]:
    return bars.measure(path)["top"]


def _sample_frames(t0: float, t1: float) -> list[int]:
    return [round((t0 + (t1 - t0) * f) * FPS) for f in (0.1, 0.5, 0.9)]


@pytest.mark.parametrize("off_a,off_b", [(0.0, 0.5), (0.5, 0.0), (0.2, -0.3)])
def test_offsets_shift_in_out(tmp_path, angles, off_a, off_b):
    """A switch to B over A's [2, 4) plays B's file at `t + offsets[B] −
    offsets[A]`: the decoded frame at three sample times per span is that
    angle's frame for that instant (bar codes, `frame_map_golden_lib.measure`)."""
    store, a = _store(tmp_path, angles)
    delta = off_b - off_a
    dispatch(store, "apply_camera_plan", {"switches": [_switch(a, 2.0, 4.0, angles["B"])],
                                          "offsets": {a: off_a, str(angles["B"]): off_b}})
    b_piece = BT.v1_pieces(store)[1]
    dq = round(delta * FPS) / FPS          # the in-point lands on the frame grid, the length is kept
    assert (b_piece.in_, b_piece.out) == pytest.approx((2.0 + dq, 4.0 + dq))
    assert b_piece.start == pytest.approx(2.0)
    out = render_export(store.edl, store.dir).path
    codes = _codes(out)
    assert len(codes) == round(A_S * FPS)
    for t0, t1, sid, shift in ((0.0, 2.0, 1, 0), (2.0, 4.0, 2, dq), (4.0, 8.0, 1, 0)):
        for n in _sample_frames(t0, t1):
            want = bars.code_of(sid, n + round(shift * FPS))
            assert codes[n] == want, (t0, t1, n, hex(codes[n]), hex(want))


def test_refuses_until_leq_at(tmp_path, angles):
    store, a = _store(tmp_path, angles)
    before, ops = store.edl.hash(), len(store.ops.ops)
    for at, until in ((4.0, 4.0), (4.0, 2.0)):
        with pytest.raises(ValueError, match="until_src"):
            dispatch(store, "apply_camera_plan", {"switches": [_switch(a, at, until, angles["B"])]})
    assert store.edl.hash() == before and len(store.ops.ops) == ops


def test_clamps_at_angle_extent(tmp_path, angles):
    """The short angle is 4 s long. Asked over A's [2, 6) it covers [2, 4)
    and A keeps the remainder; with the short angle 1.5 s BEHIND A (its
    file starts where A's 1.5 s is), A's [0, 3) becomes A[0, 1.5) + short[0, 1.5)."""
    store, a = _store(tmp_path, angles)
    short = str(angles["short"])
    res = dispatch(store, "apply_camera_plan", {"switches": [_switch(a, 2.0, 6.0, short)]})
    assert res["pieces"] == 1 and res["clamped"] == 1
    assert _spans(store) == [("camA.mp4", 0.0, 2.0, 0.0), ("camShort.mp4", 2.0, 4.0, 2.0),
                             ("camA.mp4", 4.0, 8.0, 4.0)]
    store2, a2 = _store(tmp_path / "two", angles)
    dispatch(store2, "apply_camera_plan", {"switches": [_switch(a2, 0.0, 3.0, short)],
                                           "offsets": {a2: 1.5, short: 0.0}})
    assert _spans(store2) == [("camA.mp4", 0.0, 1.5, 0.0), ("camShort.mp4", 0.0, 1.5, 1.5),
                              ("camA.mp4", 3.0, 8.0, 3.0)]
    assert store2.edl.video_extent() == pytest.approx(A_S)


def test_switch_over_a_cut_away_span_is_skipped(tmp_path, angles):
    store, a = _store(tmp_path, angles)
    dispatch(store, "cut_range", {"track": "v1", "start": 2.0, "end": 4.0})
    res = dispatch(store, "apply_camera_plan", {"switches": [_switch(a, 2.5, 3.5, angles["B"]),
                                                             _switch(a, 5.0, 6.0, angles["B"])]})
    assert res["switches"] == 1 and res["skipped"] == 1
    assert _spans(store) == [("camA.mp4", 0.0, 2.0, 0.0), ("camA.mp4", 4.0, 5.0, 2.0),
                             ("camB.mp4", 5.0, 6.0, 3.0), ("camA.mp4", 6.0, 8.0, 4.0)]


# ------------------------------------------------------ one op and the card

def test_multiple_switches_are_one_op_and_on_the_card(tmp_path, angles):
    store, a = _store(tmp_path, angles)
    before = store.edl.model_copy(deep=True)
    h0, ops, depth = store.edl.hash(), len(store.ops.ops), store.undo_depth
    res = dispatch(store, "apply_camera_plan", {"switches": [
        _switch(a, 1.0, 2.0, angles["B"]), _switch(a, 3.0, 4.5, angles["B"]),
        _switch(a, 6.0, 7.0, angles["B"])], "offsets": {a: 0.0, str(angles["B"]): 0.25}})
    assert res["switches"] == 3 and res["pieces"] == 3
    assert len(store.ops.ops) == ops + 1 and store.undo_depth == depth + 1
    op = store.ops.ops[-1]
    assert op.tool == "apply_camera_plan" and "Camera plan: 3 switches" in op.summary
    assert "camA.mp4" in res["derived_from"]
    lines = C.summarize(before, store.edl, session_dir=Path(store.dir))
    text = [c.text for c in lines]
    assert C.diff_keys(before, store.edl) <= C.covered(lines), text
    assert any("Camera" in t and "camB.mp4" in t for t in text), text
    assert not any("Deleted" in t for t in text), text
    assert store.undo() and store.edl.hash() == h0


def test_the_op_text_reads_as_a_sentence_for_one_switch(tmp_path, angles):
    """UX-13: '1 switches (1 pieces …)'."""
    store, a = _store(tmp_path, angles)
    res = dispatch(store, "apply_camera_plan", {"switches": [_switch(a, 1.0, 2.0, angles["B"])]})
    text = store.ops.ops[-1].summary
    assert res["switches"] == 1
    assert text.startswith("Camera plan: 1 switch (1 piece") and "1 switches" not in text and "1 pieces" not in text
    assert "derived_from camA.mp4" in text                                      # the frontend's opLabels reads this token


def test_caps_and_bad_shapes(tmp_path, angles):
    store, a = _store(tmp_path, angles)
    sw = _switch(a, 1.0, 2.0, angles["B"])
    with pytest.raises(ValueError, match="600"):
        dispatch(store, "apply_camera_plan", {"switches": [sw] * 601})
    with pytest.raises(ValueError, match="angle_src"):
        dispatch(store, "apply_camera_plan", {"switches": [{"src": a, "at_src": 1.0, "until_src": 2.0}]})
    with pytest.raises(ValueError, match="16"):
        dispatch(store, "apply_camera_plan", {"switches": [sw],
                                              "offsets": {f"/nowhere/{i}.mp4": 0.0 for i in range(17)}})
    with pytest.raises(ValueError):
        dispatch(store, "apply_camera_plan", {"switches": ["nope"]})
    with pytest.raises(ValueError, match="picture"):
        rec = BT.make_recorder(tmp_path / "rec.wav", seconds=8)
        dispatch(store, "apply_camera_plan", {"switches": [_switch(a, 1.0, 2.0, rec)]})
