"""Cross-platform helpers. The ONE place OS differences live.

macOS and Windows both import from here; every OS-conditional decision in the
codebase should route through a function in this module rather than an inline
`sys.platform` check, so platform behavior stays auditable and testable.
"""
from __future__ import annotations
import sys

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


def exe_name(name: str) -> str:
    """Append `.exe` on Windows for a bare binary name (idempotent)."""
    if IS_WINDOWS and not name.lower().endswith(".exe"):
        return f"{name}.exe"
    return name
