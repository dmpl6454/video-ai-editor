"""Final QA round 2: source <-> timeline mapping on speed-curve, reversed and
freeze clips.

`agent/timemap` mapped with the clip's MEAN speed and ignored `reverse`, so
on a Hero curve a caption for source second 4 landed 1 s early (8 s off on a
reversed clip, cues in forward order), and Remove silences failed with an
internal error ("still map after 1 cuts") or cut real speech around the
silence. The per-clip maps now use the clip's own curve
(`timeline_offset_at` / `source_offset_at`), mirror a reversed clip
(`out - t`), and treat a freeze as playing no source range.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from video_ai_editor.agent import timemap as tm
from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl import speed_curve as sc
from video_ai_editor.edl.schema import EDL, Canvas, Clip, Track

FPS = 30


def _curve_clip(name: str, **kw) -> Clip:
    return Clip(id="c", src="/x.mp4", in_=0.0, out=10.0, start=0.0,
                speed={"curve": sc.CURVE_PRESETS[name]} if name else None, **kw)


@pytest.mark.parametrize("name", ["hero", "montage", "bullet"])
def test_a_curve_clip_maps_through_its_curve_not_its_mean(name):
    c = _curve_clip(name)
    for s in (1.0, 2.0, 4.0, 6.0, 9.0):
        t = tm.clip_source_to_timeline(c, s)
        assert t == pytest.approx(c.timeline_offset_at(s), abs=1e-6), (name, s)
        assert tm.clip_timeline_to_source(c, t) == pytest.approx(s, abs=1e-6)


def test_a_reversed_clip_maps_mirrored():
    c = _curve_clip("", reverse=True)
    assert tm.clip_source_to_timeline(c, 1.0) == pytest.approx(9.0)
    assert tm.clip_source_to_timeline(c, 9.0) == pytest.approx(1.0)
    assert tm.clip_timeline_to_source(c, 2.0) == pytest.approx(8.0)
    rev_curve = _curve_clip("hero", reverse=True)
    assert tm.clip_source_to_timeline(rev_curve, 6.0) == pytest.approx(
        rev_curve.timeline_offset_at(4.0), abs=1e-6)


def _edl(c: Clip) -> EDL:
    edl = EDL(canvas=Canvas(w=160, h=90, fps=FPS, loudness_lufs=None),
              tracks=[Track(id="v1", type="video", clips=[c])])
    edl.recompute_duration()
    return edl


def test_words_on_a_reversed_clip_come_back_ordered_and_forward():
    edl = _edl(_curve_clip("", reverse=True))
    words = [{"word": f"W{i}", "start": float(i), "end": i + 0.5} for i in range(1, 10)]
    out = tm.map_words_to_timeline(edl, "v1", words)
    assert [w["word"] for w in out] == [f"W{i}" for i in range(9, 0, -1)]
    assert all(w["start"] <= w["end"] for w in out)
    assert out[-1]["start"] == pytest.approx(8.5) and out[-1]["end"] == pytest.approx(9.0)
    ranges = tm.source_range_to_timeline(edl, "v1", 3.0, 4.5)
    assert ranges == [pytest.approx((5.5, 7.0))]
    segs = tm.map_segments_to_timeline(edl, "v1", [{"start": 1.0, "end": 2.5, "text": "a b",
                                                     "words": words[:2]}])
    assert segs[0]["start"] < segs[0]["end"]


def test_a_freeze_plays_no_source_range():
    c = Clip(id="f", src="/x.mp4", in_=2.0, out=2.0 + 1 / FPS, start=0.0, freeze=3.0)
    edl = _edl(c)
    assert tm.source_range_to_timeline(edl, "v1", 0.0, 10.0) == []
    assert tm.map_words_to_timeline(edl, "v1", [{"word": "x", "start": 1.9, "end": 2.2}]) == []


# ------------------------------------------------------------ remove_silences

@pytest.fixture(scope="module")
def gappy(tmp_path_factory) -> str:
    """10 s of 440 Hz tone, silent at source 3.0-4.5 s."""
    dst = tmp_path_factory.mktemp("m") / "gappy.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"testsrc2=s=160x90:d=10:r={FPS}",
                    "-f", "lavfi", "-i", "sine=f=440:d=10",
                    "-af", "volume=enable='between(t,3.0,4.5)':volume=0",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(dst)],
                   check=True, capture_output=True)
    return str(dst)


def _store(tmp: Path, src: str) -> EDLStore:
    edl = EDL(canvas=Canvas(w=160, h=90, fps=FPS, loudness_lufs=None), tracks=[
        Track(id="v1", type="video", clips=[Clip(id="c", src=src, in_=0.0, out=10.0, start=0.0)])])
    edl.recompute_duration()
    tmp.mkdir(parents=True)
    (tmp / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(tmp)


@pytest.mark.parametrize("how", ["hero", "montage", "bullet", "reverse"])
def test_remove_silences_on_a_curve_or_reversed_clip_cuts_only_the_silence(tmp_path, gappy, how):
    store = _store(tmp_path / "s", gappy)
    if how == "reverse":
        dispatch(store, "set_clip_reverse", {"clip_id": "c", "reverse": True})
    else:
        dispatch(store, "set_speed", {"clip_id": "c", "preset": how})
    dispatch(store, "remove_silences", {"min_dur": 0.5, "keep_pad": 0})
    pieces = sorted((c.in_, c.out) for c in store.edl.get_track("v1").clips if isinstance(c, Clip))
    assert len(pieces) == 2, pieces
    (a0, a1), (b0, b1) = pieces
    tol = 0.12      # silencedetect's own edge + a frame of grid on a retimed clip
    assert a0 == pytest.approx(0.0, abs=1e-6) and b1 == pytest.approx(10.0, abs=0.05)
    assert a1 == pytest.approx(3.0, abs=tol), pieces
    assert b0 == pytest.approx(4.5, abs=tol), pieces
