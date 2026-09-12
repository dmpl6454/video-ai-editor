"""FastAPI app.

M1 routes:
  GET  /api/health
  POST /api/sessions                          → create empty session
  GET  /api/sessions                          → list sessions
  GET  /api/sessions/{sid}                    → session info + EDL summary + ops
  POST /api/sessions/{sid}/upload             → upload + ingest a video
  POST /api/sessions/{sid}/dispatch           → call a tool { tool, args }
  GET  /api/sessions/{sid}/edl                → full EDL JSON
  GET  /api/sessions/{sid}/ops                → ops log
  GET  /api/sessions/{sid}/transcript         → transcript (first source)
  POST /api/sessions/{sid}/preview            → render preview, returns path
  GET  /api/sessions/{sid}/preview.mp4        → stream current preview
  POST /api/sessions/{sid}/export             → render export, returns path
  GET  /api/sessions/{sid}/files/{kind}/{name}→ stream session-scoped media

M2 routes:
  POST /api/sessions/{sid}/chat               → SSE-stream a chat turn (Claude with a key, local brains without)
  GET  /api/sessions/{sid}/history            → chat history

Prompt Editor routes (api/prompt_routes.py, spec §4.7):
  POST /api/sessions/{sid}/prompt             → SSE-stream a prompt run
  POST /api/sessions/{sid}/prompt/answer      → resume a paused plan
  GET  /api/sessions/{sid}/prompt/pending     → the open clarification
  GET  /api/sessions/{sid}/prompt/run         → prompt_run.json (reconnect)
  POST /api/sessions/{sid}/prompt/cancel      → cancel the run / drop the question
  GET  /api/prompt/brains, /api/prompt/models, POST /api/prompt/models/{download,delete}
"""
from __future__ import annotations
import asyncio
import inspect
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from fastapi import FastAPI, UploadFile, File, HTTPException, Form, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import platformutil as _pu
from .config import WORKDIR, DEFAULT_CANVAS
from .storage import (new_session_id, session_dir, session_exists,
                       list_sessions, write_meta, read_meta, delete_session,
                       is_valid_session_id)
from .edl import EDLStore
from .edl.schema import Canvas, Clip
from .ingest import ingest_upload
from .render import render_preview, render_export
from .agent.dispatch import DISPATCH, dispatch, list_tools
from .agent.loop import chat_turn
from .api.uploads import (assert_room_for as _assert_room_for,
                          stream_upload_to as _stream_upload_to)

@asynccontextmanager
async def _lifespan(_app: "FastAPI"):
    _validate_ai_config()
    yield


app = FastAPI(title="video-ai-editor", lifespan=_lifespan)

# Production hardening: request IDs, structured JSON logging, error envelope,
# /livez + /readyz + /metrics, sliding-window rate limit. Idempotent.
from .api.hardening import install as _install_hardening, METRICS, get_logger
_install_hardening(app)

# Phone companion: bearer auth + Host validation + the upload cap, and the
# /api/pair/* routes behind them. The position of this block is load-bearing.
# Starlette runs the LAST-added middleware FIRST, so installing here — after
# hardening, before CORS — produces:
#     CORS -> PairAuth -> UploadLimit -> RequestContext(rate limit) -> route
# Move it below the CORS block and an OPTIONS preflight hits auth, 401s, and
# every browser request fails citing CORS instead of the real cause. Fold it
# into hardening.install() and auth ends up INSIDE the rate limiter, so a flood
# of unauthenticated requests is answered as "too many" rather than "not
# paired". All of this is inert until LAN mode is on (api/auth.py explains the
# gating): with VAE_LAN unset and no settings.json the desktop behaves exactly
# as it did in 0.5.0.
#
# The router stays MOUNTED even though the phone feature is temporarily gated
# off for this ship (api/pairing.py::PHONE_PAIRING_ENABLED). Unmounting it would
# make /api/pair/* an unrouted path — Starlette's bare 404, outside this app's
# error envelope and outside its request log. Mounted + one router-level
# dependency (pair_routes._require_phone_feature) gives a real, logged,
# enveloped 404 instead, and re-enabling the feature touches no wiring here.
from .api import pairing
from .api.auth import install as _install_pair_auth
from .api.pair_routes import router as _pair_router
_install_pair_auth(app)
app.include_router(_pair_router)

# --- the published schema has to agree with the gate -------------------------
# The mounted router makes /api/pair/* a routed 404 (above) — but it also puts
# all eight paths, and the request models behind them, into /openapi.json, /docs
# and /redoc. That contradicts the very signal the 404 exists to send: a client
# generated from the schema, or a reader opening /docs to see what this build
# offers, would get eight endpoints that every real verb answers 404 on, i.e.
# "pairing is present and merely broken". While the gate is closed the schema
# must say what is true: there is no phone companion in this build.
#
# `include_in_schema=False` on the router is the cheaper edit and was rejected:
# it also hides the routes with the flag ON, losing real documentation of a real
# feature. Filtering at publish time is posture-aware, so flipping the flag back
# on restores the docs with no further change.
_SCHEMA_REF_PREFIX = "#/components/schemas/"


def _collect_schema_refs(node: object, found: set[str]) -> None:
    """Every `#/components/schemas/X` name reachable from `node`, into `found`."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(_SCHEMA_REF_PREFIX):
            found.add(ref[len(_SCHEMA_REF_PREFIX):])
        for value in node.values():
            _collect_schema_refs(value, found)
        return
    if isinstance(node, list):
        for value in node:
            _collect_schema_refs(value, found)


def _without_pair_paths(schema: dict) -> dict:
    """A copy of `schema` with the pair paths, and the models only they use, gone.

    The component sweep is the load-bearing half: dropping the paths alone would
    still publish `LanRequest.enabled`, `ClaimRequest.code` and
    `RevokeRequest.device_id` under components/schemas — the shape of the feature
    this build says it does not have. Reachability rather than a hardcoded name
    list, so a model shared with a route that stays (HTTPValidationError) is
    kept, and a pair model added later is dropped without anyone remembering to.

    Pure and immutable: FastAPI memoises the unfiltered schema in
    `app.openapi_schema`, and mutating that cached dict would make the first
    posture served permanent.
    """
    paths = {path: item for path, item in schema.get("paths", {}).items()
             if not path.startswith("/api/pair/")}
    components = schema.get("components") or {}
    schemas = components.get("schemas") or {}
    if not schemas:
        return {**schema, "paths": paths}

    reachable: set[str] = set()
    _collect_schema_refs(paths, reachable)
    while True:                                   # a kept model may cite another
        grown = set(reachable)
        _collect_schema_refs({n: schemas[n] for n in reachable if n in schemas}, grown)
        if grown == reachable:
            break
        reachable = grown

    kept = {name: body for name, body in schemas.items() if name in reachable}
    return {**schema, "paths": paths, "components": {**components, "schemas": kept}}


_fastapi_openapi = app.openapi


def app_openapi() -> dict:
    """/openapi.json for the posture this process is actually in.

    Deliberately NOT memoised into `app.openapi_schema`: the gate is read at call
    time (`phone_pairing_enabled()` consults the environment on every call, which
    is what lets the test suite flip it per test), so caching the filtered result
    would freeze whichever posture answered first.
    """
    schema = _fastapi_openapi()
    if pairing.phone_pairing_enabled():
        return schema
    return _without_pair_paths(schema)


app.openapi = app_openapi  # type: ignore[method-assign]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)


def _validate_ai_config() -> None:
    """Say at boot which brains chat will run on, and warn if a key looks wrong.

    A missing key is NOT a degraded state any more: chat and the Prompt bar
    plan and edit on the local brains (agent/prompt/), so it logs at `info`.
    A malformed key stays a warning — that one produces a confusing first
    chat that fails with an auth error mid-conversation.
    """
    from .config import ANTHROPIC_API_KEY
    log = get_logger()
    if not ANTHROPIC_API_KEY:
        log.info(
            "ANTHROPIC_API_KEY is not set — chat and the Prompt bar run on local "
            "brains (recipes / Apple Intelligence / local model). Add a key to "
            "enable Claude."
        )
    elif not ANTHROPIC_API_KEY.startswith("sk-"):
        log.warning(
            "ANTHROPIC_API_KEY does not look like a valid key (expected an "
            "'sk-' prefix). AI chat may fail with an authentication error."
        )


# LRU-bounded in-memory store cache. Without a bound, every distinct session ID
# the server has ever seen stays in memory forever; a long-running multi-user
# instance leaks ~MB per session indefinitely. OrderedDict gives us O(1) LRU
# semantics with the same `dict`-shaped API the rest of the file expects.
#
# Lock guards the get-or-create — without it, two concurrent requests for the
# same session can both see "missing", both build an EDLStore, and the loser's
# in-memory edits get silently overwritten on the next dispatch. FastAPI runs
# sync endpoints in a threadpool so this is reachable under any real load.
import os as _os
from collections import OrderedDict
_STORES_MAX = int(_os.environ.get("VAI_STORES_CACHE_MAX", "64"))
_STORES: OrderedDict[str, EDLStore] = OrderedDict()
_STORES_LOCK = threading.Lock()


def _stores_evict_if_full() -> None:
    """Caller must hold _STORES_LOCK. Evicts the LRU entry if at cap."""
    while len(_STORES) >= _STORES_MAX:
        evicted_sid, _ = _STORES.popitem(last=False)
        # No flush needed — every commit() already wrote to disk; the in-memory
        # store is just a cache. Re-loaded lazily on next access.
        del evicted_sid


def _safe_filename(name: str | None, fallback: str) -> str:
    """Strip ffmpeg-hostile characters from a user-supplied filename.

    ffmpeg passes paths to filter sub-modules (lut3d=, subtitles=, drawtext=
    text from file, even some demuxer probes) by interpolating them into the
    filter_complex string. Special chars in those paths break the parser
    even when the file is referenced via a separate `-i` argv. Easier to
    strip the chars at upload than to escape every downstream codepath.

    Strips: : ' [ ] , ; ` $ ( ) * ? & < > | \\ \" + spaces.
    Keeps: A-Z a-z 0-9 . _ - and one final extension.
    """
    raw = Path(name or fallback).name  # path-traversal guard
    stem = Path(raw).stem
    suffix = Path(raw).suffix.lower()
    import re as _re
    stem_clean = _re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._- ")
    # Collapse runs of underscore/dash that the sub above can leave behind.
    stem_clean = _re.sub(r"[_-]{2,}", "_", stem_clean).strip("._- ")
    suffix_clean = _re.sub(r"[^A-Za-z0-9.]+", "", suffix)
    if not stem_clean:
        # Falls back to fallback's stem + a short hash of the original so two
        # all-non-ASCII filenames (Hindi/CJK) don't collide on disk.
        import hashlib as _h
        sig = _h.sha1((name or "").encode("utf-8")).hexdigest()[:6]
        stem_clean = f"{Path(fallback).stem}_{sig}"
    return f"{stem_clean}{suffix_clean}" or fallback


# One mutation at a time per session. The registry lives in api/locks.py so
# the Prompt Editor's run thread (agent/prompt/executor.py) takes the SAME
# lock as `/dispatch` and the job workers without importing this module; the
# rationale (FastAPI's threadpool + a shared EDLStore) is written there.
from .api import locks as _locks
_session_lock = _locks.session_lock


def _store(sid: str) -> EDLStore:
    # Fast path: already cached. Mark as recently used.
    with _STORES_LOCK:
        cached = _STORES.get(sid)
        if cached is not None:
            _STORES.move_to_end(sid)
            return cached
        if not session_exists(sid):
            raise HTTPException(404, f"session {sid} not found")
        # OK to create the dir tree now (subdirs etc.); we already proved the
        # session was real.
        _stores_evict_if_full()
        _STORES[sid] = EDLStore(session_dir(sid))
        return _STORES[sid]


# --- shared models ---

class DispatchRequest(BaseModel):
    tool: str
    args: dict[str, Any] = {}


class CreateSessionRequest(BaseModel):
    name: str | None = None


class ExportRequest(BaseModel):
    height: int | None = None
    fps: int | None = None
    crf: int = 18
    container: Literal["mp4", "mov"] = "mp4"


# --- routes ---

@app.get("/api/health")
def health():
    # `max_upload_bytes` is advertised here, not just enforced at the ingress,
    # so the phone's Import screen can refuse a too-large pick locally instead
    # of spending five minutes of the user's battery pushing bytes at a server
    # that will answer 413 at the end.
    from .config import APP_VERSION
    from .api.uploads import max_upload_bytes
    return {"ok": True, "version": APP_VERSION,
            "max_upload_bytes": max_upload_bytes()}


@app.get("/api/version")
def version():
    # `build` is what makes a bug report actionable — VERSION alone stayed at
    # 0.3.7 across 99 commits, so "I'm on v0.3.7" identified nothing. See
    # config.build_id().
    #
    # `phone_pairing` is the ONE channel the frontend uses to decide whether the
    # iPhone affordance exists at all. It ships False — the companion is
    # temporarily gated off (api/pairing.py::PHONE_PAIRING_ENABLED) — and the UI
    # must read it here rather than infer the feature from a 404, so turning the
    # flag back on lights the panel up with no frontend change. Kept cheap
    # because the top bar polls this: both modules are already imported by the
    # time a request can arrive, and the call is an env lookup plus a bool.
    from .api.pairing import phone_pairing_enabled
    from .config import APP_VERSION, build_id
    return {"version": APP_VERSION, "build": build_id(),
            "phone_pairing": phone_pairing_enabled()}


def _handler_hook_flags(name: str) -> dict:
    """Which optional job hooks a handler actually observes. dispatch() injects
    `set_progress`/`cancel_event` by signature (agent/dispatch.py::dispatch), so
    the signature IS the contract: a tool without `cancel_event` keeps running
    after POST /jobs/{id}/cancel and commits anyway, and one without
    `set_progress` sits at 0.0 until it completes. The UI must not offer
    Cancel or a % bar for those — advertising the flags is how it knows."""
    fn = DISPATCH.get(name)
    try:
        params = inspect.signature(fn).parameters if fn else {}
    except (TypeError, ValueError):        # builtins / C callables
        params = {}
    return {"cancellable": "cancel_event" in params,
            "reports_progress": "set_progress" in params}


@app.get("/api/tools")
def tools():
    return {"tools": [{**t, **_handler_hook_flags(t["name"])} for t in list_tools()]}


@app.get("/api/features")
def features(refresh: int = 0):
    """The `check_features` payload (ai/features.py::feature_report) over HTTP,
    so the AI panel can grey a tool out BEFORE the click and show the exact
    `fix` string instead of a 422 afterwards. Memoised in
    `ai.features.cached_feature_report` (shared with the Prompt Editor's
    planner, which reads `tools_available` from it on every turn): the probes
    import six ai.* modules and resolve a torch device — measured 2.2 s cold —
    and the answer only changes when someone installs something, which is
    what `?refresh=1` (the panel's Refresh button) is for."""
    from .ai.features import cached_feature_report
    return cached_feature_report(refresh=bool(refresh))


# ---- MCP server: let external agents (Claude Code / Cursor / Codex) drive the
# editor over HTTP. Connect with:
#   claude mcp add --transport http video-ai-editor http://127.0.0.1:8000/mcp
from .agent import mcp_server as _mcp

# The single "active" session the MCP server drives, created lazily. An agent
# can also target any session by passing session_id in a tool's arguments.
_MCP_ACTIVE_SESSION: dict[str, str] = {}


def _mcp_resolve_store(session_id: str | None):
    """(EDLStore, resolved_sid). None session_id → the active MCP session,
    created on first use so an agent can connect and start editing immediately."""
    if session_id:
        return _store(session_id), session_id
    sid = _MCP_ACTIVE_SESSION.get("id")
    if sid and session_exists(sid):
        return _store(sid), sid
    # Create a fresh MCP session.
    sid = new_session_id()
    d = session_dir(sid)
    write_meta(sid, {"name": "MCP session"})
    store = EDLStore(d)
    store.commit("init", {}, "Initial empty project")
    with _STORES_LOCK:
        _stores_evict_if_full()
        _STORES[sid] = store
    _MCP_ACTIVE_SESSION["id"] = sid
    return store, sid


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32700, "message": "parse error"}},
            status_code=400,
        )
    resp = _mcp.handle_request(body, resolve_store=_mcp_resolve_store)
    if resp is None:
        # All notifications → MCP spec says return 202 with no body.
        return Response(status_code=202)
    return JSONResponse(resp)


@app.get("/mcp")
def mcp_probe():
    """Some MCP clients GET the endpoint to check liveness before POSTing."""
    return {"server": _mcp.SERVER_INFO, "protocolVersion": _mcp.PROTOCOL_VERSION,
            "transport": "http", "active_session": _MCP_ACTIVE_SESSION.get("id")}


@app.post("/api/sessions")
def create_session(body: CreateSessionRequest | None = None):
    sid = new_session_id()
    d = session_dir(sid)
    name = (body.name if body and body.name else sid)
    write_meta(sid, {"name": name})
    # Initialize the EDL store so edl.json exists
    store = EDLStore(d)
    store.commit("init", {}, "Initial empty project")
    with _STORES_LOCK:
        _stores_evict_if_full()
        _STORES[sid] = store
    return {"id": sid, "name": name}


@app.get("/api/sessions")
def list_all():
    return {"sessions": list_sessions()}


@app.get("/api/sessions/{sid}")
def get_session(sid: str):
    store = _store(sid)
    meta = read_meta(sid)
    return {
        "id": sid,
        "name": meta.get("name", sid),
        "summary": dispatch(store, "get_timeline", {"summary": True}),
        "ops": [op.model_dump() for op in store.ops.ops[-25:]],
        "redo_available": store.redo_available,
    }


@app.delete("/api/sessions/{sid}")
def delete_session_route(sid: str):
    # sid is untrusted path input; reject anything that isn't a well-formed
    # session id before it ever reaches a filesystem delete (path traversal /
    # arbitrary directory deletion — see storage.delete_session's own
    # belt-and-suspenders check for the second layer of defense).
    if not is_valid_session_id(sid):
        raise HTTPException(400, {"code": "invalid_sid", "message": "invalid session id"})
    with _STORES_LOCK:
        _STORES.pop(sid, None)  # drop the cached store so it can't resurrect the dir
    existed = delete_session(sid)
    if not existed:
        raise HTTPException(404, {"code": "not_found", "message": "session not found"})
    return {"deleted": sid}


# The five upload ingresses below mutate the shared EDLStore like /dispatch
# does, so they follow the same two rules (spec §4.2): answer `409
# prompt_running` while a prompt run holds the session, and take the session
# lock around the edit. Without them an upload landing DURING a run's
# `store.batch()` had its commit swallowed (`EDLStore.commit` returns None
# inside a batch) — the user's own edit was either folded into the run's one
# op (so ⌘Z removed it) or wiped by the run's rollback, after the route had
# already answered 200. Verified by executing `commit` inside `batch()`.
#
# The lock is taken on a worker thread (`asyncio.to_thread`): these routes are
# `async def`, and a `threading.Lock` held by a job worker would otherwise
# stall the whole event loop.
async def _locked_edit(sid: str, edit):
    def _run():
        with _session_lock(sid):
            return edit()
    return await asyncio.to_thread(_run)


@app.post("/api/sessions/{sid}/vo_record")
async def vo_record(sid: str, request: Request, file: UploadFile = File(...),
                    start: float = Form(0.0), gain_db: float = Form(0.0)):
    """Receive a recorded mic blob (WebM/Opus or WAV) and add it to the vo track.

    Browser MediaRecorder typically produces audio/webm;codecs=opus. We trans-
    code to a session-local AAC mp4 for clean playback in the timeline pipeline.
    """
    busy = _prompt_running_response(sid)
    if busy is not None:
        return busy
    sd = session_dir(sid)
    vo_dir = sd / "uploads" / "vo"
    vo_dir.mkdir(parents=True, exist_ok=True)
    _assert_room_for(request, vo_dir)
    safe_name = _safe_filename(file.filename, "vo.webm")
    raw = vo_dir / f"raw_{safe_name}"
    await _stream_upload_to(file, raw)

    # Normalize to AAC mp4 so the audio mixer can splice it cleanly
    norm = vo_dir / f"vo_{int(time.time())}.m4a"
    proc = subprocess.run(
        [_pu.FFMPEG, "-y", "-i", str(raw),
         "-vn", "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000",
         str(norm)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        **_pu.SUBPROCESS_FLAGS,
    )
    if proc.returncode != 0:
        raise HTTPException(422, {"error": f"vo transcode failed: {proc.stderr[-800:]}"})
    raw.unlink(missing_ok=True)

    # Probe duration
    from .ingest.probe import probe as _probe
    try:
        p = _probe(norm)
    except Exception as e:
        raise HTTPException(422, {"error": str(e)})

    store = _store(sid)
    from .edl.schema import Track, Clip, AudioProps

    def _edit():
        track = store.edl.get_track("vo")
        if not track:
            track = Track(id="vo", type="vo", z=0, label="Voiceover")
            store.edl.tracks.append(track)
        clip = Clip(
            src=str(norm), in_=0.0, out=p.duration, start=float(start),
            audio=AudioProps(gain_db=float(gain_db), fade_in=0.05, fade_out=0.1),
        )
        track.clips.append(clip)
        summary = f"Voiceover {p.duration:.1f}s @ {start:.1f}s ({float(gain_db):+.1f} dB)"
        store.commit("vo_record", {"start": start, "gain_db": gain_db}, summary)
        return {"clip_id": clip.id, "src": str(norm), "duration": p.duration,
                "summary": summary, "edl_hash": store.edl.hash()}

    return await _locked_edit(sid, _edit)


@app.post("/api/sessions/{sid}/sticker_upload")
async def sticker_upload(sid: str, request: Request, file: UploadFile = File(...),
                         add_at_playhead: bool = Form(False),
                         playhead: float = Form(0.0)):
    """Upload a PNG (or other image) and optionally drop it as a sticker."""
    busy = _prompt_running_response(sid)
    if busy is not None:
        return busy
    sd = session_dir(sid)
    sticker_dir = sd / "uploads" / "stickers"
    sticker_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_filename(file.filename, "sticker.png")
    dst = sticker_dir / safe_name
    # `UploadLimitMiddleware` short-circuits on `if raw and ...`, so a body sent
    # with `Transfer-Encoding: chunked` and no Content-Length skips it entirely.
    # Without the two calls below this route had no second layer at all and a
    # chunked multipart body would fill the volume — the exact failure
    # api/uploads.py's docstring claims is closed on all six ingresses.
    _assert_room_for(request, sticker_dir)
    await _stream_upload_to(file, dst)
    info = {"src": str(dst), "filename": safe_name}
    if add_at_playhead:
        store = _store(sid)

        def _edit():
            canvas = store.edl.canvas
            dispatch(store, "add_sticker", {
                "src": str(dst),
                "start": float(playhead),
                "end": float(playhead) + 3.0,
                "position": [canvas.w / 2, canvas.h * 0.55],
                "scale": 1.0,
            })
            return store.edl.hash()

        info["edl_hash"] = await _locked_edit(sid, _edit)
    return info


@app.post("/api/sessions/{sid}/audio_upload")
async def audio_upload(sid: str, request: Request, file: UploadFile = File(...),
                       add_to_music: bool = Form(True),
                       duck: bool = Form(True),
                       volume_db: float = Form(-12.0)):
    """Upload an audio file (mp3/wav/m4a) and optionally append to the music track."""
    busy = _prompt_running_response(sid)
    if busy is not None:
        return busy
    store = _store(sid)
    sd = session_dir(sid)
    audio_dir = sd / "uploads" / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    _assert_room_for(request, audio_dir)
    safe_name = _safe_filename(file.filename, "audio.mp3")
    dst = audio_dir / safe_name
    await _stream_upload_to(file, dst)
    # Probe to get duration
    from .ingest.probe import probe as _probe
    try:
        p = _probe(dst)
    except Exception as e:
        raise HTTPException(422, {"file": safe_name, "error": str(e)})

    if add_to_music:
        await _locked_edit(sid, lambda: _add_uploaded_music(store, dst, p.duration, duck, volume_db))

    return {"src": str(dst), "duration": p.duration, "edl_hash": store.edl.hash()}


def _add_uploaded_music(store, dst: Path, duration: float, duck: bool, volume_db: float) -> None:
    """The music-track edit of `audio_upload`, run under the session lock."""
    store.edl.recompute_duration()
    start = 0.0
    # Trim the music bed to the VIDEO length. The old expression here was
    #     min(p.duration, max(edl.duration, p.duration))
    # which is the algebraic identity `min(d, max(x, d)) == d` for all x — it
    # ALWAYS returned the full song, so its "trim to project duration"
    # comment described behaviour that never existed. A 29s video plus a
    # 6:13 song therefore made edl.duration 373.71s, and the transport and
    # the render then legitimately ran minutes past the last frame of video
    # (reported on both the browser and the desktop app as "the timer keeps
    # running after the clip finishes").
    #
    # Music-first-then-video is still valid, and so is a deliberately long
    # bed on a short video, so when there is no video yet we keep the whole
    # song rather than trimming it to nothing.
    video_extent = store.edl.video_extent()
    out = min(duration, video_extent) if video_extent > 0.05 else duration
    dispatch(store, "add_music", {
        "src": str(dst), "start": start, "in": 0.0, "out": out,
        "duck": duck, "volume_db": volume_db,
    })


_SUBTITLE_SUFFIXES = frozenset({".srt", ".vtt", ".ass"})


@app.post("/api/sessions/{sid}/subtitle_upload")
async def subtitle_upload(sid: str, request: Request, file: UploadFile = File(...)):
    """Store a .srt/.vtt/.ass in the session so `import_srt` can run from the
    browser without the user typing a path. `/upload` cannot take this job:
    it ffmpeg-normalises everything it receives and 422s on a non-video.

    Deliberately does NOT dispatch. The AI panel follows up with
    dispatch("import_srt", {path}) itself, so the import goes through the one
    mutation path and lands in the op log / undo like every other edit.
    """
    busy = _prompt_running_response(sid)         # the follow-up dispatch would 409 anyway; say so first
    if busy is not None:
        return busy
    _store(sid)                                  # 404 on an unknown session
    safe_name = _safe_filename(file.filename, "captions.srt")
    if Path(safe_name).suffix.lower() not in _SUBTITLE_SUFFIXES:
        raise HTTPException(422, {
            "error": "unsupported_subtitle",
            "message": f"expected a .srt, .vtt or .ass file, got {safe_name!r}",
        })
    uploads = session_dir(sid) / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    dst = uploads / safe_name
    # See sticker_upload: the Content-Length middleware is not reached by a
    # chunked body, so the free-space precondition and the mid-stream running
    # total are the real limits on this route.
    _assert_room_for(request, uploads)
    await _stream_upload_to(file, dst)
    return {"path": str(dst), "name": dst.name}


def _match_canvas_to_source(store, probe) -> None:
    """Set the canvas orientation to match the first uploaded source.

    Every fresh session starts with the hardcoded vertical 1080x1920 default
    (edl/schema.py Canvas, empty_edl()) regardless of what gets uploaded — so
    a landscape source lands in a portrait canvas and gets pillarboxed (the
    compositor preserves aspect ratio via scale+pad, so it's letterboxed, not
    stretched/distorted as sometimes reported, but the thick black bars read
    just as badly). Only called for the FIRST upload into an empty timeline
    (see the `was_empty` check at the call site) — a later upload into an
    existing project must not silently resize the canvas the user is already
    working in.
    """
    video = probe.video
    if not video or not video.width or not video.height:
        return
    w, h = video.width, video.height
    if w == h:
        ratio = "1:1"
    elif w > h:
        ratio = "16:9"
    else:
        ratio = "9:16"
    dispatch(store, "set_aspect_ratio", {"ratio": ratio})


@app.post("/api/sessions/{sid}/upload")
async def upload(sid: str, request: Request, background_tasks: BackgroundTasks,
                 file: UploadFile = File(...),
                 add_to_timeline: bool = Form(True),
                 transcribe: bool = Form(True),
                 whisper_model: str = Form("")):
    busy = _prompt_running_response(sid)
    if busy is not None:
        return busy
    store = _store(sid)
    sd = session_dir(sid)
    uploads = sd / "uploads"
    uploads.mkdir(exist_ok=True)
    # Two guards the middleware cannot do: refuse an import the volume has no
    # room for (better than failing at 97% of a five-minute upload), and abort
    # mid-stream on a body that lied about its Content-Length, deleting the
    # partial file on the way out. See api/uploads.py.
    _assert_room_for(request, uploads)
    safe_name = _safe_filename(file.filename, "upload.mp4")
    dst = uploads / safe_name
    await _stream_upload_to(file, dst)

    # Normalize is unavoidable for the timeline to work — it's relatively fast.
    # Whisper transcription is the slow part (10-60s on CPU); push it to a
    # background task so the upload response returns immediately and the UI is
    # responsive. Transcript becomes available later via the transcript endpoint.
    try:
        res = ingest_upload(dst, uploads / dst.stem, transcribe_audio=False)
    except HTTPException:
        raise
    except Exception as e:
        # ANY ingest failure (unreadable container, exotic codec, corrupt
        # file, ffprobe/ffmpeg error, JSON parse, etc.) must be a clean 422 —
        # never a bare 500. This is the "video import failed" path users hit
        # with files that aren't really valid video.
        import logging
        logging.getLogger("video_ai_editor").warning(
            "upload ingest failed for %s: %s", safe_name, e)
        msg = str(e)
        raise HTTPException(status_code=422, detail={
            "file": safe_name,
            "error": "couldn't_import",
            "message": "Couldn't import this file — it may not be a valid video, "
                       "or it uses a codec/container we can't read. Try exporting "
                       "it as a standard H.264 .mp4 and re-importing.",
            "detail": msg[-300:] if len(msg) > 300 else msg,
        })

    # /upload is the VIDEO ingress and hardcodes track v1 below. An audio-only
    # file reaching it (an .mp4/.mov/.mkv container with no video stream slips
    # past the frontend's extension-based routing) normalizes "successfully"
    # into a picture-less mp4, lands on v1, and then breaks every subsequent
    # render with "[i:v] … matches no streams". Point the user at the audio
    # ingress instead of letting them build an unrenderable timeline.
    if res.probe.streams and res.probe.video is None:
        raise HTTPException(status_code=422, detail={
            "file": safe_name,
            "error": "audio_only_file",
            "message": "This file has no video track — it's audio only. "
                       "Add it with “Add music…” (or drop it on the Music lane) "
                       "instead of the video track.",
        })

    if add_to_timeline:
        def _edit() -> bool:
            v1 = store.edl.get_track("v1")
            was_empty = not any(True for _ in (v1.clips if v1 else []))
            if was_empty:
                _match_canvas_to_source(store, res.probe)
            store.edl.recompute_duration()
            # Append after the last V1 clip — NOT after `edl.duration`, which spans
            # every track. Importing a 6-minute song first would otherwise park the
            # next video at start=373s, stranding it behind minutes of black (now
            # that gaps actually render, that black is real footage in the export).
            start = store.edl.video_extent()
            dispatch(store, "add_clip", {
                "track": "v1",
                "src": str(res.normalized),
                "in": 0.0,
                "out": res.probe.duration,
                "start": start,
            })
            return was_empty

        was_empty = await _locked_edit(sid, _edit)
        if was_empty:
            # This upload starts a brand-new project on an empty timeline —
            # any chat history is necessarily about DIFFERENT, no-longer-
            # present footage (or a prior session's resumed project). Replaying
            # it to Claude is how "describe this video" answers end up
            # describing a video from a past conversation. A mid-project
            # upload (b-roll added to existing footage) intentionally keeps
            # history, since that context is still relevant.
            _save_history(sid, [])

    if transcribe:
        # Run whisper after we've returned. Writes to ingest.json so subsequent
        # get_transcript / add_caption_track calls find it. `whisper_model`
        # opts the user into a smaller model — `tiny.en` is ~5× faster than
        # `small` for English-only content; `small` (default) is multilingual.
        from .ingest.transcribe import transcribe as _transcribe
        out_dir = uploads / dst.stem
        normalized_path = Path(res.normalized)
        chosen_model = whisper_model.strip() or None

        def _bg_transcribe() -> None:
            try:
                tx = _transcribe(normalized_path, model_size=chosen_model)
                ingest_json = out_dir / "ingest.json"
                if ingest_json.exists():
                    data = json.loads(ingest_json.read_text(encoding="utf-8"))
                    data["transcript"] = tx.model_dump()
                    ingest_json.write_text(json.dumps(data, indent=2), encoding="utf-8")
            except Exception:
                pass

        background_tasks.add_task(_bg_transcribe)

    return {
        "src": str(dst),
        "normalized": str(res.normalized),
        "duration": res.probe.duration,
        "probe": res.probe.model_dump(),
        "edl_hash": store.edl.hash(),
        "transcript_pending": bool(transcribe),
    }


# Tools that routinely run for tens of seconds to minutes: they load a torch/
# ONNX model and process every frame. Held on the request threadpool they pin a
# worker for the whole run, which is the concrete mechanism behind the round-5
# "app becomes unresponsive" observation (VAI-11) — the default threadpool is
# small and /livez, the timeline poll and the next edit all queue behind them.
# The frontend calls these with `wait=0` and polls /api/jobs/{id}, exactly as it
# already does for export.
# `transcribe` (0.7.0, agent/prompt) is deliberately NOT here although it can
# run for a minute: this set is mirrored verbatim in mobile/lib/jobs.ts and
# pinned by tests/test_mobile_catalog_drift.py, and the phone is frozen this
# release. The prompt executor runs it on its own thread; a direct caller
# passes `?wait=0`, which the route accepts for any tool.
ASYNC_DISPATCH_TOOLS = frozenset({
    "remove_background", "object_erase", "upscale", "stabilize",
    "smooth_slow_motion", "vocal_isolate", "instrumental_isolate",
    "motion_track", "auto_caption", "multicam",
})


@app.post("/api/sessions/{sid}/dispatch")
def dispatch_tool(sid: str, body: DispatchRequest, wait: int = 1):
    """Run a tool.

    `wait=1` (default): blocks and returns the result — the long-standing
    behaviour every existing caller relies on.
    `wait=0`: returns 202 + `{job_id, status_url}` and runs the tool on the job
    executor. The completed job's `result` is the same payload the sync path
    returns. Intended for ASYNC_DISPATCH_TOOLS; permitted for any tool so the
    UI never needs a second allowlist.
    """
    store = _store(sid)
    # A prompt run holds the session lock for as long as its longest step
    # (a caption pass can be minutes). Blocking a UI gesture behind it would
    # read as a hung app, so answer 409 up front; the desktop shows "Prompt
    # running — wait or cancel". Job workers (below) keep blocking: a queued
    # job is meant to wait its turn.
    busy = _prompt_running_response(sid)
    if busy is not None:
        return busy
    if not wait:
        return _dispatch_async(sid, store, body)
    with _session_lock(sid):
        return _dispatch_sync(sid, store, body)


def _dispatch_async(sid: str, store: EDLStore, body: DispatchRequest) -> JSONResponse:
    from .api.jobs import JOB_MANAGER

    # Declaring these two parameters is what makes JobManager inject them (it
    # inspects the signature), and dispatch() then passes them on to any handler
    # that asks for them. auto_caption is the reason: large-v3 on CPU runs for
    # minutes, and a job that reports no progress and cannot be stopped is
    # indistinguishable from a hung app.
    def _job(set_progress=None, cancel_event=None) -> dict:
        # Re-resolve the store inside the worker: the LRU cache may have
        # evicted and rebuilt it while this job sat queued, and mutating a
        # detached copy would write edits that the next request never sees.
        try:
            with _session_lock(sid):
                return _dispatch_sync(sid, _store(sid), body,
                                      set_progress=set_progress,
                                      cancel_event=cancel_event)
        except HTTPException as e:
            # JobManager records `f"{type(e).__name__}: {e}"`, and an
            # HTTPException stringifies to its repr — unreadable in the UI's
            # job-error toast. Carry the detail across instead.
            detail = e.detail
            raise RuntimeError(detail if isinstance(detail, str) else json.dumps(detail)) from e

    job = JOB_MANAGER.submit(kind=f"dispatch:{body.tool}", fn=_job, session_id=sid)
    return JSONResponse(
        status_code=202,
        content={"job_id": job.id, "status": job.status,
                 "status_url": f"/api/jobs/{job.id}"},
    )


def _dispatch_sync(sid: str, store: EDLStore, body: DispatchRequest, *,
                   set_progress=None, cancel_event=None) -> dict:
    ops_before = len(store.ops.ops)
    try:
        result = dispatch(store, body.tool, body.args,
                          set_progress=set_progress, cancel_event=cancel_event)
    except KeyError as e:
        raise HTTPException(400, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        # External-tool / setup errors bubble up here (pyannote token missing,
        # ffmpeg failure, model not found, etc.) — give the user the message.
        raise HTTPException(422, str(e))
    except (OSError, subprocess.SubprocessError) as e:
        # There are ~20 `check=True` subprocess sites under ai/, so a missing
        # binary (FileNotFoundError) or a non-zero exit (CalledProcessError)
        # reached the client as a bare HTTP 500 + traceback rather than an
        # actionable message. Both are OSError/SubprocessError subclasses.
        raise HTTPException(422, f"An external tool failed: {e}")
    # Read-only tools (get_*, list_*, search_media) deliberately don't commit, so
    # `ops.last()` was the PREVIOUS mutation — making an API consumer believe a
    # read had changed the timeline. Compare the op COUNT rather than the op's
    # tool name: composite handlers commit under a different name than the tool
    # that was called (remove_silences loops cut_range), so a name check would
    # wrongly report "no change" for them.
    last_op = store.ops.last() if len(store.ops.ops) > ops_before else None
    return {
        "result": result,
        "edl_hash": store.edl.hash(),
        "op": last_op.model_dump() if last_op else None,
    }


@app.get("/api/sessions/{sid}/edl")
def get_edl(sid: str):
    store = _store(sid)
    return JSONResponse(json.loads(store.edl.to_json()))


@app.get("/api/sessions/{sid}/ops")
def get_ops(sid: str, since: int = 0):
    store = _store(sid)
    return {"ops": [op.model_dump() for op in store.ops.ops[since:]]}


@app.get("/api/sessions/{sid}/transcript")
def get_transcript(sid: str):
    store = _store(sid)
    return dispatch(store, "get_transcript", {})


def _preview_payload(sid: str, res) -> dict:
    return {
        "path": str(res.path),
        "cached": res.cached,
        "edl_hash": res.edl_hash,
        "url": f"/api/sessions/{sid}/preview.mp4?h={res.edl_hash}",
    }


# Lines that carry an actual diagnosis, as opposed to ffmpeg's inventory of its
# inputs. This distinction is load-bearing: a render with text/stickers lists
# every overlay PNG as `Input #12, png_pipe, from 'st_<hash>.png'`, so matching
# the overlay-cache pattern anywhere in stderr would blame a corrupt overlay for
# EVERY failure of a timeline that merely HAS overlays.
_ERRORISH = re.compile(
    r"(?:error|invalid|cannot|could not|failed|unable|no such file|"
    r"does not|do not match|not divisible)", re.IGNORECASE)


def _error_lines(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if _ERRORISH.search(ln))


def _render_failure_message(ffmpeg_tail: str, full: str | None = None) -> str:
    """Pick a user-facing message for a render_failed 422.

    `ffmpeg_tail` is what gets SHOWN; `full` (when given) is the complete
    RuntimeError text used for CLASSIFICATION. They differ on purpose: the
    caller truncates the display string to a few hundred characters, and the
    decisive line of an ffmpeg failure is often further back than that — a
    filtergraph-configuration error is followed by per-stream teardown summaries
    and `Conversion failed!`, so classifying on the tail alone fell through to
    the generic "corrupt frames" message for almost every real failure.

    Branch order is deliberate: interruption first (it is not a media problem at
    all), then our own generated inputs, then graph/dimension errors, then
    missing files, then stream binding, and only then "your media might be bad".
    """
    hay = full or ffmpeg_tail
    errs = _error_lines(hay)

    # 1. INTERRUPTED. A child ffmpeg that was terminated logs a NORMAL-looking
    # encode summary and then "Exiting normally, received signal 15" — nothing
    # is wrong with the media. This is what a Windows tester hit by closing the
    # console window that used to appear behind the app: the render died, and
    # the app told them their footage had "corrupt frames or an unusual codec".
    if re.search(r"received signal \d+", hay) or "Exiting normally" in hay:
        return ("The render was interrupted before it finished — something "
                "stopped the video encoder (for example closing a terminal "
                "window that the app had opened). Nothing is wrong with your "
                "media; press play or re-export to try again.")

    # 2. Our own rasterized overlay cache, but ONLY when named on an error line.
    # st_/text_/sa_ files are "<prefix><16-hex content hash>.png"
    # (text_overlay.py); mask_ files are
    # "mask_<clip_id>_<type>_<feather>_<w>x<h>.png" (compositor.py/chunks.py) —
    # a much less regular shape, hence the separate alternative.
    if re.search(r"[\\/](?:st_|text_|sa_)[0-9a-f]+\.png", errs) or \
       re.search(r"[\\/]mask_[^\\/]+\.png", errs):
        return ("Couldn't render a preview — a cached text/sticker overlay "
                "image was corrupted. Retrying will regenerate it; your "
                "media is fine.")

    # 3. Filtergraph / dimension inconsistency: the app built an internally
    # contradictory graph. Never the user's fault, and "corrupt frames" sent
    # people hunting through perfectly good footage (this is how the 4:5 aspect
    # bug and the transition-timebase bug both presented).
    if ("Padded dimensions cannot be smaller" in hay
            or "do not match the corresponding output link" in hay
            or "Input frame sizes do not match" in hay
            or re.search(r"(?:width|height) not divisible by 2", hay)
            or "Error reinitializing filters" in hay):
        return ("Couldn't render — the app built an inconsistent video "
                "filtergraph for this timeline (a size or timing mismatch "
                "between clips). This is a bug, not a problem with your media. "
                "Undoing the last edit usually clears it.")

    # 4. A missing file. Which KIND of file matters: a `.cube` named on the
    # error line is a LUT the user pointed an effect at, not the clip's source,
    # and telling them their footage moved sent them hunting through media that
    # was never the problem. (`apply_lut` now rejects a nonexistent path up
    # front, so this is the belt for a project saved before that guard, or a LUT
    # deleted after it was applied.) Found by the round-5 end-to-end sweep.
    if "No such file or directory" in errs:
        missing = re.findall(r"'([^']+)'", errs)
        lut = next((m for m in missing if m.lower().endswith(".cube")), None)
        if lut or re.search(r"lut3d", errs, re.I):
            which = f" ({Path(lut).name})" if lut else ""
            return (f"Couldn't render — a colour LUT{which} applied to a clip is "
                    f"missing. Remove that effect in Properties, or re-apply the "
                    f"LUT from an existing '.cube' file.")
        which = f" ({Path(missing[0]).name})" if missing else ""
        return (f"Couldn't render — a clip's source file{which} is missing. It "
                f"may have been moved or deleted since you added it.")

    # 5. A stream specifier that binds to nothing means a clip's source doesn't
    # have the stream its lane requires — overwhelmingly an audio-only file
    # sitting on the video track. New placements are now blocked
    # (dispatch._reject_videoless_on_video_lane), but a project saved before
    # that guard existed can't render at all, and the generic message below
    # sends the user hunting for a "corrupt" file that is perfectly fine.
    if "matches no streams" in hay or \
       "Error binding filtergraph inputs/outputs" in hay:
        which = ("an audio-only file on the video track"
                 if ":v' " in hay or ":v'" in hay
                 else "a clip whose source is missing a needed stream")
        return (f"Couldn't render — the timeline contains {which}. "
                f"Move that clip to the Music lane (or delete it) and try again.")

    return ("Couldn't render a preview for this clip — it may have corrupt "
             "frames or an unusual codec.")


@app.post("/api/sessions/{sid}/preview")
def make_preview(sid: str, wait: int = 1):
    """Render a preview.

    `wait=1` (default): blocks until done. Backwards-compatible with the
    existing frontend.
    `wait=0`: returns 202 + `{job_id, status_url}` immediately. Poll
    `/api/jobs/{job_id}` for progress; the result field gets the same
    payload the sync path returns. Use this for hosted/multi-user setups
    where the request thread shouldn't block on a 30s render.
    """
    store = _store(sid)
    if wait:
        try:
            res = render_preview(store.edl, store.dir)
        except RuntimeError as e:
            # ffmpeg render failure → actionable 422, not a bare 500. Surface a
            # short tail of ffmpeg's reason so the UI can show something useful.
            msg = str(e)
            tail = msg[-400:] if len(msg) > 400 else msg
            # Classify on the FULL message, display the tail — the decisive
            # ffmpeg line is routinely further back than 400 chars.
            raise HTTPException(422, {"error": "render_failed",
                                      "message": _render_failure_message(tail, msg),
                                      "ffmpeg": tail})
        return _preview_payload(sid, res)
    from .api.jobs import JOB_MANAGER
    edl_snapshot = store.edl  # safe — render_preview only reads
    session_dir_snapshot = store.dir

    def _job() -> dict:
        res = render_preview(edl_snapshot, session_dir_snapshot)
        return _preview_payload(sid, res)

    job = JOB_MANAGER.submit(kind="preview", fn=_job, session_id=sid)
    return JSONResponse(
        status_code=202,
        content={"job_id": job.id, "status": job.status,
                 "status_url": f"/api/jobs/{job.id}"},
    )


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    """Poll a background job. Returns the full job state.

    `result` is null until `status == "completed"`; `error` is null
    unless `status == "failed"`. Clients should poll until status is
    in {completed, failed}.
    """
    from .api.jobs import JOB_MANAGER
    job = JOB_MANAGER.get(job_id)
    if job is None:
        raise HTTPException(404, f"job {job_id} not found")
    return job.to_dict()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    """Request cancellation of a running/queued job (e.g. a long export). The
    job's ffmpeg is terminated and its status becomes 'cancelled'."""
    from .api.jobs import JOB_MANAGER
    job = JOB_MANAGER.cancel(job_id)
    if job is None:
        raise HTTPException(404, f"job {job_id} not found")
    return job.to_dict()


@app.get("/api/sessions/{sid}/jobs")
def list_session_jobs(sid: str):
    """Recent jobs scoped to a session. Useful for a UI 'Renders' panel."""
    _store(sid)  # 404 if session doesn't exist
    from .api.jobs import JOB_MANAGER
    return {"jobs": [j.to_dict() for j in JOB_MANAGER.list(session_id=sid)]}


@app.get("/api/sessions/{sid}/preview.mp4")
def stream_preview(sid: str, h: str | None = None):
    store = _store(sid)
    target_hash = h or store.edl.hash()
    p = store.dir / "previews" / f"{target_hash}.mp4"
    # Treat a 0-byte leftover (from a killed render that predates atomic writes)
    # as missing — serving it would hand the client a torn file that mp4box
    # rejects with "invalid box". Re-render instead.
    if not p.exists() or p.stat().st_size == 0:
        # Same RuntimeError -> 422 mapping the POST /preview and /export paths
        # use. Without it this route answered a render failure with a bare 500
        # plus a full traceback (logged twice), while the very same failure via
        # POST produced a clean, actionable 422. The <video> element polls THIS
        # url, so it was the shape the UI hit most often.
        try:
            res = render_preview(store.edl, store.dir)
        except RuntimeError as e:
            msg = str(e)
            tail = msg[-400:]
            raise HTTPException(422, {"error": "render_failed",
                                      "message": _render_failure_message(tail, msg),
                                      "ffmpeg": tail}) from e
        # Only serve what the caller ASKED for. This used to return whatever
        # the current EDL rendered to, even when `h` named a different render:
        # a mid-playback range request would then receive bytes from a file of
        # a different length, which the browser sees as a corrupt/short read →
        # decode stall, reload, currentTime reset. The <video> re-requests with
        # the right hash after `refresh()`, so a 404 here is recoverable; a
        # silently mismatched body is not.
        if h and res.edl_hash != h:
            raise HTTPException(404, "preview for that hash is no longer available")
        p = res.path
    return FileResponse(p, media_type="video/mp4", filename="preview.mp4")


def _export_payload(sid: str, res) -> dict:
    return {"path": str(res.path), "filename": res.path.name,
            "url": f"/api/sessions/{sid}/files/exports/{res.path.name}"}


@app.post("/api/sessions/{sid}/export")
def make_export(sid: str, body: ExportRequest | None = None, wait: int = 1):
    """Render an export at canvas resolution. `wait=0` returns 202 + job_id
    immediately (poll `/api/jobs/{job_id}`); default keeps the sync shape
    for backward compat. Exports can take minutes — use `wait=0` from any
    client where the request might time out (most browsers/proxies)."""
    store = _store(sid)
    body = body or ExportRequest()
    if wait:
        # Mirror the preview path's RuntimeError→422 handling. Without it an
        # ffmpeg failure fell through to hardening's generic handler as an
        # opaque HTTP 500 with the reason discarded into the server log.
        try:
            res = render_export(store.edl, store.dir, height=body.height,
                                fps=body.fps, crf=body.crf, container=body.container)
        except RuntimeError as e:
            msg = str(e)
            tail = msg[-400:]
            raise HTTPException(422, {
                "error": "render_failed",
                "message": _render_failure_message(tail, msg),
                "ffmpeg": tail,
            })
        return _export_payload(sid, res)
    from .api.jobs import JOB_MANAGER
    edl_snapshot = store.edl
    session_dir_snapshot = store.dir
    height, fps, crf, container = body.height, body.fps, body.crf, body.container

    def _job(set_progress=None, cancel_event=None) -> dict:
        try:
            res = render_export(edl_snapshot, session_dir_snapshot,
                                height=height, fps=fps, crf=crf, container=container,
                                on_progress=set_progress, cancel_event=cancel_event)
        except RuntimeError as e:
            # jobs.py stores `f"{type(e).__name__}: {e}"` as job.error and the
            # UI shows it verbatim — so raise something whose str() is already
            # user-facing instead of a 2000-char ffmpeg stderr dump.
            msg = str(e)
            raise RuntimeError(_render_failure_message(msg[-400:], msg)) from e
        return _export_payload(sid, res)

    job = JOB_MANAGER.submit(kind="export", fn=_job, session_id=sid)
    return JSONResponse(
        status_code=202,
        content={"job_id": job.id, "status": job.status,
                 "status_url": f"/api/jobs/{job.id}"},
    )


# --- M2: chat ---

class ChatRequest(BaseModel):
    message: str
    # Editor UI state at send time — lets Claude bind "this clip" and "here"
    # to real ids instead of guessing. All optional for older callers.
    selection: str | None = None
    multi_selection: list[str] = []
    playhead: float | None = None


def _history_path(sid: str) -> Path:
    return session_dir(sid) / "chat.json"


def _load_history(sid: str) -> list[dict]:
    p = _history_path(sid)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_history(sid: str, history: list[dict]) -> None:
    # Under the history lock: a prompt run's thread finalizes the same file
    # from `agent/prompt/service.FileHistoryWriter` and the two writes may
    # land in either order (spec §4.6).
    with _locks.history_lock(sid):
        _history_path(sid).write_text(json.dumps(history, indent=2, default=str), encoding="utf-8")


@app.get("/api/sessions/{sid}/history")
def get_history(sid: str):
    return {"history": _load_history(sid)}


@app.post("/api/sessions/{sid}/chat")
async def chat(sid: str, body: ChatRequest):
    """SSE-stream a Claude chat turn (text deltas + tool calls + ops)."""
    store = _store(sid)
    history = _load_history(sid)

    ui_state = {
        "selection": body.selection,
        "multi_selection": body.multi_selection,
        "playhead": body.playhead,
    } if (body.selection or body.playhead is not None) else None

    async def gen():
        try:
            async for evt in chat_turn(store, body.message, history,
                                       ui_state=ui_state):
                yield f"data: {json.dumps(evt)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type':'error','message':str(e)})}\n\n"
        finally:
            _save_history(sid, history)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# Prompt Editor routes (spec §4.7): `/api/sessions/{sid}/prompt*` and
# `/api/prompt/*`. Mounted after the chat route, behind the same middleware;
# `configure` hands them the LRU store resolver so the run thread re-resolves
# the store inside the session lock.
from .api import prompt_routes as _prompt_routes
_prompt_routes.configure(resolve_store=_store)
app.include_router(_prompt_routes.router)
_prompt_running_response = _prompt_routes.prompt_running_response


@app.get("/api/sessions/{sid}/waveform")
def get_waveform(sid: str, src: str, peaks_per_sec: int = 50):
    """Return downsampled audio peaks for a source path used in this session.

    `src` must lie within the session workdir (defends against path traversal).
    """
    store = _store(sid)
    sd = session_dir(sid)
    target = Path(src)
    # Same boundary semantics as /thumb: absolute-only + is_relative_to (a
    # startswith() prefix check also admitted sibling sessions whose id
    # extends this one, e.g. s_ab matching s_abcd).
    if not target.is_absolute():
        raise HTTPException(403, "src must be an absolute path")
    target = target.resolve()
    if not target.is_relative_to(sd.resolve()):
        raise HTTPException(403, "src must be inside the session workdir")
    if not target.exists():
        raise HTTPException(404, "src not found")
    from .render.waveform import waveform_peaks
    return waveform_peaks(target, sd / "cache" / "waveforms",
                          peaks_per_sec=peaks_per_sec)


@app.get("/api/sessions/{sid}/thumb")
def get_thumb(sid: str, src: str, t: float = 0.0, h: int = 54):
    """One scaled JPEG frame of a session source at time `t`.

    Feeds the timeline filmstrip / media-bin previews. Same trust posture as
    /waveform: `src` is untrusted and must resolve inside the session workdir.
    """
    _store(sid)  # validates sid shape before any filesystem work
    sd_root = session_dir(sid).resolve()
    target = Path(src)
    # Absolute-only + is_relative_to: a bare startswith() prefix check would
    # also admit sibling sessions whose id extends this one (s_ab → s_abcd),
    # and a relative src would resolve against the process CWD.
    if not target.is_absolute():
        raise HTTPException(403, "src must be an absolute path")
    target = target.resolve()
    if not target.is_relative_to(sd_root):
        raise HTTPException(403, "src must be inside the session workdir")
    if not target.exists():
        raise HTTPException(404, "src not found")
    h = max(16, min(int(h), 270))
    from .render.thumbs import thumbnail_for
    try:
        p = thumbnail_for(target, sd_root / "cache" / "thumbs", t=float(t), height=h)
    except RuntimeError as e:
        raise HTTPException(422, str(e))
    return FileResponse(p, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=3600"})


# --- M6: project save/load ---

@app.post("/api/sessions/{sid}/save_project")
def save_project_endpoint(sid: str):
    sd = session_dir(sid)
    from .storage_project import save_project
    out = sd / "exports" / f"{sd.name}.vae"
    out.parent.mkdir(parents=True, exist_ok=True)
    save_project(sid, out)
    return {"path": str(out), "filename": out.name,
            "url": f"/api/sessions/{sid}/files/exports/{out.name}",
            "size": out.stat().st_size}


@app.post("/api/load_project")
async def load_project_endpoint(request: Request, file: UploadFile = File(...)):
    """Upload a .vae and open it as a new session."""
    name = Path(file.filename or "project.vae").name
    if not name.endswith(".vae") and not name.endswith(".zip"):
        raise HTTPException(415, "expected a .vae project file")
    tmp = WORKDIR / f"_import_{name}"
    # The worst of the three unguarded ingresses: this one writes to WORKDIR,
    # the app's own working volume, rather than into a session that a user can
    # delete. A chunked body with no Content-Length used to walk straight past
    # the middleware and fill it.
    WORKDIR.mkdir(parents=True, exist_ok=True)
    _assert_room_for(request, WORKDIR)
    await _stream_upload_to(file, tmp)
    try:
        from .storage_project import load_project
        sid = load_project(tmp)
    except Exception as e:
        raise HTTPException(422, f"failed to load project: {e}")
    finally:
        tmp.unlink(missing_ok=True)
    return {"id": sid}


# Up to 16 codepoints. The bound is defence-in-depth (the real guard is
# hex-and-dashes only, so `seq` can never name a path); its size just has to
# clear the longest real emoji. 8 did not: an RGI kiss sequence with a skin tone
# on BOTH people is 10 — e.g. 👩🏻‍❤️‍💋‍👨🏿 is
# `1f469-1f3fb-200d-2764-fe0f-200d-1f48b-200d-1f468-1f3ff`. Those 15 sequences
# 400'd here while resolving perfectly in `fetch_emoji_png`, so the picker would
# have shown a swatch that could not load. Found by validating the generated
# catalogue against this regex rather than by clicking one.
_EMOJI_SEQ_RE = re.compile(r"^[0-9a-f]{1,6}(-[0-9a-f]{1,6}){0,15}$")


@app.get("/api/emoji/{seq}.png")
def serve_emoji_png(seq: str):
    """Emoji artwork by dash-joined hex codepoints (e.g. `1f60d`, `1f468-200d-1f4bb`).

    TextLayer composites emoji INSIDE a text clip from the same fetched PNGs
    the exporter bakes (render/text_overlay.render_text_png; Apple/iOS artwork,
    see ai/emoji.py). Letting the browser paint them with its own emoji font
    instead would put the OS design in the preview and the fetched set in the
    delivered file — the same preview/export mismatch stickers already had.
    This route stays load-bearing on EVERY platform, including a Linux box that
    happens to be a Mac with Apple Color Emoji installed: the bytes served
    here are a pinned artwork release, not the local font. A Mac and a Windows
    viewer must get the same pixels, which is the whole point.

    Not session-scoped: emoji artwork is global, shared, and read-only. `seq`
    is validated to hex-and-dashes before it is turned back into characters,
    so it can never name a path.
    """
    if not _EMOJI_SEQ_RE.match(seq):
        raise HTTPException(400, "bad emoji sequence")
    try:
        emoji = "".join(chr(int(p, 16)) for p in seq.split("-"))
    except ValueError:
        raise HTTPException(400, "bad emoji sequence")
    from .ai.emoji import fetch_emoji_png
    path = fetch_emoji_png(emoji)
    if not path or not Path(path).is_file():
        raise HTTPException(404, "no artwork for that emoji")
    # Immutable: the bytes for a codepoint never change, and TextLayer asks
    # for them on every text clip that contains one.
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})


class PrewarmRequest(BaseModel):
    emojis: list[str]


@app.post("/api/emoji/prewarm")
def prewarm_emoji(req: PrewarmRequest):
    """Warm the emoji artwork cache for a list the client is about to draw.

    The picker paints ~1900 swatches and a browser opens ~6 connections per
    origin, so on a cold cache the artwork trickled in over many seconds. This
    lets the client say "I am about to need these" once, and the server fetches
    the misses on a small background pool.

    The CLIENT supplies the list rather than the server deriving it, because the
    catalogue that decides what the picker draws is generated into the frontend
    bundle. Having the server keep its own copy would create two lists that must
    agree and no mechanism to make them — warming the wrong set is worse than
    not warming, since it looks like it worked.

    Returns immediately; `GET` the same path for progress. Never errors on a
    fetch failure: this is an optimisation, and anything it misses is fetched
    on demand exactly as before.
    """
    from .ai.emoji import prewarm
    # Bounded: the full RGI set is ~3.8k, so anything much past that is a client
    # bug or an abusive caller, not a picker.
    if len(req.emojis) > 5000:
        raise HTTPException(400, "too many emoji to prewarm")
    return prewarm(req.emojis)


@app.get("/api/emoji/prewarm")
def prewarm_emoji_status():
    from .ai.emoji import warm_state
    return warm_state()


_RESTYLED_SESSIONS: set[str] = set()
_RESTYLE_LOCK = threading.Lock()


def _restyle_session_stickers_once(sid: str, session_dir: Path) -> None:
    """Bring an existing project's copied emoji artwork up to the current set.

    Hung off the SERVING path rather than session load: this is the one place
    the stale bytes actually reach a user, and a project nobody opens costs
    nothing. Guarded to once per session per process — the work is a stat plus
    a byte compare per sticker, but there is no reason to repeat it per <img>.

    Deliberately not fatal and deliberately not announced: an emoji rendering
    in the right house style is a repair, not an edit the user made, and it
    changes no EDL state (the clip's `src` path is unchanged — only the bytes
    behind it, which are a re-fetchable cache artifact).
    """
    with _RESTYLE_LOCK:
        if sid in _RESTYLED_SESSIONS:
            return
        _RESTYLED_SESSIONS.add(sid)
    try:
        from .ai.emoji import refresh_session_sticker_art
        changed = refresh_session_sticker_art(session_dir / "uploads" / "stickers")
        if changed:
            get_logger().info("restyled %d sticker(s) in %s: %s",
                              len(changed), sid, ", ".join(changed))
    except Exception:
        get_logger().debug("sticker restyle skipped for %s", sid, exc_info=True)


@app.get("/api/sessions/{sid}/sticker/{clip_id}")
def serve_sticker_image(sid: str, clip_id: str):
    """Serve a sticker clip's artwork, resolved through the session's own EDL.

    StickerLayer draws stickers client-side (the server no longer bakes them
    into the preview — see build_overlay_chain's `preview` docstring), so it
    needs the real artwork bytes for every sticker, not just the ones that
    happen to sit under <session>/uploads/. Three sources legitimately don't:
    emoji added before add_sticker started copying into the session (shared
    `user_cache_dir/emoji/`), a brand kit's end-card/watermark image, and any
    sticker whose src was supplied as an absolute path. Those would otherwise
    render as an empty box in preview while exporting perfectly — the worst
    kind of mismatch.

    NOT a path-serving route: the only untrusted input is `clip_id`, used as a
    lookup key. The path served is whatever that session's EDL already stores
    and the renderer already reads, so this exposes nothing new — unlike
    widening `/files/{kind}/{name}`, whose containment check is its whole
    security model.
    """
    if not is_valid_session_id(sid):
        raise HTTPException(400, {"code": "invalid_sid", "message": "invalid session id"})
    from .edl.schema import Sticker
    store = _store(sid)
    _restyle_session_stickers_once(sid, store.dir)
    found = store.edl.get_clip(clip_id)
    clip = found[1] if found else None
    if not isinstance(clip, Sticker) or not clip.src:
        raise HTTPException(404, "sticker not found")
    path = Path(clip.src)
    if not path.is_file():
        # The artwork is genuinely gone (emoji cache cleared, end-card moved).
        # 404 so the client falls back to its glyph/outline rather than hanging.
        raise HTTPException(404, "sticker image missing")
    return FileResponse(path)


@app.get("/api/sessions/{sid}/files/{kind}/{name:path}")
def serve_session_file(sid: str, kind: str, name: str):
    # Same first-layer sid shape check as DELETE /sessions/{sid} — sid is
    # untrusted URL input that gets joined into a filesystem path below.
    if not is_valid_session_id(sid):
        raise HTTPException(400, {"code": "invalid_sid", "message": "invalid session id"})
    if kind not in {"uploads", "previews", "exports"}:
        raise HTTPException(404, "not found")
    base = (session_dir(sid) / kind).resolve()
    # `name` may include subdirs (e.g. "stickers/smile.png",
    # "Outfit.../Outfit....normalized.mp4") — the route param is {name:path},
    # so the router no longer rejects slashes and THIS containment check is
    # the only traversal guard. Anchored at <session>/<kind> and compared via
    # is_relative_to: the old startswith(str(session_dir)) prefix compare had
    # no trailing separator, so a sibling session whose dir name merely
    # EXTENDS this sid (s_abc vs s_abc12) — or a cross-kind "../snapshots/…"
    # hop — would have passed it.
    candidate = (base / name).resolve()
    if not candidate.is_relative_to(base):
        raise HTTPException(403, "forbidden")
    if not candidate.exists() and "/" not in name:
        # Bare-name fallback only: one level deeper for ingest output
        # (uploaded clips live under uploads/<stem>/). Subpath names are
        # exact by construction (StickerLayer, preview URLs) — never rglob
        # those (an unmatchable pattern with separators can raise in glob).
        for sub in base.rglob(name):
            candidate = sub
            break
    # is_file(), not exists(): {name:path} also matches "" and directory
    # names, and FileResponse(directory) is a 500, not a clean 404.
    if not candidate.is_file():
        raise HTTPException(404, "file not found")
    # Exports are downloads, not something to play in-page. Force
    # `Content-Disposition: attachment` (Starlette does this when `filename=` is
    # set) so that if this URL is ever *navigated* to — e.g. a stray anchor
    # click inside the packaged pywebview app — the webview downloads it instead
    # of handing the .mp4 to macOS's borderless native fullscreen player (which
    # had no Escape/back affordance and trapped the user). uploads/previews stay
    # inline so the frontend <video> can still stream them.
    if kind == "exports":
        return FileResponse(candidate, filename=candidate.name)
    return FileResponse(candidate)


# Mount the built frontend at the root, so the desktop wrap can open
# http://localhost:8000/ as a single self-contained app.
def _find_frontend_dist() -> Path | None:
    """Locate frontend/dist in dev (repo) AND inside a PyInstaller .app bundle.

    PyInstaller unpacks --add-data files under sys._MEIPASS, NOT next to the
    source tree, so the repo-relative path is wrong in the shipped app. Check
    the bundle dir first, then the dev path."""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "frontend" / "dist")
    candidates.append(Path(__file__).resolve().parents[2] / "frontend" / "dist")
    for c in candidates:
        if (c / "index.html").exists():
            return c
    return None


class _NoCacheHtmlStatic(StaticFiles):
    """StaticFiles that forbids caching of HTML while keeping assets cacheable.

    Vite emits content-hashed assets (`index-CkUGqqZ_.js`) referenced from a
    STABLE url (`/index.html`). Plain StaticFiles serves that HTML with only an
    etag/last-modified, so a browser is free to reuse a cached copy — which pins
    the OLD asset hash. Observed live: after a rebuild the page kept loading a
    bundle that no longer existed on disk, so a verified frontend fix appeared
    not to work at all. In the packaged app (WKWebView / WebView2) the same
    thing survives an app update and shows a stale — or blank — editor.

    The hashed assets themselves are safe to cache hard: a new build produces a
    new filename, so there is nothing to invalidate.
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        resp = await super().get_response(path, scope)
        # `path` is "" or "index.html" for the SPA entry (html=True rewrites
        # directory requests), and any unknown route also falls back to it.
        is_html = path in ("", ".", "/", "index.html") or path.endswith(".html")
        if is_html:
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
        elif path.startswith("assets/"):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


_FRONTEND_DIST = _find_frontend_dist()
if _FRONTEND_DIST is not None:
    app.mount("/", _NoCacheHtmlStatic(directory=str(_FRONTEND_DIST), html=True),
              name="frontend")
