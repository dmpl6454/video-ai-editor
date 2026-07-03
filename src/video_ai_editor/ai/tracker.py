"""Object motion tracking via OpenCV.

Given a clip + initial bounding box, runs a frame-by-frame tracker and emits a
JSON track {[t_seconds, cx_canvas, cy_canvas, w_canvas, h_canvas], ...} that
the dispatch layer turns into x/y keyframes on a TextClip or Sticker.

Uses MIL (always present in opencv-contrib-python wheel) by default. Falls
back to Vit when available — it's slower but more robust to scale change.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Literal


def _make_tracker(name: str):
    import cv2
    name = name.lower()
    if name == "vit" and hasattr(cv2, "TrackerVit_create"):
        return cv2.TrackerVit_create()
    # Default: MIL (works without external model files)
    return cv2.TrackerMIL_create()


def track_object(
    src: Path,
    bbox_norm: tuple[float, float, float, float],
    *,
    canvas_w: int,
    canvas_h: int,
    method: Literal["mil", "vit"] = "mil",
    sample_every: int = 1,
) -> dict:
    """Track a rectangle starting at `bbox_norm` (x,y,w,h normalized to source
    pixels) through `src`. Returns a dict the dispatcher can save.

    `sample_every`: emit a keyframe every Nth frame (1 = every frame).
    """
    import cv2  # type: ignore

    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise RuntimeError(f"can't open {src}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    sw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    sh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    # First frame
    ok, frame = cap.read()
    if not ok:
        cap.release()
        raise RuntimeError("empty video")

    bx, by, bw, bh = bbox_norm
    init_box = (int(bx * sw), int(by * sh), int(bw * sw), int(bh * sh))

    tracker = _make_tracker(method)
    tracker.init(frame, init_box)

    # Map source pixel space → canvas pixel space (centre point of the box).
    sx = canvas_w / max(1, sw)
    sy = canvas_h / max(1, sh)

    track: list[list[float]] = []  # [time, cx, cy, w, h]
    frame_idx = 0
    track.append([
        0.0,
        (init_box[0] + init_box[2] / 2) * sx,
        (init_box[1] + init_box[3] / 2) * sy,
        init_box[2] * sx,
        init_box[3] * sy,
    ])

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame_idx += 1
        ok2, box = tracker.update(frame)
        if not ok2:
            continue
        if frame_idx % sample_every:
            continue
        x, y, w, h = box
        track.append([
            frame_idx / fps,
            (x + w / 2) * sx,
            (y + h / 2) * sy,
            w * sx,
            h * sy,
        ])

    cap.release()
    return {
        "src": str(src),
        "fps": fps,
        "source_size": [sw, sh],
        "canvas_size": [canvas_w, canvas_h],
        "method": method,
        "track": track,
    }


def save_track(track: dict, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(track), encoding="utf-8")
    return dst


def load_track(src: Path) -> dict:
    return json.loads(src.read_text(encoding="utf-8"))
