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
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from fastapi import FastAPI, UploadFile, File, HTTPException, Form, Request, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pydantic import Field as PydField
from pydantic import field_validator

from . import platformutil as _pu
from .config import WORKDIR, DEFAULT_CANVAS
from .storage import (new_session_id, session_dir, session_exists,
                       list_sessions, write_meta, read_meta, delete_session,
                       is_valid_session_id, default_project_name, rename_session,
                       name_reopened_copy)
from .edl import EDLStore
from .edl import timebase as _tb
from .ingest.probe import video_frame_extent
from .edl.schema import Canvas, Clip
from .edl.schema import FPS_MIN as _FPS_MIN, FPS_MAX as _FPS_MAX
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
    # Wave D: no proxy encode may outlive the app (they are niced background
    # ffmpegs a daemon thread would otherwise orphan).
    from .ingest.proxy_queue import MANAGER as _PROXIES
    _PROXIES.shutdown()


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

# Settings › Claude: the Anthropic key in the macOS Keychain (QA-063-SETTINGS).
# Loopback-only, JSON-only, and no response ever carries the key.
from .api.settings_routes import router as _settings_router
app.include_router(_settings_router)

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

# The Vite dev origins exist only for browser dev. The packaged app serves its
# own page from this server (same origin), so it never needs CORS, and leaving
# :5173 allowed there let ANY other Vite project on the Mac read our answers
# with credentials (REVIEW-C3-KEY-CORS-5173).
if not getattr(sys, "frozen", False):
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
    name = _client_filename(name)
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
    if len(stem_clean) > _MAX_STEM:
        # QA-091: a 235-character name is legal on APFS, but ingest builds
        # `<stem>_<rand>/<stem>.normalized.mp4.part` from it, which passed the
        # 255-byte NAME_MAX and failed as "may not be a valid video". The
        # display name keeps the full original; the disk name only has to be
        # unique and readable, so cut it and add a hash of what was cut.
        import hashlib as _h
        sig = _h.sha1(stem_clean.encode("utf-8")).hexdigest()[:8]
        stem_clean = f"{stem_clean[:_MAX_STEM - 9].rstrip('._-')}_{sig}"
    return f"{stem_clean}{suffix_clean[:_MAX_SUFFIX]}" or fallback


# Disk-name budget for an upload's stem (QA-091). NAME_MAX is 255 bytes; the
# longest derived name is `<stem>_<8 hex>` (the import dir) and
# `<stem>.normalized.mp4.part`, so 80 leaves ample room for every suffix any
# ingest step appends.
_MAX_STEM = 80
_MAX_SUFFIX = 16


# The HTML multipart spec has browsers percent-escape exactly three characters
# in a filename: `"` as %22, CR as %0D and LF as %0A. They were sanitised
# undecoded, so `"पहला वीडियो".mp4` became `22_22.mp4` — and so did every other
# quoted name, which is one of the ways two different imports ended up at one
# path on disk (QA-001). Only these three are decoded: a real `%41` in a
# filename is literal text, not an escape.
_CLIENT_FILENAME_ESCAPES = re.compile(r"%(22|0[dD]|0[aA])")


def _client_filename(name: str | None) -> str | None:
    """The filename the user's file really had, undoing the browser's
    multipart escaping (see `_CLIENT_FILENAME_ESCAPES`)."""
    if not name:
        return name
    return _CLIENT_FILENAME_ESCAPES.sub(lambda m: chr(int(m.group(1), 16)), name)


def _display_name(name: str | None, fallback: str) -> str:
    """What to call an upload in the UI: its real name, last path component."""
    decoded = _client_filename(name) or ""
    return Path(decoded.replace("\\", "/")).name or fallback


def _unique_upload_path(parent: Path, safe_name: str) -> Path:
    """A path in `parent` that no earlier upload can own (QA-001).

    Every upload ingress used to write to `parent / safe_name`, so two files
    whose names sanitise alike (`clip (1).mp4` / `clip [1].mp4`, two cameras'
    `C0001.MP4`, a/song.wav and b/song.wav) silently replaced the first
    import's media — and every clip that referenced it. A random suffix makes a
    collision impossible, and the file is created with O_EXCL so even the
    astronomically unlikely repeat is caught instead of overwritten."""
    parent.mkdir(parents=True, exist_ok=True)
    stem, suffix = Path(safe_name).stem, Path(safe_name).suffix
    while True:
        candidate = parent / f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"
        try:
            with candidate.open("xb"):
                return candidate
        except FileExistsError:
            continue


def _unique_upload_dir(parent: Path, stem: str) -> Path:
    """A fresh directory `parent/<stem>_<random>` for one video import — the
    raw upload, its normalised mp4 and its ingest.json live together in it
    (see `_unique_upload_path` for why it must be unique)."""
    parent.mkdir(parents=True, exist_ok=True)
    while True:
        candidate = parent / f"{stem}_{uuid.uuid4().hex[:8]}"
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            continue


# One mutation at a time per session. The registry lives in api/locks.py so
# the Prompt Editor's run thread (agent/prompt/executor.py) takes the SAME
# lock as `/dispatch` and the job workers without importing this module; the
# rationale (FastAPI's threadpool + a shared EDLStore) is written there.
from .api import locks as _locks
_session_lock = _locks.session_lock


def _is_path_shaped_sid(sid: str) -> bool:
    """An id that names a directory other than its own leaf in WORKDIR."""
    return (not sid or sid in (".", "..") or any(ch in sid for ch in "/\\\x00")
            or sid.startswith("."))


def _store(sid: str) -> EDLStore:
    # Fast path: already cached. Mark as recently used.
    with _STORES_LOCK:
        cached = _STORES.get(sid)
        if cached is not None:
            _STORES.move_to_end(sid)
            return cached
        # A path-shaped id never reaches session_exists/EDLStore: '.' "exists"
        # (it is WORKDIR itself) and EDLStore would materialise a project's
        # tree there, '..' its parent (REV-B2-SID-ROUTES). Anything else that
        # is not on disk stays the 404 it always was.
        if _is_path_shaped_sid(sid):
            raise HTTPException(400, {"code": "invalid_sid", "message": "invalid session id"})
        # Only a well-formed session id names a session: WORKDIR also holds
        # non-session directories (wave D's WORKDIR/proxies), which must
        # never be adopted as a project and get a tree built inside them.
        if not is_valid_session_id(sid) or not session_exists(sid):
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
    # QA-105: the EDL hash the caller's view was built from. When present and
    # no longer the session's hash, the edit is refused (409 stale_edl) — a
    # second window on the same project would otherwise apply an edit (or an
    # UNDO of someone else's edit) against a timeline it is not showing.
    # Optional: MCP, Claude and older UI builds send none and are unaffected.
    base_hash: str | None = None


class CreateSessionRequest(BaseModel):
    name: str | None = None


class RenameSessionRequest(BaseModel):
    name: str


class ExportRequest(BaseModel):
    # A NAMED resolution: the SHORT side for the canvas orientation, so 1080 on
    # a 9:16 project is 1080x1920 (QA-025; see compositor.export_dimensions).
    # Bounded like every other numeric input (QA-041): an absurd size would
    # start an unbounded encode.
    height: int | None = PydField(None, ge=16, le=7680)
    # float: 23.976 / 29.97 / 59.94 are real delivery rates (QA-009). The
    # renderer turns it into an exact ffmpeg rational via edl.timebase.
    # Bounded to the Canvas range (QA-041): fps=1e6 built a 10M-frame graph
    # per segment — an export with no practical end. None = the canvas rate.
    fps: float | None = PydField(None, ge=_FPS_MIN, le=_FPS_MAX)
    crf: int = PydField(18, ge=0, le=51)
    # "m4a" / "wav" (QA-100): the timeline's sound alone — mastered to the
    # project's loudness target and true-peak ceiling like a video export,
    # no picture rendered. height/fps/crf/bitrate do not apply to them.
    container: Literal["mp4", "mov", "m4a", "wav"] = "mp4"
    # QA-027: None = the platform target the project carries (set by
    # apply_export_preset); 0 = no target, encode by `crf` (the Quality
    # selector's explicit choice); >0 = that average bitrate in kbps.
    bitrate_kbps: int | None = PydField(None, ge=0, le=200_000)

    @field_validator("bitrate_kbps")
    @classmethod
    def _bitrate_is_encodable(cls, v: int | None) -> int | None:
        # QA-123: 1 kbps passed and produced a smeared file at whatever floor
        # the encoder could reach. 0 still means "encode by quality".
        if v is not None and 0 < v < MIN_EXPORT_BITRATE_KBPS:
            raise ValueError(f"bitrate_kbps must be 0 (encode by quality) or at least "
                             f"{MIN_EXPORT_BITRATE_KBPS} kbps, got {v}")
        return v


#: The lowest average video bitrate an export may target (QA-123) — below this
#: even a 144p picture is mush.
MIN_EXPORT_BITRATE_KBPS = 100


# --- routes ---

@app.get("/api/health")
def health(request: Request):
    # `max_upload_bytes` is advertised here, not just enforced at the ingress,
    # so the phone's Import screen can refuse a too-large pick locally instead
    # of spending five minutes of the user's battery pushing bytes at a server
    # that will answer 413 at the end. For the desktop's own editor it is what
    # the disk can take (`upload_limit: "free_space"`, QA-114), not a fixed cap.
    #
    # `ok` stays the LIVENESS answer (desktop.py, CI and the offline probe poll
    # it); `media_tools` says whether ffmpeg/ffprobe are installed (QA-108) so
    # the UI can show how to install them instead of failing every import.
    from .config import APP_VERSION
    from .api.uploads import advertised_limit
    from .ingest.tools import media_tools_status
    limit, kind = advertised_limit(request)
    return {"ok": True, "version": APP_VERSION,
            "max_upload_bytes": limit, "upload_limit": kind,
            "media_tools": media_tools_status()}


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


@app.get("/api/downloads")
def downloads():
    """Which tools fetch model weights on their next run, and how much
    (QA-065) — what the Captions button and the AI cards badge and check right
    before a run. Separate from /api/features (whose memoised probes must equal
    `check_features`) because this changes the moment a download lands:
    existence checks only, recomputed every call, never downloads anything."""
    from .ai.weights import weights_report
    return {"downloads": weights_report()}


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
    # MCP-ANY-CONTENT-TYPE: a cross-origin page can POST text/plain or a form
    # WITHOUT a CORS preflight; application/json forces one, and the CORS
    # policy refuses it. So a non-JSON body is refused before any tool runs.
    ctype = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if ctype != "application/json":
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32600,
                       "message": "Content-Type must be application/json"}},
            status_code=415,
        )
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
    # QA-099: never the raw id — "Untitled project N" when none is given.
    name = (body.name.strip() if body and body.name and body.name.strip()
            else default_project_name())
    d = session_dir(sid)
    write_meta(sid, {"name": name, "created": time.time()})
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
        # QA-046: how many of the newest ops ⌘Z can still reach — the Undo
        # button and History's undo horizon bind to this, not to ops.length.
        "undo_depth": store.undo_depth,
    }


def _existing_session_or_error(sid: str) -> None:
    """400 for a malformed id, 404 for one that is not on disk — BEFORE
    anything calls `_store(sid)`/`session_dir(sid)`, which create the dir."""
    if not is_valid_session_id(sid):
        raise HTTPException(400, {"code": "invalid_sid", "message": "invalid session id"})
    if not session_exists(sid):
        raise HTTPException(404, {"code": "not_found", "message": "session not found"})


@app.patch("/api/sessions/{sid}")
def rename_session_route(sid: str, body: RenameSessionRequest):
    """QA-099: rename a project (the top-bar chip's inline rename)."""
    _existing_session_or_error(sid)
    try:
        name = rename_session(sid, body.name)
    except ValueError as e:
        raise HTTPException(400, {"code": "invalid_name", "message": str(e)})
    return {"id": sid, "name": name}


@app.get("/api/sessions/{sid}/head")
def session_head(sid: str):
    """The cheapest answer to "is my view of this project still current?"
    (QA-105). A window polls it (and asks on focus) and refreshes when the
    hash moved — another window, an MCP agent or a prompt run edited the
    project. Also the editor's liveness signal (QA-109)."""
    _existing_session_or_error(sid)
    store = _store(sid)
    return {"id": sid, "edl_hash": store.edl.hash(), "ops": len(store.ops.ops),
            "redo_available": store.redo_available}


def _refuse_stale_base(store: EDLStore, body: DispatchRequest) -> None:
    """409 `stale_edl` when the caller's view (`base_hash`) is not the
    timeline the edit would apply to (QA-105). Checked under the session lock
    on the sync path, so it is the exact state the edit would have mutated."""
    if not body.base_hash:
        return
    current = store.edl.hash()
    if body.base_hash != current:
        raise HTTPException(409, {
            "code": "stale_edl",
            "message": "This project changed in another window, so that edit was "
                       "not applied. The timeline has been refreshed — try again.",
            "edl_hash": current,
        })


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
    _require_media_tools()                       # QA-108: the take is transcoded by ffmpeg
    sd = session_dir(sid)
    vo_dir = sd / "uploads" / "vo"
    vo_dir.mkdir(parents=True, exist_ok=True)
    _assert_room_for(request, vo_dir)
    safe_name = _safe_filename(file.filename, "vo.webm")
    # Unique per take (QA-001): every browser recording is named "vo.webm" and
    # the output used to be vo_<whole seconds>.m4a, so two takes in the same
    # second — or two concurrent uploads — wrote one file.
    raw = _unique_upload_path(vo_dir, f"raw_{safe_name}")
    await _stream_upload_to(file, raw)

    # Normalize to AAC mp4 so the audio mixer can splice it cleanly
    norm = _unique_upload_path(vo_dir, f"vo_{int(time.time())}.m4a")
    # Off the event loop (QA-007): a subprocess.run inside this `async def`
    # froze every other request for as long as ffmpeg ran.
    proc = await asyncio.to_thread(
        subprocess.run,
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
        from .agent.dispatch import _free_audio_lane
        track = store.edl.get_track("vo")
        if not track:
            track = Track(id="vo", type="vo", z=0, label="Voiceover")
            store.edl.tracks.append(track)
        at = _tb.quantize(float(start), store.edl.canvas.fps)
        end = at + p.duration
        # A take recorded over an earlier one must not stack on it (the mixer
        # summed both): it goes on the first audio lane with room, else a new
        # one — every audio lane is mixed like the voiceover lane.
        if track.locked or any(isinstance(c, Clip) and c.start < end - 1e-9
                               and c.start + c.effective_duration > at + 1e-9 for c in track.clips):
            track = _free_audio_lane(store.edl, at, end)
        clip = Clip(
            # QA-002: the VO lands on the project frame grid like every
            # other committed edit time, so a click recorded on a flash stays
            # on it after the picture is cut frame-exactly.
            src=str(norm), in_=0.0, out=p.duration,
            start=at,
            audio=AudioProps(gain_db=float(gain_db), fade_in=0.05, fade_out=0.1),
        )
        track.clips.append(clip)
        summary = f"Voiceover {p.duration:.1f}s @ {start:.1f}s ({float(gain_db):+.1f} dB)"
        store.commit("vo_record", {"start": start, "gain_db": gain_db}, summary)
        return {"clip_id": clip.id, "src": str(norm), "duration": p.duration, "track": track.id,
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
    # `UploadLimitMiddleware` short-circuits on `if raw and ...`, so a body sent
    # with `Transfer-Encoding: chunked` and no Content-Length skips it entirely.
    # Without the two calls below this route had no second layer at all and a
    # chunked multipart body would fill the volume — the exact failure
    # api/uploads.py's docstring claims is closed on all six ingresses.
    _assert_room_for(request, sticker_dir)
    dst = _unique_upload_path(sticker_dir, safe_name)   # QA-001: never overwrite
    await _stream_upload_to(file, dst)
    info = {"src": str(dst), "filename": dst.name,
            "display_name": _display_name(file.filename, safe_name)}
    if add_at_playhead:
        store = _store(sid)

        def _edit():
            canvas = store.edl.canvas
            res = dispatch(store, "add_sticker", {
                "src": str(dst),
                "start": float(playhead),
                "end": float(playhead) + 3.0,
                "position": [canvas.w / 2, canvas.h * 0.55],
                "scale": 1.0,
            })
            # The new sticker's id, so the picker can select it (QA-128).
            if isinstance(res, dict) and res.get("sticker_id"):
                info["sticker_id"] = res["sticker_id"]
            return store.edl.hash()

        info["edl_hash"] = await _locked_edit(sid, _edit)
    return info


@app.post("/api/sessions/{sid}/audio_upload")
async def audio_upload(sid: str, request: Request, file: UploadFile = File(...),
                       add_to_music: bool = Form(True),
                       duck: bool | None = Form(None),
                       volume_db: float = Form(-12.0),
                       start: float | None = Form(None)):
    """Upload an audio file (mp3/wav/m4a, or an audio-only mp4/mov) and
    optionally add it to the music track.

    QA-083: the file lands AFTER whatever is already on the music lane (or at
    `start` when the caller names one), whole — never trimmed to the video —
    and the lane's ducking is only changed when `duck` is sent. The answer
    carries `past_video_s` so the UI can offer "Trim to video" instead of
    silently cutting an 85 s narration to a 20 s clip."""
    busy = _prompt_running_response(sid)
    if busy is not None:
        return busy
    _require_media_tools()                       # QA-108: not "damaged audio"
    store = _store(sid)
    sd = session_dir(sid)
    audio_dir = sd / "uploads" / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    _assert_room_for(request, audio_dir)
    safe_name = _safe_filename(file.filename, "audio.mp3")
    display_name = _display_name(file.filename, safe_name)
    # QA-001: a/song.wav then b/song.wav used to share uploads/audio/song.wav,
    # so the first music clip silently started playing the second song.
    dst = _unique_upload_path(audio_dir, safe_name)
    await _stream_upload_to(file, dst)
    # Probe to get duration
    from .ingest.probe import probe as _probe
    try:
        p = _probe(dst)
    except Exception as e:
        dst.unlink(missing_ok=True)
        raise HTTPException(422, {"file": safe_name, "error": "couldn't_import",
                                  "message": "Couldn't read this audio file — it may be damaged "
                                             "or in a format we can't decode.",
                                  "detail": str(e)[-300:]})
    _remember_display_name(sd, dst, display_name)

    placed: dict = {}
    if add_to_music:
        placed = await _locked_edit(
            sid, lambda: _add_uploaded_music(store, dst, p.duration, duck, volume_db, start))

    return {"src": str(dst), "duration": p.duration, "edl_hash": store.edl.hash(),
            "display_name": display_name, **placed}


def _remember_display_name(session_dir_: Path, src: Path, name: str) -> None:
    """Keep the user's real file name for a loose upload (QA-045): the disk
    name is ASCII-only and uniquified, and the Media panel lists loose files
    from disk, so without this a Hindi or emoji name was lost for good."""
    from .media_library import record_display_name
    try:
        record_display_name(session_dir_, src, name)
    except OSError:
        pass  # a name is a nicety; never fail an import over it


def _forget_display_name(session_dir_: Path, src: Path) -> None:
    from .media_library import forget_display_name
    try:
        forget_display_name(session_dir_, src)
    except OSError:
        pass


def _add_uploaded_music(store, dst: Path, duration: float, duck: bool | None,
                        volume_db: float, start: float | None = None) -> dict:
    """The music-track edit of `audio_upload`, run under the session lock.

    Placement (QA-083): after the last clip already on the music lane, so a
    second file never stacks at 0 s over the first (the mixer summed them);
    an explicit `start` wins. Length: the whole file. Trimming it to the video
    — the rule this replaced — silently cut an 85 s narration to 20 s. How far
    it runs past the picture is returned as `past_video_s`, and the UI offers
    "Trim to video" from that, so the long-song case that rule was protecting
    (a 6-minute bed on a 29 s video keeps the transport running) is one click
    away instead of a silent truncation."""
    store.edl.recompute_duration()
    music = store.edl.get_track("music")
    lane_end = max((float(c.start) + c.effective_duration for c in (music.clips if music else [])
                    if isinstance(c, Clip)), default=0.0)
    # On the project's frame grid (wave A's timebase rule): the previous bed
    # ends at its probe length (85.023447 s), not on a frame. CEIL, so the
    # new bed never overlaps the one before it.
    fps = store.edl.canvas.fps
    at = (_tb.ceil_to_frame(lane_end, fps) if start is None
          else _tb.quantize(max(0.0, float(start)), fps))
    args: dict = {"src": str(dst), "start": at, "in": 0.0, "out": duration,
                  "volume_db": volume_db}
    if duck is not None:
        args["duck"] = duck
    result = dispatch(store, "add_music", args)
    video_extent = store.edl.video_extent()
    clip_id = result.get("clip_id") if isinstance(result, dict) else None
    placed = next((c for c in store.edl.get_track("music").clips if c.id == clip_id), None)
    end = (float(placed.start) + placed.effective_duration) if placed is not None else at + duration
    past = end - video_extent if video_extent > 0.05 else 0.0
    return {"clip_id": clip_id, "start": at,
            "past_video_s": round(past, 3) if past > 0.05 else 0.0,
            "video_end": round(video_extent, 3)}


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
    # See sticker_upload: the Content-Length middleware is not reached by a
    # chunked body, so the free-space precondition and the mid-stream running
    # total are the real limits on this route.
    _assert_room_for(request, uploads)
    dst = _unique_upload_path(uploads, safe_name)   # QA-001: never overwrite
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

    QA-111, two more rules. (1) A frame shape somebody CHOSE is kept: picking
    1:1 and then importing a 16:9 clip used to turn the project 16:9 anyway.
    (2) The change is applied to the tree directly, not dispatched as its own
    `set_aspect_ratio` op — the caller folds it into the add_clip commit, so
    one Undo takes the import and the canvas change back together (it used to
    take two, and the first left an empty 16:9 project behind).
    """
    video = probe.video
    if not video or not video.width or not video.height:
        return
    if _user_chose_canvas(store):
        return
    w, h = video.width, video.height
    if w == h:
        ratio = "1:1"
    elif w > h:
        ratio = "16:9"
    else:
        ratio = "9:16"
    from .agent.dispatch import _RATIOS, _rescale_overlays_for_canvas_change
    canvas = store.edl.canvas
    new_w, new_h = _RATIOS[ratio]
    if (canvas.w, canvas.h) == (new_w, new_h):
        return
    old_w, old_h = canvas.w, canvas.h
    canvas.w, canvas.h = new_w, new_h
    # Same overlay re-placement set_aspect_ratio does: a title added before
    # the first import keeps its relative position in the new frame.
    _rescale_overlays_for_canvas_change(store.edl, old_w, old_h, new_w, new_h)


def _user_chose_canvas(store) -> bool:
    """Someone chose the project's frame shape (QA-111): a canvas-choice op in
    the log (undoing it pops the op, so the auto-match comes back with it), or
    a choice that changed nothing and so logged no op (QA-130) — remembered in
    meta.json by dispatch()."""
    from .agent.dispatch import canvas_choice_remembered, is_canvas_choice
    if any(is_canvas_choice(op.tool, op.args) for op in store.ops.ops):
        return True
    return canvas_choice_remembered(store)


def _user_chose_fps(store) -> bool:
    """True once anyone explicitly set the project frame rate (set_canvas with
    an fps). Ingest must not override that choice (QA-009)."""
    return any(op.tool == "set_canvas" and isinstance(op.args, dict) and op.args.get("fps") is not None
               for op in store.ops.ops)


def _place_ingested_clip(store, res) -> bool:
    """Put a freshly ingested video on v1 (runs under the session lock).
    Returns whether the timeline was empty before, i.e. this started a project."""
    # ONE commit for the whole import (QA-111): the canvas match, the timebase
    # and the clip. batch() also rolls the in-memory tree back if add_clip
    # refuses, so a failed import cannot leave a changed canvas with no op.
    with store.batch():
        was_empty, add_args, result = _place_ingested_clip_uncommitted(store, res)
    summary = result.get("summary", "add_clip") if isinstance(result, dict) else "add_clip"
    store.commit("add_clip", add_args, str(summary))
    return was_empty


def _place_ingested_clip_uncommitted(store, res) -> tuple[bool, dict, object]:
    from .edl import timebase as _tb
    v1 = store.edl.get_track("v1")
    was_empty = not any(True for _ in (v1.clips if v1 else []))
    if was_empty:
        _match_canvas_to_source(store, res.probe)
        # The project timebase follows the first clip (QA-009) unless the user
        # already picked one. Set in the same commit as add_clip, so one Undo
        # removes the clip and restores the old rate together.
        if res.fps and not _user_chose_fps(store):
            store.edl.canvas.fps = _tb.fps_float(res.fps)
    store.edl.recompute_duration()
    # Append after the last V1 clip — NOT after `edl.duration`, which spans
    # every track. Importing a 6-minute song first would otherwise park the
    # next video at start=373s, stranding it behind minutes of black (now
    # that gaps actually render, that black is real footage in the export).
    start = store.edl.video_extent()
    if getattr(res, "still", False):
        # QA-090: a photo lands at the default still length; its source runs
        # for minutes, so it can be extended like any clip.
        from .ingest.still import STILL_DEFAULT_SECONDS
        out = _tb.quantize(STILL_DEFAULT_SECONDS, store.edl.canvas.fps)
    else:
        # The PICTURE's frame-exact length, never format.duration — for an
        # AAC mp4 that is the padded audio, and a 600-frame clip came in as
        # out=20.01 (QA-002). add_clip clamps to it as well.
        out = _tb.floor_to_frame(
            video_frame_extent(Path(res.normalized)) or res.probe.duration,
            store.edl.canvas.fps)
    add_args = {
        "track": "v1",
        "src": str(res.normalized),
        "in": 0.0,
        "out": out,
        "start": start,
    }
    return was_empty, add_args, dispatch(store, "add_clip", add_args)


def _media_tools_http() -> HTTPException | None:
    """The one 503 `ffmpeg_missing` (QA-108) when ffmpeg/ffprobe cannot be
    found right now, else None. Every route that shells out answers with it
    instead of blaming the user's file ("may not be a valid video"), a bare
    500, or "corrupt frames"."""
    from .ingest.tools import MediaToolsMissing, missing_media_tools
    missing = missing_media_tools()
    if not missing:
        return None
    return HTTPException(503, MediaToolsMissing(missing).detail())


def _require_media_tools() -> None:
    err = _media_tools_http()
    if err is not None:
        raise err


@app.exception_handler(FileNotFoundError)
async def _missing_binary_handler(request: Request, exc: FileNotFoundError):
    """Safety net for every route not guarded above (a voice-over transcode, a
    thumbnail, a waveform…): a `FileNotFoundError` while ffmpeg/ffprobe are
    missing is the 503 `ffmpeg_missing`, not a bare 500 + traceback. Any other
    FileNotFoundError keeps the generic 500 exactly as before."""
    missing = _media_tools_http()
    if missing is not None:
        from .api.hardening import _envelope
        rid = getattr(request.state, "request_id", "")
        return _envelope(status=503, code="FFMPEG_MISSING", message=missing.detail["message"],
                         request_id=rid, details=missing.detail)
    return await app.exception_handlers[Exception](request, exc)


def _ingest_failure(safe_name: str, e: Exception,
                    diagnosis: tuple[str, str] | None = None) -> HTTPException:
    """ANY ingest failure (unreadable container, exotic codec, corrupt file,
    ffprobe/ffmpeg error, JSON parse, etc.) must be a clean 422 — never a bare
    500. This is the "video import failed" path users hit with files that
    aren't really valid video.

    `diagnosis` (QA-112, `ingest.diagnose.diagnose_unreadable`) is what the
    file itself says went wrong — empty, truncated, not media at all, an
    undecodable codec — and replaces the one generic sentence all of those
    used to share."""
    import logging
    logging.getLogger("video_ai_editor").warning(
        "upload ingest failed for %s: %s", safe_name, e)
    tools = _media_tools_http()
    if tools is not None:
        return tools
    msg = str(e)
    # A filesystem refusal is not a codec problem (QA-091): telling the user to
    # re-export a perfectly valid video as H.264 sends them the wrong way.
    import errno as _errno
    err_no = getattr(e, "errno", None) if isinstance(e, OSError) else None
    if err_no == _errno.ENAMETOOLONG:
        code, text = "name_too_long", ("Couldn't import this file — its name is too long to "
                                       "store. Rename it to something shorter and import it again.")
    elif err_no == _errno.ENOSPC:
        code, text = "disk_full", ("Couldn't import this file — the disk is full. Free some "
                                   "space and import it again.")
    elif isinstance(e, OSError) and not isinstance(e, FileNotFoundError):
        code, text = "storage_error", (f"Couldn't import this file — it couldn't be written to "
                                       f"the project folder ({e.strerror or 'file system error'}).")
    elif diagnosis is not None:
        code, text = diagnosis
    else:
        code, text = "couldn't_import", ("Couldn't import this file — it may not be a valid video, "
                                         "or it uses a codec/container we can't read. Try exporting "
                                         "it as a standard H.264 .mp4 and re-importing.")
    return HTTPException(status_code=422, detail={
        "file": safe_name,
        "error": code,
        "message": text,
        "detail": msg[-300:] if len(msg) > 300 else msg,
    })


def _background_transcriber(normalized_path: Path, out_dir: Path, whisper_model: str):
    """The whisper pass that runs after the upload has answered. Writes the
    transcript into the upload's own ingest.json so get_transcript /
    add_caption_track find it. `whisper_model` opts the user into a smaller
    model — `tiny.en` is ~5× faster than `small` for English-only content;
    `small` (default) is multilingual."""
    from .ingest.transcribe import transcribe as _transcribe
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
    return _bg_transcribe


def _handoff_audio_only(sid: str, dst: Path, upload_dir: Path, safe_name: str,
                        display_name: str, duration: float, add_to_timeline: bool) -> dict:
    """An audio-only file sent to the video ingress (QA-092): move it to
    uploads/audio and add it to the music lane like `audio_upload` does."""
    sd = session_dir(sid)
    audio_dir = sd / "uploads" / "audio"
    target = _unique_upload_path(audio_dir, safe_name)
    try:
        _pu.replace_with_retry(dst, target)
    finally:
        shutil.rmtree(upload_dir, ignore_errors=True)
    _remember_display_name(sd, target, display_name)
    placed: dict = {}
    if add_to_timeline:
        with _session_lock(sid):
            placed = _add_uploaded_music(_store(sid), target, duration, None, -12.0)
    return {"kind": "audio", "routed_to": "music", "src": str(target),
            "display_name": display_name, "duration": duration,
            "edl_hash": _store(sid).edl.hash(), "transcript_pending": False,
            "notices": [f"{display_name} has no picture, so it was added to the Music lane."],
            **placed}


@app.post("/api/sessions/{sid}/upload")
async def upload(sid: str, request: Request, background_tasks: BackgroundTasks,
                 file: UploadFile = File(...),
                 add_to_timeline: bool = Form(True),
                 transcribe: bool = Form(True),
                 whisper_model: str = Form(""),
                 wait: int = 1):
    """Import a video: stream it to disk, normalise it, put it on v1.

    Normalisation runs OFF the event loop (QA-007). It used to be a plain
    synchronous call inside this `async def`, so the single uvicorn loop could
    serve nothing — not /api/health, not an edit, not a thumbnail — until
    ffmpeg finished: 125 s for a 12-minute file, 233 s for 4K. Now:

    * ``wait=1`` (default): the normalise runs on a worker thread and this
      request answers when it is done, with the same body as before.
    * ``wait=0``: answers ``202 {job_id}`` as soon as the bytes are on disk;
      poll ``GET /api/jobs/{id}`` for ``progress`` (ffmpeg's own
      ``-progress``, 0..1) and the same body under ``result``.

    Storage (QA-001): every import gets its own directory
    ``uploads/<stem>_<random>/`` holding the raw file, the normalised mp4 and
    ingest.json, so two files whose names sanitise alike can never overwrite
    each other. The user's real filename is kept as ``display_name``.
    """
    busy = _prompt_running_response(sid)
    if busy is not None:
        return busy
    # QA-108: without ffmpeg nothing can be imported — say so before the user
    # spends minutes sending the bytes, and never blame their file.
    _require_media_tools()
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
    display_name = _display_name(file.filename, safe_name)
    upload_dir = _unique_upload_dir(uploads, Path(safe_name).stem)
    dst = upload_dir / safe_name
    try:
        await _stream_upload_to(file, dst)
    except BaseException:
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise

    def _ingest_and_place(set_progress=None, cancel_event=None) -> dict:
        # A cheap probe first: an audio-only file (QA-092) goes to the music
        # lane without being transcoded into a picture-less mp4 first.
        from .ingest.probe import probe as _probe
        try:
            pre = _probe(dst)
        except Exception:
            pre = None                  # ingest below reports the real error
        # QA-112: a probe that SUCCEEDS is not proof of media — ffmpeg reads
        # a text file as ANSI art and random bytes as a picture. Refuse those
        # before they are normalised into a clip (and re-shape the canvas).
        from .ingest.diagnose import refuse_before_ingest, refuse_short_read
        refusal = refuse_before_ingest(dst, pre)
        if refusal is not None:
            shutil.rmtree(upload_dir, ignore_errors=True)
            raise _ingest_failure(safe_name, ValueError(refusal[0]), refusal)
        if pre is not None and pre.streams and pre.video is None:
            return _handoff_audio_only(sid, dst, upload_dir, safe_name, display_name,
                                       pre.duration, add_to_timeline)
        try:
            res = ingest_upload(dst, upload_dir, transcribe_audio=False,
                                display_name=display_name,
                                on_progress=set_progress, cancel_event=cancel_event,
                                still_fps=_store(sid).edl.canvas.fps)
        except Exception as e:
            # QA-112: ask the file what is wrong with it BEFORE it is deleted.
            from .ingest.diagnose import diagnose_unreadable
            diagnosis = None
            if not isinstance(e, OSError) or isinstance(e, FileNotFoundError):
                try:
                    diagnosis = diagnose_unreadable(dst, str(e)[-2000:])
                except Exception:           # a diagnosis must never mask the failure
                    diagnosis = None
            shutil.rmtree(upload_dir, ignore_errors=True)
            raise _ingest_failure(safe_name, e, diagnosis) from e

        # QA-112: a file cut short with its index intact still declares its
        # full length; normalising decodes only what is there.
        short = None if res.still or pre is None else \
            refuse_short_read(pre.duration, res.probe.duration)
        if short is not None:
            shutil.rmtree(upload_dir, ignore_errors=True)
            raise _ingest_failure(safe_name, ValueError(short[0]), short)

        # /upload is the VIDEO ingress and hardcodes track v1 below. An
        # audio-only file (an .mp4/.mov/.mkv with no picture — the frontend
        # routes by extension, so it lands here) must never reach v1: it would
        # break every render with "[i:v] … matches no streams". QA-092: it
        # used to be refused with advice ("Add music…", "drop it on the Music
        # lane") that routed it straight back here, so the file had no way in.
        # It is handed to the audio path instead, exactly as if it had been
        # added with Add music.
        if res.probe.streams and res.probe.video is None:
            return _handoff_audio_only(sid, dst, upload_dir, safe_name, display_name,
                                       res.probe.duration, add_to_timeline)

        # Wave D: the instant-preview proxy of the new master, built in the
        # background (niced) — only when the preview engine is on.
        _queue_preview_proxy(sid, res.normalized)

        if add_to_timeline:
            with _session_lock(sid):
                # Re-resolved under the lock: the LRU may have evicted and
                # rebuilt the store while a wait=0 job sat in the queue.
                live = _store(sid)
                was_empty = _place_ingested_clip(live, res)
            if was_empty:
                # This upload starts a brand-new project on an empty timeline —
                # any chat history is necessarily about DIFFERENT, no-longer-
                # present footage (or a prior session's resumed project).
                # Replaying it to Claude is how "describe this video" answers
                # end up describing a video from a past conversation. A
                # mid-project upload (b-roll added to existing footage)
                # intentionally keeps history, since that context is still
                # relevant.
                _save_history(sid, [])

        return {
            "src": str(dst),
            "normalized": str(res.normalized),
            "display_name": display_name,
            "duration": res.probe.duration,
            "probe": res.probe.model_dump(),
            "fps": res.fps,
            "color": res.color,
            "notices": res.notices,
            "edl_hash": _store(sid).edl.hash(),
            "transcript_pending": bool(transcribe) and not res.still,
            "kind": "image" if res.still else "video",
        }

    # Whisper is the slow part (10-60s on CPU); it runs after we've answered
    # and the transcript arrives later via GET /transcript.
    bg = _background_transcriber(upload_dir / f"{dst.stem}.normalized.mp4",
                                 upload_dir, whisper_model) if transcribe else None

    if not wait:
        def _job(set_progress=None, cancel_event=None) -> dict:
            try:
                out = _ingest_and_place(set_progress=set_progress, cancel_event=cancel_event)
            except HTTPException as e:
                detail = e.detail if isinstance(e.detail, dict) else {"message": str(e.detail)}
                raise RuntimeError(detail.get("message") or str(detail)) from e
            if bg is not None and out.get("kind") == "video":
                threading.Thread(target=bg, daemon=True, name="vai-transcribe").start()
            return out
        from .api.jobs import JOB_MANAGER
        job = JOB_MANAGER.submit(kind="upload", fn=_job, session_id=sid)
        return JSONResponse(status_code=202, content={
            "job_id": job.id, "status": job.status, "src": str(dst),
            "display_name": display_name})

    out = await asyncio.to_thread(_ingest_and_place)
    if bg is not None and out.get("kind") == "video":
        background_tasks.add_task(bg)
    return out


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
def dispatch_tool(sid: str, body: DispatchRequest, wait: int = 1,
                  include: str | None = Query(None, max_length=64)):
    """Run a tool.

    `wait=1` (default): blocks and returns the result — the long-standing
    behaviour every existing caller relies on.
    `wait=0`: returns 202 + `{job_id, status_url}` and runs the tool on the job
    executor. The completed job's `result` is the same payload the sync path
    returns. Intended for ASYNC_DISPATCH_TOOLS; permitted for any tool so the
    UI never needs a second allowlist.
    `include=edl` (wave D, INSTANT_PREVIEW_SPEC §4.1/§5.2): the answer also
    carries the post-op `edl` and its `render_hash`, serialised under the same
    session lock as the edit, so the instant-preview engine never needs a
    second round trip. Without it the answer is byte-for-byte what it was.
    """
    include_edl = _wants_edl(include)
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
        # The queued job re-checks under the lock; this answers a stale view
        # with the 409 right away instead of as a failed job.
        _refuse_stale_base(store, body)
        return _dispatch_async(sid, store, body, include_edl=include_edl)
    with _session_lock(sid):
        _refuse_stale_base(store, body)
        return _dispatch_sync(sid, store, body, include_edl=include_edl)


#: Past this many bytes of EDL JSON a dispatch answer carries `edl_omitted`
#: instead of the EDL (spec §14 risk 12); the client then GETs /edl.
DISPATCH_EDL_MAX_BYTES = 1_000_000


def _wants_edl(include: str | None) -> bool:
    return "edl" in {p.strip().lower() for p in (include or "").split(",")}


def _edl_payload(store: EDLStore) -> dict:
    """`edl` + `render_hash` for a dispatch answer (called under the session
    lock, so it is exactly the state the edit produced). `render_hash` is the
    key a preview render of this state is stored under — offline-aware, the
    same value `/preview` and `preview.mp4` use."""
    text = store.edl.to_json()
    out: dict = {"render_hash": _preview_edl(store).render_hash()}
    if len(text.encode("utf-8")) > DISPATCH_EDL_MAX_BYTES:
        out["edl_omitted"] = True
    else:
        out["edl"] = json.loads(text)
    return out


def _dispatch_async(sid: str, store: EDLStore, body: DispatchRequest, *,
                    include_edl: bool = False) -> JSONResponse:
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
                live = _store(sid)
                _refuse_stale_base(live, body)
                return _dispatch_sync(sid, live, body,
                                      set_progress=set_progress,
                                      cancel_event=cancel_event,
                                      include_edl=include_edl)
        except HTTPException as e:
            # JobManager records `f"{type(e).__name__}: {e}"`, and an
            # HTTPException stringifies to its repr — unreadable in the UI's
            # job-error toast. Carry the detail across instead.
            detail = e.detail
            if isinstance(detail, dict) and detail.get("error") == "ffmpeg_missing":
                raise RuntimeError(detail["message"]) from e     # QA-108: the sentence, not JSON
            raise RuntimeError(detail if isinstance(detail, str) else json.dumps(detail)) from e

    job = JOB_MANAGER.submit(kind=f"dispatch:{body.tool}", fn=_job, session_id=sid)
    return JSONResponse(
        status_code=202,
        content={"job_id": job.id, "status": job.status,
                 "status_url": f"/api/jobs/{job.id}"},
    )


def _dispatch_sync(sid: str, store: EDLStore, body: DispatchRequest, *,
                   set_progress=None, cancel_event=None,
                   include_edl: bool = False) -> dict:
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
        _require_media_tools()                   # QA-108: say "install ffmpeg"
        raise HTTPException(422, str(e))
    except (OSError, subprocess.SubprocessError) as e:
        _require_media_tools()                   # QA-108
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
    out = {
        "result": result,
        "edl_hash": store.edl.hash(),
        "op": last_op.model_dump() if last_op else None,
        "undo_depth": store.undo_depth,  # QA-046
    }
    if include_edl:
        out.update(_edl_payload(store))
    if last_op is not None:
        _queue_timeline_proxies(sid, store)
    return out


def _queue_preview_proxy(sid: str, src) -> None:
    """Queue the eager proxy build of one source (wave D, spec §4.4). A no-op
    unless eager proxies are enabled (preview.engine != server by default),
    and never an error for the caller: a proxy is a cache."""
    from .preview_setting import eager_proxies_enabled
    if not eager_proxies_enabled():
        return
    try:
        from .ingest.proxy_queue import MANAGER as _PROXIES
        _PROXIES.ensure(src, sid=sid)
    except Exception:
        get_logger().warning("preview proxy queue failed", exc_info=True)


def _queue_timeline_proxies(sid: str, store: EDLStore) -> None:
    """After an edit: queue proxies of every media source on the timeline
    that has none yet — how AI outputs landing in cache/ (stabilize, upscale,
    reframe, …) get theirs without each writer knowing about proxies."""
    from .preview_setting import eager_proxies_enabled
    if not eager_proxies_enabled():
        return
    seen: set[str] = set()
    for t in store.edl.tracks:
        if t.type not in ("video", "audio", "music", "vo"):
            continue
        for c in t.clips:
            src = getattr(c, "src", None)
            if src and src not in seen:
                seen.add(src)
                if Path(src).is_file():
                    _queue_preview_proxy(sid, src)


@app.get("/api/sessions/{sid}/edl")
def get_edl(sid: str):
    store = _store(sid)
    return JSONResponse(json.loads(store.edl.to_json()))


# --- Media library (QA-010): the Media panel's list, independent of the timeline ---

@app.get("/api/sessions/{sid}/media")
def get_media(sid: str):
    """Everything imported into this project, with how many timeline clips use
    each item. Deleting the last clip no longer removes the media from the bin."""
    from .media_library import list_media
    store = _store(sid)
    rows = list_media(store.dir, store.edl)
    # Wave D (INSTANT_PREVIEW_SPEC §5.2): fps {num,den}, frames, pix_fmt,
    # has_audio and proxy {key,state,w,h} per row, from what the proxy layer
    # already knows — never a probe on this hot path.
    from .api.preview_routes import media_row_fields
    return {"media": [{**r, **media_row_fields(r.get("src"))} for r in rows]}


@app.delete("/api/sessions/{sid}/media/{media_id}")
def remove_media_route(sid: str, media_id: str):
    """Take an item out of the bin. Its file stays on disk (undo can still need
    it); an item that timeline clips still use is refused with 409."""
    from .media_library import MediaInUse, remove_media
    if not re.fullmatch(r"[0-9a-f]{12}", media_id):
        raise HTTPException(400, {"code": "invalid_media_id", "message": "invalid media id"})
    store = _store(sid)
    try:
        item = remove_media(store.dir, store.edl, media_id)
    except KeyError:
        raise HTTPException(404, {"code": "media_not_found", "message": "no such media in this project"})
    except MediaInUse as e:
        raise HTTPException(409, {"code": "media_in_use", "message": str(e),
                                  "clip_ids": e.item["clip_ids"]})
    return {"removed": item["id"], "name": item["name"]}


@app.post("/api/sessions/{sid}/media/{media_id}/relink")
async def relink_media_route(sid: str, media_id: str, request: Request,
                             file: UploadFile = File(...)):
    """Relink an offline item (QA-095): import the replacement the same way
    its kind is imported (video → normalised like /upload, audio → as-is),
    then `dispatch("relink_media")` every clip that played the missing file —
    one undoable edit."""
    from .media_library import list_media
    _existing_session_or_error(sid)
    if not re.fullmatch(r"[0-9a-f]{12}", media_id):
        raise HTTPException(400, {"code": "invalid_media_id", "message": "invalid media id"})
    busy = _prompt_running_response(sid)
    if busy is not None:
        return busy
    store = _store(sid)
    item = next((it for it in list_media(store.dir, store.edl) if it["id"] == media_id), None)
    if item is None:
        raise HTTPException(404, {"code": "media_not_found", "message": "no such media in this project"})
    # The clips hold the path as they were given it; the library row is the
    # resolved path. Relink every spelling that resolves to it.
    olds = sorted({c.src for t in store.edl.tracks for c in t.clips
                   if getattr(c, "src", None) and Path(c.src).resolve() == Path(item["src"])})
    if not olds:
        raise HTTPException(409, {"code": "media_unused",
                                  "message": f"{item['name']} is not used on the timeline"})
    uploads = session_dir(sid) / "uploads"
    _assert_room_for(request, uploads)
    safe_name = _safe_filename(file.filename, "relink.mp4")
    display_name = _display_name(file.filename, safe_name)
    work = _unique_upload_dir(uploads, Path(safe_name).stem)
    dst = work / safe_name
    try:
        await _stream_upload_to(file, dst)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise

    def _prepare() -> str:
        if item["kind"] == "audio":
            target = _unique_upload_path(uploads / "audio", safe_name)
            _pu.replace_with_retry(dst, target)
            shutil.rmtree(work, ignore_errors=True)
            _remember_display_name(session_dir(sid), target, display_name)
            return str(target)
        try:
            res = ingest_upload(dst, work, transcribe_audio=False, display_name=display_name,
                                still_fps=store.edl.canvas.fps)
        except Exception as e:
            shutil.rmtree(work, ignore_errors=True)
            raise _ingest_failure(safe_name, e) from e
        return str(res.normalized)

    new_src = await asyncio.to_thread(_prepare)

    def _discard_replacement() -> None:
        # A refused relink keeps nothing: the audio branch MOVED the file into
        # uploads/audio (where the library lists it as a new loose item) and
        # emptied `work`, so removing `work` alone leaked it.
        shutil.rmtree(work, ignore_errors=True)
        if item["kind"] == "audio":
            try:
                Path(new_src).unlink(missing_ok=True)
            except OSError:
                pass
            _forget_display_name(session_dir(sid), Path(new_src))

    def _edit() -> dict:
        live = _store(sid)
        ids: list[str] = []
        with live.batch():
            for old in olds:
                ids += dispatch(live, "relink_media", {"src": old, "new_src": new_src})["clip_ids"]
        summary = f"Relinked {item['name']} → {display_name}"
        live.commit("relink_media", {"src": olds[0], "new_src": new_src}, summary)
        return {"relinked": ids, "new_src": new_src, "name": display_name,
                "summary": summary, "edl_hash": live.edl.hash()}

    try:
        return await _locked_edit(sid, _edit)
    except ValueError as e:
        # Too short / no picture: the replacement is not kept.
        _discard_replacement()
        raise HTTPException(422, {"error": "relink_refused", "message": str(e)})


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


def _render_error_types() -> tuple[type[BaseException], ...]:
    """What a render can raise that is a FAILED RENDER, not a server bug
    (QA-107): ffmpeg's RuntimeError, PIL's OSError ("invalid pixel size"),
    a ValueError out of layout, and freetype's FT_Exception ("raster
    overflow"). Only RuntimeError used to map to 422, so the rest escaped
    as a bare 500 from every later preview/export."""
    types: list[type[BaseException]] = [RuntimeError, OSError, ValueError]
    try:
        import freetype as _ft  # optional (render/shaping.py)
        types.append(_ft.FT_Exception)
    except Exception:  # pragma: no cover - freetype-py absent
        pass
    return tuple(types)


RENDER_ERRORS = _render_error_types()


def _render_failure_message(ffmpeg_tail: str, full: str | None = None, *, kind: str = "preview") -> str:
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
    # 0. Our OWN post-render checks (compositor._check_picture): the render
    # finished but does not match the timeline. Review RE: an export rejected
    # for its frame count told the user "Couldn't render a preview for this
    # clip — it may have corrupt frames": wrong on both counts.
    m = re.search(r"came out (\d+) frames long where the timeline plans (\d+)", hay)
    if m:
        what = "export" if kind == "export" else "preview"
        return (f"The {what} came out {m.group(1)} frames long where the timeline plans {m.group(2)}, so it "
                f"was not kept. If the timeline changed while it rendered, {what} again. Your media is fine; "
                f"if it happens again on an unchanged timeline, it is a bug in the app.")
    if "The render produced no picture, only sound" in hay:
        return hay[hay.index("The render produced no picture"):].splitlines()[0]
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

    if kind == "export":
        return ("Couldn't export — a clip may have corrupt frames or an unusual codec. "
                "Check the clip named in the details, or convert it to MP4 and try again.")
    return ("Couldn't render a preview for this clip — it may have corrupt "
             "frames or an unusual codec.")


class _PreviewTicket:
    """One HTTP client's claim on an interactive preview render (QA-041).

    The request coroutine and the render thread meet here, under one lock:
    the render thread records which `render.cancel.PREVIEWS` event it joined,
    the coroutine records that its client went away. Whichever happens second
    calls `PREVIEWS.abandon` — exactly once — so a closed tab cancels a
    render nobody else is waiting for, and never one another window shares.
    """

    def __init__(self, sid: str) -> None:
        self.sid = sid
        self._lock = threading.Lock()
        self._ev: threading.Event | None = None
        self._finished = False
        self.gone = False
        self.abandoned = False

    def joined(self, ev: threading.Event) -> None:
        from .render import cancel as _rcancel
        with self._lock:
            self._ev = ev
            if self.gone and not self.abandoned:
                self.abandoned = True
                _rcancel.PREVIEWS.abandon(self.sid, ev)

    def client_gone(self) -> None:
        from .render import cancel as _rcancel
        with self._lock:
            self.gone = True
            if self._ev is not None and not self._finished and not self.abandoned:
                self.abandoned = True
                _rcancel.PREVIEWS.abandon(self.sid, self._ev)

    def finish(self, ev: threading.Event) -> None:
        from .render import cancel as _rcancel
        with self._lock:
            self._finished = True
            _rcancel.PREVIEWS.end(self.sid, ev, abandoned=self.abandoned)


#: How long a render waits for an in-flight edit before copying the EDL
#: without the session lock (a Prompt run holds it for its whole run).
_RENDER_SNAPSHOT_WAIT_S = 2.0


def _edl_snapshot(sid: str, store):
    """A PRIVATE deep copy of the session's EDL, taken under its dispatch lock.

    Review RE: renders read the live, mutable `store.edl` — dispatch edits it
    IN PLACE (`track.clips.remove(c)`) — so a delete landing while a render
    ran made the hash, the filtergraph and the frame-count check (`_check_
    picture`, fatal since wave E) describe three different timelines: an
    export failed at 99.8 % ("660 video frames, the timeline plans 480"), a
    Cmd+B/Delete in the UI answered POST /preview 422, and a preview of the
    EDITED timeline was cached under the PRE-edit hash (Undo then served it).
    Hash, render and check now all read this one object."""
    lock = _session_lock(sid)
    got = lock.acquire(timeout=_RENDER_SNAPSHOT_WAIT_S)
    try:
        for _ in range(3):
            try:
                return store.edl.model_copy(deep=True)
            except RuntimeError:            # a list changed under an unlocked copy: try again
                if got:
                    raise
        return store.edl.model_copy(deep=True)
    finally:
        if got:
            lock.release()


def _preview_edl(store, sid: str | None = None):
    """The EDL a preview renders: the live one (a private snapshot when `sid`
    is given — every RENDER passes it), or — when media is missing — a copy
    with slates in its place (media_offline.render_edl, QA-095)."""
    from .media_offline import render_edl
    base = _edl_snapshot(sid, store) if sid is not None else store.edl
    try:
        return render_edl(base, store.dir)
    except Exception:
        # A slate that cannot be made must not take the preview down with it;
        # the render then reports the missing file as it always did.
        import logging
        logging.getLogger("video_ai_editor").warning("offline slate failed", exc_info=True)
        return base


def _export_edl(edl, height: int | None, *, dry_run: bool = False,
                on_progress=None, cancel_event=None):
    """The EDL an export renders (QA-089): clips whose source was clamped to a
    1080p editing proxy render from a full-quality master of the original when
    the export is larger than the proxy — never the proxy scaled back up.
    `dry_run` answers whether any master is needed without making one."""
    from .ingest.masters import export_edl, master_plan
    from .render.compositor import export_dimensions
    w, h = export_dimensions(edl.canvas.w, edl.canvas.h, height)
    short = min(w, h)
    if dry_run:
        return edl.model_copy() if master_plan(edl, short) else edl
    return export_edl(edl, short, on_progress=on_progress, cancel_event=cancel_event)


def _refuse_missing_media(store, verb: str) -> None:
    """422 naming every missing file (QA-095) — an export must not quietly
    ship slates, nor fail with "a clip's source file is missing"."""
    from .media_offline import missing_media, missing_message
    rows = missing_media(store.dir, store.edl)
    if rows:
        raise HTTPException(422, {"error": "media_missing",
                                  "message": missing_message(rows, verb),
                                  "missing": rows})


def _refuse_empty_export(store) -> None:
    """QA-123: an export of a timeline with nothing on it used to reach ffmpeg
    and come back as "Couldn't render a preview for this clip — it may have
    corrupt frames". There is no clip, and nothing is corrupt."""
    if not any(track.clips for track in store.edl.tracks):
        raise HTTPException(422, {"error": "nothing_to_export",
                                  "message": "Nothing to export — the timeline is empty. "
                                             "Add a clip, then export."})


def _render_preview_latest(sid: str, store, ticket: _PreviewTicket | None = None,
                           low_priority: bool = False):
    """render_preview for an interactive client, newest-EDL-wins (QA-004).

    Registers the render with `render.cancel.PREVIEWS`: a request for a
    DIFFERENT EDL hash of the same session terminates this one's ffmpeg and
    raises RenderCancelled here. Nobody will look at a superseded preview, and
    before this every one of them ran to completion, holding `_RENDER_SLOTS`
    while the render the user was actually waiting for queued behind it.

    QA-041: a render nobody supersedes is still bounded — a wall-clock
    deadline (`cancel.preview_deadline_s`, proportional to the timeline)
    raises RenderTimedOut — and `ticket` lets the HTTP layer abandon it when
    its client disconnects (`_preview_for_client`).
    """
    from .render import cancel as _rcancel
    # QA-095: missing media previews as a "Media offline" slate; the other
    # clips still play, and the render's key reflects the offline state.
    # Review RE: a private snapshot, so an edit during the render can neither
    # fail it nor be cached under this hash.
    edl = _preview_edl(store, sid)
    ev = _rcancel.PREVIEWS.begin(sid, edl.render_hash())
    if ticket is not None:
        ticket.joined(ev)
    try:
        with _rcancel.scope(ev, deadline_s=_rcancel.preview_deadline_s(edl.duration)), \
                _rcancel.low_priority(low_priority):
            return render_preview(edl, store.dir)
    finally:
        if ticket is not None:
            ticket.finish(ev)
        else:
            _rcancel.PREVIEWS.end(sid, ev)


async def _await_client_disconnect(request: Request, ticket: _PreviewTicket) -> None:
    """Wait for the client's `http.disconnect` and mark the ticket.

    A long-lived `receive()`, not `request.is_disconnected()`: the latter
    polls with an already-cancelled scope, and behind this app's
    BaseHTTPMiddleware stack that poll never sees the disconnect (measured on
    Starlette 1.0: a bare app reports it within 0.1 s, the same route behind
    one BaseHTTPMiddleware reports False forever). The routes that use this
    take no body, so consuming the request messages here costs nothing."""
    while True:
        msg = await request.receive()
        if msg.get("type") == "http.disconnect":
            ticket.client_gone()
            return


async def _preview_for_client(request: Request, sid: str, store, low_priority: bool = False):
    """`_render_preview_latest` in the threadpool, abandoned if the HTTP
    client disconnects before it finishes (QA-041).

    A synchronous preview used to run to completion after its tab or window
    closed — with no newer request to supersede it, that could be a full
    render of a mistyped 27-hour timeline. The render runs in a worker
    thread; a watcher task waits for the client's disconnect and marks the
    ticket, which sets the render's cancel event (unless another client
    shares that render), so `render.cancel` kills its ffmpeg and the `.part`
    file is removed."""
    from starlette.concurrency import run_in_threadpool
    ticket = _PreviewTicket(sid)
    fut = asyncio.ensure_future(
        run_in_threadpool(_render_preview_latest, sid, store, ticket, low_priority))
    watcher = asyncio.ensure_future(_await_client_disconnect(request, ticket))
    try:
        # A departed client does not end the wait: the render thread still
        # has to unwind (it raises RenderCancelled within ~50 ms).
        return await fut
    finally:
        watcher.cancel()


def _preview_superseded() -> HTTPException:
    return HTTPException(409, {"error": "preview_superseded",
                               "message": "A newer edit replaced this preview render."})


def _preview_timed_out() -> HTTPException:
    return HTTPException(504, {"error": "preview_timed_out",
                               "message": "This preview took far longer than it should "
                                          "and was stopped. Check the timeline's length "
                                          "and clip positions, then try again."})


def _preview_priority_is_low(priority: str | None) -> bool:
    """`priority` of POST /preview: absent or `normal` → False, `low` → True,
    anything else a 422 (a typo must not silently render at full priority)."""
    p = (priority or "normal").strip().lower()
    if p not in ("normal", "low"):
        raise HTTPException(422, {"error": "invalid_priority",
                                  "message": "priority must be 'normal' or 'low'."})
    return p == "low"


@app.post("/api/sessions/{sid}/preview")
async def make_preview(sid: str, request: Request, wait: int = 1,
                       priority: str | None = Query(None, max_length=16)):
    """Render a preview.

    `wait=1` (default): blocks until done. Backwards-compatible with the
    existing frontend. The render is cancelled if the client disconnects
    first (QA-041), and bounded by a wall-clock deadline (504).
    `wait=0`: returns 202 + `{job_id, status_url}` immediately. Poll
    `/api/jobs/{job_id}` for progress; the result field gets the same
    payload the sync path returns. Use this for hosted/multi-user setups
    where the request thread shouldn't block on a 30s render.
    `priority=low` (wave D, INSTANT_PREVIEW_SPEC §4.1 step 8): the client
    engine already shows the edit, so this render is background work — its
    ffmpeg processes run niced (nice 10; BELOW_NORMAL on Windows). A render
    of the same hash already in flight is shared as it is.
    """
    from starlette.concurrency import run_in_threadpool
    low = _preview_priority_is_low(priority)
    store = await run_in_threadpool(_store, sid)
    if wait:
        from .render.cancel import RenderCancelled, RenderTimedOut
        try:
            res = await _preview_for_client(request, sid, store, low)
        except RenderTimedOut:
            raise _preview_timed_out() from None
        except RenderCancelled:
            raise _preview_superseded() from None
        except RENDER_ERRORS as e:
            _require_media_tools()      # QA-108: no ffmpeg is not "corrupt frames"
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
    # The offline-aware view (QA-095), exactly what wait=1 and GET
    # preview.mp4 render: missing media is slated, not a failed job, and the
    # hash agrees with the file the <video> is served.
    edl_snapshot = await run_in_threadpool(_preview_edl, store, sid)
    session_dir_snapshot = store.dir

    def _job(cancel_event=None) -> dict:
        # Same wall-clock bound as the interactive path (QA-041), and the
        # job's own cancel event reaches every ffmpeg of the render.
        from .render import cancel as _rcancel
        try:
            with _rcancel.scope(cancel_event or threading.Event(),
                                deadline_s=_rcancel.preview_deadline_s(edl_snapshot.duration)), \
                    _rcancel.low_priority(low):
                res = render_preview(edl_snapshot, session_dir_snapshot)
        except _rcancel.RenderTimedOut:
            raise RuntimeError("This preview took far longer than it should and was stopped.") from None
        except _rcancel.RenderCancelled:
            from .api.jobs import JobCancelled
            raise JobCancelled() from None
        except RENDER_ERRORS as e:
            missing = _media_tools_http()   # QA-108: the job's error says what to install
            if missing is not None:
                raise RuntimeError(missing.detail["message"]) from e
            raise
        return _preview_payload(sid, res)

    job = JOB_MANAGER.submit(kind="preview", fn=_job, session_id=sid)
    return JSONResponse(
        status_code=202,
        content={"job_id": job.id, "status": job.status,
                 "status_url": f"/api/jobs/{job.id}"},
    )


@app.get("/api/sessions/{sid}/render-cache")
def render_cache_usage(sid: str):
    """How much disk this project's render caches hold, and its budget
    (QA-106). Previews, chunks, segments and cached videos are pure functions
    of the timeline and are trimmed LRU to the budget after every render."""
    from .render import cache_budget
    _existing_session_or_error(sid)
    store = _store(sid)
    return cache_budget.usage(store.dir)


@app.delete("/api/sessions/{sid}/render-cache")
def clear_render_cache(sid: str):
    """Delete this project's render caches ("Clear render cache", QA-106).
    The preview of the CURRENT timeline is kept so the player keeps playing;
    everything else re-renders on demand. Never touches media, exports,
    transcripts or AI outputs."""
    from .render import cache_budget
    from .render.compositor import session_render_in_flight
    _existing_session_or_error(sid)
    store = _store(sid)
    # The preview on screen is keyed on the RENDERED view: with media offline
    # that is the slated copy (_preview_edl), not store.edl.
    keep = {store.dir / "previews" / f"{store.edl.render_hash()}.mp4",
            store.dir / "previews" / f"{_preview_edl(store).render_hash()}.mp4"}
    recent = cache_budget.PROTECT_RECENT_S if session_render_in_flight(store.dir) else 0.0
    freed = cache_budget.clear(store.dir, protect=tuple(keep), protect_recent_s=recent)
    return {"freed_bytes": freed, **cache_budget.usage(store.dir)}


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
async def stream_preview(sid: str, request: Request, h: str | None = None):
    from starlette.concurrency import run_in_threadpool
    store = await run_in_threadpool(_store, sid)
    # The hash the preview of the CURRENT state is stored under — offline-
    # aware (QA-095), so a render made while the media was present is never
    # served once it has gone.
    # The RENDER key (QA-131): markers/lock/lane names never re-render.
    pedl = await run_in_threadpool(_preview_edl, store)
    current_hash = pedl.render_hash()
    if h and h in (pedl.hash(), store.edl.hash()):
        # The EDL's own hash (what /dispatch answers) names the current render.
        h = current_hash
    target_hash = h or current_hash
    p = store.dir / "previews" / f"{target_hash}.mp4"
    # Treat a 0-byte leftover (from a killed render that predates atomic writes)
    # as missing — serving it would hand the client a torn file that mp4box
    # rejects with "invalid box". Re-render instead.
    if not p.exists() or p.stat().st_size == 0:
        # A missing render of a hash that is NOT the current EDL can never be
        # produced here — rendering would render the CURRENT EDL and then 404
        # anyway. That used to cost a full render per stale <video> src (a
        # session switch, an evicted preview), competing with the render the
        # user was waiting for (QA-004). Answer the 404 up front.
        if h and h != current_hash:
            raise HTTPException(404, "preview for that hash is no longer available")
        from .render.cancel import RenderCancelled, RenderTimedOut
        # Same RuntimeError -> 422 mapping the POST /preview and /export paths
        # use. Without it this route answered a render failure with a bare 500
        # plus a full traceback (logged twice), while the very same failure via
        # POST produced a clean, actionable 422. The <video> element polls THIS
        # url, so it was the shape the UI hit most often.
        try:
            res = await _preview_for_client(request, sid, store)
        except RenderTimedOut:
            raise _preview_timed_out() from None
        except RenderCancelled:
            raise _preview_superseded() from None
        except RENDER_ERRORS as e:
            _require_media_tools()      # QA-108
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


def _proxy_export_in_progress():
    """Eager preview-proxy encodes pause while an export renders (wave D,
    INSTANT_PREVIEW_SPEC §5.1): an export is the one render the user is
    watching a progress bar for."""
    from .ingest.proxy_queue import export_in_progress
    return export_in_progress()


def _export_payload(sid: str, res, timeline_hash: str | None = None) -> dict:
    # `edl_hash` is the timeline the file was rendered from — what the UI's
    # "↓ MP4 (outdated)" check compares against GET /sessions/{sid}.edl_hash.
    # The name carries the project's name (QA-098): spaces, Devanagari, … —
    # percent-encoded in the URL, verbatim as the suggested save name.
    from urllib.parse import quote
    return {"path": str(res.path), "filename": res.path.name,
            "url": f"/api/sessions/{sid}/files/exports/{quote(res.path.name)}",
            # The TIMELINE's hash, not the render view's: _export_edl swaps a
            # clamped clip's src for its full-quality master, so res.edl_hash
            # hashed a different tree and a fresh export read "(outdated)".
            "edl_hash": timeline_hash or res.edl_hash}


@app.post("/api/sessions/{sid}/export")
def make_export(sid: str, body: ExportRequest | None = None, wait: int = 1):
    """Render an export at canvas resolution. `wait=0` returns 202 + job_id
    immediately (poll `/api/jobs/{job_id}`); default keeps the sync shape
    for backward compat. Exports can take minutes — use `wait=0` from any
    client where the request might time out (most browsers/proxies)."""
    store = _store(sid)
    body = body or ExportRequest()
    _require_media_tools()                       # QA-108: every export needs ffmpeg
    _refuse_missing_media(store, "export")
    _refuse_empty_export(store)                  # QA-123
    if wait:
        # Mirror the preview path's RuntimeError→422 handling. Without it an
        # ffmpeg failure fell through to hardening's generic handler as an
        # opaque HTTP 500 with the reason discarded into the server log.
        # Review RE: one private snapshot for the hash, the render and its
        # frame-count check (an edit during the export used to fail it).
        snap = _edl_snapshot(sid, store)
        timeline_hash = snap.hash()
        # An audio-only export (QA-100) renders no picture, so it never waits
        # for full-quality video masters (QA-089).
        audio_only = body.container in ("m4a", "wav")
        try:
            with _proxy_export_in_progress():      # wave D: eager proxies pause
                res = render_export(snap if audio_only else _export_edl(snap, body.height),
                                    store.dir, height=body.height,
                                    fps=body.fps, crf=body.crf, container=body.container,
                                    bitrate_kbps=body.bitrate_kbps,
                                    project_name=read_meta(sid).get("name"))
        except RENDER_ERRORS as e:
            msg = str(e)
            tail = msg[-400:]
            raise HTTPException(422, {
                "error": "render_failed",
                "message": _render_failure_message(tail, msg, kind="export"),
                "ffmpeg": tail,
            })
        return _export_payload(sid, res, timeline_hash)
    from .api.jobs import JOB_MANAGER
    # Review RE: `store.edl` was a REFERENCE — ripple_delete mutates it in
    # place, and the export failed at 99.8 % on its frame-count check.
    edl_snapshot = _edl_snapshot(sid, store)
    session_dir_snapshot = store.dir
    height, fps, crf, container = body.height, body.fps, body.crf, body.container
    bitrate_kbps = body.bitrate_kbps
    # QA-098: the file is named after the project (as it is called NOW).
    project_name = read_meta(sid).get("name")

    def _job(set_progress=None, cancel_event=None) -> dict:
        timeline_hash = edl_snapshot.hash()
        try:
            # QA-089: first the full-quality masters a larger-than-proxy
            # export needs (the first 30 % of the bar when there are any).
            render_progress = set_progress
            # Audio-only (QA-100): no picture, so no video masters to make.
            edl = (edl_snapshot if container in ("m4a", "wav")
                   else _export_edl(edl_snapshot, height, dry_run=True))
            if edl is not edl_snapshot:
                edl = _export_edl(edl_snapshot, height, cancel_event=cancel_event,
                                  on_progress=(lambda p: set_progress(0.3 * p)) if set_progress else None)
                if set_progress:
                    render_progress = lambda p: set_progress(0.3 + 0.7 * p)  # noqa: E731
            with _proxy_export_in_progress():      # wave D: eager proxies pause
                res = render_export(edl, session_dir_snapshot,
                                    height=height, fps=fps, crf=crf, container=container,
                                    on_progress=render_progress, cancel_event=cancel_event,
                                    bitrate_kbps=bitrate_kbps, project_name=project_name)
        except RENDER_ERRORS as e:
            # jobs.py stores `f"{type(e).__name__}: {e}"` as job.error and the
            # UI shows it verbatim — so raise something whose str() is already
            # user-facing instead of a 2000-char ffmpeg stderr dump.
            msg = str(e)
            raise RuntimeError(_render_failure_message(msg[-400:], msg, kind="export")) from e
        return _export_payload(sid, res, timeline_hash)

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

# Instant-preview routes (wave D, INSTANT_PREVIEW_SPEC §5.2): proxy index,
# spans, FLAC chunks, the preview.engine setting, the reference frame map and
# bakes. Behind the same middleware (Host allowlist, cross-site refusal);
# media reads share one rate bucket (api/hardening.rate_bucket).
from .api import preview_routes as _preview_routes
_preview_routes.configure(resolve_store=_store, preview_edl=_preview_edl)
app.include_router(_preview_routes.router)

# Speed-curve presets for the Inspector (wave D, lane S2): the one table
# `edl/speed_presets.py` that set_speed and the agent tool also read.
from .api.speed_routes import router as _speed_router
app.include_router(_speed_router)

# Clip animations (wave E, F1): the preset table `edl/clip_animations.py`
# that set_animation, the agent tool and every renderer also read.
from .api.animation_routes import router as _animation_router
app.include_router(_animation_router)

# Voice effects (wave E, F3): the preset table `edl/voice_effects.py` that
# set_voice_effect and the agent tool also read, and the Inspector's
# server-rendered audition of one clip through an effect.
from .api import voice_routes as _voice_routes
_voice_routes.configure(resolve_store=_store)
app.include_router(_voice_routes.router)

# CapCut Canvas backgrounds and overlay blend modes (wave E, F2): the table
# `edl/canvas_blend.py` that set_canvas_background / set_blend_mode, the agent
# tools and both renderers also read, and an image background's cover-fitted
# picture for the engine (the render's own function).
from .api import canvas_blend_routes as _canvas_blend_routes
_canvas_blend_routes.configure(resolve_store=_store)
app.include_router(_canvas_blend_routes.router)


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
    if not _waveform_src_allowed(target, sd, store):
        raise HTTPException(403, "src must be inside the session workdir")
    if not target.exists():
        raise HTTPException(404, "src not found")
    from .render.waveform import waveform_peaks
    return waveform_peaks(target, sd / "cache" / "waveforms",
                          peaks_per_sec=peaks_per_sec)


def _waveform_src_allowed(target: Path, sd: Path, store) -> bool:
    """/waveform's read boundary (QA-081): the session workdir, the bundled
    presets (the prompt's music beds live there — drawing one answered 403),
    or a file this session's timeline already plays (it passed the read
    allowlist when it was placed, and the renderer reads it anyway)."""
    from . import config as _cfg
    if target.is_relative_to(sd.resolve()):
        return True
    try:
        if target.is_relative_to(Path(_cfg.PRESETS_DIR).resolve()):
            return True
    except OSError:
        pass
    for t in store.edl.tracks:
        for c in t.clips:
            src = getattr(c, "src", None)
            if src:
                try:
                    if Path(src).resolve() == target:
                        return True
                except OSError:
                    continue
    return False


#: The picker row draws the poster 36 px tall; 72 keeps it sharp on Retina.
_POSTER_HEIGHT = 72


@app.get("/api/sessions/{sid}/poster")
def session_poster(sid: str, v: str = ""):
    """The project picker's poster frame (QA-099-THUMBS): the first video
    clip's frame, cached per project (storage.poster_source) and re-derived
    only when the project's edl.json changed. 204 when there is nothing to
    show (no video yet, media offline, an undecodable file) — the row then
    draws its placeholder. The source comes from the server's own EDL, never
    from the request, so there is no path input here to validate."""
    from .render.thumbs import thumbnail_for
    from .storage import _edl_stamp, poster_source, session_path
    _existing_session_or_error(sid)
    found = poster_source(sid)
    if found is None:
        return Response(status_code=204, headers={"Cache-Control": "no-cache"})
    src, t = found
    try:
        p = thumbnail_for(src, session_path(sid) / "cache" / "thumbs", t=t, height=_POSTER_HEIGHT)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return Response(status_code=204, headers={"Cache-Control": "no-cache"})
    # `v` is the EDL stamp the listing handed out: while it is current the URL
    # names exactly this image, so the browser may keep it for good.
    current = v and v == _edl_stamp(session_path(sid))
    return FileResponse(p, media_type="image/jpeg", headers={
        "Cache-Control": "private, max-age=31536000, immutable" if current else "no-cache"})


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


@app.get("/api/sessions/{sid}/thumbstrip")
def get_thumbstrip(sid: str, src: str, step: float, page: int = 0,
                   n: int = 16, h: int = 72):
    """A filmstrip SPRITE (QA-059): `n` tiles side by side, tile i showing the
    frame at `(page * n + i) * step` s; slots past the end are black. One
    request (one ffmpeg run) per `n` tiles instead of one per tile. `step`
    must be on the client's grid (0.5 s × 2^k). Same trust boundary as /thumb.
    Cached on file identity (path + mtime + size) under cache/thumbs."""
    _store(sid)  # validates sid shape before any filesystem work
    sd_root = session_dir(sid).resolve()
    target = Path(src)
    if not target.is_absolute():
        raise HTTPException(403, "src must be an absolute path")
    target = target.resolve()
    if not target.is_relative_to(sd_root):
        raise HTTPException(403, "src must be inside the session workdir")
    if not target.exists():
        raise HTTPException(404, "src not found")
    h = max(16, min(int(h), 270))
    from .render.thumbs import sprite_for
    try:
        p = sprite_for(target, sd_root / "cache" / "thumbs",
                       step=float(step), page=int(page), n=int(n), height=h)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except RuntimeError as e:
        raise HTTPException(422, str(e))
    return FileResponse(p, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=3600",
                                 "X-Sprite-Tiles": str(int(n))})


# --- M6: project save/load ---

@app.post("/api/sessions/{sid}/save_project")
def save_project_endpoint(sid: str):
    _existing_session_or_error(sid)
    sd = session_dir(sid)
    from urllib.parse import quote
    from .api.download_names import download_leaf
    from .storage_project import save_project
    # Named after the project (QA-098), like its exports — the session id is
    # an internal name the user never chose. download_leaf keeps it a bare
    # leaf; the id is the fallback for an unnamed project.
    out = sd / "exports" / download_leaf(read_meta(sid).get("name"), f"{sd.name}.vae")
    out.parent.mkdir(parents=True, exist_ok=True)
    report: dict = {}
    save_project(sid, out, report=report)
    missing = report.get("missing") or []
    body = {"path": str(out), "filename": out.name,
            "url": f"/api/sessions/{sid}/files/exports/{quote(out.name)}",
            "size": out.stat().st_size, "missing": missing}
    if missing:
        # QA-096: saved, but NOT self-contained — say so, naming the files.
        from .media_offline import saved_missing_message
        body["warning"] = saved_missing_message(missing)
    return body


_NOT_A_PROJECT = ("That file is not a Video AI Editor project. Choose the .vae "
                  "file you saved with Save (a project file is a zip that "
                  "contains manifest.json).")


def _not_a_project_message(sent_name: str) -> str:
    """The 415 text. Names the file the user actually sent when its name is
    not one we would have expected, so a pick like `<sid>.vae.txt` (macOS
    appended `.txt` to a text/plain download — see serve_session_file) or a
    stray `.mp4` explains itself instead of reading as a random refusal."""
    if sent_name.lower().endswith((".vae", ".zip")):
        return _NOT_A_PROJECT
    return f"{_NOT_A_PROJECT} The file you chose was named “{sent_name}”."


def _open_project_archive(tmp: Path, sent_name: str) -> str:
    """Content probe, then load. The caller owns `tmp` and unlinks it."""
    from .storage_project import is_project_archive, load_project
    if not is_project_archive(tmp):
        raise HTTPException(415, _not_a_project_message(sent_name))
    try:
        return load_project(tmp)
    except Exception as e:
        # A genuine project archive that still failed to import — a different
        # failure from "not a project at all", and it keeps its own status.
        raise HTTPException(422, f"failed to load project: {e}")


@app.post("/api/load_project")
async def load_project_endpoint(request: Request, file: UploadFile = File(...)):
    """Upload a .vae and open it as a new session.

    The upload is judged by its CONTENT (zip magic + a root manifest.json —
    storage_project.is_project_archive), never by its filename. The name gate
    this replaced (`endswith(".vae") or endswith(".zip")`) refused the app's
    own saved projects once macOS had renamed the download `<sid>.vae.txt`,
    and refused `P.VAE` and an extensionless pick for no reason the user could
    see. The filename is now only a hint for the temp file's name — sanitised
    to its last component, and an empty/missing one still works.
    """
    name = Path(_client_filename(file.filename) or "").name or "project.vae"
    # Unique per request: two projects opened at once under the same name
    # used to stream into the same temp file (QA-001).
    tmp = WORKDIR / f"_import_{uuid.uuid4().hex[:8]}_{_safe_filename(name, 'project.vae')}"
    # The worst of the three unguarded ingresses: this one writes to WORKDIR,
    # the app's own working volume, rather than into a session that a user can
    # delete. A chunked body with no Content-Length used to walk straight past
    # the middleware and fill it. Both guards run BEFORE any content check —
    # the probe needs the bytes on disk, and the bytes must be budgeted first.
    WORKDIR.mkdir(parents=True, exist_ok=True)
    _assert_room_for(request, WORKDIR)
    await _stream_upload_to(file, tmp)
    try:
        sid = _open_project_archive(tmp, name)
    finally:
        # Every exit — 200, 415, 422 — leaves no `_import_*` behind in WORKDIR.
        tmp.unlink(missing_ok=True)
    # QA-099: a second open of the same .vae (or one saved from a project
    # still here) gets "<name> (opened <date>)", not an identical picker row.
    now = time.localtime()
    name_reopened_copy(sid, f"{time.strftime('%b', now)} {now.tm_mday}, {time.strftime('%H:%M', now)}")
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
    # SEC-REBIND-127-PREFIX defence in depth: the EDL src may be ANY absolute
    # path (add_sticker's allowlist is a no-op in the default posture), so a
    # caller that can dispatch could otherwise read any file on the Mac through
    # this route. Outside the session's own directory, serve only bytes that
    # are actually a raster image (inside it, sticker_upload's own copies of
    # any format stay servable, as /files/uploads already allows).
    if path.resolve().is_relative_to(store.dir.resolve()):
        return FileResponse(path)
    media_type = _sticker_image_type(path)
    if media_type is None:
        raise HTTPException(404, "sticker image missing")
    return FileResponse(path, media_type=media_type)


_STICKER_MAGIC: tuple[tuple[bytes, int, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", 0, "image/png"),
    (b"\xff\xd8\xff", 0, "image/jpeg"),
    (b"GIF87a", 0, "image/gif"),
    (b"GIF89a", 0, "image/gif"),
    (b"WEBP", 8, "image/webp"),
)


def _sticker_image_type(path: Path) -> str | None:
    """The image media type of `path` from its magic bytes, or None."""
    try:
        with path.open("rb") as fh:
            head = fh.read(16)
    except OSError:
        return None
    for magic, offset, media_type in _STICKER_MAGIC:
        if head[offset:offset + len(magic)] == magic:
            if media_type == "image/webp" and not head.startswith(b"RIFF"):
                continue
            return media_type
    return None


@app.get("/api/sessions/{sid}/files/{kind}/{name:path}")
def serve_session_file(sid: str, kind: str, name: str, as_name: str | None = Query(None, alias="name")):
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
        # `?name=` (QA-100): the Export dialog's File name becomes the
        # downloaded name; api/download_names keeps it a bare leaf with the
        # file's own extension.
        from .api.download_names import download_leaf
        return FileResponse(candidate, filename=download_leaf(as_name, candidate.name),
                            media_type=_export_media_type(candidate))
    return FileResponse(candidate)


def _export_media_type(path: Path) -> str | None:
    """`.vae` is a zip, and must be SERVED as one.

    `mimetypes` does not know the extension, so FileResponse fell back to
    `text/plain; charset=utf-8` for a saved project. A text/plain attachment
    with an unknown extension is exactly the case in which WebKit/macOS saves
    the download with `.txt` appended — the user got `<sid>.vae.txt`, and the
    old filename gate on /api/load_project then refused their own project.
    The Content-Disposition filename stays the real `.vae` name. None means
    "let FileResponse guess as before" — every other export type is unchanged.
    """
    return "application/zip" if path.suffix.lower() == ".vae" else None


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
