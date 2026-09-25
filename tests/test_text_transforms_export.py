"""QA-036: text x/y keyframes, rotation and scale must reach the export.

`add_keyframe` / `set_property` / `set_clip_transform` accepted all of them on
a TextClip and reported success, but the rasteriser never applied rotation or
scale and a keyframed x/y resolved to "no override" — the text sat centred,
upright, 1.0x. Real exports, measured per frame.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

from video_ai_editor.edl.keyframes import sample
from video_ai_editor.edl.schema import Keyframe, TextClip, TextStyle, Transform
from video_ai_editor.render import render_export

sys.path.insert(0, str(Path(__file__).parent))
from overlay_render_helpers import base_edl, bbox, centroid, frame_at, mask_of  # noqa: E402

W, H, FPS = 960, 540, 30
RED = (255, 0, 0)


def _red(tmp: Path, text: str, **tx) -> TextClip:
    return TextClip(id="t1", text=text, start=0.0, end=4.0, role=None,
                    style=TextStyle(color="#FF0000", size=60), transform=Transform(**tx))


def _export(tmp: Path, clip: TextClip) -> Path:
    edl = base_edl(tmp, W, H, 4.0)
    edl.get_track("tx").clips.append(clip)
    edl.recompute_duration()
    return render_export(edl, tmp, height=H).path


def _principal_angle(mask: np.ndarray) -> float:
    """Orientation of the ink's long axis, degrees clockwise from +x (y down)."""
    ys, xs = np.nonzero(mask)
    xs = xs - xs.mean()
    ys = ys - ys.mean()
    cov = np.cov(np.vstack([xs, ys]))
    evals, evecs = np.linalg.eigh(cov)
    vx, vy = evecs[:, int(np.argmax(evals))]
    ang = math.degrees(math.atan2(vy, vx))
    return (ang + 90) % 180 - 90


def test_static_rotation_and_scale_reach_export(tmp_path: Path):
    plain = _export(tmp_path / "a", _red(tmp_path, "WIDE TEXT LINE", x=480, y=270))
    turned = _export(tmp_path / "b", _red(tmp_path, "WIDE TEXT LINE", x=480, y=270,
                                          rotation=30, scale=1.5))
    m0 = mask_of(frame_at(plain, 2.0, FPS), RED)
    m1 = mask_of(frame_at(turned, 2.0, FPS), RED)
    assert abs(_principal_angle(m0)) < 3
    assert abs(_principal_angle(m1) - 30) < 4, _principal_angle(m1)
    # 1.5x the ink (area scales with k^2 = 2.25).
    assert 1.9 < m1.sum() / m0.sum() < 2.6, (m0.sum(), m1.sum())
    # Still centred on its anchor.
    cx, cy = centroid(m1)
    assert abs(cx - 480) < 12 and abs(cy - 270) < 12


def test_keyframed_x_slides_text_in_export(tmp_path: Path):
    kf = Keyframe(keyframes=[(0.0, 200.0), (3.5, 760.0)])
    out = _export(tmp_path, _red(tmp_path, "MOVING", x=kf, y=270))
    xs = []
    for t in (0.5, 1.75, 3.0):
        c = centroid(mask_of(frame_at(out, t, FPS), RED))
        assert c is not None, t
        xs.append(c[0])
        assert abs(c[0] - sample(kf, t)) < 10, (t, c[0], sample(kf, t))
        assert abs(c[1] - 270) < 10
    assert xs[0] < xs[1] < xs[2]


def test_keyframed_scale_and_rotation_animate_in_export(tmp_path: Path):
    sk = Keyframe(keyframes=[(0.0, 0.5), (3.0, 1.5)])
    rk = Keyframe(keyframes=[(0.0, 0.0), (3.0, 45.0)])
    out = _export(tmp_path, _red(tmp_path, "SPIN TEXT", x=480, y=270, scale=sk, rotation=rk))
    early = mask_of(frame_at(out, 0.3, FPS), RED)
    late = mask_of(frame_at(out, 2.9, FPS), RED)
    assert late.sum() > early.sum() * 4          # (1.47/0.55)^2 ~ 7
    assert abs(_principal_angle(early) - 4.5) < 5
    assert abs(_principal_angle(late) - 43.5) < 6
    b = bbox(late)
    assert b is not None and abs((b[0] + b[2]) / 2 - 480) < 15


# ---------- dispatch: accepted means rendered; caption geometry is refused ----------

def _store_with_text(tmp: Path, role):
    from video_ai_editor.edl import EDLStore
    edl = base_edl(tmp, W, H, 4.0)
    edl.get_track("tx").clips.append(TextClip(id="t1", text="HI", start=0, end=4, role=role))
    edl.recompute_duration()
    (tmp / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(tmp)


def test_caption_geometry_is_refused_not_silently_ignored(tmp_path: Path):
    import pytest
    from video_ai_editor.agent.dispatch import dispatch
    store = _store_with_text(tmp_path, "caption")
    for tool, args in [
        ("set_clip_transform", {"clip_id": "t1", "rotation": 30}),
        ("add_keyframe", {"clip_id": "t1", "prop": "x", "time": 0.0, "value": 300}),
        ("set_property", {"clip_id": "t1", "path": "transform.scale", "value": 1.5}),
    ]:
        with pytest.raises(ValueError, match="captions style"):
            dispatch(store, tool, args)
    # Opacity is honoured by both renderers, so it stays allowed.
    dispatch(store, "set_clip_transform", {"clip_id": "t1", "opacity": 0.5})


def test_text_keyframes_via_dispatch_reach_the_export(tmp_path: Path):
    from video_ai_editor.agent.dispatch import dispatch
    store = _store_with_text(tmp_path, None)
    dispatch(store, "set_property", {"clip_id": "t1", "path": "style.color", "value": "#FF0000"})
    dispatch(store, "add_keyframe", {"clip_id": "t1", "prop": "x", "time": 0.0, "value": 200})
    dispatch(store, "add_keyframe", {"clip_id": "t1", "prop": "x", "time": 3.0, "value": 760})
    out = render_export(store.edl, tmp_path, height=H).path
    early = centroid(mask_of(frame_at(out, 0.2, FPS), RED))
    late = centroid(mask_of(frame_at(out, 2.8, FPS), RED))
    assert early is not None and late is not None
    assert late[0] - early[0] > 400, (early, late)
