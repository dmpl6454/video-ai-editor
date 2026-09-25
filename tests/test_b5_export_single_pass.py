"""QA-097: an export reports progress from its start and encodes each frame once.

Before: the export first rendered every clip into a cached chunk (encode #1,
no progress, the bar at 0 %), then assembled the chunks (encode #2). Measured
on a 200 s 1080p clip: 0 % for 14.6 s of a 28.0 s job; after: first progress
at 0.7 s of a 13.6 s job. Timelines with very many clips keep the chunk stage
(it bounds decoder memory) and now report progress through it.

Real renders; the encode count is read from the ffmpeg processes actually
started, the progress timing from the job's own callback.
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Canvas, empty_edl
from video_ai_editor.render import compositor, render_export


@pytest.fixture(scope="module")
def src(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("media") / "busy.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=24",
         "-f", "lavfi", "-i", "sine=f=440:duration=24",
         "-vf", "noise=alls=30:allf=t", "-c:v", "libx264", "-preset", "ultrafast",
         "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(p)],
        check=True, capture_output=True)
    return p


def _store(sd: Path, src: Path, n_clips: int) -> EDLStore:
    sd.mkdir(parents=True, exist_ok=True)
    e = empty_edl()
    e.canvas = Canvas(w=1280, h=720, fps=30)
    e.canvas.loudness_lufs = None
    (sd / "edl.json").write_text(e.model_dump_json())
    s = EDLStore(sd)
    step = 24.0 / n_clips
    for i in range(n_clips):
        dispatch(s, "add_clip", {"track": "v1", "src": str(src), "in": i * step,
                                 "out": (i + 1) * step, "start": i * step})
    return s


class _FfmpegSpy:
    """Every ffmpeg the render starts, via subprocess.Popen (subprocess.run
    and render.cancel.run both go through it)."""

    def __init__(self, monkeypatch) -> None:
        self.argvs: list[list[str]] = []
        real = subprocess.Popen
        spy = self

        class _Rec(real):  # type: ignore[misc, valid-type]
            def __init__(self, args, *a, **kw):
                if isinstance(args, (list, tuple)) and args and "ffmpeg" in str(args[0]) \
                        and "ffprobe" not in str(args[0]):
                    spy.argvs.append([str(x) for x in args])
                super().__init__(args, *a, **kw)

        monkeypatch.setattr(subprocess, "Popen", _Rec)

    def video_encodes(self) -> list[list[str]]:
        """ffmpeg runs that ENCODE picture (not copies, not probes)."""
        out = []
        for a in self.argvs:
            if "-c:v" in a and a[a.index("-c:v") + 1] == "copy":
                continue
            if "-vn" in a or a[-1] in ("-", "/dev/null", "NUL"):
                continue   # sound-only renders and the encoder probe
            if any(x in a for x in ("h264_videotoolbox", "libx264", "h264_nvenc",
                                    "h264_qsv", "h264_amf")):
                out.append(a)
        return out


def test_export_encodes_every_frame_once_and_reports_progress_from_the_start(
        tmp_path, src, monkeypatch):
    s = _store(tmp_path / "s", src, n_clips=3)
    spy = _FfmpegSpy(monkeypatch)
    marks: list[tuple[float, float]] = []
    t0 = time.perf_counter()
    res = render_export(s.edl, s.dir, on_progress=lambda p: marks.append(
        (time.perf_counter() - t0, p)), cancel_event=threading.Event())
    total = time.perf_counter() - t0

    encodes = spy.video_encodes()
    assert len(encodes) == 1, (
        f"export encoded picture {len(encodes)} times (chunk + assembly = a "
        f"second generation and ~2x the time)")
    assert not list((s.dir / "cache" / "chunks").glob("chunk_*.mp4")), \
        "a plain export built per-clip chunks it never reuses"
    first = next(t for t, p in marks if p > 0)
    assert first < 0.25 * total, (
        f"first progress at {first:.1f}s of a {total:.1f}s export — the bar sat at 0")
    assert res.path.exists()


def test_many_clip_export_reports_progress_through_the_chunk_stage(
        tmp_path, src, monkeypatch):
    """Above the single-pass clip limit the chunk stage stays, and fills the
    first part of the bar instead of holding it at 0 %."""
    monkeypatch.setattr(compositor, "_EXPORT_SINGLE_PASS_MAX_CLIPS", 2)
    s = _store(tmp_path / "s", src, n_clips=4)
    marks: list[float] = []
    chunk_count_at: list[int] = []
    chunks_dir = s.dir / "cache" / "chunks"

    def prog(p: float) -> None:
        marks.append(p)
        chunk_count_at.append(len(list(chunks_dir.glob("chunk_*.mp4")))
                              if chunks_dir.exists() else 0)

    render_export(s.edl, s.dir, on_progress=prog, cancel_event=threading.Event())
    # Progress arrived while chunks were still being built…
    assert any(p > 0 and n < 4 for p, n in zip(marks, chunk_count_at)), \
        list(zip(marks, chunk_count_at))[:10]
    # …and never ran backwards across the two stages.
    assert all(b >= a - 1e-9 for a, b in zip(marks, marks[1:])), marks
    assert max(marks) > 0.9
