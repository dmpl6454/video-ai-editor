"""Verify the renderer actually burns text overlays into the output."""
from pathlib import Path
import pytest
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Clip
from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.render import render_export
from video_ai_editor.ingest import ingest_upload
from video_ai_editor.ingest.probe import probe

SAMPLE = Path("/Users/sudhanshu/Downloads/Viral Videos/Outfit Breakdown ft. @wamiqagabbi.mp4")


@pytest.mark.skipif(not SAMPLE.exists(), reason="sample not available")
def test_export_with_hook_and_brand(tmp_path):
    sd = tmp_path / "session"
    sd.mkdir()
    store = EDLStore(sd)

    res = ingest_upload(SAMPLE, sd / "uploads" / SAMPLE.stem, transcribe_audio=False)
    dispatch(store, "add_clip", {
        "track": "v1", "src": res.normalized,
        "in": 0.0, "out": 5.0, "start": 0.0,
    })
    dispatch(store, "apply_brand_kit", {
        "handle": "@quicksolutions.in",
        "hashtags": ["#tech", "#tips"],
    })
    dispatch(store, "add_hook_overlay", {"text": "WAIT FOR IT", "duration": 2.5})

    rep = dispatch(store, "audit_aesthetic", {})
    # Hook is now present, captions warned but not error
    assert all(i["key"] != "hook_missing" for i in rep["issues"])

    out = render_export(store.edl, sd, height=720)
    assert out.path.exists()
    p = probe(out.path)
    assert p.duration > 4.5
