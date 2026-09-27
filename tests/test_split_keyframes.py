"""A split, a cut_range or a trim keeps a clip's KEYFRAMED transform where it
was (review RD3).

Transform keys (x, y, scale, rotation, opacity) are clip-local timeline
seconds. `split_at` partitioned the volume automation (`_split_gain_env`) but
copied the whole clip's transform keys, unchanged, onto both halves: the right
half restarted the animation from its first key (measured: a white marker at
x 362 whole vs 320 split at k=41; 139 of 180 frames differed at 1x). Each
piece now carries the part of the animation it plays, re-based to its own 0
(`dispatch._piece_kf`): sampling a piece at `u` equals sampling the whole at
`t0 + u`, for every interpolation.
"""
from __future__ import annotations

import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))
import geometry_golden_lib as L  # noqa: E402

from video_ai_editor.agent.dispatch import _piece_kf, dispatch  # noqa: E402
from video_ai_editor.edl import EDLStore  # noqa: E402
from video_ai_editor.edl import timebase as tb  # noqa: E402
from video_ai_editor.edl.keyframes import sample  # noqa: E402
from video_ai_editor.edl.schema import Canvas, Keyframe, Transform  # noqa: E402
from video_ai_editor.render import compositor  # noqa: E402

INTERPS = ["linear", "ease-in", "ease-out", "ease-in-out", "step", "back-out"]
KEYS = [(0.0, 0.0), (1.2, 100.0), (3.0, 40.0), (4.8, 150.0)]


def _close(piece, whole, t0: float, span: float) -> list[float]:
    bad = []
    for i in range(0, 241):
        u = span * i / 240
        if abs(sample(piece, u) - sample(whole, t0 + u)) > 1e-6:
            bad.append(round(u, 4))
    return bad


@pytest.mark.parametrize("interp", INTERPS)
@pytest.mark.parametrize("cut", [0.5, 1.2, 2.0, 3.0, 4.1, 4.8, 5.5])
def test_a_piece_samples_exactly_the_part_of_the_curve_it_plays(interp, cut):
    whole = Keyframe(keyframes=KEYS, interp=interp)
    left, right = _piece_kf(whole, 0.0, cut), _piece_kf(whole, cut, None)
    assert _close(left, whole, 0.0, cut) == []
    assert _close(right, whole, cut, 6.0 - cut) == []
    mid = _piece_kf(whole, 1.0, 3.7)
    assert _close(mid, whole, 1.0, 2.7) == []


def test_a_linear_cut_gets_a_key_at_the_cut_and_no_key_outside():
    whole = Keyframe(keyframes=KEYS, interp="linear")
    right = _piece_kf(whole, 2.0, None)
    ts = [t for t, _ in right.keyframes]
    assert ts[0] == 0.0 and min(ts) >= 0.0
    assert right.keyframes[0][1] == pytest.approx(100 - 60 * 0.8 / 1.8)
    left = _piece_kf(whole, 0.0, 2.0)
    assert max(t for t, _ in left.keyframes) == pytest.approx(2.0)


def test_a_piece_past_the_last_key_is_that_constant():
    assert _piece_kf(Keyframe(keyframes=KEYS, interp="linear"), 5.0, None) == 150.0


def _store(tmp: Path, track: str, speed, src: str, bg: str | None = None) -> tuple[EDLStore, str]:
    st = EDLStore(tmp)
    st.edl.canvas = Canvas(w=640, h=360, fps=30)
    st.edl.canvas.loudness_lufs = None
    st.commit("c", {}, "c")
    if track == "v2":
        dispatch(st, "add_clip", {"track": "v1", "src": bg or src, "in": 0, "out": 9, "start": 0})
    cid = dispatch(st, "add_clip", {"track": track, "src": src, "in": 1.0, "out": 7.0,
                                    "start": 1.0 if track == "v2" else 0.0})["clip_id"]
    if track == "v2":
        st.edl.get_clip(cid)[1].transform = Transform(x=320, y=180, scale=1.5)
        st.commit("t", {}, "t")
    if isinstance(speed, str):
        dispatch(st, "set_speed", {"clip_id": cid, "preset": speed})
    elif speed is not None:
        dispatch(st, "set_speed", {"clip_id": cid, "factor": speed})
    c = st.edl.get_clip(cid)[1]
    eff = c.effective_duration
    x0 = 0 if track == "v1" else 320
    for prop, t, v in (("x", 0.0, x0), ("x", round(eff * 0.8 * 30) / 30, x0 + 150),
                       ("scale", 0.0, 1.0 if track == "v1" else 1.5),
                       ("scale", round(eff * 0.6 * 30) / 30, 1.6 if track == "v1" else 0.8)):
        dispatch(st, "add_keyframe", {"clip_id": cid, "prop": prop, "time": t, "value": v})
    return st, cid


def _timeline_samples(st: EDLStore, track: str) -> list[tuple[float, float]]:
    """(x, scale) of the clip under each output frame of the lane."""
    out = []
    clips = sorted(st.edl.get_track(track).clips, key=lambda c: c.start)
    for c in clips:
        n = compositor.clip_frames(c, 30)
        for j in range(n):
            u = tb.time_of(j, 30)
            out.append((sample(c.transform.x, u), sample(c.transform.scale, u)))
    return out


@pytest.mark.parametrize("op", ["split", "cut", "trim"])
@pytest.mark.parametrize("track", ["v1", "v2"])
def test_edit_ops_keep_the_animation_on_its_frames(tmp_path, track, op):
    st, cid = _store(tmp_path / "s", track, 2.0, "bars.mp4")
    whole = _timeline_samples(st, track)
    c = st.edl.get_clip(cid)[1]
    t0 = c.start
    if op == "split":
        for frac in (0.23, 0.47, 0.71):
            dispatch(st, "split_at", {"time": tb.time_of(round((t0 + c.effective_duration * frac) * 30), 30),
                                      "track": track})
        got, want = _timeline_samples(st, track), whole
    elif op == "cut":
        a, b = tb.frame_of(t0, 30) + 20, tb.frame_of(t0, 30) + 35
        dispatch(st, "cut_range", {"track": track, "start": tb.time_of(a, 30), "end": tb.time_of(b, 30)})
        got, want = _timeline_samples(st, track), whole[:20] + whole[35:]
    else:
        dispatch(st, "trim_clip", {"clip_id": cid, "in": c.in_ + 24 / 30, "move_start": True})
        got, want = _timeline_samples(st, track), whole[12:]
    assert len(got) == len(want)
    bad = [i for i, (a, b) in enumerate(zip(got, want)) if abs(a[0] - b[0]) > 1e-6 or abs(a[1] - b[1]) > 1e-6]
    assert bad == [], f"{len(bad)} frames animate differently, first {bad[:5]}"


# ------------------------------------------------------------ real renders

@pytest.fixture(scope="module")
def land(tmp_path_factory) -> tuple[str, str]:
    """The keyed picture, and a flat background for the overlay case (the
    same picture under a PiP of itself hides the PiP's motion)."""
    d = tmp_path_factory.mktemp("kfland")
    bg = d / "flat.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x203050:s=640x360:r=30:d=10",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(bg)], check=True)
    return str(L.make_source(d / "land30.mp4", L.Src("land30", 1280, 720, Fraction(30), 300))), str(bg)


@pytest.mark.parametrize("track,speed", [("v1", None), ("v1", 2.0), ("v2", None)],
                         ids=["v1_1x", "v1_2x", "v2_1x"])
def test_a_split_keyframed_clip_exports_the_whole_clips_pictures(tmp_path, land, track, speed):
    st, cid = _store(tmp_path / "s", track, speed, land[0], bg=land[1])

    def render(name: str) -> np.ndarray:
        p = compositor._render(st.edl, tmp_path / f"{name}.mp4", height=360, fps=30, preview=False,
                               cache_dir=tmp_path / "cache", chunked=False)
        return L.decode_rgb(p, 640, 360).astype(np.float64)

    whole = render("whole")
    c = st.edl.get_clip(cid)[1]
    for frac in (0.23, 0.47, 0.71):
        dispatch(st, "split_at", {"time": tb.time_of(round((c.start + c.effective_duration * frac) * 30), 30),
                                  "track": track})
    split = render("split")
    assert len(split) == len(whole)
    err = [float(np.abs(whole[i] - split[i]).mean()) for i in range(len(whole))]
    bad = [i for i, e in enumerate(err) if e > 2.0]
    assert bad == [], f"{len(bad)} frames differ (max mean-abs {max(err):.1f}), first {bad[:6]}"
