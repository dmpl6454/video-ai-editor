"""Caption quality: cue formatting + auto_caption wiring.

caption_format is pure logic (deterministic) so it's fully tested. The
large-v3 auto_caption path is heavy (model load, ~1x realtime) so its live
test is gated behind VAI_RUN_CAPTION_TESTS=1 and skips on CI.
"""
from __future__ import annotations
import os
from pathlib import Path

import pytest

from video_ai_editor.ingest.caption_format import build_cues, cues_from_segments, _wrap_two_lines


def _w(word, s, e):
    return {"word": word, "start": s, "end": e}


def test_wrap_two_lines_balances():
    out = _wrap_two_lines("one two three four five six", max_chars=12)
    assert "\n" in out
    line1, line2 = out.split("\n")
    # Roughly balanced — neither line wildly longer than the other.
    assert abs(len(line1) - len(line2)) <= 8


def test_wrap_keeps_short_single_line():
    assert "\n" not in _wrap_two_lines("short", max_chars=42)


def test_cues_respect_char_budget():
    words = [_w(f"word{i}", i * 0.4, i * 0.4 + 0.4) for i in range(30)]
    cues = build_cues(words, max_chars=20, max_lines=2, max_dur=6.0)
    for c in cues:
        longest_line = max(len(ln) for ln in c.text.split("\n"))
        assert longest_line <= 20 + 6  # small slack for the final word


def test_cues_break_on_sentence_punctuation():
    words = [_w("hello", 0.0, 0.5), _w("world.", 0.5, 1.0),
             _w("next", 1.1, 1.6), _w("one.", 1.6, 2.1)]
    cues = build_cues(words, min_dur=0.1, gap_break=5.0)
    assert len(cues) == 2
    assert cues[0].text.replace("\n", " ") == "hello world."
    assert cues[1].text.replace("\n", " ") == "next one."


def test_cues_break_on_long_pause():
    words = [_w("before", 0.0, 0.5), _w("pause", 0.5, 1.0),
             _w("after", 3.0, 3.5)]  # 2s gap
    cues = build_cues(words, min_dur=0.1, gap_break=0.6)
    assert len(cues) == 2


def test_cues_enforce_min_duration():
    # A single quick word should be stretched to min_dur.
    cues = build_cues([_w("quick", 1.0, 1.1)], min_dur=1.0)
    assert len(cues) == 1
    assert cues[0].end - cues[0].start >= 1.0 - 1e-6


def test_cues_from_segments_synthesizes_missing_word_timing():
    # An imported-style segment with no word list still produces timed cues.
    segs = [{"start": 0.0, "end": 4.0, "text": "alpha beta gamma delta", "words": []}]
    cues = cues_from_segments(segs)
    assert cues
    joined = " ".join(c.text.replace("\n", " ") for c in cues)
    assert "alpha" in joined and "delta" in joined


def test_cues_from_segments_handles_devanagari():
    segs = [{"start": 0.0, "end": 3.0,
             "text": "नमस्कार सभी को आज एक नए सफर पर", "words": []}]
    cues = cues_from_segments(segs, max_chars=20)
    assert cues
    # Devanagari counted by character; should wrap, not crash.
    assert all(c.end > c.start for c in cues)


def test_auto_caption_registered_and_in_tools():
    from video_ai_editor.agent.dispatch import DISPATCH
    from video_ai_editor.agent.tools import ALL_TOOLS
    assert "auto_caption" in DISPATCH
    assert any(t["name"] == "auto_caption" for t in ALL_TOOLS)


def test_auto_caption_requires_v1_clip(tmp_path: Path):
    from video_ai_editor.edl import EDLStore
    from video_ai_editor.edl.schema import EDL, Track, Canvas
    from video_ai_editor.agent.dispatch import dispatch
    edl = EDL(canvas=Canvas(w=1080, h=1920, fps=30),
              tracks=[Track(id="v1", type="video", clips=[])])
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)
    with pytest.raises(ValueError, match="no clip on v1"):
        dispatch(store, "auto_caption", {})


@pytest.mark.skipif(
    os.environ.get("VAI_RUN_CAPTION_TESTS") != "1",
    reason="large-v3 caption run is heavy; set VAI_RUN_CAPTION_TESTS=1",
)
def test_auto_caption_live_hindi(tmp_path: Path):
    import subprocess
    from video_ai_editor.edl import EDLStore
    from video_ai_editor.edl.schema import EDL, Track, Clip, Canvas
    from video_ai_editor.agent.dispatch import dispatch
    src = tmp_path / "hi.mp4"
    # synth a clip with a Hindi TTS-like tone won't transcribe; use a real file
    # if present, else skip.
    real = Path("/Users/sudhanshu/Downloads/https-:www.instagram.com:p:C_CPRMBiROG.mp4")
    if not real.exists():
        pytest.skip("no real Hindi clip available")
    edl = EDL(canvas=Canvas(w=1080, h=1920, fps=30),
              tracks=[Track(id="v1", type="video",
                            clips=[Clip(src=str(real), in_=0, out=15, start=0, id="c1")])])
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)
    r = dispatch(store, "auto_caption", {"style": "ig_chunky", "language": "hi"})
    assert r["language"] == "hi"
    assert r["cues"] >= 3
    cap = store.edl.get_track("captions")
    assert all("�" not in c.text for c in cap.clips), "replacement char leaked"
    assert all(c.end > c.start for c in cap.clips), "zero-duration cue"
