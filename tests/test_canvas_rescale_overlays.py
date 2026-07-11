"""agent/dispatch.py — clip/sticker/text transforms rescale on a canvas
change (R6, issue 37).

Regression coverage: set_aspect_ratio/set_canvas used to only touch
canvas.w/h. Every transform's x/y is an ABSOLUTE canvas-pixel coordinate
(text_overlay.py reads tx.x/tx.y directly), so switching e.g. 9:16
(1080x1920) to 16:9 (1920x1080) left a sticker placed at y=1600 (fine in a
1920-tall canvas) sitting far below the now-1080-tall canvas — invisible,
reported as "emojis vanished after changing aspect ratio".
"""
from __future__ import annotations
import tempfile
from pathlib import Path

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Canvas, Track, Clip, Sticker, Transform, Keyframe
from video_ai_editor.agent.dispatch import dispatch


def _store_with_sticker_at(x: float, y: float, canvas_w=1080, canvas_h=1920) -> EDLStore:
    tmp = tempfile.mkdtemp()
    src = str(Path(tmp) / "nonexistent" / "x.mp4")
    edl = EDL(
        canvas=Canvas(w=canvas_w, h=canvas_h, fps=30),
        tracks=[
            Track(id="v1", type="video", clips=[
                Clip(src=src, in_=0, out=10, start=0, id="c1"),
            ]),
            Track(id="stickers", type="sticker", z=11, clips=[
                Sticker(id="s1", src=src, start=0.0, end=2.0,
                        transform=Transform(x=x, y=y)),
            ]),
        ],
    )
    edl.recompute_duration()
    (Path(tmp) / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(Path(tmp))


def _sticker(store: EDLStore) -> Sticker:
    return store.edl.get_track("stickers").clips[0]


def test_set_aspect_ratio_rescales_sticker_position_proportionally():
    # Sticker at 80% across, 83% down a 1080x1920 canvas -> (864, 1600).
    store = _store_with_sticker_at(864, 1600, canvas_w=1080, canvas_h=1920)
    dispatch(store, "set_aspect_ratio", {"ratio": "16:9"})  # -> 1920x1080
    s = _sticker(store)
    # Same RELATIVE position in the new 1920x1080 canvas: x*=(1920/1080),
    # y*=(1080/1920).
    assert abs(s.transform.x - 864 * (1920 / 1080)) < 1e-6
    assert abs(s.transform.y - 1600 * (1080 / 1920)) < 1e-6
    assert store.edl.canvas.w == 1920 and store.edl.canvas.h == 1080


def test_set_aspect_ratio_keeps_sticker_within_frame_after_the_switch():
    """The core visible-symptom check: a sticker positioned near the bottom of
    a tall canvas must NOT end up below the bottom edge of a short one."""
    store = _store_with_sticker_at(540, 1750, canvas_w=1080, canvas_h=1920)  # near bottom
    dispatch(store, "set_aspect_ratio", {"ratio": "16:9"})  # -> 1920x1080
    s = _sticker(store)
    assert 0 <= s.transform.y <= store.edl.canvas.h
    assert 0 <= s.transform.x <= store.edl.canvas.w


def test_set_canvas_rescales_sticker_position():
    store = _store_with_sticker_at(500, 1000, canvas_w=1000, canvas_h=1000)
    dispatch(store, "set_canvas", {"w": 2000, "h": 500})
    s = _sticker(store)
    assert abs(s.transform.x - 1000) < 1e-6  # 500 * (2000/1000)
    assert abs(s.transform.y - 500) < 1e-6    # 1000 * (500/1000)


def test_canvas_change_with_no_dimension_change_is_a_noop():
    store = _store_with_sticker_at(540, 960, canvas_w=1080, canvas_h=1920)
    dispatch(store, "set_canvas", {"fps": 24})  # w/h unchanged
    s = _sticker(store)
    assert abs(s.transform.x - 540) < 1e-6
    assert abs(s.transform.y - 960) < 1e-6


def test_video_clip_transform_also_rescales():
    """Not just overlays — a v1/PIP clip's own transform.x/y is also an
    absolute canvas coordinate and must rescale the same way."""
    tmp = tempfile.mkdtemp()
    src = str(Path(tmp) / "nonexistent" / "x.mp4")
    edl = EDL(
        canvas=Canvas(w=1080, h=1920, fps=30),
        tracks=[
            Track(id="v1", type="video", clips=[
                Clip(src=src, in_=0, out=10, start=0, id="c1"),
            ]),
            Track(id="v2", type="video", z=1, clips=[
                Clip(src=src, in_=0, out=10, start=0, id="c2",
                     transform=Transform(x=800, y=1400, scale=0.5)),
            ]),
        ],
    )
    edl.recompute_duration()
    (Path(tmp) / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(Path(tmp))

    dispatch(store, "set_aspect_ratio", {"ratio": "1:1"})  # -> 1080x1080
    pip = store.edl.get_track("v2").clips[0]
    assert abs(pip.transform.x - 800 * (1080 / 1080)) < 1e-6  # sx=1, unchanged
    assert abs(pip.transform.y - 1400 * (1080 / 1920)) < 1e-6
    # scale is a size multiplier, not a position — must NOT be touched by
    # the position rescale.
    assert abs(pip.transform.scale - 0.5) < 1e-9


def test_keyframed_sticker_position_rescales_every_keyframe():
    tmp = tempfile.mkdtemp()
    src = str(Path(tmp) / "nonexistent" / "x.mp4")
    edl = EDL(
        canvas=Canvas(w=1080, h=1920, fps=30),
        tracks=[
            Track(id="v1", type="video", clips=[Clip(src=src, in_=0, out=10, start=0, id="c1")]),
            Track(id="stickers", type="sticker", z=11, clips=[
                Sticker(id="s1", src=src, start=0.0, end=2.0,
                        transform=Transform(
                            x=Keyframe(keyframes=[(0.0, 200.0), (1.0, 800.0)]),
                            y=1000.0,
                        )),
            ]),
        ],
    )
    edl.recompute_duration()
    (Path(tmp) / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(Path(tmp))

    dispatch(store, "set_aspect_ratio", {"ratio": "16:9"})  # -> 1920x1080
    s = _sticker(store)
    sx = 1920 / 1080
    assert isinstance(s.transform.x, Keyframe)
    assert abs(s.transform.x.keyframes[0][1] - 200.0 * sx) < 1e-6
    assert abs(s.transform.x.keyframes[1][1] - 800.0 * sx) < 1e-6
    assert abs(s.transform.y - 1000.0 * (1080 / 1920)) < 1e-6


def test_auto_reframe_rescales_overlay_positions():
    """A sticker at a given (x,y) must be proportionally repositioned when
    auto_reframe changes the canvas dimensions, not left at old coords."""
    store = _store_with_sticker_at(540, 1440, canvas_w=1080, canvas_h=1920)
    old_w, old_h = store.edl.canvas.w, store.edl.canvas.h
    old_x = _sticker(store).transform.x
    # subject_track=False: skip MediaPipe reframing (fixture uses a
    # nonexistent src path) — this test is only about the canvas-size
    # bookkeeping, not the subject-tracked crop.
    dispatch(store, "auto_reframe", {"ratio": "16:9", "subject_track": False})  # -> 1920x1080
    new_w = store.edl.canvas.w
    s = _sticker(store)
    # x should scale by new_w/old_w
    assert abs(s.transform.x - old_x * (new_w / old_w)) < 1e-3
    assert abs(s.transform.y - 1440 * (1080 / 1920)) < 1e-6
