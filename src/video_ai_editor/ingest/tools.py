"""Is the video engine's toolchain (ffmpeg + ffprobe) actually installed? — QA-108.

Every import, preview, export and most edits shell out to ffmpeg or ffprobe
(`platformutil.FFMPEG` / `FFPROBE`, bare names resolved on PATH at call time).
`config.py` already widens PATH at import so a double-clicked .app finds a
Homebrew/MacPorts binary (and Windows finds a winget ffmpeg), which means the
honest first-run question is simply "does `shutil.which` find them NOW?" —
the same lookup `subprocess` is about to make.

When the answer was no, nothing said so. `/api/health` reported `ok: true`,
an import was refused with "it may not be a valid video" (blaming the user's
perfectly good file), preview and export came back as bare 500s or "corrupt
frames", and a background job showed `FileNotFoundError: … 'ffmpeg'` verbatim.

This module is the ONE answer. `media_tools_status()` feeds `/api/health`
(which the UI reads to show a sticky "install ffmpeg" banner) and every route
that would shell out maps the failure to a single 503 `ffmpeg_missing` whose
message says exactly what to type. Read live on every call, never cached: a
user who installs ffmpeg while the app is open is fixed on the next request,
and the tests can hide the binaries by pointing PATH at an empty directory.
"""
from __future__ import annotations

import shutil

from .. import platformutil as _pu

#: The binaries the editor cannot work without, by their user-facing names.
REQUIRED_TOOLS: tuple[str, ...] = ("ffmpeg", "ffprobe")

#: The stable error code every route uses for this condition.
ERROR_CODE = "ffmpeg_missing"


def install_command() -> str:
    """The one command that installs both tools on this OS."""
    if _pu.IS_WINDOWS:
        return "winget install Gyan.FFmpeg"
    if _pu.IS_MAC:
        return "brew install ffmpeg"
    return "sudo apt install ffmpeg"


def missing_media_tools() -> list[str]:
    """The required tools `shutil.which` cannot find right now ([] = all present)."""
    return [name for name in REQUIRED_TOOLS if not shutil.which(_pu.exe_name(name))]


def missing_message(missing: list[str] | None = None) -> str:
    """A sentence for a person, not a developer: what is wrong, the exact
    command, and that the app must be reopened afterwards."""
    missing = missing if missing is not None else missing_media_tools()
    what = " and ".join(missing) if missing else "ffmpeg"
    cmd = install_command()
    if _pu.IS_MAC:
        how = (f"Open Terminal, run  {cmd}  (Homebrew is at brew.sh if you don't "
               f"have it yet), then quit and reopen Video AI Editor.")
    elif _pu.IS_WINDOWS:
        how = (f"Open PowerShell, run  {cmd}  then quit and reopen Video AI Editor.")
    else:
        how = f"Install it with  {cmd}  then restart Video AI Editor."
    return (f"Video AI Editor can't find {what}, the video engine it uses to import, "
            f"preview and export. Your files are fine. {how}")


def media_tools_status() -> dict:
    """The `/api/health` field: `{ok, missing, install_command, message}`."""
    missing = missing_media_tools()
    return {
        "ok": not missing,
        "missing": missing,
        "install_command": install_command(),
        "message": missing_message(missing) if missing else None,
    }


class MediaToolsMissing(RuntimeError):
    """Raised (or mapped to) when ffmpeg/ffprobe cannot be found. Its str() is
    already the user-facing sentence, so a job that fails with it shows the
    install instruction rather than a traceback."""

    def __init__(self, missing: list[str] | None = None) -> None:
        self.missing = missing if missing is not None else missing_media_tools()
        super().__init__(missing_message(self.missing))

    def detail(self) -> dict:
        return {"error": ERROR_CODE, "message": str(self), "missing": self.missing,
                "install_command": install_command()}


def require_media_tools() -> None:
    """Raise `MediaToolsMissing` when either binary is absent."""
    missing = missing_media_tools()
    if missing:
        raise MediaToolsMissing(missing)
