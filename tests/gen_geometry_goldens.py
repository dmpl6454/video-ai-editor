"""Regenerate tests/goldens/geometry_cases.json (see geometry_golden_lib).

    .venv/bin/python tests/gen_geometry_goldens.py [--only NAME] [--work DIR]

Every case is rendered through the REAL compositor (`_render`, the export's
single pass) and measured frame by frame.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

import geometry_golden_lib as lib  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--work", default="")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--reuse", action="store_true", help="re-measure existing renders in --work")
    args = ap.parse_args()
    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="geogold_"))
    paths, infos = lib.ensure_sources(work)
    old = {c["name"]: c for c in lib.load()["cases"]} if lib.GOLDEN.exists() else {}
    records = []
    for case in lib.cases():
        if args.only and args.only not in case.name:
            if case.name in old:
                records.append(old[case.name])
            continue
        t0 = time.time()
        measured = lib.render_case(case, paths, work / "sessions", reuse=args.reuse)
        records.append(lib.case_record(case, infos, measured))
        print(f"{case.name}: {len(measured)} frames in {time.time() - t0:.1f}s", flush=True)
    if not args.no_write:
        lib.GOLDEN.write_text(json.dumps(lib.document(records), indent=1) + "\n")
        print(f"wrote {lib.GOLDEN}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
