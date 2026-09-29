"""Gate (0.8.0 final QA): a plain split of a clip that plays a speed CURVE
(Hero, Montage, ...) was rolled back by the prompt contract as "It changed a
clip's speed". Re-slicing a curve gives each piece its own part of the curve,
so each piece's MEAN speed differs from the whole clip's, though nothing about
the playback changed. Found by tests/test_wave_d2_fixer_ui.py
(key-free Prompt bar: "add a hero speed ramp", then "split at 1 second").

The rule still catches a real speed change on a split clip: a piece that
stops playing a curve, or a constant-speed clip whose factor moved.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture, no_downloads  # noqa: E402,F401

from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.agent.prompt.contract import Contract  # noqa: E402
from video_ai_editor.agent.prompt.contract_diff import diff  # noqa: E402


def _curve_clip(tmp_path):
    st = F.make_store(tmp_path)
    cid = st.edl.get_track("v1").clips[0].id
    dispatch(st, "set_speed", {"clip_id": cid, "preset": "hero"})
    return st, cid


def _split(st, t: float):
    before = st.edl.model_copy(deep=True)
    dispatch(st, "split_at", {"track": "v1", "time": t})
    return before, st.edl


@pytest.mark.usefixtures("desktop_posture")
def test_splitting_a_curve_clip_is_not_a_speed_change(tmp_path):
    st, _ = _curve_clip(tmp_path)
    before, after = _split(st, 1.0)
    d = diff(before, after)
    assert "v1:split" in d.categories, d.categories
    assert "clip:speed" not in d.categories, d.categories
    assert Contract.read("split at 1 second").judge(before, after) == []


@pytest.mark.usefixtures("desktop_posture")
def test_a_split_piece_that_loses_its_curve_is_still_a_speed_change(tmp_path):
    st, _ = _curve_clip(tmp_path)
    before, after = _split(st, 1.0)
    piece = after.get_track("v1").clips[1]
    dispatch(st, "set_speed", {"clip_id": piece.id, "factor": 2.0})
    assert "clip:speed" in diff(before, st.edl).categories


@pytest.mark.usefixtures("desktop_posture")
def test_a_split_piece_given_another_curve_is_still_a_speed_change(tmp_path):
    st, _ = _curve_clip(tmp_path)
    before, after = _split(st, 1.0)
    piece = after.get_track("v1").clips[1]
    dispatch(st, "set_speed", {"clip_id": piece.id, "preset": "montage"})
    assert "clip:speed" in diff(before, st.edl).categories


@pytest.mark.usefixtures("desktop_posture")
def test_a_constant_speed_split_piece_that_changes_factor_is_still_caught(tmp_path):
    st = F.make_store(tmp_path)
    cid = st.edl.get_track("v1").clips[0].id
    dispatch(st, "set_speed", {"clip_id": cid, "factor": 1.5})
    before, _ = _split(st, 1.0)
    piece = st.edl.get_track("v1").clips[1]
    dispatch(st, "set_speed", {"clip_id": piece.id, "factor": 2.0})
    assert "clip:speed" in diff(before, st.edl).categories


# --- beat sync --------------------------------------------------------------
# Gate (0.8.0 final QA): "cut to the beat of the music" (benchmark case 10) was
# rolled back as "It zoomed a clip, which the request did not ask for": the
# contract's clause reader had no beat-sync phrasing, so the recipe's beat
# pulses (scale keyframes) were judged unasked. INTENT_FAMILIES already calls
# beat_sync a composite recipe; the clause reader now agrees.

@pytest.mark.parametrize("phrase", ["cut to the beat of the music", "cut to the beat", "beat sync it",
                                    "sync the cuts to the beat", "cut on the beats"])
def test_beat_sync_phrasings_read_as_the_composite_recipe(phrase):
    assert Contract.read(phrase).composite, phrase


@pytest.mark.parametrize("phrase", ["cut the first 5 seconds", "turn the music down", "zoom in on clip 2"])
def test_plain_edits_do_not_read_as_composite(phrase):
    assert not Contract.read(phrase).composite, phrase


@pytest.fixture
def generated_music_beds(tmp_path, monkeypatch):
    """Hermetic music beds. The recipes expander asks "Which music?" when
    `presets.music_beds()` is empty, and a CI runner has no generated
    presets/music (the beds are build products, not checked in). Synthesise
    them (lavfi, ffmpeg only, deterministic) into tmp_path and point
    `music_dir` there, so the test never depends on the developer's cache."""
    from video_ai_editor.agent.prompt import presets
    beds_dir = tmp_path / "music_beds"
    try:
        beds = presets.generate_music_beds(beds_dir)
    except Exception as e:  # ffmpeg missing / lavfi aevalsrc unsupported / ebur128 unparsable
        pytest.skip(f"music beds cannot be generated here: {type(e).__name__}: {str(e)[:120]}")
    if not beds:
        pytest.skip("music beds cannot be generated here: generate_music_beds returned none")
    monkeypatch.setattr(presets, "music_dir", lambda: beds_dir)
    return beds


@pytest.mark.usefixtures("desktop_posture", "no_downloads", "generated_music_beds")
def test_cut_to_the_beat_commits_on_the_key_free_ladder(tmp_path):
    from video_ai_editor.agent.prompt import service
    st = F.make_store(tmp_path)
    F.collect(service.prompt_turn(st, "add chill background music and duck it under my voice", [],
                                  brain="recipes", ui_state={}))
    before = len(st.edl.get_track("v1").clips)
    ev = F.collect(service.prompt_turn(st, "cut to the beat of the music", [], brain="recipes", ui_state={}))
    types = [x["type"] for x in ev]
    reply = "".join(x.get("text", "") for x in ev if x["type"] == "text_delta")
    assert "clarify" not in types and "op" in types, reply
    assert len(st.edl.get_track("v1").clips) > before, reply
