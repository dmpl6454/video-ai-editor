"""Silent-source robustness: a video with no audio track must still normalize
and render. Found while testing the .app — a synthetic/screen-recording clip
with no audio crashed the renderer ("Stream specifier ':a' matches no streams")
because the per-clip audio concat assumed every input had an audio stream.
"""
from __future__ import annotations
import subprocess
from pathlib import Path

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Track, Clip, Canvas
from video_ai_editor.ingest.normalize import normalize, _has_audio
from video_ai_editor.render import render_preview


def _silent_clip(path: Path):
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", "color=c=blue:s=320x180:d=2:r=30", "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True,
    )


def test_normalize_injects_silent_audio(tmp_path: Path):
    src = tmp_path / "silent.mp4"
    _silent_clip(src)
    assert _has_audio(src) is False
    dst = tmp_path / "norm.mp4"
    normalize(src, dst)
    assert _has_audio(dst) is True, "normalize must add a silent track"


def test_silent_clip_renders(tmp_path: Path):
    src = tmp_path / "silent.mp4"
    _silent_clip(src)
    norm = tmp_path / "norm.mp4"
    normalize(src, norm)
    edl = EDL(canvas=Canvas(w=1080, h=1920, fps=30),
              tracks=[Track(id="v1", type="video",
                            clips=[Clip(src=str(norm), in_=0, out=2, start=0, id="c1")])])
    edl.recompute_duration()
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)
    res = render_preview(store.edl, tmp_path, height=540)
    assert res.path.exists() and res.path.stat().st_size > 0


def test_has_audio_true_for_clip_with_sound(tmp_path: Path):
    src = tmp_path / "withsound.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=320x180:d=2:r=30",
         "-f", "lavfi", "-i", "sine=f=440:d=2", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src)],
        check=True, capture_output=True,
    )
    assert _has_audio(src) is True
