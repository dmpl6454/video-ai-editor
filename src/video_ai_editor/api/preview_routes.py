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
    run in front of every route here unchanged; nothing here is a write, so
    the JSON-only rule for state-changing routes does not arise.

The frame_map / bake / duck_curve / LUT routes of spec §5.2 belong to their
own modules' lanes and mount here when they land.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query
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


def configure(*, resolve_store: Callable[[str], Any]) -> None:
    """main.py hands over its LRU-cached store resolver (404/400 on bad sids)."""
    global _RESOLVE_STORE
    _RESOLVE_STORE = resolve_store


def _store(sid: str):
    if _RESOLVE_STORE is None:           # pragma: no cover - wiring error
        raise HTTPException(500, "preview routes are not configured")
    return _RESOLVE_STORE(sid)


# ---- settings -------------------------------------------------------------------

@router.get("/api/settings/preview")
def get_preview_settings() -> JSONResponse:
    engine, source = preview_setting.preview_engine()
    return JSONResponse({"engine": engine, "source": source,
                         "choices": list(preview_setting.ENGINES),
                         "default": preview_setting.DEFAULT_ENGINE,
                         "eager_proxies": preview_setting.eager_proxies_enabled()},
                        headers=_NO_STORE)


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


__all__ = ["router", "configure", "media_row_fields", "ON_DEMAND_WAIT_S"]
