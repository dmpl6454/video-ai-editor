"""Generate the procedural music beds under `presets/music/` (spec §2.8).

    uv run python scripts/gen_music_beds.py            # presets/music/
    uv run python scripts/gen_music_beds.py --out DIR  # elsewhere (tests, CI)
    uv run python scripts/gen_music_beds.py --force    # regenerate

WHY GENERATED, NOT COMMITTED
----------------------------
Four 180-second 48 kHz stereo WAVs are ~140 MB of binary the repository would
carry forever for content that is a deterministic function of forty lines of
lavfi expression. `build_app.sh` runs this before `--add-data presets:presets`,
the test fixtures run it into a temp directory, and `.gitignore` keeps the
output out of commits. The beds are procedural (a kick on an exact grid, an
off-beat click, a slow pad) so the `.json` sidecar can state the beat grid
outright — the `beat_sync` recipe needs no beat detector for a preset bed and
the benchmark can check splits against a known truth, not against another
model's guess.

The synthesis lives in `video_ai_editor.agent.prompt.presets.generate_music_beds`
so a fixture can import it; this file is only the command line.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from video_ai_editor.agent.prompt import presets  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--out", type=Path, default=None, help="output directory (default presets/music)")
    ap.add_argument("--duration", type=float, default=presets.BED_DURATION_S,
                    help=f"seconds per bed (default {presets.BED_DURATION_S:g})")
    ap.add_argument("--force", action="store_true", help="regenerate beds that already exist")
    ns = ap.parse_args(argv)
    beds = presets.generate_music_beds(ns.out, duration_s=ns.duration, force=ns.force)
    for b in beds:
        print(f"{b.path}  {b.bpm:g} BPM  {b.mood}  {b.duration_s:g}s")
    return 0 if beds else 1


if __name__ == "__main__":
    raise SystemExit(main())
