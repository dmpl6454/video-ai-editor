"""QA-041 remainders: a preview render nobody supersedes is still bounded.

Wave A cancelled a preview when a NEWER request superseded it. Two cases were
left running to completion:

* a render nobody supersedes — a mistyped 27-hour timeline, or a stuck
  ffmpeg — now has a wall-clock deadline (`render.cancel.preview_deadline_s`)
  and answers 504 `preview_timed_out`;
* a synchronous preview whose HTTP client disconnects (closed tab/window) is
  abandoned: its ffmpeg is killed and nothing is published.

Real renders, real ffmpeg processes, a real uvicorn socket for the disconnect.
"""
from __future__ import annotations

import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor.api.hardening import RATE
from video_ai_editor.main import app


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch) -> Path:
    from video_ai_editor import main as _main, storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    RATE.windows.clear()
    _main._STORES.clear()
    return tmp_path


@pytest.fixture(scope="module")
def slow_src(tmp_path_factory) -> Path:
    """1080p noise: expensive to decode + scale, so a render takes seconds."""
    p = tmp_path_factory.mktemp("media") / "slow.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30:d=40",
         "-f", "lavfi", "-i", "sine=f=440:duration=40",
         "-vf", "noise=alls=40:allf=t", "-c:v", "libx264", "-preset", "ultrafast",
         "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(p)],
        check=True, capture_output=True)
    return p


class _PopenSpy:
    """Record every Popen the render path starts (render.cancel.run uses
    subprocess.Popen when a scope is active)."""

    def __init__(self, monkeypatch) -> None:
        self.procs: list[subprocess.Popen] = []
        real = subprocess.Popen
        spy = self

        class _Rec(real):  # type: ignore[misc, valid-type]
            def __init__(self, args, *a, **kw):
                super().__init__(args, *a, **kw)
                if args and "ffmpeg" in str(args[0]):
                    spy.procs.append(self)

        monkeypatch.setattr(subprocess, "Popen", _Rec)

    def alive(self) -> list[subprocess.Popen]:
        return [p for p in self.procs if p.poll() is None]


def _session(c: TestClient, src: Path) -> str:
    sid = c.post("/api/sessions", json={"name": "bounds"}).json()["id"]
    r = c.post(f"/api/sessions/{sid}/dispatch", json={
        "tool": "add_clip",
        "args": {"track": "v1", "src": str(src), "in": 0.0, "out": 40.0, "start": 0.0}})
    assert r.status_code == 200, r.text
    # Rotated, so the render has real per-frame work in it.
    edl = c.get(f"/api/sessions/{sid}/edl").json()
    cid = next(t for t in edl["tracks"] if t["id"] == "v1")["clips"][0]["id"]
    r = c.post(f"/api/sessions/{sid}/dispatch", json={
        "tool": "set_clip_transform", "args": {"clip_id": cid, "rotation": 4.0}})
    assert r.status_code == 200, r.text
    return sid


def _leftovers(sd: Path) -> list[Path]:
    return [p for p in sd.rglob("*") if ".part" in p.name]


def test_unsuperseded_preview_hits_its_wall_clock_deadline(workdir, slow_src, monkeypatch):
    """Nobody supersedes this render; the deadline stops it (504), kills its
    ffmpeg and publishes nothing. Pre-fix: it ran to completion (200)."""
    from video_ai_editor.render import cancel as rcancel
    monkeypatch.setattr(rcancel, "DEADLINE_BASE_S", 1.0)
    monkeypatch.setattr(rcancel, "DEADLINE_PER_TIMELINE_S", 0.0)
    spy = _PopenSpy(monkeypatch)
    c = TestClient(app)
    sid = _session(c, slow_src)
    h = c.get(f"/api/sessions/{sid}").json()["summary"]["edl_hash"]

    t0 = time.perf_counter()
    r = c.post(f"/api/sessions/{sid}/preview")
    took = time.perf_counter() - t0

    assert r.status_code == 504, (r.status_code, r.text[:300], f"{took:.1f}s")
    assert r.json()["error"]["details"]["error"] == "preview_timed_out"
    assert took < 6.0, f"deadline of 1 s answered after {took:.1f}s"
    assert spy.procs, "the render never started an ffmpeg"
    assert not spy.alive(), "a timed-out render left ffmpeg running"
    sd = workdir / sid
    assert not (sd / "previews" / f"{h}.mp4").exists()
    assert not _leftovers(sd), _leftovers(sd)


def test_deadline_scales_with_the_timeline():
    from video_ai_editor.render import cancel as rcancel
    assert rcancel.preview_deadline_s(0) == rcancel.DEADLINE_BASE_S
    assert rcancel.preview_deadline_s(100) == pytest.approx(
        rcancel.DEADLINE_BASE_S + 100 * rcancel.DEADLINE_PER_TIMELINE_S)


def test_nested_scope_never_extends_a_deadline():
    from video_ai_editor.render import cancel as rcancel
    ev = threading.Event()
    with rcancel.scope(ev, deadline_s=0.0):
        with rcancel.scope(ev, deadline_s=3600):
            with pytest.raises(rcancel.RenderTimedOut):
                rcancel.check()
    with rcancel.scope(ev):
        rcancel.check()   # no deadline: nothing raised


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(workdir):
    import uvicorn
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           lifespan="off", log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    yield port
    server.should_exit = True
    th.join(timeout=10)


def test_preview_whose_client_disconnects_is_abandoned(workdir, slow_src, live_server,
                                                       monkeypatch):
    """Close the socket mid-render: the ffmpeg dies and no preview or `.part`
    file remains. Pre-fix the render ran on for its full length."""
    spy = _PopenSpy(monkeypatch)
    c = TestClient(app)
    sid = _session(c, slow_src)
    h = c.get(f"/api/sessions/{sid}").json()["summary"]["edl_hash"]

    sock = socket.create_connection(("127.0.0.1", live_server))
    sock.sendall((f"POST /api/sessions/{sid}/preview HTTP/1.1\r\n"
                  f"Host: 127.0.0.1:{live_server}\r\nContent-Length: 0\r\n\r\n").encode())
    deadline = time.monotonic() + 20
    while not spy.alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert spy.alive(), "the preview never started rendering"
    time.sleep(1.0)
    assert spy.alive(), "render finished too fast to observe the disconnect"
    sock.close()                         # the tab closed

    t_close = time.monotonic()
    while spy.alive() and time.monotonic() - t_close < 5:
        time.sleep(0.05)
    assert not spy.alive(), (
        f"ffmpeg still running {time.monotonic() - t_close:.1f}s after the client left")
    # Let the worker unwind, then nothing may have been published.
    time.sleep(0.5)
    sd = workdir / sid
    assert not (sd / "previews" / f"{h}.mp4").exists()
    assert not _leftovers(sd), _leftovers(sd)


def test_disconnect_does_not_cancel_a_render_another_client_shares(workdir):
    """Two requests for the same hash share one render: one leaving must not
    kill it for the other."""
    from video_ai_editor.render.cancel import LatestPerSession
    reg = LatestPerSession()
    a = reg.begin("s_x", "h1")
    b = reg.begin("s_x", "h1")
    assert a is b
    reg.abandon("s_x", a)
    assert not a.is_set(), "one departed client cancelled a shared render"
    reg.end("s_x", a, abandoned=True)
    reg.abandon("s_x", b)
    assert b.is_set(), "the last client leaving must cancel the render"
