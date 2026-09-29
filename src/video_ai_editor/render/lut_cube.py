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

Lines are read the way ffmpeg reads them (`_ffmpeg_lines`): `fgets` into a
512-byte buffer, so a line ends at "\n" only and a longer one arrives in
511-byte pieces. Final QA (run 2, round 2): `str.splitlines` also split on
"\r", "\x85" and friends, so a CR-only file (ffmpeg: one line, "3D LUT is
empty") was accepted and then broke every render, and a valid file with a
UTF-8 comment ("Å", "ą", "Ņ", Cyrillic "х" all hold a 0x85 byte) was refused.

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
#: vf_lut3d.c MAX_LINE_SIZE: `fgets(line, 512, f)` reads at most 511 bytes.
_FGETS_MAX = 511
#: C `isspace` in the C locale (ffmpeg's `av_isspace`), not Python's wider set
#: (which also holds "\x85", "\xa0" and "\x1c"-"\x1f").
_C_SPACE = " \t\n\v\f\r"


def _ffmpeg_lines(text: str) -> list[tuple[int, str, bool]]:
    """`(file line number, text, is a continuation)` for every `fgets` read
    of `text`: split on "\n" only, each line in pieces of at most 511 bytes
    (the "\n" counts), one trailing "\r" dropped (a CRLF file). A piece that
    is not the start of its line is a continuation — ffmpeg parses it as a
    line of its own."""
    out: list[tuple[int, str, bool]] = []
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()                                  # the file ends with "\n"
    for lineno, line in enumerate(lines, start=1):
        # `fgets` stops after 511 bytes; the "\n" is one of them when read
        full = line + "\n"
        for k in range(0, len(full), _FGETS_MAX):
            piece = full[k:k + _FGETS_MAX].removesuffix("\n").removesuffix("\r")
            if k and not piece:
                continue                             # just the "\n": fgets reads a blank line
            out.append((lineno, piece, k > 0))
    return out


class InvalidLut(ValueError):
    """A `.cube` ffmpeg cannot render as a 3D LUT; str() is user-facing."""


def _is_blank_or_comment(line: str) -> bool:
    s = line.lstrip(_C_SPACE)
    return not s or s.startswith("#")


def _parse_size(token: str, name: str) -> int:
    if not re.fullmatch(r"[1-9]\d*", token) or not MIN_SIZE <= int(token) <= MAX_SIZE:
        raise InvalidLut(
            f"{name} has LUT_3D_SIZE {token}, which the app can't use — it must be a "
            f"whole number from {MIN_SIZE} to {MAX_SIZE}.")
    return int(token)


def _check_table(lines: list[tuple[int, str, bool]], start: int, size: int, name: str,
                 saw_1d: bool) -> None:
    need = size ** 3
    rows = 0
    for lineno, line, continued in lines[start:]:
        if _is_blank_or_comment(line) or line.startswith(_SKIPPED_IN_TABLE):
            continue
        if continued and rows < need:
            raise InvalidLut(
                f"{name} can't be read — line {lineno} is too long (over {_FGETS_MAX} "
                f"characters). Shorten or remove it.")
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
    if "\n" not in text and "\r" in text:
        # ffmpeg reads the whole file as ONE line: "3D LUT is empty"
        raise InvalidLut(f"{name} uses old Mac (CR-only) line endings, which the app "
                         f"can't read — export it again, or re-save it with standard "
                         f"line endings.")
    lines = _ffmpeg_lines(text)
    saw_1d = False
    for i, (_lineno, line, _continued) in enumerate(lines):
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
