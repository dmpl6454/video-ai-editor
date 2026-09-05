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
"""
from __future__ import annotations

import os
import shutil
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


def max_upload_bytes() -> int:
    """Read live, not captured at import, so a user who sets the variable in
    `.env` and restarts gets it without a rebuild — and so the tests can move
    it without reloading the module graph."""
    raw = os.environ.get("VAI_MAX_UPLOAD_BYTES", "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            return DEFAULT_MAX_UPLOAD_BYTES
        if value > 0:
            return value
    return DEFAULT_MAX_UPLOAD_BYTES


def _too_large(declared: int | None) -> dict:
    limit = max_upload_bytes()
    return {
        "error": "upload_too_large",
        "message": (f"That file is larger than this Mac will accept "
                    f"({limit // (1024 * 1024)} MB). Trim it first, or raise "
                    f"VAI_MAX_UPLOAD_BYTES and restart."),
        "limit_bytes": limit,
        "declared_bytes": declared,
    }


class UploadLimitMiddleware(BaseHTTPMiddleware):
    """Refuse an over-sized body from its `Content-Length` alone.

    Deliberately a middleware and not a per-route dependency: it covers every
    ingress including ones added later, and it answers before FastAPI has begun
    consuming the stream. Routes still call `stream_upload_to` for the bodies
    that arrive without a usable Content-Length.
    """

    async def dispatch(self, request: Request, call_next):
        raw = request.headers.get("content-length")
        if raw and request.method in {"POST", "PUT", "PATCH"}:
            try:
                declared = int(raw)
            except ValueError:
                declared = 0
            if declared > max_upload_bytes():
                details = _too_large(declared)
                return JSONResponse(
                    status_code=413,
                    content={"error": {"code": "TOO_LARGE",
                                       "message": details["message"],
                                       "details": details}},
                )
        return await call_next(request)


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
    raise HTTPException(507, {
        "error": "insufficient_space",
        "message": (f"Not enough free space on this Mac to import that file. "
                    f"It needs about {needed // (1024 * 1024)} MB free "
                    f"(importing writes a normalised copy alongside the "
                    f"original) and there is {free // (1024 * 1024)} MB."),
        "needed_bytes": needed,
        "free_bytes": free,
    })


async def stream_upload_to(file: UploadFile, dst: Path) -> int:
    """Copy `file` to `dst`, aborting past the limit. Returns bytes written.

    On overflow the partial file is unlinked before the 413 is raised. Leaving
    it would mean a client could fill the disk with rejected uploads — the cap
    would count each request but the bytes would still be there.
    """
    limit = max_upload_bytes()
    written = 0
    try:
        with dst.open("wb") as fh:
            while chunk := await file.read(_CHUNK):
                written += len(chunk)
                if written > limit:
                    raise HTTPException(413, _too_large(None))
                fh.write(chunk)
    except HTTPException:
        dst.unlink(missing_ok=True)
        raise
    except OSError:
        dst.unlink(missing_ok=True)
        raise
    return written
