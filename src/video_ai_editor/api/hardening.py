"""Production hardening for the FastAPI app.

What this adds:
  - Request IDs: every request gets an `X-Request-ID` (echoed back in the
    response). If the caller already sent one, we honour it. Logs include
    the ID so a single request is greppable end-to-end.
  - Structured JSON logging: one line per request with method, path, status,
    duration_ms, request_id. Plays nicely with `jq` and Datadog/Loki.
  - Consistent error envelope: every 4xx/5xx body is
        {"error": {"code", "message", "request_id", "details"?}}
    so frontends and SDK consumers parse one shape, not seven.
  - /readyz and /livez probes — distinct semantics:
        * /livez — process is alive (always 200 unless we're shutting down).
        * /readyz — process AND its dependencies (ffmpeg) are usable.
  - /metrics — Prometheus-style counters + histograms. No external dep:
    we hand-roll the text format since the only consumer is Prometheus
    and its protocol is stable + tiny.
  - In-process rate limit: sliding-window per-IP, configurable. Default is
    permissive (60 req/s) so dev doesn't notice; production sets RATE_LIMIT.

NOT here: the upload size cap. This module's docstring claimed one for three
releases and no such middleware was ever installed — the frontend's 413 branch
was dead code the whole time. It now lives in api/uploads.py, which does the
Content-Length pre-check, the mid-stream abort and the free-space precondition
together, because only the last two need to know where the file is being
written.
"""
from __future__ import annotations
import json
import logging
import re
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

# ---------------------------------------------------------------------------
# Structured logger — JSON one-line records.

_logger = logging.getLogger("video_ai_editor")


def _log_dir():
    """Where app.log rotates (review RD3). `VAI_LOG_DIR` names it; a pytest
    process (the handler is installed at IMPORT, before tests/conftest.py
    can redirect anything) logs under the temp dir, and a backend started
    with an absolute WORKDIR (a scratch or QA server) logs beside it — only
    the app itself writes the owner's ~/Library/Application Support log, so
    QA traffic never rotates the owner's diagnostics away."""
    import os
    import sys
    import tempfile
    from pathlib import Path
    env = os.environ.get("VAI_LOG_DIR")
    if env:
        return Path(env)
    if "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST"):
        return Path(tempfile.gettempdir()) / "vae-pytest-logs"
    wd = os.environ.get("WORKDIR")
    if wd and os.path.isabs(wd):
        return Path(wd) / "logs"
    from .. import platformutil as _pu
    return _pu.user_data_dir("Video AI Editor") / "logs"


if not _logger.handlers:
    handler = logging.StreamHandler()
    class _JSONFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
            payload: dict[str, Any] = {
                "ts": round(record.created, 3),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            }
            for k in ("request_id", "method", "path", "status",
                      "duration_ms", "session_id", "tool"):
                v = getattr(record, k, None)
                if v is not None:
                    payload[k] = v
            if record.exc_info:
                payload["exc"] = self.formatException(record.exc_info)
            return json.dumps(payload, default=str)
    handler.setFormatter(_JSONFormatter())
    _logger.addHandler(handler)

    # ALSO log to a rotating file. In both windowed builds — the macOS .app and
    # the Windows pythonw/console=False exe — stdout and stderr are None or
    # discarded, so every diagnostic this app produced was simply lost: a hang or
    # a startup failure left literally nothing to look at. A file handler is the
    # prerequisite for hiding the console at all (see run.ps1), and it is what
    # makes a user-reported "it froze" actionable.
    try:
        from logging.handlers import RotatingFileHandler
        log_dir = _log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_dir / "app.log", maxBytes=2_000_000,
                                 backupCount=3, encoding="utf-8")
        fh.setFormatter(_JSONFormatter())
        _logger.addHandler(fh)
    except Exception:  # pragma: no cover - never let logging setup break boot
        # A read-only or missing data dir must not stop the app from starting;
        # the stream handler above still works wherever a console exists.
        pass

    _logger.setLevel(logging.INFO)
    _logger.propagate = False


def get_logger() -> logging.Logger:
    return _logger


# ---------------------------------------------------------------------------
# Metrics — Prometheus text format, no client dep.

class _Metrics:
    def __init__(self) -> None:
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._hist_buckets = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
        self._hist: dict[tuple[str, tuple[tuple[str, str], ...]],
                         tuple[list[int], float, int]] = {}

    def counter(self, name: str, labels: dict[str, str] | None = None,
                value: float = 1.0) -> None:
        key = (name, tuple(sorted((labels or {}).items())))
        self._counters[key] += value

    def observe(self, name: str, value: float,
                labels: dict[str, str] | None = None) -> None:
        key = (name, tuple(sorted((labels or {}).items())))
        if key not in self._hist:
            self._hist[key] = ([0] * (len(self._hist_buckets) + 1), 0.0, 0)
        buckets, total, count = self._hist[key]
        for i, b in enumerate(self._hist_buckets):
            if value <= b:
                buckets[i] += 1
        buckets[-1] += 1  # +Inf bucket
        self._hist[key] = (buckets, total + value, count + 1)

    def render(self) -> str:
        lines: list[str] = []
        for (name, labels), v in sorted(self._counters.items()):
            lines.append(_format_metric(name, labels, v))
        for (name, labels), (buckets, total, count) in sorted(self._hist.items()):
            le_labels_seen: list[tuple[tuple[str, str], ...]] = []
            for i, b in enumerate(self._hist_buckets):
                lab = labels + (("le", str(b)),)
                lines.append(_format_metric(name + "_bucket", lab, buckets[i]))
                le_labels_seen.append(lab)
            lab = labels + (("le", "+Inf"),)
            lines.append(_format_metric(name + "_bucket", lab, buckets[-1]))
            lines.append(_format_metric(name + "_sum", labels, total))
            lines.append(_format_metric(name + "_count", labels, count))
        return "\n".join(lines) + "\n"


def _format_metric(name: str, labels: tuple[tuple[str, str], ...], value: float) -> str:
    if labels:
        lab = ",".join(f'{k}="{v}"' for k, v in labels)
        return f"{name}{{{lab}}} {value}"
    return f"{name} {value}"


METRICS = _Metrics()


# ---------------------------------------------------------------------------
# Rate limiter — sliding window per (ip, scope).

class _RateLimiter:
    """Sliding-window limiter with a BOUNDED key map.

    `windows` is keyed on (ip, path) and the path contains session ids, clip ids
    and cache hashes — unbounded cardinality. As a plain defaultdict it grew for
    the entire process lifetime, one empty deque per distinct URL ever seen.

    Deliberately NOT "fixed" by collapsing the key to the bare IP: MAX_THUMB_TILES
    means a single timeline paint can issue hundreds of /thumb requests, and one
    60-rps bucket for the whole client would 429 them and blank the filmstrip.
    Bounding the map keeps the per-path semantics and removes the leak.
    """

    #: Ample for any real client (one entry per distinct URL inside a 1s window);
    #: small enough that the map can never be a memory problem.
    MAX_KEYS = 4096

    def __init__(self, *, default_rps: float = 60.0, window_s: float = 1.0):
        self.default_rps = default_rps
        self.window_s = window_s
        self.windows: OrderedDict[str, deque[float]] = OrderedDict()

    def _sweep(self, now: float) -> None:
        """Drop keys whose window has fully expired, then LRU-trim the remainder."""
        cutoff = now - self.window_s
        stale = [k for k, w in self.windows.items() if not w or w[-1] < cutoff]
        for k in stale:
            del self.windows[k]
        while len(self.windows) > self.MAX_KEYS:
            self.windows.popitem(last=False)

    def allow(self, key: str, rps: float | None = None) -> bool:
        rps = rps or self.default_rps
        now = time.monotonic()
        w = self.windows.get(key)
        if w is None:
            # Sweep on insertion only — an O(n) scan per request would itself be
            # a cost, and insertions are the only thing that grows the map.
            if len(self.windows) >= self.MAX_KEYS:
                self._sweep(now)
            w = self.windows[key] = deque()
        else:
            self.windows.move_to_end(key)
        cutoff = now - self.window_s
        while w and w[0] < cutoff:
            w.popleft()
        if len(w) >= rps * self.window_s:
            return False
        w.append(now)
        return True


RATE = _RateLimiter()

#: Per-second allowance for the filmstrip routes, keyed like every other route
#: on (client, path). The limiter's key drops the query string, so EVERY tile
#: of a session's /thumb (and every /waveform slice) shares one bucket — the
#: per-path design above never protected the filmstrip the way its docstring
#: says: a 17-clip timeline at 80 px/s paints ~170 tiles in well under a
#: second, the 61st and every later one came back 429 and the strip showed
#: holes (measured 2026-09-11 on a real edit, 94 rejected tiles per paint).
#: These routes are cheap cached file reads after the first render, so the
#: allowance is ten times the default; everything else keeps DEFAULT via
#: RATE.default_rps (tests monkeypatch that, so it is not duplicated here).
FILMSTRIP_RPS = 600.0
_FILMSTRIP_SUFFIXES = ("/thumb", "/waveform")


def rps_for_path(path: str) -> float | None:
    """The allowance a route gets, or None for the limiter's default."""
    if path.endswith(_FILMSTRIP_SUFFIXES):
        return FILMSTRIP_RPS
    return None


#: Wave D preview media (api/preview_routes.py, spec §5.2): proxy init
#: segments, span packs and FLAC chunks. A playing timeline fetches several of
#: them a second, each on its OWN path, so the per-(IP, path) bucket above is
#: the wrong shape twice over — it never limits them and it grows one key per
#: span. READS of them (GET/HEAD) are exempt from it and share ONE bucket per
#: client instead, capped at PREVIEW_MEDIA_RPS across all of them. index.json,
#: any other path and any other method keep the per-path bucket.
PREVIEW_MEDIA_RPS = 400.0
PREVIEW_MEDIA_BUCKET = "preview-media"
#: Bake media (spec §5.3: one session's bake of one render hash, init + span
#: packs) are the same kind of request and share the same bucket.
_PREVIEW_MEDIA = re.compile(
    r"^/api/proxies/[0-9a-f]{24}/(init\.mp4|v/\d{1,6}\.bin|a/\d{1,6}\.flac)$"
    r"|^/api/sessions/s_[A-Za-z0-9]{6,64}/bake/[0-9a-f]{16}/(init\.mp4|v/\d{1,6}\.bin)$")
_PREVIEW_MEDIA_METHODS = frozenset({"GET", "HEAD"})


def rate_bucket(path: str, method: str = "GET") -> tuple[str, float | None]:
    """(bucket name, allowance) of a request for the rate limiter."""
    if method.upper() in _PREVIEW_MEDIA_METHODS and _PREVIEW_MEDIA.match(path):
        return PREVIEW_MEDIA_BUCKET, PREVIEW_MEDIA_RPS
    return path, rps_for_path(path)


# ---------------------------------------------------------------------------
# Middleware

class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request,
                       call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        request.state.request_id = rid
        start = time.perf_counter()

        # Rate limit (skip /metrics, /healthz, /livez, /readyz)
        path = request.url.path
        if not path.startswith(("/metrics", "/healthz", "/livez", "/readyz")):
            bucket, rps = rate_bucket(path, request.method)
            ip = (request.client.host if request.client else "unknown") + ":" + bucket
            if not RATE.allow(ip, rps):
                METRICS.counter("vai_http_rate_limited_total", {"path": path})
                _logger.warning("rate-limited", extra={
                    "request_id": rid, "method": request.method, "path": path,
                })
                return JSONResponse(
                    status_code=429,
                    content={"error": {"code": "RATE_LIMITED",
                                       "message": "Too many requests",
                                       "request_id": rid}},
                    headers={"X-Request-ID": rid, "Retry-After": "1"},
                )

        try:
            resp = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            METRICS.observe("vai_http_request_duration_seconds",
                            duration_ms / 1000.0,
                            {"path": path, "method": request.method})
            METRICS.counter("vai_http_requests_total",
                            {"path": path, "method": request.method, "status": "500"})
            _logger.exception("unhandled exception", extra={
                "request_id": rid, "method": request.method, "path": path,
                "status": 500, "duration_ms": duration_ms,
            })
            raise

        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        resp.headers["X-Request-ID"] = rid
        METRICS.observe("vai_http_request_duration_seconds",
                        duration_ms / 1000.0,
                        {"path": path, "method": request.method})
        METRICS.counter("vai_http_requests_total",
                        {"path": path, "method": request.method,
                         "status": str(resp.status_code)})
        _logger.info("request", extra={
            "request_id": rid, "method": request.method, "path": path,
            "status": resp.status_code, "duration_ms": duration_ms,
        })
        return resp


# ---------------------------------------------------------------------------
# Error envelope

def _envelope(*, status: int, code: str, message: str, request_id: str,
              details: Any = None) -> JSONResponse:
    body: dict[str, Any] = {"error": {"code": code, "message": message,
                                       "request_id": request_id}}
    if details is not None:
        body["error"]["details"] = details
    return JSONResponse(status_code=status, content=body,
                        headers={"X-Request-ID": request_id})


def _safe_input(value: Any) -> Any:
    """The offending input, as something `json.dumps` always accepts (QA-107).

    FastAPI puts the raw request body into a validation error's `input`: a
    JSON body sent as `text/plain` arrives as `bytes`, and `ctx` can hold the
    exception object itself. Handing those to JSONResponse raised inside the
    error handler, so a malformed request came back as a 500. Scalars pass
    through, bytes are decoded, anything else is summarised, and every string
    is capped so a 50 MB junk body is not echoed back."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value[:200]).decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value[:200]
    if isinstance(value, (list, tuple)):
        return [_safe_input(v) for v in list(value)[:20]]
    if isinstance(value, dict):
        return {str(k)[:60]: _safe_input(v) for k, v in list(value.items())[:20]}
    return repr(value)[:200]


def _safe_validation_errors(errors: Any) -> list[dict]:
    out: list[dict] = []
    for err in list(errors or [])[:20]:
        if not isinstance(err, dict):
            out.append({"msg": str(err)[:200]})
            continue
        item: dict[str, Any] = {
            "type": str(err.get("type", "")),
            "loc": [p if isinstance(p, (int, str)) else str(p) for p in err.get("loc", ())],
            "msg": str(err.get("msg", ""))[:300],
        }
        if "input" in err:
            item["input"] = _safe_input(err["input"])
        if isinstance(err.get("ctx"), dict):
            item["ctx"] = {str(k): _safe_input(v) for k, v in err["ctx"].items()}
        out.append(item)
    return out


def install(app: FastAPI) -> None:
    """Wire all hardening middleware + exception handlers + ops endpoints
    into an existing FastAPI app. Idempotent — safe to call multiple times."""
    # Middleware once
    if not getattr(app.state, "_hardening_installed", False):
        app.add_middleware(RequestContextMiddleware)
        app.state._hardening_installed = True

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException) -> Response:
        rid = getattr(request.state, "request_id", uuid.uuid4().hex[:12])
        # Map a handful of well-known status codes to stable codes.
        code_map = {404: "NOT_FOUND", 400: "BAD_REQUEST", 401: "UNAUTHORIZED",
                    403: "FORBIDDEN", 409: "CONFLICT", 413: "TOO_LARGE",
                    422: "UNPROCESSABLE", 429: "RATE_LIMITED", 500: "INTERNAL"}
        code = code_map.get(exc.status_code, f"HTTP_{exc.status_code}")
        msg = exc.detail if isinstance(exc.detail, str) else "request failed"
        details = exc.detail if not isinstance(exc.detail, str) else None
        return _envelope(status=exc.status_code, code=code, message=msg,
                         request_id=rid, details=details)

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(request: Request, exc: RequestValidationError) -> Response:
        rid = getattr(request.state, "request_id", uuid.uuid4().hex[:12])
        errors = _safe_validation_errors(exc.errors())
        first = errors[0] if errors else {}
        where = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        msg = f"invalid request: {where + ' — ' if where else ''}{first.get('msg', '')}".rstrip(" —:")
        return _envelope(status=422, code="VALIDATION_ERROR",
                         message=msg or "invalid request", request_id=rid,
                         details=errors)

    @app.exception_handler(ValueError)
    async def _value_exc(request: Request, exc: ValueError) -> Response:
        rid = getattr(request.state, "request_id", uuid.uuid4().hex[:12])
        return _envelope(status=400, code="BAD_REQUEST", message=str(exc),
                         request_id=rid)

    @app.exception_handler(Exception)
    async def _generic_exc(request: Request, exc: Exception) -> Response:
        rid = getattr(request.state, "request_id", uuid.uuid4().hex[:12])
        _logger.exception("unhandled", extra={"request_id": rid})
        return _envelope(status=500, code="INTERNAL",
                         message="internal server error", request_id=rid)

    @app.get("/livez", include_in_schema=False)
    async def livez() -> dict:
        # `async def`, deliberately. A `def` endpoint runs on Starlette's
        # bounded anyio worker threadpool — the same one every synchronous
        # route uses — so a handful of in-flight renders or model loads could
        # queue the liveness probe behind them and a healthy process would
        # report dead (the exact shape of the round-5 VAI-11 report). This
        # handler does no blocking work, so running it directly on the event
        # loop is both correct and immune to threadpool saturation.
        return {"ok": True}

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> Response:
        import shutil as _shutil
        from .. import platformutil as _pu
        ffmpeg = _shutil.which(_pu.FFMPEG)
        if not ffmpeg:
            return JSONResponse(status_code=503,
                                content={"ok": False, "missing": ["ffmpeg"]})
        return JSONResponse(status_code=200,
                            content={"ok": True, "ffmpeg": ffmpeg})

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(content=METRICS.render(),
                        media_type="text/plain; version=0.0.4")
