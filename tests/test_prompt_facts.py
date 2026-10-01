"""`build_facts` details the findings exposed (agent/prompt/facts.py, §2.1):
the feature gate must not remove tools that work without an optional speed
tier, and the music bed's clip ids are known so a "replace" can remove it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture  # noqa: E402,F401

from video_ai_editor.agent.prompt import facts as FA  # noqa: E402
from video_ai_editor.agent.prompt import planner as P  # noqa: E402
from video_ai_editor.ai import features as _features  # noqa: E402

pytestmark = pytest.mark.usefixtures("desktop_posture")

# The real shape `/api/features` reports on this Mac: the CUDA speed tier is
# unavailable and names auto_caption; nothing else is missing.
MAC_REPORT = {"available": [], "unavailable": [
    {"key": "gpu_transcribe", "feature": "GPU-accelerated transcription (NVIDIA)",
     "tools": ["auto_caption", "get_transcript"], "optional": True,
     "fix": "`uv sync --all-extras --group dev --group cuda` (adds cuBLAS + cuDNN, ~1.3GB) on a machine with an NVIDIA GPU"},
]}


def test_an_optional_speed_tier_never_gates_the_tools_it_names(tmp_path: Path):
    store = F.make_store(tmp_path)
    built = FA.build_facts(store, None, feature_report=MAC_REPORT)
    assert {"auto_caption", "get_transcript", "transcribe", "add_caption_track"} <= built.tools_available
    hard = {"available": [], "unavailable": [{"key": "captions", "tools": ["auto_caption", "add_caption_track"], "fix": "x"}]}
    gated = FA.build_facts(store, None, feature_report=hard)
    assert not ({"auto_caption", "transcribe"} & gated.tools_available)


def test_the_live_report_flags_the_speed_tier_and_hinglish_captions_ask_one_question(tmp_path: Path):
    report = _features.feature_report()
    gpu = next(e for e in report["available"] + report["unavailable"] if e["key"] == "gpu_transcribe")
    assert gpu.get("optional") is True
    store = F.make_store(tmp_path)
    built = FA.build_facts(store, None, feature_report=report)
    if "faster_whisper" in sys.modules or __import__("importlib").util.find_spec("faster_whisper"):
        assert "auto_caption" in built.tools_available
    facts = built.with_(first_use={"madlad": 3_000_000_000, **{k: v for k, v in built.first_use.items() if k != "madlad"}})
    plan = P.plan("add hinglish captions", facts)
    keys = [q.key for q in plan.blocking_questions]
    assert keys == ["downloads"], keys          # never a second `gate_auto_caption` on a Mac


def test_music_clip_ids_are_reported(tmp_path: Path):
    store = F.make_store(tmp_path)
    bed = F.music_bed(tmp_path / "sess")
    from video_ai_editor.agent.dispatch import dispatch
    dispatch(store, "add_music", {"src": str(bed), "start": 0.0, "volume_db": -14})
    built = FA.build_facts(store, None, feature_report={"unavailable": []})
    music = store.edl.get_track("music")
    assert built.has_music and built.music_clip_ids == [c.id for c in music.clips]


def test_the_brain_block_lives_in_facts_brain_and_both_files_stay_within_the_limit():
    """SC-09: facts.py went from 714 to 815 lines with the brain block; it is split out, both are <= 800."""
    from video_ai_editor.agent.prompt import facts_brain
    for mod in (FA, facts_brain):
        assert len(Path(mod.__file__).read_text(encoding="utf-8").splitlines()) <= 800, mod.__name__
    assert FA.DialogueLaneFact is facts_brain.DialogueLaneFact and FA.BrainEditFact is facts_brain.BrainEditFact
    assert not hasattr(FA, "_brain_facts")


def test_brain_off_facts_carry_no_brain_field(tmp_path: Path, monkeypatch):
    """SC-12 / SC-04: with brain.enabled off the brain reads nothing (the lane used to be built before the flag was looked at)."""
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "0")
    store = F.make_store(tmp_path)
    from video_ai_editor.agent.dispatch import dispatch
    dispatch(store, "sync_dialogue_lane", {"src": next(iter(store.edl.get_track("v1").clips)).src, "lane": "a1",
                                           "offsets": {}})
    assert FA.build_facts(store, None, feature_report={"unavailable": []}).dialogue_lane is None
    from video_ai_editor.agent.prompt import facts_brain
    assert facts_brain.brain_facts(store, store.edl, Path(store.dir), resolve=str) == {}
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    lane = FA.build_facts(store, None, feature_report={"unavailable": []}).dialogue_lane
    assert lane is not None and lane.in_sync is None and lane.offsets == {}        # no brain run laid it: nothing to measure against


def test_a_transcript_that_is_not_coming_is_not_pending_with_the_brain_on(tmp_path: Path, monkeypatch):
    """Finalize: an upload made with `transcribe=false` (`transcript_status: skipped`) or whose whisper pass
    raised (`failed`) has no transcript and never will; only an upload still awaiting its pass is pending."""
    import json
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    store = F.make_store(tmp_path, with_transcript=False)
    assert FA.build_facts(store, None, feature_report=MAC_REPORT).transcript_pending is True     # awaiting whisper
    ing = next((tmp_path / "uploads").rglob("ingest.json"))
    for status in ("skipped", "failed"):
        ing.write_text(json.dumps({"src": "x", "transcript_status": status}), encoding="utf-8")
        assert FA.build_facts(store, None, feature_report=MAC_REPORT).transcript_pending is False, status


def test_the_marker_is_ignored_with_the_brain_off_as_in_0_8_0(tmp_path: Path, monkeypatch):
    """Closer N-24 (2): the flag-off Prompt bar reads ingest.json as 0.8.0 did: no transcript and a fresh file is pending."""
    import json
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "0")
    store = F.make_store(tmp_path, with_transcript=False)
    ing = next((tmp_path / "uploads").rglob("ingest.json"))
    for status in ("skipped", "failed"):
        ing.write_text(json.dumps({"src": "x", "transcript_status": status}), encoding="utf-8")
        assert FA.build_facts(store, None, feature_report=MAC_REPORT).transcript_pending is True, status