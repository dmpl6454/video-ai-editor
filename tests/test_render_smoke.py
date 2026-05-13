"""Smoke test the renderer with a real sample."""
from pathlib import Path
import pytest
from video_ai_editor.edl import empty_edl
from video_ai_editor.edl.schema import Clip, Canvas
from video_ai_editor.render import render_preview, render_export
from video_ai_editor.ingest import ingest_upload

SAMPLE = Path("/Users/sudhanshu/Downloads/Viral Videos/Outfit Breakdown ft. @wamiqagabbi.mp4")


@pytest.mark.skipif(not SAMPLE.exists(), reason="sample not available")
def test_render_preview_and_export(tmp_path):
    sess = tmp_path / "session"
    res = ingest_upload(SAMPLE, sess, transcribe_audio=False)
    edl = empty_edl(canvas=Canvas(w=1080, h=1920, fps=30))
    # Take 3s of the source
    edl.tracks[0].clips.append(Clip(src=res.normalized, in_=0.0, out=3.0, start=0.0))
    edl.recompute_duration()
    pv = render_preview(edl, sess)
    assert pv.path.exists() and pv.path.stat().st_size > 0
    pv2 = render_preview(edl, sess)
    assert pv2.cached is True  # second call hits cache


@pytest.mark.skipif(not SAMPLE.exists(), reason="sample not available")
def test_render_concatenates_two_ranges(tmp_path):
    sess = tmp_path / "session"
    res = ingest_upload(SAMPLE, sess, transcribe_audio=False)
    edl = empty_edl()
    # Two non-contiguous slices from the same source; should concat without drift.
    edl.tracks[0].clips.append(Clip(src=res.normalized, in_=0.0, out=2.0, start=0.0))
    edl.tracks[0].clips.append(Clip(src=res.normalized, in_=4.0, out=6.0, start=2.0))
    edl.recompute_duration()
    pv = render_preview(edl, sess)
    # Verify duration is ~4s
    from video_ai_editor.ingest.probe import probe
    out = probe(pv.path)
    assert 3.5 < out.duration < 4.5
