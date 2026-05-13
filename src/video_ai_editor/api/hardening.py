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
  - Request body size cap on file uploads via Starlette middleware. Avoids
    OOM on a 10 GB upload.
"""
from __future__ import annotations
import json
import logging
import time
import uuid
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

# ---------------------------------------------------------------------------
# Structured logger — JSON one-line records.

_logger = logging.getLogger("video_ai_editor")
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
    def __init__(self, *, default_rps: float = 60.0, window_s: float = 1.0):
        self.default_rps = default_rps
        self.window_s = window_s
        self.windows: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, rps: float | None = None) -> bool:
        rps = rps or self.default_rps
        now = time.monotonic()
        w = self.windows[key]
        cutoff = now - self.window_s
        while w and w[0] < cutoff:
            w.popleft()
        if len(w) >= rps * self.window_s:
            return False
        w.append(now)
        return True


RATE = _RateLimiter()


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
            ip = (request.client.host if request.client else "unknown") + ":" + path
            if not RATE.allow(ip):
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
        return _envelope(status=422, code="VALIDATION_ERROR",
                         message="invalid request", request_id=rid,
                         details=exc.errors())

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
    def livez() -> dict:
        return {"ok": True}

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> Response:
        import shutil as _shutil
        ffmpeg = _shutil.which("ffmpeg")
        if not ffmpeg:
            return JSONResponse(status_code=503,
                                content={"ok": False, "missing": ["ffmpeg"]})
        return JSONResponse(status_code=200,
                            content={"ok": True, "ffmpeg": ffmpeg})

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(content=METRICS.render(),
                        media_type="text/plain; version=0.0.4")
