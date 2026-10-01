"""The seams the Editor Brain SURFACES reach the other lanes through (EB1-F).

Lane F (routes, the card, the reply, the versions strip) lands in parallel
with lane C's `brain/versions.py` and lane D's `brain/graph.py`, so every
import of theirs is lazy and goes through here:

  * `analyse_fn()` — D's `brain.graph.analyse(session_dir, sources) -> gid`
    (synchronous; the analyse route wraps it in a job). None until it lands.
  * `versions()` — C's `brain.versions` helper: `list(session_dir)`,
    `record(store, *, label, decisions_id, kind)` and
    `restore(store, version_id)` (the one that performs
    `commit("restore_version", …)` of the snapshot tree); rows live in
    `<session>/brain/versions.json`.

Expected signatures (the integrator adapts the seam, not the callers):

    brain.graph.analyse(session_dir: Path, sources: list[str], *,
                        set_progress=None, cancel_event=None, layers=None, force=False,
                        wait_transcript_s=0.0) -> GraphId (a `str` with `.pinned` / `.waiting_for`)
    brain.versions.list_versions(session_dir: Path) -> list[dict]
    brain.versions.record_version(store, *, label: str, decisions_id: str | None, kind: str) -> dict
    brain.versions.restore_version(store, version_id: str) -> dict   # {"op": dict | None, "version": row}
        raises UnknownVersion / NotRestorable (distinct types; the seam re-raises them as
        `UnknownVersionError` / `VersionNotRestorable`, matched by TYPE, never by message)
"""
from __future__ import annotations

import importlib
import inspect
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Protocol

_log = logging.getLogger(__name__)

VERSIONS_FILE = "versions.json"
RESTORE_TOOL = "restore_version"
ANALYSE_KIND = "brain_analyse"
#: How long the analysis job waits for an upload's background transcript before it gives up and says so.
TRANSCRIPT_WAIT_S = 180.0
#: sid → the latest analysis job id (the graph summary's `analysis`, the gate's wait).
_ANALYSIS_JOBS: dict[str, str] = {}
_FLIGHT = threading.Lock()                          # check-then-submit of a session's analysis is one step
_PROGRESS: dict[str, dict[str, Any]] = {}           # job id → the last {layer, pct, eta_s} it reported
_CANCELLED: set[str] = set()                        # job ids a person cancelled
_RESUME: dict[str, threading.Event] = {}            # sid → set when the run is cancelled while the gate resumes
_KEEP_EVENTS = 64
#: Reading the footage costs a couple of seconds of setup plus a small fraction of the media's length (measured
#: on the EB1 fixtures: a 70 s talking head reads in about 3 s). Good enough for the first estimate; once a
#: read is past its sync stage its own pace takes over.
READ_FIXED_S, READ_PER_MEDIA_S = 1.5, 0.04


class AnalysisBusy(RuntimeError):
    """The session's footage is already being read and this request differs from the run in flight."""


class UnknownVersionError(KeyError):
    """No such version in `versions.json`."""


class VersionNotRestorable(LookupError):
    """The version exists but its snapshot was pruned."""


def _import(name: str):
    try:
        return importlib.import_module(name)
    except Exception:  # noqa: BLE001 — a half-landed package must not take the routes down
        return None


def analyse_fn() -> Callable[..., Any] | None:
    mod = _import("video_ai_editor.brain.graph")
    fn = getattr(mod, "analyse", None) if mod is not None else None
    return fn if callable(fn) else None


def session_sources(edl: Any) -> list[str]:
    """The session's media files, in timeline order, each once: the main
    lane first, then the audio lanes (a recorder upload)."""
    out: list[str] = []
    tracks = sorted(edl.tracks, key=lambda t: (0 if t.id == "v1" else 1 if t.type == "video" else 2))
    for t in tracks:
        for c in sorted(t.clips, key=lambda c: getattr(c, "start", 0.0)):
            src = getattr(c, "src", None)
            if isinstance(src, str) and src and src not in out:
                out.append(src)
    return out


def analysis_event(layer: str, pct: float, eta_s: float | None, **extra: Any) -> dict[str, Any]:
    """The frame shape spec §8.7 names (service.BRAIN_EVENT_TYPES). `layer` is the layer being read
    (`sync`, `transcript`, `audio`, `speakers`, `speech`, `semantic`, `graph`, `done`; empty when the
    analyser did not say); `extra` keys (optional, e.g. `waiting: "transcript"`) are added only when given."""
    return {"type": "analysis", "layer": layer, "pct": max(0.0, min(100.0, float(pct))), "eta_s": eta_s,
            **{k: v for k, v in extra.items() if v is not None}}


def estimate_read_s(media_s: float) -> float:
    """How long reading `media_s` seconds of footage takes, from the media's length alone (the gate's
    "≈ N min", the first `eta_s` of a read)."""
    return READ_FIXED_S + READ_PER_MEDIA_S * max(0.0, float(media_s))


def scrub_error(text: Any) -> str:
    """`text` with every absolute path reduced to its leaf name — what reaches the job record and the reply."""
    from ...brain.digest import scrub_paths_in_text
    return scrub_paths_in_text(text)


def _accepted_kwargs(fn: Callable[..., Any], candidates: dict[str, Any]) -> dict[str, Any]:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return {}
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(candidates)
    return {k: v for k, v in candidates.items() if k in params}


def _expected_read_s(sources: list[str]) -> float | None:
    try:
        from ...brain.analysis import pcm as _pcm
        return estimate_read_s(sum(_pcm.duration_s(x) for x in sources))
    except Exception:  # noqa: BLE001 — an estimate is a courtesy
        return None


def _eta(p: float, spent_s: float, expect_s: float | None) -> float | None:
    """Seconds left: the read's own pace once it has some (past 20 %), else the estimate from the media's length."""
    if p >= 0.2:
        return spent_s * (1 - p) / p
    return None if expect_s is None else max(0.0, expect_s * (1 - p))


def _wire(fn: Callable[..., Any], session_dir: Path, sources: list[str], layers: list[str] | None, force: bool,
          job_id: list[str], ready: threading.Event) -> Callable[..., dict]:
    """The job body: run the analyser, keep its progress by layer, pin a finished graph unless the read was
    cancelled or is still waiting for the transcript, and scrub what a failure says."""
    from ...api.jobs import JobCancelled
    from ...brain import store as _bstore

    def _job(set_progress=None, cancel_event=None) -> dict:
        ready.wait(5.0)                               # the submitter has recorded this job's id
        started = time.time()
        expect = _expected_read_s(sources)
        events: deque[dict[str, Any]] = deque(maxlen=_KEEP_EVENTS)

        def _progress(p: float, layer: str | None = None) -> None:
            if set_progress:
                set_progress(p)
            waiting = layer == "transcript"
            eta = None if waiting else _eta(p, time.time() - started, expect)
            evt = analysis_event(layer or "", p * 100.0, eta, waiting="transcript" if waiting else None)
            events.append(evt)
            _PROGRESS[job_id[0]] = evt

        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelled()
        kwargs = _accepted_kwargs(fn, {"set_progress": _progress, "cancel_event": cancel_event, "layers": layers,
                                       "force": force, "wait_transcript_s": TRANSCRIPT_WAIT_S})
        try:
            gid = fn(session_dir, list(sources), **kwargs)
        except JobCancelled:
            raise
        except Exception as e:  # noqa: BLE001 — said to the person without a directory in it
            if type(e).__name__ == "Cancelled":
                raise JobCancelled() from None
            _log.warning("footage analysis failed: %s: %s", type(e).__name__, e)
            raise type(type(e).__name__, (RuntimeError,), {})(scrub_error(e)) from None
        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelled()                      # a cancelled read pins nothing, whatever the analyser returned
        pinned = bool(getattr(gid, "pinned", True))
        if pinned:
            _bstore.set_current_graph(session_dir, str(gid))
        return {"graph_id": str(gid), "sources": [Path(x).name for x in sources], "events": list(events)[-8:],
                "pinned": pinned, "waiting_for": getattr(gid, "waiting_for", None)}
    return _job


def _running(job: Any) -> bool:
    return job is not None and job.status in ("queued", "running")


def _prune() -> None:
    from ...api.jobs import JOB_MANAGER
    for jid in [j for j in _PROGRESS if JOB_MANAGER.get(j) is None]:
        _PROGRESS.pop(jid, None)
        _CANCELLED.discard(jid)


def submit_analysis(store: Any, *, layers: list[str] | None = None, force: bool = False) -> tuple[Any, bool]:
    """`(job, joined)`: the ONE way an analysis starts. ONE analysis per session at a time — a request for
    the read that is already running JOINS it (`joined` True); a forced rebuild or a layer subset cannot ride
    on a run that is not doing it and is refused with `AnalysisBusy` (say it, do not queue it)."""
    fn = analyse_fn()
    if fn is None:
        raise LookupError("footage analysis is not installed in this build (brain/graph.py)")
    from ...api.jobs import JOB_MANAGER
    session_dir = Path(store.dir)
    sid = session_dir.name
    with _FLIGHT:
        live = latest_analysis_job(sid)
        if _running(live):
            if live.cancel_event.is_set():
                # cancel only SETS the event; the read stays 'running' until its next layer boundary. A request made
                # now must not join it (it would be answered "Cancelled" for a request the person just made)
                raise AnalysisBusy("The last read is still stopping — ask again in a moment.")
            if force or layers:
                raise AnalysisBusy("The footage is already being read — wait for it to finish, then ask again.")
            return live, True
        _prune()
        ids, ready = [""], threading.Event()
        job = JOB_MANAGER.submit(kind=ANALYSE_KIND, fn=_wire(fn, session_dir, session_sources(store.edl), layers,
                                                             force, ids, ready), session_id=sid)
        ids[0] = job.id
        _ANALYSIS_JOBS[sid] = job.id
        ready.set()
    return job, False


def start_analysis(store: Any, *, layers: list[str] | None = None, force: bool = False) -> Any:
    """Submit the footage analysis for `store`'s session to the jobs machinery and return the job (the
    running one when there is one — see `submit_analysis`). The job's result is `{graph_id, sources (leaf
    names), events, pinned, waiting_for}`; a finished read pins the graph it built unless it was cancelled or
    the upload's transcript was still being written (`pinned` False, `waiting_for` "transcript").
    `LookupError` when the analyser is not in this build; `AnalysisBusy` when a different read is running."""
    return submit_analysis(store, layers=layers, force=force)[0]


def latest_analysis_job(sid: str) -> Any | None:
    from ...api.jobs import JOB_MANAGER
    return JOB_MANAGER.get(_ANALYSIS_JOBS.get(sid, ""))


def analysis_progress(job_id: str) -> dict[str, Any] | None:
    """The last `analysis` frame the job reported ({type, layer, pct, eta_s, …}), None before its first."""
    return _PROGRESS.get(job_id)


def cancel_analysis(sid: str) -> str | None:
    """Cancel the session's footage read: the running job (its `cancel_event`, honoured at every layer
    boundary) and the gate's re-plan that waits on it. The job id, `"resume"` when only the re-plan was
    still to come, None when there was nothing to cancel."""
    from ...api.jobs import JOB_MANAGER
    with _FLIGHT:
        job = latest_analysis_job(sid)
        hit = None
        if _running(job):
            _CANCELLED.add(job.id)
            JOB_MANAGER.cancel(job.id)
            hit = job.id
        ev = _RESUME.get(sid)
        if ev is not None:
            ev.set()
            hit = hit or "resume"
    return hit


def resume_cancel_event(sid: str) -> threading.Event:
    """Registered by the analysis gate for the length of its resume (read → re-plan); `cancel_analysis` sets it."""
    with _FLIGHT:
        return _RESUME.setdefault(sid, threading.Event())


def end_resume(sid: str) -> None:
    with _FLIGHT:
        _RESUME.pop(sid, None)


def was_cancelled(job: Any) -> bool:
    return job is not None and (job.status == "cancelled" or job.id in _CANCELLED)


class VersionsApi(Protocol):
    def list(self, session_dir: Path) -> list[dict[str, Any]]: ...
    def record(self, store: Any, *, label: str, decisions_id: str | None, kind: str) -> dict[str, Any]: ...
    def restore(self, store: Any, version_id: str) -> dict[str, Any]: ...


def _row(v: Any) -> dict[str, Any]:
    """A `Version` dataclass (C's `as_row()` + `restorable`) or a dict → the row dict."""
    if isinstance(v, dict):
        return dict(v)
    row = v.as_row() if hasattr(v, "as_row") else dict(vars(v))
    if hasattr(v, "restorable"):
        row["restorable"] = bool(v.restorable)
    return row


class _ModuleVersions:
    """C's `brain/versions.py` behind the seam's three names:
    `list_versions(session_dir) -> [Version]`, `record(store, *, label,
    decisions_id, kind) -> Version`, `restore(store, id) -> Op | None`
    (`UnknownVersion` / `NotRestorable`, two distinct types, matched here by type)."""

    def __init__(self, mod: Any) -> None:
        self._mod = mod

    def list(self, session_dir: Path) -> list[dict[str, Any]]:
        fn = getattr(self._mod, "list_versions", None) or getattr(self._mod, "list")
        return [_row(r) for r in fn(Path(session_dir))]

    def record(self, store: Any, *, label: str, decisions_id: str | None, kind: str) -> dict[str, Any]:
        fn = getattr(self._mod, "record", None) or getattr(self._mod, "record_version")
        return _row(fn(store, label=label, decisions_id=decisions_id, kind=kind))

    def restore(self, store: Any, version_id: str) -> dict[str, Any]:
        fn = getattr(self._mod, "restore", None) or getattr(self._mod, "restore_version")
        try:
            out = fn(store, version_id)
        except self._mod.NotRestorable as e:
            raise VersionNotRestorable(str(e)) from e
        except self._mod.UnknownVersion as e:
            raise UnknownVersionError(version_id) from e
        if isinstance(out, dict) and "version" in out:
            return dict(out)
        op = out.model_dump() if hasattr(out, "model_dump") else out
        row = next((r for r in self.list(Path(store.dir)) if r.get("id") == version_id), {"id": version_id})
        return {"op": op, "version": row}


def versions() -> VersionsApi:
    """C's `brain/versions.py` behind the seam's three names."""
    from ...brain import versions as mod
    return _ModuleVersions(mod)


__all__ = ["VERSIONS_FILE", "RESTORE_TOOL", "ANALYSE_KIND", "TRANSCRIPT_WAIT_S", "AnalysisBusy", "UnknownVersionError",
           "VersionNotRestorable", "analyse_fn", "session_sources", "analysis_event", "estimate_read_s", "scrub_error", "submit_analysis",
           "start_analysis", "latest_analysis_job", "analysis_progress", "cancel_analysis", "resume_cancel_event",
           "end_resume", "was_cancelled", "versions", "VersionsApi"]
