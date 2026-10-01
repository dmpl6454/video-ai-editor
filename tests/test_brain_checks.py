"""The two blocking checks of wave EB1 (`brain/checks.py`) and the frozen
sets around them (EB1 lane C).

* `BLOCKING_CHECKS` gains EXACTLY `no_cut_mid_word` and `dialogue_in_sync`;
* `PLAN_DENY` is byte-for-byte the 0.8.0 set (no tool leaves the fence);
* `no_cut_mid_word` measures the a1 seams when a1 holds the dialogue and
  the v1 seams otherwise — in the SOURCE clock (a word straddling a cut is
  clipped by timemap, so only the source instant of each cut edge can say
  "mid-word"); a source-continuous seam (a camera switch, a bare split) is
  exempt;
* `dialogue_in_sync` (EDL only) catches an a1 clip shifted by more than half
  a frame, an unmuted angle piece, an a1 clip over a v1 gap and a v1
  transition — using the plan's own `sync_dialogue_lane` offsets.
"""
from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture  # noqa: E402,F401

from video_ai_editor.agent.prompt import schema, verify  # noqa: E402
from video_ai_editor.agent.prompt.schema import Postcondition  # noqa: E402
from video_ai_editor.brain import checks as BC  # noqa: E402
from video_ai_editor.edl.schema import AudioProps, Clip, Track, Transition  # noqa: E402

D = importlib.import_module("video_ai_editor.agent.dispatch")
pytestmark = pytest.mark.usefixtures("desktop_posture")

FROZEN_BLOCKING_0_8_0 = frozenset({
    "speed_equals", "freeze_held", "clip_reversed", "clip_zoomed", "clip_flipped", "clips_absent",
    "effect_absent", "effect_present", "clip_duration", "transitions_count_geq", "transitions_absent",
    "canvas_bg_set", "blend_is", "voice_effect_is", "animation_is", "volume_db", "track_muted",
    "clips_muted", "video_fade_set", "audio_fade_set", "music_fade_set", "canvas_aspect", "caption_look",
    "captions_style", "export_preset_applied", "loudness_target_set", "text_present", "text_style_is",
})
FROZEN_PLAN_DENY = frozenset({
    "undo", "redo", "repair_media_paths", "repair_chunks", "save_show_template",
    "record_voiceover", "import_srt", "export_srt", "export_vtt", "export_ass",
    "multicam", "find_broll", "object_erase", "motion_track", "remove_background",
    "set_property", "add_clip", "add_sticker", "add_effect", "pyannote_status",
    "list_shows",
    "vocal_isolate", "instrumental_isolate", "diarize", "assign_caption_speakers", "search_media",
})


# ---------------------------------------------------------------- frozen sets

def test_blocking_set_gains_exactly_two():
    assert schema.BLOCKING_CHECKS == FROZEN_BLOCKING_0_8_0 | {"no_cut_mid_word", "dialogue_in_sync"}
    assert schema.CHECK_SPECS["no_cut_mid_word"].args == {"tol": 0.02}
    assert "tol" in schema.CHECK_SPECS["dialogue_in_sync"].args
    assert not schema.CHECK_SPECS["no_cut_mid_word"].needs_render
    assert not schema.CHECK_SPECS["dialogue_in_sync"].needs_render
    BC.install()
    assert verify.CHECKS["no_cut_mid_word"] is BC.c_no_cut_mid_word
    assert verify.CHECKS["dialogue_in_sync"] is BC.c_dialogue_in_sync


def test_plan_deny_frozen():
    assert schema.PLAN_DENY == FROZEN_PLAN_DENY
    assert len(schema.PLAN_DENY) == 26
    for tool in ("cut_source_ranges", "apply_camera_plan", "sync_dialogue_lane"):
        assert schema.TOOL_STAGE[tool] == schema.STAGE_CUTS and tool not in schema.PLAN_DENY


# ---------------------------------------------------------------- helpers

def _ctx(store, plan_steps=()):
    plan = F.plan_of(*plan_steps) if plan_steps else F.plan_of(F.step("get_timeline"))
    return verify.VerifyCtx(store=store, plan=plan, exec_result=SimpleNamespace(edl_before=store.edl, steps=[]),
                            facts_before=F.facts_for(store))


def _pc(check: str, **args) -> Postcondition:
    return Postcondition(check=check, args=args, human=check)


def _v1(store) -> list[Clip]:
    return [c for c in store.edl.get_track("v1").clips if isinstance(c, Clip)]


def _lay_a1(store, src: str, pieces: list[Clip], *, fade: float = 0.005, offset: float = 0.0) -> Track:
    """The lane `sync_dialogue_lane` would build: one abutting a1 clip per v1
    piece (a test builds the tree by hand — the tool is lane B's)."""
    a1 = store.edl.get_track("a1")
    a1.clips = []
    n = len(pieces)
    for i, p in enumerate(pieces):
        a1.clips.append(Clip(src=src, **{"in": p.in_ + offset}, out=p.out + offset, start=p.start,
                             audio=AudioProps(fade_in=0.0 if i == 0 else fade, fade_out=0.0 if i == n - 1 else fade)))
    return a1


# ---------------------------------------------------------------- no_cut_mid_word

def test_no_cut_mid_word_uses_a1_when_present(tmp_path: Path):
    store = F.make_store(tmp_path)
    src = _v1(store)[0].src
    # a cut through the middle of "hello" (1.0-1.5): remove source 1.2-1.3
    D.dispatch(store, "cut_range", {"track": "v1", "start": 1.2, "end": 1.3})
    pieces = _v1(store)
    assert len(pieces) == 2 and pieces[1].in_ == pytest.approx(1.3, abs=0.02)
    ctx = _ctx(store)
    res = BC.c_no_cut_mid_word(ctx, _pc("no_cut_mid_word", tol=0.02))
    assert res.passed is False and "hello" in str(res.detail)
    # a1 holds the dialogue and is CONTINUOUS across that v1 seam (a camera
    # switch, in effect): the v1 seam is no longer an audio cut → passes
    a1 = _lay_a1(store, src, pieces)
    a1.clips[1].in_ = a1.clips[0].out                     # continuous source across the seam
    res = BC.c_no_cut_mid_word(ctx, _pc("no_cut_mid_word", tol=0.02))
    assert res.passed is True, res.detail
    assert res.measured["lane"] == "a1"
    # …and an a1 seam that IS a source discontinuity mid-word fails again
    a1.clips[1].in_ = 1.3
    res = BC.c_no_cut_mid_word(ctx, _pc("no_cut_mid_word", tol=0.02))
    assert res.passed is False and res.measured["lane"] == "a1"


def test_no_cut_mid_word_passes_on_a_clean_cut_and_tolerates_the_edge(tmp_path: Path):
    store = F.make_store(tmp_path)
    # remove the filler "um" (0.6-0.9) with a pad that stays outside "so" and "hello"
    D.dispatch(store, "cut_range", {"track": "v1", "start": 0.55, "end": 0.95})
    ctx = _ctx(store)
    assert BC.c_no_cut_mid_word(ctx, _pc("no_cut_mid_word", tol=0.02)).passed is True
    # a cut edge 10 ms inside a word's start is within tol=0.02: tolerated; 40 ms in is not
    store2 = F.make_store(tmp_path / "two", name="s2")
    D.dispatch(store2, "cut_range", {"track": "v1", "start": 0.55, "end": 1.0 + 1 / 60})  # ~17 ms into "hello"
    assert BC.c_no_cut_mid_word(_ctx(store2), _pc("no_cut_mid_word", tol=0.02)).passed is True
    store3 = F.make_store(tmp_path / "three", name="s3")
    D.dispatch(store3, "cut_range", {"track": "v1", "start": 0.55, "end": 1.05})
    assert BC.c_no_cut_mid_word(_ctx(store3), _pc("no_cut_mid_word", tol=0.02)).passed is False


def test_no_cut_mid_word_without_a_transcript_cannot_measure(tmp_path: Path):
    store = F.make_store(tmp_path, with_transcript=False)
    D.dispatch(store, "cut_range", {"track": "v1", "start": 1.2, "end": 1.3})
    res = BC.c_no_cut_mid_word(_ctx(store), _pc("no_cut_mid_word", tol=0.02))
    assert res.passed is None


# ---------------------------------------------------------------- dialogue_in_sync

def test_dialogue_in_sync_detects_a_shifted_a1_clip_and_an_unmuted_angle_piece(tmp_path: Path):
    store = F.make_store(tmp_path)
    src = _v1(store)[0].src
    D.dispatch(store, "split_at", {"track": "v1", "time": 4.0})
    D.dispatch(store, "cut_range", {"track": "v1", "start": 6.0, "end": 7.0})
    pieces = _v1(store)
    assert len(pieces) == 3
    for p in pieces:
        p.audio.mute = True
    _lay_a1(store, src, pieces)
    step = F.step("sync_dialogue_lane", src=src, lane="a1", offsets={src: 0.0}, seam_fade_s=0.005,
                  mute_camera_mics=True)
    ctx = _ctx(store, [step])
    pc = _pc("dialogue_in_sync")
    ok = BC.c_dialogue_in_sync(ctx, pc)
    assert ok.passed is True, ok.detail
    assert ok.measured["pieces"] == 3 and ok.measured["tol"] == pytest.approx(1 / 60, abs=1e-6)
    # an a1 clip shifted by one frame (> half a frame): out of sync
    a1 = store.edl.get_track("a1")
    a1.clips[1].in_ = a1.clips[1].in_ + 1 / 30
    bad = BC.c_dialogue_in_sync(ctx, pc)
    assert bad.passed is False and "in_" in str(bad.detail)
    a1.clips[1].in_ = a1.clips[1].in_ - 1 / 30
    assert BC.c_dialogue_in_sync(ctx, pc).passed is True
    # an unmuted camera-mic piece on v1
    pieces[2].audio.mute = False
    bad = BC.c_dialogue_in_sync(ctx, pc)
    assert bad.passed is False and "muted" in str(bad.detail)
    pieces[2].audio.mute = True
    # an a1 clip over a v1 gap (a fourth a1 clip past the picture)
    a1.clips.append(Clip(src=src, **{"in": 0.0}, out=1.0, start=pieces[-1].start + pieces[-1].effective_duration + 2.0))
    bad = BC.c_dialogue_in_sync(ctx, pc)
    assert bad.passed is False and "gap" in str(bad.detail)
    a1.clips.pop()
    # a v1 transition while a1 exists
    store.edl.get_track("v1").transitions.append(Transition(at=pieces[1].start, type="fade", duration=0.5))
    bad = BC.c_dialogue_in_sync(ctx, pc)
    assert bad.passed is False and "transition" in str(bad.detail)
    store.edl.get_track("v1").transitions.clear()
    # offsets are honoured — the tool's rule, a1.in_ = piece.in_ + offsets[dialogue] − offsets[piece.src]
    # ("positive = lags the reference"): the recorder D lags angle A by 0.5 s, so under a piece
    # showing A-second p.in_ the recorder plays p.in_ + 0.5
    dsrc = str(Path(src).with_name("recorder.wav"))
    shutil.copyfile(src, dsrc)
    _lay_a1(store, dsrc, pieces, offset=0.5)
    step2 = F.step("sync_dialogue_lane", src=dsrc, lane="a1", offsets={src: 0.0, dsrc: 0.5}, seam_fade_s=0.005)
    assert BC.c_dialogue_in_sync(_ctx(store, [step2]), pc).passed is True
    step3 = F.step("sync_dialogue_lane", src=dsrc, lane="a1", offsets={src: 0.0, dsrc: 0.0}, seam_fade_s=0.005)
    assert BC.c_dialogue_in_sync(_ctx(store, [step3]), pc).passed is False


def test_dialogue_in_sync_without_a_lane_is_unmeasured_and_installed_in_verify(tmp_path: Path):
    store = F.make_store(tmp_path)
    res = BC.c_dialogue_in_sync(_ctx(store), _pc("dialogue_in_sync"))
    assert res.passed is None
    BC.install()
    ran = verify.run_check(_ctx(store), _pc("dialogue_in_sync"))
    assert ran.check == "dialogue_in_sync" and ran.as_dict()["blocking"] is True


# ---------------------------------------------------------------- EB1 integration

def _graph_session(tmp_path: Path, monkeypatch, *, move_hello: tuple[float, float] | None = None):
    """A store under a WORKDIR with the hand graph current (tests/brain_contract_fixtures)."""
    import brain_contract_fixtures as BF
    from video_ai_editor import config
    from video_ai_editor.brain import store as BS
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    store = F.make_store(tmp_path, name="s_graph")
    src = _v1(store)[0].src
    BF.write_brain_files(Path(store.dir), src, workdir=tmp_path)
    if move_hello is not None:
        layer = BF.speech_layer()
        hello = next(w for w in layer["words"] if w["text"] == "hello")
        hello["t0"], hello["t1"] = move_hello
        BS.write_layer(BF.SRC_KEY, "speech", "hand-v1", layer, workdir=tmp_path)
    return store, src


def test_no_cut_mid_word_measures_by_the_content_graph_when_one_is_current(tmp_path: Path, monkeypatch):
    """The graph's words are repaired onto the sound; whisper's are not (on
    fixture TH it put "Um," 0.2 s past its own voiced island and called a
    clean cut mid-word). With a current graph the graph is the stick."""
    store, _src = _graph_session(tmp_path, monkeypatch)
    D.dispatch(store, "cut_range", {"track": "v1", "start": 1.2, "end": 1.3})          # through "hello" (1.0-1.5)
    res = BC.c_no_cut_mid_word(_ctx(store), _pc("no_cut_mid_word", tol=0.02))
    assert res.passed is False and res.measured["words"] == "content graph" and "hello" in str(res.detail)
    # the SAME cut where the graph says the word really sits at 1.3-1.8: both edges are outside it
    store2, _ = _graph_session(tmp_path / "two", monkeypatch, move_hello=(1.3, 1.8))
    D.dispatch(store2, "cut_range", {"track": "v1", "start": 1.2, "end": 1.3})
    res2 = BC.c_no_cut_mid_word(_ctx(store2), _pc("no_cut_mid_word", tol=0.02))
    assert res2.passed is True and res2.measured["words"] == "content graph", res2.detail
    # …which the upload transcript alone calls mid-word (no graph: the old stick)
    plain = F.make_store(tmp_path / "three", name="s_plain")
    D.dispatch(plain, "cut_range", {"track": "v1", "start": 1.2, "end": 1.3})
    res3 = BC.c_no_cut_mid_word(_ctx(plain), _pc("no_cut_mid_word", tol=0.02))
    assert res3.passed is False and "words" not in res3.measured


def test_the_graph_measures_every_angles_edges_on_the_reference_clock(tmp_path: Path, monkeypatch):
    store, src = _graph_session(tmp_path, monkeypatch)
    words = [{"text": "hello", "t0": 1.0, "t1": 1.5}]
    D.dispatch(store, "cut_range", {"track": "v1", "start": 3.0, "end": 3.5})
    seams = BC.cut_seams(store.edl, "v1")
    assert len(seams) == 1
    # the file runs 2.2 s ahead of the reference: its second 3.0 / 3.5 is reference 0.8 / 1.3 → mid-"hello"
    bad = BC._graph_offending(store.edl, "v1", seams, words, {src: 2.2}, 0.02)
    assert len(bad) == 1 and "hello" in bad[0] and "3.500" in bad[0]
    assert BC._graph_offending(store.edl, "v1", seams, words, {src: 0.0}, 0.02) == []
    assert BC._graph_offending(store.edl, "v1", seams, words, {"/another/file.mp4": 2.2}, 0.02) == []


def test_dialogue_in_sync_accepts_the_tools_clamp_where_the_recorder_had_not_started(tmp_path: Path, monkeypatch):
    """The camera rolled 0.35 s before the recorder: under the first piece
    the lane starts on the next frame boundary at which the recorder has
    sound (dispatch._dialogue_piece). Measured in the P2 demo: the check
    called that layout out of sync and rolled the edit back."""
    store = F.make_store(tmp_path)
    src = _v1(store)[0].src
    rec = str(Path(src).with_name("recorder.wav"))
    shutil.copyfile(src, rec)
    D.dispatch(store, "cut_range", {"track": "v1", "start": 6.0, "end": 7.0})
    args = {"src": rec, "lane": "a1", "offsets": {src: 0.35, rec: 0.0}, "seam_fade_s": 0.005, "mute_camera_mics": True}
    D.dispatch(store, "sync_dialogue_lane", args)
    a1 = [c for c in store.edl.get_track("a1").clips if isinstance(c, Clip)]
    fps = store.edl.canvas.fps
    first = a1[0]
    assert first.start > 0.0 and abs(first.start * fps - round(first.start * fps)) < 1e-6
    assert first.in_ == pytest.approx(first.start - 0.35, abs=1e-6)
    ctx = _ctx(store, [F.step("sync_dialogue_lane", **args)])
    ok = BC.c_dialogue_in_sync(ctx, _pc("dialogue_in_sync"))
    assert ok.passed is True, ok.detail
    # the clamp is not a licence: the clamped clip one frame late, or playing the wrong second, is out of sync
    first.start += 1 / fps
    assert BC.c_dialogue_in_sync(ctx, _pc("dialogue_in_sync")).passed is False
    first.start -= 1 / fps
    first.in_ += 0.1
    bad = BC.c_dialogue_in_sync(ctx, _pc("dialogue_in_sync"))
    assert bad.passed is False and "in_" in str(bad.detail)


@pytest.mark.parametrize("first", ["video_ai_editor.agent.prompt.verify", "video_ai_editor.brain.checks"])
def test_the_checks_register_on_import_of_the_verify_module_alone(first):
    """Finalize (FX-C2 request): the brain checks used to register only when the executor was imported, so
    `test_every_check_spec_has_an_implementation` failed in a fresh process. Either import order must do it,
    with no import cycle, and the executor must not be needed."""
    import subprocess
    code = (f"import {first}, sys\n"
            "from video_ai_editor.agent.prompt import verify as V\n"
            "from video_ai_editor.agent.prompt.schema import CHECK_SPECS\n"
            "assert not (set(CHECK_SPECS) - set(V.CHECKS)), sorted(set(CHECK_SPECS) - set(V.CHECKS))\n"
            "assert 'video_ai_editor.agent.prompt.executor' not in sys.modules\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src")})
    assert r.returncode == 0, r.stderr[-800:]


def test_a_failed_dict_measure_never_reaches_the_user_as_a_dict():
    """Finalize (FX-D request, UX-12): the rollback question quoted `measured {'lane': 'a1', 'seams': 21, ...}`."""
    from video_ai_editor.agent.prompt.executor import check_failure_message
    from video_ai_editor.agent.prompt.verify import CheckResult
    bad = CheckResult(check="no_cut_mid_word", human="no cut lands inside a spoken word", passed=False,
                      measured={"lane": "a1", "seams": 21, "offending": ["x", "y", "z"]},
                      expected="cut edges outside every kept word",
                      detail="“really” cut at source 3.100s (seam 00:00:05:00 on a1); “so” cut at source 9.2s "
                             "(seam 00:00:09:00 on a1); “and” cut at source 12.0s (seam 00:00:12:00 on a1)")
    text = check_failure_message(bad)
    assert "{" not in text and "'lane'" not in text and "measured" not in text, text
    assert text.startswith("no cut lands inside a spoken word did not hold: “really” cut at source 3.100s")
    assert text.endswith("(and 1 more)")
    scalar = CheckResult(check="captions_cover", human="captions cover the speech", passed=False, measured=0.4,
                         expected=0.9)
    assert check_failure_message(scalar) == "captions cover the speech did not hold (measured 0.4, expected 0.9)"
