"""The Anthropic API key, kept in the macOS Keychain (QA-063-SETTINGS).

WHY THE KEYCHAIN, AND ONLY THE KEYCHAIN
---------------------------------------
A packaged .app has no way for a person to edit `.env` (the repo copy is
inside a read-only bundle; the user-level one lives in a folder they never
see), so before Settings the Claude rung was reachable only by developers.
The key is a credential that bills someone's account, so it is stored in
exactly one place — the login Keychain, under the service
`SERVICE` ("Video AI Editor") and account `ACCOUNT` — and NEVER in a file the
app writes (settings.json, meta.json, the EDL, chat history, logs), never in a
response body (only a masked suffix leaves `status()`), and never on a
command line: `security add-generic-password -w <key>` would put it in the
argv every process on the Mac can read with `ps`, so the write goes through
`security -i` on STDIN instead.

The one legal spelling of an accepted key is `KEY_RE` (`sk-ant-` + base64url
characters). Enforcing it BEFORE the write does two jobs: a pasted key with a
stray quote or newline is refused with a sentence instead of saved broken,
and the stdin command line `security -i` parses can never be broken out of
(no quote, backslash, space or newline can reach it).

HOW THE BACKEND READS IT
------------------------
`config.anthropic_api_key()` is the single resolver: a non-empty
`ANTHROPIC_API_KEY` (shell or .env) wins; a PRESENT-BUT-EMPTY one is an
explicit "no Claude" and the Keychain is not consulted (that is how the test
gate and the benchmark harness keep an owner's saved key out of their runs);
otherwise `anthropic_key()` below. Reads are memoised for `CACHE_TTL_S` so a
chat turn does not spawn `security` every time; `write()`/`delete()` drop the
memo, which is what makes a key saved in Settings take effect without a
restart.

Only macOS has a Keychain. Elsewhere `available()` is False and Settings says
to set ANTHROPIC_API_KEY instead — an honest limitation, not a silent no-op.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path

from . import platformutil as _pu

#: The Keychain item the app owns. Tests point this at a throwaway service
#: (tests/conftest.py) so no test can read or write the owner's real item, and
#: a QA server started with VAI_KEYCHAIN_SERVICE does the same for a live run.
SERVICE = os.environ.get("VAI_KEYCHAIN_SERVICE") or "Video AI Editor"
ACCOUNT = "anthropic_api_key"

#: An Anthropic key: `sk-ant-` then URL-safe base64 characters. Real keys are
#: ~108 characters; the bounds only reject what cannot be a key.
KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_-]{20,400}")

SECURITY_BIN = "/usr/bin/security"
_TIMEOUT_S = 10.0
#: `security` exits 44 (errSecItemNotFound) when there is no such item.
_NOT_FOUND = 44
CACHE_TTL_S = 60.0

_LOCK = threading.Lock()
#: service -> (monotonic time read, value or None). Process memory only.
_CACHE: dict[str, tuple[float, str | None]] = {}


class KeychainError(RuntimeError):
    """A Keychain operation failed. The message is written for the person at
    the Mac and never contains the key."""


def available() -> bool:
    """Can this machine store the key? (macOS with the `security` tool.)"""
    return _pu.IS_MAC and Path(SECURITY_BIN).exists()


def valid_key(value: object) -> bool:
    return isinstance(value, str) and KEY_RE.fullmatch(value) is not None


def mask(value: str | None) -> str | None:
    """The only form of a key that may leave this process: its last four
    characters behind a mask, e.g. `sk-ant-…WxYz`."""
    if not value:
        return None
    return f"sk-ant-…{value[-4:]}" if len(value) > 8 else "••••"


def _account_home() -> str:
    """The user's real home directory, whatever $HOME says. The login
    Keychain belongs to the ACCOUNT: `security` finds it through $HOME, and
    with $HOME pointed elsewhere (a sandboxed test server, a launcher that
    isolates app data) it finds no keychain and blocks on a "create a
    keychain?" prompt until the timeout."""
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError, OSError):
        return os.path.expanduser("~")


def _run(args: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess:
    # Output is captured and never logged: `find-generic-password -w` prints
    # the secret itself, and `delete-generic-password` prints the item's
    # attributes.
    return subprocess.run(
        [SECURITY_BIN, *args], input=stdin, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=_TIMEOUT_S,
        env={**os.environ, "HOME": _account_home()}, **_pu.SUBPROCESS_FLAGS,
    )


def read(*, service: str | None = None, account: str = ACCOUNT) -> str | None:
    """The stored value, or None when there is none (or no Keychain)."""
    if not available():
        return None
    svc = service or SERVICE
    try:
        proc = _run(["find-generic-password", "-s", svc, "-a", account, "-w"])
    except (OSError, subprocess.SubprocessError):
        _warn("read", None)
        return None
    if proc.returncode == _NOT_FOUND:
        return None
    if proc.returncode != 0:
        _warn("read", proc.returncode)
        return None
    value = proc.stdout.strip()
    return value or None


def write(value: str, *, service: str | None = None, account: str = ACCOUNT) -> None:
    """Store (or replace) the key. KeychainError on any failure."""
    if not valid_key(value):
        raise KeychainError("That doesn't look like an Anthropic API key. It starts with "
                            "“sk-ant-” — copy it again from console.anthropic.com.")
    if not available():
        raise KeychainError("Saving a key needs the macOS Keychain, which this computer "
                            "doesn't have. Set ANTHROPIC_API_KEY before starting the app instead.")
    svc = service or SERVICE
    if '"' in svc or "\n" in svc:        # our own constant; guard the stdin grammar anyway
        raise KeychainError("invalid Keychain service name")
    # KEY_RE admits no quote, backslash, whitespace or newline, so this one
    # line is exactly one `security` command whatever the key is.
    line = (f'add-generic-password -U -s "{svc}" -a "{account}" -l "{svc}" '
            f'-j "Anthropic API key for Claude in {svc}" -w "{value}"\n')
    try:
        proc = _run(["-i"], stdin=line)
    except (OSError, subprocess.SubprocessError):
        raise KeychainError("The Keychain didn't answer. Try again, or unlock your "
                            "login keychain in Keychain Access.") from None
    # `security -i` exits with the last command's status; the read-back below
    # is the proof either way.
    if proc.returncode != 0:
        raise KeychainError("The key couldn't be saved to your Keychain. If it is locked, "
                            "unlock it in Keychain Access and try again.")
    if read(service=svc, account=account) != value:
        raise KeychainError("The key was not saved to your Keychain. Try again.")
    _forget(svc)


def delete(*, service: str | None = None, account: str = ACCOUNT) -> bool:
    """Remove the item. True if one was removed, False if there was none."""
    if not available():
        return False
    svc = service or SERVICE
    try:
        proc = _run(["delete-generic-password", "-s", svc, "-a", account])
    except (OSError, subprocess.SubprocessError):
        raise KeychainError("The Keychain didn't answer. Try again.") from None
    _forget(svc)
    if proc.returncode == _NOT_FOUND:
        return False
    if proc.returncode != 0:
        raise KeychainError("The key couldn't be removed from your Keychain. "
                            "Remove “Video AI Editor” in Keychain Access instead.")
    return True


def anthropic_key() -> str:
    """The stored Anthropic key ('' if none), memoised for CACHE_TTL_S."""
    svc = SERVICE
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(svc)
        if hit is not None and now - hit[0] < CACHE_TTL_S:
            return hit[1] or ""
    value = read(service=svc)
    with _LOCK:
        _CACHE[svc] = (now, value)
    return value or ""


def _forget(service: str) -> None:
    with _LOCK:
        _CACHE.pop(service, None)


def _warn(action: str, code: int | None) -> None:
    # The return code only — never stdout/stderr (they can hold the secret).
    import logging
    logging.getLogger("video_ai_editor").warning(
        "keychain %s failed (security exit %s)", action, code)
