"""ingest.probe answers from a cache keyed on the file's identity (realpath,
size, mtime_ns): a committed edit (trim_clip probes its source twice) no
longer pays two ffprobe runs every time, which kept the client-mode trim
round trip at ~55 ms of the 80 ms §11.1 budget (instant preview 1c)."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from video_ai_editor.ingest import probe as P

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


def _clip(path: Path, seconds: float, rate: int = 30) -> Path:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=size=64x36:rate={rate}:duration={seconds}", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


@pytest.fixture()
def counted(monkeypatch):
    calls: list[list[str]] = []
    real = subprocess.run

    def run(cmd, *a, **k):
        if cmd and str(cmd[0]).endswith("ffprobe"):
            calls.append(list(cmd))
        return real(cmd, *a, **k)

    monkeypatch.setattr(P.subprocess, "run", run)
    P.clear_cache()
    yield calls
    P.clear_cache()


def test_probe_and_extent_run_ffprobe_once_per_file_version(tmp_path, counted):
    f = _clip(tmp_path / "a.mp4", 1.0)
    first = P.probe(f)
    again = P.probe(f)
    assert first.duration == pytest.approx(again.duration) and first.duration == pytest.approx(1.0, abs=0.05)
    assert P.video_frame_extent(f) == pytest.approx(1.0, abs=1e-9)
    assert P.video_frame_extent(f) == pytest.approx(1.0, abs=1e-9)
    assert len(counted) == 2                      # one probe, one extent

    # a caller mutating its answer never changes the next caller's
    again.streams.clear()
    assert P.probe(f).video is not None

    # a new version of the file (other length) is probed afresh
    _clip(tmp_path / "b.mp4", 2.0)
    os.replace(tmp_path / "b.mp4", f)
    later = time.time() + 5
    os.utime(f, (later, later))
    assert P.probe(f).duration == pytest.approx(2.0, abs=0.05)
    assert P.video_frame_extent(f) == pytest.approx(2.0, abs=1e-9)
    assert len(counted) == 4


def test_failures_are_not_cached(tmp_path, counted):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(subprocess.CalledProcessError):
        P.probe(bad)
    assert P.video_frame_extent(bad) is None
    assert P.video_frame_extent(bad) is None
    assert len(counted) == 3                      # every failed attempt asks again
    missing = tmp_path / "missing.mp4"
    assert P.video_frame_extent(missing) is None


def test_trim_clip_dispatch_probes_its_source_once(tmp_path, counted):
    from video_ai_editor.agent.dispatch import dispatch
    from video_ai_editor.edl import EDLStore
    from video_ai_editor.edl.schema import Clip

    src = _clip(tmp_path / "s.mp4", 2.0)
    store = EDLStore(tmp_path / "sess")
    store.edl.get_track("v1").clips.append(Clip(id="c_0", src=str(src), in_=0.0, out=2.0, start=0.0))
    store.commit("seed", {}, "seed")
    cid = "c_0"
    counted.clear()
    for i in range(5):
        dispatch(store, "trim_clip", {"clip_id": cid, "in": 0.1 * (i + 1)})
    assert len(counted) <= 2, counted            # was 2 ffprobe runs per trim
