"""Regression: a chunk left over from an interrupted render must NOT poison
the next render. The validator detects missing-moov-atom files and forces a
rebuild instead of feeding the corrupt mp4 into ffmpeg's filter graph."""
from __future__ import annotations
import subprocess
from pathlib import Path

from video_ai_editor.edl.schema import EDL, Track, Clip, Canvas
from video_ai_editor.edl import EDLStore
from video_ai_editor.render.chunks import (
    chunk_is_valid, chunk_path_for, fingerprint_clip,
)
from video_ai_editor.render import render_preview
from video_ai_editor.agent.dispatch import dispatch


def _make_video(path: Path):
    keyed = path.with_suffix(".keyed.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=2:r=30",
         "-pix_fmt", "yuv420p", str(keyed)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(keyed),
         "-f", "lavfi", "-i", "sine=f=440:duration=2",
         "-c:v", "copy", "-c:a", "aac", "-shortest", str(path)],
        check=True, capture_output=True,
    )


def test_chunk_is_valid_rejects_truncated_mp4(tmp_path: Path):
    good = tmp_path / "good.mp4"
    _make_video(good)
    assert chunk_is_valid(good)

    # Same file truncated to 256 bytes — exactly the failure mode we saw in
    # production (file exists, has bytes, but no moov atom yet).
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(good.read_bytes()[:256])
    assert not chunk_is_valid(bad)

    empty = tmp_path / "empty.mp4"
    empty.touch()
    assert not chunk_is_valid(empty)

    missing = tmp_path / "missing.mp4"
    assert not chunk_is_valid(missing)


def test_render_recovers_from_corrupt_chunk(tmp_path: Path):
    """Plant a corrupt chunk for the V1 clip's expected fingerprint, then run
    render_preview. The validator must drop + rebuild it — not feed it to
    ffmpeg, which would explode with `moov atom not found`."""
    src = tmp_path / "v.mp4"
    _make_video(src)
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(src=str(src), in_=0, out=2, start=0, id="c1"),
        ]),
    ])
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)

    # Plant a poison chunk at the expected fingerprint path
    from video_ai_editor.render.compositor import _video_encoder_args
    enc = _video_encoder_args(preview=True)
    fp = fingerprint_clip(store.edl.tracks[0].clips[0],
                          canvas_w=320, canvas_h=180, fps=30, encoder_args=enc)
    chunks_dir = tmp_path / "cache" / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    poison = chunk_path_for(chunks_dir, fp)
    poison.write_bytes(b"ftyp" + b"\x00" * 200)  # mp4 header but no moov
    assert poison.exists() and poison.stat().st_size > 0
    assert not chunk_is_valid(poison)  # validator agrees it's bad

    # Render must succeed despite the planted poison
    res = render_preview(store.edl, tmp_path, height=180)
    assert res.path.exists() and res.path.stat().st_size > 1024

    # Poison should be replaced by a valid chunk
    assert chunk_is_valid(poison), "validator-rebuilt chunk should be valid"


def test_repair_chunks_purges_corrupt_files(tmp_path: Path):
    src = tmp_path / "v.mp4"
    _make_video(src)
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(src=str(src), in_=0, out=2, start=0, id="c1"),
        ]),
    ])
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)

    chunks_dir = tmp_path / "cache" / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    bad = chunks_dir / "chunk_dead.mp4"
    bad.write_bytes(b"ftyp" + b"\x00" * 200)
    good = chunks_dir / "chunk_good.mp4"
    _make_video(good)

    r = dispatch(store, "repair_chunks", {})
    assert r["scanned"] >= 2
    assert any("chunk_dead.mp4" in s for s in r["removed"])
    assert not any("chunk_good.mp4" in s for s in r["removed"])
    assert not bad.exists()
    assert good.exists()
