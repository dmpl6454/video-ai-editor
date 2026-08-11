"""Crop/reposition ("Fill frame") framing: rotation, and unticking the box.

Several of the reported symptoms were one class of fault — two renderers of the
same property disagreeing, so the picture jumped the moment an edit committed.
The client draws the live preview (CSS) and compositor.py bakes the real one;
whenever those two implement a transform differently, the user sees the edit
"change after I made it".
"""
from __future__ import annotations
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Canvas, empty_edl
from video_ai_editor.render.compositor import _build_clip_video_chain, render_export


def _store(tmp_path: Path, w: int = 1080, h: int = 1920) -> EDLStore:
    (tmp_path / "edl.json").write_text(
        empty_edl(Canvas(w=w, h=h, fps=30)).model_dump_json())
    return EDLStore(tmp_path)


def _mk_video(path: Path, w: int, h: int, dur: float = 2.0, color: str = "white") -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}:d={dur}:r=30",
         "-f", "lavfi", "-i", f"sine=f=440:d={dur}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
         str(path)], check=True, capture_output=True)
    return path


def _add(store: EDLStore, src: Path, dur: float = 2.0) -> str:
    dispatch(store, "add_clip",
             {"track": "v1", "src": str(src), "in": 0.0, "out": dur, "start": 0.0})
    return store.edl.get_track("v1").clips[0].id


def _chain(store: EDLStore, canvas_w: int, canvas_h: int) -> str:
    return _build_clip_video_chain(
        store.edl.get_track("v1").clips[0], input_label="[0:v]", label_out="[o]",
        canvas_w=canvas_w, canvas_h=canvas_h)


# --- rotation ---------------------------------------------------------------

def test_the_preview_canvas_box_clips_its_contents():
    """The live preview must not paint outside the frame it is previewing.

    `liveTransform` is applied with CSS, and a CSS `rotate()` does NOT shrink
    its element — the corners swing out of the box. With the box left at
    `overflow: visible` they kept painting into the surrounding pane, so a 60
    degree rotation drew 711px wide against a 349px frame (2.04x, measured in
    the running app) and then snapped to the cropped version the instant the
    value committed: "while doing it it is fine, but once I leave the bar the
    results are different". `.preview-pane`'s own overflow is no substitute —
    that is the whole pane, several times wider than the canvas box.
    """
    src = (Path(__file__).resolve().parents[1]
           / "frontend/src/components/Preview.tsx").read_text(encoding="utf-8")
    marker = "width: boxSize.w, height: boxSize.h"
    assert marker in src, "canvas-box element not found — did it get renamed?"
    line_start = src.index(marker)
    decl = src[line_start:src.index("\n", line_start)]
    assert "overflow: 'hidden'" in decl, (
        f"the canvas box must clip its contents, got:\n{decl}")

def test_rotation_does_not_shrink_the_picture(tmp_path: Path):
    """It expanded to the rotated bbox then scaled that back down to fit, so a
    3-degree straighten visibly zoomed the whole shot out — and disagreed with
    the CSS live preview, which rotates in place."""
    store = _store(tmp_path, 1280, 720)
    cid = _add(store, _mk_video(tmp_path / "w.mp4", 1280, 720))
    dispatch(store, "set_clip_transform", {"clip_id": cid, "rotation": 30})
    chain = _chain(store, 1280, 720)
    assert "rotate=" in chain
    assert "rotw" not in chain and "roth" not in chain, (
        "rotation must not expand to the rotated bounding box — that expansion "
        "is what forced the shrink-to-fit that followed it")


@pytest.mark.parametrize("rot", [5, 30])
def test_a_rotated_frame_still_reaches_every_canvas_edge(tmp_path: Path, rot: int):
    """In-place rotation keeps the picture full-bleed; only the swung-out
    corners go black. Shrink-to-fit insets it on all four sides instead."""
    store = _store(tmp_path, 1280, 720)
    cid = _add(store, _mk_video(tmp_path / "w.mp4", 1280, 720))
    dispatch(store, "set_clip_transform", {"clip_id": cid, "rotation": rot})
    out = render_export(store.edl, tmp_path / "cache", height=720)
    mp4 = Path(getattr(out, "path", out))
    frame = tmp_path / f"f{rot}.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", str(mp4),
                    "-frames:v", "1", str(frame)], check=True, capture_output=True)
    a = np.asarray(Image.open(frame).convert("L"))
    ys, xs = np.where(a > 200)
    assert xs.min() == 0 and xs.max() == a.shape[1] - 1, "picture inset horizontally"
    assert ys.min() == 0 and ys.max() == a.shape[0] - 1, "picture inset vertically"


# --- unticking "Fill frame" -------------------------------------------------

def test_leaving_cover_restores_the_original_framing(tmp_path: Path):
    """Reported as: unmarking Fill frame leaves the video cropped instead of
    returning it to its original aspect. x/y are a crop-window pan that only
    means anything under cover; carried into contain they translate an already
    letterboxed picture and crop the far edge."""
    store = _store(tmp_path)
    cid = _add(store, _mk_video(tmp_path / "wide.mp4", 1920, 1080))
    dispatch(store, "set_clip_fit", {"clip_id": cid, "fit": "cover"})
    dispatch(store, "set_clip_transform",
             {"clip_id": cid, "x": 200, "y": -50, "scale": 1.45})

    r = dispatch(store, "set_clip_fit", {"clip_id": cid, "fit": "contain"})
    c = store.edl.get_clip(cid)[1]
    assert c.fit == "contain"
    assert (c.transform.x, c.transform.y, c.transform.scale) == (0.0, 0.0, 1.0)
    assert "reset" in r["summary"]

    # ...and the render is then a plain letterbox: no crop anywhere in it.
    chain = _chain(store, 1080, 1920)
    assert "force_original_aspect_ratio=decrease" in chain and "pad=" in chain
    assert "crop=" not in chain


def test_entering_cover_does_not_touch_the_transform(tmp_path: Path):
    """The reset is one-directional. Ticking the box must not silently discard
    a scale/position the user already set."""
    store = _store(tmp_path)
    cid = _add(store, _mk_video(tmp_path / "wide.mp4", 1920, 1080))
    dispatch(store, "set_clip_transform", {"clip_id": cid, "x": 30, "scale": 1.2})
    dispatch(store, "set_clip_fit", {"clip_id": cid, "fit": "cover"})
    c = store.edl.get_clip(cid)[1]
    assert (c.transform.x, c.transform.scale) == (30.0, 1.2)


def test_contain_to_contain_is_a_no_op_for_the_transform(tmp_path: Path):
    """Only a cover -> contain transition resets. Re-asserting contain must not
    wipe a transform the user set while already in contain."""
    store = _store(tmp_path)
    cid = _add(store, _mk_video(tmp_path / "wide.mp4", 1920, 1080))
    dispatch(store, "set_clip_transform", {"clip_id": cid, "x": 45, "scale": 1.3})
    dispatch(store, "set_clip_fit", {"clip_id": cid, "fit": "contain"})
    c = store.edl.get_clip(cid)[1]
    assert (c.transform.x, c.transform.scale) == (45.0, 1.3)


def test_leaving_cover_does_not_touch_a_PIP_clip(tmp_path: Path):
    """`fit` is read ONLY by _build_clip_video_chain, which the compositor uses
    for the v1 base layer — render/pip.py never looks at it. So on a v2/PIP clip
    x/y/scale are the PIP's on-canvas PLACEMENT, not a crop pan, and toggling
    'Fill frame' there renders no differently at all. The first version of this
    reset turned that harmless no-op into a silent teardown of the PIP layout:
    a bottom-right PIP jumped to three-quarters off the top-left corner.
    """
    store = _store(tmp_path)
    src = _mk_video(tmp_path / "pip.mp4", 1920, 1080)
    dispatch(store, "add_clip",
             {"track": "v2", "src": str(src), "in": 0.0, "out": 2.0, "start": 0.0})
    cid = store.edl.get_track("v2").clips[0].id
    dispatch(store, "set_clip_transform",
             {"clip_id": cid, "x": 800, "y": 1500, "scale": 0.6})

    dispatch(store, "set_clip_fit", {"clip_id": cid, "fit": "cover"})
    dispatch(store, "set_clip_fit", {"clip_id": cid, "fit": "contain"})
    c = store.edl.get_clip(cid)[1]
    assert (c.transform.x, c.transform.y, c.transform.scale) == (800.0, 1500.0, 0.6), (
        "a fit toggle destroyed a PIP placement it does not even render")


def test_leaving_cover_keeps_a_scalar_scale_a_keyframed_pan_depends_on(tmp_path: Path):
    """Guarding each property independently is not enough — they are coupled.

    Zeroing a SCALAR `scale` while a KEYFRAMED `x` survives leaves the pan with
    no headroom: the animated branch emits `scale=<canvas>*max(1,1.0)`, so `iw`
    equals the canvas, ffmpeg clamps the pan expression to 0, and the authored
    animation renders motionless. Preserving a keyframed value while destroying
    what makes it visible is not preserving it.
    """
    store = _store(tmp_path)
    cid = _add(store, _mk_video(tmp_path / "wide.mp4", 1920, 1080, dur=3.0), dur=3.0)
    dispatch(store, "set_clip_fit", {"clip_id": cid, "fit": "cover"})
    dispatch(store, "set_clip_transform", {"clip_id": cid, "scale": 1.8})
    dispatch(store, "add_keyframe",
             {"clip_id": cid, "time": 0.0, "prop": "x", "value": -300})
    dispatch(store, "add_keyframe",
             {"clip_id": cid, "time": 3.0, "prop": "x", "value": 300})

    dispatch(store, "set_clip_fit", {"clip_id": cid, "fit": "contain"})
    c = store.edl.get_clip(cid)[1]
    assert c.transform.scale == 1.8, "the pan lost the zoom headroom it needs"
    assert not isinstance(c.transform.x, (int, float)), "the keyframed pan was flattened"


def test_leaving_cover_keeps_keyframed_values(tmp_path: Path):
    """A keyframed transform is authored animation; a fit toggle has no
    business flattening it to a constant."""
    store = _store(tmp_path)
    cid = _add(store, _mk_video(tmp_path / "wide.mp4", 1920, 1080))
    dispatch(store, "set_clip_fit", {"clip_id": cid, "fit": "cover"})
    dispatch(store, "add_keyframe",
             {"clip_id": cid, "time": 0.0, "prop": "scale", "value": 1.0})
    dispatch(store, "add_keyframe",
             {"clip_id": cid, "time": 1.0, "prop": "scale", "value": 2.0})
    before = store.edl.get_clip(cid)[1].transform.scale
    assert not isinstance(before, (int, float)), "fixture should be keyframed"

    dispatch(store, "set_clip_fit", {"clip_id": cid, "fit": "contain"})
    after = store.edl.get_clip(cid)[1].transform.scale
    assert after == before, "a keyframed scale must survive the fit toggle"


# --- cover pan sign (the contract the crop view mirrors) --------------------

def test_positive_x_moves_the_picture_right_under_cover(tmp_path: Path):
    """lib/cropLayout.ts places the picture at `+x`; that is only correct if
    the bake also treats +x as 'the picture moves right'."""
    store = _store(tmp_path)
    cid = _add(store, _mk_video(tmp_path / "wide.mp4", 1920, 1080))
    dispatch(store, "set_clip_fit", {"clip_id": cid, "fit": "cover"})
    dispatch(store, "set_clip_transform", {"clip_id": cid, "x": 120})
    chain = _chain(store, 1080, 1920)
    # The crop window moves LEFT by x, which IS the picture moving right by x.
    assert "-120.00" in chain, (
        f"expected the pan to subtract x from the crop origin:\n{chain}")


def test_the_cover_pan_sign_holds_in_the_actual_PIXELS(tmp_path: Path):
    """The filtergraph assertion above only proves a string. This proves the
    bake agrees with what the crop window frames, which is the property the
    whole view depends on: with the window fixed, `+x` slides the picture right,
    so the window ends up over the source's LEFT edge and that is what renders.
    """
    store = _store(tmp_path)
    src = tmp_path / "bars.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "color=c=gray:s=1920x1080:d=2:r=30",
         "-f", "lavfi", "-i", "sine=f=440:d=2",
         "-vf", "drawbox=x=0:y=0:w=300:h=1080:color=red@1:t=fill,"
                "drawbox=x=1620:y=0:w=300:h=1080:color=blue@1:t=fill",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
         str(src)], check=True, capture_output=True)
    cid = _add(store, src)
    dispatch(store, "set_clip_fit", {"clip_id": cid, "fit": "cover"})

    def bars(x: float) -> tuple[float, float]:
        dispatch(store, "set_clip_transform", {"clip_id": cid, "x": x})
        out = render_export(store.edl, tmp_path / "cache", height=640)
        mp4 = Path(getattr(out, "path", out))
        f = tmp_path / f"x{x}.png"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", str(mp4),
                        "-frames:v", "1", str(f)], check=True, capture_output=True)
        a = np.asarray(Image.open(f).convert("RGB")).astype(int)
        red = ((a[:, :, 0] > 120) & (a[:, :, 1] < 80) & (a[:, :, 2] < 80)).mean()
        blue = ((a[:, :, 2] > 120) & (a[:, :, 0] < 80) & (a[:, :, 1] < 80)).mean()
        return red, blue

    r_mid, b_mid = bars(0)
    assert r_mid < 0.01 and b_mid < 0.01, "a centred cover crop shows neither edge"
    r_pos, b_pos = bars(600)
    assert r_pos > 0.2 and b_pos < 0.01, "+x must reveal the source's LEFT edge"
    r_neg, b_neg = bars(-600)
    assert b_neg > 0.2 and r_neg < 0.01, "-x must reveal the source's RIGHT edge"
