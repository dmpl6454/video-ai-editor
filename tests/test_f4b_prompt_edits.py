"""Wave E, lane F4b: the ops, facts and checks behind edits BY NAME.

  * `remove_effects` takes a filter (by effect TYPE) off clips in ONE commit —
    proven on a decoded render: a blue clip under the mono LUT renders grey,
    and blue again after the op; one undo brings the grey back.
  * `flip_clip` toggles or sets Transform.flip_h / flip_v in one commit, and
    the export mirrors the picture (decoded: a left-white / right-black
    source renders right-white).
  * `build_facts` carries what the removal / length / level / flip recipes
    need (clips' in/out/speed/curve/gain/effects/looks, caption ids, text
    overlays, transitions) — from a real store.
  * Every new verifier check both passes and FAILS on the EDL it measures.
  * The validator pins a `flip_clip` toggle to the state it produces.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture  # noqa: E402,F401

from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.agent.prompt import verify as V  # noqa: E402
from video_ai_editor.agent.prompt.facts import build_facts  # noqa: E402
from video_ai_editor.agent.prompt.recipes import pc  # noqa: E402
from video_ai_editor.agent.prompt.validate import validate_plan  # noqa: E402
from video_ai_editor.edl.schema import Canvas, Clip, EDL, Track, Transform  # noqa: E402
from video_ai_editor.edl.snapshot import EDLStore  # noqa: E402
from video_ai_editor.render import render_preview  # noqa: E402

HAS_FLIP = "flip_h" in Transform.model_fields
W, H = 320, 180


def _video(path: Path, vf: str) -> Path:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:d=2:r=30",
                    "-f", "lavfi", "-i", "sine=f=440:duration=2", "-vf", vf, "-c:v", "libx264",
                    "-preset", "ultrafast", "-pix_fmt", "yuv444p", "-c:a", "aac", "-shortest", str(path)],
                   check=True, capture_output=True)
    return path


def _store(tmp_path: Path, src: Path) -> EDLStore:
    edl = EDL(canvas=Canvas(w=W, h=H, fps=30), tracks=[
        Track(id="v1", type="video", clips=[Clip(src=str(src), in_=0, out=2, start=0, id="c1")])])
    edl.recompute_duration()
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(tmp_path)


def _frame(store: EDLStore, tmp_path: Path, t: float = 1.0) -> np.ndarray:
    out = render_preview(store.edl, tmp_path, height=H).path
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t}", "-i", str(out), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], check=True, capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, W, 3).astype(float)


# --------------------------------------------------------------------------- remove_effects

@pytest.mark.usefixtures("desktop_posture")
def test_remove_effects_takes_the_filter_off_the_export_in_one_step(tmp_path):
    store = _store(tmp_path, _video(tmp_path / "blue.mp4", "drawbox=c=blue:t=fill"))
    blue = _frame(store, tmp_path).mean(axis=(0, 1))
    assert blue[2] > 150 and blue[0] < 60, blue
    dispatch(store, "apply_lut", {"clip_id": "c1", "src": "mono.cube"})
    dispatch(store, "color_grade", {"clip_id": "c1", "brightness": 0.05})
    grey = _frame(store, tmp_path).mean(axis=(0, 1))
    assert abs(grey[2] - grey[0]) < 12, grey                      # the mono LUT: no colour
    n_ops = store.undo_depth
    r = dispatch(store, "remove_effects", {"clip_ids": ["c1"], "types": ["lut"]})
    assert r["removed"] == 1 and store.undo_depth == n_ops + 1
    assert [e.type for e in store.edl.get_clip("c1")[1].effects] == ["color"]   # only the filter went
    back = _frame(store, tmp_path).mean(axis=(0, 1))
    assert back[2] > 150 and back[2] - back[0] > 100, back       # blue again
    store.undo()
    assert [e.type for e in store.edl.get_clip("c1")[1].effects] == ["lut", "color"]


@pytest.mark.usefixtures("desktop_posture")
def test_remove_effects_refuses_what_it_cannot_do(tmp_path):
    store = _store(tmp_path, _video(tmp_path / "blue.mp4", "drawbox=c=blue:t=fill"))
    with pytest.raises(ValueError, match="clip_ids"):
        dispatch(store, "remove_effects", {"types": ["lut"]})
    with pytest.raises(ValueError, match="no lut effect"):
        dispatch(store, "remove_effects", {"clip_ids": ["c1"]})          # nothing to remove: no silent commit
    with pytest.raises(ValueError, match="unknown effect"):
        dispatch(store, "remove_effects", {"clip_ids": ["c1"], "types": ["sparkle"]})
    with pytest.raises(ValueError, match="not found"):
        dispatch(store, "remove_effects", {"clip_ids": ["nope"]})


# --------------------------------------------------------------------------- flip_clip

@pytest.mark.skipif(not HAS_FLIP, reason="needs Transform.flip_h (lane F4a)")
@pytest.mark.usefixtures("desktop_posture")
def test_flip_clip_toggles_one_axis_per_commit_and_the_export_is_mirrored(tmp_path):
    # left half white, right half black
    store = _store(tmp_path, _video(tmp_path / "half.mp4", f"drawbox=x=0:y=0:w={W // 2}:h={H}:c=white:t=fill"))
    f0 = _frame(store, tmp_path)
    assert f0[:, : W // 4].mean() > 200 and f0[:, 3 * W // 4:].mean() < 40
    n = store.undo_depth
    assert dispatch(store, "flip_clip", {"clip_id": "c1", "axis": "horizontal"})["flip_h"] is True
    assert store.undo_depth == n + 1
    f1 = _frame(store, tmp_path)
    assert f1[:, : W // 4].mean() < 40 and f1[:, 3 * W // 4:].mean() > 200     # mirrored
    assert dispatch(store, "flip_clip", {"clip_id": "c1", "axis": "vertical", "value": True})["flip_v"] is True
    tx = store.edl.get_clip("c1")[1].transform
    assert (tx.flip_h, tx.flip_v) == (True, True)
    assert dispatch(store, "flip_clip", {"clip_id": "c1", "axis": "horizontal"})["flip_h"] is False   # toggle
    store.undo()
    assert store.edl.get_clip("c1")[1].transform.flip_h is True


@pytest.mark.skipif(not HAS_FLIP, reason="needs Transform.flip_h (lane F4a)")
@pytest.mark.usefixtures("desktop_posture")
def test_flip_clip_refuses_text_and_audio(tmp_path):
    store = F.make_store(tmp_path)
    dispatch(store, "add_text", {"text": "HI", "start": 0, "end": 1})
    tid = next(c.id for t in store.edl.tracks for c in t.clips if getattr(c, "text", None) == "HI")
    with pytest.raises(ValueError, match="media clip or a sticker"):
        dispatch(store, "flip_clip", {"clip_id": tid, "axis": "horizontal"})
    bed = F.music_bed(Path(store.dir).parent)
    dispatch(store, "add_music", {"src": str(bed), "start": 0, "duck": False})
    mid = store.edl.get_track("music").clips[0].id
    with pytest.raises(ValueError, match="audio lane"):
        dispatch(store, "flip_clip", {"clip_id": mid, "axis": "horizontal"})


# --------------------------------------------------------------------------- facts

@pytest.mark.usefixtures("desktop_posture")
def test_facts_carry_what_an_edit_by_name_needs(tmp_path):
    store = F.make_store(tmp_path)
    dispatch(store, "split_at", {"track": "v1", "time": 4.0})
    dispatch(store, "split_at", {"track": "v1", "time": 8.0})
    a, b, c = (x.id for x in store.edl.get_track("v1").clips)
    dispatch(store, "add_caption_track", {})
    dispatch(store, "add_text", {"text": "Day One", "start": 0, "end": 3})
    dispatch(store, "apply_lut", {"clip_id": a, "src": "mono.cube"})
    dispatch(store, "set_volume", {"target": b, "db": -3.0})
    dispatch(store, "set_speed", {"clip_id": c, "preset": "hero"})
    dispatch(store, "add_transition", {"at": 4.0, "type": "dissolve"})
    f = build_facts(store, {"selection": b}, feature_report={})
    fa, fb, fc = (f.clip(x) for x in (a, b, c))
    assert (fa.src_in, fa.src_out, fa.effects, fa.looks) == (0.0, 4.0, ["lut"], ["mono.cube"])
    assert fb.gain_db == -3.0 and fb.speed == 1.0 and fb.curve is None
    assert fc.curve and fc.duration == pytest.approx(store.edl.get_clip(c)[1].effective_duration, abs=1e-3)
    assert fa.src_duration == pytest.approx(F.CLIP_DUR, abs=0.1)
    assert len(f.caption_clip_ids) == 3 and all(store.edl.get_clip(i) for i in f.caption_clip_ids)
    assert [(t.text, t.start, t.end) for t in f.texts] == [("Day One", 0.0, 3.0)]
    assert [(t.at, t.type) for t in f.transitions] == [(4.0, "dissolve")]


# --------------------------------------------------------------------------- checks

def _ctx(edl: EDL):
    return SimpleNamespace(edl=edl)


def test_every_new_check_passes_and_fails_on_the_edl_it_measures():
    e = EDL(canvas=Canvas(w=W, h=H, fps=30), tracks=[Track(id="v1", type="video", clips=[
        Clip(src="a.mp4", in_=0, out=2, start=0, id="c_a"), Clip(src="a.mp4", in_=2, out=4, start=2, id="c_b")])])
    e.get_track("v1").clips[0].effects = [__import__("video_ai_editor.edl.schema", fromlist=["Effect"]).Effect(
        type="lut", params={"src": "mono.cube"})]
    from video_ai_editor.edl.schema import Transition
    e.get_track("v1").transitions = [Transition(at=2.0, type="fade", duration=0.5)]
    ctx = _ctx(e)
    run = lambda name, **a: V.CHECKS[name](ctx, pc(name, name, **a)).passed  # noqa: E731
    assert run("transitions_absent", at=2.0) is False and run("transitions_absent") is False
    e.get_track("v1").transitions = []
    assert run("transitions_absent", at=2.0) is True and run("transitions_absent") is True
    assert run("clips_absent", clip_ids=["c_a"]) is False and run("clips_absent", clip_ids=["gone"]) is True
    assert run("effect_absent", clip_id=["c_a"], types=["lut"]) is False
    assert run("effect_absent", clip_id=["c_b"], types=["lut"]) is True
    assert run("clip_duration", clip_id="c_b", seconds=2.0, tol=0.02) is True
    assert run("clip_duration", clip_id="c_b", seconds=1.5, tol=0.02) is False
    if HAS_FLIP:
        assert run("clip_flipped", clip_id="c_a", flip_h=True) is False
        e.get_track("v1").clips[0].transform.flip_h = True
        assert run("clip_flipped", clip_id="c_a", flip_h=True) is True
        assert run("clip_flipped", clip_id="c_a", flip_h=True, flip_v=True) is False


def test_the_validator_pins_a_flip_toggle_to_the_state_it_produces():
    from video_ai_editor.agent.prompt.facts import ClipFact, TimelineFacts
    f = TimelineFacts.minimal(duration=4.0, v1_clip_ids=["c_a"], clip_ids=["c_a"], track_ids=["v1"],
                              clips=[ClipFact(id="c_a", track="v1", start=0, duration=4, src_in=0, src_out=4,
                                              flip_h=True)])
    p = F.plan_of(F.step("flip_clip", clip_id="c_a", axis="horizontal"))
    out = validate_plan(p, f)
    assert out.steps[0].args["value"] is False                     # on → the toggle turns it OFF
    p = F.plan_of(F.step("remove_effects", clip_ids=["c_nope"], types=["lut"]))
    with pytest.raises(ValueError, match="not a clip on this timeline"):
        validate_plan(p, f)
