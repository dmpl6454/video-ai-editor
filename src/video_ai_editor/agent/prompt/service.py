"""The prompt turn: SSE event contract and route table (spec §4.1, §4.7).

Frozen contract (§0.2): `EVENT_TYPES`, `PROMPT_EVENT_TYPES`, `ROUTES`, and
the signatures of `prompt_turn` / `resume`. X completes the bodies.

Event shapes (all single-line `json.dumps` frames; `done` is always last):

  existing, unchanged — `text_delta{text}`, `tool_use{name,args,id}`,
  `tool_result{name,result,id,is_error?}`, `op{op}`, `done`, `error{message}`
  new —
  `brain{status:"trying"|"answered"|"failed", brain, label, model?, detail?, latency_ms?}`
      one per attempt; the LAST `answered` is authoritative; emitted within
      100 ms of turn start (recipes is instant).
  `plan{plan}`           the validated Plan (incl. downloads_needed,
                         estimated_seconds); re-sent after a clarification.
  `step{index,total,tool,status:"running"|"ok"|"failed"|"skipped",progress?,summary?,effect?:"none",error?}`
                         plus per-step `tool_use`/`tool_result` with ids
                         `f"{plan.id}_s{index}"` so ChatOverlay and the phone
                         render steps as a Claude turn.
  `verify{plan_id, checks:[{check,human,pass,measured,expected,unit?,detail?,headline}], passed,total,rendered}`
  `clarify{token, plan_id, questions:[NeedsInput…], expires_in_s}` then `done`.
  `op` exactly once, AFTER verify. An `undo` / `redo` intent has no steps and
  no verify: `op` (only when the store actually moved) then the reply.

WHY the first `text_delta` of every turn starts with `"via <Brain label> — "`:
`mobile/lib/sse.ts` returns `{kind:"empty"}` for any type outside its
KNOWN_TYPES, so `brain` events are invisible on the phone; the prefix is the
only way it can show which brain answered with zero mobile changes (a stated
hard constraint).

WHY execution outlives the stream: iOS suspends the app on lock; cancelling
on disconnect would roll back a 3-minute caption run and lie to the phone's
history, which recovers from history and never replays. A disconnect only
unsubscribes; cancellation is `POST …/prompt/cancel` alone (§4.2).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable, Iterable

from .schema import Plan

#: The six shapes `agent/loop.py` already streams; unchanged.
LEGACY_EVENT_TYPES: tuple[str, ...] = ("text_delta", "tool_use", "tool_result", "op", "done", "error")

#: The five the Prompt Editor adds (§4.1). `mobile/lib/sse.ts` drops them.
PROMPT_EVENT_TYPES: tuple[str, ...] = ("brain", "plan", "step", "verify", "clarify")

EVENT_TYPES: tuple[str, ...] = LEGACY_EVENT_TYPES + PROMPT_EVENT_TYPES

BRAIN_STATUSES: tuple[str, ...] = ("trying", "answered", "failed")
STEP_STATUSES: tuple[str, ...] = ("running", "ok", "failed", "skipped")

#: Prefix of the first `text_delta` of every turn: `VIA_PREFIX.format(label=…)`.
VIA_PREFIX = "via {label} — "

#: Clarification tokens expire after this (§4.3 `expires:+600s`).
CLARIFY_TTL_S = 600
#: Total planning budget across the on-device brains (§3.4).
PLANNING_BUDGET_S = 12.0
#: How long the executor waits for an in-flight upload transcript (§4.2).
TRANSCRIPT_WAIT_S = 30.0
#: Verify render is skipped (checks `pass=None`) above this duration (§4.4).
VERIFY_RENDER_MAX_DURATION_S = 600.0

#: Route table (§4.7). Session routes take `{sid}`; the last three are global.
ROUTES: dict[str, tuple[str, str]] = {
    "prompt":          ("POST", "/api/sessions/{sid}/prompt"),          # {message, selection?, multi_selection?, playhead?, brain?, resume_run?} → SSE
    "answer":          ("POST", "/api/sessions/{sid}/prompt/answer"),   # {token, answers} → SSE
    "pending":         ("GET",  "/api/sessions/{sid}/prompt/pending"),  # → {pending}
    "run":             ("GET",  "/api/sessions/{sid}/prompt/run"),      # → prompt_run.json (current or last)
    "cancel":          ("POST", "/api/sessions/{sid}/prompt/cancel"),   # {token?} → {cancelled}
    "brains":          ("GET",  "/api/prompt/brains"),                  # ?refresh=1 → brains_report() (memo 60 s)
    "models":          ("GET",  "/api/prompt/models"),                  # → {tier, models:[…]}
    "models_download": ("POST", "/api/prompt/models/download"),         # {id} → 202 {job_id}; loopback-only else 403
    "models_delete":   ("POST", "/api/prompt/models/delete"),           # {id} → 202 {job_id}; loopback-only else 403
}

#: Routes that must refuse non-loopback callers with 403 (§4.7): these are
#: the only network-egress paths in the prompt feature.
LOOPBACK_ONLY_ROUTES: frozenset[str] = frozenset({"models_download", "models_delete"})

#: `409 {"code": "prompt_running", "run_id": …}` from `/dispatch` while a
#: prompt run holds the session lock (§4.2).
PROMPT_RUNNING_CODE = "prompt_running"

#: Per-session files the feature writes (relative to the session dir).
PENDING_FILE = "prompt_pending.json"
RUNLOG_FILE = "prompt_run.json"
SNAPSHOT_DIR = "cache/prompt_snap"


def route(name: str, sid: str | None = None) -> str:
    """Path for a route in `ROUTES`: the FastAPI template (`{sid}` kept) when
    no sid is given — what `api/prompt_routes.py` registers — or the concrete
    path for one session. (`str.format(sid=None)` would register
    `/api/sessions/None/prompt`, which is what the first cut of this did.)"""
    _, path = ROUTES[name]
    if sid is None:
        return path
    return path.format(sid=sid)


def via(label: str) -> str:
    return VIA_PREFIX.format(label=label)


def is_known_event(event: dict[str, Any]) -> bool:
    return event.get("type") in EVENT_TYPES


def unknown_event_types(events: Iterable[dict[str, Any]]) -> set[str]:
    """For the SSE-contract test: every `type` a stream used that is not in
    `EVENT_TYPES`."""
    return {str(e.get("type")) for e in events} - set(EVENT_TYPES)


# --------------------------------------------------------------------------
# wiring the app provides (main.py / api/prompt_routes.py::configure)
# --------------------------------------------------------------------------

#: `sid → EDLStore` — main's LRU-cached `_store` once configured; before that
#: (tests, MCP-less processes) a fresh store per session dir.
_RESOLVE_STORE: Callable[[str], Any] | None = None


def configure(*, resolve_store: Callable[[str], Any] | None = None) -> None:
    global _RESOLVE_STORE
    _RESOLVE_STORE = resolve_store


def resolve_store(sid: str) -> Any:
    if _RESOLVE_STORE is not None:
        return _RESOLVE_STORE(sid)
    from ...edl import EDLStore
    from ...storage import session_dir, session_exists
    if not session_exists(sid):
        raise KeyError(f"session {sid} not found")
    return EDLStore(session_dir(sid))


def _resolver_for(store: Any) -> Callable[[str], Any]:
    """Re-resolve through the app when configured, but hand back the very
    store this turn was given for its own session — a TestClient or a bare
    `chat_turn` caller may hold a store the app's cache never saw."""
    sid = Path(store.dir).name
    own_dir = Path(store.dir).resolve()

    def _resolve(target: str) -> Any:
        if _RESOLVE_STORE is not None:
            try:
                resolved = _RESOLVE_STORE(target)
            except Exception:  # noqa: BLE001 — fall back to the object we hold
                if target != sid:
                    raise
            else:
                # The app resolver answers by sid under WORKDIR. A caller that
                # handed us a store living elsewhere (a bare `chat_turn`, the
                # MCP server, a test) must get THAT store back, not an empty
                # session the resolver just created under the same name.
                if target != sid or Path(resolved.dir).resolve() == own_dir:
                    return resolved
        if target == sid:
            return store
        return resolve_store(target)

    return _resolve


# --------------------------------------------------------------------------
# chat history: the run thread and the chat route both write chat.json
# --------------------------------------------------------------------------

class FileHistoryWriter:
    """`<session>/chat.json`, the same file `main._load_history` /
    `_save_history` use, written under `api.locks.history_lock(sid)`.

    `finalize` rewrites the assistant block whose text carries `(run <id>)`
    — the provisional "working on…" line `prompt_turn` appended — with the
    final reply, or appends one if the route's `finally: _save_history` has
    not written it yet. Idempotent, so it is correct whether that save ran
    before or after the run finished (§4.6)."""

    def path(self, sid: str) -> Path:
        from ...storage import session_dir
        return session_dir(sid) / "chat.json"

    def load(self, sid: str) -> list[dict]:
        p = self.path(sid)
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def save(self, sid: str, history: list[dict]) -> None:
        from ...api.locks import history_lock
        with history_lock(sid):
            self.path(sid).write_text(json.dumps(history, indent=2, default=str), encoding="utf-8")

    def finalize(self, sid: str, run_id: str, text: str) -> None:
        from ...api.locks import history_lock
        with history_lock(sid):
            history = self.load(sid)
            updated = replace_provisional(history, run_id, text)
            self.path(sid).write_text(json.dumps(updated, indent=2, default=str), encoding="utf-8")


HISTORY = FileHistoryWriter()


def provisional_text(label: str, title: str, run_id: str) -> str:
    return f"{via(label)}working on: {title} (run {run_id})…"


def _block_mentions_run(message: dict, run_id: str) -> bool:
    if message.get("role") != "assistant":
        return False
    content = message.get("content")
    blocks = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
    return any(isinstance(b, dict) and b.get("type") == "text" and f"(run {run_id})" in str(b.get("text", ""))
               for b in blocks)


def _block_has_text(message: dict, text: str) -> bool:
    if message.get("role") != "assistant":
        return False
    content = message.get("content")
    blocks = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
    return any(isinstance(b, dict) and b.get("type") == "text" and b.get("text") == text for b in blocks)


def replace_provisional(history: list[dict], run_id: str, text: str) -> list[dict]:
    """A new history list with the provisional block for `run_id` replaced by
    `text` (or a final assistant block appended when there is none).

    Idempotent in BOTH directions the race can run (§4.6): if the final text
    is already there — the SSE generator saw `done` and rewrote its in-memory
    list before the route's `finally` saved it — nothing is appended; if only
    the provisional line is there, it is replaced."""
    out = [dict(m) for m in history]
    if any(_block_has_text(m, text) for m in out):
        return out
    for i in range(len(out) - 1, -1, -1):
        if _block_mentions_run(out[i], run_id):
            out[i] = {"role": "assistant", "content": [{"type": "text", "text": text}]}
            return out
    out.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
    return out


def _replace_in_place(history: list[dict], run_id: str, text: str) -> None:
    """Same as `replace_provisional`, on the list object the chat route will
    save in its `finally` — so the in-memory copy agrees with the disk one."""
    updated = replace_provisional(history, run_id, text)
    history[:] = updated


# --------------------------------------------------------------------------
# planning: facts → router (brain events) → plan
# --------------------------------------------------------------------------

class PlannerUnavailable(RuntimeError):
    pass


def build_facts_for(store: Any, ui_state: dict | None) -> Any:
    """`facts.build_facts` with the process-wide feature memo (never the
    2.2 s cold probe twice, §2.1). Raises whatever the builder raises; the
    turn reports it as "Could not read the timeline: …"."""
    from ...ai.features import cached_feature_report
    from .facts import build_facts
    return build_facts(store, ui_state, feature_report=cached_feature_report())


def question_text(q: Any) -> str:
    """The clarification as the phone sees it: the question, the options and
    how to answer — a whole-word reply (§4.3)."""
    text = q.question.rstrip()
    if q.kind == "confirm":
        return f"{text} Reply **yes** or **no**."
    if q.options:
        labels = [f"**{o.value}**" for o in q.options]
        if len(labels) > 1:
            return f"{text} Reply {', '.join(labels[:-1])} or {labels[-1]}."
        return f"{text} Reply {labels[0]}."
    if q.kind in ("number", "duration"):
        unit = f" ({q.unit})" if q.unit else (" (seconds)" if q.kind == "duration" else "")
        return f"{text} Reply with a number{unit}."
    return f"{text} Reply with your answer."


def _brain_event(status: str, brain: str, **extra: Any) -> dict[str, Any]:
    from .brains.base import BRAIN_LABELS
    return {"type": "brain", "status": status, "brain": brain,
            "label": BRAIN_LABELS.get(brain, brain), **{k: v for k, v in extra.items() if v is not None}}


def _route(req: Any, *, brain: str | None, emit: Callable[[dict], None]) -> Any:
    """`brains.router.plan` when B's router is installed; else the recipes
    planner alone, with the brain events the router would have emitted. An
    object with `.plan` (Plan | None) and, when clarifying, `.questions`."""
    try:
        from .brains import router as _router
    except ImportError:
        _router = None
    if _router is not None:
        return _router.plan(req, order=[brain] if brain else None, emit=emit)
    try:
        from .planner import plan as recipes_plan
    except ImportError as e:
        raise PlannerUnavailable(
            "no planner is installed: neither agent/prompt/brains/router.py nor "
            "agent/prompt/planner.py could be imported") from e
    emit(_brain_event("trying", "recipes"))
    started = time.monotonic()
    plan = recipes_plan(req.prompt, req.facts)
    emit(_brain_event("answered", "recipes", latency_ms=int((time.monotonic() - started) * 1000)))
    return SimpleNamespace(plan=plan, attempts=[], questions=None, reply=None)


def _plan_in_thread(req: Any, brain: str | None):
    """Run the router on a worker thread and hand its `brain` events to the
    event loop as they happen (the on-device brains take seconds; the first
    `brain` frame must not wait for the last)."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    def _emit(evt: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, evt)

    def _work():
        try:
            return _route(req, brain=brain, emit=_emit)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    task = asyncio.ensure_future(asyncio.to_thread(_work))
    return task, queue


async def _stream_planning(req: Any, brain: str | None):
    """Yields `brain` events, then a final `("routed", result)` tuple."""
    task, queue = _plan_in_thread(req, brain)
    while True:
        evt = await queue.get()
        if evt is None:
            break
        yield evt
    yield ("routed", await task)


def _clarify_plan(routed: Any, brain: str) -> Plan | None:
    """A read-only Plan carrying the router's intent question, when it asked
    one (`RoutedPlan.clarify`, a single NeedsInput; `questions` accepted too)."""
    from .schema import NeedsInput
    raw = getattr(routed, "clarify", None)
    questions = [raw] if raw is not None else list(getattr(routed, "questions", None) or [])
    if not questions:
        return None
    qs = [q if isinstance(q, NeedsInput) else NeedsInput.model_validate(q) for q in questions]
    note = getattr(routed, "note", None)
    return Plan.new(intent="ask", brain=brain, needs_input=qs, confidence=0.3,
                    reply=None if note in (None, "", "clarify_intent") else str(note))


# --------------------------------------------------------------------------
# undo / redo: a whole prompt run is one history step (§4.2 "one op, one
# undo step"), taken outside the executor
# --------------------------------------------------------------------------

#: The reply suffix every mutating turn carries (summary.py says the same for
#: a run); the phone reads this text verbatim, so the keys are spelled out.
_UNDO_HINT = "Undo with ⌘Z."
_REDO_HINT = "Redo with ⇧⌘Z."


def apply_history_step(store: Any, verb: str) -> tuple[str, dict[str, Any] | None]:
    """Perform `undo` / `redo` on `store` and return `(reply, op_payload)`.

    Goes through `dispatch()` — the only mutation path — with the very
    handlers the chat loop's tool calls and the desktop's `/dispatch` use
    (`dispatch.undo_op` / `redo_op`), so a prompt "undo" rewinds exactly
    what ⌘Z would: one snapshot, i.e. the whole previous prompt run
    (`run_plan` commits ONCE after its `store.batch()`).

    WHY a synthesised `Op` for undo: `EDLStore.undo` POPS the op it reverts,
    so `store.ops.last()` afterwards is the entry before it — streaming that
    (as the cloud chat loop does) would tell the clients "this op happened"
    about an edit that did not. The desktop and the phone only use an `op`
    frame as "the EDL changed, refresh", but the record they get should say
    what actually changed: the undone op mirrored, hashes swapped. Redo
    appends its own `redo` entry, which is streamed as-is. Nothing to undo /
    redo → no op payload, so no client refresh for a no-op.

    Must be called under `api.locks.session_lock(sid)` (the caller does).
    """
    from ...edl import Op
    from ..dispatch import dispatch

    if verb not in ("undo", "redo"):
        raise ValueError(f"not a history step: {verb!r}")
    undone = store.ops.last() if verb == "undo" else None
    hash_before = store.edl.hash()
    result = dispatch(store, verb, {})
    if not result.get("ok"):
        return f"Nothing to {verb}.", None
    if verb == "redo":
        last = store.ops.last()
        return f"Redid the last undone edit. {_UNDO_HINT}", last.model_dump() if last else None
    summary = undone.summary if undone is not None else "the last edit"
    op = Op(seq=undone.seq if undone is not None else len(store.ops.ops), ts=time.time(), tool="undo",
            args={}, summary=f"Undo: {summary}", edl_hash_before=hash_before,
            edl_hash_after=store.edl.hash(), by="user")
    return f"Undid: {summary}. {_REDO_HINT}", op.model_dump()


async def _history_step(store: Any, plan: Plan, *, history: list[dict]) -> AsyncIterator[dict]:
    """The `undo` / `redo` turn: `op` (when something changed), the reply,
    `done`. Before this existed the planner's step-less undo Plan fell into
    the read-only branch and the turn streamed "Undoing the last edit." +
    `done` without touching the store — a no-op that claimed success
    (benchmark case 22). `PLAN_DENY` rightly forbids an undo STEP inside a
    plan (it cannot be validated against a tree it has not seen), so the
    intent is honoured here, at the service, not in the executor."""
    from ...api import locks
    from .brains.base import BRAIN_LABELS

    sid = Path(store.dir).name
    verb = plan.intent
    label = BRAIN_LABELS.get(plan.brain, plan.brain)
    running = locks.prompt_run(sid)
    if running is not None:
        # Same answer `/dispatch` gives (409 prompt_running): blocking on the
        # lock behind a caption pass and then rewinding IT is not what the
        # user meant by "undo that".
        text = via(label) + (f"A prompt run ({running}) is still editing this session — "
                             f"wait for it or cancel it, then {verb} again.")
        history.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
        yield {"type": "error", "message": text}
        yield {"type": "done"}
        return

    def _work() -> tuple[str, dict[str, Any] | None]:
        with locks.session_lock(sid):
            # Re-resolve inside the lock, as the executor does: the app's LRU
            # may have evicted the object the route captured.
            return apply_history_step(_resolver_for(store)(sid), verb)

    try:
        reply, op = await asyncio.to_thread(_work)
    except Exception as e:  # noqa: BLE001 — the turn must end with a frame, not a traceback
        text = via(label) + f"{verb.title()} failed: {type(e).__name__}: {e}"
        history.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
        yield {"type": "error", "message": text}
        yield {"type": "done"}
        return
    text = via(label) + reply
    history.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
    if op is not None:
        yield {"type": "op", "op": op}
    yield {"type": "text_delta", "text": text}
    yield {"type": "done"}


# --------------------------------------------------------------------------
# the turn
# --------------------------------------------------------------------------

async def _iter_bus(bus: Any, start: int = 0) -> AsyncIterator[dict]:
    index = start
    while True:
        evt = await asyncio.to_thread(bus.wait_next, index)
        if evt is None:
            if bus.closed and index >= len(bus):
                return
            continue
        index += 1
        yield evt
        if evt.get("type") == "done":
            return


async def _run_and_stream(store: Any, plan: Plan, facts: Any, *, prompt: str, history: list[dict],
                          pre_events: list[dict],
                          consented_downloads: frozenset[str] = frozenset()) -> AsyncIterator[dict]:
    from . import executor
    from .brains.base import BRAIN_LABELS
    from .runlog import RunBus

    sid = Path(store.dir).name
    bus = RunBus()
    for evt in pre_events:
        bus.publish(evt)
    handle = executor.start_run(_resolver_for(store), sid, plan, facts, prompt=prompt,
                                history_writer=HISTORY, bus=bus, consented_downloads=consented_downloads)
    label = BRAIN_LABELS.get(plan.brain, plan.brain)
    history.append({"role": "assistant", "content": [
        {"type": "text", "text": provisional_text(label, plan.title or plan.intent, handle.run_id)}]})
    try:
        async for evt in _iter_bus(bus, start=len(pre_events)):
            if evt.get("type") == "done" and handle.final_text:
                _replace_in_place(history, handle.run_id, handle.final_text)
            yield evt
    finally:
        # A client that went away mid-run: the route's `finally` will save
        # this list. If the run has since finished, save the truth rather
        # than the provisional line; if not, the provisional line stays and
        # the run thread's `finalize` replaces it on disk when it is done.
        if handle.final_text:
            _replace_in_place(history, handle.run_id, handle.final_text)


async def _pause(store: Any, plan: Plan, facts: Any, *, prompt: str, history: list[dict],
                 ui_state: dict | None) -> AsyncIterator[dict]:
    from . import pending
    from .brains.base import BRAIN_LABELS

    record = pending.save_pending(Path(store.dir), plan=plan, prompt=prompt, facts=facts, ui_state=ui_state)
    questions = plan.blocking_questions
    text = via(BRAIN_LABELS.get(plan.brain, plan.brain)) + " ".join(question_text(q) for q in questions)
    if plan.reply:
        text = f"{text} ({plan.reply})"
    history.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
    yield {"type": "text_delta", "text": text}
    yield {"type": "clarify", "token": record["token"], "plan_id": plan.id,
           "questions": [q.model_dump() for q in questions], "expires_in_s": CLARIFY_TTL_S}
    yield {"type": "done"}


async def _replay(store: Any, run_id: str, *, from_index: int = 0) -> AsyncIterator[dict]:
    """Re-attach to a run's bus from `from_index` (the desktop's last seen
    frame count, §5.1) and stream to `done`. A run no longer in memory (the
    process restarted, or a later run replaced it) cannot be replayed —
    `GET …/prompt/run` still serves its record — so say so and end."""
    from . import executor
    handle = executor.get_run(Path(store.dir).name, run_id)
    if handle is None:
        yield {"type": "error", "message": f"run {run_id} is not in memory — read prompt_run.json instead"}
        yield {"type": "done"}
        return
    async for evt in _iter_bus(handle.bus, start=max(0, int(from_index or 0))):
        yield evt


async def prompt_turn(store: Any, user_message: str, history: list[dict], *,
                      ui_state: dict | None = None, brain: str | None = None,
                      resume_run: str | None = None, from_index: int = 0) -> AsyncIterator[dict]:
    """One prompt turn as an SSE event stream (§4.6): facts → pending check →
    router.plan (emits `brain` events) → clarify-intent or `plan` → start_run
    → subscribe → yield until `done`.

    `chat_turn`'s no-key branch delegates here; the cloud path is untouched.
    `history` is the list the chat route saves in its `finally`; the user
    message and the (provisional, then final) assistant reply are appended
    to it, tool blocks never are.
    """
    from . import pending
    from .brains.base import BRAIN_LABELS, BrainRequest, BrainUnavailable
    from .recipes import cards

    if resume_run:
        async for evt in _replay(store, resume_run, from_index=from_index):
            yield evt
        return

    session_dir = Path(store.dir)
    notes: list[str] = []
    try:
        facts = await asyncio.to_thread(build_facts_for, store, ui_state)
    except Exception as e:  # noqa: BLE001 — planning failures must reach the user as text
        yield {"type": "error", "message": f"Could not read the timeline: {e}"}
        yield {"type": "done"}
        return

    record = pending.load_pending(session_dir)
    if record is not None:
        valid, why = pending.pending_is_valid(record, facts)
        if valid:
            answers = pending.try_parse_answer(user_message, record)
            if answers is not None:
                async for evt in resume(store, record["token"], answers, ui_state,
                                        history=history, user_message=user_message):
                    yield evt
                return
            notes.append("Dropped the earlier question — planning your new request.")
        else:
            notes.append(f"Dropped the earlier question ({why}).")
        pending.clear_pending(session_dir)

    history.append({"role": "user", "content": user_message})
    req = BrainRequest(prompt=user_message, facts=facts, recipes=cards())
    brain = brain or (os.environ.get("VAI_BRAIN") or "").strip().lower() or None
    if brain in ("", "auto"):
        brain = None
    pre_events: list[dict] = []
    routed: Any = None
    answered: str | None = None
    try:
        async for item in _stream_planning(req, brain):
            if isinstance(item, tuple):
                routed = item[1]
                break
            pre_events.append(item)
            if item.get("status") == "answered":
                answered = item.get("brain")
            yield item
    except (PlannerUnavailable, BrainUnavailable) as e:
        fix = getattr(e, "fix", None)
        yield {"type": "error", "message": f"No brain could plan this: {e}" + (f" Fix: {fix}" if fix else "")}
        yield {"type": "done"}
        return
    except Exception as e:  # noqa: BLE001
        yield {"type": "error", "message": f"Planning failed: {type(e).__name__}: {e}"}
        yield {"type": "done"}
        return

    plan: Plan | None = getattr(routed, "plan", None)
    if plan is None:
        plan = _clarify_plan(routed, answered or "recipes")
    if plan is None:
        text = via(BRAIN_LABELS.get(answered or "recipes", "Recipes")) + (
            getattr(routed, "reply", None) or "I could not work out what to change — try naming the edit "
            "(captions, silences, music, reframe, shorts…).")
        history.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
        yield {"type": "text_delta", "text": text}
        yield {"type": "done"}
        return
    if answered != plan.brain:
        evt = _brain_event("answered", plan.brain)
        pre_events.append(evt)
        yield evt
    if notes:
        plan = plan.with_(reply=" ".join(notes + ([plan.reply] if plan.reply else [])))
    plan_evt = {"type": "plan", "plan": plan.model_dump()}
    pre_events.append(plan_evt)
    yield plan_evt

    if plan.blocking_questions:
        async for evt in _pause(store, plan, facts, prompt=user_message, history=history, ui_state=ui_state):
            yield evt
        return
    if plan.intent in ("undo", "redo"):
        # Step-less by design (PLAN_DENY), so it must be taken BEFORE the
        # read-only branch or it is silently a no-op.
        async for evt in _history_step(store, plan, history=history):
            yield evt
        return
    if plan.is_read_only:
        text = via(BRAIN_LABELS.get(plan.brain, plan.brain)) + (plan.reply or "Nothing to change.")
        history.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
        yield {"type": "text_delta", "text": text}
        yield {"type": "done"}
        return
    async for evt in _run_and_stream(store, plan, facts, prompt=user_message, history=history,
                                     pre_events=pre_events):
        yield evt


async def resume(store: Any, token: str, answers: dict[str, Any], ui_state: dict | None = None,
                 *, history: list[dict] | None = None, user_message: str | None = None
                 ) -> AsyncIterator[dict]:
    """Resume a paused plan with clarification answers (§4.3). Reached from
    `POST …/prompt/answer` or a whole-message match on the next chat turn.
    `history`/`user_message` are supplied by `prompt_turn`; the route loads
    and saves history itself when they are omitted.
    """
    from . import pending
    from .brains.base import BRAIN_LABELS

    session_dir = Path(store.dir)
    sid = session_dir.name
    own_history = history is None
    history = HISTORY.load(sid) if history is None else history
    record = pending.load_pending(session_dir)
    if record is None or record.get("token") != token:
        yield {"type": "error", "message": "There is no pending question for this session (or the token is stale)."}
        yield {"type": "done"}
        return
    try:
        facts = await asyncio.to_thread(build_facts_for, store, ui_state or record.get("ui_state") or None)
    except Exception as e:  # noqa: BLE001
        yield {"type": "error", "message": f"Could not read the timeline: {e}"}
        yield {"type": "done"}
        return
    valid, why = pending.pending_is_valid(record, facts)
    if not valid:
        pending.clear_pending(session_dir)
        yield {"type": "error", "message": f"That question no longer applies: {why}. Ask again."}
        yield {"type": "done"}
        return

    plan = pending.pending_plan(record)
    # Consent (§1.4): a **download** answer names the tools whose artefacts
    # the executor may fetch on THIS run — the only place that set is built.
    consented: frozenset[str] = frozenset()
    if str(answers.get("downloads", "")).strip().lower() in ("yes", "download", "y", "true", "1"):
        consented = frozenset(d.tool for d in plan.downloads_needed)
    pending.clear_pending(session_dir)
    prompt = record.get("prompt", "")
    history.append({"role": "user", "content": user_message or
                    ", ".join(f"{k}: {v}" for k, v in answers.items())})
    # `planner.apply_answers` fills every `$ask:<key>` placeholder (music_src
    # → add_music.src, range → cut_range.start/end, platform →
    # apply_export_preset.name), degrades a "skip" on downloads honestly and
    # re-validates. `pending.apply_answers` (binding by arg NAME only) left
    # the literal "$ask:…" in the step and the run failed after the user
    # had answered — verified live on "trim it" → "the first 5 seconds".
    try:
        from .planner import apply_answers as _apply
        plan = await asyncio.to_thread(_apply, plan, answers, facts)
    except ImportError:
        plan, notes = pending.apply_answers(plan, answers, duration=facts.duration)
        if notes:
            plan = plan.with_(reply=" ".join(([plan.reply] if plan.reply else []) + notes))
    except ValueError as e:      # PlanRejected — the answer made a plan the boundary refuses
        text = via(BRAIN_LABELS.get(plan.brain, plan.brain)) + f"That answer cannot run: {e}"
        history.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
        yield {"type": "error", "message": text}
        yield {"type": "done"}
        if own_history:
            HISTORY.save(sid, history)
        return
    if plan.blocking_questions:
        async for evt in _pause(store, plan, facts, prompt=prompt, history=history, ui_state=ui_state):
            yield evt
        if own_history:
            HISTORY.save(sid, history)
        return
    pre_events = [_brain_event("answered", plan.brain), {"type": "plan", "plan": plan.model_dump()}]
    for evt in pre_events:
        yield evt
    if plan.intent in ("undo", "redo"):
        async for evt in _history_step(store, plan, history=history):
            yield evt
        if own_history:
            HISTORY.save(sid, history)
        return
    if plan.is_read_only:
        text = via(BRAIN_LABELS.get(plan.brain, plan.brain)) + (plan.reply or "Nothing to change.")
        history.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
        yield {"type": "text_delta", "text": text}
        yield {"type": "done"}
        if own_history:
            HISTORY.save(sid, history)
        return
    async for evt in _run_and_stream(store, plan, facts, prompt=prompt, history=history, pre_events=pre_events,
                                     consented_downloads=consented):
        yield evt
    if own_history:
        HISTORY.save(sid, history)


__all__ = ["LEGACY_EVENT_TYPES", "PROMPT_EVENT_TYPES", "EVENT_TYPES", "BRAIN_STATUSES",
           "STEP_STATUSES", "VIA_PREFIX", "CLARIFY_TTL_S", "PLANNING_BUDGET_S",
           "TRANSCRIPT_WAIT_S", "VERIFY_RENDER_MAX_DURATION_S", "ROUTES", "LOOPBACK_ONLY_ROUTES",
           "PROMPT_RUNNING_CODE", "PENDING_FILE", "RUNLOG_FILE", "SNAPSHOT_DIR",
           "route", "via", "is_known_event", "unknown_event_types", "configure", "resolve_store",
           "FileHistoryWriter", "HISTORY", "provisional_text", "replace_provisional",
           "PlannerUnavailable", "build_facts_for", "question_text", "apply_history_step",
           "prompt_turn", "resume"]
