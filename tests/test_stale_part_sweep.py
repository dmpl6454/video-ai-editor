"""A crash mid-export no longer leaves a hidden .part file (or a runaway
encoder) behind for good (final QA, round 3).

THE DEFECT: an export writes to `.{name}.{pid}.{tid}.part.mp4`
(`compositor._part_path`) and renames it when done. When the backend died
mid-export (Force Quit, an out-of-memory kill, a crash of the in-process app)
the ffmpeg child kept encoding on its own for ~15-25 s, and its 340 MB
.part file stayed in the project for good: dot-named, so Finder hid it,
`/render-cache` reported 0 bytes and "Clear render cache" freed nothing.

THE RULE NOW: a .part file whose writer's PID is dead is stale — counted by
the render-cache readout, removed by "Clear render cache" and swept at
startup — and at startup an encoder still writing one is stopped. A .part
of a LIVE writer (this process's own render in flight) is never touched.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from video_ai_editor import platformutil as _pu
from video_ai_editor.render import cache_budget as CB


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _session(tmp_path: Path) -> Path:
    sd = tmp_path / "wd" / "s_abc123"
    (sd / "exports").mkdir(parents=True)
    (sd / "previews").mkdir()
    return sd


def test_pid_alive():
    assert _pu.pid_alive(os.getpid())
    assert not _pu.pid_alive(_dead_pid())
    assert not _pu.pid_alive(0) and not _pu.pid_alive(-5)


def test_stale_parts_are_counted_cleared_and_swept(tmp_path):
    sd = _session(tmp_path)
    dead = _dead_pid()
    stale = sd / "exports" / f".kill test 1920x1080 30fps q18.{dead}.6207811584.part.mp4"
    stale.write_bytes(b"x" * 5000)
    stale_prev = sd / "previews" / f".abc.{dead}.1.part.mp4"
    stale_prev.write_bytes(b"x" * 700)
    live = sd / "exports" / f".in flight.{os.getpid()}.1.part.mp4"
    live.write_bytes(b"y" * 300)
    export = sd / "exports" / "done 1920x1080.mp4"
    export.write_bytes(b"z" * 100)

    assert set(CB.stale_parts(sd)) == {stale, stale_prev}
    assert CB.usage(sd)["bytes"] == 5700, "the readout counts what Clear can free"
    assert CB.clear(sd) == 5700
    assert not stale.exists() and not stale_prev.exists()
    assert live.exists() and export.exists(), "a live writer's part and an export are never touched"

    stale.write_bytes(b"x" * 10)
    assert CB.sweep_stale_parts(sd.parent) == 10
    assert not stale.exists() and live.exists()


@pytest.mark.skipif(_pu.IS_WINDOWS, reason="orphan encoders are found through ps on POSIX")
def test_an_orphaned_encoder_of_a_dead_backend_is_stopped(tmp_path):
    sd = _session(tmp_path)
    fake = tmp_path / "bin" / "ffmpeg"
    fake.parent.mkdir()
    fake.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    fake.chmod(0o755)
    dead = _dead_pid()
    orphan = subprocess.Popen([str(fake), "-y", str(sd / "exports" / f".a.{dead}.7.part.mp4")])
    mine = subprocess.Popen([str(fake), "-y", str(sd / "exports" / f".b.{os.getpid()}.7.part.mp4")])
    elsewhere = subprocess.Popen([str(fake), "-y", str(tmp_path / f".c.{dead}.7.part.mp4")])
    try:
        time.sleep(0.3)
        stopped = CB.stop_orphan_encoders(sd.parent)
        assert orphan.pid in stopped
        assert orphan.wait(timeout=5) is not None
        assert mine.poll() is None, "an encoder of a live backend keeps running"
        assert elsewhere.poll() is None, "only encoders writing into the workdir are stopped"
    finally:
        for p in (orphan, mine, elsewhere):
            if p.poll() is None:
                p.kill()
                p.wait()


def test_startup_sweeps_the_workdir(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from video_ai_editor import main
    sd = _session(tmp_path)
    stale = sd / "exports" / f".x.{_dead_pid()}.1.part.mp4"
    stale.write_bytes(b"x" * 10)
    monkeypatch.setattr(main, "WORKDIR", sd.parent)
    with TestClient(main.app):
        pass
    assert not stale.exists()
