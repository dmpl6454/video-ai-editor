"""`auto_cut_to_beats(min_shot=…)` never leaves a V1 shot shorter than the
minimum, and does so by SKIPPING beats — never by moving a cut off the beat.

WHY: the beat_sync recipe promises `min_shot_geq(0.8)`. When the music bed
already exists the plan cannot precompute the grid, so `auto_cut_to_beats`
is the only thing placing the cuts — and it used to split on every Nth beat
regardless, so the fragment before the first beat was whatever the footage
happened to give (benchmark case 10 measured 0.697 s) and the plan failed
its own postcondition. `min_shot` defaults to 0 so every existing caller
keeps the historical behaviour.

Beats come from a stand-in `librosa` (the handler imports it lazily by
name, so sys.modules is the seam) — the test means the same thing with or
without the optional dependency installed.
"""
from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import _beat_cuts_keeping_min_shot, dispatch
from video_ai_editor.edl.schema import Clip
from video_ai_editor.edl.snapshot import EDLStore

MIN_SHOT = 0.8
DENSE_BEATS = [0.5 * i for i in range(1, 12)]        # 0.5 … 5.5 on a 6 s clip (120 BPM)


# ------------------------------------------------------------ the pure rule

def test_beats_too_close_to_an_edge_or_the_previous_cut_are_skipped_not_shifted():
    kept, merged = _beat_cuts_keeping_min_shot(DENSE_BEATS, [(0.0, 6.0)], MIN_SHOT)
    # 0.5 is 0.5 s from the head; 5.5 is 0.5 s from the tail; every other
    # odd beat is 0.5 s from the cut just kept — six skipped, five kept.
    assert kept == [1.0, 2.0, 3.0, 4.0, 5.0] and merged == 6
    assert set(kept) <= set(DENSE_BEATS)              # cadence kept: nothing was nudged


def test_zero_min_shot_is_the_historical_cut_on_every_beat():
    kept, merged = _beat_cuts_keeping_min_shot(DENSE_BEATS, [(0.0, 6.0)], 0.0)
    assert kept == DENSE_BEATS and merged == 0


def test_existing_boundaries_and_gaps_count_as_shot_edges():
    # Two shots with a gap: 0–2.4 and 3.0–6.0. Beats 2.0 (0.4 s before the
    # first shot's tail) and 3.5 (0.5 s after the second one's head) would
    # leave short fragments and join the four previous-cut merges; 2.5 lies
    # in the gap and 3.0 sits ON an edge — neither is a merge, there is
    # nothing to cut there.
    shots = [(0.0, 2.4), (3.0, 6.0)]
    kept, merged = _beat_cuts_keeping_min_shot(DENSE_BEATS, shots, MIN_SHOT)
    assert kept == [1.0, 4.0, 5.0] and merged == 6
    assert 2.5 not in kept and all(any(s < t < e for s, e in shots) for t in kept)


def test_a_shot_shorter_than_twice_min_shot_gets_no_cut_at_all():
    kept, merged = _beat_cuts_keeping_min_shot([0.5, 1.0], [(0.0, 1.5)], MIN_SHOT)
    assert kept == [] and merged == 2


# ------------------------------------------------- through the real handler

def _fake_librosa(beats: list[float]) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        load=lambda path, sr, mono, offset, duration: ([], sr),
        beat=types.SimpleNamespace(beat_track=lambda y, sr: (120.0, list(range(len(beats))))),
        frames_to_time=lambda frames, sr: types.SimpleNamespace(tolist=lambda: list(beats)),
    )


def _store(tmp_path: Path) -> EDLStore:
    video, music = tmp_path / "v.mp4", tmp_path / "m.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=c=blue:s=320x180:d=6:r=30", "-c:v", "libx264",
                    "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(video)],
                   check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=f=200:duration=6", "-c:a", "pcm_s16le", str(music)],
                   check=True, capture_output=True)
    store = EDLStore(tmp_path / "sess")
    dispatch(store, "add_clip", {"track": "v1", "src": str(video), "in": 0, "out": 6, "start": 0})
    dispatch(store, "add_clip", {"track": "music", "src": str(music), "in": 0, "out": 6, "start": 0})
    return store


def _v1(store: EDLStore) -> list[Clip]:
    return [c for c in store.edl.get_track("v1").clips if isinstance(c, Clip)]


@pytest.fixture
def dense_beats(monkeypatch):
    monkeypatch.setitem(sys.modules, "librosa", _fake_librosa(DENSE_BEATS))


def test_handler_honours_min_shot_and_reports_the_merged_beats(tmp_path: Path, dense_beats):
    store = _store(tmp_path)
    ops_before = len(store.ops.ops)

    result = dispatch(store, "auto_cut_to_beats", {"subdivision": 1, "min_shot": MIN_SHOT})

    assert (result["splits"], result["merged"], result["beats_total"]) == (5, 6, 11)
    assert "6 skipped" in result["summary"]
    clips = _v1(store)
    assert len(clips) == 6
    assert min(c.effective_duration for c in clips) >= MIN_SHOT
    boundaries = [round(c.start, 3) for c in clips[1:]]
    assert boundaries == [1.0, 2.0, 3.0, 4.0, 5.0]       # every kept cut is ON a beat
    assert len(store.ops.ops) == ops_before + 1           # still one undo step
    dispatch(store, "undo", {})
    assert len(_v1(store)) == 1


def test_handler_default_is_the_historical_every_beat_behaviour(tmp_path: Path, dense_beats):
    store = _store(tmp_path)

    result = dispatch(store, "auto_cut_to_beats", {"subdivision": 1})

    assert (result["splits"], result["merged"]) == (11, 0)
    assert min(c.effective_duration for c in _v1(store)) == pytest.approx(0.5)


def test_subdivision_still_thins_the_grid_before_the_min_shot_rule(tmp_path: Path, dense_beats):
    store = _store(tmp_path)

    result = dispatch(store, "auto_cut_to_beats", {"subdivision": 2, "min_shot": MIN_SHOT})

    # Every 2nd beat: 0.5, 1.5, 2.5, 3.5, 4.5, 5.5 — 0.5 (head) and 5.5
    # (tail) are skipped, the rest are 1 s apart.
    assert (result["splits"], result["merged"]) == (4, 2)
    assert [round(c.start, 3) for c in _v1(store)[1:]] == [1.5, 2.5, 3.5, 4.5]
