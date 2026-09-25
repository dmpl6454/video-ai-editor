"""QA-037: a reversed clip plays backwards in the preview AND the export.

Before: `set_property reverse=true` reported success and nothing in render/
read the field — the export decoded frames 0, 30, 180 at t = 0, 1, 6 s. Now a
reversed clip plays a cached intermediate built in bounded segments
(render/reverse.py). Every assertion decodes what ffmpeg wrote: frame numbers
from a frame-counter source, flash/click positions from a clap track.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from timing_fixtures import av_offsets_ms, decode_frame_numbers, make_clap, make_frame_counter
from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Canvas, Clip, Transform, empty_edl
from video_ai_editor.render import render_export, render_preview
from video_ai_editor.render import reverse as rev

FPS = 30


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("rev_media")
    return {"fc": make_frame_counter(d / "fc30.mp4", frames=600, fps=FPS),
            "clap": make_clap(d / "clap.mp4", seconds=10, fps=FPS)}


def _store(sd: Path) -> EDLStore:
    sd.mkdir(parents=True, exist_ok=True)
    e = empty_edl()
    e.canvas = Canvas(w=320, h=180, fps=FPS)
    e.canvas.loudness_lufs = None
    (sd / "edl.json").write_text(e.model_dump_json())
    return EDLStore(sd)


def _reversed_v1(s: EDLStore, src: Path, a: float, b: float) -> None:
    dispatch(s, "add_clip", {"track": "v1", "src": str(src), "in": a, "out": b, "start": 0.0})
    cid = s.edl.get_track("v1").clips[0].id
    r = dispatch(s, "set_property", {"clip_id": cid, "path": "reverse", "value": True})
    assert s.edl.get_track("v1").clips[0].reverse is True, r


def test_reversed_clip_exports_and_previews_backwards(tmp_path, media, monkeypatch):
    # Small segments, so the intermediate is several reversed pieces joined
    # last-first — the joins are exactly where a frame could be lost or doubled.
    monkeypatch.setattr(rev, "_SEGMENT_BYTES", 320 * 180 * 3 // 2 * 37)
    s = _store(tmp_path / "s")
    _reversed_v1(s, media["fc"], 2.0, 8.0)          # source frames 60..239
    want = list(range(239, 59, -1))
    for out in (render_export(s.edl, s.dir).path, render_preview(s.edl, s.dir).path):
        assert decode_frame_numbers(out) == want, out.name
    inter = list((s.dir / "cache" / "reversed").glob("rev_*.mov"))
    assert len(inter) == 1, "preview and export share one cached intermediate"


def test_a_reversed_clip_keeps_its_speed_and_neighbours(tmp_path, media):
    s = _store(tmp_path / "s")
    dispatch(s, "add_clip", {"track": "v1", "src": str(media["fc"]), "in": 0.0,
                             "out": 1.0, "start": 0.0})
    dispatch(s, "add_clip", {"track": "v1", "src": str(media["fc"]), "in": 10.0,
                             "out": 12.0, "start": 1.0})
    second = s.edl.get_track("v1").clips[1].id
    dispatch(s, "set_property", {"clip_id": second, "path": "reverse", "value": True})
    dispatch(s, "set_speed", {"clip_id": second, "factor": 2.0})
    nums = decode_frame_numbers(render_export(s.edl, s.dir).path)
    assert nums[:30] == list(range(0, 30))                      # forward neighbour
    assert nums[30:] == list(range(359, 299, -2)), nums[30:]    # backwards at 2x


def test_reversed_sound_is_reversed_and_stays_on_the_picture(tmp_path, media):
    """A 4 ms click at the START of each flash frame, reversed, ends where
    that frame ends: every click sits 1/30 s − 4 ms after its flash."""
    s = _store(tmp_path / "s")
    _reversed_v1(s, media["clap"], 0.5, 8.5)
    offs = av_offsets_ms(render_export(s.edl, s.dir).path)
    assert len(offs) >= 7, offs
    assert all(abs(o - (1000 / FPS - 4.0)) < 1.5 for o in offs), offs


def test_a_reversed_pip_plays_backwards(tmp_path, media):
    s = _store(tmp_path / "s")
    s.edl.get_track("v2").clips.append(Clip(
        src=str(media["fc"]), in_=3.0, out=5.0, start=1.0, reverse=True,
        transform=Transform(x=160, y=90, scale=2.86)))
    s.edl.recompute_duration()
    nums = decode_frame_numbers(render_export(s.edl, s.dir).path)
    assert nums[30:90] == list(range(149, 89, -1)), nums[25:95]


def test_nothing_reversed_means_nothing_copied():
    e = empty_edl()
    assert rev.with_reversed_sources(e, None, 30) is e
