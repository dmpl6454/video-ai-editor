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
import os
import subprocess
import threading
import time
from typing import Iterator

from .. import platformutil as _pu

_POLL_S = 0.05

_SCOPE: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar(
    "vae_render_cancel", default=None)
#: Absolute `time.monotonic()` deadline of the active scope, or None (QA-041).
_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "vae_render_deadline", default=None)
#: Wave D (instant preview §4.1 step 8, §9.4): a preview render the client
#: engine does not wait on runs at lower CPU priority. Every ffmpeg that
#: `run()` starts inside `low_priority()` is niced (POSIX `nice -n 10`,
#: Windows BELOW_NORMAL_PRIORITY_CLASS) — the render's own process, never
#: the server's. Thread pools that submit `contextvars.copy_context().run`
#: (chunks.get_or_build_chunks) carry it into their workers.
_LOW_PRIORITY: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "vae_render_low_priority", default=False)


class RenderCancelled(Exception):
    """The render was superseded by a newer one for the same session."""


class RenderTimedOut(RenderCancelled):
    """The render outlived its wall-clock deadline (QA-041).

    A subclass, so every place that already cleans up after a cancelled
    render (unlinking `.part` files, releasing slots) handles it unchanged;
    the HTTP layer tells the two apart, because "a newer edit replaced this"
    and "this render ran far longer than it ever should" are different
    messages to a user."""


#: Wall-clock allowance for a preview render nobody supersedes (QA-041): a
#: fixed floor plus a multiple of the timeline's length. A 540p preview
#: renders many times faster than real time, so 3x real time is only ever
#: reached by an ffmpeg that is stuck or by a timeline far longer than anyone
#: meant (a typo'd start of 100000 s). Overridable for tests and slow boxes.
DEADLINE_BASE_S = float(os.environ.get("VAI_PREVIEW_DEADLINE_BASE_S", "180") or 180)
DEADLINE_PER_TIMELINE_S = float(
    os.environ.get("VAI_PREVIEW_DEADLINE_PER_TIMELINE_S", "3") or 3)


#: Longest timeline the deadline scales with (6 h, the EDL's TIME_RANGE). An
#: EDL written before the model clamped times could still report 1e9 s, which
#: made the deadline ~95 years — only supersede/disconnect could stop it.
DEADLINE_MAX_TIMELINE_S = 6 * 3600.0


def preview_deadline_s(timeline_s: float) -> float:
    """Seconds a preview of a `timeline_s`-long timeline may run for."""
    try:
        t = float(timeline_s or 0.0)
    except (TypeError, ValueError):
        t = 0.0
    if t != t:  # NaN
        t = DEADLINE_MAX_TIMELINE_S
    return DEADLINE_BASE_S + DEADLINE_PER_TIMELINE_S * min(DEADLINE_MAX_TIMELINE_S, max(0.0, t))


@contextlib.contextmanager
def scope(event: threading.Event, *, deadline_s: float | None = None
          ) -> Iterator[threading.Event]:
    """Run the enclosed render so that setting `event` aborts it.

    `deadline_s` (seconds from now) additionally aborts it with
    RenderTimedOut once that much wall-clock time has passed. A nested scope
    never extends an outer deadline, only tightens it."""
    token = _SCOPE.set(event)
    dtoken = None
    if deadline_s is not None:
        new = time.monotonic() + max(0.0, float(deadline_s))
        outer = _DEADLINE.get()
        dtoken = _DEADLINE.set(new if outer is None else min(outer, new))
    try:
        yield event
    finally:
        if dtoken is not None:
            _DEADLINE.reset(dtoken)
        _SCOPE.reset(token)


def current() -> threading.Event | None:
    return _SCOPE.get()


@contextlib.contextmanager
def low_priority(enabled: bool = True) -> Iterator[None]:
    """Run the enclosed render's ffmpeg processes niced (see _LOW_PRIORITY)."""
    token = _LOW_PRIORITY.set(bool(enabled))
    try:
        yield
    finally:
        _LOW_PRIORITY.reset(token)


def is_low_priority() -> bool:
    return _LOW_PRIORITY.get()


def _prioritised(args, kwargs: dict) -> tuple:
    """(argv, Popen kwargs) with the active priority applied."""
    if not _LOW_PRIORITY.get() or isinstance(args, (str, bytes)):
        return args, {**_pu.SUBPROCESS_FLAGS, **kwargs}
    return (_pu.low_priority_argv(list(args)),
            {**_pu.SUBPROCESS_FLAGS, **kwargs, **_pu.LOW_PRIORITY_SUBPROCESS_FLAGS})


def _expired() -> bool:
    dl = _DEADLINE.get()
    return dl is not None and time.monotonic() >= dl


def _raise_if_stopped(ev: threading.Event | None) -> None:
    if ev is not None and ev.is_set():
        raise RenderCancelled()
    if _expired():
        raise RenderTimedOut()


def check() -> None:
    """Raise RenderCancelled if the active scope has been cancelled (or
    RenderTimedOut if its deadline has passed)."""
    _raise_if_stopped(_SCOPE.get())


def acquire(sem: threading.Semaphore) -> None:
    """Acquire `sem`, giving up (RenderCancelled) if the scope is cancelled
    while waiting — a superseded render must not hold its place in the queue."""
    ev = _SCOPE.get()
    if ev is None:
        sem.acquire()
        return
    while not sem.acquire(timeout=_POLL_S):
        _raise_if_stopped(ev)
    try:
        _raise_if_stopped(ev)
    except RenderCancelled:
        sem.release()
        raise


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
        _raise_if_stopped(ev)
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
        # sid -> [edl_hash, event, holders, abandoned]
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
            self._live[sid] = [edl_hash, ev, 1, 0]
            return ev

    def abandon(self, sid: str, ev: threading.Event) -> None:
        """One holder of `ev` stopped waiting (its HTTP client went away —
        QA-041). The render is cancelled only once EVERY holder has abandoned
        it: identical concurrent requests share one render, and a closed tab
        must not kill the render another window is still waiting for. Call
        `end(sid, ev, abandoned=True)` afterwards, as usual."""
        with self._lock:
            cur = self._live.get(sid)
            if cur is None or cur[1] is not ev:
                ev.set()   # no longer the live render: nobody else can want it
                return
            cur[3] += 1
            if cur[3] >= cur[2]:
                ev.set()

    def cancel(self, sid: str) -> bool:
        """Cancel whatever is live under `sid` (every holder's event is set;
        their later `end` calls are no-ops). True if something was live."""
        with self._lock:
            cur = self._live.pop(sid, None)
        if cur is None:
            return False
        cur[1].set()
        return True

    def end(self, sid: str, ev: threading.Event, *, abandoned: bool = False) -> None:
        with self._lock:
            cur = self._live.get(sid)
            if cur is None or cur[1] is not ev:
                return
            cur[2] -= 1
            if abandoned:
                cur[3] = max(0, cur[3] - 1)
            if cur[2] <= 0:
                del self._live[sid]


PREVIEWS = LatestPerSession()

#: Wave D preview proxies (ingest/proxy_queue.py): one live proxy build per
#: SOURCE PATH, keyed by the proxy key (file identity). A source rewritten in
#: place gets a new key, which supersedes — terminates — the build of the
#: old bytes; the same key shares one event, so an on-demand span job and the
#: eager build of the same file live and die together.
PROXIES = LatestPerSession()


def run_prioritised(args, **kwargs) -> subprocess.CompletedProcess:
    """``subprocess.run`` at the ACTIVE PRIORITY (niced inside
    `low_priority()`, like `run`) but outside any cancellation scope.

    For the short probes a render makes along the way whose answers are
    cached (encoder capability, has-audio, container duration) and for the
    stream copy that files a finished preview's video-only twin: killing one
    of those on a superseding edit would cache a wrong answer or lose a
    finished file, yet under ``priority=low`` they must not run at the
    server's own priority either (wave D3, INSTANT_PREVIEW_SPEC §9.4)."""
    argv, kw = _prioritised(args, kwargs)
    return subprocess.run(argv, **{**_pu.SUBPROCESS_FLAGS, **kw})


def run(args, *, check: bool = False, capture_output: bool = False, **kwargs
        ) -> subprocess.CompletedProcess:
    """``subprocess.run`` that honours the active cancellation scope."""
    ev = _SCOPE.get()
    argv, kw = _prioritised(args, kwargs)
    if ev is None:
        return subprocess.run(argv, check=check, capture_output=capture_output,
                              **{**_pu.SUBPROCESS_FLAGS, **kw})
    _raise_if_stopped(ev)
    if capture_output:
        kw["stdout"] = subprocess.PIPE
        kw["stderr"] = subprocess.PIPE
    proc = subprocess.Popen(argv, **{**_pu.SUBPROCESS_FLAGS, **kw})
    while True:
        try:
            out, err = proc.communicate(timeout=_POLL_S)
            break
        except subprocess.TimeoutExpired:
            timed_out = _expired()
            if ev.is_set() or timed_out:
                # SIGKILL, not SIGTERM: a terminated ffmpeg first flushes its
                # encoder and writes a trailer (measured: up to ~2 s under
                # load), for an output that is about to be deleted anyway —
                # every caller renders to a `.part` file it unlinks on error.
                proc.kill()
                proc.communicate()
                if timed_out and not ev.is_set():
                    raise RenderTimedOut() from None
                raise RenderCancelled() from None
    cp = subprocess.CompletedProcess(args, proc.returncode, out, err)
    if check:
        cp.check_returncode()
    return cp
