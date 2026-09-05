"""Bearer auth, Host validation and the auth lockout for LAN mode.

WHAT THIS DEFENDS AGAINST
-------------------------
Through 0.5.0 the server had no authentication of any kind. That was a
defensible choice while the only socket was `127.0.0.1:8765` and the only
principal was the person at the keyboard. The phone companion changes the
premise: to reach the Mac from an iPhone the socket has to be on the LAN, and
at that moment every peer on the coffee-shop Wi-Fi can call `/dispatch`.

Four layers, because no single one of them is enough:

  1. **Bearer token.** Only a device that claimed a pair code gets one, and it
     is checked on every request. This is the actual authentication.
  2. **Host allowlist -> 421.** A bearer alone does not stop DNS rebinding: a
     web page on `evil.com` can re-resolve its own name to the Mac's LAN IP and
     then make same-origin requests to it from the victim's browser. The
     browser sends `Host: evil.com`. We only ever answer to a loopback name or
     a bare IP literal, so a rebinding request is refused before it reaches a
     route. Enforced unconditionally on `/api/pair/*` — the mint-and-read path
     is what a rebinding attack would want most — and everywhere once auth is
     armed.
  3. **`Sec-Fetch-Site: cross-site` refused.** Belt to the Host check's braces,
     and it costs one dict lookup. Native clients send no such header.
  4. **`X-VAE-Client: 1` required.** A custom header is not a CORS-simple
     header, so a browser MUST preflight before sending it, and this app's CORS
     policy only allows `http://localhost:5173`. A page that cannot preflight
     cannot send the header, and without the header it cannot reach an API
     route at all. Native clients simply set it.

MIDDLEWARE ORDER IS PART OF THE DESIGN
--------------------------------------
Starlette runs the LAST-added middleware first, so `main.py` installs this
between `hardening.install()` and the CORS block, which produces:

    CORSMiddleware  ->  PairAuthMiddleware  ->  UploadLimitMiddleware
                    ->  RequestContextMiddleware (rate limit)  ->  route

CORS must stay outermost or an `OPTIONS` preflight would hit auth, get a 401,
and every browser request would fail with a CORS error that names the wrong
cause. Auth must stay outside the rate limiter so that a flood of
unauthenticated requests is rejected as unauthenticated rather than absorbed
into a per-path rate bucket.

WHY THE EXISTING 1338 TESTS STILL PASS
--------------------------------------
Enforcement is gated on `pairing.auth_required()`, which is False unless LAN
mode is on or the socket is bound publicly — neither of which a test does. On
top of that, `TestClient` reports `request.client.host == "testclient"` and
sends `Host: testserver`; both are explicitly allowed, so a future test that
DOES arm the posture still works.
"""
from __future__ import annotations

import os
import time
import uuid
from collections import OrderedDict, deque

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware

from . import pairing
# `_envelope` is private by name only — it is hardening's single definition of
# this app's error body, and re-implementing the shape here is how two
# error formats end up on one API.
from .hardening import METRICS, _envelope
from .uploads import UploadLimitMiddleware

#: Paths that answer before auth, always. Ops probes leak nothing and are the
#: only way to tell "the Mac is asleep" from "the Mac said no" from outside.
#: Exact matches, not prefixes — a prefix test would also open anything that
#: merely starts with these names.
_OPEN_PATHS = frozenset({"/livez", "/readyz"})

#: The one API route that MUST work without a token, because obtaining a token
#: is what it does. Protected by the pair code itself (single-use, 10 minutes),
#: the Host allowlist and the rate limiter.
_CLAIM_PATH = "/api/pair/claim"

#: Failed auths per IP per window before the IP is locked out. Sized against
#: the client, not the attacker: a single timeline paint issues ~24 thumbnail
#: requests at once, so a threshold near that number would lock out a phone
#: whose token had merely expired and then present the user with a green
#: connection banner over a dead app. Media paths are excluded from the count
#: entirely for the same reason.
LOCKOUT_THRESHOLD = 60
LOCKOUT_WINDOW_S = 60.0

#: Bound so the failure map cannot itself become the memory exhaustion it is
#: meant to prevent.
_MAX_LOCKOUT_KEYS = 1024

_failures: OrderedDict[str, deque[float]] = OrderedDict()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _reject(*, status: int, code: str, message: str, request_id: str):
    """One exit for every refusal, so /metrics can answer "is the phone being
    turned away, and for which of the four reasons?".

    These responses never reach RequestContextMiddleware — auth sits outside it
    — so without this counter a wall of 401s would be invisible to /metrics
    while every 429 was visible, which is precisely backwards.
    """
    METRICS.counter("vai_auth_rejected_total", {"reason": code})
    return _envelope(status=status, code=code, message=message,
                     request_id=request_id)


def _is_loopback(request: Request) -> bool:
    """Is this request from the machine the server runs on?

    The desktop's own React frontend holds no bearer token and never will —
    it is served by this process to a webview on the same machine — so
    loopback is a first-class principal, not a bypass. `testclient` is
    Starlette's synthetic peer and is accepted only under pytest, so it can
    never be a real network peer's chosen hostname.
    """
    host = _client_ip(request)
    if host in {"127.0.0.1", "::1", "localhost"}:
        return True
    return host == "testclient" and bool(os.environ.get("PYTEST_CURRENT_TEST"))


def host_header_allowed(raw: str) -> bool:
    """Accept loopback names, bare IP literals, and Starlette's `testserver`.

    Reject every other NAME. That is the whole DNS-rebinding defence: an
    attacker can point `evil.com` at a private IP, but they cannot make the
    victim's browser send a `Host` header that is not their own domain. A bare
    IP in the Host header means the user typed the IP, which is direct LAN
    access, not rebinding.

    A consequence worth stating: `http://my-mac.local:8765` is refused. The
    pair payload always carries an IP literal (`pairing.host_candidates`), so
    nothing the app itself produces relies on an mDNS name.
    """
    host = (raw or "").strip().lower()
    if not host:
        return False
    if host.startswith("["):                      # [::1]:8765
        host = host.split("]", 1)[0].lstrip("[")
    elif host.count(":") == 1:                    # 10.0.0.5:8765
        host = host.split(":", 1)[0]
    if host in {"localhost", "127.0.0.1", "::1", "testserver", "0.0.0.0"}:
        return True
    if host.startswith("127."):
        return True
    parts = host.split(".")
    return len(parts) == 4 and all(p.isdigit() and len(p) <= 3 for p in parts)


def _record_failure(ip: str) -> bool:
    """Note one auth failure. Returns True once the IP is locked out."""
    now = time.monotonic()
    window = _failures.get(ip)
    if window is None:
        if len(_failures) >= _MAX_LOCKOUT_KEYS:
            _failures.popitem(last=False)
        window = _failures[ip] = deque()
    else:
        _failures.move_to_end(ip)
    cutoff = now - LOCKOUT_WINDOW_S
    while window and window[0] < cutoff:
        window.popleft()
    window.append(now)
    return len(window) >= LOCKOUT_THRESHOLD


def _locked_out(ip: str) -> bool:
    window = _failures.get(ip)
    if not window:
        return False
    cutoff = time.monotonic() - LOCKOUT_WINDOW_S
    while window and window[0] < cutoff:
        window.popleft()
    return len(window) >= LOCKOUT_THRESHOLD


def reset_lockout(ip: str | None = None) -> None:
    """Clear the failure counter. Called on a successful auth (a device that
    just proved itself is not an attacker mid-guess) and by the tests."""
    if ip is None:
        _failures.clear()
    else:
        _failures.pop(ip, None)


def _bearer(request: Request) -> str:
    raw = request.headers.get("authorization") or ""
    if raw[:7].lower() == "bearer ":
        return raw[7:].strip()
    return ""


class PairAuthMiddleware(BaseHTTPMiddleware):
    """Enforce the four layers described in the module docstring."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in _OPEN_PATHS:
            return await call_next(request)

        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]

        # The posture ladder's third rung, observed rather than declared.
        # `scope["server"]` is the LOCAL address this connection landed on —
        # uvicorn fills it from the accepted socket's `getsockname()` — so a
        # non-loopback value is proof the socket is reachable off-machine, no
        # matter who started the process or what flags they passed. Before
        # 0.6.0 only `desktop.py::main` could arm this, which left
        # `uvicorn ... --host 0.0.0.0` with `VAE_LAN` unset running with no
        # authentication at all.
        #
        # Note the ORDER: arm first, read `auth_required()` second, so the very
        # request that reveals the exposure is already covered by it.
        server = request.scope.get("server") or (None, None)
        pairing.note_local_address(server[0])

        armed = pairing.auth_required()

        # --- layer 2/3: origin plausibility -----------------------------------
        # UNCONDITIONAL, on every route, in every posture.
        #
        # This block used to sit behind `if armed or is_pair_route:` — on for
        # the pair routes, off for everything else until LAN mode armed — which
        # meant it did nothing at all in the ONLY posture that ships by default
        # (LAN off, bound to 127.0.0.1) — i.e. it was switched off for 100% of
        # users while the CHANGELOG claimed "a page on evil.com that re-resolves
        # its own name to your Mac's LAN address now gets a 421 instead of a
        # session". It did not: `POST /api/sessions` with `Host: evil.com` and
        # `Sec-Fetch-Site: cross-site` returned a session id, and the follow-up
        # `dispatch` reached the handler. CORS is no help there — after a DNS
        # rebind the page is same-origin to the browser — and in that posture
        # `restrict_paths_active()` is False, so `add_clip.src` will read any
        # file on the Mac and `export_srt.path` will write one.
        #
        # It costs two dict lookups and breaks nothing the desktop does: the
        # PyWebView shell and the Vite dev proxy both send `Host: 127.0.0.1:8765`
        # or `localhost:5173`, both allowed; the frontend fetches `/api/...`
        # relative, so the browser sends `same-origin`, never `cross-site`.
        if not host_header_allowed(request.headers.get("host", "")):
            return _reject(
                status=421, code="MISDIRECTED_REQUEST", request_id=rid,
                message=("This server only answers to its own address. If "
                         "you reached it through a hostname, use the IP "
                         "shown in the desktop app's Phone panel."))
        if request.headers.get("sec-fetch-site", "") == "cross-site":
            return _reject(
                status=403, code="FORBIDDEN", request_id=rid,
                message="Cross-site requests are not accepted.")

        if not armed:
            return await call_next(request)

        # --- everything below here only happens in LAN mode -------------------
        if _is_loopback(request):
            return await call_next(request)

        media = pairing.is_media_path(path)

        # --- layer 4: the header a hostile page cannot send -------------------
        # Media paths are exempt because native players (AVURLAsset) drop custom
        # headers across redirects and range refetches; they authenticate with a
        # short-lived `?k=` token instead, which a page cannot obtain.
        if not media and request.headers.get("x-vae-client") != "1":
            return _reject(
                status=403, code="FORBIDDEN", request_id=rid,
                message="Missing client header. Update the companion app.")

        if _locked_out(_client_ip(request)):
            return _reject(
                status=401, code="AUTH_LOCKOUT", request_id=rid,
                message=("Too many rejected requests from this device. Wait a "
                         "minute, then pair again from the desktop app."))

        # Claiming a pair code is how a device GETS a token, so it cannot
        # require one. It is guarded by the code itself (128 bits, single-use,
        # ten minutes), by everything above, and by the rate limiter below.
        if path == _CLAIM_PATH:
            return await call_next(request)

        device = self._authenticate(request, media)
        if device is None:
            # Media failures never count toward the lockout: one stale token
            # plus one filmstrip paint is two dozen 401s in under a second, and
            # locking the device out for that would be the app punishing itself.
            if not media:
                _record_failure(_client_ip(request))
            return _reject(
                status=401, code="UNAUTHORIZED", request_id=rid,
                message="Not paired with this Mac. Scan the code again.")

        reset_lockout(_client_ip(request))
        pairing.touch_device(device.get("id", ""))
        request.state.device_id = device.get("id", "")
        return await call_next(request)

    @staticmethod
    def _authenticate(request: Request, media: bool) -> dict | None:
        token = _bearer(request)
        if token:
            return pairing.device_for_token(token)
        if media:
            k = request.query_params.get("k")
            if k:
                return pairing.verify_media_token(k)
        return None


def install(app: FastAPI) -> None:
    """Wire auth + upload limits onto an app. Idempotent, like hardening's."""
    if getattr(app.state, "_pair_auth_installed", False):
        return
    # Added inner-first: the LAST add_middleware call is the OUTERMOST layer,
    # so auth gets to reject a request before the upload cap reads its headers.
    app.add_middleware(UploadLimitMiddleware)
    app.add_middleware(PairAuthMiddleware)
    app.state._pair_auth_installed = True
    # Arm (or leave disarmed) the filesystem allowlist to match the posture the
    # process booted into. Without this, a Mac that has LAN mode saved in
    # settings.json would come up listening on 0.0.0.0 with `import_srt.path`
    # pointed anywhere it likes.
    pairing.sync_path_restriction()
