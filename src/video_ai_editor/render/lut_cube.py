"""Check a `.cube` LUT the way ffmpeg's `lut3d` filter will read it.

Final QA (0.8.0 LUT import): `/lut_upload` only checked the extension, so a 1D
LUT, a truncated 3D one, or `LUT_3D_SIZE 300` was imported and applied, and
then every preview and export failed with "a clip may have corrupt frames or an
unusual codec". ffmpeg decides at filter init, before it decodes a frame, and
its only words are "3D LUT is empty", "Unexpected EOF" or a bare "Error
initializing filters" — none names the file. So the file is checked here, at
import and again in `apply_lut`, and the render-failure message uses the same
check to name a LUT that went bad after it was applied.

This mirrors libavfilter/vf_lut3d.c `parse_cube` (ffmpeg 8.1):

* lines before the `LUT_3D_SIZE` line are ignored; the keyword must start the
  line (a byte-order mark or indentation hides it: "3D LUT is empty");
* the size is 2..256;
* after it, blank lines, `#` comments, `TITLE…` and `DOMAIN_MIN`/`DOMAIN_MAX`
  lines are skipped; every other line must start with three numbers — any other
  keyword there (`LUT_3D_INPUT_RANGE`, another `DOMAIN_…`) fails the render.

It is deliberately stricter than ffmpeg in three places, each a file ffmpeg
"renders" wrongly rather than refuses: a row must be exactly three finite
numbers (plus an optional `# comment`), the table must hold exactly N^3 rows
(extra rows usually mean a 1D shaper in front of the 3D table, which ffmpeg
reads AS 3D rows), and the size must be plain decimal (ffmpeg's strtol reads
`033` as octal 27).
"""
from __future__ import annotations

import re
from pathlib import Path

#: vf_lut3d.c MAX_LEVEL.
MAX_SIZE = 256
MIN_SIZE = 2

_NUM = r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
_ROW_RE = re.compile(rf"^[ \t]*{_NUM}[ \t]+{_NUM}[ \t]+{_NUM}[ \t]*(?:#.*)?$")
_SIZE_RE = re.compile(r"^LUT_3D_SIZE\s+(\S+)")
_SKIPPED_IN_TABLE = ("TITLE", "DOMAIN_MIN ", "DOMAIN_MAX ")


class InvalidLut(ValueError):
    """A `.cube` ffmpeg cannot render as a 3D LUT; str() is user-facing."""


def _is_blank_or_comment(line: str) -> bool:
    s = line.lstrip()
    return not s or s.startswith("#")


def _parse_size(token: str, name: str) -> int:
    if not re.fullmatch(r"[1-9]\d*", token) or not MIN_SIZE <= int(token) <= MAX_SIZE:
        raise InvalidLut(
            f"{name} has LUT_3D_SIZE {token}, which the app can't use — it must be a "
            f"whole number from {MIN_SIZE} to {MAX_SIZE}.")
    return int(token)


def _check_table(lines: list[str], start: int, size: int, name: str, saw_1d: bool) -> None:
    need = size ** 3
    rows = 0
    for lineno, line in enumerate(lines[start:], start=start + 1):
        if _is_blank_or_comment(line) or line.startswith(_SKIPPED_IN_TABLE):
            continue
        if rows == need:
            if _ROW_RE.match(line):
                shaper = " (a 1D shaper in front of the 3D table isn't supported)" if saw_1d else ""
                raise InvalidLut(
                    f"{name} has more colour rows than LUT_3D_SIZE {size} allows "
                    f"({need}){shaper} — export it again as a plain 3D .cube.")
            continue            # ffmpeg stops reading after the table
        if not _ROW_RE.match(line):
            raise InvalidLut(
                f"{name} can't be read — line {lineno} should be three numbers "
                f"(red green blue) but reads {line.strip()[:40]!r}.")
        rows += 1
    if rows < need:
        raise InvalidLut(
            f"{name} is incomplete — LUT_3D_SIZE {size} needs {need} colour rows "
            f"but the file has {rows}.")


def validate_cube(path: str | Path, *, display_name: str | None = None) -> None:
    """Raise `InvalidLut` unless ffmpeg's lut3d will render `path` as intended."""
    p = Path(path)
    name = display_name or p.name
    try:
        # latin-1 never fails and keeps bytes as bytes, like the C parser: a
        # UTF-8 byte-order mark stays in front of the first keyword.
        text = p.read_bytes().decode("latin-1")
    except OSError:
        raise InvalidLut(f"{name} can't be read.") from None
    lines = text.splitlines()
    saw_1d = False
    for i, line in enumerate(lines):
        if line.startswith("LUT_1D_SIZE"):
            saw_1d = True
        m = _SIZE_RE.match(line)
        if m:
            _check_table(lines, i + 1, _parse_size(m.group(1), name), name, saw_1d)
            return
    if saw_1d:
        raise InvalidLut(f"{name} is a 1D LUT — import a 3D .cube LUT instead "
                         f"(one with a LUT_3D_SIZE line).")
    raise InvalidLut(f"{name} isn't a 3D .cube LUT — it has no LUT_3D_SIZE line "
                     f"at the start of a line.")


def cube_problem(path: str | Path, *, display_name: str | None = None) -> str | None:
    """The user-facing reason `path` can't be rendered, or None when it can."""
    try:
        validate_cube(path, display_name=display_name)
    except InvalidLut as e:
        return str(e)
    return None
