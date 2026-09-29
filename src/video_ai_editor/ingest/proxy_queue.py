"""Scheduling for preview proxies: eager background builds, on-demand spans.

Spec: docs/design/INSTANT_PREVIEW_SPEC.md §4.4, §5.1 "Scheduling", §11.4.

  * EAGER. ``ensure(src)`` queues a build of the whole proxy at normal
    priority: probe → FLAC chunks (fast, first) → every missing span, in
    order, as ONE streaming encode per contiguous run of missing spans, each
    span written the moment its frames are out. At most ``MAX_EAGER`` eager
    builds run at once, so a worker slot is always free for on-demand work.
  * ON DEMAND. ``request_span(key, n)`` returns span n if it is on disk, and
    otherwise runs a high-priority job for spans ``[n, n + ONDEMAND_SPANS)``
    (an all-intra span needs no earlier frame, so it can be encoded
    independently of everything else) and waits up to ``timeout``. The first
    span of a fresh import is ready in a few hundred ms.
  * NICED. Every encode runs at nice 10 (``platformutil.low_priority_argv``),
    at most ``MAX_WORKERS`` at once.
  * PAUSED DURING EXPORT. ``export_in_progress()`` (main.py wraps the export
    render in it) stops eager encodes — the running one is killed and
    re-queued; it resumes from its first missing span afterwards, so nothing
    already written is redone. On-demand spans still run: they are what the
    user is looking at.
  * RETRIED. An ffmpeg that leaves before its frames are out — killed under
    memory pressure, the pipe closed in the middle of a box — is not a
    failure yet: ``proxy.encode_spans`` starts it again from the first span
    it did not hand out, ``proxy.ENCODE_RETRIES`` times with backoff, and
    only then marks the proxy failed with the exit code and ffmpeg's last
    words (the route's 410 message). A cancel or an export pause is never
    retried (Final QA r4: the early EOF escaped the job as ``EOFError`` and
    the span was not built until something asked for it again). Any other
    exception out of a job — EIO from the span writer, ``FileNotFoundError``
    from Popen when ffmpeg is gone — marks the proxy failed as well (final
    sweep 4: the worker only logged it, the index stayed pending and every
    poll of the span route queued the same doomed encode); ENOSPC alone is
    a hold, not a failure.
  * CANCELLABLE. Jobs of one source share a ``render.cancel.PROXIES`` scope
    keyed by the source's realpath; a new key for the same path (the file
    was rewritten) supersedes the old build. ``cancel(src)`` stops it.
  * BOUNDED. After each build the proxies LRU class
    (``render.cache_budget.enforce_proxies``) trims span files.

Worker threads are daemons started lazily on first use, so importing this
module (every test that imports main.py does) starts nothing.
"""
from __future__ import annotations

import contextlib
import errno
import heapq
import itertools
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from . import proxy as P
from ..render import cancel as _cancel

_log = logging.getLogger("video_ai_editor")

MAX_WORKERS = 2
MAX_EAGER = 1
ONDEMAND_SPANS = 2
PRIORITY_URGENT = 0
PRIORITY_NORMAL = 10
#: After a job of a key failed because the disk is full (ENOSPC), requests
#: for its spans / audio answer "disk full" at once for this long instead of
#: queueing another encode (Final QA r2: every 202 poll started a fresh full
#: span encode that failed on write — 102 failed jobs in two minutes — and
#: the route said "pending" for good, so the engine never degraded).
DISK_FULL_HOLD_S = 10.0


def _is_disk_full(e: BaseException | None) -> bool:
    """ENOSPC anywhere in the exception chain (or in an ffmpeg message)."""
    seen = 0
    while e is not None and seen < 8:
        if isinstance(e, OSError) and e.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)):
            return True
        if "No space left on device" in str(e):
            return True
        e = e.__cause__ or e.__context__
        seen += 1
    return False


@dataclass(order=True)
class _Job:
    priority: int
    seq: int
    kind: str = field(compare=False)            # "eager" | "spans" | "audio"
    key: str = field(compare=False)
    src: str = field(compare=False)
    spans: tuple[int, ...] = field(compare=False, default=())
    done: threading.Event = field(compare=False, default_factory=threading.Event)
    error: str | None = field(compare=False, default=None)


class ProxyManager:
    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._heap: list[_Job] = []
        self._seq = itertools.count()
        self._workers: list[threading.Thread] = []
        self._eager_running = 0
        self._eager_keys: set[str] = set()          # queued or running
        self._span_jobs: dict[tuple[str, int], _Job] = {}
        self._audio_jobs: dict[str, _Job] = {}
        self._probe_locks: dict[str, threading.Lock] = {}
        self._exports = 0
        self._export_event = threading.Event()      # set while an export runs
        self._gen = 0                               # worker generation
        self._busy = 0
        self._disk_full: dict[str, float] = {}      # key → monotonic time of the ENOSPC
        self.stats = {"eager_builds": 0, "span_jobs": 0, "failures": 0, "paused": 0, "disk_full": 0}

    # ---- public API -------------------------------------------------------------

    def ensure(self, src: str | os.PathLike, *, sid: str | None = None,
               eager: bool = True, probe_timeout: float = 0.0) -> str:
        """Key of ``src``'s proxy; records ``sid`` as a referencing session.
        With ``eager`` the whole proxy is queued (a no-op when complete or
        already queued). ``probe_timeout`` > 0 probes synchronously first
        (bounded), so the caller can answer with the index right away."""
        real = os.path.realpath(os.fspath(src))
        key = P.proxy_key(real)
        P.proxy_dir(key).mkdir(parents=True, exist_ok=True)
        if sid:
            P.add_ref(key, sid)
        if probe_timeout > 0:
            self._probe_bounded(key, real, probe_timeout)
        if eager:
            self._queue_eager(key, real)
        return key

    def info(self, key: str, src: str | None = None) -> P.SourceInfo | None:
        """The probed SourceInfo of ``key`` (probing now if ``src`` given)."""
        info = P.load_source(key)
        if info is not None or src is None:
            return info
        return self._probe(key, src)

    def disk_full(self, key: str) -> bool:
        """A job of ``key`` failed for a full disk within DISK_FULL_HOLD_S."""
        at = self._disk_full.get(key)
        if at is None:
            return False
        if time.monotonic() - at < DISK_FULL_HOLD_S:
            return True
        self._disk_full.pop(key, None)
        return False

    def request_span(self, key: str, n: int, *, timeout: float = 2.0) -> Path | None:
        """Path of span ``n`` once it exists; None if not ready in ``timeout``
        (or while its key is held for a full disk — see ``disk_full``)."""
        p = P.span_path(key, n)
        if p.is_file():
            return p
        info = P.load_source(key)
        if info is None or not (0 <= n < info.spans) or self._failed(key) or self.disk_full(key):
            return None
        job = self._queue_spans(key, info, n)
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cv:
            while not p.is_file() and not job.done.is_set():
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cv.wait(min(left, 0.1))
        return p if p.is_file() else None

    def request_audio(self, key: str, *, chunk: int | None = None,
                      timeout: float = 2.0) -> bool:
        """True once the FLAC chunks exist — or, with ``chunk``, as soon as
        THAT chunk is on disk (chunks are written in order and atomically,
        so chunk 0 of a long source is servable long before the last one;
        spec §4.4/§11.1). Also True when the audio is complete, which is how
        an out-of-range ``chunk`` is told apart from a slow one. Queues an
        URGENT audio-only job: not an eager build, so neither MAX_EAGER nor
        an export can hold the sound of a new import behind another
        source's picture."""
        def ready() -> bool:
            if chunk is not None and P.chunk_path(key, chunk).is_file():
                return True
            return P.audio_done(key)

        if ready():
            return True
        info = P.load_source(key)
        if info is None or self._failed(key) or self.disk_full(key):
            return False
        self._queue_audio(key, info)
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cv:
            while not ready() and time.monotonic() < deadline:
                self._cv.wait(0.02)
        return ready()

    def cancel(self, src: str | os.PathLike) -> bool:
        """Stop every running job of ``src`` (queued ones finish as no-ops)."""
        real = os.path.realpath(os.fspath(src))
        with self._cv:
            self._heap = [j for j in self._heap if j.src != real]
            heapq.heapify(self._heap)
            for k in [k for k in self._span_jobs if self._span_jobs[k].src == real]:
                self._span_jobs.pop(k).done.set()
            for k in [k for k in self._audio_jobs if self._audio_jobs[k].src == real]:
                self._audio_jobs.pop(k).done.set()
            self._eager_keys = {k for k in self._eager_keys
                                if (P.load_source(k) or _Nil).src != real}
            self._cv.notify_all()
        return _cancel.PROXIES.cancel(real)

    def wait_idle(self, timeout: float = 60.0) -> bool:
        """Block until no job is queued or running (tests, shutdown)."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while self._heap or self._eager_running or self._busy:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(min(left, 0.1))
        return True

    @contextlib.contextmanager
    def export_in_progress(self) -> Iterator[None]:
        """Pause eager proxy encodes while an export renders (spec §5.1)."""
        with self._cv:
            self._exports += 1
            self._export_event.set()
        try:
            yield
        finally:
            with self._cv:
                self._exports -= 1
                if self._exports <= 0:
                    self._exports = 0
                    self._export_event.clear()
                self._cv.notify_all()

    @property
    def exporting(self) -> bool:
        return self._exports > 0

    def shutdown(self) -> None:
        """Stop the workers and kill every running encode (app exit). The
        manager stays usable: the next job starts a fresh set of workers (a
        TestClient's lifespan runs this once per client)."""
        with self._cv:
            self._gen += 1
            self._workers = []
            srcs = {j.src for j in self._heap}
            self._heap = []
            self._eager_keys.clear()
            self._cv.notify_all()
        for src in srcs:
            _cancel.PROXIES.cancel(src)
        with _cancel.PROXIES._lock:
            live = list(_cancel.PROXIES._live)
        for src in live:
            _cancel.PROXIES.cancel(src)

    # ---- queueing ---------------------------------------------------------------

    def _start_workers(self) -> None:
        if self._workers:
            return
        for i in range(MAX_WORKERS):
            t = threading.Thread(target=self._worker, args=(self._gen,),
                                 name=f"proxy-worker-{i}", daemon=True)
            t.start()
            self._workers.append(t)

    def _queue_eager(self, key: str, src: str, priority: int = PRIORITY_NORMAL) -> None:
        with self._cv:
            if key in self._eager_keys:
                if priority < PRIORITY_NORMAL:
                    for j in self._heap:
                        if j.kind == "eager" and j.key == key and j.priority > priority:
                            j.priority = priority
                    heapq.heapify(self._heap)
                return
            if self._complete(key):
                return
            self._eager_keys.add(key)
            heapq.heappush(self._heap, _Job(priority, next(self._seq), "eager", key, src))
            self._start_workers()
            self._cv.notify_all()

    def _queue_spans(self, key: str, info: P.SourceInfo, n: int) -> _Job:
        with self._cv:
            hit = self._span_jobs.get((key, n))
            if hit is not None and not hit.done.is_set():
                return hit
            spans = []
            for m in range(n, min(info.spans, n + ONDEMAND_SPANS)):
                if P.span_path(key, m).is_file() or (key, m) in self._span_jobs:
                    break
                spans.append(m)
            job = _Job(PRIORITY_URGENT, next(self._seq), "spans", key, info.src,
                       spans=tuple(spans or (n,)))
            for m in job.spans:
                self._span_jobs[(key, m)] = job
            heapq.heappush(self._heap, job)
            self._start_workers()
            self._cv.notify_all()
            return job

    def _queue_audio(self, key: str, info: P.SourceInfo) -> _Job:
        with self._cv:
            hit = self._audio_jobs.get(key)
            if hit is not None and not hit.done.is_set():
                return hit
            job = _Job(PRIORITY_URGENT, next(self._seq), "audio", key, info.src)
            self._audio_jobs[key] = job
            heapq.heappush(self._heap, job)
            self._start_workers()
            self._cv.notify_all()
            return job

    def _next_job(self) -> _Job | None:
        """Highest-priority runnable job; eager ones only when a slot is free
        and no export runs. Called with the condition held."""
        runnable = [j for j in self._heap
                    if j.kind != "eager" or (self._eager_running < MAX_EAGER and not self._exports)]
        if not runnable:
            return None
        job = min(runnable)
        self._heap.remove(job)
        heapq.heapify(self._heap)
        return job

    def _worker(self, gen: int) -> None:
        while gen == self._gen:
            with self._cv:
                job = self._next_job()
                while job is None:
                    self._cv.wait(0.5)
                    if gen != self._gen:
                        return
                    job = self._next_job()
                self._busy += 1
                if job.kind == "eager":
                    self._eager_running += 1
            try:
                if job.kind == "eager":
                    self._run_eager(job)
                elif job.kind == "audio":
                    self._run_audio(job)
                else:
                    self._run_spans(job)
            except Exception as e:           # never let a worker die
                job.error = str(e)
                if _is_disk_full(e):
                    # held (disk_full): the routes answer 507 instead of
                    # queueing the same doomed encode on every poll
                    with self._cv:
                        self._disk_full[job.key] = time.monotonic()
                    self.stats["disk_full"] += 1
                    _log.warning("proxy job failed: disk full (%s)", job.key)
                else:
                    # failed for good, like a ProxyError: the volume under
                    # WORKDIR gone (EIO from the span writer), the ffmpeg
                    # binary gone (FileNotFoundError from Popen), a bug. Mark
                    # it, so the routes answer 410 on the next poll instead of
                    # queueing the same doomed encode on every one (final
                    # sweep 4: the index stayed 'pending' for good).
                    _log.warning("proxy job failed", exc_info=True)
                    self.stats["failures"] += 1
                    try:
                        P.mark_failed(job.key, f"{type(e).__name__}: {e}")
                    except Exception:        # a proxy dir that cannot be written
                        _log.warning("proxy failure of %s could not be recorded", job.key,
                                     exc_info=True)
            finally:
                with self._cv:
                    self._busy -= 1
                    if job.kind == "eager":
                        self._eager_running -= 1
                    for m in job.spans:
                        if self._span_jobs.get((job.key, m)) is job:
                            del self._span_jobs[(job.key, m)]
                    if self._audio_jobs.get(job.key) is job:
                        del self._audio_jobs[job.key]
                    job.done.set()
                    self._cv.notify_all()

    # ---- job bodies -------------------------------------------------------------

    def _probe_lock(self, key: str) -> threading.Lock:
        with self._cv:
            return self._probe_locks.setdefault(key, threading.Lock())

    def _probe(self, key: str, src: str) -> P.SourceInfo | None:
        with self._probe_lock(key):
            info = P.load_source(key)
            if info is not None:
                return info
            try:
                info = P.probe_source(src, key)
            except (P.ProxyError, OSError, ValueError) as e:
                P.mark_failed(key, f"probe failed: {e}")
                return None
            except Exception as e:           # ffprobe CalledProcessError etc.
                P.mark_failed(key, f"probe failed: {e}")
                return None
            P.save_source(info)
            idx = P.read_index(key) or {}
            if "frames" not in idx:
                P.write_index(key, {**P.static_index(info), **{k: v for k, v in idx.items()
                                                               if k in ("codec", "init_key")}})
            return info

    def _probe_bounded(self, key: str, src: str, timeout: float) -> None:
        if P.load_source(key) is not None:
            return
        t = threading.Thread(target=self._probe, args=(key, src), daemon=True)
        t.start()
        t.join(timeout)

    def _failed(self, key: str) -> bool:
        return bool((P.read_index(key) or {}).get("failed"))

    def _complete(self, key: str) -> bool:
        idx = P.live_index(key)
        return bool(idx and idx.get("state") in ("ready", "failed"))

    def _scope(self, info: P.SourceInfo):
        return _cancel.PROXIES.begin(info.src, info.key)

    def _run_eager(self, job: _Job) -> None:
        info = self._probe(job.key, job.src)
        if info is None:
            with self._cv:
                self._eager_keys.discard(job.key)
            return
        ev = self._scope(info)
        paused = False
        try:
            if not P.audio_done(info.key):
                P.build_audio(info, cancel=_Either(ev, self._export_event))
                with self._cv:
                    self._cv.notify_all()
            for f0, f1 in self._missing_runs(info):
                P.encode_spans(info, f0, f1, on_init=lambda b: P.accept_init(info, b),
                               on_span=self._span_writer(info),
                               cancel=_Either(ev, self._export_event))
            self.stats["eager_builds"] += 1
        except P.Cancelled:
            paused = self._exports > 0 and not ev.is_set()
        except P.ProxyError as e:
            self.stats["failures"] += 1
            P.mark_failed(info.key, str(e))
        finally:
            _cancel.PROXIES.end(info.src, ev)
            with self._cv:
                self._eager_keys.discard(job.key)
        if paused:
            # Killed for an export: queue it again; it resumes from its first
            # missing span once the export is over.
            self.stats["paused"] += 1
            self._queue_eager(job.key, job.src)
            return
        self._enforce_budget()

    def _run_spans(self, job: _Job) -> None:
        info = P.load_source(job.key)
        if info is None:
            return
        todo = [n for n in job.spans if not P.span_path(job.key, n).is_file()]
        if not todo:
            return
        ev = self._scope(info)
        try:
            f0 = P.span_range(info, todo[0])[0]
            f1 = P.span_range(info, todo[-1])[1]
            P.encode_spans(info, f0, f1, on_init=lambda b: P.accept_init(info, b),
                           on_span=self._span_writer(info), cancel=ev)
            self.stats["span_jobs"] += 1
        except P.Cancelled:
            pass
        except P.ProxyError as e:
            self.stats["failures"] += 1
            P.mark_failed(info.key, str(e))
        finally:
            _cancel.PROXIES.end(info.src, ev)

    def _run_audio(self, job: _Job) -> None:
        info = P.load_source(job.key)
        if info is None or P.audio_done(job.key):
            return
        ev = self._scope(info)
        try:
            P.build_audio(info, cancel=ev)
        except P.Cancelled:
            pass
        except P.ProxyError as e:
            self.stats["failures"] += 1
            P.mark_failed(info.key, str(e))
        finally:
            _cancel.PROXIES.end(info.src, ev)

    def _span_writer(self, info: P.SourceInfo):
        def write(n: int, samples: list[bytes]) -> None:
            P.write_span(info.key, n, P.span_range(info, n)[0], samples)
            with self._cv:
                self._cv.notify_all()
        return write

    @staticmethod
    def _missing_runs(info: P.SourceInfo) -> list[tuple[int, int]]:
        runs: list[tuple[int, int]] = []
        for n in P.missing_spans(info.key, info):
            f0, f1 = P.span_range(info, n)
            if runs and runs[-1][1] == f0:
                runs[-1] = (runs[-1][0], f1)
            else:
                runs.append((f0, f1))
        return runs

    def _enforce_budget(self) -> None:
        try:
            from ..render import cache_budget
            cache_budget.enforce_proxies(P.proxies_root())
        except Exception:
            _log.warning("proxy cache trim failed", exc_info=True)


class _Either:
    """An Event-like 'set when either is set' view (cancel OR export pause)."""

    def __init__(self, a: threading.Event, b: threading.Event):
        self._a, self._b = a, b

    def is_set(self) -> bool:
        return self._a.is_set() or self._b.is_set()


class _Nil:
    src = None


MANAGER = ProxyManager()


def export_in_progress():
    """Context manager: eager proxy encodes pause while an export renders."""
    return MANAGER.export_in_progress()


__all__ = ["MANAGER", "ProxyManager", "export_in_progress", "MAX_WORKERS",
           "MAX_EAGER", "ONDEMAND_SPANS"]
