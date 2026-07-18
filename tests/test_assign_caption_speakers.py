"""assign_caption_speakers — the tool that makes TextClip.speaker real.

`diarize` is read-only (returns turns, never touches the EDL) and the
`speaker` field sat unread by anything. This tool assigns each caption the
max-overlap speaker and sets a per-speaker style color that the renderers
honor. Tests pass `turns` inline so no diarization model is needed.
"""
from __future__ import annotations
from pathlib import Path

import pytest

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import TextClip
from video_ai_editor.agent.dispatch import dispatch


TURNS = [
    {"speaker": "SPEAKER_00", "start": 0.0, "end": 4.0},
    {"speaker": "SPEAKER_01", "start": 4.0, "end": 8.0},
]


@pytest.fixture
def store(tmp_path: Path) -> EDLStore:
    s = EDLStore(tmp_path / "sess")
    cap = s.edl.get_track("captions")
    cap.clips.append(TextClip(text="hello there", start=0.5, end=2.0, role="caption"))
    cap.clips.append(TextClip(text="hi back", start=4.5, end=6.0, role="caption"))
    cap.clips.append(TextClip(text="straddles", start=3.5, end=4.6, role="caption"))
    return s


def test_assigns_by_max_overlap(store: EDLStore):
    r = dispatch(store, "assign_caption_speakers", {"turns": TURNS})
    caps = store.edl.get_track("captions").clips
    assert caps[0].speaker == "SPEAKER_00"
    assert caps[1].speaker == "SPEAKER_01"
    # straddler: [3.5,4.6] overlaps S00 by 0.5s and S01 by 0.6s → S01
    assert caps[2].speaker == "SPEAKER_01"
    assert r["assigned"] == 3


def test_second_speaker_gets_visible_color(store: EDLStore):
    dispatch(store, "assign_caption_speakers", {"turns": TURNS})
    caps = store.edl.get_track("captions").clips
    # first speaker keeps default white (role-style sentinel), second is colored
    assert caps[0].style.color == "#FFFFFF"
    assert caps[1].style.color != "#FFFFFF"


def test_brand_palette_wins_for_secondary_speakers(store: EDLStore):
    from video_ai_editor.edl.schema import BrandKit
    store.edl.brand_kit = BrandKit(palette=["#123456"])
    dispatch(store, "assign_caption_speakers", {"turns": TURNS})
    caps = store.edl.get_track("captions").clips
    assert caps[1].style.color == "#123456"


def test_no_captions_is_loud(tmp_path: Path):
    s = EDLStore(tmp_path / "empty")
    with pytest.raises(ValueError, match="no caption clips"):
        dispatch(s, "assign_caption_speakers", {"turns": TURNS})


def test_empty_turns_is_loud(store: EDLStore):
    with pytest.raises(ValueError):
        dispatch(store, "assign_caption_speakers", {"turns": []})
