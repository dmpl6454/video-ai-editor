"""agent/dispatch.py — text/sticker overlays follow the v1 timeline on
cut/trim/delete (R4).

Regression coverage for issues 31/32/50 (docs/superpowers/plans/
2026-07-10-editor-issues-verification-and-fixes.md): cut_range, ripple_delete,
and trim_clip used to re-time only the video track they mutate. A Sticker or
TextClip is pinned to absolute timeline seconds, so shortening the footage
left every overlay at its OLD absolute time — sitting past the now-shorter
content, or drifted onto unrelated footage ("emoji popped up at the end that
was never added there").

Each test builds an EDL with one v1 clip plus one overlay (sticker or text)
at a known position, performs the mutation, and asserts the overlay's
start/end followed the same left-shift the video content underwent.
"""
from __future__ import annotations
import tempfile
from pathlib import Path

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Canvas, Track, Clip, Sticker, TextClip, Transform
from video_ai_editor.agent.dispatch import dispatch


def _store_with_clip_and_sticker(clip_duration: float, sticker_start: float, sticker_end: float,
                                  clip_start: float = 0.0) -> EDLStore:
    tmp = tempfile.mkdtemp()
    src = str(Path(tmp) / "nonexistent" / "x.mp4")
    edl = EDL(
        canvas=Canvas(w=1080, h=1920, fps=30),
        tracks=[
            Track(id="v1", type="video", clips=[
                Clip(src=src, in_=0.0, out=clip_duration, start=clip_start, id="c1"),
            ]),
            Track(id="stickers", type="sticker", z=11, clips=[
                Sticker(id="s1", src=src, start=sticker_start, end=sticker_end,
                        transform=Transform(x=100, y=100)),
            ]),
        ],
    )
    edl.recompute_duration()
    (Path(tmp) / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(Path(tmp))


def _sticker(store: EDLStore) -> Sticker:
    t = store.edl.get_track("stickers")
    return t.clips[0]


# ---------- cut_range ----------

def test_cut_range_shifts_sticker_after_the_cut():
    # 10s clip [0,10). Cut out the middle [3,6) -> ripple leaves [0,7).
    # Sticker at [7,9) (after the cut) must shift left by (6-3)=3 -> [4,6).
    store = _store_with_clip_and_sticker(clip_duration=10.0, sticker_start=7.0, sticker_end=9.0)
    dispatch(store, "cut_range", {"track": "v1", "start": 3.0, "end": 6.0})
    s = _sticker(store)
    assert abs(s.start - 4.0) < 1e-6
    assert abs(s.end - 6.0) < 1e-6


def test_cut_range_leaves_sticker_before_the_cut_unchanged():
    store = _store_with_clip_and_sticker(clip_duration=10.0, sticker_start=1.0, sticker_end=2.0)
    dispatch(store, "cut_range", {"track": "v1", "start": 5.0, "end": 8.0})
    s = _sticker(store)
    assert abs(s.start - 1.0) < 1e-6
    assert abs(s.end - 2.0) < 1e-6


def test_cut_range_collapses_sticker_inside_the_cut_to_the_cut_point():
    # Sticker entirely inside [3,6) has no surviving footage to sit on;
    # collapse to the cut point rather than leaving it at a now-meaningless
    # absolute time.
    store = _store_with_clip_and_sticker(clip_duration=10.0, sticker_start=4.0, sticker_end=5.0)
    dispatch(store, "cut_range", {"track": "v1", "start": 3.0, "end": 6.0})
    s = _sticker(store)
    assert abs(s.start - 3.0) < 1e-6
    assert s.end > s.start  # kept a minimum visible span, didn't invert


def test_cut_range_on_non_v1_track_does_not_ripple_overlays():
    """Cutting a PIP/v2 clip is a secondary-layer edit — it must not shift
    the primary overlay timeline."""
    tmp = tempfile.mkdtemp()
    src = str(Path(tmp) / "nonexistent" / "x.mp4")
    edl = EDL(
        canvas=Canvas(w=1080, h=1920, fps=30),
        tracks=[
            Track(id="v1", type="video", clips=[Clip(src=src, in_=0, out=10, start=0, id="c1")]),
            Track(id="v2", type="video", z=1, clips=[Clip(src=src, in_=0, out=10, start=0, id="c2")]),
            Track(id="stickers", type="sticker", z=11, clips=[
                Sticker(id="s1", src=src, start=7.0, end=9.0, transform=Transform(x=100, y=100)),
            ]),
        ],
    )
    edl.recompute_duration()
    (Path(tmp) / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(Path(tmp))
    dispatch(store, "cut_range", {"track": "v2", "start": 3.0, "end": 6.0})
    s = _sticker(store)
    assert abs(s.start - 7.0) < 1e-6
    assert abs(s.end - 9.0) < 1e-6


# ---------- ripple_delete ----------

def test_ripple_delete_of_v1_clip_shifts_sticker_after_it():
    tmp = tempfile.mkdtemp()
    src = str(Path(tmp) / "nonexistent" / "x.mp4")
    # Two v1 clips: c1 [0,5), c2 [5,15). Sticker at [8,9), inside c2.
    # Deleting c1 removes [0,5) and everything ripples left by 5 ->
    # sticker should land at [3,4).
    edl = EDL(
        canvas=Canvas(w=1080, h=1920, fps=30),
        tracks=[
            Track(id="v1", type="video", clips=[
                Clip(src=src, in_=0, out=5, start=0, id="c1"),
                Clip(src=src, in_=0, out=10, start=5, id="c2"),
            ]),
            Track(id="stickers", type="sticker", z=11, clips=[
                Sticker(id="s1", src=src, start=8.0, end=9.0, transform=Transform(x=100, y=100)),
            ]),
        ],
    )
    edl.recompute_duration()
    (Path(tmp) / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(Path(tmp))
    dispatch(store, "ripple_delete", {"clip_id": "c1"})
    s = _sticker(store)
    assert abs(s.start - 3.0) < 1e-6
    assert abs(s.end - 4.0) < 1e-6


def test_ripple_delete_of_a_sticker_does_not_shift_other_overlays():
    """Deleting the sticker ITSELF (not a v1 clip) must not ripple anything
    else — only a v1 media-clip deletion ripples overlays."""
    tmp = tempfile.mkdtemp()
    src = str(Path(tmp) / "nonexistent" / "x.mp4")
    edl = EDL(
        canvas=Canvas(w=1080, h=1920, fps=30),
        tracks=[
            Track(id="v1", type="video", clips=[Clip(src=src, in_=0, out=10, start=0, id="c1")]),
            Track(id="stickers", type="sticker", z=11, clips=[
                Sticker(id="s1", src=src, start=1.0, end=2.0, transform=Transform(x=100, y=100)),
                Sticker(id="s2", src=src, start=7.0, end=8.0, transform=Transform(x=100, y=100)),
            ]),
        ],
    )
    edl.recompute_duration()
    (Path(tmp) / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(Path(tmp))
    dispatch(store, "ripple_delete", {"clip_id": "s1"})
    remaining = store.edl.get_track("stickers").clips
    assert len(remaining) == 1
    assert abs(remaining[0].start - 7.0) < 1e-6
    assert abs(remaining[0].end - 8.0) < 1e-6


# ---------- trim_clip ----------

def test_trim_clip_from_the_back_shifts_only_overlays_after_the_trimmed_tail():
    # Clip at start=5, duration=10 -> footprint [5,15). Trim OUT so
    # new_duration=6 -> footprint becomes [5,11). The removed tail is
    # [11,15) (removed_start = old_start + new_duration = 11, shift = 4).
    # Sticker at [16,17) is genuinely AFTER the removed tail (not inside
    # it) and must shift left by 4 -> [12,13).
    store = _store_with_clip_and_sticker(clip_duration=10.0, sticker_start=16.0, sticker_end=17.0,
                                          clip_start=5.0)
    dispatch(store, "trim_clip", {"clip_id": "c1", "out": 6.0})
    s = _sticker(store)
    assert abs(s.start - 12.0) < 1e-6
    assert abs(s.end - 13.0) < 1e-6


def test_trim_clip_from_the_back_collapses_overlay_inside_the_removed_tail():
    # Sticker at [13,14) sits INSIDE the removed [11,15) tail — it has no
    # surviving footage under it, so it collapses to the cut point (11),
    # matching cut_range's "inside the cut" behavior.
    store = _store_with_clip_and_sticker(clip_duration=10.0, sticker_start=13.0, sticker_end=14.0,
                                          clip_start=5.0)
    dispatch(store, "trim_clip", {"clip_id": "c1", "out": 6.0})
    s = _sticker(store)
    assert abs(s.start - 11.0) < 1e-6
    assert s.end > s.start


def test_trim_clip_from_the_back_leaves_overlays_before_the_trim_unchanged():
    store = _store_with_clip_and_sticker(clip_duration=10.0, sticker_start=6.0, sticker_end=7.0,
                                          clip_start=5.0)
    dispatch(store, "trim_clip", {"clip_id": "c1", "out": 6.0})
    s = _sticker(store)
    assert abs(s.start - 6.0) < 1e-6
    assert abs(s.end - 7.0) < 1e-6


def test_trim_clip_from_the_front_shifts_overlays_after_the_trimmed_head():
    # Clip at start=5, in_=0, out=10 -> duration=10, footprint [5,15).
    # Trim IN to 4 -> new_duration=6, footprint conceptually [5,11) once
    # ripple repacks. The removed HEAD is [5,9) (removed_start=old_start=5,
    # removed_len = 10-6=4). A sticker at [10,11) sits after that removed
    # head and must shift left by 4 -> [6,7).
    store = _store_with_clip_and_sticker(clip_duration=10.0, sticker_start=10.0, sticker_end=11.0,
                                          clip_start=5.0)
    dispatch(store, "trim_clip", {"clip_id": "c1", "in": 4.0})
    s = _sticker(store)
    assert abs(s.start - 6.0) < 1e-6
    assert abs(s.end - 7.0) < 1e-6


def test_trim_clip_growing_out_does_not_ripple_overlays():
    """Extending a trim (out increases, duration grows) doesn't remove any
    timeline content — nothing should shift."""
    store = _store_with_clip_and_sticker(clip_duration=5.0, sticker_start=7.0, sticker_end=8.0,
                                          clip_start=0.0)
    dispatch(store, "trim_clip", {"clip_id": "c1", "out": 8.0})  # 5 -> 8, grows
    s = _sticker(store)
    assert abs(s.start - 7.0) < 1e-6
    assert abs(s.end - 8.0) < 1e-6


def test_trim_clip_on_v2_does_not_ripple_overlays():
    tmp = tempfile.mkdtemp()
    src = str(Path(tmp) / "nonexistent" / "x.mp4")
    edl = EDL(
        canvas=Canvas(w=1080, h=1920, fps=30),
        tracks=[
            Track(id="v1", type="video", clips=[Clip(src=src, in_=0, out=10, start=0, id="c1")]),
            Track(id="v2", type="video", z=1, clips=[Clip(src=src, in_=0, out=10, start=0, id="c2")]),
            Track(id="stickers", type="sticker", z=11, clips=[
                Sticker(id="s1", src=src, start=7.0, end=9.0, transform=Transform(x=100, y=100)),
            ]),
        ],
    )
    edl.recompute_duration()
    (Path(tmp) / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(Path(tmp))
    dispatch(store, "trim_clip", {"clip_id": "c2", "out": 3.0})
    s = _sticker(store)
    assert abs(s.start - 7.0) < 1e-6
    assert abs(s.end - 9.0) < 1e-6


# ---------- text clips ripple the same way ----------

def test_text_clip_ripples_the_same_way_as_a_sticker():
    tmp = tempfile.mkdtemp()
    src = str(Path(tmp) / "nonexistent" / "x.mp4")
    edl = EDL(
        canvas=Canvas(w=1080, h=1920, fps=30),
        tracks=[
            Track(id="v1", type="video", clips=[Clip(src=src, in_=0, out=10, start=0, id="c1")]),
            Track(id="text", type="text", z=10, clips=[
                TextClip(id="t1", text="HELLO", start=7.0, end=9.0,
                          transform=Transform(x=100, y=100), role="super"),
            ]),
        ],
    )
    edl.recompute_duration()
    (Path(tmp) / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(Path(tmp))
    dispatch(store, "cut_range", {"track": "v1", "start": 3.0, "end": 6.0})
    text = store.edl.get_track("text").clips[0]
    assert abs(text.start - 4.0) < 1e-6
    assert abs(text.end - 6.0) < 1e-6
