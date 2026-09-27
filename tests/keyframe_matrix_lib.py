"""The v1 keyframe matrix (Wave D3, lane E1a): every keyframed property, on
every clock a clip can have, measured on decoded renders against the value
the UI samples at `playhead - clip.start` (lib/overlay.ts `sampleKF`;
`edl/keyframes.sample` is its Python twin).

One render per (property group, clock): three clips of the same clock at
timeline 0 s, 2 s and 30 s. Clocks: 1x, 0.5x, 2x, a speed curve, reverse,
reverse at 2x, a freeze. Groups:

* ``pan``  — x (3 keys), y (3 keys), opacity keyed, scale 2 (static): the
  white centre marker's centroid gives x/y, the grey's gain gives opacity;
* ``zoom`` — scale (3 keys) and rotation (3 keys): the red and green side
  markers give the zoom (their distance) and the angle (their direction).

x and scale are linear; y, opacity and rotation take one interpolation per
clock, so the 7 clocks cover all 7 interpolations (linear, ease-in,
ease-out, ease-in-out, step, back-out, bounce — the last is linear in every
implementation). ``interp`` renders then key x and scale with each
interpolation (7 clips at 1x). The mid keys sit ON frame times (the UI keys
at the playhead), so a `step` is checked at its tie.

The source is static (flat grey, three markers), so geometry is all that a
frame shows: which source frame a clock picks is pinned elsewhere
(frame_map goldens).
"""
from __future__ import annotations

import contextlib
import itertools
import json
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from video_ai_editor.edl.keyframes import sample
from video_ai_editor.edl.schema import EDL, Canvas, Clip, Keyframe, empty_edl
from video_ai_editor.render import compositor

REPO = Path(__file__).resolve().parents[1]
GOLDEN = REPO / "tests" / "goldens" / "keyframe_matrix.json"
VERSION = 1
W, H, FPS = 640, 360, 30
GREY = 128
SRC_W, SRC_H, SRC_FRAMES = 1280, 720, 360
#: source markers: name → (u, v, size px, rgb)
MARKERS = {
    "white": (0.5, 0.5, 48, (255, 255, 255)),
    "red": (0.35, 0.5, 32, (255, 0, 0)),
    "green": (0.65, 0.5, 32, (0, 255, 0)),
}
STARTS = (0.0, 2.0, 30.0)
INTERPS = ("linear", "ease-in", "ease-out", "ease-in-out", "step", "back-out", "bounce")
#: clock → clip fields (every footprint ≤ 2 s, so the clips at 0 and 2 never overlap)
CLOCKS: dict[str, dict[str, Any]] = {
    "1x": {"in": 1.0, "out": 2.5},
    "0.5x": {"in": 1.0, "out": 2.0, "speed": 0.5},
    "2x": {"in": 1.0, "out": 4.0, "speed": 2.0},
    "curve": {"in": 1.0, "out": 2.6, "speed": {"curve": [[0, 1], [0.5, 0.5], [1, 1.5]]}},
    "reverse": {"in": 1.0, "out": 2.5, "reverse": True},
    "reverse_2x": {"in": 1.0, "out": 4.0, "speed": 2.0, "reverse": True},
    "freeze": {"in": 1.5, "out": 1.5 + 1 / 30, "freeze": 1.5},
}


def kf(pts, interp="linear") -> dict:
    return {"keyframes": [list(p) for p in pts], "interp": interp}


def on_frame(t: float) -> float:
    """A key time on the frame grid (the UI keys at the playhead)."""
    return round(t * FPS) / FPS


def group_transform(group: str, dur: float, interp: str, *, x_interp: str = "linear") -> dict:
    mid = on_frame(0.55 * dur)
    if group == "pan":
        return {"x": kf([(0.1, -90), (mid, 60), (dur - 0.1, 120)], x_interp),
                "y": kf([(0.0, 50), (on_frame(0.4 * dur), -10), (dur, -40)], interp),
                "opacity": kf([(0.15, 1.0), (dur - 0.15, 0.35)], interp),
                "scale": 2.0}
    return {"scale": kf([(0.1, 1.0), (mid, 1.7), (dur - 0.1, 1.2)], x_interp),
            "rotation": kf([(0.0, -20), (on_frame(0.4 * dur), 25), (dur, 70)], interp)}


@dataclass
class Render:
    name: str
    group: str
    #: clip id → (start, clock, transform)
    clips: list[tuple[str, float, str, dict]] = field(default_factory=list)


def _clip(cid: str, src: str, start: float, clock: str, tx: dict) -> Clip:
    spec = CLOCKS[clock]
    c = Clip(src=src, id=cid, start=start, speed=spec.get("speed"))
    c.in_, c.out = spec["in"], spec["out"]
    if spec.get("reverse"):
        c.reverse = True
    if spec.get("freeze") is not None:
        c.freeze = spec["freeze"]
    for k, v in tx.items():
        setattr(c.transform, k, Keyframe(**v) if isinstance(v, dict) else v)
    return c


def footprint(clock: str) -> float:
    return _clip("x", "s", 0.0, clock, {}).effective_duration


def renders() -> list[Render]:
    out: list[Render] = []
    for i, clock in enumerate(CLOCKS):
        interp = INTERPS[i % len(INTERPS)]
        d = footprint(clock)
        for group in ("pan", "zoom"):
            r = Render(f"{group}_{clock}", group)
            for j, st in enumerate(STARTS):
                r.clips.append((f"{group[0]}{i}{j}", st, clock, group_transform(group, d, interp)))
            out.append(r)
    d = footprint("1x")
    for group in ("pan", "zoom"):
        r = Render(f"{group}_interp", group)
        for j, interp in enumerate(INTERPS):
            r.clips.append((f"{group[0]}i{j}", 2.0 * j, "1x", group_transform(group, d, "linear", x_interp=interp)))
        out.append(r)
    return out


def build_edl(r: Render, src: str) -> EDL:
    e = empty_edl(Canvas(w=W, h=H, fps=FPS))
    e.canvas.loudness_lufs = None
    v1 = e.get_track("v1")
    for cid, st, clock, tx in r.clips:
        v1.clips.append(_clip(cid, src, st, clock, tx))
    e.recompute_duration()
    return e


def edl_json(edl: EDL, src_key: str = "mx") -> dict:
    d = edl.model_dump(by_alias=True, mode="json")
    d["tracks"] = [t for t in d["tracks"] if t["id"] == "v1"]
    for c in d["tracks"][0]["clips"]:
        c["src"] = src_key
    return d


# ------------------------------------------------------------------ source

def make_source(path: Path) -> Path:
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    boxes = ",".join(
        f"drawbox=x={round(u * SRC_W) - m // 2}:y={round(v * SRC_H) - m // 2}:w={m}:h={m}"
        f":color=0x{r:02x}{g:02x}{b:02x}:t=fill"
        for u, v, m, (r, g, b) in MARKERS.values())
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"color=0x{GREY:02x}{GREY:02x}{GREY:02x}:s={SRC_W}x{SRC_H}:r={FPS}:d={SRC_FRAMES / FPS + 1}",
         "-vf", f"{boxes},format=yuv420p", "-frames:v", str(SRC_FRAMES),
         "-c:v", "libx264", "-qp", "0", "-preset", "veryfast", str(path)],
        check=True, capture_output=True)
    return path


# --------------------------------------------------------------- measuring

def clip_frames(edl: EDL) -> dict[str, tuple[int, int]]:
    """clip id → [first, end) output frame on the grid (compositor's plan)."""
    fr: dict[str, tuple[int, int]] = {}
    for c in edl.get_track("v1").clips:
        f0 = round(c.start * FPS)
        fr[c.id] = (f0, f0 + compositor.clip_frames(c, FPS))
    return fr


def decode(path: Path, ks: list[int], w: int = W, h: int = H) -> tuple[np.ndarray, np.ndarray]:
    """RGB and luma of output frames `ks` (ascending) only."""
    runs: list[list[int]] = []
    for k in ks:
        if runs and k == runs[-1][1] + 1:
            runs[-1][1] = k
        else:
            runs.append([k, k])
    sel = "+".join(f"between(n\\,{a}\\,{b})" for a, b in runs)
    rgb = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-vf", f"select='{sel}'",
                          "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    yuv = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-vf", f"select='{sel}'",
                          "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True, check=True).stdout
    n = len(ks)
    return (np.frombuffer(rgb, np.uint8).reshape(n, h, w, 3)[:n],
            np.frombuffer(yuv, np.uint8).reshape(n, h, w)[:n])


def _box(mask: np.ndarray, pad: int = 4) -> tuple[slice, slice] | None:
    ys, xs = np.nonzero(mask)
    if len(xs) < 6:
        return None
    h, w = mask.shape
    return (slice(max(0, ys.min() - pad), min(h, ys.max() + pad + 1)),
            slice(max(0, xs.min() - pad), min(w, xs.max() + pad + 1)))


def measure(rgb: np.ndarray, y: np.ndarray) -> dict[str, Any]:
    """Luma-weighted marker centroids (canvas px, pixel-edge coords), found
    by colour and weighted by luma (free of the 4:2:0 chroma siting), each
    normalised to its own peak so an opacity keyframe does not bias it; and
    the gain (median grey luma → the RGB multiplier)."""
    f = rgb.astype(np.int32)
    lum = y.astype(np.float64)
    r, g, b = f[..., 0], f[..., 1], f[..., 2]
    mn = np.minimum(np.minimum(r, g), b)
    greyish = (np.abs(r - g) < 10) & (np.abs(g - b) < 10) & (mn > 8)
    bg_rgb = float(np.median(mn[greyish])) if greyish.any() else 0.0
    bg_y = float(np.median(lum[greyish])) if greyish.any() else 16.0
    out: dict[str, Any] = {"gain": round((bg_rgb / GREY), 4)}
    masks = {
        "white": mn > bg_rgb + max(12.0, 0.4 * (255 - GREY) * bg_rgb / GREY),
        "red": (r - np.maximum(g, b)) > max(20.0, 0.4 * bg_rgb),
        "green": (g - np.maximum(r, b)) > max(20.0, 0.4 * bg_rgb),
    }
    for name, m in masks.items():
        bx = _box(m)
        hh, ww = m.shape
        if bx is None or bx[0].start == 0 or bx[1].start == 0 or bx[0].stop == hh or bx[1].stop == ww:
            out[name] = None
            continue
        win = lum[bx]
        peak = win.min() if name == "red" else win.max()
        wt = np.clip((win - bg_y) / (peak - bg_y), 0.0, 1.0) if abs(peak - bg_y) > 3 else None
        if wt is None or wt.sum() < 4:
            out[name] = None
            continue
        gy, gx = np.mgrid[bx[0], bx[1]]
        tot = float(wt.sum())
        out[name] = [round(float((wt * gx).sum()) / tot + 0.5, 3), round(float((wt * gy).sum()) / tot + 0.5, 3)]
    return out


@contextlib.contextmanager
def software_encoder():
    """libx264 instead of the hardware ladder: deterministic output, so a
    measurement is a property of the filter graph, not of the encoder."""
    saved = compositor._usable_encoder
    compositor._usable_encoder = lambda name: False
    try:
        yield
    finally:
        compositor._usable_encoder = saved


def render_and_measure(r: Render, src: str, work: Path, *, chunked: bool = False,
                       preview: bool = False, height: int = H) -> tuple[EDL, list[dict]]:
    edl = build_edl(r, src)
    sess = work / f"{r.name}{'-chunked' if chunked else ''}{'-preview' if preview else ''}-{height}"
    sess.mkdir(parents=True, exist_ok=True)
    with software_encoder():
        if preview:
            path = Path(compositor.render_preview(edl, sess / "session", height=height).path)
        else:
            path = compositor._render(edl, sess / "out.mp4", height=height, fps=FPS, preview=False,
                                      cache_dir=sess / "cache", chunked=chunked, crf=12)
    if height != H:
        return edl, [{"path": str(path)}]
    spans = clip_frames(edl)
    ks = sorted(k for a, b in spans.values() for k in range(a, b))
    rgb, ys = decode(path, ks)
    rows = []
    for i, k in enumerate(ks):
        cid = next(c for c, (a, b) in spans.items() if a <= k < b)
        rows.append({"k": k, "clip": cid, **measure(rgb[i], ys[i])})
    return edl, rows


# ---------------------------------------------------------- the UI's values

def ui_values(clip: Clip, k: int) -> dict[str, float]:
    """What the UI shows at output frame k: every property sampled at
    `playhead - clip.start` (sampleKF / keyframes.sample)."""
    t = k / FPS - clip.start
    tx = clip.transform
    num = lambda v, d: float(v) if isinstance(v, (int, float)) else d  # noqa: E731
    return {p: (sample(getattr(tx, p), t) if isinstance(getattr(tx, p), Keyframe) else num(getattr(tx, p), dflt))
            for p, dflt in (("x", 0.0), ("y", 0.0), ("scale", 1.0), ("rotation", 0.0), ("opacity", 1.0))}


def expected_markers(clip: Clip, v: dict[str, float]) -> dict[str, tuple[float, float]]:
    """Where the chain puts each marker for UI values `v`: contain (the
    1280x720 source is exactly the 640x360 canvas) → rotate in place about
    the centre, clockwise → scale to (W·s, H·s) on the chroma grid, centred
    in the pan frame, crop at centre − (x, y): ffmpeg's integers
    (compositor `kf_pan_frame`, pad/crop offsets snapped to even)."""
    s = v["scale"]
    sw, sh = max(2, int(W * s / 2) * 2), max(2, int(H * s / 2) * 2)
    fw, fh = compositor.kf_pan_frame(clip.transform, W, H)
    px, py = int((fw - sw) / 2) // 2 * 2, int((fh - sh) / 2) // 2 * 2

    def crop(c: float, inp: int, outp: int) -> int:
        x = math.floor(c + 0.5) if c - math.floor(c) != 0.5 else (math.floor(c) + (math.floor(c) % 2))
        x = min(max(x, 0), inp - outp)
        return x - x % 2

    cx, cy = crop((fw - W) // 2 - v["x"], fw, W), crop((fh - H) // 2 - v["y"], fh, H)
    th = math.radians(v["rotation"])
    out = {}
    for name, (u, vv, _m, _c) in MARKERS.items():
        x1, y1 = u * W - W / 2, vv * H - H / 2
        x2 = W / 2 + x1 * math.cos(th) - y1 * math.sin(th)
        y2 = H / 2 + x1 * math.sin(th) + y1 * math.cos(th)
        out[name] = (x2 * sw / W + px - cx, y2 * sh / H + py - cy)
    return out


def marker_error(clip: Clip, k: int, measured: dict, names: tuple[str, ...]) -> float:
    """The largest distance (px, per axis) between a measured marker and
    where the UI's values put it — the smallest over the UI values nudged
    ±1e-6: ffmpeg evaluates the same expression in floats, and a value ON
    an integer boundary (x = 97.5 → lrint, s = 1.7 → trunc(640·s/2))
    rounds either way."""
    v = ui_values(clip, k)
    best = math.inf
    for dx, dy, ds in itertools.product((0.0, 1e-6, -1e-6), repeat=3):
        e = expected_markers(clip, {**v, "x": v["x"] + dx, "y": v["y"] + dy, "scale": v["scale"] + ds})
        err = max(max(abs(measured[n][0] - e[n][0]), abs(measured[n][1] - e[n][1])) for n in names)
        best = min(best, err)
    return best


def load() -> dict:
    return json.loads(GOLDEN.read_text())
