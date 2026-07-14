"""search_media: local CLIP visual search + transcript (spoken) search.

The CLIP model is heavy (~150 MB download, torch on MPS), so the visual
ranking test is gated on open_clip being importable AND a fast env opt-in,
skipping cleanly on CI. The spoken-scope and arg-validation tests run always.
"""
from __future__ import annotations
import json
import os
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Track, Clip, Canvas
from video_ai_editor.agent.dispatch import dispatch


def _mk(path: Path, vf: str):
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", vf,
                    "-pix_fmt", "yuv420p", str(path)],
                   check=True, capture_output=True)


def _store_with_clips(tmp_path: Path, specs: list[tuple[str, str]]) -> EDLStore:
    # Mirror the real ingest_upload() contract (ingest/pipeline.py): the
    # normalized media and its ingest.json are siblings in the same
    # uploads/<stem>/ directory, and a clip's `src` points at that normalized
    # file. get_transcript()/find_moments() resolve the v1 clip's ingest.json
    # from Path(src).parent — so the fixture must put them in the same dir,
    # not an unrelated one, or the "current source" resolution can't find it.
    clips = []
    ingest_dir = tmp_path / "uploads" / "clip0"
    ingest_dir.mkdir(parents=True, exist_ok=True)
    for i, (name, vf) in enumerate(specs):
        p = ingest_dir / f"{name}.normalized.mp4"
        _mk(p, vf)
        clips.append(Clip(src=str(p), in_=0, out=2, start=i * 2, id=name))
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30),
              tracks=[Track(id="v1", type="video", clips=clips)])
    edl.recompute_duration()
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    # seed a transcript where get_transcript reads it: <v1 clip's dir>/ingest.json
    (ingest_dir / "ingest.json").write_text(json.dumps({
        "transcript": {
            "language": "en", "duration": 6.0,
            "segments": [
                {"id": 0, "start": 0.0, "end": 2.0, "text": "the sunset was beautiful", "words": []},
                {"id": 1, "start": 2.0, "end": 4.0, "text": "deep blue ocean waves", "words": []},
            ],
        }
    }))
    return EDLStore(tmp_path)


def test_search_media_rejects_bad_args(tmp_path: Path):
    store = _store_with_clips(tmp_path, [("a", "color=c=red:s=320x180:d=2")])
    with pytest.raises(ValueError, match="query is empty"):
        dispatch(store, "search_media", {"query": "   "})
    with pytest.raises(ValueError, match="scope must be"):
        dispatch(store, "search_media", {"query": "x", "scope": "weird"})


def test_search_media_spoken_scope(tmp_path: Path):
    store = _store_with_clips(tmp_path, [("a", "color=c=red:s=320x180:d=2")])
    out = dispatch(store, "search_media", {"query": "ocean", "scope": "spoken"})
    assert out["spoken"]["status"] == "ok"
    hits = out["spoken"]["results"]
    assert len(hits) == 1
    assert "ocean" in hits[0]["text"].lower()
    assert hits[0]["start"] == 2.0


def test_search_media_visual_unavailable_is_graceful(tmp_path: Path, monkeypatch):
    # Force the "CLIP not installed" branch and confirm it doesn't raise.
    import video_ai_editor.ai.clip_search as CS
    monkeypatch.setattr(CS, "available", lambda: False)
    store = _store_with_clips(tmp_path, [("a", "color=c=red:s=320x180:d=2")])
    out = dispatch(store, "search_media", {"query": "x", "scope": "visual"})
    assert out["visual"]["status"] == "unavailable"


@pytest.mark.skipif(
    os.environ.get("VAI_RUN_CLIP_TESTS") != "1",
    reason="CLIP model download is heavy; set VAI_RUN_CLIP_TESTS=1 to run",
)
def test_search_media_visual_ranks_correctly(tmp_path: Path):
    store = _store_with_clips(tmp_path, [
        ("sunset", "gradients=s=320x180:d=2:c0=orange:c1=red"),
        ("ocean",  "gradients=s=320x180:d=2:c0=navy:c1=blue"),
        ("forest", "gradients=s=320x180:d=2:c0=darkgreen:c1=green"),
    ])
    out = dispatch(store, "search_media",
                   {"query": "a warm orange sunset", "scope": "visual"})
    assert out["visual"]["status"] == "ok"
    results = out["visual"]["results"]
    assert results, "no visual results"
    assert results[0]["clip_id"] == "sunset", f"got {results[0]['clip_id']}"


def test_search_media_exposed_in_mcp_tools_list(tmp_path: Path, monkeypatch):
    import importlib
    from video_ai_editor import storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    from video_ai_editor import main as _main
    importlib.reload(_main)
    from fastapi.testclient import TestClient
    client = TestClient(_main.app)
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
    names = {t["name"] for t in r["result"]["tools"]}
    assert "search_media" in names
