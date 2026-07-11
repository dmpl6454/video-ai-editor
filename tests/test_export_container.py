"""Export container option (Task 2b): mp4 vs mov, both real playable files.

Both containers use the identical H.264/AAC encoder args and
`-movflags +faststart` — only the output extension (and therefore the ffmpeg-
inferred muxer / ISO-BMFF major_brand) differs. This test synthesizes a tiny
timeline with ffmpeg lavfi sources (no external sample media required) and
renders it via the real `render_export` entrypoint for both containers, then
verifies each output with ffprobe.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from video_ai_editor import platformutil as _pu
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Canvas, Clip
from video_ai_editor.render import render_export


def _mk_video(path: Path, *, duration: float = 1.0, w: int = 160, h: int = 90) -> None:
    keyed = path.with_suffix(".keyed.mp4")
    subprocess.run(
        [_pu.FFMPEG, "-y", "-f", "lavfi",
         "-i", f"color=c=red:s={w}x{h}:d={duration}:r=30",
         "-pix_fmt", "yuv420p", str(keyed)],
        check=True, capture_output=True,
    )
    subprocess.run(
        [_pu.FFMPEG, "-y", "-i", str(keyed),
         "-f", "lavfi", "-i", f"sine=f=440:duration={duration}",
         "-c:v", "copy", "-c:a", "aac", "-shortest", str(path)],
        check=True, capture_output=True,
    )


def _ffprobe(path: Path) -> dict:
    proc = subprocess.run(
        [_pu.FFPROBE, "-v", "error", "-show_streams", "-show_format",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True, encoding="utf-8", errors="replace",
    )
    return json.loads(proc.stdout)


def _mk_store(tmp_path: Path, src: Path) -> EDLStore:
    store = EDLStore(tmp_path)
    store.edl.canvas = Canvas(w=160, h=90, fps=30)
    store.edl.tracks[0].clips.append(Clip(src=str(src), in_=0.0, out=1.0, start=0.0))
    store.edl.recompute_duration()
    return store


@pytest.mark.parametrize("container,expected_ext", [("mp4", ".mp4"), ("mov", ".mov")])
def test_export_container_produces_valid_playable_file(tmp_path, container, expected_ext):
    src = tmp_path / "src.mp4"
    _mk_video(src)
    store = _mk_store(tmp_path / "session", src)

    res = render_export(store.edl, tmp_path / "session", container=container)

    assert res.path.exists()
    assert res.path.suffix == expected_ext
    assert res.path.stat().st_size > 0

    probe = _ffprobe(res.path)
    fmt = probe["format"]
    # Both mp4 and mov are muxed by ffmpeg's ISO-BMFF "mov" muxer family and
    # both report this same format_name — the container FAMILY is identical,
    # only the file extension / major_brand differ (see plan Task 2b step 3).
    assert fmt["format_name"] == "mov,mp4,m4a,3gp,3g2,mj2"
    assert float(fmt["duration"]) > 0

    codecs = {s["codec_type"]: s["codec_name"] for s in probe["streams"]}
    assert codecs.get("video") == "h264"
    assert codecs.get("audio") == "aac"


def test_export_container_temp_file_matches_final_extension(tmp_path):
    """Regression guard: the in-progress `.part` temp file ffmpeg actually
    writes to must carry the SAME extension as the final destination, or
    ffmpeg (which infers its muxer from the argv output path's extension)
    would mux MOV content into an mp4-tagged temp file that then just gets
    renamed to `.mov` — producing a mislabeled file, not a real MOV."""
    from video_ai_editor.render.compositor import _part_path

    dst_mov = tmp_path / "export_abc123.mov"
    tmp = _part_path(dst_mov)
    assert tmp.suffix == ".mov"

    dst_mp4 = tmp_path / "export_abc123.mp4"
    tmp2 = _part_path(dst_mp4)
    assert tmp2.suffix == ".mp4"


def test_export_default_container_is_mp4(tmp_path):
    src = tmp_path / "src.mp4"
    _mk_video(src)
    store = _mk_store(tmp_path / "session", src)

    res = render_export(store.edl, tmp_path / "session")

    assert res.path.suffix == ".mp4"
