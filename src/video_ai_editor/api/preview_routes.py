"""HTTP surface of the instant-preview engine (wave D, spec §5.2).

Routes (all GET — nothing here changes state a user can see; building a
proxy span on demand is a cache fill, like /thumb rendering a tile):

  GET /api/settings/preview                      preview.engine, read-only
  GET /api/sessions/{sid}/proxy?src=<path>       key + summary of one source's
                                                 proxy (probes; queues the
                                                 eager build when enabled)
  GET /api/sessions/{sid}/proxies                the same for every media
                                                 clip on the timeline
  GET /api/proxies/{key}/index.json              the proxy index + live span state
  GET /api/proxies/{key}/init.mp4                ftyp+moov (the shared avcC)
  GET /api/proxies/{key}/v/{n}.bin               span pack n (HTTP Range ok)
  GET /api/proxies/{key}/a/{n}.flac              FLAC chunk n (HTTP Range ok);
                                                 X-Audio-Gain = the linear gain
                                                 to multiply it back by
  PUT /api/settings/preview                      write preview.engine (JSON
                                                 only, loopback, same origin)
  GET /api/sessions/{sid}/frame_map?h=<hash>     the reference program map of
                                                 the CURRENT render hash (409
                                                 when `h` is stale), §8
  GET /api/sessions/{sid}/preview_loudness?h=    {gain_db, current,
                                                 target_lufs}: the server
                                                 preview's master gain for
                                                 the CURRENT hash's sound
  GET /api/sessions/{sid}/bake/{h}/index.json    bake of previews/<h>.mp4
      [?ranges=k0-k1,...]                        (§5.3; queues those spans)
  GET /api/sessions/{sid}/bake/{h}/init.mp4      its init (its own avcC class)
  GET /api/sessions/{sid}/bake/{h}/v/{n}.bin     bake span n = output frames
                                                 [n*S, (n+1)*S); 404 until the
                                                 preview of `h` has landed

Posture:
  * `v/` and `a/` encode on demand, blocking at most ON_DEMAND_WAIT_S; past
    that they answer 202 + `Retry-After: 0.2` and keep encoding.
  * A proxy key is served only while a LIVE session references it: the
    session route records the session in the proxy's refs.json after the
    same containment check /waveform uses, and the key routes refuse (404)
    a key no existing session references. Keys are 24 hex chars and span
    numbers integers, so no request text ever reaches a path unparsed.
  * Rate limit: GET/HEAD of the MEDIA paths (init.mp4, v/*.bin, a/*.flac)
    are exempt from the per-(IP, path) bucket every other route has and
    share one 400 rps bucket per client instead (api/hardening.rate_bucket)
    — a playing timeline fetches several spans a second and each has its
    own path. index.json and every other method keep the per-path bucket.
  * File serving is confined to WORKDIR/proxies (spec §14 risk 16): no
    route here serves a session's cache/.
  * The Host allowlist and cross-site refusal (api/auth.PairAuthMiddleware)
    run in front of every route here unchanged. The one write (PUT
    /api/settings/preview) is JSON-only, loopback-only and same-origin, like
    the other app-settings writes (api/settings_routes).
  * Bake media (init.mp4, v/*.bin) share the preview-media bucket too; a
    bake is served only from its own session's previews/ (hash validated,
    the session resolved by the same store resolver every route uses).

The duck_curve / LUT routes of spec §5.2 are Phase 2 and mount here when
they land (preview_loudness landed in Final QA r3).
"""
from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from .. import preview_setting
from ..ingest import proxy as P
from ..ingest.proxy_queue import MANAGER

router = APIRouter(tags=["preview"])

#: How long a span/chunk request may block while it is encoded (spec §5.2).
ON_DEMAND_WAIT_S = 2.0
#: How long the session route waits for a first probe of a new source.
PROBE_WAIT_S = 2.0
RETRY_AFTER_S = "0.2"

_PENDING_HEADERS = {"Retry-After": RETRY_AFTER_S, "Cache-Control": "no-store"}
_NO_STORE = {"Cache-Control": "no-store"}
#: Span packs and chunks are fixed for their key (file identity + recipe),
#: but may be evicted and rebuilt, so the browser may keep them only briefly.
_MEDIA_CACHE = {"Cache-Control": "private, max-age=3600"}

_RESOLVE_STORE: Callable[[str], Any] | None = None
#: main._preview_edl: the offline-aware EDL a preview renders (QA-095) — the
#: one `render_hash` and the preview file are keyed on.
_PREVIEW_EDL: Callable[[Any], Any] | None = None


def configure(*, resolve_store: Callable[[str], Any],
              preview_edl: Callable[[Any], Any] | None = None) -> None:
    """main.py hands over its LRU-cached store resolver (404/400 on bad sids)
    and its preview-EDL view."""
    global _RESOLVE_STORE, _PREVIEW_EDL
    _RESOLVE_STORE = resolve_store
    if preview_edl is not None:
        _PREVIEW_EDL = preview_edl


def _store(sid: str):
    if _RESOLVE_STORE is None:           # pragma: no cover - wiring error
        raise HTTPException(500, "preview routes are not configured")
    return _RESOLVE_STORE(sid)


# ---- settings -------------------------------------------------------------------

def _preview_settings_body() -> dict:
    engine, source = preview_setting.preview_engine()
    return {"engine": engine, "source": source,
            "choices": list(preview_setting.ENGINES),
            "default": preview_setting.DEFAULT_ENGINE,
            "eager_proxies": preview_setting.eager_proxies_enabled()}


@router.get("/api/settings/preview")
def get_preview_settings() -> JSONResponse:
    return JSONResponse(_preview_settings_body(), headers=_NO_STORE)


#: `{"engine": "client"}` is 20 bytes; anything past this is not a setting.
_MAX_SETTING_BODY = 1024


@router.put("/api/settings/preview")
async def put_preview_settings(request: Request) -> JSONResponse:
    """Write `preview.engine` (spec §1 G6, §12's kill switch). A state change,
    so: loopback only (a paired phone runs its own preview, N7), the app's
    own origin only, and `application/json` only — a cross-origin page can
    send text/plain or a form without a CORS preflight, never JSON. The
    answer is the same body GET returns, so the caller sees whether an
    environment override (VAI_PREVIEW_ENGINE) still wins."""
    from .auth import _is_loopback
    from .settings_routes import _same_origin
    if not _is_loopback(request):
        raise HTTPException(403, "desktop_only: the preview engine is set on the Mac itself.")
    if not _same_origin(request):
        raise HTTPException(403, "desktop_only: the preview engine is set from the app's own window.")
    ctype = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if ctype != "application/json":
        raise HTTPException(415, "Send the setting as application/json.")
    raw = await request.body()
    if len(raw) > _MAX_SETTING_BODY:
        raise HTTPException(413, "That request is far too large for one setting.")
    try:
        body = json.loads(raw.decode("utf-8") or "null")
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(400, 'Send the setting as JSON: {"engine": "auto"}.') from None
    if not isinstance(body, dict) or set(body) != {"engine"}:
        raise HTTPException(400, 'Send exactly one field: {"engine": "auto" | "client" | "server"}.')
    try:
        preview_setting.set_preview_engine(body["engine"])
    except ValueError:
        raise HTTPException(422, {"code": "invalid_engine",
                                  "message": "preview.engine must be auto, client or server.",
                                  "choices": list(preview_setting.ENGINES)}) from None
    return JSONResponse(_preview_settings_body(), headers=_NO_STORE)


# ---- session-scoped: which proxy does this source use? ---------------------------

def _src_allowed(target: Path, store) -> bool:
    """The /waveform read boundary: the session dir, the bundled presets, or
    a file this session's timeline already plays."""
    from .. import config as _cfg
    sd = Path(store.dir).resolve()
    if target.is_relative_to(sd):
        return True
    try:
        if target.is_relative_to(Path(_cfg.PRESETS_DIR).resolve()):
            return True
    except OSError:
        pass
    return target in _timeline_srcs(store)


def _timeline_srcs(store) -> set[Path]:
    out: set[Path] = set()
    for t in store.edl.tracks:
        if getattr(t, "type", None) not in ("video", "audio", "music", "vo"):
            continue
        for c in t.clips:
            src = getattr(c, "src", None)
            if not src:
                continue
            try:
                out.add(Path(src).resolve())
            except OSError:
                continue
    return out


def _summary(key: str) -> dict:
    idx = P.live_index(key) or {}
    out = {"key": key, "state": idx.get("state", "pending"),
           "index_url": f"/api/proxies/{key}/index.json"}
    for f in ("w", "h", "frames", "src_rate", "span_frames", "spans", "codec",
              "init_key", "error"):
        if f in idx:
            out[f] = idx[f]
    if "audio" in idx:
        out["silent"] = bool((idx.get("audio") or {}).get("silent"))
    return out


def _ensure(sid: str, target: Path) -> dict:
    try:
        key = MANAGER.ensure(target, sid=sid,
                             eager=preview_setting.eager_proxies_enabled(),
                             probe_timeout=PROBE_WAIT_S)
    except OSError:
        raise HTTPException(404, "source file not found") from None
    return _summary(key)


@router.get("/api/sessions/{sid}/proxy")
def session_proxy(sid: str, src: str = Query(..., min_length=1, max_length=4096)):
    store = _store(sid)
    try:
        target = Path(src).resolve()
    except (OSError, ValueError):
        raise HTTPException(400, "invalid src") from None
    if not _src_allowed(target, store):
        raise HTTPException(403, "forbidden")
    if not target.is_file():
        raise HTTPException(404, "source file not found")
    return JSONResponse(_ensure(sid, target), headers=_NO_STORE)


@router.get("/api/sessions/{sid}/proxies")
def session_proxies(sid: str):
    """Every media source on the timeline and its proxy (one probe each at
    most; eager builds queued when enabled)."""
    store = _store(sid)
    rows = []
    for target in sorted(_timeline_srcs(store), key=str):
        if not target.is_file():
            rows.append({"src": str(target), "state": "offline"})
            continue
        try:
            rows.append({"src": str(target), **_ensure(sid, target)})
        except HTTPException:
            rows.append({"src": str(target), "state": "offline"})
    return JSONResponse({"proxies": rows}, headers=_NO_STORE)


def media_row_fields(src: str | None) -> dict:
    """The /media row additions of spec §5.2, from the proxy's cached probe.
    A source nobody has asked a proxy for yet reports `proxy.state: "none"`
    and no stream facts (probing here would put ffprobe on the Media panel's
    refresh path). Never raises."""
    if not src:
        return {}
    try:
        key = P.proxy_key(src)
    except (OSError, ValueError):
        return {"proxy": {"key": None, "state": "offline", "w": None, "h": None}}
    info = P.load_source(key) if P.proxy_dir(key).is_dir() else None
    if info is None:
        failed = (P.read_index(key) or {}).get("failed")
        return {"proxy": {"key": key, "state": "failed" if failed else "none",
                          "w": None, "h": None}}
    idx = P.live_index(key) or {}
    w, h = info.proxy_size()
    return {**P.media_facts(info),
            "proxy": {"key": key, "state": idx.get("state", "pending"),
                      "w": w or None, "h": h or None}}


# ---- key-scoped proxy data ---------------------------------------------------------

def _live_key(key: str) -> None:
    """404 unless `key` is well formed AND referenced by an existing session."""
    if not P.is_valid_key(key):
        raise HTTPException(404, "not found")
    from .. import storage as _storage
    for sid in P.refs(key):
        if _storage.is_valid_session_id(sid) and _storage.session_path(sid).is_dir():
            return
    raise HTTPException(404, "not found")


def _pending(what: str) -> JSONResponse:
    return JSONResponse(status_code=202, content={"status": "pending", "what": what},
                        headers=_PENDING_HEADERS)


def _gone_if_failed(key: str) -> None:
    idx = P.read_index(key) or {}
    if idx.get("failed"):
        raise HTTPException(410, {"code": "proxy_failed",
                                  "message": idx.get("error") or "proxy build failed"})
    # Not failed for good, but its last encode could not be written: a 5xx
    # the engine counts toward its degraded tier, not an endless "pending"
    # (Final QA r2). Held DISK_FULL_HOLD_S, then a request encodes again.
    if MANAGER.disk_full(key):
        raise HTTPException(507, {"code": "disk_full",
                                  "message": "the disk is full — free some space for the preview"})


def _serve(path: Path, media_type: str) -> FileResponse:
    from ..render import cache_budget
    cache_budget.touch(path)
    return FileResponse(path, media_type=media_type, headers=_MEDIA_CACHE)


@router.get("/api/proxies/{key}/index.json")
def proxy_index(key: str):
    _live_key(key)
    idx = P.live_index(key)
    if idx is None or "frames" not in idx:
        if (P.read_index(key) or {}).get("failed"):
            _gone_if_failed(key)
        return _pending("index")
    if MANAGER.disk_full(key):
        idx = {**idx, "span_error": "disk_full"}
    return JSONResponse(idx, headers=_NO_STORE)


@router.get("/api/proxies/{key}/init.mp4")
def proxy_init(key: str):
    _live_key(key)
    p = P.init_path(key)
    if not p.is_file():
        _gone_if_failed(key)
        # The init segment comes out of the first encode of any span.
        info = P.load_source(key)
        if info is None:
            return _pending("init")
        if not info.has_video:
            raise HTTPException(404, "source has no video")
        MANAGER.request_span(key, 0, timeout=ON_DEMAND_WAIT_S)
        if not p.is_file():
            _gone_if_failed(key)
            return _pending("init")
    return _serve(p, "video/mp4")


_NUM = re.compile(r"^\d{1,6}$")


def _index_arg(n: str) -> int:
    if not _NUM.match(n or ""):
        raise HTTPException(404, "not found")
    return int(n)


@router.get("/api/proxies/{key}/v/{n}.bin")
def proxy_span(key: str, n: str):
    _live_key(key)
    i = _index_arg(n)
    info = P.load_source(key)
    if info is None:
        _gone_if_failed(key)
        return _pending("probe")
    if i >= info.spans:
        raise HTTPException(404, "no such span")
    p = MANAGER.request_span(key, i, timeout=ON_DEMAND_WAIT_S)
    if p is None:
        _gone_if_failed(key)
        return _pending("span")
    return _serve(p, "application/octet-stream")


@router.get("/api/proxies/{key}/a/{n}.flac")
def proxy_chunk(key: str, n: str):
    _live_key(key)
    i = _index_arg(n)
    info = P.load_source(key)
    if info is None:
        _gone_if_failed(key)
        return _pending("probe")
    if not info.has_audio:
        raise HTTPException(404, "source has no audio")
    p = P.chunk_path(key, i)
    if not p.is_file():
        if not MANAGER.request_audio(key, chunk=i, timeout=ON_DEMAND_WAIT_S):
            _gone_if_failed(key)
            return _pending("audio")
        if not p.is_file():
            raise HTTPException(404, "no such chunk")
    resp = _serve(p, "audio/flac")
    # The gain is published in index.json BEFORE the chunk is renamed into
    # place (proxy.build_audio), so this read always sees it.
    resp.headers["X-Audio-Gain"] = format(P.chunk_gain(key, i), "g")
    return resp


# ---- the reference program map (spec §4.1 step 8, §8, R14) -----------------------

#: How long /frame_map waits for the session lock (an edit in flight) and
#: for first probes of new sources before answering 202.
FRAME_MAP_WAIT_S = 2.0
_FRAME_MAP_CACHE_MAX = 32
_FRAME_MAP_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()
_FRAME_MAP_LOCK = threading.Lock()


def _preview_view(store):
    if _PREVIEW_EDL is None:           # pragma: no cover - wiring error
        return store.edl
    return _PREVIEW_EDL(store)


def _snapshot_view(sid: str, store):
    """A private deep copy of the preview EDL, taken under the session lock
    (dispatch mutates the live EDL in place), so the hash we compare and the
    map we build describe one state. None when the lock stays busy (a prompt
    run holds it for its whole run)."""
    from . import locks
    lock = locks.session_lock(sid)
    if not lock.acquire(timeout=FRAME_MAP_WAIT_S):
        return None
    try:
        return _preview_view(store).model_copy(deep=True)
    finally:
        lock.release()


def _render_srcs(view) -> list[str]:
    """The v1 media sources the program map selects frames from, in first-use
    order (render order)."""
    from ..render.frame_map import render_clips
    out: list[str] = []
    for c in render_clips(view):
        if c.src and c.src not in out:
            out.append(c.src)
    return out


class _SourcesPending(Exception):
    pass


class _SourcesFailed(Exception):
    def __init__(self, srcs: list[str]):
        super().__init__(", ".join(srcs))
        self.srcs = srcs


def _frame_map_sources(sid: str, srcs: list[str]) -> tuple[dict, tuple]:
    """{src: frame_map.SourceInfo} from each source's proxy (source.json: the
    stream facts and the master pts table), probing new ones (bounded).
    Returns the table and its identity (proxy keys, i.e. file identity)."""
    from ..render.frame_map import SourceInfo as FMSource
    import time as _time
    deadline = _time.monotonic() + FRAME_MAP_WAIT_S
    table: dict = {}
    ident: list[str] = []
    pending, failed = False, []
    for src in srcs:
        try:
            target = Path(src).resolve()
            key = P.proxy_key(target)
        except (OSError, ValueError):
            failed.append(src)
            continue
        info = P.load_source(key) if P.proxy_dir(key).is_dir() else None
        if info is None:
            try:
                MANAGER.ensure(target, sid=sid, eager=preview_setting.eager_proxies_enabled(),
                               probe_timeout=max(0.05, deadline - _time.monotonic()))
            except OSError:
                failed.append(src)
                continue
            info = P.load_source(key)
        else:
            P.add_ref(key, sid)
        if info is None:
            if (P.read_index(key) or {}).get("failed"):
                failed.append(src)
            else:
                pending = True
            continue
        if not info.has_video:
            failed.append(src)
            continue
        table[src] = FMSource.from_proxy(info)
        ident.append(key)
    if failed:
        raise _SourcesFailed(failed)
    if pending:
        raise _SourcesPending()
    return table, tuple(ident)


@router.get("/api/sessions/{sid}/frame_map")
def session_frame_map(sid: str, h: str = Query(..., min_length=1, max_length=64)):
    """The Python reference program map (`render/frame_map.frame_map_json`)
    of the session's CURRENT render hash — the offline-aware preview EDL, the
    same state `/dispatch?include=edl` answered with — plus the SourceInfo
    table it was built from (`sources`, {src: SourceInfo.to_json()}), so the
    client can build its own map from the same numbers and compare (R14).

    409 `stale_render_hash` when `h` is not the current hash (another edit
    landed; the client re-checks for the newer one). 202 + Retry-After while
    an edit holds the session or a source is still being probed. 422
    `source_unavailable` when a source cannot be proxied (the client keeps
    that range BAKED). Cached per (session, hash, source identities)."""
    from ..render import bake as _bake
    from ..render.frame_map import frame_map_json
    if not _bake.is_valid_hash(h):
        raise HTTPException(400, {"code": "invalid_hash", "message": "h must be a render hash"})
    store = _store(sid)
    view = _snapshot_view(sid, store)
    if view is None:
        return _pending("session busy")
    current = view.render_hash()
    if h != current:
        raise HTTPException(409, {"code": "stale_render_hash",
                                  "message": "The timeline changed; ask for the current render hash.",
                                  "render_hash": current})
    srcs = _render_srcs(view)
    try:
        table, ident = _frame_map_sources(sid, srcs)
    except _SourcesPending:
        return _pending("sources")
    except _SourcesFailed as e:
        raise HTTPException(422, {"code": "source_unavailable",
                                  "message": "A source on the timeline cannot be previewed "
                                             "by the client; that range stays server-drawn.",
                                  "srcs": e.srcs}) from None
    ck = (sid, h, ident)
    with _FRAME_MAP_LOCK:
        body = _FRAME_MAP_CACHE.get(ck)
        if body is not None:
            _FRAME_MAP_CACHE.move_to_end(ck)
    if body is None:
        body = frame_map_json(view, table)
        body["sources"] = {src: info.to_json() for src, info in table.items()}
        with _FRAME_MAP_LOCK:
            _FRAME_MAP_CACHE[ck] = body
            while len(_FRAME_MAP_CACHE) > _FRAME_MAP_CACHE_MAX:
                _FRAME_MAP_CACHE.popitem(last=False)
    # Immutable for this hash + these files, but the key the client asks by
    # (the hash) does not cover the files, so: no shared caching.
    return JSONResponse(body, headers=_NO_STORE)


# ---- preview loudness (spec §3.6, §5.2, §7) --------------------------------------------

@router.get("/api/sessions/{sid}/preview_loudness")
def session_preview_loudness(sid: str, h: str = Query(..., min_length=1, max_length=64)):
    """`{gain_db, current, target_lufs}` for the session's CURRENT render
    hash: the master gain the server preview applies to this sound
    (render/preview_loudness.preview_gain) — the Instant preview's master
    gain. `current` false: the session's last-known gain (None before any
    preview was measured), which the client plays marked APPROX until a
    preview of this sound lands. 409 `stale_render_hash` when `h` is not the
    current hash; 202 while an edit holds the session (Final QA r3: there was
    no such route, and the Instant preview played ~11 dB under the export)."""
    from ..render import bake as _bake
    from ..render import preview_loudness as _pl
    if not _bake.is_valid_hash(h):
        raise HTTPException(400, {"code": "invalid_hash", "message": "h must be a render hash"})
    store = _store(sid)
    view = _snapshot_view(sid, store)
    if view is None:
        return _pending("session busy")
    current = view.render_hash()
    if h != current:
        raise HTTPException(409, {"code": "stale_render_hash",
                                  "message": "The timeline changed; ask for the current render hash.",
                                  "render_hash": current})
    return JSONResponse(_pl.preview_gain(view, store.dir), headers=_NO_STORE)


# ---- bakes (spec §5.3, R13) ----------------------------------------------------------

def _bake_key(sid: str, h: str) -> str:
    """The bake key of `h` in session `sid`; 404 until previews/<h>.mp4 has
    landed (or for a malformed hash — no hash text reaches a path unchecked),
    410 when the preview cannot be baked."""
    from ..render import bake as _bake
    if not _bake.is_valid_hash(h):
        raise HTTPException(404, "not found")
    store = _store(sid)
    try:
        key = _bake.ensure(sid, store.dir, h)
    except (OSError, ValueError):
        raise HTTPException(404, "not found") from None
    if key is None:
        raise HTTPException(404, "no preview of that render yet")
    if P.load_source(key) is None:
        _gone_if_failed(key)
        raise HTTPException(404, "no preview of that render yet")
    return key


@router.get("/api/sessions/{sid}/bake/{h}/index.json")
def bake_index(sid: str, h: str, ranges: str | None = Query(None, max_length=8192)):
    """The bake's index (init_key, codec, w, h, frames, span_frames,
    span_ready, ...). `ranges=k0-k1,...` (half-open output frames, the
    client's BAKED ranges) queues exactly the spans they touch and lists
    them as `queued`. The first answer waits (≤ ON_DEMAND_WAIT_S) for one
    span to encode so it can name the init class (`init_key`)."""
    from ..render import bake as _bake
    try:
        wanted = _bake.parse_ranges(ranges)
    except ValueError as e:
        raise HTTPException(400, {"code": "invalid_ranges", "message": str(e)}) from None
    key = _bake_key(sid, h)
    queued = _bake.queue_ranges(key, wanted) if wanted else []
    if not P.init_path(key).is_file():
        # The client plans its init switches by `init_key` (the avcC class,
        # §3.2), which exists once any span of the bake is encoded: wait for
        # the first one it asked for (bounded, like init.mp4 does).
        MANAGER.request_span(key, queued[0] if queued else 0, timeout=ON_DEMAND_WAIT_S)
    idx = _bake.index(key, h)
    if idx is None:
        return _pending("index")
    idx["queued"] = queued
    return JSONResponse(idx, headers=_NO_STORE)


@router.get("/api/sessions/{sid}/bake/{h}/init.mp4")
def bake_init(sid: str, h: str):
    key = _bake_key(sid, h)
    p = P.init_path(key)
    if not p.is_file():
        # The init comes out of the first encode of any span of the bake.
        MANAGER.request_span(key, 0, timeout=ON_DEMAND_WAIT_S)
        if not p.is_file():
            _gone_if_failed(key)
            return _pending("init")
    return _serve(p, "video/mp4")


@router.get("/api/sessions/{sid}/bake/{h}/v/{n}.bin")
def bake_span(sid: str, h: str, n: str):
    key = _bake_key(sid, h)
    i = _index_arg(n)
    info = P.load_source(key)
    if info is None or i >= info.spans:
        raise HTTPException(404, "no such span")
    p = MANAGER.request_span(key, i, timeout=ON_DEMAND_WAIT_S)
    if p is None:
        _gone_if_failed(key)
        return _pending("span")
    return _serve(p, "application/octet-stream")


__all__ = ["router", "configure", "media_row_fields", "ON_DEMAND_WAIT_S", "FRAME_MAP_WAIT_S"]
