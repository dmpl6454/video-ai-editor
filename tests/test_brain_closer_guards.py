"""EB1 closer, guards and shared primitives: what the survived mutants showed nothing pins, one copy of each cap."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from video_ai_editor.agent.prompt.facts import TimelineFacts  # noqa: E402
from video_ai_editor.brain import compile as C  # noqa: E402
from video_ai_editor.brain.planner import plan  # noqa: E402


# --------------------------------------------------------------------------
# guards the survived mutants showed nothing pins (N-19)
# --------------------------------------------------------------------------

@pytest.fixture
def on_timeline_file(tmp_path):
    f = tmp_path / "already_on_timeline.mp4"
    f.write_bytes(b"x")
    return str(f.resolve())


def test_only_the_brain_tools_may_use_a_file_that_is_on_the_timeline_but_was_never_offered(on_timeline_file):
    """Mutant: `_on_timeline` accepted ANY tool. add_music must still be refused a path that was not offered."""
    import prompt_fixtures as F
    from video_ai_editor.agent.prompt import executor, validate as V
    facts = TimelineFacts.minimal(duration=30.0, allowed_paths=set(), timeline_paths={on_timeline_file}, tools_available=set())
    for tool in ("sync_dialogue_lane", "cut_source_ranges", "apply_camera_plan"):
        assert V._on_timeline(tool, on_timeline_file, facts) is True, tool
    for tool in ("add_clip", "add_music", "add_overlay", "tts_voiceover", "transcribe"):
        assert V._on_timeline(tool, on_timeline_file, facts) is False, tool
    reasons: list[str] = []
    executor._check_path_arg("add_music", "src", on_timeline_file, "read", facts, reasons)
    assert reasons and "path not offered" in reasons[0], reasons
    reasons = []
    executor._check_path_arg("sync_dialogue_lane", "src", on_timeline_file, "read", facts, reasons)
    assert reasons == [], reasons
    with pytest.raises(V.PlanRejected) as ei:
        V.validate_plan(F.plan_of(F.step("add_music", src=on_timeline_file, gain_db=-12)), facts)
    assert any("path not offered" in r for r in ei.value.reasons), ei.value.reasons


@pytest.mark.parametrize("extra, flagged", [(0.0, False), (0.1, False), (0.5, True), (1.0, True), (3.0, True)])
def test_a_run_that_removes_more_than_the_plan_names_is_blocked_at_a_boundary_not_five_seconds_off(tmp_path, extra, flagged):
    """Mutant: the removal_within_plan tolerance +5.0 s. The slack is a frame per cut and a millisecond, not a figure
    that grows past what one second of picture would show."""
    from types import SimpleNamespace
    import brain_contract_fixtures as BF
    import prompt_fixtures as F
    from video_ai_editor import config
    from video_ai_editor.agent.prompt.schema import Step
    from video_ai_editor.brain import checks as BCH
    config.WORKDIR = tmp_path
    store = F.make_store(tmp_path, name="s_overrun")
    src = next(c for c in store.edl.get_track("v1").clips).src
    BF.write_brain_files(Path(store.dir), src, workdir=tmp_path, bed=str(F.music_bed(tmp_path)))
    plan = F.plan_of(Step(tool="cut_source_ranges", args={"track": "v1", "ranges": "$brain:cuts", "plan_ref": BF.DID}, why="x"))
    before = store.edl.model_copy(deep=True)
    from video_ai_editor.brain import resolve as _r
    edp = _r.load_edp(_r.brain_dir_for(store), BF.DID)
    named, _cuts = BCH.planned_removal(before, edp, _r.brain_dir_for(store), list(plan.steps))
    track = store.edl.get_track("v1")
    remove = named + extra
    keep_out = max(c.start + c.effective_duration for c in track.clips) - remove
    if keep_out <= 0.2:
        pytest.skip("fixture too short for this overrun")
    last = track.clips[-1]
    last.out = last.in_ + max(0.1, (keep_out - last.start))
    got = BCH.removal_overrun(store, plan, SimpleNamespace(edl_before=before))
    assert bool(got) is flagged, (named, extra, store.edl.video_extent(), before.video_extent(), got)


# --------------------------------------------------------------------------
# one copy of each primitive, or a test that says they agree (N-20)
# --------------------------------------------------------------------------

def test_the_analysis_filler_lexicon_is_the_removal_tools_default_list():
    import importlib
    from video_ai_editor.brain.analysis import fillers
    D = importlib.import_module("video_ai_editor.agent.dispatch")
    assert set(fillers.LEXICAL_FILLERS) == set(D._DEFAULT_FILLERS)


def test_the_compilers_stage_order_and_caps_are_the_resolvers_and_the_tools():
    from video_ai_editor.agent import tools
    from video_ai_editor.brain import plan_rules, resolve
    from video_ai_editor.brain import caption_lane
    assert C.STAGE2_ORDER is resolve.STAGE2_ORDER and C.MAX_RANGE_S == tools.CUT_RANGE_MAX_S
    assert resolve.MAX_RANGES_PER_DISPATCH == plan_rules.MAX_CUT_RANGES == tools.CUT_RANGES_MAX
    assert resolve.MAX_SWITCHES_PER_DISPATCH == plan_rules.MAX_CAMERA_SWITCHES == tools.CAMERA_SWITCHES_MAX
    assert caption_lane.MAX_CUES == tools.CAPTION_CUES_MAX
    from video_ai_editor import storage_brain
    from video_ai_editor.agent.prompt.schema import PLAN_REF_PATTERN
    from video_ai_editor.brain.schema import MAX_GRAPH_BYTES
    assert storage_brain.BRAIN_FILE_CAP == MAX_GRAPH_BYTES and storage_brain._EDP_RE.match("decisions/d_0badf00d.json")
    assert not storage_brain._EDP_RE.match("decisions/d_0badf00dd.json") and PLAN_REF_PATTERN == "^d_[0-9a-f]{8}$"


# --------------------------------------------------------------------------
# the path scrubs: file: URLs, a path inside a sentence, an unrooted path (N-23)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text, clean", [
    ("see file:///Users/x/y.mp4", "see y.mp4"),
    ("ffmpeg: file:/Users/x/My Clip.mp4: No such file", "ffmpeg: My Clip.mp4: No such file"),
    ("FILE://host/Users/x/y.mp4 failed", "y.mp4 failed"),
    ("pcm decode failed: /Users/x/Library/Caches/a b/clip.mp4: bad", "pcm decode failed: clip.mp4: bad"),
    ("and/or 24/7 a \"quote\"", "and/or 24/7 a \"quote\""),
])
def test_the_text_scrub_reduces_every_spelling_of_a_path_to_its_leaf(text, clean):
    from video_ai_editor.brain import digest
    out = digest.scrub_paths_in_text(text)
    assert out == clean and "/Users" not in out and "file:" not in out.lower()


def test_the_cards_and_routes_scrub_a_path_inside_a_sentence_too():
    from video_ai_editor.agent.prompt.brain_card import scrub_paths
    got = scrub_paths({"a": "Cut at /Users/me/x.mp4 now", "b": ["~/x.mp4", "C:\\Users\\me\\x.mp4", "/Users/me/My Clip.mov", "0:37 and/or 24/7"],
                       "/Users/me/k.mp4": 3, "n": 4.5, "u": "file:///Users/me/z.mp4", "plain": "Cut a 1.4 s silence at 0:37"})
    assert got == {"a": "Cut at x.mp4 now", "b": ["x.mp4", "x.mp4", "My Clip.mov", "0:37 and/or 24/7"], "k.mp4": 3, "n": 4.5,
                   "u": "z.mp4", "plain": "Cut a 1.4 s silence at 0:37"}


@pytest.mark.parametrize("leak", ["Users/sudhanshu/Library/Caches/x", "back\\slash", "the Library/Caches folder", "work/s_1/uploads/a.mov"])
def test_an_unrooted_path_is_refused_by_the_last_line_guard(leak):
    from video_ai_editor.brain import digest
    with pytest.raises(digest.PathLeak):
        digest.assert_path_free({"windows": [{"start": 0, "end": 1, "text": leak}], "k": 1})


@pytest.mark.parametrize("prose", ["and/or", "24/7", "1/2/2024", 'He said "no" to me, it\'s fine', "go to the left/right/centre please"])
def test_prose_with_a_slash_is_not_a_path(prose):
    from video_ai_editor.brain import digest
    digest.assert_path_free({"windows": [{"start": 0, "end": 1, "text": prose}], "k": 1})
