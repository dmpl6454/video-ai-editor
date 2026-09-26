"""The bundled fonts' licence file: write it, and check a font folder or a
built .app carries it.

    uv run python scripts/font_licences.py --write           # fonts/ + frontend/public/fonts/
    uv run python scripts/font_licences.py --check-app APP   # exit 1 if the bundle lacks it

WHY THIS EXISTS
---------------
Every typeface the app ships (the renderer's `fonts/`, mirrored for the
browser preview in `frontend/public/fonts/`) is under the SIL Open Font
License 1.1. OFL §2 lets the fonts be bundled with software only if "each copy
contains the above copyright notice and this license". 0.7.2 shipped the TTFs
with neither. `OFL.txt` now sits next to the fonts in both folders, so it
rides the existing `--add-data fonts:fonts` and `frontend/dist` copies into
the .app, and Help lists it (frontend/src/lib/fontLicences.ts parses it).

The copyright lines are READ FROM EACH FONT (OpenType `name` table, ID 0), not
typed: a font swapped for a new version updates its notice on the next
`--write`, and `check_dir` fails when a font has no matching notice. Stdlib
only (a 40-line `name` reader), because build_app.sh runs the check and
fontTools is not a declared dependency.

The licence body is the OFL-1.1 text exactly as google/fonts distributes it
(ofl/anton/OFL.txt, fetched 2026-09-26); `OFL_BODY_SHA256` pins it so an edit
to the legal text is a test failure, not a silent change.
"""
from __future__ import annotations

import argparse
import hashlib
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FONT_DIRS = (ROOT / "fonts", ROOT / "frontend" / "public" / "fonts")
LICENCE_NAME = "OFL.txt"
FONT_SUFFIXES = (".ttf", ".otf")
# Where build_app.sh's --add-data puts each folder inside the .app.
APP_FONT_DIRS = ("Contents/Resources/fonts", "Contents/Resources/frontend/dist/fonts")

OFL_INTRO = (
    "This Font Software is licensed under the SIL Open Font License, Version 1.1.\n"
    "This license is copied below, and is also available with a FAQ at:\n"
    "https://openfontlicense.org\n"
)

OFL_BODY = """-----------------------------------------------------------
SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007
-----------------------------------------------------------

PREAMBLE
The goals of the Open Font License (OFL) are to stimulate worldwide
development of collaborative font projects, to support the font creation
efforts of academic and linguistic communities, and to provide a free and
open framework in which fonts may be shared and improved in partnership
with others.

The OFL allows the licensed fonts to be used, studied, modified and
redistributed freely as long as they are not sold by themselves. The
fonts, including any derivative works, can be bundled, embedded,
redistributed and/or sold with any software provided that any reserved
names are not used by derivative works. The fonts and derivatives,
however, cannot be released under any other type of license. The
requirement for fonts to remain under this license does not apply
to any document created using the fonts or their derivatives.

DEFINITIONS
"Font Software" refers to the set of files released by the Copyright
Holder(s) under this license and clearly marked as such. This may
include source files, build scripts and documentation.

"Reserved Font Name" refers to any names specified as such after the
copyright statement(s).

"Original Version" refers to the collection of Font Software components as
distributed by the Copyright Holder(s).

"Modified Version" refers to any derivative made by adding to, deleting,
or substituting -- in part or in whole -- any of the components of the
Original Version, by changing formats or by porting the Font Software to a
new environment.

"Author" refers to any designer, engineer, programmer, technical
writer or other person who contributed to the Font Software.

PERMISSION & CONDITIONS
Permission is hereby granted, free of charge, to any person obtaining
a copy of the Font Software, to use, study, copy, merge, embed, modify,
redistribute, and sell modified and unmodified copies of the Font
Software, subject to the following conditions:

1) Neither the Font Software nor any of its individual components,
in Original or Modified Versions, may be sold by itself.

2) Original or Modified Versions of the Font Software may be bundled,
redistributed and/or sold with any software, provided that each copy
contains the above copyright notice and this license. These can be
included either as stand-alone text files, human-readable headers or
in the appropriate machine-readable metadata fields within text or
binary files as long as those fields can be easily viewed by the user.

3) No Modified Version of the Font Software may use the Reserved Font
Name(s) unless explicit written permission is granted by the corresponding
Copyright Holder. This restriction only applies to the primary font name as
presented to the users.

4) The name(s) of the Copyright Holder(s) or the Author(s) of the Font
Software shall not be used to promote, endorse or advertise any
Modified Version, except to acknowledge the contribution(s) of the
Copyright Holder(s) and the Author(s) or with their explicit written
permission.

5) The Font Software, modified or unmodified, in part or in whole,
must be distributed entirely under this license, and must not be
distributed under any other license. The requirement for fonts to
remain under this license does not apply to any document created
using the Font Software.

TERMINATION
This license becomes null and void if any of the above conditions are
not met.

DISCLAIMER
THE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT
OF COPYRIGHT, PATENT, TRADEMARK, OR OTHER RIGHT. IN NO EVENT SHALL THE
COPYRIGHT HOLDER BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
INCLUDING ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL
DAMAGES, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM
OTHER DEALINGS IN THE FONT SOFTWARE.
"""
# The upstream text has one trailing space (after "embedded,"); editors strip
# trailing whitespace from source, so it is restored here, not typed above.
OFL_BODY = OFL_BODY.replace("can be bundled, embedded,\n", "can be bundled, embedded, \n")
OFL_BODY_SHA256 = hashlib.sha256(OFL_BODY.encode("utf-8")).hexdigest()

HEADER = """Fonts bundled with Video AI Editor
==================================

The typefaces below ship inside this app (the export renderer and the preview
both draw with them). Each is licensed under the SIL Open Font License,
Version 1.1 (OFL-1.1). Every copyright notice is the font file's own
(its OpenType name table); the licence text follows the list.
"""


def name_record(path: Path, name_id: int) -> str | None:
    """One English string from a TTF/OTF `name` table (platform 3 or 1)."""
    data = path.read_bytes()
    num_tables = struct.unpack(">H", data[4:6])[0]
    for i in range(num_tables):
        tag, _cs, off, _ln = struct.unpack(">4sIII", data[12 + 16 * i: 28 + 16 * i])
        if tag != b"name":
            continue
        _fmt, count, str_off = struct.unpack(">HHH", data[off: off + 6])
        best: str | None = None
        for r in range(count):
            pid, eid, lid, nid, ln, so = struct.unpack(">HHHHHH", data[off + 6 + 12 * r: off + 18 + 12 * r])
            if nid != name_id:
                continue
            raw = data[off + str_off + so: off + str_off + so + ln]
            if pid == 3 and lid == 0x409:
                return raw.decode("utf-16-be").strip()
            if pid == 0 or pid == 3:
                best = best or raw.decode("utf-16-be").strip()
            elif pid == 1 and eid == 0 and lid == 0:
                best = best or raw.decode("mac_roman").strip()
        return best
    return None


def font_files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in FONT_SUFFIXES)


def entries(folders=FONT_DIRS) -> list[tuple[str, list[str], str]]:
    """(family, [file names], copyright) per family, over every font folder."""
    fam: dict[tuple[str, str], set[str]] = {}
    for d in folders:
        for f in font_files(d):
            family = name_record(f, 16) or name_record(f, 1) or f.stem
            fam.setdefault((family, name_record(f, 0) or ""), set()).add(f.name)
    return sorted(((k[0], sorted(v), k[1]) for k, v in fam.items()), key=lambda e: e[0].lower())


def render(folders=FONT_DIRS) -> str:
    blocks = [f"{family} ({', '.join(files)})\n{cr}\n" for family, files, cr in entries(folders)]
    return HEADER + "\n" + "\n".join(blocks) + "\n" + OFL_INTRO + "\n\n" + OFL_BODY


def check_dir(folder: Path) -> list[str]:
    """Problems with `folder/OFL.txt` against the fonts actually in `folder`."""
    lic = folder / LICENCE_NAME
    if not lic.is_file():
        return [f"{lic}: missing"]
    text = lic.read_text(encoding="utf-8")
    problems = []
    body_at = text.find(OFL_BODY.splitlines()[0])
    if body_at < 0 or hashlib.sha256(text[body_at:].encode("utf-8")).hexdigest() != OFL_BODY_SHA256:
        problems.append(f"{lic}: the OFL-1.1 text is missing or altered")
    for f in font_files(folder):
        cr = name_record(f, 0)
        if not cr or cr not in text:
            problems.append(f"{lic}: no copyright notice for {f.name} ({cr!r})")
        if f.name not in text:
            problems.append(f"{lic}: {f.name} is not listed")
    return problems


def check_app(app: Path) -> list[str]:
    problems = []
    for rel in APP_FONT_DIRS:
        d = app / rel
        if not d.is_dir():
            problems.append(f"{d}: font folder missing from the bundle")
            continue
        problems += check_dir(d)
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true", help="write OFL.txt into fonts/ and frontend/public/fonts/")
    g.add_argument("--check", action="store_true", help="check the repo's font folders")
    g.add_argument("--check-app", type=Path, metavar="APP", help="check a built .app bundle")
    a = ap.parse_args(argv)
    if a.write:
        text = render()
        for d in FONT_DIRS:
            (d / LICENCE_NAME).write_text(text, encoding="utf-8")
            print(f"wrote {d / LICENCE_NAME}")
        return 0
    problems = check_app(a.check_app) if a.check_app else [p for d in FONT_DIRS for p in check_dir(d)]
    for p in problems:
        print(f"[font licences] {p}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
