"""Cross-platform helpers. The ONE place OS differences live.

macOS and Windows both import from here; every OS-conditional decision in the
codebase should route through a function in this module rather than an inline
`sys.platform` check, so platform behavior stays auditable and testable.
"""
from __future__ import annotations
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

# Spread into every subprocess.run/Popen/check_output/check_call as
# `**_pu.SUBPROCESS_FLAGS`. On Windows, a windowed parent (frozen exe built
# with console=False, or pythonw) spawning a console child (ffmpeg/ffprobe/
# whisper-cli/...) pops up a visible terminal window for every task unless the
# call passes creationflags=subprocess.CREATE_NO_WINDOW. On macOS/Linux this is
# an empty dict, so the spread is a no-op and behavior is byte-identical.
#
# NOTE: the dict-spread raises TypeError if a call site ALSO passes its own
# creationflags= kwarg (duplicate keyword). No site does today — a future site
# that needs extra creation flags must drop the spread and OR the flag in
# manually: creationflags=subprocess.CREATE_NO_WINDOW | <extra> (guarded for
# Windows, since CREATE_NO_WINDOW only exists there).
# tests/test_subprocess_no_window.py statically enforces that every subprocess
# call site under src/video_ai_editor carries one of the two forms.
SUBPROCESS_FLAGS: dict = (
    {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}
)


def exe_name(name: str) -> str:
    """Append `.exe` on Windows for a bare binary name (idempotent)."""
    if IS_WINDOWS and not name.lower().endswith(".exe"):
        return f"{name}.exe"
    return name


# Resolved once at import. Bare names are fine when on PATH; exe_name makes the
# Windows form explicit so callers can also feed these to find_binary.
FFMPEG = exe_name("ffmpeg")
FFPROBE = exe_name("ffprobe")


def ffmpeg_filter_path(path: Path | str) -> str:
    """Escape a filesystem path for embedding inside an ffmpeg *filtergraph*
    option value (e.g. `vidstabdetect=result=<here>`, `sendcmd=f=<here>`,
    `movie=filename=<here>`).

    This is NOT the same as passing a path as an ffmpeg `-i` argv element (that
    needs no escaping). Inside a filtergraph, `:` separates filter options and
    `\\` is an escape char, so a raw Windows path like `C:\\Users\\x\\a.trf`
    is mangled by the parser. The robust, empirically-verified form is:
      1. Convert `\\` to `/` — ffmpeg accepts forward slashes on Windows, which
         removes every backslash-as-escape hazard.
      2. Escape each remaining `:` (the drive-letter colon) as `\\\\:` — the
         only escaping that survives ffmpeg's two-pass filtergraph parser
         (single-backslash and single-quoting both fail).
    On POSIX a normal path has no backslashes and no colon, so it passes
    through unchanged (a rare stray colon is still escaped defensively)."""
    s = str(path).replace("\\", "/")
    return s.replace(":", "\\\\:")


def find_binary(name: str, extra_dirs: list[Path]) -> str | None:
    """Locate a native binary cross-platform.

    1. `shutil.which(exe_name(name))` — respects PATH, adds `.exe` on Windows.
    2. Each dir in `extra_dirs` (both `name` and `exe_name(name)`).
    Returns the resolved path string, or None if nowhere found.
    """
    found = shutil.which(exe_name(name))
    if found:
        return found
    for d in extra_dirs:
        for cand in (Path(d) / exe_name(name), Path(d) / name):
            if cand.exists():
                return str(cand)
    return None


def user_data_dir(app_name: str) -> Path:
    """Per-OS writable application data directory.

    Windows: %APPDATA%\\<app_name>            (roaming; falls back to ~/AppData/Roaming)
    macOS:   ~/Library/Application Support/<app_name>
    Other:   ~/.local/share/<app_name>        (XDG)
    """
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / app_name
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / app_name
    return Path.home() / ".local" / "share" / app_name


def user_cache_dir(app_name: str) -> Path:
    """Per-OS cache directory (regenerable data).

    Windows: %LOCALAPPDATA%\\<app_name>\\cache
    macOS:   ~/Library/Caches/<app_name>
    Other:   ~/.cache/<app_name>
    """
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / app_name / "cache"
    if IS_MAC:
        return Path.home() / "Library" / "Caches" / app_name
    return Path.home() / ".cache" / app_name


def read_text_utf8(path: Path | str) -> str:
    return Path(path).read_text(encoding="utf-8")


def write_text_utf8(path: Path | str, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


def replace_with_retry(src: Path | str, dst: Path | str,
                       attempts: int = 10, delay: float = 0.05) -> None:
    """os.replace with retry. On Windows, replacing a file another process has
    open (e.g. a Starlette FileResponse streaming the preview) raises
    PermissionError; a short backoff lets the reader finish. On POSIX this
    almost always succeeds on the first try."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:  # pragma: no cover - Windows-timing path
            last = e
            time.sleep(delay * (i + 1))
    raise last  # type: ignore[misc]


def unlink_with_retry(path: Path | str,
                      attempts: int = 5, delay: float = 0.05) -> None:
    """Path.unlink(missing_ok=True) with the same Windows open-file retry."""
    p = Path(path)
    for i in range(attempts):
        try:
            p.unlink(missing_ok=True)
            return
        except PermissionError:  # pragma: no cover - Windows-timing path
            time.sleep(delay * (i + 1))
    # Best-effort: a leftover cache file is not fatal.


def rmtree_with_retry(path: Path | str,
                      attempts: int = 10, delay: float = 0.1) -> None:
    """shutil.rmtree with retry/backoff for Windows mandatory file locking.

    On Windows, a directory containing a file with any open handle (e.g. a
    Starlette FileResponse still streaming a previews/*.mp4 or exports/*.mp4,
    an in-flight render's *.part.mp4, or a lingering AV/indexer scan) cannot
    be deleted — shutil.rmtree(ignore_errors=False) raises PermissionError/
    OSError partway through, leaving the tree partially deleted. A short
    backoff lets the other handle-holder finish, mirroring
    replace_with_retry/unlink_with_retry above. On POSIX an open file can be
    unlinked while still held open, so rmtree normally succeeds on the first
    try there regardless."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            shutil.rmtree(path, ignore_errors=False)
            return
        except FileNotFoundError:
            # Already gone (e.g. a partial previous rmtree finished the job,
            # or a concurrent delete raced us) — nothing left to remove.
            return
        except (PermissionError, OSError) as e:  # pragma: no cover - Windows-timing path
            last = e
            time.sleep(delay * (i + 1))
    raise last  # type: ignore[misc]
