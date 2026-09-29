"""POST /preview?priority=low renders niced (wave D, INSTANT_PREVIEW_SPEC §4.1
step 8, §9.4): with the client engine drawing the edit, the server preview is
background work and must not compete with the UI for CPU.

Measured on the REAL processes: every process the render spawns is sampled
from outside while it runs — its executable name and its kernel nice value
(`os.getpriority`) — so the test proves the ffmpeg that renders is itself
niced, not that some argv carried the word "nice".
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor.api import hardening
from video_ai_editor.main import app
from video_ai_editor.render import cancel as C

from proxy_fixtures import make_barcode_master

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX nice; Windows uses a priority class")


def _proc_name(pid: int) -> str | None:
    if os.path.isdir("/proc"):
        try:
            return Path(f"/proc/{pid}/comm").read_text().strip()
        except OSError:
            return None
    lib = ctypes.CDLL(ctypes.util.find_library("proc") or "libproc.dylib")
    buf = ctypes.create_string_buffer(256)
    n = lib.proc_name(int(pid), buf, 256)
    return buf.value.decode(errors="replace") if n > 0 else None


class _Sampler:
    """Wraps subprocess.Popen: every spawned pid is sampled every ~2 ms for
    (executable name, nice) until it is gone."""

    def __init__(self) -> None:
        self.samples: dict[int, list[tuple[str | None, int]]] = {}
        self.argv: dict[int, list[str]] = {}
        self._threads: list[threading.Thread] = []
        self._real = subprocess.Popen

    def install(self, monkeypatch) -> None:
        sampler = self

        class Watched(self._real):  # type: ignore[misc, valid-type]
            def __init__(self, args, *a, **kw):
                super().__init__(args, *a, **kw)
                sampler._watch(self.pid, list(args) if not isinstance(args, str) else [args])

        monkeypatch.setattr(subprocess, "Popen", Watched)

    def _watch(self, pid: int, argv: list[str]) -> None:
        self.argv[pid] = argv
        rows = self.samples.setdefault(pid, [])

        def loop() -> None:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                # name -> nice -> name: the niced probe is `nice -n 10 ffprobe`,
                # and on Linux `nice` raises its own niceness and THEN exec()s
                # the target. Reading nice first and comm second could pair
                # nice=0 (still the starting `nice` image, or pre-nice) with
                # comm=ffprobe (after the exec) - an impossible pair. A pair is
                # recorded only when both name reads agree, i.e. the process did
                # not change image between them; the nice value is then the one
                # that image really ran at. The LOW comparison is untouched.
                name = _proc_name(pid)
                if name is None:
                    return
                try:
                    nice = os.getpriority(os.PRIO_PROCESS, pid)
                except OSError:
                    return
                if _proc_name(pid) == name:
                    rows.append((name, nice))
                time.sleep(0.002)

        t = threading.Thread(target=loop, daemon=True)
        t.start()
        self._threads.append(t)

    def join(self) -> None:
        for t in self._threads:
            t.join(5)

    def report(self) -> str:
        return "\n".join(f"{sorted({n for _, n in self.samples.get(pid, [])})} {' '.join(map(str, a))[:300]}"
                         for pid, a in self.argv.items())

    def ffmpeg_nices(self) -> list[set[int]]:
        """Per spawned ffmpeg RENDER — one that decodes a timeline input and
        filters or encodes it — the nice values it was seen running at once
        it had exec'd ffmpeg. Not counted (both measured under 0.1 s, and
        both issued by compositor.py outside the cancellable render path):
        the once-per-process encoder capability probes (a lavfi test source
        into `-f null`) and the pure stream copy that files the video-only
        cache entry (`-c:v copy -an`, no filter, no encode)."""
        out = []
        for pid, argv in self.argv.items():
            if not any(os.path.basename(str(a)) in ("ffmpeg", "ffmpeg.exe") for a in argv):
                continue
            text = " ".join(map(str, argv))
            if "-encoders" in argv or ("lavfi" in argv and argv[-2:] == ["null", "-"]):
                continue
            if "-c:v" in argv and argv[argv.index("-c:v") + 1] == "copy" and "-an" in argv \
                    and "-af" not in argv and "-filter_complex" not in text:
                continue
            seen = {nice for name, nice in self.samples.get(pid, []) if name and "ffmpeg" in name}
            if seen:
                out.append(seen)
        return out


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    from video_ai_editor import main as _main, storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    _main._STORES.clear()
    hardening.RATE.windows.clear()
    yield TestClient(app)


def _session(client, tmp_path: Path, seconds: float = 4.0) -> str:
    sid = client.post("/api/sessions").json()["id"]
    frames = int(seconds * 30) + 30
    src = make_barcode_master(tmp_path / sid / "uploads" / "c" / "c.normalized.mp4", frames=frames)
    for tool, args in (("set_canvas", {"w": 640, "h": 360, "fps": 30}),
                       ("add_clip", {"src": str(src), "track": "v1", "start": 0,
                                     "in": 0, "out": seconds / 2}),
                       ("add_clip", {"src": str(src), "track": "v1", "start": seconds / 2,
                                     "in": 0.5, "out": 0.5 + seconds / 2})):
        r = client.post(f"/api/sessions/{sid}/dispatch", json={"tool": tool, "args": args})
        assert r.status_code == 200, r.text
    return sid


if hasattr(os, "getpriority"):
    BASE = os.getpriority(os.PRIO_PROCESS, 0)
    LOW = min(19, BASE + 10) if os.uname().sysname == "Linux" else min(20, BASE + 10)
else:  # Windows: pytestmark skips every test, but the module must still import
    BASE = LOW = 0


def test_priority_low_renders_with_niced_ffmpeg(client, tmp_path, monkeypatch):
    sid = _session(client, tmp_path)
    sampler = _Sampler()
    sampler.install(monkeypatch)
    r = client.post(f"/api/sessions/{sid}/preview", params={"priority": "low"})
    sampler.join()
    assert r.status_code == 200, r.text
    nices = sampler.ffmpeg_nices()
    assert nices, f"no render ffmpeg observed: {sampler.argv}"
    assert all(seen == {LOW} for seen in nices), sampler.report()
    # The server itself was never lowered.
    assert os.getpriority(os.PRIO_PROCESS, 0) == BASE


def test_default_priority_renders_at_the_servers_own_priority(client, tmp_path, monkeypatch):
    sid = _session(client, tmp_path)
    sampler = _Sampler()
    sampler.install(monkeypatch)
    r = client.post(f"/api/sessions/{sid}/preview")
    sampler.join()
    assert r.status_code == 200, r.text
    nices = sampler.ffmpeg_nices()
    assert nices and all(seen == {BASE} for seen in nices), nices


def test_priority_low_job_path_is_niced_too(client, tmp_path, monkeypatch):
    sid = _session(client, tmp_path, seconds=3.0)
    sampler = _Sampler()
    sampler.install(monkeypatch)
    r = client.post(f"/api/sessions/{sid}/preview", params={"priority": "low", "wait": 0})
    assert r.status_code == 202, r.text
    url = r.json()["status_url"]
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        job = client.get(url).json()
        if job["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.05)
    sampler.join()
    assert job["status"] == "completed", job
    nices = sampler.ffmpeg_nices()
    assert nices and all(seen == {LOW} for seen in nices), nices


@pytest.mark.parametrize("value", ["urgent", "LOWEST", "1"])
def test_unknown_priority_is_422_and_renders_nothing(client, tmp_path, value):
    sid = client.post("/api/sessions").json()["id"]
    r = client.post(f"/api/sessions/{sid}/preview", params={"priority": value})
    assert r.status_code == 422
    assert r.json()["error"]["details"]["error"] == "invalid_priority"
    assert not list((tmp_path / sid / "previews").glob("*.mp4"))


def test_priority_normal_and_case_are_accepted(client, tmp_path):
    sid = _session(client, tmp_path, seconds=1.0)
    assert client.post(f"/api/sessions/{sid}/preview", params={"priority": "normal"}).status_code == 200
    assert client.post(f"/api/sessions/{sid}/preview", params={"priority": " Low "}).status_code == 200


def test_low_priority_scope_is_a_context_not_a_global():
    """The scope nices what runs inside it (and threads that copy the
    context), and nothing after it."""
    import contextvars
    from concurrent.futures import ThreadPoolExecutor
    assert not C.is_low_priority()
    with C.low_priority():
        assert C.is_low_priority()
        ctx = contextvars.copy_context()
        with ThreadPoolExecutor(1) as ex:
            assert ex.submit(ctx.run, C.is_low_priority).result() is True
            assert ex.submit(C.is_low_priority).result() is False   # no copy: not inherited
        out = C.run(["sh", "-c", "ps -o nice= -p $$"], capture_output=True, text=True)
        assert int(out.stdout.strip()) == LOW
    assert not C.is_low_priority()
    out = C.run(["sh", "-c", "ps -o nice= -p $$"], capture_output=True, text=True)
    assert int(out.stdout.strip()) == BASE
