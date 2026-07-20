"""split_at must mint UNIQUE ids for the right-hand halves.

The old scheme appended a literal 'b' (c_X → c_Xb), so splitting a clip
whose earlier split-sibling already claimed c_Xb produced two clips with
the same id — and every clip_id-targeted tool then hits the wrong one.
Found live: split at 3.0 then 1.2 on the same source clip.
"""
from __future__ import annotations
from pathlib import Path

import subprocess

import pytest

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Clip, Track
from video_ai_editor.agent.dispatch import dispatch


@pytest.fixture
def store(tmp_path: Path) -> EDLStore:
    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", "color=c=blue:s=160x90:d=6:r=30",
         "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True)
    s = EDLStore(tmp_path)
    s.edl.get_track("v1").clips = [
        Clip(src=str(src), in_=0, out=6, start=0, id="c_orig"),
    ]
    s.edl.recompute_duration()
    return s


def test_repeated_splits_never_duplicate_ids(store: EDLStore):
    dispatch(store, "split_at", {"track": "v1", "time": 3.0})
    dispatch(store, "split_at", {"track": "v1", "time": 1.2})
    dispatch(store, "split_at", {"track": "v1", "time": 4.5})
    ids = [c.id for c in store.edl.get_track("v1").clips]
    assert len(ids) == 4
    assert len(set(ids)) == len(ids), f"duplicate clip ids after splits: {ids}"
