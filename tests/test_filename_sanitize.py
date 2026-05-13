"""Regression: ffmpeg-hostile characters in upload filenames must never reach
the renderer's filter_complex. Two layers of protection:

  1. `_safe_filename` at upload time strips them at the source.
  2. `repair_media_paths` dispatch tool cleans up older sessions whose EDL
     still references hostile-named files.
"""
from __future__ import annotations
import shutil
import subprocess
from pathlib import Path

from video_ai_editor.main import _safe_filename
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Track, Clip, Canvas
from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.render import render_preview


def test_safe_filename_strips_hostile_chars():
    cases = {
        "Lo-fi: 'beats' [chill], #1.mp3": "Lo-fi_beats_chill_1.mp3",
        "video$with(parens)&punct?.mov": "video_with_parens_punct.mov",
        "  spaced  out  .wav": "spaced_out.wav",
    }
    for raw, expected in cases.items():
        assert _safe_filename(raw, "audio.mp3") == expected, raw


def test_safe_filename_handles_non_ascii():
    # Hindi-only name → hash-suffixed fallback (no collisions across calls).
    out = _safe_filename("नमस्ते दोस्तों.mp3", "audio.mp3")
    assert out.startswith("audio_") and out.endswith(".mp3"), out
    # Same input → same hash (deterministic).
    assert out == _safe_filename("नमस्ते दोस्तों.mp3", "audio.mp3")
    # Different inputs → different hashes.
    other = _safe_filename("中文.mp3", "audio.mp3")
    assert out != other


def test_safe_filename_falls_back_for_empty():
    assert _safe_filename(None, "audio.mp3") == "audio.mp3"
    assert _safe_filename("", "audio.mp3") == "audio.mp3"


def test_repair_media_paths_copies_and_rewrites(tmp_path: Path):
    # Build a session with a music clip whose source has hostile chars.
    bad = tmp_path / "Lo-fi: 'beats' [chill].mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=f=200:duration=1",
         "-c:a", "mp3", str(bad)],
        check=True, capture_output=True,
    )
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video"),
        Track(id="music", type="music", clips=[
            Clip(src=str(bad), in_=0, out=1, start=0, id="m1"),
        ]),
    ])
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)
    r = dispatch(store, "repair_media_paths", {})
    assert r["repaired"], r
    new_src = store.edl.tracks[1].clips[0].src
    # New leaf must be hostile-char-free
    new_name = Path(new_src).name
    for ch in [":", "'", "[", "]", ",", " "]:
        assert ch not in new_name, f"{ch!r} survived in {new_name}"
    # Original file is left in place (copy, not move)
    assert bad.exists()
    # New file exists
    assert Path(new_src).exists()


def test_repair_then_render_succeeds(tmp_path: Path):
    """The full point: after repair, the renderer must accept the path."""
    # Build a video + a hostile-named audio file.
    src_v = tmp_path / "v.mp4"
    keyed = tmp_path / "k.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=2:r=30",
         "-pix_fmt", "yuv420p", str(keyed)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(keyed),
         "-f", "lavfi", "-i", "sine=f=440:duration=2",
         "-c:v", "copy", "-c:a", "aac", "-shortest", str(src_v)],
        check=True, capture_output=True,
    )
    bad = tmp_path / "Lo-fi: 'beats' [chill], #1.mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=f=200:duration=2",
         "-c:a", "mp3", str(bad)],
        check=True, capture_output=True,
    )
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(src=str(src_v), in_=0, out=2, start=0, id="c1"),
        ]),
        Track(id="music", type="music", clips=[
            Clip(src=str(bad), in_=0, out=2, start=0, id="m1"),
        ]),
    ])
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)
    dispatch(store, "repair_media_paths", {})
    res = render_preview(store.edl, tmp_path, height=180)
    assert res.path.exists() and res.path.stat().st_size > 0
