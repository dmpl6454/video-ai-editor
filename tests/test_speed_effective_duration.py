"""Speed must change a clip's TIMELINE footprint (CapCut semantics).

The EDL treated `duration` (source seconds) as timeline time everywhere, so
a 2x clip rendered 5s of video while the timeline drew 10s, `edl.duration`
said 10s, and the transport total never changed — the tester's exact report.
"""
from __future__ import annotations
import json
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Track, Clip, Canvas
from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.render import render_preview


def _mk_video(path: Path, *, duration: float = 5.0, color: str = "blue"):
    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi", "-i", f"color=c={color}:s=320x180:d={duration}:r=30",
         "-f", "lavfi", "-i", f"sine=f=440:duration={duration}",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True, capture_output=True)


@pytest.fixture
def store(tmp_path: Path) -> EDLStore:
    a = tmp_path / "a.mp4"; b = tmp_path / "b.mp4"
    _mk_video(a); _mk_video(b, color="red")
    s = EDLStore(tmp_path)
    s.edl.get_track("v1").clips = [
        Clip(src=str(a), in_=0, out=5, start=0, id="c1"),
        Clip(src=str(b), in_=0, out=5, start=5, id="c2"),
    ]
    s.edl.recompute_duration()
    return s


def test_effective_duration_property():
    c = Clip(src="x.mp4", in_=0, out=6, start=0, id="c")
    assert c.effective_duration == 6.0
    c.speed = 2.0
    assert c.effective_duration == 3.0
    c.speed = 0.5
    assert c.effective_duration == 12.0
    # Curve dicts (schema-only today) fall back to source duration.
    c.speed = {"curve": [[0, 1.0], [3, 2.0]]}
    assert c.effective_duration == 6.0


def test_set_speed_retimes_timeline(store: EDLStore):
    """2x on clip 1: its footprint halves, clip 2 ripples left, total drops."""
    dispatch(store, "set_speed", {"clip_id": "c1", "factor": 2.0})
    edl = store.edl
    c1, c2 = edl.get_track("v1").clips
    assert c1.effective_duration == pytest.approx(2.5)
    assert c2.start == pytest.approx(2.5), "clip 2 must ripple to the new end"
    assert edl.duration == pytest.approx(7.5)


def test_set_speed_slowdown_pushes_right(store: EDLStore):
    dispatch(store, "set_speed", {"clip_id": "c1", "factor": 0.5})
    edl = store.edl
    c2 = edl.get_track("v1").clips[1]
    assert c2.start == pytest.approx(10.0)
    assert edl.duration == pytest.approx(15.0)


def test_set_speed_ripples_overlays(store: EDLStore):
    """A caption sitting over clip 2 must move with it when clip 1 speeds up."""
    dispatch(store, "add_text", {"text": "over c2", "start": 6.0, "end": 8.0,
                                 "role": "super", "allow_stack": True})
    dispatch(store, "set_speed", {"clip_id": "c1", "factor": 2.0})
    tx = None
    for t in store.edl.tracks:
        for c in t.clips:
            if getattr(c, "text", None) == "over c2":
                tx = c
    assert tx is not None
    assert tx.start == pytest.approx(3.5), "overlay must shift left by 2.5s"
    assert tx.end == pytest.approx(5.5)


def test_rendered_duration_matches_edl_duration(store: EDLStore):
    dispatch(store, "set_speed", {"clip_id": "c1", "factor": 2.0})
    out = render_preview(store.edl, store.session_dir
                         if hasattr(store, "session_dir") else store.edl_path.parent,
                         height=180).path
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nokey=1:noprint_wrappers=1", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    rendered = float(probe.stdout.strip())
    assert rendered == pytest.approx(store.edl.duration, abs=0.35), (
        f"rendered {rendered}s vs edl.duration {store.edl.duration}s")


def test_cut_range_on_sped_clip_uses_effective_time(store: EDLStore):
    """Cutting timeline [1.0, 1.5) out of a 2x clip removes 1.0s of SOURCE
    (0.5 timeline-seconds * 2x), leaving a 2.0s-footprint clip pair."""
    dispatch(store, "set_speed", {"clip_id": "c1", "factor": 2.0})
    # c1 now occupies [0, 2.5); cut [1.0, 1.5) from inside it.
    dispatch(store, "cut_range", {"track": "v1", "start": 1.0, "end": 1.5})
    clips = store.edl.get_track("v1").clips
    left, right = clips[0], clips[1]
    assert left.out == pytest.approx(2.0)      # 1.0 timeline * 2x source
    assert right.in_ == pytest.approx(3.0)     # 1.5 timeline * 2x source
    assert right.effective_duration == pytest.approx(1.0)
    # Timeline repacked: 1.0 + 1.0 + 5.0 (clip c2) = 7.0 total
    assert store.edl.duration == pytest.approx(7.0)


def test_cut_range_ids_stay_unique(store: EDLStore):
    dispatch(store, "cut_range", {"track": "v1", "start": 1.0, "end": 1.5})
    dispatch(store, "cut_range", {"track": "v1", "start": 0.5, "end": 0.7})
    ids = [c.id for c in store.edl.get_track("v1").clips]
    assert len(set(ids)) == len(ids), f"duplicate ids after cuts: {ids}"


def test_split_at_on_sped_clip_uses_effective_time(store: EDLStore):
    """Splitting a 2x clip at timeline t=1.0 must cut 2.0s into the SOURCE."""
    dispatch(store, "set_speed", {"clip_id": "c1", "factor": 2.0})
    dispatch(store, "split_at", {"track": "v1", "time": 1.0})
    clips = store.edl.get_track("v1").clips
    left = clips[0]
    assert left.out == pytest.approx(2.0), (
        "left half must contain 2.0 source-seconds (1.0 timeline * 2x)")
    assert clips[1].start == pytest.approx(1.0)
