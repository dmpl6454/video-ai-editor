"""MediaPipe-driven subject-tracked auto-reframe.

We sample frames at ~2 Hz, find the dominant face/person, build a smoothed
crop window over time, then write per-frame `crop` / `pad` ffmpeg directives
into a sendcmd file so the crop window animates with the subject.

Output is a new mp4 in the session cache. The clip's `src` is then swapped to
that mp4 in the EDL.
"""
from __future__ import annotations
import hashlib
import subprocess
from pathlib import Path
import json

from .. import platformutil as _pu

# Sample 2 frames per second for tracking
SAMPLE_HZ = 2.0


def _key(src: Path, target_w: int, target_h: int) -> str:
    return hashlib.sha256(f"{src}|{target_w}x{target_h}".encode()).hexdigest()[:14]


def _detect_subject_centers(src: Path) -> list[tuple[float, float, float]]:
    """For each sample, return (timestamp, cx_norm, cy_norm) where coords are 0..1.

    Uses OpenCV Haar Cascade for face detection (ships with cv2, no model
    download). Cheap and good enough to drive a smoothed crop window. The new
    MediaPipe tasks API would also work but requires a separate .task download.
    """
    import cv2

    cap = cv2.VideoCapture(str(src))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if n_frames == 0 or not src_w or not src_h:
        cap.release()
        return []
    step = max(1, int(round(fps / SAMPLE_HZ)))

    haar_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    detector = cv2.CascadeClassifier(str(haar_path))
    if detector.empty():
        cap.release()
        return []

    out: list[tuple[float, float, float]] = []
    i = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if i % step == 0:
            t = i / fps
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = detector.detectMultiScale(gray, scaleFactor=1.2, minNeighbors=4,
                                              minSize=(40, 40))
            if len(faces) > 0:
                # Pick the largest face
                fx, fy, fw, fh = max(faces, key=lambda r: r[2] * r[3])
                cx = (fx + fw / 2) / src_w
                cy = (fy + fh / 2) / src_h
                out.append((t, float(cx), float(cy)))
            else:
                out.append((t, 0.5, 0.5))
        i += 1
    cap.release()
    return out


def _smooth(samples: list[tuple[float, float, float]], window: int = 5) -> list[tuple[float, float, float]]:
    """Moving-average smoothing on (cx, cy) so the crop doesn't twitch."""
    if not samples:
        return samples
    out: list[tuple[float, float, float]] = []
    for i, (t, cx, cy) in enumerate(samples):
        s = max(0, i - window)
        e = min(len(samples), i + window + 1)
        cs = sum(s2[1] for s2 in samples[s:e]) / (e - s)
        cy2 = sum(s2[2] for s2 in samples[s:e]) / (e - s)
        out.append((t, cs, cy2))
    return out


def reframe_clip(src: Path, cache_dir: Path, *, target_w: int, target_h: int) -> Path:
    """Crop+scale a clip to (target_w, target_h) keeping the dominant subject in frame."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    dst = cache_dir / f"reframe_{_key(src, target_w, target_h)}.mp4"
    if dst.exists() and dst.stat().st_size > 0:
        return dst

    # Probe source size
    proc = subprocess.run(
        [_pu.FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate", "-of", "json", str(src)],
        capture_output=True, text=True, check=True,
    )
    info = json.loads(proc.stdout)
    s = info["streams"][0]
    src_w, src_h = int(s["width"]), int(s["height"])

    samples = _detect_subject_centers(src)
    samples = _smooth(samples)

    target_aspect = target_w / target_h
    src_aspect = src_w / src_h

    # The crop window in source pixels: we crop a region with the target aspect,
    # as large as possible, centered on the smoothed subject.
    if target_aspect <= src_aspect:
        # Crop horizontally (e.g. 16:9 source → 9:16 output)
        cw = int(src_h * target_aspect)
        ch = src_h
    else:
        # Crop vertically (e.g. 9:16 source → 16:9 output)
        cw = src_w
        ch = int(src_w / target_aspect)

    if not samples:
        # Fallback: dead-center crop
        cx_px = src_w // 2
        cy_px = src_h // 2
        x = max(0, min(src_w - cw, cx_px - cw // 2))
        y = max(0, min(src_h - ch, cy_px - ch // 2))
        proc = subprocess.run(
            [_pu.FFMPEG, "-y", "-i", str(src),
             "-vf", f"crop={cw}:{ch}:{x}:{y},scale={target_w}:{target_h}",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
             "-c:a", "aac", str(dst)],
            capture_output=True, check=True,
        )
        return dst

    # Animated crop using `sendcmd` to update crop x/y over time.
    # Build a sendcmd file with one entry per sample.
    cmd_path = cache_dir / f"reframe_cmd_{_key(src, target_w, target_h)}.txt"
    lines: list[str] = []
    for t, cx_n, cy_n in samples:
        cx_px = cx_n * src_w
        cy_px = cy_n * src_h
        x = max(0, min(src_w - cw, int(cx_px - cw / 2)))
        y = max(0, min(src_h - ch, int(cy_px - ch / 2)))
        lines.append(f"{t:.3f} crop x {x}, crop y {y};")
    cmd_path.write_text("\n".join(lines), encoding="utf-8")

    cmd_arg = str(cmd_path).replace("\\", r"\\").replace(":", r"\:").replace("'", r"\'")
    vf = f"sendcmd=f={cmd_arg},crop={cw}:{ch}:0:0,scale={target_w}:{target_h}"
    proc = subprocess.run(
        [_pu.FFMPEG, "-y", "-i", str(src),
         "-vf", vf,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", str(dst)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg reframe failed (rc={proc.returncode}):\n{proc.stderr[-1500:]}")
    return dst
