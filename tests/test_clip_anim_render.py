"""Clip animations render (wave E, F1): every In, Out and Combo preset on the
main track, on an overlay (PIP) lane and on a sticker, at 25 and 30 fps —
decoded, measured and compared, frame by frame, with the animation's own
value at that frame's clip-local time k/R (`edl/clip_animations.AnimPlan`).

The source is a white box on black (`clip_anim_lib`), so each frame's white
region gives the box's centre, size, orientation, brightness and edge
softness; the expected frame is rasterised from the pose the plan gives and
measured the same way (canvas clipping included). A frame early or late
misplaces a slide by tens of pixels — the tolerance is 2.5 px.
"""
from __future__ import annotations

import math
import subprocess
from pathlib import Path

import numpy as np
import pytest

from clip_anim_lib import (FFMPEG, H, W, Blob, angle_diff, decode_y, make_box_source, measure)
from video_ai_editor.edl import clip_animations as A
from video_ai_editor.edl.schema import Canvas, Clip, Keyframe, Sticker, Track, Transform, empty_edl
from video_ai_editor.render import compositor

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")

CLIP_S = 2.0
DUR = 0.6
IN_IDS = A.PRESET_IDS["in"]
OUT_IDS = A.PRESET_IDS["out"]
COMBO_IDS = A.PRESET_IDS["combo"]
#: (anim_in, anim_out, anim_combo) per clip: In i paired with Out i, then the Combos
SPECS: list[tuple[str | None, str | None, str | None]] = (
    [(i, o, None) for i, o in zip(IN_IDS, OUT_IDS)] + [(None, None, c) for c in COMBO_IDS])

#: the white box inside each target, in its own frame (w, h): v1 draws the
#: 640x360 source full canvas; a PIP is 35 % of the long side (224x126) of
#: that source; a sticker is a 200x100 PNG with a 100x50 white core at 22 %
#: of the long side (140x70).
BOX = {"v1": (160.0, 60.0), "pip": (56.0, 21.0), "sticker": (70.0, 35.0)}
POS = {"v1": (W / 2, H / 2), "pip": (250.0, 190.0), "sticker": (380.0, 170.0)}


def _sticker_png(path: Path) -> Path:
    from PIL import Image
    im = Image.new("RGBA", (200, 100), (0, 0, 0, 0))
    im.paste((255, 255, 255, 255), (50, 25, 150, 75))
    im.save(path)
    return path


def _black(path: Path, fps: int, seconds: float) -> Path:
    subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"color=c=black:s={W}x{H}:r={fps}:d={seconds}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    str(path)], check=True, capture_output=True)
    return path


def _apply(obj, spec, dur_in=DUR, dur_out=DUR):
    a_in, a_out, a_combo = spec
    obj.anim_in, obj.anim_out, obj.anim_combo = a_in, a_out, a_combo
    obj.anim_dur = dur_in if a_in else None
    obj.anim_out_dur = dur_out if a_out else None


def build_edl(target: str, fps: int, root: Path, specs=SPECS, *, keyed: bool = False):
    """One timeline: a clip per spec, back to back, on `target`."""
    root.mkdir(parents=True, exist_ok=True)
    total = CLIP_S * len(specs)
    box = make_box_source(root / f"box{fps}.mp4", w=W, h=H, fps=fps, seconds=CLIP_S + 1)
    e = empty_edl(Canvas(w=W, h=H, fps=fps))
    e.canvas.loudness_lufs = None
    v1 = e.get_track("v1")
    objs = []
    if target == "v1":
        for i, spec in enumerate(specs):
            c = Clip(src=str(box), start=CLIP_S * i, id=f"a{i:02d}")
            c.in_, c.out = 0.0, CLIP_S
            if keyed:
                c.transform.x = Keyframe(keyframes=[(0.0, -60.0), (CLIP_S, 60.0)])
                c.transform.scale = Keyframe(keyframes=[(0.0, 0.9), (CLIP_S, 1.1)])
            _apply(c, spec)
            v1.clips.append(c)
            objs.append(c)
    else:
        bg = Clip(src=str(_black(root / f"black{fps}.mp4", fps, total + 1)), id="bg")
        bg.out = total
        v1.clips.append(bg)
        if target == "pip":
            lane = Track(id="v2", type="video", z=5, clips=[])
            e.tracks.append(lane)
            for i, spec in enumerate(specs):
                c = Clip(src=str(box), start=CLIP_S * i, id=f"p{i:02d}",
                         transform=Transform(x=POS["pip"][0], y=POS["pip"][1]))
                c.in_, c.out = 0.0, CLIP_S
                if keyed:
                    c.transform.rotation = Keyframe(keyframes=[(0.0, 0.0), (CLIP_S, 30.0)])
                _apply(c, spec)
                lane.clips.append(c)
                objs.append(c)
        else:
            png = _sticker_png(root / "sticker.png")
            lane = next(t for t in e.tracks if t.type == "sticker")
            for i, spec in enumerate(specs):
                s = Sticker(src=str(png), start=CLIP_S * i, end=CLIP_S * (i + 1), id=f"s{i:02d}",
                            transform=Transform(x=POS["sticker"][0], y=POS["sticker"][1]))
                if keyed:
                    s.transform.x = Keyframe(keyframes=[(0.0, 330.0), (CLIP_S, 430.0)])
                _apply(s, spec)
                lane.clips.append(s)
                objs.append(s)
    e.recompute_duration()
    return e, objs


def render(e, root: Path, name: str, fps: int, **kw) -> np.ndarray:
    out = compositor._render(e, root / f"{name}.mp4", height=H, fps=fps, preview=kw.get("preview", False),
                             cache_dir=root / "cache", chunked=kw.get("chunked", False))
    return decode_y(out)


def base_pose(target: str, obj, t: float) -> tuple[float, float, float, float]:
    """(cx, cy, scale, rotation) of the target's box at clip-local t, without
    the animation (the keyed pose)."""
    from video_ai_editor.edl.keyframes import sample
    tx = obj.transform
    if target == "v1":
        return W / 2 + sample(tx.x, t), H / 2 + sample(tx.y, t), sample(tx.scale, t), sample(tx.rotation, t)
    x = sample(tx.x, t) if isinstance(tx.x, Keyframe) else float(tx.x)
    y = sample(tx.y, t) if isinstance(tx.y, Keyframe) else float(tx.y)
    return x, y, sample(tx.scale, t), sample(tx.rotation, t)


def expected(target: str, obj, t: float) -> tuple[Blob | None, float, float, float]:
    """The expected measurement at clip-local t, the fade gain, the blur
    weight and the total scale."""
    window = obj.effective_duration if isinstance(obj, Clip) else obj.end - obj.start
    pl = A.plan_of(obj, window)
    cx, cy, s, r = base_pose(target, obj, t)
    cx += pl.value("x", t) * W
    cy += pl.value("y", t) * H
    s *= pl.value("scale", t)
    r += pl.value("rotation", t)
    gain = A.fade_gain(pl, t)
    bw, bh = BOX[target]
    ys, xs = np.mgrid[0:H, 0:W].astype(np.float64) + 0.5
    rad = math.radians(r)
    dx, dy = xs - cx, ys - cy
    lx = math.cos(rad) * dx + math.sin(rad) * dy
    ly = -math.sin(rad) * dx + math.cos(rad) * dy
    inside = (np.abs(lx) <= bw * s / 2) & (np.abs(ly) <= bh * s / 2)
    img = (inside * (255.0 * gain)).astype(np.uint8)
    return measure(img), gain, A.blur_weight(pl, t), s


def frames_to_check(spec, fps: int) -> list[int]:
    n = int(round(CLIP_S * fps))
    d = int(round(DUR * fps))
    if spec[2]:
        return sorted({0, n // 7, n // 4, n // 3, n // 2, (2 * n) // 3, n - 1})
    return sorted({0, d // 4, d // 2, (3 * d) // 4, d + 2, n // 2, n - d, n - d + d // 4, n - d + d // 2,
                   n - d + (3 * d) // 4, n - 1})


def compare(got: Blob | None, want: Blob | None, gain: float, blur: float, where: str,
            scale: float = 1.0) -> list[str]:
    bad: list[str] = []
    if want is None or want.lit < 30 or gain < 0.2:
        # an edge row or two may still show (a box sliding off the canvas
        # leaves its antialiased last row)
        if got is not None and got.lit > max(200, (want.lit if want else 0) * 3) and gain >= 0.2:
            bad.append(f"{where}: expected nothing visible, got {got}")
        return bad
    if got is None:
        return [f"{where}: nothing visible, expected {want}"]
    pos_tol = 2.5 + (12.0 if blur > 0.05 else 0.0)
    if abs(got.cx - want.cx) > pos_tol or abs(got.cy - want.cy) > pos_tol:
        bad.append(f"{where}: centre ({got.cx:.1f},{got.cy:.1f}) vs ({want.cx:.1f},{want.cy:.1f})")
    if blur < 0.05 and want.area >= 300:
        # edges: ±1 px per side (overlay snaps to the chroma grid, scalers
        # round to even), so the slack grows with the perimeter
        side = math.sqrt(want.area)
        if abs(got.area - want.area) > 0.08 * want.area + 3.0 * side:
            bad.append(f"{where}: area {got.area:.0f} vs {want.area:.0f}")
        if angle_diff(got.angle, want.angle) > 2.5:
            bad.append(f"{where}: angle {got.angle:.1f} vs {want.angle:.1f}")
    if blur < 0.05 and abs(got.level - 255 * gain) > 14:
        bad.append(f"{where}: level {got.level:.0f} vs {255 * gain:.0f}")
    if blur >= 0.6 and got.sharp > 0.55:
        bad.append(f"{where}: blur weight {blur:.2f} but edges are sharp ({got.sharp:.2f})")
    if blur == 0.0 and got.sharp < 0.6 and want.area >= 3000 and abs(scale - 1) < 1e-6:
        bad.append(f"{where}: no blur expected, edges soft ({got.sharp:.2f})")
    return bad


@pytest.fixture(scope="module")
def renders(tmp_path_factory):
    cache: dict = {}

    def get(target: str, fps: int, keyed: bool = False):
        key = (target, fps, keyed)
        if key not in cache:
            root = tmp_path_factory.mktemp(f"anim-{target}-{fps}-{int(keyed)}")
            specs = SPECS if not keyed else [(None, None, "rock"), ("slide_up", "zoom_in", None),
                                             ("zoom_out", "slide_left", None)]
            e, objs = build_edl(target, fps, root, specs, keyed=keyed)
            cache[key] = (e, objs, render(e, root, "export", fps), specs)
        return cache[key]
    return get


@pytest.mark.parametrize("fps", [25, 30])
@pytest.mark.parametrize("target", ["v1", "pip", "sticker"])
def test_every_preset_renders_its_own_value_on_every_frame(renders, target, fps):
    e, objs, ys, specs = renders(target, fps)
    n = int(round(CLIP_S * fps))
    assert len(ys) == n * len(specs), (len(ys), n * len(specs))
    bad: list[str] = []
    for i, (spec, obj) in enumerate(zip(specs, objs)):
        for k in frames_to_check(spec, fps):
            t = k / fps
            want, gain, blur, sc = expected(target, obj, t)
            got = measure(ys[i * n + k])
            bad += compare(got, want, gain, blur, f"{target}@{fps} {spec} k={k}", sc)
    assert not bad, "\n".join(bad[:30])


@pytest.mark.parametrize("fps", [25, 30])
@pytest.mark.parametrize("target", ["v1", "pip", "sticker"])
def test_animation_composes_with_keyframes(renders, target, fps):
    """An animation rides ON TOP of the keyed pose, on the keys' own clock."""
    e, objs, ys, specs = renders(target, fps, keyed=True)
    n = int(round(CLIP_S * fps))
    bad: list[str] = []
    for i, (spec, obj) in enumerate(zip(specs, objs)):
        for k in frames_to_check(spec, fps):
            want, gain, blur, sc = expected(target, obj, k / fps)
            bad += compare(measure(ys[i * n + k]), want, gain, blur, f"{target}@{fps} keyed {spec} k={k}", sc)
    assert not bad, "\n".join(bad[:30])


def test_a_frame_late_would_be_caught(renders):
    """The tolerance is tight enough: the value one frame later misses."""
    e, objs, ys, specs = renders("v1", 30)
    i = SPECS.index(("slide_left", "slide_left", None))
    obj = objs[i]
    n = int(round(CLIP_S * 30))
    k = int(round(DUR * 30)) // 2
    want_now, g, b, _s = expected("v1", obj, k / 30)
    want_late, _, _, _ = expected("v1", obj, (k + 1) / 30)
    got = measure(ys[i * n + k])
    assert compare(got, want_now, g, b, "now") == []
    assert compare(got, want_late, g, b, "late") != []


def test_blur_mix_softens_then_clears(renders):
    e, objs, ys, specs = renders("v1", 30)
    n = 60
    i = SPECS.index(("blur_in", "blur_out", None))
    sharp = [measure(ys[i * n + k]).sharp for k in range(n)]
    assert sharp[0] < 0.2 and sharp[20] > 0.95 and sharp[30] > 0.95 and sharp[n - 1] < 0.35, sharp
    # monotone in, monotone out
    assert all(b >= a - 0.02 for a, b in zip(sharp[:18], sharp[1:18])), sharp[:18]
    assert all(b <= a + 0.02 for a, b in zip(sharp[42:], sharp[43:])), sharp[42:]


def test_v1_chunked_preview_matches_the_export(tmp_path):
    """The chunk renderer bakes the same animation (its key includes it)."""
    specs = [("zoom_in", "slide_down", None), (None, None, "shake"), ("blur_in", "fade_out", None)]
    e, objs = build_edl("v1", 30, tmp_path, specs)
    exp = render(e, tmp_path, "export", 30)
    pre = render(e, tmp_path, "chunked", 30, preview=True, chunked=True)
    assert len(pre) == len(exp)
    mse = float(np.mean((pre.astype(np.float64) - exp.astype(np.float64)) ** 2))
    psnr = 99.0 if mse == 0 else 10 * math.log10(255 ** 2 / mse)
    assert psnr > 38, psnr


def test_filter_text_of_a_clip_without_animation_is_unchanged():
    """No animation → byte-identical filter text (and the model omits the
    fields): the chain for a plain, a keyed and a faded clip."""
    from video_ai_editor.render.compositor import _build_clip_video_chain
    for kw in ({}, {"video_fade_in": 0.4}):
        c = Clip(src="/nonexistent.mp4", out=2.0, id="c_x", **kw)
        ch = _build_clip_video_chain(c, input_label="[0:v]", label_out="[v]", canvas_w=W, canvas_h=H, fps=30)
        assert "gblur" not in ch and "split" not in ch
        assert "anim" not in c.model_dump_json()
    c = Clip(src="/nonexistent.mp4", out=2.0, id="c_x")
    c.transform.x = Keyframe(keyframes=[(0.0, 0.0), (1.0, 50.0)])
    plain = _build_clip_video_chain(c, input_label="[0:v]", label_out="[v]", canvas_w=W, canvas_h=H, fps=30)
    c.anim_in = "fade_in"
    c.anim_in = None
    again = _build_clip_video_chain(c, input_label="[0:v]", label_out="[v]", canvas_w=W, canvas_h=H, fps=30)
    assert plain == again


@pytest.mark.parametrize("fps", [25, 30])
def test_split_keeps_in_left_and_out_right_in_the_render(tmp_path, fps):
    """A split through an animated clip renders its In on the LEFT piece only
    and its Out on the RIGHT piece only — nothing animates at the seam."""
    from video_ai_editor.agent.dispatch import dispatch
    from video_ai_editor.edl import EDLStore
    e, objs = build_edl("v1", fps, tmp_path, [("zoom_in", "slide_down", None)])
    (tmp_path / "sess").mkdir()
    (tmp_path / "sess" / "edl.json").write_text(e.model_dump_json(by_alias=True))
    st = EDLStore(tmp_path / "sess")
    r = dispatch(st, "split_at", {"track": "v1", "time": 1.0})
    left, right = st.edl.get_clip(objs[0].id)[1], st.edl.get_clip(r["halves"][objs[0].id])[1]
    assert (left.anim_in, left.anim_out, right.anim_in, right.anim_out) == ("zoom_in", None, None, "slide_down")
    ys = render(st.edl, tmp_path, "split", fps)
    n = int(round(CLIP_S * fps))
    assert len(ys) == n
    half = n // 2
    bad: list[str] = []
    # the left piece: its In at its start, at rest up to the seam
    for k in (0, 2, int(DUR * fps) // 2, half - 2, half - 1):
        want, g, b, sc = expected("v1", left, k / fps)
        bad += compare(measure(ys[k]), want, g, b, f"left k={k}", sc)
    # the right piece: at rest from the seam, its Out at its end
    for k in (half, half + 1, half + 3, n - int(DUR * fps) // 2, n - 1):
        want, g, b, sc = expected("v1", right, (k - half) / fps)
        bad += compare(measure(ys[k]), want, g, b, f"right k={k}", sc)
    rest, *_ = expected("v1", left, 0.9)
    for k in (half - 1, half, half + 1):
        got = measure(ys[k])
        assert abs(got.cx - rest.cx) < 1.5 and abs(got.cy - rest.cy) < 1.5 and abs(got.area / rest.area - 1) < 0.05, (k, got)
    assert not bad, "\n".join(bad)
