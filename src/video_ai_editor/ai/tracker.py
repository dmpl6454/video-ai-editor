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

    if sample_every < 1:
        # `frame_idx % 0` below is a ZeroDivisionError; a negative stride never
        # matches, so the whole loop silently emits nothing.
        raise ValueError(f"sample_every must be >= 1, got {sample_every}")

    # Clamp the init box into the frame. `dispatch._norm_bbox` already rejects
    # a non-0..1 bbox, but this is the layer where getting it wrong is FATAL
    # rather than merely wrong: MIL sizes its feature buffers from the box
    # area, so a bbox given in pixels (e.g. [10,10,100,100] -> a 192000×108000
    # box) makes cv2 allocate until the OS SIGKILLs the whole app — exit 137,
    # no traceback, every session in the process gone with it (QA round 5,
    # VAI-06, and the likeliest contributor to the VAI-11 instability report).
    # A library that can kill the process must defend itself, not trust its
    # caller.
    bx, by, bw, bh = (float(v) for v in bbox_norm)
    bx = min(max(bx, 0.0), 1.0)
    by = min(max(by, 0.0), 1.0)
    bw = min(max(bw, 0.0), 1.0 - bx)
    bh = min(max(bh, 0.0), 1.0 - by)
    init_box = (int(bx * sw), int(by * sh), int(bw * sw), int(bh * sh))
    if init_box[2] < 2 or init_box[3] < 2:
        cap.release()
        raise ValueError(
            f"bbox {tuple(bbox_norm)} is {init_box[2]}×{init_box[3]}px in a "
            f"{sw}×{sh} source — too small to track (bbox is normalised 0..1)")

    tracker = _make_tracker(method)
    try:
        tracker.init(frame, init_box)
    except Exception as e:
        cap.release()
        raise RuntimeError(f"tracker init failed on {init_box}: {e}") from e

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

    try:
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
    finally:
        # A cv2.error mid-loop used to leak the VideoCapture (and its decoder
        # buffers) for the life of the process.
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
