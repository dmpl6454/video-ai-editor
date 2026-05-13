"""Smoke test against one of the user's actual sample clips (small one)."""
import os
from pathlib import Path
import pytest
from video_ai_editor.ingest import ingest_upload

SAMPLE = Path("/Users/sudhanshu/Downloads/Viral Videos/Outfit Breakdown ft. @wamiqagabbi.mp4")


@pytest.mark.skipif(not SAMPLE.exists(), reason="sample not available")
def test_probe_and_normalize_real_video(tmp_path):
    out = tmp_path / "session"
    res = ingest_upload(SAMPLE, out, transcribe_audio=False)
    assert res.probe.duration > 0
    assert res.probe.video is not None
    assert Path(res.normalized).exists()
    assert Path(res.normalized).stat().st_size > 0
    # CFR normalization should produce a vertical-friendly H.264 mp4
    assert res.probe.video.codec_name == "h264"


def test_srt_round_trip():
    from video_ai_editor.ingest.srt_io import import_srt, export_srt
    import tempfile
    sample = """1
00:00:00,000 --> 00:00:02,500
Hello world

2
00:00:02,500 --> 00:00:05,000
This is a test
"""
    with tempfile.NamedTemporaryFile("w", suffix=".srt", delete=False) as f:
        f.write(sample)
        path = Path(f.name)
    t = import_srt(path)
    assert len(t.segments) == 2
    assert t.segments[0].text == "Hello world"
    assert t.segments[1].end == 5.0
    out = export_srt(t)
    assert "Hello world" in out
    assert "00:00:02,500 --> 00:00:05,000" in out
