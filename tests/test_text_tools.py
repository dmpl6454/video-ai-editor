"""Tests for the M2 text/brand/audit tools."""
import tempfile
from pathlib import Path
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Clip, TextClip
from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.render.ass_writer import edl_to_ass


def _store_with_video() -> EDLStore:
    tmp = tempfile.mkdtemp()
    store = EDLStore(Path(tmp))
    dispatch(store, "add_clip", {
        "track": "v1", "src": str(Path(tmp) / "nonexistent" / "x.mp4"),
        "in": 0.0, "out": 30.0, "start": 0.0,
    })
    return store


def test_add_super_text_creates_clip():
    store = _store_with_video()
    res = dispatch(store, "add_super_text", {
        "text": "BUY NOW", "start": 0.0, "end": 2.0, "role": "super",
    })
    assert "clip_id" in res
    track = store.edl.get_track("tx_super")
    assert track is not None and len(track.clips) == 1
    assert isinstance(track.clips[0], TextClip)


def test_add_hook_overlay_uses_hook_track():
    store = _store_with_video()
    dispatch(store, "add_hook_overlay", {"text": "WAIT FOR IT...", "duration": 2.5})
    hook_track = store.edl.get_track("tx_hook")
    assert hook_track is not None and len(hook_track.clips) == 1
    assert hook_track.clips[0].end == 2.5


def test_apply_brand_kit_attaches_watermark_and_endcard():
    store = _store_with_video()
    dispatch(store, "apply_brand_kit", {
        "handle": "@quicksolutions.in",
        "hashtags": ["#techtips", "#tech"],
    })
    wm = store.edl.get_track("tx_watermark")
    ec = store.edl.get_track("tx_endcard")
    assert wm and len(wm.clips) == 1
    assert ec and len(ec.clips) == 1
    assert "@quicksolutions" in wm.clips[0].text


def test_audit_aesthetic_flags_missing_hook():
    store = _store_with_video()
    rep = dispatch(store, "audit_aesthetic", {})
    keys = {i["key"] for i in rep["issues"]}
    assert "hook_missing" in keys
    # Add a hook → no longer missing
    dispatch(store, "add_hook_overlay", {"text": "HOOK", "duration": 3.0})
    rep2 = dispatch(store, "audit_aesthetic", {})
    keys2 = {i["key"] for i in rep2["issues"]}
    assert "hook_missing" not in keys2


def test_ass_writer_emits_dialogue_for_text_clips():
    store = _store_with_video()
    dispatch(store, "add_super_text", {"text": "Hello world", "start": 1.0, "end": 3.0})
    ass = edl_to_ass(store.edl)
    assert "[V4+ Styles]" in ass
    assert "Style: super" in ass
    assert "Dialogue:" in ass
    assert "Hello world" in ass
