"""agent/dispatch.py — transcript resolution is scoped to the CURRENT v1 source.

Regression coverage for R3 (docs/superpowers/plans/
2026-07-10-editor-issues-verification-and-fixes.md): get_transcript(),
add_caption_track(), and auto_caption() used to resolve "the transcript" via
`sd.glob("uploads/**/ingest.json")[0]` — an arbitrary hit with no ordering
guarantee. In a session with more than one uploaded source (e.g. the user
replaced the footage, or a second video was imported), this could silently
return a DIFFERENT video's transcript than the one actually on the timeline —
exactly the "Claude describes a stale video" bug reported after testing.

The fix resolves ingest.json as a sibling of the v1 clip's own `src` path
(ingest_upload() always writes them into the same uploads/<stem>/ directory),
so the transcript returned is always the one for the clip that's actually
on the timeline right now.
"""
from __future__ import annotations
import json
from pathlib import Path

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Canvas, Track, Clip
from video_ai_editor.agent.dispatch import dispatch, get_transcript, _current_v1_ingest_json


def _seed_ingest(dir_: Path, text: str) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    p = dir_ / "ingest.json"
    p.write_text(json.dumps({
        "transcript": {
            "language": "en", "duration": 2.0,
            "segments": [{"id": 0, "start": 0.0, "end": 2.0, "text": text, "words": []}],
        }
    }))
    return p


def _store_with_v1_clip(tmp_path: Path, clip_dir_name: str) -> EDLStore:
    clip_dir = tmp_path / "uploads" / clip_dir_name
    clip_dir.mkdir(parents=True, exist_ok=True)
    src = clip_dir / f"{clip_dir_name}.normalized.mp4"
    src.write_bytes(b"not a real mp4 - dispatch/get_transcript never opens it")
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30),
              tracks=[Track(id="v1", type="video", clips=[
                  Clip(src=str(src), in_=0, out=2, start=0, id="c1"),
              ])])
    edl.recompute_duration()
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(tmp_path)


def test_current_v1_ingest_json_matches_the_active_clips_own_directory(tmp_path: Path):
    store = _store_with_v1_clip(tmp_path, "video_a")
    _seed_ingest(tmp_path / "uploads" / "video_a", "this is video a")
    # A second, unrelated upload directory exists in the same session —
    # the old glob-first code could just as easily have picked this one.
    _seed_ingest(tmp_path / "uploads" / "video_b_stale", "this is a stale different video")

    resolved = _current_v1_ingest_json(store)
    assert resolved is not None
    assert resolved.parent.name == "video_a"


def test_get_transcript_returns_the_current_clips_transcript_not_an_older_one(tmp_path: Path):
    store = _store_with_v1_clip(tmp_path, "video_b")
    # Seed the STALE transcript first (so it would sort/glob first on most
    # filesystems if the code still picked "the first hit").
    _seed_ingest(tmp_path / "uploads" / "aaa_older_upload", "old stale screen recording narration")
    _seed_ingest(tmp_path / "uploads" / "video_b", "the current yoga video transcript")

    tx = get_transcript(store, {})
    texts = " ".join(s["text"] for s in tx["segments"])
    assert "current yoga video" in texts
    assert "stale" not in texts


def test_get_transcript_empty_when_no_ingest_json_for_current_clip(tmp_path: Path):
    store = _store_with_v1_clip(tmp_path, "video_c")
    # A transcript exists, but for a DIFFERENT (unrelated) upload dir — must
    # not be returned as if it belonged to the current clip.
    _seed_ingest(tmp_path / "uploads" / "unrelated", "someone else's video")

    tx = get_transcript(store, {})
    assert tx["segments"] == []


def test_get_transcript_empty_on_a_fresh_timeline_with_no_clips(tmp_path: Path):
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30),
              tracks=[Track(id="v1", type="video", clips=[])])
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)
    tx = get_transcript(store, {})
    assert tx["segments"] == []


def test_switching_the_v1_clip_switches_the_resolved_transcript(tmp_path: Path):
    """Replacing the footage on v1 (ripple_delete old clip, add_clip new one)
    must make get_transcript follow the new clip, not keep returning the old
    transcript — this is the exact "remembers an old media" symptom."""
    store = _store_with_v1_clip(tmp_path, "first_video")
    _seed_ingest(tmp_path / "uploads" / "first_video", "narration for the first video")
    assert "first video" in " ".join(s["text"] for s in get_transcript(store, {})["segments"])

    # Replace v1's clip with a new source (simulating a fresh upload).
    old_id = store.edl.tracks[0].clips[0].id
    dispatch(store, "ripple_delete", {"clip_id": old_id})
    second_dir = tmp_path / "uploads" / "second_video"
    second_dir.mkdir(parents=True, exist_ok=True)
    second_src = second_dir / "second_video.normalized.mp4"
    second_src.write_bytes(b"placeholder")
    _seed_ingest(second_dir, "narration for the second video")
    dispatch(store, "add_clip", {
        "track": "v1", "src": str(second_src), "in": 0.0, "out": 2.0, "start": 0.0,
    })

    tx_after = get_transcript(store, {})
    texts_after = " ".join(s["text"] for s in tx_after["segments"])
    assert "second video" in texts_after
    assert "first video" not in texts_after
