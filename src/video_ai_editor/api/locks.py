"""Per-session locks and the prompt-run registry (spec §4.2).

Moved here verbatim from `main.py` (`_SESSION_LOCKS`, `_SESSION_LOCKS_GUARD`,
`_session_lock`) so that a module which is not the FastAPI app — the prompt
executor's daemon thread — can take the same lock the `/dispatch` route and
the job workers take. Importing `main` from `agent/prompt/executor.py` would
be a circular import (main imports the chat loop, which imports the service,
which imports the executor).

Three things live here, all process-local:

  * `session_lock(sid)` — one mutation at a time per session. FastAPI runs
    sync endpoints in a threadpool and `dispatch()` read-modify-writes a
    shared EDLStore, so two concurrent edits would interleave and the second
    snapshot/ops entry would describe a state neither caller asked for.
    Uncontended acquisition is ~100 ns.
  * `history_lock(sid)` — serialises the two writers of `<session>/chat.json`:
    the chat route's `finally: _save_history` and the prompt run thread's
    `finalize`, which may run in either order (§4.6). Separate from the
    session lock because history is written while the session lock is held
    by the run.
  * the prompt-run registry — `register_prompt_run` / `prompt_run` /
    `clear_prompt_run`. `/dispatch` checks it BEFORE blocking on the session
    lock and answers `409 prompt_running` instead of hanging a UI gesture
    behind a three-minute caption run. Job workers keep blocking (documented
    in main.py): a queued job is meant to wait.

WHY a per-session `run_id` and not a bare flag: `clear_prompt_run(sid, run_id)`
only clears the entry it registered, so a run that finished late cannot erase
the registration of the run that replaced it.
"""
from __future__ import annotations

import threading

_SESSION_LOCKS: dict[str, threading.Lock] = {}
_SESSION_LOCKS_GUARD = threading.Lock()

_HISTORY_LOCKS: dict[str, threading.Lock] = {}

_PROMPT_RUNS: dict[str, str] = {}


def session_lock(sid: str) -> threading.Lock:
    with _SESSION_LOCKS_GUARD:
        return _SESSION_LOCKS.setdefault(sid, threading.Lock())


def history_lock(sid: str) -> threading.Lock:
    with _SESSION_LOCKS_GUARD:
        return _HISTORY_LOCKS.setdefault(sid, threading.Lock())


def register_prompt_run(sid: str, run_id: str) -> None:
    with _SESSION_LOCKS_GUARD:
        _PROMPT_RUNS[sid] = run_id


def prompt_run(sid: str) -> str | None:
    """The id of the prompt run currently holding `sid`, or None."""
    with _SESSION_LOCKS_GUARD:
        return _PROMPT_RUNS.get(sid)


def clear_prompt_run(sid: str, run_id: str) -> None:
    with _SESSION_LOCKS_GUARD:
        if _PROMPT_RUNS.get(sid) == run_id:
            del _PROMPT_RUNS[sid]


__all__ = ["session_lock", "history_lock", "register_prompt_run", "prompt_run",
           "clear_prompt_run"]
