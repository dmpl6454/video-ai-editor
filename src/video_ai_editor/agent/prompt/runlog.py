"""The run record and the in-memory event bus a prompt run publishes to.

Spec §4.2: execution outlives the SSE connection. The run thread never
writes to a socket; it publishes every event to a `RunBus` (an append-only
list under a condition variable) and mirrors the run's state into
`<session>/prompt_run.json`. An SSE generator SUBSCRIBES — it reads the bus
from an index and yields until `done`. A client that disconnects only stops
reading; the run keeps going; a client that reconnects (desktop reload,
phone unlocked) replays from the index it last saw. Cancellation is a
separate, explicit act (`POST …/prompt/cancel` → the run's `cancel_event`).

WHY a full list and not a bounded ring: a run emits at most a few hundred
frames (steps × {running, ok} + tool_use/tool_result + progress ticks at
≤ 4 Hz) and lives only as long as `executor.RUNS` keeps the handle (the last
run per session). A ring that dropped early frames would make a reconnect
miss the `plan` event — the one frame the desktop needs to draw anything.

The run record (`RunRecord`) is the durable half: what `GET …/prompt/run`
returns after a process restart, and what the benchmark reads. It is
rewritten on every event (a JSON of a few KB — cheaper than any debate about
which events matter) and is always internally consistent because a single
thread writes it.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .service import RUNLOG_FILE

RunStatus = str   # "planning" | "running" | "verifying" | "done" | "failed" | "cancelled" | "clarify"

#: How long one blocking `wait_next` call sleeps before re-checking the
#: subscriber's cancellation; also bounds how quickly an SSE generator
#: notices a dropped client between frames.
WAIT_SLICE_S = 0.5


def new_run_id() -> str:
    return f"r_{uuid4().hex[:8]}"


class RunBus:
    """Append-only event log with blocking reads from an index."""

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._cond = threading.Condition()
        self._closed = False

    def publish(self, event: dict[str, Any]) -> int:
        """Append one event; returns its index. Publishing `done` closes the
        bus — nothing may follow it, so a subscriber's `done` is final."""
        with self._cond:
            if self._closed:
                raise RuntimeError("RunBus is closed: `done` was already published")
            self._events.append(event)
            if event.get("type") == "done":
                self._closed = True
            self._cond.notify_all()
            return len(self._events) - 1

    @property
    def closed(self) -> bool:
        with self._cond:
            return self._closed

    def __len__(self) -> int:
        with self._cond:
            return len(self._events)

    def snapshot(self, start: int = 0) -> list[dict[str, Any]]:
        with self._cond:
            return list(self._events[start:])

    def wait_next(self, index: int, timeout: float = WAIT_SLICE_S) -> dict[str, Any] | None:
        """The event at `index`, blocking up to `timeout` for it to arrive.
        None on timeout, or when the bus is closed and `index` is past the
        end (nothing will ever arrive)."""
        with self._cond:
            if index < len(self._events):
                return self._events[index]
            if self._closed:
                return None
            self._cond.wait(timeout)
            if index < len(self._events):
                return self._events[index]
            return None

    def iter_from(self, start: int = 0, *, should_stop=None) -> Iterator[dict[str, Any]]:
        """Blocking iterator from `start` until `done`. `should_stop()` is
        polled between waits so a caller can unsubscribe without cancelling
        the run — the whole point of the decoupling."""
        index = start
        while True:
            evt = self.wait_next(index)
            if evt is None:
                if self.closed and index >= len(self):
                    return
                if should_stop is not None and should_stop():
                    return
                continue
            index += 1
            yield evt
            if evt.get("type") == "done":
                return


@dataclass
class StepRecord:
    index: int
    total: int
    tool: str
    status: str = "running"            # running | ok | failed | skipped
    summary: str = ""
    effect: str | None = None          # "none" when the tool reported nothing to do
    error: str | None = None
    started: float = field(default_factory=time.time)
    ended: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"index": self.index, "total": self.total, "tool": self.tool,
                "status": self.status, "summary": self.summary, "effect": self.effect,
                "error": self.error, "started": self.started, "ended": self.ended}


@dataclass
class RunRecord:
    """`<session>/prompt_run.json` — the durable state of the current or last
    prompt run (spec §0.2 contract: `{run_id, plan_id, status, started,
    steps, verify, reply, op}`; the extra keys are additive)."""
    run_id: str
    plan_id: str | None
    prompt: str
    brain: str | None = None
    status: RunStatus = "planning"
    started: float = field(default_factory=time.time)
    ended: float | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    verify: dict[str, Any] | None = None
    reply: str | None = None
    op: dict[str, Any] | None = None
    error: str | None = None
    child_runs: list[dict[str, Any]] = field(default_factory=list)
    events: int = 0                     # how many bus events exist; reconnect resumes from here

    def as_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "plan_id": self.plan_id, "prompt": self.prompt,
                "brain": self.brain, "status": self.status, "started": self.started,
                "ended": self.ended, "steps": list(self.steps), "verify": self.verify,
                "reply": self.reply, "op": self.op, "error": self.error,
                "child_runs": list(self.child_runs), "events": self.events}


def runlog_path(session_dir: Path) -> Path:
    return Path(session_dir) / RUNLOG_FILE


def write_record(session_dir: Path, record: RunRecord) -> None:
    """Atomic rewrite (tmp + replace) so a reader never sees a torn file."""
    path = runlog_path(session_dir)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record.as_dict(), indent=1, default=str), encoding="utf-8")
    tmp.replace(path)


def read_record(session_dir: Path) -> dict[str, Any] | None:
    path = runlog_path(session_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class RunLog:
    """Binds a `RunBus` to a `RunRecord`: `emit()` publishes AND folds the
    event into the record AND rewrites the file. One writer (the run thread),
    so no lock beyond the bus's own."""

    def __init__(self, session_dir: Path, record: RunRecord, bus: RunBus | None = None) -> None:
        self.session_dir = Path(session_dir)
        self.record = record
        self.bus = bus or RunBus()
        self._steps: dict[int, StepRecord] = {}
        # The run thread and its synthetic-progress ticker both emit; the
        # fold + file rewrite must not interleave.
        self._lock = threading.Lock()

    def emit(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._fold(event)
            self.record.events = self.bus.publish(event) + 1
            write_record(self.session_dir, self.record)

    def set_status(self, status: RunStatus, *, error: str | None = None) -> None:
        with self._lock:
            self.record.status = status
            if error is not None:
                self.record.error = error
            if status in ("done", "failed", "cancelled", "clarify"):
                self.record.ended = time.time()
            write_record(self.session_dir, self.record)

    def _fold(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "step":
            idx = int(event.get("index", 0))
            rec = self._steps.get(idx)
            if rec is None:
                rec = StepRecord(index=idx, total=int(event.get("total", 0)),
                                 tool=str(event.get("tool", "")))
                self._steps[idx] = rec
            status = str(event.get("status", rec.status))
            if status == "running" and rec.status in ("ok", "failed", "skipped"):
                return              # a progress tick that raced the terminal frame — the step is over
            rec.status = status
            rec.summary = str(event.get("summary") or rec.summary)
            rec.effect = event.get("effect", rec.effect)
            rec.error = event.get("error", rec.error)
            if rec.status in ("ok", "failed", "skipped"):
                rec.ended = time.time()
            self.record.steps = [self._steps[i].as_dict() for i in sorted(self._steps)]
        elif kind == "verify":
            self.record.verify = {k: v for k, v in event.items() if k != "type"}
        elif kind == "op":
            self.record.op = event.get("op")
        elif kind == "plan":
            plan = event.get("plan") or {}
            self.record.plan_id = plan.get("id", self.record.plan_id)
            self.record.brain = plan.get("brain", self.record.brain)
        elif kind == "error":
            self.record.error = str(event.get("message"))

    def set_reply(self, text: str) -> None:
        """The final summary text. Kept off the wire frame on purpose: the
        `text_delta{text}` shape is frozen (§4.1), so the record learns which
        text is THE reply from the executor, not from a flag in the event."""
        with self._lock:
            self.record.reply = text
            write_record(self.session_dir, self.record)


__all__ = ["RunBus", "RunLog", "RunRecord", "StepRecord", "new_run_id", "runlog_path",
           "write_record", "read_record", "WAIT_SLICE_S"]
