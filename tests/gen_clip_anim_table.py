"""Generate the browser's copy of the clip-animation table and its parity
cases (wave E, F1):

* frontend/src/lib/anim/clipAnimTable.json — `clip_animations.table_json()`,
  what `lib/anim/clipAnim.ts` (the engine's geometry, pipDraw, StickerLayer)
  evaluates;
* frontend/src/lib/anim/clipAnimCases.json — every preset's channels, fade
  gain and blur weight at many clip-local times over several windows,
  sampled with `AnimPlan.value` (the numbers the export's expressions give),
  which lib/anim/clipAnim.test.ts asserts clipAnim.ts reproduces.

tests/test_clip_animations.py fails when either checked-in file no longer
matches the Python table.

    .venv/bin/python tests/gen_clip_anim_table.py
"""
from __future__ import annotations

import json
from pathlib import Path

from video_ai_editor.edl import clip_animations as A

DIR = Path(__file__).resolve().parents[1] / "frontend" / "src" / "lib" / "anim"
TABLE = DIR / "clipAnimTable.json"
CASES = DIR / "clipAnimCases.json"
CHANNELS = ("scale", "x", "y", "rotation")


def cases() -> list[dict]:
    out: list[dict] = []
    specs: list[dict] = []
    for i, (pin, pout) in enumerate(zip(A.IN_PRESETS, A.OUT_PRESETS)):
        specs.append({"anim_in": pin.id, "anim_out": pout.id,
                      "anim_dur": (None, 0.3, 1.2)[i % 3], "anim_out_dur": (0.8, None, 0.25)[i % 3]})
    for p in A.COMBO_PRESETS:
        specs.append({"anim_combo": p.id})
    specs.append({"anim_in": "zoom_in", "anim_dur": 3.0})       # capped at 40 %
    for spec in specs:
        for window in (0.4, 2.0, 5.25):
            pl = A.plan(spec.get("anim_in"), spec.get("anim_out"), spec.get("anim_combo"),
                        spec.get("anim_dur"), spec.get("anim_out_dur"), window)
            ts = sorted({round(window * k / 40, 6) for k in range(41)} | {0.0, 1 / 30, 1 / 25})
            samples = []
            for t in ts:
                samples.append([t, *[round(pl.value(ch, t), 9) for ch in CHANNELS],
                                round(A.fade_gain(pl, t), 9), round(A.blur_weight(pl, t), 9)])
            out.append({"spec": spec, "window": window, "d_in": pl.d_in, "d_out": pl.d_out,
                        "peaks": {ch: round(pl.peak(ch), 9) for ch in CHANNELS}, "samples": samples})
    return out


def main() -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    TABLE.write_text(json.dumps(A.table_json(), indent=1) + "\n")
    CASES.write_text(json.dumps({"channels": list(CHANNELS), "cases": cases()}, separators=(",", ":")) + "\n")
    print(f"wrote {TABLE} and {CASES}")


if __name__ == "__main__":
    main()
