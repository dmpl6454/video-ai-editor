"""`/api/pair/*` — the endpoints the desktop panel and the phone talk to.

Split by who is allowed to call them:

  DESKTOP ONLY (loopback, and refused outright when LAN mode is off)
      POST /api/pair/lan       arm or disarm LAN mode
      GET  /api/pair/info      LAN state, addresses, limits — drives the panel
      POST /api/pair/new       mint a claim code + the QR payload
      GET  /api/pair/devices   list paired phones
      POST /api/pair/revoke    unpair one phone

  PHONE
      POST /api/pair/claim     burn a code, receive a bearer token
      GET  /api/pair/whoami    who am I, and what can this Mac do
      POST /api/pair/media_token  a 60s `?k=` token for native media loaders

`api/auth.py` has already applied the Host allowlist and the `Sec-Fetch-Site`
check to every path here (unconditionally, LAN mode or not) by the time a
handler runs, and has already rejected an unauthenticated caller on everything
except `/claim`. What is left for these handlers is the desktop/phone split.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import pairing
from .auth import _is_loopback

router = APIRouter(prefix="/api/pair", tags=["pair"])


class LanRequest(BaseModel):
    enabled: bool


class ClaimRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    device_name: str = Field(default="iPhone", max_length=64)


class RevokeRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)


def _require_desktop(request: Request) -> None:
    """These routes control the security posture itself.

    Only the person at the Mac may arm LAN mode, mint a pairing code or revoke
    a device — a paired phone must not be able to pair a second phone, and a
    LAN peer must not be able to do any of it. Loopback is the check because
    loopback is exactly "someone at this machine".
    """
    if not _is_loopback(request):
        raise HTTPException(403, {
            "error": "desktop_only",
            "message": "Do this in the Video AI Editor app on your Mac.",
        })


def _require_lan() -> None:
    """Refuse to hand out pairing material while LAN mode is off.

    Without this, `/api/pair/new` would mint a code for a server no phone can
    reach, and the user would blame the phone. The panel calls `/info` first
    and only offers the button when `lan_enabled` is true.
    """
    if not pairing.lan_enabled():
        raise HTTPException(409, {
            "error": "lan_disabled",
            "message": ("Turn on “Allow my iPhone to connect” first. It is off "
                        "by default: it puts this Mac's editor on your local "
                        "network."),
        })


@router.get("/info")
def pair_info(request: Request) -> dict:
    """Everything the desktop's Phone panel renders."""
    _require_desktop(request)
    return {
        **pairing.server_info(),
        "bound_public": pairing.bound_public(),
        "hosts": pairing.host_candidates(),
        "devices": pairing.list_devices(),
        "pending_codes": pairing.pending_codes(),
        "settings_path": str(pairing.settings_path()),
        "code_ttl_s": pairing.PAIR_CODE_TTL_S,
    }


@router.post("/lan")
def set_lan(request: Request, body: LanRequest) -> dict:
    """Arm or disarm LAN mode, persisted across restarts.

    Returns `restart_required` because the bind address is decided once, in
    `desktop.py::main`, before uvicorn starts — see
    `pairing.set_lan_enabled`. The panel must say so rather than showing a QR
    code for a socket that is still on 127.0.0.1.
    """
    _require_desktop(request)
    result = pairing.set_lan_enabled(body.enabled)
    return {
        **result,
        "hosts": pairing.host_candidates(),
        # The escape hatch for a user whose footage lives somewhere the LAN
        # allowlist does not cover. Stated here so the panel can print it
        # verbatim instead of inventing wording for it.
        "allowed_roots_hint": (
            "While your iPhone is connected, tools can only read and write "
            "inside the editor's workdir, ~/Movies, ~/Downloads and "
            "~/Pictures. Set VAI_ALLOWED_ROOTS to add a folder."
        ),
    }


@router.post("/new")
def new_code(request: Request) -> dict:
    """Mint a single-use claim code and the exact QR payload for it."""
    _require_desktop(request)
    _require_lan()
    hosts = pairing.host_candidates()
    if not hosts:
        raise HTTPException(409, {
            "error": "no_lan_address",
            "message": ("This Mac has no local network address right now. "
                        "Connect it to the same Wi-Fi as your iPhone."),
        })
    port = pairing.server_port()
    code = pairing.new_pair_code()
    return {
        "code": code,
        "host": hosts[0],
        "hosts": hosts,
        "port": port,
        "payload": pairing.pair_payload(hosts[0], port, code),
        "expires_in_s": pairing.PAIR_CODE_TTL_S,
    }


@router.post("/claim")
def claim(body: ClaimRequest) -> dict:
    """The phone's half of pairing. Burns the code, returns the only copy of
    the bearer token that will ever exist."""
    _require_lan()
    device = pairing.claim_pair_code(body.code.strip(), body.device_name)
    if device is None:
        # One message for "wrong", "already used" and "expired" on purpose:
        # distinguishing them tells an attacker which guesses were close.
        raise HTTPException(401, {
            "error": "bad_code",
            "message": ("That code is not valid any more. Codes work once and "
                        "expire after ten minutes — show a new one on the Mac."),
        })
    return {**device, "server": pairing.server_info()}


@router.get("/whoami")
def whoami(request: Request) -> dict:
    """Identity plus the facts the phone needs to set expectations.

    `job_workers` is in here for a concrete reason: the background pool is
    shared with the desktop, so one export and one `upscale` will park a
    preview in `queued` at 0%. The companion shows the queue instead of a
    progress bar that has not moved.
    """
    device_id = getattr(request.state, "device_id", "")
    device = pairing.device_by_id(device_id) if device_id else None
    return {
        "device": ({"id": device["id"], "name": device["name"],
                    "created_at": device.get("created_at", 0.0)}
                   if device else None),
        # True for the desktop's own frontend, which is on loopback and
        # therefore never carries a token. The phone always sees a device.
        "loopback": _is_loopback(request),
        "server": pairing.server_info(),
    }


@router.get("/devices")
def devices(request: Request) -> dict:
    _require_desktop(request)
    return {"devices": pairing.list_devices()}


@router.post("/revoke")
def revoke(request: Request, body: RevokeRequest) -> dict:
    """Unpair one phone. Its bearer and any live media token stop working on
    the next request — media tokens are re-checked against the device list."""
    _require_desktop(request)
    removed = pairing.revoke_device(body.device_id)
    if not removed:
        raise HTTPException(404, {"error": "no_such_device",
                                  "message": "That device is not paired."})
    return {"revoked": body.device_id, "devices": pairing.list_devices()}


@router.post("/media_token")
def media_token(request: Request) -> dict:
    """A 60-second credential that rides in `?k=` on media URLs only.

    Native iOS loaders (`AVURLAsset`, the image loader behind the filmstrip)
    do not reliably carry a custom `Authorization` header across redirects and
    byte-range refetches, and there is no simulator here to find out which of
    them do. Rather than guess, the phone uses both paths: headers where it
    controls the request, and this token where the OS owns it.
    """
    device_id = getattr(request.state, "device_id", "")
    if not device_id:
        # Loopback (the desktop's own UI) needs no token — it is already
        # trusted by address — so this is a client bug worth naming.
        raise HTTPException(400, {
            "error": "no_device",
            "message": "Media tokens are only issued to a paired device.",
        })
    return pairing.mint_media_token(device_id)
