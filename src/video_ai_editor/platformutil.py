"""Cross-platform helpers. The ONE place OS differences live.

macOS and Windows both import from here; every OS-conditional decision in the
codebase should route through a function in this module rather than an inline
`sys.platform` check, so platform behavior stays auditable and testable.
"""
from __future__ import annotations
import os
import shutil
import sys
import time
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


def exe_name(name: str) -> str:
    """Append `.exe` on Windows for a bare binary name (idempotent)."""
    if IS_WINDOWS and not name.lower().endswith(".exe"):
        return f"{name}.exe"
    return name


# Resolved once at import. Bare names are fine when on PATH; exe_name makes the
# Windows form explicit so callers can also feed these to find_binary.
FFMPEG = exe_name("ffmpeg")
FFPROBE = exe_name("ffprobe")


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
