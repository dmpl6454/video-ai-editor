"""In-process background job manager for renders.

Used for /preview + /export when the caller doesn't want to block the HTTP
request thread. Renders run in a dedicated `ThreadPoolExecutor` (separate
from FastAPI's request threadpool) so a flood of long renders can't starve
ordinary GETs.

Trade-offs we picked:
- In-process, single-instance only. No Redis, no Celery. Right call for the
  desktop-app + small-team-server scope; if you grow to multi-machine you
  rip this out and put RQ/Arq in front.
- Job state is in memory only. A process restart loses queued jobs but the
  underlying render cache survives (so re-requesting the same EDL hash hits
  the cache and returns instantly).
- Worker count defaults to 2 (renders are CPU+ffmpeg-bound; more concurrency
  doesn't help). Override with VAI_JOB_WORKERS.
"""
from __future__ import annotations
import inspect
import os
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field
from typing import Any, Callable, Literal


JobStatus = Literal["queued", "running", "completed", "failed", "cancelled"]


class JobCancelled(Exception):
    """Raised inside a job fn (via its cancel_event) to abort cleanly. The
    JobManager maps it to status='cancelled' rather than 'failed'."""


@dataclass
class Job:
    id: str
    kind: str                                  # "preview" / "export" / etc.
    status: JobStatus = "queued"
    progress: float = 0.0                      # 0.0..1.0 (best-effort)
    result: dict | None = None                 # populated on completion
    error: str | None = None                   # populated on failure
    started_at: float | None = None
    completed_at: float | None = None
    created_at: float = field(default_factory=time.time)
    session_id: str | None = None              # for permission scoping
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _future: Future | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "status": self.status,
            "progress": self.progress, "result": self.result, "error": self.error,
            "created_at": self.created_at, "started_at": self.started_at,
            "completed_at": self.completed_at, "session_id": self.session_id,
        }


class JobManager:
    """Single global instance. Submit jobs, poll status, retain N completed."""

    def __init__(self, *, workers: int = 2, retain_completed: int = 200):
        self._executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="vai-job"
        )
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._lock = threading.Lock()
        self._retain = retain_completed

    def submit(self, *, kind: str, fn: Callable[..., dict],
               session_id: str | None = None, **kwargs: Any) -> Job:
        """Queue `fn(**kwargs)` for background execution. Returns the Job
        immediately. The Job's `result` field gets populated on success."""
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, session_id=session_id)
        with self._lock:
            self._jobs[job.id] = job
            self._jobs.move_to_end(job.id)
            self._evict_old_completed_locked()

        def _set_progress(p: float) -> None:
            # Single float assignment — atomic under the GIL, so no lock needed
            # on the hot path (progress fires many times per render).
            job.progress = 0.0 if p < 0 else 1.0 if p > 1 else float(p)

        # Cooperative hooks are opt-in: only passed to fns that declare them,
        # so existing zero-arg job fns keep working unchanged.
        inject: dict[str, Any] = {}
        try:
            params = inspect.signature(fn).parameters
            if "set_progress" in params:
                inject["set_progress"] = _set_progress
            if "cancel_event" in params:
                inject["cancel_event"] = job.cancel_event
        except (TypeError, ValueError):
            pass

        def _run() -> None:
            if job.cancel_event.is_set():       # cancelled before it started
                with self._lock:
                    job.status = "cancelled"
                    job.completed_at = time.time()
                return
            with self._lock:
                job.status = "running"
                job.started_at = time.time()
            try:
                out = fn(**kwargs, **inject)
                with self._lock:
                    job.status = "completed"
                    job.result = out
                    job.completed_at = time.time()
                    job.progress = 1.0
            except JobCancelled:
                with self._lock:
                    job.status = "cancelled"
                    job.completed_at = time.time()
            except Exception as e:  # noqa: BLE001 (we want to capture *anything*)
                with self._lock:
                    # A failure that lands while a cancel is pending is a cancel,
                    # not an error (e.g. ffmpeg killed mid-write).
                    if job.cancel_event.is_set():
                        job.status = "cancelled"
                    else:
                        job.status = "failed"
                        job.error = f"{type(e).__name__}: {e}"
                    job.completed_at = time.time()

        job._future = self._executor.submit(_run)
        return job

    def cancel(self, job_id: str) -> Job | None:
        """Signal a job to stop. A running job's fn observes `cancel_event` and
        bails (→ 'cancelled'); a still-queued job is marked cancelled outright."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            job.cancel_event.set()
            if job.status == "queued":
                job.status = "cancelled"
                job.completed_at = time.time()
                if job._future is not None:
                    job._future.cancel()
            return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, *, session_id: str | None = None) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        if session_id is not None:
            jobs = [j for j in jobs if j.session_id == session_id]
        return jobs

    def _evict_old_completed_locked(self) -> None:
        """Drop the oldest finished jobs once we exceed the retention cap."""
        completed_ids = [
            jid for jid, j in self._jobs.items()
            if j.status in ("completed", "failed")
        ]
        # Drop in insertion order (oldest first) until under cap.
        excess = len(completed_ids) - self._retain
        for jid in completed_ids[:max(0, excess)]:
            self._jobs.pop(jid, None)

    def shutdown(self, wait: bool = True) -> None:
        """Stop accepting new jobs; drain in-flight if `wait`."""
        self._executor.shutdown(wait=wait)


# Singleton — the rest of the codebase imports this directly.
JOB_MANAGER = JobManager(
    workers=int(os.environ.get("VAI_JOB_WORKERS", "2")),
    retain_completed=int(os.environ.get("VAI_JOB_RETAIN", "200")),
)
