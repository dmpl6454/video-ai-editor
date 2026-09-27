"""Regenerate tests/goldens/keyframe_matrix.json (see keyframe_matrix_lib).

    .venv/bin/python tests/gen_keyframe_matrix.py [--work DIR]

Every render of the matrix goes through the REAL compositor (the export's
single pass) and every output frame of every clip is measured.
frontend/src/lib/preview/render/keyframeMatrix.test.ts holds lib/overlay.ts
`sampleKF` and geometry.ts to it.
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

import keyframe_matrix_lib as lib  # noqa: E402


def document(results: list[tuple[lib.Render, object, list[dict]]]) -> dict:
    return {
        "version": lib.VERSION, "canvas": [lib.W, lib.H], "fps": lib.FPS, "grey": lib.GREY,
        "source": {"key": "mx", "w": lib.SRC_W, "h": lib.SRC_H, "frames": lib.SRC_FRAMES,
                   "info": {"rate": [lib.FPS, 1], "tb": [1, 15360], "frames": lib.SRC_FRAMES,
                            "start_ticks": 0, "w": lib.SRC_W, "h": lib.SRC_H}},
        "markers": {n: [u, v] for n, (u, v, _m, _c) in lib.MARKERS.items()},
        "renders": [{"name": r.name, "group": r.group, "edl": lib.edl_json(edl), "rows": rows}
                    for r, edl, rows in results],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default="")
    args = ap.parse_args()
    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="kfmatrix_"))
    src = str(lib.make_source(work / "src" / "mx.mp4"))
    results = []
    for r in lib.renders():
        t0 = time.time()
        edl, rows = lib.render_and_measure(r, src, work / "sessions")
        results.append((r, edl, rows))
        print(f"{r.name}: {len(rows)} frames in {time.time() - t0:.1f}s", flush=True)
    lib.GOLDEN.write_text(json.dumps(document(results), separators=(",", ":")) + "\n")
    print(f"wrote {lib.GOLDEN}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
