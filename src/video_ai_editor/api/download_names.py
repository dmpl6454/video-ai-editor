"""The name an export is SAVED under (QA-100).

Renders are stored as `exports/export_<hash>.<ext>` — an internal name. The
Export dialog's File name is what the user wants on disk, and two paths save
it: the native Save-As box (desktop.py `save_export`) and the browser download
(`GET …/files/exports/<file>?name=…`, whose Content-Disposition decides the
downloaded name — an `<a download>` attribute cannot override it). Both go
through `download_leaf`, so the rule is one: a bare leaf, never a path, with the
source file's extension.
"""
from __future__ import annotations

from pathlib import Path

_BAD = set('<>:"|?*') | {chr(c) for c in range(32)}


def download_leaf(suggested: str | None, filename: str) -> str:
    """`suggested` as a safe leaf carrying `filename`'s extension, else `filename`."""
    if not suggested or not isinstance(suggested, str):
        return filename
    leaf = Path(suggested.replace("\\", "/")).name
    leaf = " ".join("".join(" " if ch in _BAD else ch for ch in leaf).split()).strip(".")[:120].strip()
    if not leaf:
        return filename
    ext = Path(filename).suffix
    if Path(leaf).suffix.lower() != ext.lower():
        leaf = f"{leaf}{ext}"
    return leaf
