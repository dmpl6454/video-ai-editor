"""`/api/sessions/{sid}/prompt*` and `/api/prompt/*` (spec §4.7).

Mounted by main.py after the chat route, behind the same middleware
(`PairAuthMiddleware`, the rate limiter) — no new listener, no new
middleware. `configure(resolve_store=…)` hands the routes main's LRU-cached
store resolver; the SSE routes stream `agent.prompt.service` exactly the way
`/chat` streams `chat_turn`: plain `data: <json>\\n\\n` frames, `done` last.

The two model routes are the ONLY network-egress paths of the whole feature
(a model download) and refuse non-loopback callers with 403 — a paired phone
must not be able to start a 4 GB download on the Mac.
"""
from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..agent.prompt import service
from . import locks
from .auth import _is_loopback

router = APIRouter(tags=["prompt"])

_RESOLVE_STORE: Callable[[str], Any] | None = None


def configure(*, resolve_store: Callable[[str], Any]) -> None:
    """Called once by main.py. The service shares the resolver so the run
    thread re-resolves the store inside the session lock (§4.2)."""
    global _RESOLVE_STORE
    _RESOLVE_STORE = resolve_store
    service.configure(resolve_store=resolve_store)


def _store(sid: str) -> Any:
    if _RESOLVE_STORE is None:
        raise HTTPException(503, "prompt routes are not configured")
    return _RESOLVE_STORE(sid)


class PromptRequest(BaseModel):
    message: str = Field(default="", max_length=4000)
    selection: str | None = None
    multi_selection: list[str] = []
    playhead: float | None = None
    brain: str | None = None
    resume_run: str | None = None
    #: With `resume_run`: replay the bus from this frame index (the desktop
    #: keeps the count of frames it already rendered; 0 replays everything).
    from_index: int = Field(default=0, ge=0)


class AnswerRequest(BaseModel):
    token: str = Field(min_length=1, max_length=64)
    answers: dict[str, Any] = Field(default_factory=dict)
    #: A preview card's Apply (true) / Change (false) — the same as
    #: `answers: {"apply": "yes" | "no"}` (agent/prompt/preview.py).
    apply: bool | None = None


class CancelRequest(BaseModel):
    token: str | None = None


class ModelRequest(BaseModel):
    id: str = Field(min_length=1, max_length=200)


def _ui_state(body: PromptRequest) -> dict | None:
    if body.selection or body.playhead is not None:
        return {"selection": body.selection, "multi_selection": body.multi_selection,
                "playhead": body.playhead}
    return None


def _sse(gen: AsyncIterator[dict], *, on_finish: Callable[[], None] | None = None) -> StreamingResponse:
    async def _frames():
        try:
            async for evt in gen:
                yield f"data: {json.dumps(evt)}\n\n"
        except Exception as e:  # noqa: BLE001 — the stream must end with a frame, not a traceback
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        finally:
            if on_finish is not None:
                on_finish()
    return StreamingResponse(_frames(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post(service.route("prompt"))
async def prompt(sid: str, body: PromptRequest):
    store = _store(sid)
    if body.resume_run:
        return _sse(service.prompt_turn(store, "", [], resume_run=body.resume_run,
                                        from_index=body.from_index))
    if not body.message.strip():
        raise HTTPException(400, "message is required")
    history = service.HISTORY.load(sid)
    gen = service.prompt_turn(store, body.message, history, ui_state=_ui_state(body), brain=body.brain)
    return _sse(gen, on_finish=lambda: service.HISTORY.save(sid, history))


@router.post(service.route("answer"))
async def answer(sid: str, body: AnswerRequest):
    store = _store(sid)
    answers = dict(body.answers)
    if body.apply is not None:
        answers["apply"] = "yes" if body.apply else "no"
    return _sse(service.resume(store, body.token, answers))


@router.get(service.route("pending"))
def pending(sid: str):
    from ..agent.prompt import pending as _pending
    store = _store(sid)
    record = _pending.load_pending(store.dir)
    if record is None:
        return {"pending": None}
    valid, why = _pending.pending_is_valid(record, None)
    if not valid:
        _pending.clear_pending(store.dir)
        return {"pending": None, "dropped": why}
    plan = _pending.pending_plan(record)
    from ..agent.prompt.preview import public_view
    body = {"token": record["token"], "plan_id": plan.id, "prompt": record.get("prompt"),
            "brain": record.get("brain"),
            "questions": [q.model_dump() for q in plan.blocking_questions],
            "expires_in_s": max(0, int(float(record["expires"]) - time.time()))}
    view = public_view(record)
    if view is not None:
        body["preview"] = view
    return {"pending": body}


@router.get(service.route("run"))
def run(sid: str):
    from ..agent.prompt import executor, runlog
    store = _store(sid)
    record = runlog.read_record(store.dir)
    handle = executor.get_run(sid)
    live = handle is not None and handle.run_id == (record or {}).get("run_id")
    return {"run": record, "live": live and not handle.finished,
            "replayable": live}


@router.post(service.route("cancel"))
def cancel(sid: str, body: CancelRequest | None = None):
    from ..agent.prompt import executor
    from ..agent.prompt import pending as _pending
    store = _store(sid)
    cancelled: dict[str, Any] = {"run": None, "pending": None}
    handle = executor.active_run(sid)
    if handle is not None:
        handle.cancel()
        cancelled["run"] = handle.run_id
    record = _pending.load_pending(store.dir)
    token = body.token if body else None
    if record is not None and (token is None or record.get("token") == token):
        _pending.clear_pending(store.dir)
        cancelled["pending"] = record.get("token")
    return {"cancelled": cancelled}


# ---- "Ask before applying Prompt bar edits" (0.8.0, prompt_setting.py) ------

SETTINGS_ROUTE = "/api/settings/prompt"
_MAX_SETTING_BODY = 1024


def _prompt_settings_body() -> dict[str, Any]:
    from .. import prompt_setting
    on, source = prompt_setting.confirm_before_apply()
    return {"confirm_before_apply": on, "source": source, "default": prompt_setting.DEFAULT_CONFIRM}


@router.get(SETTINGS_ROUTE)
def get_prompt_settings() -> JSONResponse:
    return JSONResponse(_prompt_settings_body(), headers={"Cache-Control": "no-store"})


@router.put(SETTINGS_ROUTE)
async def put_prompt_settings(request: Request) -> JSONResponse:
    """Write the setting. A state change, so the same posture as every other
    app-settings write (api/preview_routes PUT /api/settings/preview):
    loopback only, the app's own origin only, `application/json` only."""
    from .. import prompt_setting
    from .settings_routes import _same_origin
    if not _is_loopback(request):
        raise HTTPException(403, "desktop_only: this setting is changed on the Mac itself.")
    if not _same_origin(request):
        raise HTTPException(403, "desktop_only: this setting is changed from the app's own window.")
    ctype = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if ctype != "application/json":
        raise HTTPException(415, "Send the setting as application/json.")
    raw = await request.body()
    if len(raw) > _MAX_SETTING_BODY:
        raise HTTPException(413, "That request is far too large for one setting.")
    try:
        data = json.loads(raw.decode("utf-8") or "null")
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(400, 'Send the setting as JSON: {"confirm_before_apply": true}.') from None
    if not isinstance(data, dict) or set(data) != {"confirm_before_apply"} \
            or not isinstance(data["confirm_before_apply"], bool):
        raise HTTPException(400, 'Send exactly one field: {"confirm_before_apply": true | false}.')
    prompt_setting.set_confirm_before_apply(data["confirm_before_apply"])
    return JSONResponse(_prompt_settings_body(), headers={"Cache-Control": "no-store"})


def _brains_report(refresh: bool) -> dict[str, Any]:
    """`brains_report()` from B's router (it memoises 60 s itself and honours
    `refresh`). When the router is not installed the answer says exactly
    that, with the recipes brain reported from what IS importable — an
    honest ladder, never a 500."""
    try:
        from ..agent.prompt.brains import router as _router
    except ImportError:
        return _fallback_brains_report()
    return _router.brains_report(refresh=refresh)


def _fallback_brains_report() -> dict[str, Any]:
    """Same payload shape as `router.brains_report` (§0.2 contract: ladder,
    pinned, best_available, brains[], cloud_allowed, machine, generated_at)
    so the badge renders unchanged on a build without the router."""
    import importlib
    import platform
    import sys
    from ..agent.prompt.brains.base import BRAIN_IDS, BRAIN_LABELS, available, unavailable
    try:
        importlib.import_module("video_ai_editor.agent.prompt.planner")
        recipes = available("grammar + recipe table")
    except ImportError:
        recipes = unavailable("planner module missing", "install agent/prompt/planner.py",
                              action="install")
    missing = unavailable("brains router not installed",
                          "agent/prompt/brains/router.py is missing from this build", action="install")
    rows = [{"id": b, "label": BRAIN_LABELS[b], "order": i, "in_ladder": b == "recipes",
             **(recipes if b == "recipes" else missing)} for i, b in enumerate(BRAIN_IDS)]
    return {
        "ladder": ["recipes"], "pinned": None, "best_available": "recipes", "brains": rows,
        "cloud_allowed": False,
        "machine": {"os": platform.mac_ver()[0] or platform.platform(), "arch": platform.machine(),
                    "ram_gb": None, "frozen": bool(getattr(sys, "frozen", False))},
        "generated_at": time.time(),
        "detail": "brains router not installed; only the recipes brain can answer",
    }


@router.get(service.route("brains"))
def brains(refresh: int = 0):
    return _brains_report(bool(refresh))


def _models_module():
    try:
        from ..agent.prompt import models as _models
    except ImportError:
        return None
    return _models


@router.get(service.route("models"))
def models():
    mod = _models_module()
    if mod is None:
        return {"tier": None, "models": [],
                "detail": "local model support is not installed in this build (agent/prompt/models.py)"}
    return mod.list_models()


def _loopback_only(request: Request) -> None:
    # A string detail: the hardening envelope (`api/hardening._http_exc`)
    # forwards only string details as the message.
    if not _is_loopback(request):
        raise HTTPException(403, "desktop_only: model downloads can only be started from the Mac itself.")


def _model_job(request: Request, body: ModelRequest, action: str) -> JSONResponse:
    _loopback_only(request)
    mod = _models_module()
    if mod is None:
        raise HTTPException(501, "local model support is not installed in this build")
    fn = getattr(mod, action, None)
    if fn is None:
        raise HTTPException(501, f"model {action} is not supported by this build")
    from .jobs import JOB_MANAGER

    def _job(set_progress=None, cancel_event=None) -> dict:
        return fn(body.id, set_progress=set_progress, cancel_event=cancel_event)

    job = JOB_MANAGER.submit(kind=f"model_{action}", fn=_job)
    return JSONResponse(status_code=202, content={"job_id": job.id, "status": job.status,
                                                  "status_url": f"/api/jobs/{job.id}"})


@router.post(service.route("models_download"))
def models_download(request: Request, body: ModelRequest):
    return _model_job(request, body, "download")


@router.post(service.route("models_delete"))
def models_delete(request: Request, body: ModelRequest):
    return _model_job(request, body, "delete")


def prompt_running_response(sid: str) -> JSONResponse | None:
    """`409 {"code": "prompt_running", "run_id": …}` when a prompt run holds
    `sid` (§4.2); None otherwise. main's `/dispatch` calls this BEFORE
    blocking on the session lock."""
    run_id = locks.prompt_run(sid)
    if run_id is None:
        return None
    return JSONResponse(status_code=409, content={
        "code": service.PROMPT_RUNNING_CODE, "run_id": run_id,
        "message": "Prompt running — wait or cancel.",
        "cancel_url": service.route("cancel", sid)})


__all__ = ["router", "configure", "prompt_running_response", "PromptRequest", "AnswerRequest",
           "CancelRequest", "ModelRequest"]
