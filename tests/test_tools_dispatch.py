"""Tools dispatch — verify each mutation produces the expected EDL diff + ops entry."""
import tempfile
from pathlib import Path
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Clip
from video_ai_editor.agent.dispatch import dispatch


def _store_with_one_clip() -> EDLStore:
    tmp = tempfile.mkdtemp()
    store = EDLStore(Path(tmp))
    dispatch(store, "add_clip", {
        "track": "v1", "src": str(Path(tmp) / "nonexistent" / "x.mp4"),
        "in": 0.0, "out": 10.0, "start": 0.0,
    })
    return store


def test_add_clip_creates_clip_and_op():
    store = _store_with_one_clip()
    assert len(store.edl.tracks[0].clips) == 1
    assert store.ops.last().tool == "add_clip"


def test_cut_range_removes_inner_segment():
    store = _store_with_one_clip()
    # cut 3..6 from a single 10s clip → expect two clips: [0..3] and [6..10],
    # ripple-collapsed to [0..3] then [3..7] (start at 3 because we close the gap).
    dispatch(store, "cut_range", {"track": "v1", "start": 3.0, "end": 6.0})
    clips = store.edl.tracks[0].clips
    assert len(clips) == 2
    assert abs(clips[0].duration - 3.0) < 1e-6
    assert abs(clips[1].duration - 4.0) < 1e-6
    assert abs(clips[1].start - 3.0) < 1e-6  # ripple closed the 3s gap


def test_split_at_produces_two_clips():
    store = _store_with_one_clip()
    dispatch(store, "split_at", {"track": "v1", "time": 4.0})
    clips = store.edl.tracks[0].clips
    assert len(clips) == 2
    assert abs(clips[0].duration - 4.0) < 1e-6
    assert abs(clips[1].duration - 6.0) < 1e-6


def test_ripple_delete_then_undo():
    store = _store_with_one_clip()
    cid = store.edl.tracks[0].clips[0].id
    dispatch(store, "ripple_delete", {"clip_id": cid})
    assert len(store.edl.tracks[0].clips) == 0
    dispatch(store, "undo", {})
    assert len(store.edl.tracks[0].clips) == 1


def test_set_aspect_ratio_changes_canvas():
    store = _store_with_one_clip()
    dispatch(store, "set_aspect_ratio", {"ratio": "16:9"})
    assert store.edl.canvas.w == 1920
    assert store.edl.canvas.h == 1080


def _add_media_clip(store: EDLStore, track: str, start: float, dur: float) -> str:
    tmp = Path(store.dir)
    res = dispatch(store, "add_clip", {
        "track": track, "src": str(tmp / "nonexistent" / "y.mp4"),
        "in": 0.0, "out": dur, "start": start,
    })
    return res["clip_id"]


def test_move_clip_onto_occupied_range_snaps_to_free_gap():
    # a1 has a clip at [0, 10). vo has an existing (voiceover) clip at [0, 5)
    # — mirrors "Main audio" clip dragged onto the "Voiceover" row where a
    # real recorded VO already sits.
    store = _store_with_one_clip()  # v1 clip [0,10) — unrelated track
    a1_id = _add_media_clip(store, "a1", 0.0, 10.0)
    _add_media_clip(store, "vo", 0.0, 5.0)

    dispatch(store, "move_clip", {"clip_id": a1_id, "new_start": 1.0, "new_track": "vo"})

    vo_track = store.edl.get_track("vo")
    clips = sorted(vo_track.clips, key=lambda c: c.start)
    assert len(clips) == 2
    # Both clips still exist and do NOT overlap in time.
    a, b = clips[0], clips[1]
    assert a.start + a.duration <= b.start + 1e-6
    # The pre-existing vo clip [0,5) is untouched; the dropped a1 clip (10s
    # long) snapped to the first free gap at-or-after the requested time.
    assert abs(a.start - 0.0) < 1e-6
    assert abs(a.duration - 5.0) < 1e-6
    assert abs(b.start - 5.0) < 1e-6


def test_move_clip_onto_free_space_is_unaffected():
    store = _store_with_one_clip()
    _add_media_clip(store, "vo", 0.0, 5.0)
    a1_id = _add_media_clip(store, "a1", 0.0, 3.0)

    # Moving into a1's own track, to a spot with no overlap, should land
    # exactly where requested (no snapping needed).
    dispatch(store, "move_clip", {"clip_id": a1_id, "new_start": 20.0})
    c = store.edl.get_clip(a1_id)[1]
    assert abs(c.start - 20.0) < 1e-6


def test_add_sticker_returns_a_running_sticker_count(tmp_path):
    """Regression for issue 51 (agent miscounts stickers): add_sticker must
    hand back ground truth in its own tool_result rather than forcing the
    caller to make a separate get_timeline call to find out how many exist."""
    from PIL import Image
    png = tmp_path / "sticker.png"
    Image.new("RGBA", (32, 32), (255, 0, 0, 255)).save(png)

    store = _store_with_one_clip()
    r1 = dispatch(store, "add_sticker", {"src": str(png), "start": 0.0, "end": 1.0})
    assert r1["sticker_count"] == 1
    r2 = dispatch(store, "add_sticker", {"src": str(png), "start": 1.0, "end": 2.0})
    assert r2["sticker_count"] == 2
    r3 = dispatch(store, "add_sticker", {"src": str(png), "start": 2.0, "end": 3.0})
    assert r3["sticker_count"] == 3


def test_get_timeline_reports_per_track_clip_count(tmp_path):
    from PIL import Image
    png = tmp_path / "sticker.png"
    Image.new("RGBA", (32, 32), (0, 255, 0, 255)).save(png)

    store = _store_with_one_clip()
    dispatch(store, "add_sticker", {"src": str(png), "start": 0.0, "end": 1.0})
    dispatch(store, "add_sticker", {"src": str(png), "start": 1.0, "end": 2.0})

    snap = dispatch(store, "get_timeline", {"summary": True})
    by_id = {t["id"]: t for t in snap["tracks"]}
    assert by_id["v1"]["clip_count"] == 1
    assert by_id["stickers"]["clip_count"] == 2


def test_color_grade_merges_into_a_single_effect_instead_of_stacking():
    """Regression for issues 16.4-16.8: each Properties.tsx slider release
    sends only the ONE param that changed. Before this fix, color_grade
    appended a brand-new Effect(type='color') every call, so adjusting
    brightness then contrast then brightness again left 3 independent filter
    passes on the clip instead of one clip having one color grade."""
    store = _store_with_one_clip()
    cid = store.edl.tracks[0].clips[0].id

    dispatch(store, "color_grade", {"clip_id": cid, "brightness": 0.1})
    dispatch(store, "color_grade", {"clip_id": cid, "contrast": 1.3})
    dispatch(store, "color_grade", {"clip_id": cid, "brightness": 0.25})

    clip = store.edl.tracks[0].clips[0]
    color_effects = [e for e in clip.effects if e.type == "color"]
    assert len(color_effects) == 1, "must merge into one effect, not stack"
    assert color_effects[0].params["brightness"] == 0.25  # last write wins
    assert color_effects[0].params["contrast"] == 1.3      # earlier params kept


def test_color_grade_creates_one_effect_on_first_call():
    store = _store_with_one_clip()
    cid = store.edl.tracks[0].clips[0].id
    dispatch(store, "color_grade", {"clip_id": cid, "saturation": 1.5})
    clip = store.edl.tracks[0].clips[0]
    assert len([e for e in clip.effects if e.type == "color"]) == 1


def test_color_reset_neutralizes_all_params_via_merge():
    """The Properties.tsx 'Reset' button for Color dispatches color_grade with
    all-neutral values — confirm the merge path actually neutralizes prior
    non-default params rather than leaving them mixed in."""
    store = _store_with_one_clip()
    cid = store.edl.tracks[0].clips[0].id
    dispatch(store, "color_grade", {"clip_id": cid, "brightness": 0.3, "contrast": 1.8, "tint": 0.5})
    dispatch(store, "color_grade", {
        "clip_id": cid, "brightness": 0, "contrast": 1, "saturation": 1, "temp": 0, "tint": 0,
    })
    clip = store.edl.tracks[0].clips[0]
    color_effects = [e for e in clip.effects if e.type == "color"]
    assert len(color_effects) == 1
    assert color_effects[0].params == {
        "brightness": 0.0, "contrast": 1.0, "saturation": 1.0, "temp": 0.0, "tint": 0.0,
    }


def test_transform_reset_restores_identity_transform():
    """The Properties.tsx 'Reset' button for Transform dispatches
    set_clip_transform with the schema's identity defaults."""
    store = _store_with_one_clip()
    cid = store.edl.tracks[0].clips[0].id
    dispatch(store, "set_clip_transform", {
        "clip_id": cid, "x": 300, "y": 450, "scale": 2.5, "rotation": 45, "opacity": 0.5,
    })
    clip = store.edl.tracks[0].clips[0]
    assert clip.transform.scale == 2.5  # sanity: the mutation actually applied

    dispatch(store, "set_clip_transform", {
        "clip_id": cid, "x": 0, "y": 0, "scale": 1, "rotation": 0, "opacity": 1,
    })
    clip = store.edl.tracks[0].clips[0]
    assert clip.transform.x == 0.0
    assert clip.transform.y == 0.0
    assert clip.transform.scale == 1.0
    assert clip.transform.rotation == 0.0
    assert clip.transform.opacity == 1.0


def test_audio_reset_restores_zero_gain_and_no_fades():
    store = _store_with_one_clip()
    cid = store.edl.tracks[0].clips[0].id
    dispatch(store, "set_volume", {"target": cid, "db": -12})
    dispatch(store, "add_fade", {"clip_id": cid, "in_s": 1.5, "out_s": 2.0})
    clip = store.edl.tracks[0].clips[0]
    assert clip.audio.gain_db == -12

    dispatch(store, "set_volume", {"target": cid, "db": 0})
    dispatch(store, "add_fade", {"clip_id": cid, "in_s": 0, "out_s": 0})
    clip = store.edl.tracks[0].clips[0]
    assert clip.audio.gain_db == 0.0
    assert clip.audio.fade_in == 0.0
    assert clip.audio.fade_out == 0.0


def test_speed_reset_restores_1x():
    store = _store_with_one_clip()
    cid = store.edl.tracks[0].clips[0].id
    dispatch(store, "set_speed", {"clip_id": cid, "factor": 1.75})
    clip = store.edl.tracks[0].clips[0]
    assert clip.speed == 1.75

    dispatch(store, "set_speed", {"clip_id": cid, "factor": 1})
    clip = store.edl.tracks[0].clips[0]
    assert clip.speed == 1.0


def test_add_sticker_enforces_a_minimum_span_when_end_collapses_to_start(tmp_path):
    """Regression for issue 31b: a caller-supplied end <= start (e.g.
    StickerPanel.tsx clamping end to edl.duration when inserting near the
    tail of the timeline) must not silently produce a near-zero/inverted
    sticker window."""
    from PIL import Image
    png = tmp_path / "sticker.png"
    Image.new("RGBA", (32, 32), (255, 0, 0, 255)).save(png)

    store = _store_with_one_clip()
    r = dispatch(store, "add_sticker", {
        "src": str(png), "start": 58.0, "end": 58.0,  # end == start
    })
    sticker = store.edl.get_track("stickers").clips[0]
    assert sticker.end - sticker.start >= 2.9  # floored to a real ~3s span
    assert sticker.start == 58.0


def test_add_sticker_enforces_a_minimum_span_when_end_is_before_start(tmp_path):
    from PIL import Image
    png = tmp_path / "sticker.png"
    Image.new("RGBA", (32, 32), (0, 255, 0, 255)).save(png)

    store = _store_with_one_clip()
    dispatch(store, "add_sticker", {
        "src": str(png), "start": 58.0, "end": 57.5,  # end < start
    })
    sticker = store.edl.get_track("stickers").clips[0]
    assert sticker.end - sticker.start >= 2.9


def test_add_sticker_keeps_a_normal_explicit_span_unchanged(tmp_path):
    from PIL import Image
    png = tmp_path / "sticker.png"
    Image.new("RGBA", (32, 32), (0, 0, 255, 255)).save(png)

    store = _store_with_one_clip()
    dispatch(store, "add_sticker", {"src": str(png), "start": 2.0, "end": 4.5})
    sticker = store.edl.get_track("stickers").clips[0]
    assert sticker.start == 2.0
    assert sticker.end == 4.5


def test_add_marker_default_color_is_not_playhead_red():
    """Regression: add_marker's default color used to be #ff4d6d — the exact
    playhead stroke color in Timeline.tsx — so a marker with no explicit color
    was visually indistinguishable from the (static) playhead, reading as
    "two playheads, one frozen"."""
    store = _store_with_one_clip()
    res = dispatch(store, "add_marker", {"time": 1.0})
    mid = res["marker_id"]
    marker = next(m for m in store.edl.markers if m.id == mid)
    # Must not collide with the timeline playhead color (#ff4d6d).
    assert marker.color.lower() != "#ff4d6d"


def test_add_super_text_dedupes_exact_duplicate():
    """Regression for the "double subtitle" bug: an identical re-run of
    add_super_text (same text/role/start/end) must not stack a second
    TextClip on top of the first."""
    store = _store_with_one_clip()
    args = {"text": "RIPPLE TEST", "role": "super", "start": 0.0, "end": 3.0}
    dispatch(store, "add_super_text", dict(args))
    dispatch(store, "add_super_text", dict(args))  # identical re-run
    supers = [c for t in store.edl.tracks for c in t.clips
              if getattr(c, "text", None) == "RIPPLE TEST"]
    assert len(supers) == 1, "identical add_super_text must not stack duplicates"


def test_add_super_text_allows_distinct_text():
    store = _store_with_one_clip()
    dispatch(store, "add_super_text", {"text": "A", "role": "super", "start": 0.0, "end": 3.0})
    dispatch(store, "add_super_text", {"text": "B", "role": "super", "start": 0.0, "end": 3.0})
    supers = [c for t in store.edl.tracks for c in t.clips if getattr(c, "text", None) in ("A", "B")]
    assert len(supers) == 2, "distinct captions must both survive"


def test_add_super_text_replace_drops_prior_overlapping_same_role():
    store = _store_with_one_clip()
    dispatch(store, "add_super_text", {"text": "OLD", "role": "super", "start": 0.0, "end": 3.0})
    dispatch(store, "add_super_text", {
        "text": "NEW", "role": "super", "start": 1.0, "end": 4.0, "replace": True,
    })
    supers = [c for t in store.edl.tracks for c in t.clips if getattr(c, "role", None) == "super"]
    assert len(supers) == 1
    assert supers[0].text == "NEW"


def test_add_text_dedupes_exact_duplicate():
    """Same idempotency guard as add_super_text, applied to add_text's own
    text/role/start/end fields."""
    store = _store_with_one_clip()
    args = {"text": "LOWER THIRD", "role": "lower_third", "start": 0.0, "end": 3.0}
    dispatch(store, "add_text", dict(args))
    dispatch(store, "add_text", dict(args))  # identical re-run
    matches = [c for t in store.edl.tracks for c in t.clips
               if getattr(c, "text", None) == "LOWER THIRD"]
    assert len(matches) == 1, "identical add_text must not stack duplicates"


def test_add_text_allows_distinct_text():
    store = _store_with_one_clip()
    dispatch(store, "add_text", {"text": "A", "role": "caption", "start": 0.0, "end": 3.0})
    dispatch(store, "add_text", {"text": "B", "role": "caption", "start": 0.0, "end": 3.0})
    matches = [c for t in store.edl.tracks for c in t.clips if getattr(c, "text", None) in ("A", "B")]
    assert len(matches) == 2, "distinct text overlays must both survive"
