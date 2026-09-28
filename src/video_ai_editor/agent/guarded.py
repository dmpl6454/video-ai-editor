"""The `/dispatch` guards, for the agent surfaces that are not `/dispatch`.

Final QA: the MCP route (`mcp_server.handle_message`) and the cloud chat's
tool calls (`loop.chat_turn`) called `dispatch()` directly. `/dispatch` does
two things first — it refuses with `409 prompt_running` while a Prompt-bar run
holds the project, and it runs the tool under `session_lock(sid)` — so:

  * during a Prompt-bar run an MCP `add_text` answered isError:false and a
    chat tool_use emitted tool_result ok + an op, then Cancel restored the
    pre-run timeline and the agent's confirmed edit was silently gone (or,
    un-cancelled, it was committed in the middle of the prompt's batch);
  * both surfaces ran the tool inside an `async def`, on the event loop, so a
    transcribe froze /api/health, the UI and the preview for its whole run.

`guarded_dispatch` is the one function both call — from a worker thread
(the callers use `run_in_threadpool` / `asyncio.to_thread`), never the loop,
because it may block on the session lock behind a job.
"""
from __future__ import annotations

from typing import Any

from ..api import locks

PROMPT_RUNNING_CODE = "prompt_running"
PROMPT_RUNNING_MESSAGE = ("A Prompt-bar run is editing this project — wait for it to "
                          "finish or cancel it, then try again.")


class PromptRunning(RuntimeError):
    """The project is held by a Prompt-bar run (the `/dispatch` 409)."""

    code = PROMPT_RUNNING_CODE

    def __init__(self) -> None:
        super().__init__(PROMPT_RUNNING_MESSAGE)


def session_id_of(store: Any) -> str:
    """The session a store belongs to: its directory is `<WORKDIR>/<sid>`."""
    return store.dir.name


def guarded_dispatch(store: Any, sid: str, tool: str, args: dict) -> dict:
    """`dispatch(store, tool, args)` behind the `/dispatch` guards: refused
    while a prompt run holds `sid` (checked again once the lock is ours — a run
    registers before it takes the lock), serialised with every other writer."""
    from .dispatch import dispatch
    if locks.prompt_run(sid) is not None:
        raise PromptRunning()
    with locks.session_lock(sid):
        if locks.prompt_run(sid) is not None:
            raise PromptRunning()
        return dispatch(store, tool, args)


__all__ = ["PromptRunning", "PROMPT_RUNNING_CODE", "PROMPT_RUNNING_MESSAGE",
           "guarded_dispatch", "session_id_of"]
