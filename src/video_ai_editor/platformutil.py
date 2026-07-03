"""Cross-platform helpers. The ONE place OS differences live.

macOS and Windows both import from here; every OS-conditional decision in the
codebase should route through a function in this module rather than an inline
`sys.platform` check, so platform behavior stays auditable and testable.
"""
from __future__ import annotations
import os
import shutil
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


def exe_name(name: str) -> str:
    """Append `.exe` on Windows for a bare binary name (idempotent)."""
    if IS_WINDOWS and not name.lower().endswith(".exe"):
        return f"{name}.exe"
    return name


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
