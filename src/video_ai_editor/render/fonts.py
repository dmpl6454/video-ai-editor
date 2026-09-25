"""Bundled-font names: the ONE rule for turning a font argument into a file.

A `font` value everywhere in the EDL (`TextStyle.font`, `CaptionLook.font`,
`BrandKit.font`) is a bundled font NAME — a file stem inside
`config.FONTS_DIR` such as ``Anton-Regular`` (``Anton-Regular.ttf`` is
accepted too). It is never a caller path. Before this module the renderer did
``FONTS_DIR / name``, so ``'/etc/hosts'`` (pathlib drops the left side of a
join with an absolute path) or ``'../../x'`` escaped the directory, reached
``ImageFont.truetype`` and made every later export 500; the tool handlers
checked nothing, or checked ``(FONTS_DIR / name).exists()``, which has the
same hole.

`resolve_font` only ever looks at the leaf name, resolves it, and requires
the result to sit inside FONTS_DIR. `check_font_arg` is the tool-boundary
twin: it raises ValueError (→ 400) for anything that is not a bundled font.
"""
from __future__ import annotations

from pathlib import Path

from ..config import FONTS_DIR

_FONT_SUFFIXES = (".ttf", ".otf")


def bundled_fonts() -> dict[str, Path]:
    """stem -> file for every bundled font."""
    out: dict[str, Path] = {}
    try:
        for p in sorted(FONTS_DIR.iterdir()):
            if p.suffix.lower() in _FONT_SUFFIXES and p.is_file():
                out.setdefault(p.stem, p)
    except OSError:
        pass
    return out


def _is_plain_name(name: str) -> bool:
    return bool(name) and "/" not in name and "\\" not in name and "\x00" not in name \
        and not name.startswith(".")


def resolve_font(name: object) -> Path | None:
    """The bundled font file `name` names, or None. Never leaves FONTS_DIR."""
    if not isinstance(name, str):
        return None
    n = name.strip()
    if not _is_plain_name(n):
        return None
    stem = n
    for suf in _FONT_SUFFIXES:
        if stem.lower().endswith(suf):
            stem = stem[: -len(suf)]
            break
    p = bundled_fonts().get(stem)
    if p is None:
        return None
    try:
        root = FONTS_DIR.resolve()
        rp = p.resolve()
    except OSError:
        return None
    return rp if rp.is_relative_to(root) else None


def check_font_arg(value: object, what: str = "font") -> str | None:
    """Validate a font argument at a tool boundary. None/"" clears (returns
    None); a bundled name is returned as given (stripped); anything else
    raises ValueError listing what is available."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    p = resolve_font(value)
    if p is None:
        available = ", ".join(sorted(bundled_fonts()))
        raise ValueError(f"{what} {value!r} is not a bundled font. Available: {available}")
    return str(value).strip()
