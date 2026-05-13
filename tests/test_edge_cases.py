"""Edge cases + stress: empty EDL, broken upload, concurrent renders,
huge filenames, special characters, oversized inputs."""
from __future__ import annotations
import concurrent.futures
import subprocess
import threading
from pathlib import Path

import pytest

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Track, Clip, Canvas, empty_edl
from video_ai_editor.render import render_preview


def _mk(p: Path, dur: float = 1.0):
    keyed = p.with_suffix(".keyed.mp4")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                    "-i", f"color=c=blue:s=320x180:d={dur}:r=30",
                    "-pix_fmt", "yuv420p", str(keyed)],
                   check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-i", str(keyed),
                    "-f", "lavfi", "-i", f"sine=f=440:duration={dur}",
                    "-c:v", "copy", "-c:a", "aac", "-shortest", str(p)],
                   check=True, capture_output=True)


def test_render_empty_edl_returns_black_with_silence(tmp_path: Path):
    """Rendering with no V1 clips should produce a black video with silence,
    not crash — supports the 'add hook overlay before any media' UX."""
    edl = empty_edl()
    edl.duration = 1.0
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)
    res = render_preview(store.edl, tmp_path, height=180)
    assert res.path.exists() and res.path.stat().st_size > 1024


def test_render_with_corrupt_source_raises_clean_error(tmp_path: Path):
    """A source file referenced in the EDL but corrupt should raise a
    RuntimeError with the ffmpeg stderr — not a generic exception with no
    context."""
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a real mp4")
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(src=str(bad), in_=0, out=1, start=0, id="c1"),
        ]),
    ])
    edl.recompute_duration()
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)
    with pytest.raises(RuntimeError) as exc_info:
        render_preview(store.edl, tmp_path, height=180)
    assert "ffmpeg" in str(exc_info.value).lower() or "moov" in str(exc_info.value).lower()


def test_concurrent_renders_dedup_to_one_ffmpeg(tmp_path: Path):
    """Two callers asking for the same EDL preview at the same time should
    share one ffmpeg invocation (the in-flight dedup table)."""
    src = tmp_path / "src.mp4"; _mk(src, dur=2.0)
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(src=str(src), in_=0, out=2, start=0, id="c1"),
        ]),
    ])
    edl.recompute_duration()
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)

    # First clear any cached preview so both threads must actually render.
    for p in (tmp_path / "previews").glob("*.mp4"):
        p.unlink()

    results: list = []
    err: list = []

    def _run():
        try:
            results.append(render_preview(store.edl, tmp_path, height=180))
        except Exception as e:
            err.append(e)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: _run(), range(4)))

    assert not err, f"errors: {err}"
    assert len(results) == 4
    # All results point at the same cached file
    paths = {str(r.path) for r in results}
    assert len(paths) == 1, paths


def test_very_long_filename_is_handled(tmp_path: Path):
    """A filename approaching POSIX NAME_MAX (255 bytes) still uploads + edits."""
    long_name = ("a" * 200) + ".mp4"  # well under 255
    src = tmp_path / long_name
    _mk(src, dur=1.0)
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(src=str(src), in_=0, out=1, start=0, id="c1"),
        ]),
    ])
    edl.recompute_duration()
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)
    res = render_preview(store.edl, tmp_path, height=180)
    assert res.path.exists()


def test_filename_with_spaces_and_unicode(tmp_path: Path):
    """Unicode + spaces in filenames must work after sanitisation. Tests the
    full upload sanitisation path that the user hit in production."""
    from video_ai_editor.main import _safe_filename
    raw = "  Hello World — émoji 🎬.mp4  "
    sanitized = _safe_filename(raw, "video.mp4")
    assert " " not in sanitized
    assert ":" not in sanitized
    assert "'" not in sanitized
    src = tmp_path / sanitized
    _mk(src, dur=1.0)
    assert src.exists()


def test_split_at_boundary_doesnt_create_zero_length_clips(tmp_path: Path):
    """Splitting EXACTLY at clip boundaries must not produce a 0-length clip."""
    src = tmp_path / "src.mp4"; _mk(src, dur=2.0)
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(src=str(src), in_=0, out=2, start=0, id="c1"),
        ]),
    ])
    edl.recompute_duration()
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)
    from video_ai_editor.agent.dispatch import dispatch
    # Split AT the start (t=0) — should be a no-op or skipped gracefully.
    dispatch(store, "split_at", {"time": 0.0})
    # Split AT the end (t=2.0) — same.
    dispatch(store, "split_at", {"time": 2.0})
    # All resulting clips should have duration > 0.
    for c in store.edl.tracks[0].clips:
        if hasattr(c, "duration"):
            assert c.duration > 0, f"zero-length clip: {c}"


def test_negative_times_are_rejected_or_clamped(tmp_path: Path):
    """move_clip with negative timestamp should either reject or clamp to 0."""
    src = tmp_path / "src.mp4"; _mk(src, dur=2.0)
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(src=str(src), in_=0, out=2, start=0, id="c1"),
        ]),
    ])
    edl.recompute_duration()
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)
    from video_ai_editor.agent.dispatch import dispatch
    try:
        dispatch(store, "move_clip", {"clip_id": "c1", "new_start": -5.0})
    except (ValueError, RuntimeError):
        pass
    # Either way: clip.start should not be negative.
    assert store.edl.tracks[0].clips[0].start >= 0
