"""Cancellation scope for PREVIEW renders (QA-004).

A preview render is superseded the moment the same session asks for a render
of a different EDL: nobody will ever look at the old one. Before this module
the server ran every superseded render to completion, so a burst of edits
queued N full ffmpeg renders behind `_RENDER_SLOTS` and the render the user
was actually waiting for started last.

The scope is a ``ContextVar`` holding a ``threading.Event`` rather than an
argument threaded through every render function, because the ffmpeg calls it
must reach sit four layers down (render_preview -> _render -> chunk pool ->
render_clip_to_chunk) in code several lanes edit. ``run()`` is a drop-in for
the ``subprocess.run`` calls on the preview path:

* no scope active (export, MCP tools, tests calling render_preview directly)
  -> it IS ``subprocess.run``, looked up at call time, so behaviour and any
  test spy on ``subprocess.run`` are unchanged;
* scope active -> the process is polled and terminated as soon as the event
  is set, and ``RenderCancelled`` is raised.

A thread pool does not inherit context variables: a caller fanning work out
to threads must submit ``contextvars.copy_context().run`` (see
chunks.get_or_build_chunks).
"""
from __future__ import annotations

import contextlib
import contextvars
import subprocess
import threading
from typing import Iterator

from .. import platformutil as _pu

_POLL_S = 0.05

_SCOPE: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar(
    "vae_render_cancel", default=None)


class RenderCancelled(Exception):
    """The render was superseded by a newer one for the same session."""


@contextlib.contextmanager
def scope(event: threading.Event) -> Iterator[threading.Event]:
    """Run the enclosed render so that setting `event` aborts it."""
    token = _SCOPE.set(event)
    try:
        yield event
    finally:
        _SCOPE.reset(token)


def current() -> threading.Event | None:
    return _SCOPE.get()


def check() -> None:
    """Raise RenderCancelled if the active scope has been cancelled."""
    ev = _SCOPE.get()
    if ev is not None and ev.is_set():
        raise RenderCancelled()


def acquire(sem: threading.Semaphore) -> None:
    """Acquire `sem`, giving up (RenderCancelled) if the scope is cancelled
    while waiting — a superseded render must not hold its place in the queue."""
    ev = _SCOPE.get()
    if ev is None:
        sem.acquire()
        return
    while not sem.acquire(timeout=_POLL_S):
        if ev.is_set():
            raise RenderCancelled()
    if ev.is_set():
        sem.release()
        raise RenderCancelled()


def wait(event: threading.Event, timeout: float) -> bool:
    """``event.wait(timeout)`` that raises RenderCancelled as soon as the
    active scope is cancelled (a superseded request waiting on another
    request's identical render must not sit out the whole render)."""
    ev = _SCOPE.get()
    if ev is None:
        return event.wait(timeout)
    remaining = float(timeout)
    while remaining > 0:
        if event.wait(min(_POLL_S, remaining)):
            return True
        if ev.is_set():
            raise RenderCancelled()
        remaining -= _POLL_S
    return event.is_set()


class LatestPerSession:
    """One live preview render per session: the newest EDL hash wins.

    ``begin(sid, h)`` returns the cancel event for a render of hash `h`.
    Asking for a DIFFERENT hash than the one in flight sets the older render's
    event (supersedes it); asking for the SAME hash shares its event, so
    concurrent identical requests (which render_preview's _INFLIGHT already
    collapses into one ffmpeg job) live and die together. ``end`` must be
    called once per ``begin``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # sid -> [edl_hash, event, holders]
        self._live: dict[str, list] = {}

    def begin(self, sid: str, edl_hash: str) -> threading.Event:
        with self._lock:
            cur = self._live.get(sid)
            if cur is not None and cur[0] == edl_hash and not cur[1].is_set():
                cur[2] += 1
                return cur[1]
            if cur is not None:
                cur[1].set()
            ev = threading.Event()
            self._live[sid] = [edl_hash, ev, 1]
            return ev

    def end(self, sid: str, ev: threading.Event) -> None:
        with self._lock:
            cur = self._live.get(sid)
            if cur is None or cur[1] is not ev:
                return
            cur[2] -= 1
            if cur[2] <= 0:
                del self._live[sid]


PREVIEWS = LatestPerSession()


def run(args, *, check: bool = False, capture_output: bool = False, **kwargs
        ) -> subprocess.CompletedProcess:
    """``subprocess.run`` that honours the active cancellation scope."""
    ev = _SCOPE.get()
    if ev is None:
        return subprocess.run(args, check=check, capture_output=capture_output,
                              **{**_pu.SUBPROCESS_FLAGS, **kwargs})
    if ev.is_set():
        raise RenderCancelled()
    if capture_output:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    proc = subprocess.Popen(args, **{**_pu.SUBPROCESS_FLAGS, **kwargs})
    while True:
        try:
            out, err = proc.communicate(timeout=_POLL_S)
            break
        except subprocess.TimeoutExpired:
            if ev.is_set():
                # SIGKILL, not SIGTERM: a terminated ffmpeg first flushes its
                # encoder and writes a trailer (measured: up to ~2 s under
                # load), for an output that is about to be deleted anyway —
                # every caller renders to a `.part` file it unlinks on error.
                proc.kill()
                proc.communicate()
                raise RenderCancelled() from None
    cp = subprocess.CompletedProcess(args, proc.returncode, out, err)
    if check:
        cp.check_returncode()
    return cp
