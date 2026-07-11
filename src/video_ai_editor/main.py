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
  POST /api/sessions/{sid}/chat               → SSE-stream a Claude chat turn
  GET  /api/sessions/{sid}/history            → chat history
"""
from __future__ import annotations
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from fastapi import FastAPI, UploadFile, File, HTTPException, Form, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import platformutil as _pu
from .config import WORKDIR, DEFAULT_CANVAS
from .storage import (new_session_id, session_dir, session_exists,
                       list_sessions, write_meta, read_meta, delete_session)
from .edl import EDLStore
from .edl.schema import Canvas
from .ingest import ingest_upload
from .render import render_preview, render_export
from .agent.dispatch import dispatch, list_tools
from .agent.loop import chat_turn

@asynccontextmanager
async def _lifespan(_app: "FastAPI"):
    _validate_ai_config()
    yield


app = FastAPI(title="video-ai-editor", lifespan=_lifespan)

# Production hardening: request IDs, structured JSON logging, error envelope,
# /livez + /readyz + /metrics, sliding-window rate limit. Idempotent.
from .api.hardening import install as _install_hardening, METRICS, get_logger
_install_hardening(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)


def _validate_ai_config() -> None:
    """Warn loudly (but don't crash) if the AI chat backend isn't usable.

    The editor works fine without Claude — only the chat pane needs it — so a
    missing key is a warning, not a fatal. Surfacing it at boot saves users a
    confusing first chat that fails with a billing/auth error mid-conversation.
    """
    from .config import ANTHROPIC_API_KEY
    log = get_logger()
    if not ANTHROPIC_API_KEY:
        log.warning(
            "ANTHROPIC_API_KEY is not set — the 'Tell Claude what to do' chat "
            "pane will be disabled. Add it to .env to enable AI features."
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


# --- routes ---

@app.get("/api/health")
def health():
    from .config import APP_VERSION
    return {"ok": True, "version": APP_VERSION}


@app.get("/api/version")
def version():
    from .config import APP_VERSION
    return {"version": APP_VERSION}


@app.get("/api/tools")
def tools():
    return {"tools": list_tools()}


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
    with _STORES_LOCK:
        _STORES.pop(sid, None)  # drop the cached store so it can't resurrect the dir
    existed = delete_session(sid)
    if not existed:
        raise HTTPException(404, {"code": "not_found", "message": "session not found"})
    return {"deleted": sid}


@app.post("/api/sessions/{sid}/vo_record")
async def vo_record(sid: str, file: UploadFile = File(...),
                    start: float = Form(0.0), gain_db: float = Form(0.0)):
    """Receive a recorded mic blob (WebM/Opus or WAV) and add it to the vo track.

    Browser MediaRecorder typically produces audio/webm;codecs=opus. We trans-
    code to a session-local AAC mp4 for clean playback in the timeline pipeline.
    """
    sd = session_dir(sid)
    vo_dir = sd / "uploads" / "vo"
    vo_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_filename(file.filename, "vo.webm")
    raw = vo_dir / f"raw_{safe_name}"
    with raw.open("wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)

    # Normalize to AAC mp4 so the audio mixer can splice it cleanly
    norm = vo_dir / f"vo_{int(time.time())}.m4a"
    proc = subprocess.run(
        [_pu.FFMPEG, "-y", "-i", str(raw),
         "-vn", "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000",
         str(norm)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
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


@app.post("/api/sessions/{sid}/sticker_upload")
async def sticker_upload(sid: str, file: UploadFile = File(...),
                         add_at_playhead: bool = Form(False),
                         playhead: float = Form(0.0)):
    """Upload a PNG (or other image) and optionally drop it as a sticker."""
    sd = session_dir(sid)
    sticker_dir = sd / "uploads" / "stickers"
    sticker_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_filename(file.filename, "sticker.png")
    dst = sticker_dir / safe_name
    with dst.open("wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    info = {"src": str(dst), "filename": safe_name}
    if add_at_playhead:
        store = _store(sid)
        canvas = store.edl.canvas
        dispatch(store, "add_sticker", {
            "src": str(dst),
            "start": float(playhead),
            "end": float(playhead) + 3.0,
            "position": [canvas.w / 2, canvas.h * 0.55],
            "scale": 1.0,
        })
        info["edl_hash"] = store.edl.hash()
    return info


@app.post("/api/sessions/{sid}/audio_upload")
async def audio_upload(sid: str, file: UploadFile = File(...),
                       add_to_music: bool = Form(True),
                       duck: bool = Form(True),
                       volume_db: float = Form(-12.0)):
    """Upload an audio file (mp3/wav/m4a) and optionally append to the music track."""
    store = _store(sid)
    sd = session_dir(sid)
    audio_dir = sd / "uploads" / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_filename(file.filename, "audio.mp3")
    dst = audio_dir / safe_name
    with dst.open("wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    # Probe to get duration
    from .ingest.probe import probe as _probe
    try:
        p = _probe(dst)
    except Exception as e:
        raise HTTPException(422, {"file": safe_name, "error": str(e)})

    if add_to_music:
        store.edl.recompute_duration()
        # Place at start of timeline; loop or trim to project duration if needed
        start = 0.0
        out = min(p.duration, max(store.edl.duration, p.duration))
        dispatch(store, "add_music", {
            "src": str(dst), "start": start, "in": 0.0, "out": out,
            "duck": duck, "volume_db": volume_db,
        })

    return {"src": str(dst), "duration": p.duration, "edl_hash": store.edl.hash()}


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
async def upload(sid: str, background_tasks: BackgroundTasks,
                 file: UploadFile = File(...),
                 add_to_timeline: bool = Form(True),
                 transcribe: bool = Form(True),
                 whisper_model: str = Form("")):
    store = _store(sid)
    sd = session_dir(sid)
    uploads = sd / "uploads"
    uploads.mkdir(exist_ok=True)
    safe_name = _safe_filename(file.filename, "upload.mp4")
    dst = uploads / safe_name
    with dst.open("wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)

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

    if add_to_timeline:
        v1 = store.edl.get_track("v1")
        was_empty = not any(True for _ in (v1.clips if v1 else []))
        if was_empty:
            _match_canvas_to_source(store, res.probe)
        store.edl.recompute_duration()
        start = store.edl.duration
        dispatch(store, "add_clip", {
            "track": "v1",
            "src": str(res.normalized),
            "in": 0.0,
            "out": res.probe.duration,
            "start": start,
        })
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


@app.post("/api/sessions/{sid}/dispatch")
def dispatch_tool(sid: str, body: DispatchRequest):
    store = _store(sid)
    try:
        result = dispatch(store, body.tool, body.args)
    except KeyError as e:
        raise HTTPException(400, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        # External-tool / setup errors bubble up here (pyannote token missing,
        # ffmpeg failure, model not found, etc.) — give the user the message.
        raise HTTPException(422, str(e))
    last_op = store.ops.last()
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


def _render_failure_message(ffmpeg_tail: str) -> str:
    """Pick a user-facing message for a render_failed 422.

    ffmpeg's stderr names the file it choked on. If that file lives under our
    own overlay-PNG cache (st_/text_/sa_/mask_ prefixes), the corrupt input is
    an app-generated rasterized overlay, not the user's uploaded media — say
    so instead of implying their video/audio file is bad.
    """
    # st_/text_/sa_ cache files are named "<prefix><16-hex-char content hash>.png"
    # (text_overlay.py); mask_ files are "mask_<clip_id>_<type>_<feather>_<w>x<h>.png"
    # (compositor.py/chunks.py) — a much less regular shape, so it gets its own
    # alternative rather than trying to force one pattern to fit both.
    if re.search(r"[\\/](?:st_|text_|sa_)[0-9a-f]+\.png", ffmpeg_tail) or \
       re.search(r"[\\/]mask_[^\\/]+\.png", ffmpeg_tail):
        return ("Couldn't render a preview — a cached text/sticker overlay "
                 "image was corrupted. Retrying will regenerate it; your "
                 "media is fine.")
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
            raise HTTPException(422, {"error": "render_failed",
                                      "message": _render_failure_message(tail),
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
        res = render_preview(store.edl, store.dir)
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
        res = render_export(store.edl, store.dir, height=body.height,
                            fps=body.fps, crf=body.crf)
        return _export_payload(sid, res)
    from .api.jobs import JOB_MANAGER
    edl_snapshot = store.edl
    session_dir_snapshot = store.dir
    height, fps, crf = body.height, body.fps, body.crf

    def _job(set_progress=None, cancel_event=None) -> dict:
        res = render_export(edl_snapshot, session_dir_snapshot,
                            height=height, fps=fps, crf=crf,
                            on_progress=set_progress, cancel_event=cancel_event)
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
    _history_path(sid).write_text(json.dumps(history, indent=2, default=str), encoding="utf-8")


@app.get("/api/sessions/{sid}/history")
def get_history(sid: str):
    return {"history": _load_history(sid)}


@app.post("/api/sessions/{sid}/chat")
async def chat(sid: str, body: ChatRequest):
    """SSE-stream a Claude chat turn (text deltas + tool calls + ops)."""
    store = _store(sid)
    history = _load_history(sid)

    async def gen():
        try:
            async for evt in chat_turn(store, body.message, history):
                yield f"data: {json.dumps(evt)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type':'error','message':str(e)})}\n\n"
        finally:
            _save_history(sid, history)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/sessions/{sid}/waveform")
def get_waveform(sid: str, src: str, peaks_per_sec: int = 50):
    """Return downsampled audio peaks for a source path used in this session.

    `src` must lie within the session workdir (defends against path traversal).
    """
    store = _store(sid)
    sd = session_dir(sid)
    target = Path(src).resolve()
    sd_resolved = sd.resolve()
    if not str(target).startswith(str(sd_resolved)):
        raise HTTPException(403, "src must be inside the session workdir")
    if not target.exists():
        raise HTTPException(404, "src not found")
    from .render.waveform import waveform_peaks
    return waveform_peaks(target, sd / "cache" / "waveforms",
                          peaks_per_sec=peaks_per_sec)


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
async def load_project_endpoint(file: UploadFile = File(...)):
    """Upload a .vae and open it as a new session."""
    name = Path(file.filename or "project.vae").name
    if not name.endswith(".vae") and not name.endswith(".zip"):
        raise HTTPException(415, "expected a .vae project file")
    tmp = WORKDIR / f"_import_{name}"
    with tmp.open("wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    try:
        from .storage_project import load_project
        sid = load_project(tmp)
    except Exception as e:
        raise HTTPException(422, f"failed to load project: {e}")
    finally:
        tmp.unlink(missing_ok=True)
    return {"id": sid}


@app.get("/api/sessions/{sid}/files/{kind}/{name}")
def serve_session_file(sid: str, kind: str, name: str):
    if kind not in {"uploads", "previews", "exports"}:
        raise HTTPException(404, "not found")
    sd = session_dir(sid)
    # `name` may include subdirs (e.g. "Outfit.../Outfit....normalized.mp4")
    candidate = (sd / kind / name).resolve()
    # Prevent path traversal
    if not str(candidate).startswith(str(sd.resolve())):
        raise HTTPException(403, "forbidden")
    if not candidate.exists():
        # Try one level deeper for ingest output (uploaded clips live under uploads/<stem>/)
        for sub in (sd / kind).rglob(name):
            candidate = sub
            break
    if not candidate.exists():
        raise HTTPException(404, "file not found")
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


_FRONTEND_DIST = _find_frontend_dist()
if _FRONTEND_DIST is not None:
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="frontend")
