"""REGRESSION (QA-011 follow-up): whisper.cpp must not collapse Hindi into 'ॐ'.

The QA-011 word-timing change tried `-dtw <preset> -nfa` first on every run.
Turning flash attention off (which DTW requires) makes the `small` model decode
47.4-76.2 s of the benchmark Hindi narration — after the upload normaliser —
as 73 repeated 'ॐ' tokens, so four sentences of real speech vanished and
Hinglish captions read "om om om ..." for 29 s. Plain `-ojf` token times plus
the voicing snap were measured better on English too, so the DTW attempt is
gone.

This drives the REAL pipeline: the upload normaliser writes the file the app
actually transcribes, and the real whisper-cli decodes it. Skips where the
benchmark media, whisper-cli or the ggml `small` model are absent (CI).
"""
from __future__ import annotations

import json
from itertools import groupby
from pathlib import Path

import pytest

from video_ai_editor.ingest import transcribe as T

BENCH = Path("/Users/sudhanshu/Library/Caches/Video AI Editor/bench/8515fa4411c9")
SRC = BENCH / "scene_hi_16x9.mp4"
GT = BENCH / "narration_hi.json"

pytestmark = pytest.mark.skipif(
    not (SRC.exists() and GT.exists()) or not T._whisper_cpp_available()
    or not T._whisper_cpp_model_path("small").exists(),
    reason="needs the benchmark Hindi scene, whisper-cli and ggml-small")


def _voiced_spans() -> list[tuple[float, float]]:
    data = json.loads(GT.read_text())
    return [(u["voiced_start"], u["voiced_end"]) for u in data["utterances"]
            if u["kind"] == "speech" and u.get("voiced_start") is not None]


def _covered(spans, segs) -> float:
    """Fraction of ground-truth voiced speech time lying inside a segment
    whose text is real speech (not a single repeated token)."""
    total = hit = 0.0
    for a, b in spans:
        total += b - a
        for s0, s1 in segs:
            hit += max(0.0, min(b, s1) - max(a, s0))
    return hit / total


@pytest.fixture(scope="module")
def transcript(tmp_path_factory):
    from video_ai_editor.ingest.normalize import normalize
    dst = tmp_path_factory.mktemp("hi") / "scene_hi_16x9.normalized.mp4"
    normalize(SRC, dst)
    return T._transcribe_via_whisper_cpp(dst, None, "small")


def test_no_token_repeats_more_than_five_times_in_a_row(transcript):
    tokens = [t for seg in transcript.segments for t in seg.text.split()]
    worst = max((len(list(g)) for _, g in groupby(tokens)), default=0)
    assert worst <= 5, (worst, [s.text[:60] for s in transcript.segments])
    assert sum(t.count("ॐ") for t in tokens) == 0


def test_every_sentence_of_speech_is_transcribed(transcript):
    def degenerate(text: str) -> bool:
        toks = text.split()
        return len(toks) >= 8 and max(toks.count(t) for t in set(toks)) > len(toks) / 2

    segs = [(s.start, s.end) for s in transcript.segments if not degenerate(s.text)]
    cov = _covered(_voiced_spans(), segs)
    assert cov >= 0.95, (round(cov, 3), [(round(a, 2), round(b, 2)) for a, b in segs])
