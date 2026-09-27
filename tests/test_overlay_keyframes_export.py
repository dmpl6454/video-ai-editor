"""Keyframes on OVERLAY (PiP), text and sticker layers export what the
preview draws (review RD3).

* A PiP's keyframed OPACITY and ROTATION exported as a constant: pip.py took
  the LAST key for the whole clip (`_scalar_or_last`) while the preview
  animates both (StickerLayer samples them at the render-local time). A PiP
  fade-in keyed 0 -> 1 measured 128 grey at clip-local 0, 1, 1.97 and 2.5 s
  (0, 64, 126, 128 due); a spin keyed 0 -> 90 degrees measured 89.9 degrees
  in every frame.
* A STEP key on its own frame switches ON that frame in the preview
  (lib/overlay.ts sampleKF) and, since E1a, in the v1 chain; the PiP, text
  and sticker expressions still printed key times to 4 digits and picked the
  segment on the raw `t`, so a key authored at 23/30 s switched a frame late.

The expected values come from `edl.keyframes.sample`, which mirrors the
client sampler (`sampleKF`) key for key.
"""
from __future__ import annotations

import math
import subprocess
from pathlib import Path

import numpy as np
import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl import timebase as tb
from video_ai_editor.edl.keyframes import sample
from video_ai_editor.edl.schema import Canvas, Keyframe, Transform
from video_ai_editor.render import compositor

W, H, R = 320, 180, 30


def _lavfi(dst: Path, color: str, vf: str = "", secs: float = 12.0) -> str:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s={W}x{H}:r={R}:d={secs}",
                    *(["-vf", vf] if vf else []),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "15", str(dst)], check=True)
    return str(dst)


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, str]:
    d = tmp_path_factory.mktemp("ovkf")
    return {
        "black": _lavfi(d / "black.mp4", "black"),
        "grey": _lavfi(d / "grey.mp4", "0x808080"),
        "white": _lavfi(d / "white.mp4", "white"),
        # left half white, right half black: its orientation is measurable
        "half": _lavfi(d / "half.mp4", "white", f"drawbox=x={W // 2}:y=0:w={W // 2}:h={H}:color=black:t=fill"),
    }


def _decode_y(path: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         check=True, capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, H, W).astype(np.float64)


def _store(tmp: Path, bg: str) -> EDLStore:
    st = EDLStore(tmp)
    st.edl.canvas = Canvas(w=W, h=H, fps=R)
    st.edl.canvas.loudness_lufs = None
    st.commit("c", {}, "c")
    dispatch(st, "add_clip", {"track": "v1", "src": bg, "in": 0, "out": 8, "start": 0})
    return st


def _pip(st: EDLStore, src: str, start: float, speed, **tx) -> str:
    cid = dispatch(st, "add_clip", {"track": "v2", "src": src, "in": 1.0, "out": 5.0, "start": start})["clip_id"]
    c = st.edl.get_clip(cid)[1]
    c.transform = Transform(**{"x": W / 2, "y": H / 2, "scale": 1.5, **tx})
    st.commit("t", {}, "t")
    if isinstance(speed, str):
        dispatch(st, "set_speed", {"clip_id": cid, "preset": speed})
    elif speed is not None:
        dispatch(st, "set_speed", {"clip_id": cid, "factor": speed})
    return cid


def _render(st: EDLStore, tmp: Path, name: str) -> np.ndarray:
    p = compositor._render(st.edl, tmp / f"{name}.mp4", height=H, fps=R, preview=False,
                           cache_dir=tmp / "cache", chunked=False)
    return _decode_y(p)


SPEEDS = [None, 2.0, "hero"]


@pytest.mark.parametrize("start", [0.0, 2.0])
@pytest.mark.parametrize("speed", SPEEDS, ids=lambda s: "1x" if s is None else str(s))
def test_keyed_pip_opacity_animates_in_the_export(tmp_path, media, start, speed):
    st = _store(tmp_path / "s", media["black"])
    op = Keyframe(keyframes=[(0.0, 0.0), (2.0, 1.0)], interp="linear")
    cid = _pip(st, media["white"], start, speed, opacity=op)
    frames = _render(st, tmp_path, "o")
    c = st.edl.get_clip(cid)[1]
    k0 = tb.frame_of(c.start, R)
    n = compositor.clip_frames(c, R)
    bad = []
    for j in range(0, min(n, 75), 4):
        y = frames[k0 + j][H // 2 - 8:H // 2 + 8, W // 2 - 8:W // 2 + 8].mean()
        want = 255 * sample(op, tb.time_of(j, R))     # full-range grey decode
        if abs(y - want) > 5:
            bad.append((j, round(y, 1), round(want, 1)))
    assert bad == [], f"PiP opacity off the keyed curve: {bad[:6]}"


def _angle(frame: np.ndarray, bg: float = 128.0, r: float = 34.0) -> float:
    """Direction (degrees, image axes) of the PiP's white half from its
    centre, over a disc well inside the element."""
    yy, xx = np.mgrid[0:H, 0:W]
    dx, dy = xx - W / 2, yy - H / 2
    disc = dx * dx + dy * dy <= r * r
    w = (frame - bg) * disc
    return math.degrees(math.atan2((w * dy).sum(), (w * dx).sum()))


def _adiff(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


@pytest.mark.parametrize("speed", SPEEDS, ids=lambda s: "1x" if s is None else str(s))
def test_keyed_pip_rotation_animates_in_the_export(tmp_path, media, speed):
    # Calibrate the export's own rotation sense on STATIC renders.
    st0 = _store(tmp_path / "c", media["grey"])
    cid0 = _pip(st0, media["half"], 0.0, None)
    base = _angle(_render(st0, tmp_path, "r0")[10])
    st0.edl.get_clip(cid0)[1].transform.rotation = 30.0
    st0.commit("r", {}, "r")
    sign = 1.0 if _adiff(_angle(_render(st0, tmp_path, "r30")[10]), base + 30) < 5 else -1.0
    assert _adiff(_angle(_render(st0, tmp_path, "r30b")[10]), base + sign * 30) < 5

    st = _store(tmp_path / "s", media["grey"])
    rot = Keyframe(keyframes=[(0.0, 0.0), (2.0, 90.0)], interp="linear")
    cid = _pip(st, media["half"], 1.0, speed, rotation=rot)
    frames = _render(st, tmp_path, "r")
    c = st.edl.get_clip(cid)[1]
    k0 = tb.frame_of(c.start, R)
    bad = []
    for j in range(0, min(compositor.clip_frames(c, R), 75), 5):
        got = _angle(frames[k0 + j])
        want = base + sign * sample(rot, tb.time_of(j, R))
        if _adiff(got, want) > 3:
            bad.append((j, round(got, 1), round(want, 1)))
    assert bad == [], f"PiP rotation off the keyed curve: {bad[:6]}"


def _x_centroid(frame: np.ndarray) -> float:
    w = np.clip(frame - 60, 0, None)
    return float((w * np.arange(W)[None, :]).sum() / max(1e-9, w.sum()))


@pytest.mark.parametrize("start", [1.0, 1 / 3])
def test_a_step_key_on_its_own_frame_switches_on_that_frame_on_a_pip(tmp_path, media, start):
    st = _store(tmp_path / "s", media["black"])
    key = tb.time_of(23, R)                       # 0.7666… s: prints 0.7667 at 4 digits
    x = Keyframe(keyframes=[(0.0, 110.0), (key, 210.0)], interp="step")
    cid = _pip(st, media["white"], tb.quantize(start, R), None, x=x)
    st.edl.get_clip(cid)[1].transform.scale = 0.5
    st.commit("s", {}, "s")
    frames = _render(st, tmp_path, "x")
    k0 = tb.frame_of(st.edl.get_clip(cid)[1].start, R)
    got = [round(_x_centroid(frames[k0 + j])) for j in (21, 22, 23, 24)]
    want = [round(sample(x, tb.time_of(j, R))) for j in (21, 22, 23, 24)]
    assert want == [110, 110, 210, 210]
    assert all(abs(g - w) <= 2 for g, w in zip(got, want)), (got, want)


def test_a_step_key_on_its_own_frame_switches_on_that_frame_on_a_text(tmp_path, media):
    st = _store(tmp_path / "s", media["black"])
    start = tb.time_of(20, R)                      # 2/3 s: prints 0.6667 at 4 digits (late)
    r = dispatch(st, "add_text", {"text": "IIIIIIII", "start": start, "end": start + 3.0,
                                  "x": W / 2, "y": H / 2, "scale": 2.0})
    tid = r.get("clip_id") or r.get("id")
    key = tb.time_of(23, R)
    op = Keyframe(keyframes=[(0.0, 1.0), (key, 0.2)], interp="step")
    st.edl.get_clip(tid)[1].transform.opacity = op
    st.commit("o", {}, "o")
    frames = _render(st, tmp_path, "t")
    k0 = tb.frame_of(start, R)
    bright = [float(frames[k0 + j].max()) for j in (21, 22, 23, 24)]
    assert bright[0] > 150 and bright[1] > 150, bright
    assert bright[2] < 110 and bright[3] < 110, f"the step at frame 23 switched late: {bright}"
