"""Writes frontend/src/lib/preview/render/__fixtures__/canvas_bg_cases.json:
the blur frame size and sigma render/canvas_bg.py uses per canvas and blur
level, which lib/preview/render/canvasBg.ts must reproduce (both suites
assert the file: tests/test_f2_canvas_blend.py, canvasBg.test.ts)."""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "frontend/src/lib/preview/render/__fixtures__/canvas_bg_cases.json"
CANVASES = [(360, 640), (640, 360), (1080, 1920), (1920, 1080), (1366, 768), (1080, 1080), (1080, 1350), (34, 18)]


def cases() -> dict:
    from video_ai_editor.render import canvas_bg as C
    return {"blur": [{"canvas": [w, h], "small": list(C.blur_dims(w, h)),
                      "sigma": [round(C.blur_sigma_small(lv, w, h), 9) for lv in (1, 2, 3, 4)]}
                     for w, h in CANVASES]}


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(cases(), indent=1) + "\n", encoding="utf-8")
    print(OUT)
