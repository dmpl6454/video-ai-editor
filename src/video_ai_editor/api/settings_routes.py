"""`/api/settings/anthropic-key` — add, test and remove the Claude key from
inside the app (QA-063-SETTINGS). Storage is keychain.py; this file is the
HTTP boundary, and it is written around one rule: THE KEY NEVER COMES BACK.

  * No response body carries it — `status()` returns `keychain.mask()` (the
    last four characters) and a source; errors are fixed sentences.
  * The body is parsed by hand, never by a pydantic model: FastAPI's 422
    echoes the offending `input`, which for this route would be the key
    (api/hardening._safe_input truncates it, it does not remove it).
  * Nothing here logs the body or an exception string. The request log line
    (hardening) carries method, path and status only.
  * `Cache-Control: no-store` on every answer.

Posture, like the model-download routes in prompt_routes: LOOPBACK ONLY (a
paired phone must not be able to set, remove or spend the Mac's key), and a
write must be `application/json` — a cross-origin page can send text/plain
or a form with no CORS preflight, but not JSON. The Host allowlist and the
cross-site refusal (api/auth.PairAuthMiddleware, SEC-REBIND) already run in
front of every route; these are the route's own layers on top.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import config, keychain
from .auth import _is_loopback, same_origin

router = APIRouter(tags=["settings"])

ROUTE = "/api/settings/anthropic-key"
#: A key is ~110 bytes; anything past this is not a key-shaped request.
_MAX_BODY_BYTES = 4096
_NO_STORE = {"Cache-Control": "no-store"}
_TEST_TIMEOUT_S = 15.0


#: One rule, shared with the middleware that applies it to every write
#: (api/auth.same_origin, SEC-SAME-ORIGIN). The settings routes also apply
#: it to their GET, because that answer describes the key.
_same_origin = same_origin


def _guard(request: Request, *, write: bool) -> None:
    if not _is_loopback(request):
        raise HTTPException(403, "desktop_only: the API key can only be managed on the Mac itself.")
    if not _same_origin(request):
        raise HTTPException(403, "desktop_only: the API key can only be managed from the app's own window.")
    if write:
        ctype = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            raise HTTPException(415, "Send the request as application/json.")


def status() -> dict[str, Any]:
    """What Settings shows. Never the key: a mask and where it comes from."""
    source = config.anthropic_key_source()
    key = config.anthropic_api_key() if source in ("env", "keychain") else ""
    return {
        "configured": bool(key),
        "source": source,
        "masked": keychain.mask(key),
        "keychain_available": keychain.available(),
        # A key from the environment is managed where it was set, not here.
        "can_edit": keychain.available() and source != "env",
    }


def _answer(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers=_NO_STORE)


async def _key_from_body(request: Request) -> str:
    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        raise HTTPException(413, "That is far too long to be an API key.")
    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(400, "Send the key as JSON: {\"key\": \"sk-ant-…\"}.") from None
    key = body.get("key") if isinstance(body, dict) else None
    if not isinstance(key, str):
        raise HTTPException(400, "Paste your Anthropic API key.")
    key = key.strip()
    if not keychain.valid_key(key):
        raise HTTPException(400, "That doesn't look like an Anthropic API key. It starts with "
                                 "“sk-ant-” — copy it again from console.anthropic.com.")
    return key


@router.get(ROUTE)
def get_key_status(request: Request):
    _guard(request, write=False)
    return _answer(status())


@router.post(ROUTE)
async def save_key(request: Request):
    _guard(request, write=True)
    if config.anthropic_key_source() == "env":
        raise HTTPException(409, "This app was started with ANTHROPIC_API_KEY set, and that key is "
                                 "used instead. Change it where it was set.")
    key = await _key_from_body(request)
    try:
        await asyncio.to_thread(keychain.write, key)
    except keychain.KeychainError as e:
        raise HTTPException(503, str(e)) from None
    finally:
        del key
    return _answer(status())


@router.delete(ROUTE)
async def remove_key(request: Request):
    _guard(request, write=False)
    if config.anthropic_key_source() == "env":
        raise HTTPException(409, "This key comes from ANTHROPIC_API_KEY, not from Settings. "
                                 "Remove it where it was set.")
    try:
        removed = await asyncio.to_thread(keychain.delete)
    except keychain.KeychainError as e:
        raise HTTPException(503, str(e)) from None
    return _answer({**status(), "removed": removed})


def _probe_anthropic(key: str) -> None:
    """One cheap authenticated call (list one model). Raises on failure.
    Replaced by tests: the suite never talks to api.anthropic.com."""
    from anthropic import Anthropic
    Anthropic(api_key=key, timeout=_TEST_TIMEOUT_S, max_retries=0).models.list(limit=1)


#: Indirection so tests can swap the network call out.
PROBE: Callable[[str], None] = _probe_anthropic


def _probe_message(e: Exception) -> tuple[bool, str]:
    """(ok, sentence) for a probe failure. Never `str(e)`: an SDK error body
    is not ours to show, and nothing about the key may be echoed."""
    code = getattr(e, "status_code", None)
    name = type(e).__name__
    if code == 401 or name == "AuthenticationError":
        return False, ("Anthropic rejected this key. Check it was copied completely, "
                       "or create a new one at console.anthropic.com.")
    if code == 403 or name == "PermissionDeniedError":
        return False, "This key isn't allowed to use the Anthropic API."
    if code == 429 or name == "RateLimitError":
        return True, "The key works. Anthropic is rate-limiting it right now, so Claude may be slow."
    if name in ("APIConnectionError", "APITimeoutError") or isinstance(e, (OSError, TimeoutError)):
        return False, "Couldn't reach Anthropic. Check your internet connection and try again."
    return False, ("Anthropic couldn't check the key right now"
                   + (f" (error {code})." if isinstance(code, int) else ".") + " Try again later.")


@router.post(ROUTE + "/test")
async def test_key(request: Request):
    """Check the key Claude would use right now (env or Keychain)."""
    _guard(request, write=True)
    key = config.anthropic_api_key()
    if not key:
        return _answer({"ok": False, "message": "No key is set up yet."})
    try:
        await asyncio.to_thread(PROBE, key)
    except Exception as e:  # noqa: BLE001 — every failure becomes a sentence
        ok, message = _probe_message(e)
        return _answer({"ok": ok, "message": message})
    finally:
        del key
    return _answer({"ok": True, "message": "The key works. Claude is ready."})


__all__ = ["router", "status", "PROBE", "ROUTE"]
