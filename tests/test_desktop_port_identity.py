"""The launcher must never drive a backend that is not its own.

The notarized 0.5.0 DMG shipped with this hole and it produced a report that
looked like something else entirely ("no save dialog appears on export"). A dev
`uvicorn` was holding port 8765, so the packaged app's own uvicorn exited
(SystemExit(1), recorded in its log) — but `_wait_for_server` accepted ANY 200
on `/api/health`, the foreign server answered instantly and won the race
against the crash being recorded, and the window opened against a stranger's
editor. Every session, render and export then went to the OTHER process's
workdir, so `save_export` looked in this process's `exports/` dir, found
nothing, returned None, and `store.ts` rendered that as "user cancelled": total
silence.

Two mechanisms are pinned here, and they are deliberately different in kind:

* `_server_is_ours()` — the identity gate. `uvicorn.Server.started` flips True
  only once OUR socket is listening, and a TCP listen is exclusive, so it is a
  proof rather than an echo (an identity *token* would need `main.py` to grow a
  route to reflect it back). This is the guarantee: it closes the race even when
  the port is grabbed between the probe and the bind.
* `_port_in_use()` — the pre-bind probe. Cheap, and it turns "the backend exited
  with code 1" into a sentence naming the port before any window appears.

The chosen response is REFUSE, not "bind a free port instead". WORKDIR is
per-user, not per-port: two copies of this app would read-modify-write the same
`edl.json` and snapshots with no cross-process lock. Refusing keeps the second
copy from corrupting the first one's projects.
"""
from __future__ import annotations

import socket
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from video_ai_editor import desktop


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

class _OkHandler(BaseHTTPRequestHandler):
    """A stranger on the port that answers /api/health with a perfect 200."""

    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler's own spelling)
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # keep pytest output clean
        pass


@pytest.fixture
def foreign_backend():
    """A real HTTP server on a real port — the thing the app must not adopt."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _OkHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


class _FakeServer:
    """Stands in for uvicorn.Server; only `started` is read."""

    def __init__(self, started: bool) -> None:
        self.started = started


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------
# the identity gate
# --------------------------------------------------------------------------

def test_a_foreign_200_is_not_adopted(monkeypatch, foreign_backend):
    """The shipped bug, reproduced and then closed.

    The `owned=None` half of this assertion is what makes it a real test: it
    proves the foreign server genuinely answers 200, so the gated call
    returning False is the gate working — not a server that was never up.
    """
    monkeypatch.setattr(desktop, "_SERVER", None)
    url = f"http://127.0.0.1:{foreign_backend}/api/health"
    assert desktop._wait_for_server(url, timeout=0.5) is True
    assert desktop._wait_for_server(
        url, timeout=0.5, owned=desktop._server_is_ours) is False


def test_our_own_backend_is_adopted_immediately(monkeypatch, foreign_backend):
    """The gate must not cost the normal path anything."""
    monkeypatch.setattr(desktop, "_SERVER", _FakeServer(started=True))
    url = f"http://127.0.0.1:{foreign_backend}/api/health"
    t0 = time.time()
    assert desktop._wait_for_server(
        url, timeout=5.0, owned=desktop._server_is_ours) is True
    assert time.time() - t0 < 1.0


def test_server_is_ours_reads_the_bind_not_the_object(monkeypatch):
    """`started` is the signal, because it flips only once the socket listens."""
    monkeypatch.setattr(desktop, "_SERVER", None)
    assert desktop._server_is_ours() is False
    monkeypatch.setattr(desktop, "_SERVER", _FakeServer(started=False))
    assert desktop._server_is_ours() is False
    monkeypatch.setattr(desktop, "_SERVER", _FakeServer(started=True))
    assert desktop._server_is_ours() is True


def test_an_unowned_200_does_not_spin_hot(monkeypatch, foreign_backend):
    """The sleep used to live in the `except` arm only, so a server that
    answers instantly with a 200 we refuse would busy-loop a core until the
    timeout."""
    monkeypatch.setattr(desktop, "_SERVER", None)
    url = f"http://127.0.0.1:{foreign_backend}/api/health"
    before = time.process_time()
    desktop._wait_for_server(url, timeout=1.0, owned=desktop._server_is_ours)
    assert time.process_time() - before < 0.5


def test_the_handoff_polls_with_the_identity_gate(monkeypatch):
    """Wiring, not logic: the gate is worthless if a call site forgets it, and
    both of `_open_editor_when_ready`'s polls run against the same port."""
    monkeypatch.setattr(desktop, "_SERVER_ERROR", None)
    monkeypatch.setattr(desktop, "_LATE_RETRY_EVERY_S", 0.01)
    monkeypatch.setattr(desktop, "_LATE_RETRY_FOR_S", 0.05)
    seen: list[object] = []

    def _record(url, timeout=15.0, abort=None, owned=None):
        seen.append(owned)
        return False

    monkeypatch.setattr(desktop, "_wait_for_server", _record)

    class _Win:
        def load_url(self, url): pass
        def load_html(self, html): pass

    desktop._open_editor_when_ready(_Win(), "http://127.0.0.1:8765",
                                    "http://127.0.0.1:8765/api/health", 0.1)
    assert seen, "no poll happened at all"
    assert all(o is desktop._server_is_ours for o in seen), seen


# --------------------------------------------------------------------------
# the pre-bind probe
# --------------------------------------------------------------------------

def test_probe_sees_a_real_listener():
    port = _free_port()
    assert desktop._port_in_use("127.0.0.1", port) is False
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(5)
    try:
        assert desktop._port_in_use("127.0.0.1", port) is True
    finally:
        srv.close()


def test_a_time_wait_socket_from_the_previous_run_is_not_a_conflict():
    """The false-refusal trap this probe would otherwise walk into.

    Quitting the app leaves its own connections in TIME_WAIT with THIS port as
    their local port, and a bind without SO_REUSEADDR is then refused with the
    same EADDRINUSE a live listener gives (verified: the plain bind below
    fails). asyncio sets SO_REUSEADDR for a listener on POSIX, so uvicorn would
    bind fine — a probe that skipped the option would tell the user "another
    copy is already running" every time they relaunched promptly.
    """
    if desktop._pu.IS_WINDOWS:
        pytest.skip("SO_REUSEADDR is deliberately not set on Windows")
    port = _free_port()
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(5)
    cli = socket.create_connection(("127.0.0.1", port))
    conn, _ = srv.accept()
    conn.close()          # the server side closes first -> it TIME_WAITs
    cli.close()
    srv.close()
    # The plain bind IS the detector for "did the kernel leave something on
    # this port", so the test needs no external tool (`netstat`/`ss` are not
    # installed on every CI image) and cannot go vacuous: either it is refused,
    # which is the state we want to prove the probe survives, or there is
    # nothing lingering and there is nothing to test.
    plain = socket.socket()
    try:
        plain.bind(("127.0.0.1", port))
    except OSError as e:
        assert e.errno in desktop._BUSY_ERRNOS, e
    else:
        pytest.skip("the kernel left no lingering socket to test against")
    finally:
        plain.close()
    assert desktop._port_in_use("127.0.0.1", port) is False


def test_a_question_the_probe_cannot_answer_is_not_a_conflict():
    """Anything but EADDRINUSE means "cannot tell", and the caller must then
    behave exactly as it did before the probe existed rather than refuse."""
    assert desktop._port_in_use("no-such-host.invalid", 8765) is False


def test_the_conflict_message_names_the_port_and_the_url():
    port = _free_port()
    assert desktop._port_conflict_message(
        "127.0.0.1", port, f"http://127.0.0.1:{port}") is None
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(5)
    try:
        msg = desktop._port_conflict_message(
            "127.0.0.1", port, f"http://127.0.0.1:{port}")
    finally:
        srv.close()
    assert msg and str(port) in msg and f"http://127.0.0.1:{port}" in msg


# --------------------------------------------------------------------------
# main() — refuse, explain, and do not start a second backend
# --------------------------------------------------------------------------

class _FakeWindow:
    def __init__(self, **kw):
        self.kw = kw
        self.events = types.SimpleNamespace(loaded=[])


def _stub_webview(monkeypatch):
    """Install a pywebview stand-in and return the created-window recorder."""
    made: list[_FakeWindow] = []
    mod = types.ModuleType("webview")

    def create_window(**kw):
        w = _FakeWindow(**kw)
        made.append(w)
        return w

    mod.create_window = create_window          # type: ignore[attr-defined]
    mod.start = lambda *a, **k: None           # type: ignore[attr-defined]
    mod.FileDialog = types.SimpleNamespace(SAVE="save")  # type: ignore[attr-defined]
    mod.windows = []                           # type: ignore[attr-defined]
    # Real pywebview exposes a `settings` mapping, and `main()` sets
    # ALLOW_DOWNLOADS on it before creating the window (without it every
    # `<a download>` in the packaged app is inert). A stub lacking it made
    # main() take its defensive except-branch instead of the real path.
    mod.settings = {"ALLOW_DOWNLOADS": False}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "webview", mod)
    return made


def test_main_refuses_and_explains_when_the_port_is_taken(monkeypatch):
    """No silent adoption, no splash to sit through, and no second backend."""
    port = _free_port()
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(5)
    monkeypatch.setattr(desktop, "_ensure_frontend_built", lambda: None)
    monkeypatch.setattr(desktop, "_SERVER_ERROR", None)
    monkeypatch.setattr(desktop, "_SERVER", None)
    monkeypatch.setenv("VAE_PORT", str(port))
    monkeypatch.setattr(desktop, "_diag", lambda m: None)
    served: list[tuple] = []
    monkeypatch.setattr(desktop, "_serve", lambda *a: served.append(a))
    # A poll must not even be attempted: the port's owner would answer it.
    monkeypatch.setattr(desktop, "_wait_for_server",
                        lambda *a, **k: pytest.fail("polled a foreign backend"))
    made = _stub_webview(monkeypatch)
    try:
        desktop.main()
    finally:
        srv.close()
    assert served == [], "started a backend that cannot possibly bind"
    assert len(made) == 1
    win = made[0]
    assert win.kw["url"] is None, "opened the editor against the stranger"
    html = win.kw["html"]
    assert str(port) in html
    assert "quit it" in html                    # the actionable sentence
    assert "Still checking" not in html         # nothing will change on its own
    assert desktop._SERVER_ERROR and str(port) in desktop._SERVER_ERROR


def test_main_starts_the_backend_and_gates_the_poll_when_the_port_is_free(monkeypatch):
    """The normal path, unchanged — plus the gate at main()'s own call site."""
    port = _free_port()
    monkeypatch.setattr(desktop, "_ensure_frontend_built", lambda: None)
    monkeypatch.setattr(desktop, "_SERVER_ERROR", None)
    monkeypatch.setattr(desktop, "_SERVER", None)
    monkeypatch.setenv("VAE_PORT", str(port))
    started = threading.Event()
    monkeypatch.setattr(desktop, "_serve", lambda *a: started.set())
    seen: list[object] = []

    def _record(url, timeout=15.0, abort=None, owned=None):
        seen.append(owned)
        return True                      # pretend the backend is already warm

    monkeypatch.setattr(desktop, "_wait_for_server", _record)
    made = _stub_webview(monkeypatch)
    desktop.main()
    assert started.wait(2.0), "the backend thread never ran"
    assert seen == [desktop._server_is_ours]
    assert made[0].kw["url"] == f"http://127.0.0.1:{port}"
    assert made[0].kw["html"] is None
