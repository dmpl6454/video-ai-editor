"""Upload size and disk-space limits.

There was no upload cap before 0.6.0. Not a small one, not a permissive one —
none. A paired phone (or, in LAN mode, any peer on the network) could POST an
arbitrarily large body to `/upload`, `/audio_upload`, `/vo_record`,
`/sticker_upload`, `/subtitle_upload` or `/load_project` and the server would
stream every byte of it to disk until the volume filled. The Import screen's
413 branch existed and was dead code.

Three defences, in the order they fire:

  1. `Content-Length` pre-check (middleware). The cheapest one, and the only
     one that can refuse a 4 GB body BEFORE any of it is on the wire. A client
     that is honest about its size — every real one is — gets an instant 413.
  2. Free-space precondition (route). Refusing a 3 GB upload onto a volume
     with 800 MB free is better than accepting it, spending five minutes, and
     failing at 97%.
  3. Mid-stream abort (route). Content-Length is a claim, not a fact, and a
     chunked body has none at all. The streaming helper counts what it has
     actually written, stops at the limit, and DELETES the partial file — a
     rejected upload that leaves 4 GB of garbage in the session directory has
     not really been rejected.

The limit is echoed in `/api/health` and in `/api/pair/whoami` so the phone's
Import screen can refuse a too-large pick locally, before spending the user's
time and battery pushing bytes at a server that will say no.

WHO THE FIXED CAP IS FOR (QA-114). The 4 GiB cap exists for bodies arriving
from ANOTHER device — a paired phone or a LAN peer. It used to apply to every
import, so a desktop user dragging in a 6 GB ProRes take from their own disk
was refused, and told to "raise VAI_MAX_UPLOAD_BYTES and restart" — an
instruction for a developer, not for someone using a Mac app. A request from
this machine's own editor (loopback, and not a hosted deployment that sets
VAI_RESTRICT_PATHS) is now bounded by FREE DISK SPACE instead: the same
`FREE_SPACE_HEADROOM` rule the precondition uses, enforced up front from the
declared size and again mid-stream. `VAI_MAX_UPLOAD_BYTES`, when an operator
sets it, still applies to everyone; it is documented for developers here and
in CLAUDE.md, and never named in a message a user reads.
"""
from __future__ import annotations

import os
import shutil
from contextvars import ContextVar
from pathlib import Path

from fastapi import HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

#: 4 GiB. Comfortably above any phone-shot clip (an hour of 4K HEVC from an
#: iPhone is ~25 GB, but that is not something you hand to a companion app over
#: Wi-Fi), and far below "fills the disk". Override with VAI_MAX_UPLOAD_BYTES.
DEFAULT_MAX_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024

#: How much room must remain AFTER the upload lands. Normalisation writes a
#: second copy of the file next to the original, so an upload needs roughly
#: twice its own size, plus room for the preview renders that follow.
FREE_SPACE_HEADROOM = 2.5

_CHUNK = 1 << 20


def _operator_cap() -> int | None:
    """`VAI_MAX_UPLOAD_BYTES` when an operator set a usable value, else None.
    Read live, not captured at import, so a value set in `.env` takes effect on
    the next launch without a rebuild — and so the tests can move it without
    reloading the module graph."""
    raw = os.environ.get("VAI_MAX_UPLOAD_BYTES", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def max_upload_bytes() -> int:
    """The fixed cap for a body from ANOTHER device (phone / LAN peer): the
    operator's value, else `DEFAULT_MAX_UPLOAD_BYTES`. What `/api/pair/whoami`
    advertises to the phone."""
    return _operator_cap() or DEFAULT_MAX_UPLOAD_BYTES


def _is_local_desktop(request: Request) -> bool:
    """Is this the Mac's own editor talking to its own engine?

    Loopback (the same rule auth uses — `auth._is_loopback`), and not a hosted
    deployment: a server behind a reverse proxy sees every client as loopback,
    and such deployments are the ones that set `VAI_RESTRICT_PATHS`.
    """
    from .. import config
    from .auth import _is_loopback
    return _is_loopback(request) and not config.RESTRICT_PATHS


def free_space_limit(dest_dir: Path | None = None) -> int | None:
    """The largest import the volume can take with `FREE_SPACE_HEADROOM` to
    spare (a normalised copy is written beside the original). None when the
    volume cannot be stat-ed."""
    from .. import config
    target = Path(dest_dir) if dest_dir is not None else Path(config.WORKDIR)
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        free = shutil.disk_usage(target).free
    except OSError:
        return None
    return int(free / FREE_SPACE_HEADROOM)


def upload_limit_for(request: Request) -> int | None:
    """The fixed byte cap for this request, or None when the only bound is free
    disk space (the desktop's own imports, QA-114)."""
    cap = _operator_cap()
    if cap is not None:
        return cap
    if _is_local_desktop(request):
        return None
    return DEFAULT_MAX_UPLOAD_BYTES


def advertised_limit(request: Request) -> tuple[int, str]:
    """(`max_upload_bytes`, `upload_limit`) for `/api/health`: the fixed cap
    ("fixed"), or what the disk can take right now ("free_space")."""
    cap = upload_limit_for(request)
    if cap is not None:
        return cap, "fixed"
    room = free_space_limit()
    return (room if room is not None else DEFAULT_MAX_UPLOAD_BYTES), "free_space"


#: The limit the ingress middleware chose for the request being handled. The
#: route-level streaming helper reads it (it has no Request in hand); the
#: middleware runs first and a ContextVar set there is visible downstream.
#: `_UNSET` = called outside a request (unit tests): fall back to the fixed cap.
_UNSET = object()
_REQUEST_LIMIT: ContextVar[object] = ContextVar("vai_upload_limit", default=_UNSET)


def human_bytes(n: int | float) -> str:
    """1536 -> "1.5 KB", 4294967296 -> "4 GB". Binary units, one decimal when it
    matters — "0 MB" was what a 10 KB limit used to print."""
    value = float(max(0, n))
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "bytes":
                return f"{int(value)} bytes"
            text = f"{value:.1f}".rstrip("0").rstrip(".")
            return f"{text} {unit}"
        value /= 1024
    return f"{int(n)} bytes"  # pragma: no cover - loop always returns


def _too_large(declared: int | None, limit: int | None = None) -> dict:
    limit = limit if limit is not None else max_upload_bytes()
    size = (f"That file is {human_bytes(declared)}, which is more than the "
            if declared else "That file is larger than the ")
    return {
        "error": "upload_too_large",
        "message": (f"{size}{human_bytes(limit)} this editor accepts in one "
                    f"import. Trim or compress it first, then import it again."),
        "limit_bytes": limit,
        "declared_bytes": declared,
    }


def _no_room(needed: int, free: int) -> HTTPException:
    return HTTPException(507, {
        "error": "insufficient_space",
        "message": (f"Not enough free space on this Mac to import that file. "
                    f"It needs about {human_bytes(needed)} free (importing writes "
                    f"a converted copy alongside the original) and there is "
                    f"{human_bytes(free)}. Free some space and import it again."),
        "needed_bytes": needed,
        "free_bytes": free,
    })


class UploadLimitMiddleware(BaseHTTPMiddleware):
    """Refuse an over-sized body from its `Content-Length` alone.

    Deliberately a middleware and not a per-route dependency: it covers every
    ingress including ones added later, and it answers before FastAPI has begun
    consuming the stream. Routes still call `stream_upload_to` for the bodies
    that arrive without a usable Content-Length.
    """

    async def dispatch(self, request: Request, call_next):
        limit = upload_limit_for(request)
        raw = request.headers.get("content-length")
        if raw and limit is not None and request.method in {"POST", "PUT", "PATCH"}:
            try:
                declared = int(raw)
            except ValueError:
                declared = 0
            if declared > limit:
                details = _too_large(declared, limit)
                return JSONResponse(
                    status_code=413,
                    content={"error": {"code": "TOO_LARGE",
                                       "message": details["message"],
                                       "details": details}},
                )
        token = _REQUEST_LIMIT.set(limit)
        try:
            return await call_next(request)
        finally:
            _REQUEST_LIMIT.reset(token)


def assert_room_for(request: Request, dest_dir: Path) -> None:
    """Refuse an upload the volume cannot hold, before reading a byte.

    Uses the declared Content-Length — the only size estimate available up
    front. A body with no Content-Length skips this check and is caught by the
    running total in `stream_upload_to` instead.
    """
    raw = request.headers.get("content-length")
    if not raw:
        return
    try:
        declared = int(raw)
    except ValueError:
        return
    if declared <= 0:
        return
    try:
        free = shutil.disk_usage(dest_dir).free
    except OSError:
        # An un-stat-able destination is the route's problem, not ours; failing
        # the precondition here would turn a clear "couldn't write" into a
        # confusing "not enough space".
        return
    needed = int(declared * FREE_SPACE_HEADROOM)
    if free >= needed:
        return
    raise _no_room(needed, free)


async def stream_upload_to(file: UploadFile, dst: Path) -> int:
    """Copy `file` to `dst`, aborting past the limit. Returns bytes written.

    On overflow the partial file is unlinked before the 413 is raised. Leaving
    it would mean a client could fill the disk with rejected uploads — the cap
    would count each request but the bytes would still be there.
    """
    chosen = _REQUEST_LIMIT.get()
    limit: int | None = max_upload_bytes() if chosen is _UNSET else chosen  # type: ignore[assignment]
    # The desktop's own import (limit None) is bounded by the disk instead: a
    # chunked body declares no size, so this running check is the only one.
    room = free_space_limit(dst.parent) if limit is None else None
    written = 0
    try:
        with dst.open("wb") as fh:
            while chunk := await file.read(_CHUNK):
                written += len(chunk)
                if limit is not None and written > limit:
                    raise HTTPException(413, _too_large(None, limit))
                if room is not None and written > room:
                    raise _no_room(int(written * FREE_SPACE_HEADROOM),
                                   int(room * FREE_SPACE_HEADROOM))
                fh.write(chunk)
    except HTTPException:
        dst.unlink(missing_ok=True)
        raise
    except OSError:
        dst.unlink(missing_ok=True)
        raise
    return written
