"""Warm-render assembly fast path.

When every V1 clip has a valid cached chunk and nothing composites on top
(no transitions, no PIP, no baked overlays), the timeline assembly should
concat the chunks with `-c:v copy` instead of re-encoding the whole
timeline — the re-encode made every tiny edit cost ~55ms per timeline
second regardless of edit size. These tests pin (a) the fast path being
taken, (b) the fallback when a baked overlay is present, and (c) the
music-mix variant (video copied, audio re-encoded).
"""
from __future__ import annotations
import json
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.edl.schema import EDL, Track, Clip, Canvas, Sticker
from video_ai_editor.render import render_preview


def _mk_video(path: Path, *, color: str = "blue", freq: int = 440,
              duration: float = 2.0, w: int = 320, h: int = 180):
    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}:d={duration}:r=30",
         "-f", "lavfi", "-i", f"sine=f={freq}:duration={duration}",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True, capture_output=True,
    )


def _mk_audio(path: Path, *, freq: int = 220, duration: float = 2.0):
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=f={freq}:duration={duration}",
         "-c:a", "aac", str(path)],
        check=True, capture_output=True,
    )


def _mk_png(path: Path):
    from PIL import Image
    Image.new("RGBA", (32, 32), (255, 0, 0, 255)).save(path)


def _probe(path: Path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format",
         "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=True,
    )
    return json.loads(proc.stdout)


def _two_clip_edl(a: Path, b: Path) -> EDL:
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(src=str(a), in_=0, out=2, start=0, id="c1"),
            Clip(src=str(b), in_=0, out=2, start=2, id="c2"),
        ]),
    ])
    edl.recompute_duration()
    return edl


class _RunSpy:
    """Record every subprocess.run argv while delegating to the real one."""
    def __init__(self):
        self.calls: list[list[str]] = []
        self._real = subprocess.run

    def __call__(self, args, *a, **kw):
        if isinstance(args, (list, tuple)):
            self.calls.append([str(x) for x in args])
        return self._real(args, *a, **kw)

    def has(self, *needles: str) -> bool:
        """All needles present as exact argv elements of one call."""
        return any(all(n in call for n in needles) for call in self.calls)

    def has_sub(self, needle: str) -> bool:
        """Needle appears as a substring of any argv element of any call."""
        return any(needle in arg for call in self.calls for arg in call)


@pytest.fixture
def run_spy(monkeypatch):
    spy = _RunSpy()
    monkeypatch.setattr(subprocess, "run", spy)
    return spy


def test_warm_visual_edit_uses_streamcopy_assembly(tmp_path: Path, run_spy):
    a = tmp_path / "a.mp4"; b = tmp_path / "b.mp4"
    _mk_video(a, color="blue"); _mk_video(b, color="red", freq=660)
    edl = _two_clip_edl(a, b)

    render_preview(edl, tmp_path, height=180)

    edl.tracks[0].clips[0].transform.scale = 0.8
    run_spy.calls.clear()
    out = render_preview(edl, tmp_path, height=180).path

    assert run_spy.has("concat", "-safe", "copy"), (
        "warm chunk-only assembly did not use the concat-demuxer "
        "stream-copy path"
    )
    info = _probe(out)
    streams = {s["codec_type"] for s in info["streams"]}
    assert streams == {"video", "audio"}
    assert abs(float(info["format"]["duration"]) - 4.0) < 0.3


def test_baked_overlay_disables_streamcopy(tmp_path: Path, run_spy):
    """Stickers are baked server-side even in preview — the fast path must
    yield to the filter_complex assembly so they keep rendering."""
    a = tmp_path / "a.mp4"; b = tmp_path / "b.mp4"; png = tmp_path / "st.png"
    _mk_video(a, color="blue"); _mk_video(b, color="red", freq=660)
    _mk_png(png)
    edl = _two_clip_edl(a, b)
    edl.tracks.append(Track(id="st1", type="sticker", z=5, clips=[
        Sticker(src=str(png), start=0.2, end=1.8, id="s1"),
    ]))
    edl.recompute_duration()

    render_preview(edl, tmp_path, height=180)

    edl.tracks[0].clips[0].transform.scale = 0.8
    run_spy.calls.clear()
    out = render_preview(edl, tmp_path, height=180).path

    assert not run_spy.has("-f", "concat", "-c:v", "copy"), (
        "stream-copy fast path must not run when overlays need baking"
    )
    assert run_spy.has_sub("overlay="), (
        "sticker overlay missing from the assembly render"
    )
    assert {s["codec_type"] for s in _probe(out)["streams"]} == {"video", "audio"}


def test_streamcopy_with_music_reencodes_audio_only(tmp_path: Path, run_spy):
    a = tmp_path / "a.mp4"; b = tmp_path / "b.mp4"; m = tmp_path / "m.m4a"
    _mk_video(a, color="blue"); _mk_video(b, color="red", freq=660)
    _mk_audio(m)
    edl = _two_clip_edl(a, b)
    edl.tracks.append(Track(id="music", type="music", clips=[
        Clip(src=str(m), in_=0, out=2, start=0, id="m1"),
    ]))
    edl.recompute_duration()

    render_preview(edl, tmp_path, height=180)

    edl.tracks[0].clips[0].transform.scale = 0.8
    run_spy.calls.clear()
    out = render_preview(edl, tmp_path, height=180).path

    assert run_spy.has("concat", "copy", "[afinal]"), (
        "music-mix warm edit should copy video and re-encode only audio"
    )
    info = _probe(out)
    assert {s["codec_type"] for s in info["streams"]} == {"video", "audio"}
    assert abs(float(info["format"]["duration"]) - 4.0) < 0.3
