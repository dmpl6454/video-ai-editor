"""QA-011 remainder: remove_fillers' DEFAULT list must not cut content words.

The default was ["um", "uh", "like", "you know", "so basically"]. Matching is
per single token, so the two multi-word entries never matched anything, and
"like" cut the VERB out of "Um, I like this part." (measured on scene_16x9:
0.333 s of that sentence gone). A caller who wants "like" removed passes it.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.agent.timemap import source_range_to_timeline
from video_ai_editor.edl.snapshot import EDLStore


def _store(tmp_path: Path) -> tuple[EDLStore, str]:
    src = tmp_path / "talk.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=6:r=30",
                    "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000:duration=6",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(src)], check=True, capture_output=True)
    store = EDLStore(tmp_path / "sess")
    dispatch(store, "add_clip", {"track": "v1", "src": str(src), "in": 0, "out": 6, "start": 0})
    words = [("Um,", 0.5, 0.8), ("I", 1.0, 1.1), ("like", 1.2, 1.5), ("this", 1.6, 1.8),
             ("part.", 1.9, 2.3), ("uh", 3.0, 3.3), ("like,", 3.5, 3.8), ("done.", 4.0, 4.4)]
    (src.parent / "ingest.json").write_text(json.dumps({"transcript": {
        "language": "en", "duration": 6.0,
        "segments": [{"id": 0, "start": 0.5, "end": 4.4,
                      "text": " ".join(w for w, _, _ in words),
                      "words": [{"word": w, "start": s, "end": e} for w, s, e in words]}]}}))
    return store, str(src)


def _on_timeline(store, src, s, e) -> bool:
    return bool(source_range_to_timeline(store.edl, "v1", s, e, src=src))


def test_default_pass_keeps_the_verb_like(tmp_path):
    store, src = _store(tmp_path)
    out = dispatch(store, "remove_fillers", {})
    assert out["words"] == 2                      # "Um," and "uh"
    assert not _on_timeline(store, src, 0.5, 0.8)
    assert not _on_timeline(store, src, 3.0, 3.3)
    assert _on_timeline(store, src, 1.2, 1.5), "the verb in 'I like this part' was cut"


def test_like_is_still_removable_when_asked_for(tmp_path):
    store, src = _store(tmp_path)
    out = dispatch(store, "remove_fillers", {"words": ["like"], "pad": 0.0})
    assert out["words"] == 2
    assert not _on_timeline(store, src, 1.2, 1.5)
