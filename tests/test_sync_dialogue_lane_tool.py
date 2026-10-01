"""`sync_dialogue_lane` — the derived dialogue lane (EB1-B, spec §4.6.1, §5.4 row 4).

The lane is REBUILT, never edited: the tool drops its own clips of the
dialogue source from `a1`, removes the music-lane copy the audio-only upload
handoff placed (once), lays ONE abutting `Clip` of the dialogue file per v1
piece — `start = piece.start`, `in_` mapped by the per-file offsets — with
5 ms fades at internal seams and 0 at the ends, and mutes the camera-mic audio
of every v1 angle piece (gain preserved). Idempotent; one commit; last stage-2
step of every brain plan.

MEASURED, not inferred: after each structural op (keep, cuts, split, reorder,
duplicate, move, angle swap) the lane is re-synced and the render is decoded —
`timing_fixtures`' flash/click measurement (`brain_tool_fixtures.av_offsets_ms`,
the same pairing with a 50 ms click refractory) pairs every flash of the
picture with the nearest click of the lane, and every pair must sit within HALF A FRAME.

OFFSET SIGN (recorded deviation, brief "Frozen contracts" row 3): the brief
writes the lane's `in_` as `piece.in_ + offsets[piece.src] − offsets[src]`
while freezing `apply_camera_plan` to `in_ += offsets[angle] − offsets[src]`
and spec §2.4 to "positive = that file lags the reference". Those two are
consistent only when the lane maps `in_ = piece.in_ + offsets[src] −
offsets[piece.src]` (an event at reference second r is at file second
r + offsets[file]); with the brief's literal sign the clicks land 2·offset
away from the flashes. The test below is the proof: with camera A +0.4 s and
camera B −0.2 s, the lane's clicks meet the flashes only with this sign.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain_tool_fixtures as BT  # noqa: E402
from timing_fixtures import flash_onsets  # noqa: E402

from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.agent.prompt import changes as C  # noqa: E402
from video_ai_editor.edl.snapshot import EDLStore  # noqa: E402
from video_ai_editor.render import render_export  # noqa: E402

D = importlib.import_module("video_ai_editor.agent.dispatch")

FPS = 30
SECONDS = 12.0
OFF_A, OFF_B = 0.4, -0.2
FADE = 0.005
HALF = BT.half_frame_ms(FPS)


@pytest.fixture(autouse=True)
def _posture():
    with BT.restriction_off():
        yield


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("dlg")
    return {"rec": BT.make_recorder(d / "recorder.wav", seconds=SECONDS),
            "A": BT.make_click_camera(d / "camA.mp4", offset_s=OFF_A, seconds=SECONDS),
            "B": BT.make_click_camera(d / "camB.mp4", offset_s=OFF_B, seconds=SECONDS),
            "broll": BT.make_silent_broll(d / "broll.mp4", seconds=4.0)}


def _offsets(media) -> dict[str, float]:
    return {str(media["A"]): OFF_A, str(media["B"]): OFF_B, str(media["rec"]): 0.0}


def _store(tmp_path: Path, media, name: str = "s") -> EDLStore:
    store = BT.session(tmp_path, name=name)
    dispatch(store, "add_clip", {"track": "v1", "src": str(media["A"]), "in": 0, "out": SECONDS, "start": 0})
    return store


def _sync(store: EDLStore, media, **over) -> dict:
    args = {"src": str(media["rec"]), "lane": "a1", "offsets": _offsets(media),
            "seam_fade_s": FADE, "mute_camera_mics": True, **over}
    return dispatch(store, "sync_dialogue_lane", args)


def _off(media, src: str) -> float:
    return _offsets(media).get(src, 0.0)


def _edl_in_sync(store: EDLStore, media) -> None:
    """EDL-only: for every v1 piece of an angle, the a1 clip playing at the
    piece's start plays the reference second the picture shows (`in_` mapped
    by the offsets), abuts the next one, and no a1 clip sits over a v1 gap."""
    a1 = BT.lane_clips(store, "a1")
    rec = str(media["rec"])
    assert all(c.src == rec and c.linked_to is None and c.audio.gain_db == 0 for c in a1)
    for p in BT.v1_pieces(store):
        if p.src not in _offsets(media):
            assert not any(c.start < p.start + p.effective_duration - 1e-6
                           and c.start + c.effective_duration > p.start + 1e-6 for c in a1), "gap expected"
            continue
        assert p.audio.mute is True
        ref0 = p.in_ - _off(media, p.src)                  # reference second at the piece's start
        start = p.start + max(0.0, -ref0)                  # clamped when the file has no such second
        c = next((c for c in a1 if abs(c.start - start) < 1e-6), None)
        assert c is not None, (p.start, ref0, [(x.start, x.in_) for x in a1])
        assert c.in_ == pytest.approx(max(0.0, ref0), abs=1e-6)
        assert c.start + c.effective_duration == pytest.approx(
            min(p.start + p.effective_duration, start + (SECONDS - c.in_)), abs=1e-6)
    for c in a1:
        assert any(abs(c.start - p.start) < 0.5 + 1e-6 for p in BT.v1_pieces(store))


def _render_in_sync(store: EDLStore, expect_flashes: int) -> list[float]:
    out = render_export(store.edl, store.dir).path
    flashes = flash_onsets(out)
    assert len(flashes) == expect_flashes, flashes
    offs = BT.av_offsets_ms(out)
    assert len(offs) == expect_flashes, (offs, flashes)
    assert max(abs(o) for o in offs) <= HALF, [round(o, 2) for o in offs]
    return offs


# ------------------------------------------------ the measured slice, op by op

def test_rebuilds_a1_after_each_structural_op(tmp_path, media):
    """keep → cuts → split → reorder → duplicate → move → angle swap; after
    each, `sync_dialogue_lane` alone puts the lane back in step: every flash
    of the render has its click within half a frame."""
    store = _store(tmp_path, media)
    a = str(BT.v1_pieces(store)[0].src)
    res = _sync(store, media)
    assert res["clips"] == 1 and res["muted"] == 1 and res["lane"] == "a1"
    _edl_in_sync(store, media)
    _render_in_sync(store, expect_flashes=12)              # reference seconds 0..11, seen at A 0.4 … 11.4

    # keep: A's [1.0, 10.6) survives (the complement is cut, tail first)
    dispatch(store, "cut_source_ranges", {"track": "v1", "ranges": [
        {"src": a, "start": 0.0, "end": 1.0}, {"src": a, "start": 10.6, "end": SECONDS}]})
    _sync(store, media)
    _edl_in_sync(store, media)
    _render_in_sync(store, expect_flashes=10)              # flashes at A 1.4 … 10.4

    # cuts: two pauses inside the kept span
    dispatch(store, "cut_source_ranges", {"track": "v1", "ranges": [
        {"src": a, "start": 4.5, "end": 5.3}, {"src": a, "start": 7.6, "end": 8.3}]})
    _sync(store, media)
    _edl_in_sync(store, media)
    _render_in_sync(store, expect_flashes=10)

    dispatch(store, "split_at", {"track": "v1", "time": 2.0})
    _sync(store, media)
    _edl_in_sync(store, media)
    assert len(BT.lane_clips(store, "a1")) == len(BT.v1_pieces(store)) == 4

    ids = [c.id for c in BT.v1_pieces(store)]
    dispatch(store, "reorder_clips", {"track": "v1", "order": [ids[2], ids[0], ids[3], ids[1]]})
    _sync(store, media)
    _edl_in_sync(store, media)
    _render_in_sync(store, expect_flashes=10)

    dispatch(store, "duplicate_clip", {"clip_id": ids[0]})
    _sync(store, media)
    _edl_in_sync(store, media)
    assert len(BT.lane_clips(store, "a1")) == 5
    _render_in_sync(store, expect_flashes=12)              # the copy (A 1.0-3.0) repeats two flashes

    last = BT.v1_pieces(store)[-1]
    dispatch(store, "move_clip", {"clip_id": last.id, "new_start": 0.0, "close_gap": True})
    _sync(store, media)
    _edl_in_sync(store, media)
    _render_in_sync(store, expect_flashes=12)

    # angle swap: the piece playing A's [5.3, 7.6) shows B for [5.5, 7.0)
    dispatch(store, "apply_camera_plan", {"switches": [
        {"src": a, "at_src": 5.5, "until_src": 7.0, "angle_src": str(media["B"])}],
        "offsets": _offsets(media)})
    assert any(p.src == str(media["B"]) for p in BT.v1_pieces(store))
    res = _sync(store, media)
    _edl_in_sync(store, media)
    assert res["muted"] == len(BT.v1_pieces(store))
    _render_in_sync(store, expect_flashes=12)              # B shows reference 6 where A did


def test_wrong_offset_sign_is_measurably_out_of_step(tmp_path, media):
    """The proof of the sign: the brief's literal formula puts the lane
    2·offset away from the picture (0.8 s on camera A)."""
    store = _store(tmp_path, media)
    _sync(store, media, offsets={str(media["A"]): -OFF_A, str(media["rec"]): 0.0})
    out = render_export(store.edl, store.dir).path
    offs = BT.av_offsets_ms(out, window_s=0.9)
    assert offs and min(abs(o) for o in offs) > 10 * HALF   # every click 0.8 s from its flash


# ---------------------------------------------------------------- semantics

def test_idempotent_twice_same_hash(tmp_path, media):
    store = _store(tmp_path, media)
    dispatch(store, "split_at", {"track": "v1", "time": 4.0})
    _sync(store, media)
    h1, ops1 = store.edl.hash(), len(store.ops.ops)
    res = _sync(store, media)
    assert store.edl.hash() == h1 and len(store.ops.ops) == ops1
    assert res["clips"] == 2 and res["removed_from_music"] == 0
    assert len(BT.lane_clips(store, "a1")) == 2


def test_the_recorder_leaving_the_music_lane_is_said_once_on_the_card(tmp_path, media):
    from video_ai_editor.main import _add_uploaded_music
    store = _store(tmp_path, media)
    _add_uploaded_music(store, media["rec"], SECONDS, None, -12.0)
    before = store.edl.model_copy(deep=True)
    res = _sync(store, media)
    assert res["removed_from_music"] == 1
    text = [c.text for c in C.summarize(before, store.edl, session_dir=Path(store.dir))]
    assert C.diff_keys(before, store.edl) <= C.covered(C.summarize(before, store.edl, session_dir=Path(store.dir)))
    said = [t for t in text if "recorder.wav" in t]
    assert len(said) == 1 and "it is now the dialogue" in said[0], text
    assert not [t for t in text if t.startswith(("Removed", "Added audio"))], text


def test_removes_music_lane_copy_once(tmp_path, media):
    from video_ai_editor.main import _add_uploaded_music
    store = _store(tmp_path, media)
    _add_uploaded_music(store, media["rec"], SECONDS, None, -12.0)
    assert any(c.src == str(media["rec"]) for c in BT.lane_clips(store, "music"))
    res = _sync(store, media)
    assert res["removed_from_music"] == 1
    assert not any(c.src == str(media["rec"]) for c in BT.lane_clips(store, "music"))
    assert "Music lane" in res["summary"]
    # two copies: exactly one is taken, the other is the person's own placement
    _add_uploaded_music(store, media["rec"], SECONDS, None, -12.0)
    _add_uploaded_music(store, media["rec"], SECONDS, None, -12.0)
    res = _sync(store, media)
    assert res["removed_from_music"] == 1
    assert sum(c.src == str(media["rec"]) for c in BT.lane_clips(store, "music")) == 1


def test_mutes_v1_angle_pieces_and_keeps_gain(tmp_path, media):
    store = _store(tmp_path, media)
    cid = BT.v1_pieces(store)[0].id
    dispatch(store, "set_volume", {"target": cid, "db": -7.0})
    dispatch(store, "add_clip", {"track": "v1", "src": str(media["broll"]), "in": 0, "out": 4.0,
                                 "start": SECONDS})
    res = _sync(store, media)
    angle, broll = BT.v1_pieces(store)
    assert res["muted"] == 1
    assert angle.audio.mute is True and angle.audio.gain_db == -7.0
    assert broll.audio.mute is False
    store2 = _store(tmp_path / "off", media)
    _sync(store2, media, mute_camera_mics=False)
    assert BT.v1_pieces(store2)[0].audio.mute is False
    assert len(BT.lane_clips(store2, "a1")) == 1


def test_seam_fades_internal_only(tmp_path, media):
    store = _store(tmp_path, media)
    dispatch(store, "split_at", {"track": "v1", "time": 4.0})
    dispatch(store, "split_at", {"track": "v1", "time": 8.0})
    _sync(store, media)
    fades = [(c.audio.fade_in, c.audio.fade_out) for c in BT.lane_clips(store, "a1")]
    assert fades == [(0.0, FADE), (FADE, FADE), (FADE, 0.0)]
    # a gap (B-roll on v1) is an end too: no fade into or out of silence
    store2 = _store(tmp_path / "gap", media)
    dispatch(store2, "split_at", {"track": "v1", "time": 4.0})
    mid = BT.v1_pieces(store2)[1]
    dispatch(store2, "cut_range", {"track": "v1", "start": 4.0, "end": 8.0})
    dispatch(store2, "add_clip", {"track": "v1", "src": str(media["broll"]), "in": 0, "out": 4.0,
                                  "start": 4.0})
    assert [Path(p.src).name for p in BT.v1_pieces(store2)] == ["camA.mp4", "broll.mp4", "camA.mp4"], mid
    _sync(store2, media)
    fades = [(c.audio.fade_in, c.audio.fade_out) for c in BT.lane_clips(store2, "a1")]
    assert fades == [(0.0, 0.0), (0.0, 0.0)]


def test_broll_on_v1_leaves_a_gap(tmp_path, media):
    store = _store(tmp_path, media)
    dispatch(store, "split_at", {"track": "v1", "time": 4.0})
    dispatch(store, "cut_range", {"track": "v1", "start": 4.0, "end": 8.0})
    dispatch(store, "add_clip", {"track": "v1", "src": str(media["broll"]), "in": 0, "out": 4.0, "start": 4.0})
    res = _sync(store, media)
    a1 = BT.lane_clips(store, "a1")
    assert [(round(c.start, 3), round(c.start + c.effective_duration, 3)) for c in a1] == [(0.4, 4.0), (8.0, 12.0)]
    assert [(round(c.in_, 3), round(c.out, 3)) for c in a1] == [(0.0, 3.6), (7.6, 11.6)]
    assert res["gaps"] == [[4.0, 8.0]] and res["clips"] == 2
    _edl_in_sync(store, media)


def test_refuses_locked_or_non_audio_lane(tmp_path, media):
    store = _store(tmp_path, media)
    before, ops = store.edl.hash(), len(store.ops.ops)
    with pytest.raises(ValueError, match="audio"):
        _sync(store, media, lane="music")
    dispatch(store, "set_track_locked", {"track": "a1", "locked": True})
    with pytest.raises(ValueError, match="locked"):
        _sync(store, media)
    dispatch(store, "set_track_locked", {"track": "a1", "locked": False})
    with pytest.raises(ValueError, match="lane"):
        _sync(store, media, lane="v9")
    assert len(store.ops.ops) == ops + 2 and BT.lane_clips(store, "a1") == []
    with pytest.raises(ValueError, match="audio"):
        _sync(store, media, src=str(tmp_path / "missing.wav"))
    # an absent `a2` is created by `_free_audio_lane`'s rule, labelled Dialogue
    res = _sync(store, media, lane="a2")
    lane = store.edl.get_track("a2")
    assert res["lane"] == "a2" and lane is not None and lane.type == "audio" and lane.label == "Dialogue"
    assert store.edl.hash() != before


def test_a_refusal_never_leaves_a_new_lane_behind(tmp_path, media, monkeypatch):
    """SC-15: the lane was created BEFORE `store.batch()`, so a refusal after that point left an
    uncommitted `a7` in the in-memory EDL, folded into the next commit with no op of its own."""
    store = _store(tmp_path, media)
    dispatch(store, "split_at", {"track": "v1", "time": 4.0})
    dispatch(store, "split_at", {"track": "v1", "time": 8.0})
    before, ops = store.edl.model_dump_json(), len(store.ops.ops)
    monkeypatch.setattr(D, "DIALOGUE_PIECES_MAX", 2)                       # 3 v1 pieces: over the cap
    with pytest.raises(ValueError, match="at most 2 v1 pieces"):
        _sync(store, media, lane="a7")
    assert store.edl.get_track("a7") is None and store.edl.model_dump_json() == before
    monkeypatch.undo()
    monkeypatch.setattr(D, "_lay_dialogue", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):                        # a failure INSIDE the batch rolls the lane back
        _sync(store, media, lane="a7")
    assert store.edl.get_track("a7") is None and store.edl.model_dump_json() == before
    assert len(store.ops.ops) == ops
    monkeypatch.undo()
    assert _sync(store, media, lane="a7")["lane"] == "a7" and store.edl.get_track("a7") is not None   # and it still works


def test_the_op_text_reads_as_a_sentence_for_one_piece(tmp_path, media):
    """UX-13: '1 pieces of recorder.wav … 1 camera clips muted'."""
    store = _store(tmp_path, media)
    _sync(store, media)
    text = store.ops.ops[-1].summary
    assert text.startswith("Dialogue lane synced: 1 piece of recorder.wav")
    assert "1 camera clip muted" in text and "1 pieces" not in text and "1 camera clips" not in text


def test_foreign_clips_on_the_lane_are_left_alone(tmp_path, media):
    store = _store(tmp_path, media)
    dispatch(store, "split_at", {"track": "v1", "time": 6.0})
    dispatch(store, "detach_audio", {"clip_id": BT.v1_pieces(store)[1].id, "track": "a1"})
    foreign = [c for c in BT.lane_clips(store, "a1")]
    assert len(foreign) == 1 and foreign[0].src == str(media["A"])
    res = _sync(store, media)
    a1 = BT.lane_clips(store, "a1")
    assert res["foreign"] == 1 and res["clips"] == 2
    assert [c.id for c in a1 if c.src == str(media["A"])] == [foreign[0].id]


def test_one_commit_and_the_card(tmp_path, media):
    store = _store(tmp_path, media)
    dispatch(store, "split_at", {"track": "v1", "time": 4.0})
    before = store.edl.model_copy(deep=True)
    h0, ops, depth = store.edl.hash(), len(store.ops.ops), store.undo_depth
    res = _sync(store, media)
    assert len(store.ops.ops) == ops + 1 and store.undo_depth == depth + 1
    op = store.ops.ops[-1]
    assert op.tool == "sync_dialogue_lane" and "Dialogue lane synced" in op.summary
    lines = C.summarize(before, store.edl, session_dir=Path(store.dir))
    text = [c.text for c in lines]
    assert C.diff_keys(before, store.edl) <= C.covered(lines), text
    assert any("Dialogue" in t and "recorder.wav" in t and "2 pieces" in t for t in text), text
    assert any("muted" in t.lower() and "2" in t for t in text), text
    # EB1 integration: the lane is ONE line — its pieces are not listed again one by one (the podcast demo's
    # card carried 28 "Added audio 'recorder.wav' at …" lines under the line that had already said it)
    assert not [t for t in text if t.startswith("Added audio")], text
    assert len([t for t in text if "recorder.wav" in t]) == 1, text
    assert res["clips"] == 2
    assert store.undo() and store.edl.hash() == h0
    with store.batch():
        _sync(store, media)
        assert len(store.ops.ops) == ops


# ------------------------------------------- lane EB1-A's P2 fixture (when READY)

_A_READY = Path.home() / "Library/Caches/Video AI Editor/qa-fix/eb1/EB1-A-fixtures/READY.md"


def _p2_fixture():
    """Lane A's two-camera podcast (`tests/brain_fixtures.py`), or a skip:
    before A posts READY.md its builder is still moving (and costs ≈ 40 s of
    TTS), so this test activates itself the moment the fixture is declared
    done rather than building it early."""
    if not _A_READY.exists():
        pytest.skip("EB1-A's P2 fixture is not READY yet (the synthetic click sources above cover the same assertions)")
    try:
        import brain_fixtures as BF
        fx = BF.build_brain_fixtures()
    except Exception as e:  # noqa: BLE001 — a fixture that cannot build is a skip, not a lane failure
        pytest.skip(f"EB1-A fixture unavailable: {type(e).__name__}: {e}")
    if fx.p2 is None:
        pytest.skip(f"P2 skipped by its builder: {fx.p2_skip_reason}")
    return fx.p2


def test_rebuilds_a1_on_lane_a_p2_fixture(tmp_path):
    """The same rebuild-after-structural-ops proof on A's recorder + two
    cameras (+0.35 / −0.20 s at file level, 20 fps): after a keep, cuts, a
    switch to camera B and a re-sync, every flash has its click within half
    a frame (`brain_fixture_truth.av_offsets_ms_strict`, the speech-safe
    click threshold)."""
    p2 = _p2_fixture()
    from brain_fixture_truth import av_offsets_ms_strict
    fps = int(p2.truth.fps)
    store = BT.session(tmp_path, fps=fps)
    dispatch(store, "add_clip", {"track": "v1", "src": p2.cam_a, "in": 0, "out": 60.0, "start": 0})
    a = str(BT.v1_pieces(store)[0].src)
    names = {"cam_a": a, "cam_b": str(Path(p2.cam_b).resolve()), "recorder": str(Path(p2.recorder_wav).resolve())}
    offsets = {names.get(k, k): float(v) for k, v in dict(p2.truth.offsets).items()}
    offsets.setdefault(names["recorder"], 0.0)
    args = {"src": p2.recorder_wav, "lane": "a1", "offsets": offsets, "seam_fade_s": FADE}
    dispatch(store, "cut_source_ranges", {"track": "v1", "ranges": [
        {"src": a, "start": 0.0, "end": 2.0}, {"src": a, "start": 21.3, "end": 22.9}, {"src": a, "start": 40.0, "end": 60.0}]})
    dispatch(store, "apply_camera_plan", {"switches": [
        {"src": a, "at_src": 8.0, "until_src": 14.0, "angle_src": p2.cam_b}], "offsets": offsets})
    dispatch(store, "sync_dialogue_lane", args)
    assert all(p.audio.mute for p in BT.v1_pieces(store))
    out = render_export(store.edl, store.dir).path
    offs = av_offsets_ms_strict(out)
    assert len(offs) >= 5, offs
    assert max(abs(o) for o in offs) <= BT.half_frame_ms(fps), [round(o, 2) for o in offs]
