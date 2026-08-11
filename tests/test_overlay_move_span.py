"""Moving a text/sticker overlay must PRESERVE its span.

Reported as: "I removed the old video and added an 8-second one, but the
timeline still ran to 56s and played a black screen after the new clip ended."
The timeline was right about the data — a 0.1s sliver really was sitting at
55.98s. Reconstructed from the session's own ops log, the chain was:

  move_clip on an overlay  ->  start moved, `end` left behind (span inverted)
  ripple_delete            ->  _ripple_overlays remaps start/end independently,
                               so the inverted pair collapsed to its 0.1s floor
                               and stranded itself past the end of the footage
  edl.duration             ->  a max over EVERY track, so that sliver pinned
                               the timeline at the OLD footage length

A media Clip is immune: its span comes from in_/out, not an absolute `end`.
"""
from __future__ import annotations
import subprocess
from pathlib import Path

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Canvas, empty_edl


def _store(tmp_path: Path) -> EDLStore:
    (tmp_path / "edl.json").write_text(
        empty_edl(Canvas(w=1920, h=1080, fps=30)).model_dump_json())
    return EDLStore(tmp_path)


def _mk_video(path: Path, dur: float) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc=size=320x180:rate=30:duration={dur}",
         "-f", "lavfi", "-i", f"sine=f=440:duration={dur}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(path)], check=True, capture_output=True)
    return path


def test_moving_a_text_clip_keeps_its_duration(tmp_path: Path):
    s = _store(tmp_path)
    tid = dispatch(s, "add_text",
                   {"text": "HELLO", "start": 2.0, "end": 5.0, "role": "super"})["id"]

    dispatch(s, "move_clip", {"clip_id": tid, "new_start": 20.0})
    c = s.edl.get_clip(tid)[1]
    assert (c.start, c.end) == (20.0, 23.0), f"span destroyed: {c.start}-{c.end}"

    # ...and moving back LEFT must not invert it either.
    dispatch(s, "move_clip", {"clip_id": tid, "new_start": 0.5})
    c = s.edl.get_clip(tid)[1]
    assert (c.start, c.end) == (0.5, 3.5)


def test_moving_a_sticker_keeps_its_duration(tmp_path: Path):
    s = _store(tmp_path)
    sid = dispatch(s, "add_sticker",
                   {"emoji": "\U0001f680", "start": 1.0, "end": 4.0})["sticker_id"]
    dispatch(s, "move_clip", {"clip_id": sid, "new_start": 30.0})
    c = s.edl.get_clip(sid)[1]
    assert (c.start, c.end) == (30.0, 33.0)


def test_a_moved_overlay_never_persists_an_inverted_span(tmp_path: Path):
    """end < start validated and saved happily — nothing rejected it."""
    s = _store(tmp_path)
    tid = dispatch(s, "add_text",
                   {"text": "X", "start": 10.0, "end": 12.0, "role": "super"})["id"]
    for target in (40.0, 0.0, 7.5, 100.0):
        dispatch(s, "move_clip", {"clip_id": tid, "new_start": target})
        c = s.edl.get_clip(tid)[1]
        assert c.end > c.start, f"inverted at new_start={target}: {c.start}-{c.end}"
        assert abs((c.end - c.start) - 2.0) < 1e-6


def test_replacing_a_long_video_with_a_short_one_shortens_the_timeline(tmp_path: Path):
    """The end-to-end symptom, replayed from the reported session's ops log."""
    s = _store(tmp_path)
    long_v = _mk_video(tmp_path / "long.mp4", 6.0)
    short_v = _mk_video(tmp_path / "short.mp4", 2.0)

    dispatch(s, "add_clip", {"track": "v1", "src": str(long_v),
                             "in": 0.0, "out": 6.0, "start": 0.0})
    tid = dispatch(s, "add_text",
                   {"text": "FIRE", "start": 0.2, "end": 3.5, "role": "super"})["id"]
    sid = dispatch(s, "add_sticker",
                   {"emoji": "\U0001f680", "start": 0.5, "end": 3.0})["sticker_id"]

    # Drag both overlays out past the end of the footage — the gesture that
    # used to invert them.
    dispatch(s, "move_clip", {"clip_id": sid, "new_start": 60.0})
    dispatch(s, "move_clip", {"clip_id": tid, "new_start": 60.0})
    for cid in (tid, sid):
        c = s.edl.get_clip(cid)[1]
        assert c.end > c.start, f"{cid} inverted by the drag: {c.start}-{c.end}"

    # Delete every video clip, then bring in a short one.
    v1 = s.edl.get_track("v1")
    for c in list(v1.clips):
        dispatch(s, "ripple_delete", {"clip_id": c.id})

    # No overlay may have collapsed to the 0.1s minimum-span floor: that floor
    # is for a clip genuinely straddling a cut, not for one that merely sat
    # after it.
    for cid, want in ((tid, 3.3), (sid, 2.5)):
        c = s.edl.get_clip(cid)[1]
        assert abs((c.end - c.start) - want) < 1e-6, (
            f"{cid} span collapsed to {c.end - c.start:.3f}s (expected {want})")

    dispatch(s, "add_clip", {"track": "v1", "src": str(short_v),
                             "in": 0.0, "out": 2.0, "start": 0.0})
    assert s.edl.video_extent() < 2.5
