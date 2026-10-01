"""Editor Brain routes (EB1-F; spec §8.7, the EB1 subset).

    GET/PUT /api/settings/brain                          brain.enabled (brain_setting.py)
    POST    /api/sessions/{sid}/brain/analyse            → 202 {job_id}: D's analyse in a job
    GET     /api/sessions/{sid}/brain/graph              the current graph, LEAF names only
    GET     /api/sessions/{sid}/brain/decisions/{did}    the frozen EDP, leaf names only
    GET     /api/sessions/{sid}/brain/versions           versions.json + which is live
    POST    /api/sessions/{sid}/brain/versions/{vid}/restore   one `restore_version` op

Mounted by main.py behind the same middleware as every route (Host
allowlist, `PairAuthMiddleware`'s same-origin rule on every write — the
enumeration in tests/test_same_origin_writes.py picks these up). `sid` is
validated by main's store resolver (`is_valid_session_id` + on disk). With
`brain.enabled` off every session route answers 404 `brain_disabled`: the
surfaces are absent, not merely empty. Nothing here changes the EDL except
Restore, which goes through C's versions helper's `commit("restore_version",
…)` of the snapshot tree (brain_seams; F never touches dispatch.py).

The analysis job runs D's synchronous `brain.graph.analyse(session_dir,
sources)` on the render pool (`JOB_MANAGER`, kind `brain_analyse`) with the
session's media paths — never the session lock, so the person keeps editing.
Progress is the job's (`GET /api/jobs/{id}`); the frames the spec names,
`analysis{layer, pct, eta_s}` (service.BRAIN_EVENT_TYPES, `layer` = the layer
being read), are kept on the job's result. ONE read per session at a time: a
second `POST …/brain/analyse` joins the running job (`joined: true`), a forced
rebuild or layer subset is refused 409 `analysis_running`. The read waits (up
to `brain_seams.TRANSCRIPT_WAIT_S`) for an upload's background transcript; a
graph made without it is NOT pinned (`result.pinned` false, `waiting_for`
"transcript"). A finished job pins `<session>/brain/graph/current.json` for
the reads; `POST …/prompt/cancel` cancels it. The graph route reads through
the store's cap and its current-graph rule (no newest-file fallback).
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import brain_setting
from ..agent.prompt import brain_card, brain_seams
from ..brain import schema as _schema
from ..brain import store as _bstore
from .auth import _is_loopback, same_origin
from .hardening import _envelope

router = APIRouter(tags=["brain"])

SETTINGS_ROUTE = "/api/settings/brain"
_NO_STORE = {"Cache-Control": "no-store"}
_MAX_SETTING_BODY = 1024
GRAPH_DIR = ("brain", "graph")
CURRENT_FILE = "current.json"
GID_RE = re.compile(r"^g_[0-9a-f]{12}$")            # lane C's graph id (brain/store.py), as lane D writes it
ANALYSE_KIND = brain_seams.ANALYSE_KIND

_RESOLVE_STORE: Callable[[str], Any] | None = None


def configure(*, resolve_store: Callable[[str], Any]) -> None:
    global _RESOLVE_STORE
    _RESOLVE_STORE = resolve_store


def _store(sid: str) -> Any:
    if _RESOLVE_STORE is None:
        raise HTTPException(503, "brain routes are not configured")
    return _RESOLVE_STORE(sid)


def _refuse(request: Request, status: int, code: str, message: str) -> JSONResponse:
    rid = getattr(request.state, "request_id", uuid.uuid4().hex[:12])
    return _envelope(status=status, code=code, message=message, request_id=rid)


def _disabled(request: Request) -> JSONResponse | None:
    if brain_setting.is_enabled():
        return None
    return _refuse(request, 404, "brain_disabled",
                   "The Editor Brain is off. Turn it on in Settings (brain.enabled) to use it.")


# ---- the setting -------------------------------------------------------------------

def _settings_body() -> dict[str, Any]:
    on, source = brain_setting.enabled()
    return {"enabled": on, "source": source, "default": brain_setting.DEFAULT_ENABLED}


@router.get(SETTINGS_ROUTE)
def get_brain_settings() -> JSONResponse:
    return JSONResponse(_settings_body(), headers=_NO_STORE)


@router.put(SETTINGS_ROUTE)
async def put_brain_settings(request: Request) -> JSONResponse:
    """The same posture as every app-settings write (api/prompt_routes PUT
    /api/settings/prompt): loopback, the app's own origin, JSON only."""
    if not _is_loopback(request):
        raise HTTPException(403, "desktop_only: this setting is changed on the Mac itself.")
    if not same_origin(request):
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
        raise HTTPException(400, 'Send the setting as JSON: {"enabled": true}.') from None
    if not isinstance(data, dict) or set(data) != {"enabled"} or not isinstance(data["enabled"], bool):
        raise HTTPException(400, 'Send exactly one field: {"enabled": true | false}.')
    brain_setting.set_enabled(data["enabled"])
    return JSONResponse(_settings_body(), headers=_NO_STORE)


# ---- analyse ---------------------------------------------------------------------------

class AnalyseRequest(BaseModel):
    layers: list[str] | None = Field(default=None, max_length=8)
    force: bool = False


_sources = brain_seams.session_sources
_accepted_kwargs = brain_seams._accepted_kwargs
analysis_event = brain_seams.analysis_event


@router.post("/api/sessions/{sid}/brain/analyse")
def analyse(sid: str, request: Request, body: AnalyseRequest | None = None):
    off = _disabled(request)
    if off is not None:
        return off
    store = _store(sid)
    req = body or AnalyseRequest()
    try:
        job, joined = brain_seams.submit_analysis(store, layers=req.layers, force=req.force)
    except LookupError as e:
        raise HTTPException(501, str(e)) from e
    except brain_seams.AnalysisBusy as e:
        return _refuse(request, 409, "analysis_running", str(e))
    return JSONResponse(status_code=202, content={"job_id": job.id, "status": job.status, "joined": joined,
                                                  "status_url": f"/api/jobs/{job.id}"})


# ---- graph -------------------------------------------------------------------------------

def _current_gid(session_dir: Path) -> str | None:
    """The session's pinned graph — the store's rule (`current.json` naming a file that exists), with no
    newest-file fallback: the summary never shows a graph the validator treats as absent."""
    gid = _bstore.current_graph_id(session_dir)
    if gid is not None and _bstore.graph_path(session_dir, gid).is_file():
        return gid
    return None


def _layer_names(layers: Any) -> list[str]:
    if isinstance(layers, dict):
        return sorted(str(k) for k in layers)
    if isinstance(layers, list):
        return sorted(str(x.get("name") if isinstance(x, dict) else x) for x in layers)
    return []


def _speakers(raw: Any) -> dict[str, Any]:
    """C's `Graph.speakers` is a list of `GraphSpeaker`; a layer file holds
    `{k, speakers: [...]}`. Either way: k and `{id, role_guess, name}`."""
    rows = raw.get("speakers") if isinstance(raw, dict) else raw
    rows = [s for s in (rows or []) if isinstance(s, dict)] if isinstance(rows, list) else []
    k = raw.get("k") if isinstance(raw, dict) else None
    return {"k": k if isinstance(k, int) else (len(rows) or None),
            "speakers": [{"id": s.get("id"), "role_guess": s.get("role_guess"), "name": s.get("name")} for s in rows]}


def _source_leaf(s: Any) -> str:
    if isinstance(s, dict):
        return str(s.get("leaf") or Path(str(s.get("path") or s.get("key") or "")).name)
    return Path(str(s)).name


def _blockers(graph: dict[str, Any]) -> list[dict[str, str]]:
    """Why no edit may be offered on this graph (`brain.graph.blockers`): a graph without its speech layer."""
    from ..brain.graph import blockers
    return blockers(graph)


def graph_summary(graph: dict[str, Any]) -> dict[str, Any]:
    """The summary the panel and the tests read: ids, layer names and
    status, speakers, angles, counts — with every path reduced to its leaf
    (C's `Graph`: `layers: {name: params}`, `digests`, `sources: [Source]`,
    `speakers: [GraphSpeaker]`, `timings_s`; a layer-status dict reads too)."""
    layers = graph.get("layers")
    status = {}
    if isinstance(layers, dict):
        status = {str(k): (v.get("status") if isinstance(v, dict) else "ok") for k, v in layers.items()}
    body = {
        "id": graph.get("id"), "digest": graph.get("digest"), "digests": graph.get("digests"),
        "layers": _layer_names(layers), "layer_status": status,
        "reference": graph.get("reference"), "content_type": graph.get("content_type"),
        "speakers": _speakers(graph.get("speakers")),
        "angles": graph.get("angles"),
        "scenes": len(graph["scenes"]) if isinstance(graph.get("scenes"), list) else graph.get("scenes"),
        "topics": graph.get("topics") if isinstance(graph.get("topics"), list) else None,
        "timings": graph.get("timings") if graph.get("timings") is not None else graph.get("timings_s"),
        "sources": [_source_leaf(s) for s in (graph.get("sources") or [])],
        "blockers": _blockers(graph),
    }
    return brain_card.scrub_paths(body)


@router.get("/api/sessions/{sid}/brain/graph")
def get_graph(sid: str, request: Request):
    off = _disabled(request)
    if off is not None:
        return off
    store = _store(sid)
    session_dir = Path(store.dir)
    gid = _current_gid(session_dir)
    if gid is None:
        return _refuse(request, 404, "no_graph", "This project's footage has not been read yet.")
    try:
        graph = _bstore.read_json(_bstore.graph_path(session_dir, gid), cap=_schema.MAX_GRAPH_BYTES, what="graph")
    except _bstore.TooLarge:
        return _refuse(request, 413, "graph_too_large", "This project's analysis is too large to show.")
    except (OSError, ValueError):
        return _refuse(request, 404, "no_graph", "This project's footage has not been read yet.")
    if graph is None:
        return _refuse(request, 404, "no_graph", "This project's footage has not been read yet.")
    body = graph_summary(graph if isinstance(graph, dict) else {})
    job = brain_seams.latest_analysis_job(sid)
    body["analysis"] = ({"job_id": job.id, "status": job.status, "progress": job.progress}
                        if job is not None else None)
    return JSONResponse(body, headers=_NO_STORE)


# ---- decisions ----------------------------------------------------------------------

@router.get("/api/sessions/{sid}/brain/decisions/{did}")
def get_decisions(sid: str, did: str, request: Request):
    off = _disabled(request)
    if off is not None:
        return off
    store = _store(sid)
    if not brain_card.DID_RE.match(did):
        return _refuse(request, 404, "no_decisions", "No such edit plan.")
    try:
        edp = _bstore.read_json(_bstore.edp_path(Path(store.dir), did), cap=_schema.MAX_GRAPH_BYTES, what="decisions")
    except _bstore.TooLarge:
        return _refuse(request, 413, "decisions_too_large", "That edit plan is too large to show.")
    except (OSError, ValueError):
        edp = None
    if not isinstance(edp, dict) or edp.get("id") != did:
        return _refuse(request, 404, "no_decisions", "No such edit plan.")
    return JSONResponse(brain_card.scrub_paths(edp), headers=_NO_STORE)


# ---- versions -----------------------------------------------------------------------

def _version_rows(store: Any) -> list[dict[str, Any]]:
    api = brain_seams.versions()
    session_dir = Path(store.dir)
    live = store.edl.hash()
    rows = []
    for r in api.list(session_dir):
        snap = getattr(api, "snapshot_for", None)
        restorable = bool(snap(session_dir, r)) if callable(snap) else bool(r.get("restorable", True))
        rows.append({**r, "current": r.get("edl_hash") == live, "restorable": restorable})
    return rows


@router.get("/api/sessions/{sid}/brain/versions")
def get_versions(sid: str, request: Request):
    off = _disabled(request)
    if off is not None:
        return off
    store = _store(sid)
    return JSONResponse({"versions": _version_rows(store), "live_hash": store.edl.hash()}, headers=_NO_STORE)


@router.post("/api/sessions/{sid}/brain/versions/{vid}/restore")
def restore_version(sid: str, vid: str, request: Request):
    off = _disabled(request)
    if off is not None:
        return off
    store = _store(sid)
    from . import locks
    from .prompt_routes import prompt_running_response
    busy = prompt_running_response(sid)
    if busy is not None:
        return busy
    with locks.session_lock(sid):
        try:
            out = brain_seams.versions().restore(store, vid)
        except brain_seams.UnknownVersionError:
            return _refuse(request, 404, "unknown_version", "No such version.")
        except brain_seams.VersionNotRestorable:
            return _refuse(request, 409, "no_longer_restorable",
                           "That version's snapshot was pruned; it is no longer restorable.")
    return JSONResponse({"op": out.get("op"), "version": out.get("version"), "edl_hash": store.edl.hash()},
                        headers=_NO_STORE)


__all__ = ["router", "configure", "SETTINGS_ROUTE", "ANALYSE_KIND", "analysis_event", "graph_summary"]
