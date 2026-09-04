"""Let a user supply ANTHROPIC_API_KEY without hand-making a dotfile.

Before this module the ONLY route to a working chat pane in the packaged app
was creating `~/Library/Application Support/Video AI Editor/.env` by hand — a
Finder-*hidden* folder, holding a Finder-*hidden* file, in an app whose whole
premise is that you never leave it. Everything here exists to make
`POST /api/settings/api-key` a complete substitute for that.

Three things are load-bearing:

  - **Where** it writes. `config._load_dotenv()` reads `<repo>/.env` first
    and the per-OS user data dir's `.env` second, and among files the FIRST
    loaded wins. So a dev checkout's own `.env` deliberately keeps beating what
    the UI writes here — a developer's committed-nothing local key is not
    silently replaced by whatever was typed into a running app. In the frozen
    app there is no repo `.env` (the bundle is read-only), so this file is the
    only one, which is exactly the case this exists for.

  - **How** it writes. The file may already hold other keys (HUGGINGFACE_TOKEN,
    WHISPER_MODEL, …) that nothing else can regenerate, so a write MERGES: every
    other line survives byte-for-byte, comments included. It is staged and
    swapped in via `_pu.replace_with_retry`, the same atomic-write idiom the
    mp4/PNG caches use — a torn `.env` would take the user's other settings with
    it. Mode is 0600: this file holds a billable credential.

  - **Rebinding the live process.** `config.ANTHROPIC_API_KEY` is a module-level
    constant, and `agent/loop.py` + `ai/vision.py` did `from ..config import
    ANTHROPIC_API_KEY` at IMPORT time. Setting `os.environ` alone therefore
    changes nothing until a restart — the chat pane would stay dead with the key
    plainly saved, which is the same "the file plainly contains it" failure mode
    the `.env` BOM bug produced. `apply_key_to_process` rebinds the name in
    every already-imported module of this package that holds it as a str.

The key is never returned, never logged and never echoed back — `is_configured()`
answers a bool and that is the whole read surface.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from . import platformutil as _pu

APP_NAME = "Video AI Editor"
ENV_KEY = "ANTHROPIC_API_KEY"

#: SHAPE only. A real validity check is a network round-trip to Anthropic, which
#: would make "save my key" fail on a flaky connection and hand the user a
#: billing/auth error they can do nothing about at that moment. This is the same
#: bar `main._validate_ai_config()` already warns against (an `sk-` prefix), plus
#: a length and a charset — the charset is what stops a pasted newline or a
#: `KEY=value` fragment from corrupting the `.env` we are about to write.
_KEY_RE = re.compile(r"^sk-[A-Za-z0-9_\-]{16,256}$")

KEY_SHAPE_HINT = ("That doesn't look like an Anthropic API key. Keys start with "
                  "'sk-ant-' and are one long line with no spaces — copy it from "
                  "console.anthropic.com → API keys.")


def user_env_path() -> Path:
    """The `.env` this module owns: the per-OS user data dir's, never the repo's.

    Writing the repo `.env` from an HTTP endpoint would let a running app edit
    its own source tree, and in the frozen app that tree is inside a read-only
    bundle anyway.
    """
    return _pu.user_data_dir(APP_NAME) / ".env"


def looks_like_key(key: str) -> bool:
    return bool(_KEY_RE.match(key or ""))


def is_configured() -> bool:
    """Whether the live process has a key. Deliberately reads `os.environ` and
    not `config.ANTHROPIC_API_KEY`: a shell-provided key, a `.env`-provided one
    and one just saved through this module must all read the same, and only the
    environment sees all three."""
    return bool(os.environ.get(ENV_KEY, "").strip())


def merge_env_text(existing: str, key: str, value: str) -> str:
    """Return `existing` with `key=value` set, every other line untouched.

    Matches an assignment the way `config._apply_env_file` parses one
    (`line.split("=", 1)[0].strip()`), so what we rewrite is exactly what the
    loader would have read. Duplicates collapse onto the first occurrence —
    the loader lets a LATER entry win, so leaving a stale second line behind
    would silently override the value we were asked to store.
    """
    out: list[str] = []
    replaced = False
    for line in existing.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped \
                and stripped.split("=", 1)[0].strip() == key:
            if not replaced:
                out.append(f"{key}={value}")
                replaced = True
            continue  # drop any further assignment of the same key
        out.append(line)
    if not replaced:
        # Keep a trailing blank line from turning into a mid-file one.
        while out and not out[-1].strip():
            out.pop()
        out.append(f"{key}={value}")
    return "\n".join(out) + "\n"


def save_key(key: str, *, path: Path | None = None) -> Path:
    """Persist `key` to the user `.env`, preserving every other setting there.

    Staged-then-swapped (`_pu.replace_with_retry`) so a crash mid-write cannot
    leave a half-file, and 0600 on both the stage and the destination so the
    credential is never briefly world-readable.
    """
    path = path or user_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = ""
    if path.exists():
        # BOM-tolerant, matching config's own reader: a `.env` a Windows user
        # made in Notepad starts EF BB BF, and re-emitting that byte in front of
        # the first key is how the key stops loading while the file plainly
        # contains it.
        try:
            existing = _pu.read_text_config(path)
        except OSError:
            existing = ""
    text = merge_env_text(existing, ENV_KEY, key)

    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        # newline="\n" so a Windows write doesn't produce CRLF that the loader
        # would then have to strip out of the value.
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        # O_CREAT's mode is ignored when the stage file already exists (a
        # previous crashed write), and umask can only tighten it — so set it
        # explicitly rather than trusting the open().
        _chmod_600(tmp)
        _pu.replace_with_retry(tmp, path)
    except BaseException:
        _pu.unlink_with_retry(tmp)
        raise
    _chmod_600(path)
    return path


def _chmod_600(path: Path) -> None:
    """Owner-only. A no-op-ish on Windows (only the read-only bit is modelled),
    which is why it must never be allowed to raise there."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def apply_key_to_process(key: str) -> None:
    """Make the running process use `key` with no restart.

    `os.environ` is the source of truth every lazy reader consults, but the two
    modules that bound the constant at import time (`agent/loop.py`,
    `ai/vision.py`) would keep serving the empty string — the chat pane stays
    disabled and the save looks like it did nothing. Rebinding by name across
    this package's already-imported modules covers those without either module
    having to know this one exists.
    """
    os.environ[ENV_KEY] = key
    root = __name__.split(".")[0]
    for name, mod in list(sys.modules.items()):
        if mod is None or not (name == root or name.startswith(root + ".")):
            continue
        # `isinstance(..., str)` and not `hasattr`: only rebind something that
        # is already a string constant of this name, never shadow a function or
        # a class that happens to share it.
        if isinstance(getattr(mod, ENV_KEY, None), str):
            try:
                setattr(mod, ENV_KEY, key)
            except Exception:      # a frozen/proxied module object
                pass
