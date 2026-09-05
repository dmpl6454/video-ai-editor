"""Pairing state for the iOS companion — settings, devices, tokens, hosts.

This module is pure state + policy. It opens no sockets, imports no FastAPI,
and knows nothing about requests; `api/auth.py` enforces what it decides and
`api/pair_routes.py` exposes it. Keeping the split means the security posture
is testable without a client.

WHY A FILE AND NOT AN ENV VAR
-----------------------------
The first design read `VAE_LAN` from the environment. A double-clicked `.app`
inherits launchd's environment, which has no such variable and no user-facing
way to acquire one — so the toggle would have been unreachable in the only
build that ships, while `/api/pair/new` refused to mint a code without it. The
switch therefore lives in a JSON file next to the app's logs and `.env`, in the
one directory `platformutil.user_data_dir` guarantees is writable in the frozen
build (api/hardening.py already creates `logs/` there).

THE POSTURE LADDER
------------------
    lan_enabled() == False, bound to 127.0.0.1   -> no auth, no restriction.
                                                    This is 0.5.0 exactly, and
                                                    it is the default.
    lan_enabled() == True                        -> auth required, path
                                                    restriction forced on,
                                                    desktop.py binds 0.0.0.0.
    a request ARRIVES on a non-loopback           -> auth required REGARDLESS
    interface (however the socket got there)        of the toggle.

That last rung matters: turning the toggle off does not un-bind a socket that
is already listening on 0.0.0.0, so it must not be able to disarm the auth on
it either. `auth_required()` is the OR of both, never just the toggle.

WHAT ENFORCES THE THIRD RUNG, EXACTLY
-------------------------------------
Two things, and it is worth being precise because the wording used to be
broader than the mechanism. Until 0.6.0 `_bound_public` was set from exactly
one place — `desktop.py::main`, which knows what it asked uvicorn to bind — so
`uvicorn video_ai_editor.main:app --host 0.0.0.0` with `VAE_LAN` unset came up
on every interface with NO authentication and the path allowlist disarmed. The
docstring said "for any reason"; the mechanism covered one reason.

Now:
  * `mark_bound_public()` — desktop.py, before the server thread starts. Knows
    the intent, arms before the socket exists.
  * `note_local_address()` — `api/auth.py`, on every request, from the LOCAL
    address the connection landed on (`scope["server"]`, which uvicorn fills
    from the accepted socket's `getsockname()`). A connection that arrived on
    192.168.1.20 is proof the socket is off-machine reachable, whoever started
    it and whatever they passed. It arms BEFORE that request is evaluated, so
    the request that reveals the exposure is itself authenticated.

Deliberately one-way: nothing lowers this rung inside a process. A hand-rolled
`--host 0.0.0.0` run should still set `VAE_LAN=1` if it wants the QR flow, but
it no longer has to for the auth to exist.

THE PAIR PAYLOAD GRAMMAR
------------------------
The desktop renders this exact string as a QR code and the phone's in-app
camera reads it back:

    vaepair:v=1&h=<host>&p=<port>&c=<code>

  * `vaepair:` is an opaque scheme with NO registered handler on iOS, by
    design. The app installs no deep-link handler and declares no `scheme`, so
    a hostile web page cannot fire a pairing payload at an installed app and
    re-point the phone at a server it controls. The prefix exists only so the
    scanner can reject a QR code that is not ours, with a readable message.
  * Everything after the colon is a standard urlencoded query string, so the
    parser on the phone is `split(':', 1)` + `parse_qs` and new keys are
    additive.
  * `h` is an IPv4 literal from `host_candidates()`, `p` the port, `c` a
    128-bit hex claim code that is single-use and expires in ten minutes.

The code IS a secret on a screen: anyone who photographs it within that window
can claim a token. That is why the window is short, why claiming burns the
code, why every device can be revoked individually, and why LAN mode is the
master switch above all of it.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from .. import platformutil as _pu

# --- constants ---------------------------------------------------------------

#: An unclaimed pair code is worthless after this long. Short enough that a
#: photographed screen is not a standing invitation; long enough to walk to the
#: other side of the room and find the app.
PAIR_CODE_TTL_S = 600.0

#: Media tokens ride in a query string (`?k=`), because AVURLAsset and friends
#: will not reliably carry a custom Authorization header across a redirect or a
#: byte-range refetch. A URL leaks into logs and Referer headers, so this TTL is
#: as short as a filmstrip paint can tolerate.
MEDIA_TOKEN_TTL_S = 60.0

#: Bump when the on-disk shape changes in a way a reader must notice.
SETTINGS_VERSION = 1

_PAYLOAD_PREFIX = "vaepair:"
_CODE_RE = re.compile(r"\A[0-9a-f]{32}\Z")

# Media paths a `?k=` token is accepted on — and ONLY these. A media token is
# minted for native loaders that cannot carry a header; it must never be a
# general-purpose credential, so anything that can mutate state or read the
# chat is off the list. Matched as a suffix against the request path.
MEDIA_PATH_SUFFIXES = ("/preview.mp4", "/thumb", "/waveform")
MEDIA_PATH_FRAGMENTS = ("/files/", "/sticker/")

_lock = threading.RLock()
_pending: dict[str, float] = {}           # claim code -> expiry (monotonic)
_cache: tuple[tuple[float, int], dict] | None = None  # ((mtime, size), parsed)

#: Per-process HMAC key for media tokens. Regenerated on every boot, which is
#: the point: a media URL that leaked into a log yesterday is dead today.
_MEDIA_SECRET = secrets.token_bytes(32)

#: Set by desktop.py once it knows what it actually bound to. See the posture
#: ladder above — this is the rung the toggle cannot lower.
_bound_public = False

#: Set by desktop.py alongside the bind address. 0 means "nobody told us".
_server_port = 0


# --- settings file -----------------------------------------------------------

def settings_path() -> Path:
    """`~/Library/Application Support/Video AI Editor/settings.json` on macOS.

    Resolved per call rather than at import so the tests (and a relocated data
    dir) can point `platformutil.user_data_dir` somewhere else.
    """
    return _pu.user_data_dir("Video AI Editor") / "settings.json"


def _blank() -> dict:
    return {"version": SETTINGS_VERSION, "lan_enabled": False, "devices": []}


def load_settings() -> dict:
    """Read settings, memoised on the file's mtime.

    `auth_required()` runs on every request, so an unconditional read+parse per
    request would put a stat AND a JSON parse on the hot path of the filmstrip.
    An mtime check is one stat; a write from the Phone panel changes it and the
    next request re-reads. A missing or corrupt file reads as defaults rather
    than raising — a broken settings file must fail CLOSED on features, not
    take the whole editor down.
    """
    global _cache
    p = settings_path()
    try:
        st = p.stat()
        # Size as well as mtime: two writes inside one filesystem timestamp
        # tick are rare but not impossible, and a stale "which phones are
        # paired" answer is the kind of bug that only shows up on someone
        # else's machine.
        stamp = (st.st_mtime, st.st_size)
    except OSError:
        # No settings file at all — the default posture, and the hot path for
        # every request on a normal desktop. Only touch the lock if there is
        # actually something to invalidate.
        if _cache is not None:
            with _lock:
                _cache = None
        return _blank()
    with _lock:
        if _cache is not None and _cache[0] == stamp:
            return _cache[1]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _blank()
    if not isinstance(data, dict):
        return _blank()
    merged = {**_blank(), **data}
    if not isinstance(merged.get("devices"), list):
        merged["devices"] = []
    with _lock:
        _cache = (stamp, merged)
    return merged


def save_settings(data: dict) -> None:
    """Write settings atomically and invalidate the mtime cache.

    Atomic because this file is the only record of which phones are paired: a
    half-written file after a crash would silently unpair every device, and the
    user's next clue would be an iPhone that says "not paired" with no
    explanation.
    """
    global _cache
    p = settings_path()
    # 0o700 on the directory and 0o600 on the file. This is the sole record of
    # which phones are paired — every device id, name, and last_seen — and on a
    # shared Mac the process umask made it 0644, readable by every other local
    # account. The tokens themselves are SHA-256 of 256-bit secrets, so they are
    # not recoverable from it, but "who is paired with this Mac" is not public
    # information either.
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # `mkstemp` rather than a predictable `.json.tmp`: the name is unguessable
    # and the descriptor is O_EXCL | 0o600 from the first byte, so there is no
    # window in which a world-readable temp file holds the same content.
    fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=".settings-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    with _lock:
        _cache = None


def _mutate(fn) -> None:
    """Read-modify-write settings under `_lock`, as one operation.

    WHY THIS EXISTS
    ---------------
    `claim_pair_code`, `touch_device`, `revoke_device` and `set_lan_enabled`
    each did `load_settings()` -> compute -> `save_settings()` with no lock
    across the pair. `save_settings` is atomic per WRITE (os.replace), which
    guarantees no torn file — and guarantees nothing at all about last-writer
    correctness.

    The failure that made this worth fixing: the user revokes a lost iPhone
    from the Phone panel. `revoke_device` runs on a threadpool worker. Meanwhile
    `PairAuthMiddleware` calls `touch_device` on the event loop for a request
    that authenticated a moment earlier — it has already loaded the OLD device
    list, and its write puts the revoked phone back with a fresh `last_seen`.
    The panel shows the phone gone; the phone keeps working.

    `_lock` is an RLock and `load_settings` takes it internally, so the
    re-entrancy is already handled.
    """
    with _lock:
        save_settings(fn(load_settings()))


# --- posture -----------------------------------------------------------------

def _env_lan() -> bool:
    """`VAE_LAN=1` forces LAN mode on.

    NOT the shipping mechanism (see the module docstring) — this exists so the
    test suite and `uvicorn --host 0.0.0.0` dev runs can arm the posture without
    writing to the user's real Application Support directory.
    """
    return os.environ.get("VAE_LAN", "").strip().lower() in {"1", "true", "yes", "on"}


def lan_enabled() -> bool:
    """Is the phone companion allowed to exist? Read live, never cached in a
    module constant — the desktop's Phone panel flips this at runtime."""
    if _env_lan():
        return True
    return bool(load_settings().get("lan_enabled", False))


def mark_bound_public(public: bool) -> None:
    """Record that the HTTP socket is (or is not) reachable off-machine.

    Called by desktop.py before the server thread starts. See the posture
    ladder: this is what stops the LAN toggle from disarming auth on a socket
    that is still listening on every interface.
    """
    global _bound_public
    _bound_public = bool(public)
    sync_path_restriction()


def _is_public_local_addr(addr: object) -> bool:
    """Is `addr` a real, non-loopback IP literal?

    Deliberately strict about being a LITERAL. Starlette's `TestClient` reports
    `scope["server"] == ("testserver", 80)` — a name, not an address — and a
    name proves nothing about which interface anything arrived on, so it must
    never arm the posture. Only an address the kernel actually assigned to an
    interface can do that.
    """
    if not isinstance(addr, str) or not addr:
        return False
    host = addr.strip().lower().strip("[]")
    if host in {"127.0.0.1", "::1", "0.0.0.0", "::", "localhost", "testserver"}:
        return False
    if host.startswith("127."):
        return False
    try:
        import ipaddress
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not ip.is_loopback and not ip.is_unspecified


def note_local_address(addr: object) -> None:
    """Observe the local address a request landed on; arm if it is off-machine.

    The second half of the posture ladder's third rung — see the module
    docstring. One-way on purpose: a process that has served a connection on a
    LAN interface has proved its socket is reachable there, and a later
    loopback request does not un-prove it.
    """
    global _bound_public
    if _bound_public or not _is_public_local_addr(addr):
        return
    mark_bound_public(True)


def bound_public() -> bool:
    return _bound_public


def set_server_port(port: int) -> None:
    """Record the port uvicorn actually bound.

    The QR payload has to carry a port, and `VAE_PORT` is only the *request*:
    `desktop.py` is the one place that knows what was really used, and a dev
    run on :8000 must not print :8765 on a code the phone then fails to reach.
    """
    global _server_port
    _server_port = int(port)


def server_port(observed: int = 0) -> int:
    """Port for the pair payload.

    Preference order, and the order matters because a wrong port here is a QR
    the phone silently fails to reach:

    1. What `desktop.py` recorded via `set_server_port` — the packaged app is
       the only caller that truly knows what uvicorn bound.
    2. `observed`: the port the CURRENT request arrived on. A bare
       `uvicorn ... --port 8000` run (the documented dev path, and what run.sh
       does) never calls `set_server_port`, and used to fall through to the
       packaged default of 8765 — printing a code for a port with nothing
       behind it. The request itself is ground truth, so use it.
    3. `VAE_PORT`, then the packaged default, for callers with no request in
       hand.
    """
    if _server_port:
        return _server_port
    if observed:
        return int(observed)
    try:
        return int(os.environ.get("VAE_PORT", "8765"))
    except ValueError:
        return 8765


def observed_port(request) -> int:
    """The port this request actually arrived on, or 0 if the server did not
    say. Starlette puts it in `scope["server"] = (host, port)`; `url.port` is
    None when the client omitted a non-default port from the Host header."""
    server = request.scope.get("server") or ()
    if len(server) == 2 and server[1]:
        return int(server[1])
    port = getattr(request.url, "port", None)
    return int(port) if port else 0


def auth_required() -> bool:
    """Must every request carry a paired bearer token?

    False in the default desktop posture and false under `TestClient`, which is
    why the 1338 existing tests keep passing untouched.
    """
    return lan_enabled() or _bound_public


def sync_path_restriction() -> None:
    """Arm or disarm config's filesystem allowlist to match the posture.

    Called from `api/auth.install()` at boot and from every LAN toggle. The two
    are one decision: a socket that a stranger can reach must not also hand
    that stranger `import_srt.path` pointed at `~/.ssh/id_ed25519`.
    """
    from ..config import enable_path_restriction
    enable_path_restriction(auth_required())


def set_lan_enabled(enabled: bool) -> dict:
    """Persist the LAN toggle. Returns the new public state.

    `restart_required` is the honest part: the bind address is chosen once, in
    `desktop.py::main`, before uvicorn starts. Turning LAN on cannot move a
    listening socket from 127.0.0.1 to 0.0.0.0, so the panel has to say
    "restart the app" rather than pretend the phone can connect now.
    """
    _mutate(lambda data: {**data, "lan_enabled": bool(enabled)})
    sync_path_restriction()
    return {
        "lan_enabled": bool(enabled),
        "bound_public": _bound_public,
        # Off -> on needs a restart to re-bind. On -> off needs one to stop
        # listening. Either way the socket in front of the caller is unchanged.
        "restart_required": bool(enabled) != _bound_public,
        "auth_required": auth_required(),
    }


# --- devices + tokens --------------------------------------------------------

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def list_devices() -> list[dict]:
    """Paired devices, without their token hashes."""
    return [{"id": d.get("id", ""), "name": d.get("name", ""),
             "created_at": d.get("created_at", 0.0),
             "last_seen": d.get("last_seen", 0.0)}
            for d in load_settings().get("devices", [])
            if isinstance(d, dict)]


def new_pair_code() -> str:
    """Mint a single-use claim code and start its ten-minute clock."""
    code = secrets.token_hex(16)
    with _lock:
        _prune_pending()
        _pending[code] = time.monotonic() + PAIR_CODE_TTL_S
    return code


def _prune_pending() -> None:
    now = time.monotonic()
    for code in [c for c, exp in _pending.items() if exp <= now]:
        del _pending[code]


def pending_codes() -> int:
    with _lock:
        _prune_pending()
        return len(_pending)


def revoke_pair_code(code: str) -> bool:
    with _lock:
        return _pending.pop(code, None) is not None


def claim_pair_code(code: str, device_name: str) -> dict | None:
    """Burn `code` and register a device. Returns the device record with its
    one-and-only plaintext token, or None if the code is unknown or expired.

    The token is stored hashed. That is not theatre about file permissions —
    it means a settings.json handed to a support thread, or synced into a
    backup, is not a live credential for the Mac.
    """
    if not _CODE_RE.match(code or ""):
        return None
    with _lock:
        _prune_pending()
        if _pending.pop(code, None) is None:
            return None
    token = secrets.token_urlsafe(32)
    device = {
        "id": secrets.token_hex(8),
        "name": (device_name or "iPhone").strip()[:64] or "iPhone",
        "token_sha256": _hash_token(token),
        "created_at": time.time(),
        "last_seen": time.time(),
    }
    _mutate(lambda data: {
        **data,
        "devices": [d for d in data.get("devices", []) if isinstance(d, dict)] + [device],
    })
    return {"id": device["id"], "name": device["name"], "token": token,
            "created_at": device["created_at"]}


def device_for_token(token: str) -> dict | None:
    """Resolve a bearer token to its device record, or None.

    Compared with `compare_digest` over the hashes so a wrong token costs the
    same time as a right one.
    """
    if not token:
        return None
    wanted = _hash_token(token)
    for d in load_settings().get("devices", []):
        if not isinstance(d, dict):
            continue
        stored = d.get("token_sha256") or ""
        if stored and hmac.compare_digest(stored, wanted):
            return d
    return None


def device_by_id(device_id: str) -> dict | None:
    for d in load_settings().get("devices", []):
        if isinstance(d, dict) and d.get("id") == device_id:
            return d
    return None


def touch_device(device_id: str) -> None:
    """Record a device's last activity — but only once a minute.

    A filmstrip paint is dozens of requests; persisting `last_seen` on each one
    would rewrite settings.json dozens of times a second and invalidate the
    settings cache every time, turning the auth check back into a JSON parse per
    request. One write a minute is enough to answer "is this phone still
    around?" in the Phone panel.
    """
    now = time.time()
    # The whole read-modify-write is inside `_lock`, including the "is it worth
    # writing?" decision: a touch that raced a revoke used to resurrect the
    # revoked device from a list it had loaded before the revoke landed.
    with _lock:
        data = load_settings()
        devices = [d for d in data.get("devices", []) if isinstance(d, dict)]
        current = next((d for d in devices if d.get("id") == device_id), None)
        if current is None:
            return
        if now - float(current.get("last_seen") or 0.0) < 60.0:
            return
        save_settings({**data, "devices": [
            {**d, "last_seen": now} if d.get("id") == device_id else d
            for d in devices]})


def revoke_device(device_id: str) -> bool:
    """Unpair one phone. Its bearer AND every media token it holds die with it,
    because media tokens are re-checked against the device list on use."""
    with _lock:
        data = load_settings()
        devices = [d for d in data.get("devices", []) if isinstance(d, dict)]
        remaining = [d for d in devices if d.get("id") != device_id]
        if len(remaining) == len(devices):
            return False
        save_settings({**data, "devices": remaining})
    return True


# --- media tokens ------------------------------------------------------------

def mint_media_token(device_id: str) -> dict:
    """A 60-second, media-paths-only credential for native loaders.

    Stateless HMAC rather than a table: a timeline paint asks for one token and
    then issues ~24 thumbnail requests with it, and a table would either grow
    per paint or need sweeping. Revocation still works because `verify` looks
    the device up again on every use.
    """
    expires = time.time() + MEDIA_TOKEN_TTL_S
    body = f"{device_id}.{int(expires)}"
    sig = hmac.new(_MEDIA_SECRET, body.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return {"token": f"{body}.{sig}", "expires_at": expires,
            "ttl_s": MEDIA_TOKEN_TTL_S}


def verify_media_token(token: str) -> dict | None:
    """Return the device for a valid, unexpired media token, else None."""
    if not token or token.count(".") != 2:
        return None
    device_id, exp_raw, sig = token.split(".")
    try:
        expires = int(exp_raw)
    except ValueError:
        return None
    if expires < time.time():
        return None
    body = f"{device_id}.{exp_raw}"
    expect = hmac.new(_MEDIA_SECRET, body.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expect, sig):
        return None
    return device_by_id(device_id)


def is_media_path(path: str) -> bool:
    """Is this a path a `?k=` media token may be used on?"""
    return (path.endswith(MEDIA_PATH_SUFFIXES)
            or any(frag in path for frag in MEDIA_PATH_FRAGMENTS))


# --- host discovery ----------------------------------------------------------

_PRIVATE_PREFIXES = ("10.", "192.168.", "169.254.")


def is_private_ipv4(addr: str) -> bool:
    """RFC 1918 + link-local + CGNAT (100.64/10, which is what a Tailscale or
    a carrier-NAT interface looks like). Anything else is either loopback,
    handled by the caller, or a public address we refuse to advertise."""
    if addr.startswith(_PRIVATE_PREFIXES):
        return True
    parts = addr.split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return False
    a, b = int(parts[0]), int(parts[1])
    if a == 172 and 16 <= b <= 31:
        return True
    if a == 100 and 64 <= b <= 127:
        return True
    return False


def _primary_ipv4() -> str | None:
    """The address this machine would use to reach the outside world.

    A connected UDP socket sends no packets — it just makes the kernel pick a
    source address via the routing table — so this is instant and works with no
    network. Deliberately NOT `getaddrinfo(gethostname())`, which on this Mac
    returns no IPv4 at all: the hostname has no A record on a DHCP LAN, so that
    approach reports "no network" on a perfectly connected machine.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # TEST-NET-1, reserved for documentation. Never routed anywhere.
        s.connect(("192.0.2.1", 53))
        addr = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return addr if isinstance(addr, str) and addr != "0.0.0.0" else None


def _ifconfig_ipv4() -> list[str]:
    """Every IPv4 the machine has, from `ifconfig -a` / `ipconfig`.

    The fallback and the completeness pass: `_primary_ipv4` returns exactly one
    address, and a Mac plugged into ethernet AND on Wi-Fi has two the phone
    could use. Best-effort — a missing binary just means one candidate.
    """
    cmd = ["ipconfig"] if _pu.IS_WINDOWS else ["/sbin/ifconfig", "-a"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=5,
                              **_pu.SUBPROCESS_FLAGS)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return re.findall(r"\b(?:inet |IPv4 Address[^:]*:\s*)(\d+\.\d+\.\d+\.\d+)",
                      proc.stdout)


def host_candidates() -> list[str]:
    """Addresses the phone could dial this Mac on, best first.

    RFC 1918 addresses come first because those are the ones iOS's Local
    Network permission covers and the ones the companion's "is this a local
    address?" check accepts without a warning. CGNAT/link-local addresses are
    appended rather than dropped so a Tailscale-only setup still has something
    to show — the phone is the side that decides whether to warn about them.
    """
    found: list[str] = []
    for addr in [_primary_ipv4(), *_ifconfig_ipv4()]:
        if not addr or addr in found or addr.startswith("127."):
            continue
        if not is_private_ipv4(addr):
            continue
        found.append(addr)
    # Stable: RFC 1918 keeps discovery order (primary first), CGNAT and
    # link-local fall to the back without being reordered among themselves.
    return sorted(found, key=lambda a: 0 if a.startswith(("10.", "192.168.", "172.")) else 1)


def pair_payload(host: str, port: int, code: str) -> str:
    """Build the exact string the desktop renders as a QR code.

    See the grammar in the module docstring. Kept here, next to the parser's
    only source of truth, so the phone side can be written against one
    definition rather than against a paraphrase in a spec.
    """
    return _PAYLOAD_PREFIX + urlencode({"v": "1", "h": host,
                                        "p": str(int(port)), "c": code})


def server_info() -> dict[str, Any]:
    """Facts the phone needs to set expectations before it asks for anything.

    `job_workers` is here because it is the difference between "the render is
    slow" and "the render has not started": the pool is shared with the desktop
    (api/jobs.py), so one export plus one `upscale` parks every preview in
    `queued` at 0%. The phone shows the queue rather than a lying progress bar.
    """
    from ..config import APP_VERSION
    from .uploads import max_upload_bytes
    return {
        "version": APP_VERSION,
        "lan_enabled": lan_enabled(),
        "auth_required": auth_required(),
        "job_workers": _job_workers(),
        "max_upload_bytes": max_upload_bytes(),
        "media_token_ttl_s": MEDIA_TOKEN_TTL_S,
    }


def _job_workers() -> int:
    """Size of the shared background-job pool.

    Read off the live executor rather than re-reading VAI_JOB_WORKERS, because
    the singleton in api/jobs.py is built once at import: if anything changed
    the variable afterwards, the env would report a number that no longer
    matches the pool the phone is actually queueing behind — which is the exact
    lie this field exists to prevent. Falls back to the same default jobs.py
    uses if the executor ever stops exposing it.
    """
    from .jobs import JOB_MANAGER
    workers = getattr(JOB_MANAGER._executor, "_max_workers", None)
    if isinstance(workers, int) and workers > 0:
        return workers
    return int(os.environ.get("VAI_JOB_WORKERS", "2"))
