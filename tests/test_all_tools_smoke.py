"""Coverage smoke test: every dispatch tool gets called once with valid args
on a freshly-built session. The point is not to verify each tool's exact
semantics (other test files do that) but to catch:

  - Tools wired in DISPATCH but raising on import.
  - Argument-validation bugs (missing fields, wrong types) that surface only
    when you actually call them.
  - State left in a broken shape after the call (asserts the EDL still
    serializes after every tool).

Tools that need network / heavy AI weights / external models / audio sources
are SKIP-listed — those have their own targeted tests.
"""
from __future__ import annotations
import os
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import (
    EDL, Track, Clip, TextClip, Sticker, Canvas, Transform, BrandKit,
)
from video_ai_editor.agent.dispatch import DISPATCH, dispatch


# ---------------------------------------------------------------------------
# Tools that have their own dedicated tests OR require heavy/unavailable deps.
# We still _construct_ the call argv to make sure dispatch routing works.

NETWORK_OR_HEAVY = {
    "find_moments",        # Claude vision API call
    "match_style",         # vision per-shot
    "generate_hook",       # Claude API
    "remove_background",   # rembg + 170MB model
    "object_erase",        # LaMa + 200MB model
    "upscale",             # Real-ESRGAN binary
    "smooth_slow_motion",  # RIFE binary
    "stabilize",           # vidstab two-pass, slow
    "auto_reframe",        # mediapipe
    "vocal_isolate",       # Demucs, slow
    "instrumental_isolate",
    "tts_voiceover",       # Piper
    "diarize",             # pyannote OR librosa heuristic
    "assign_caption_speakers",  # runs diarize when no turns given; targeted test exists
    "translate_captions",  # Argos Translate
    "make_shorts",         # heuristic over vision
    "multicam",            # multi-input audio sync
    "motion_track",        # OpenCV per-frame
    "noise_reduce",        # noisereduce, slow
    "auto_cut_to_beats",   # librosa beats
}

# Tools that mutate session state but need bespoke args we'll provide per-tool.
PER_TOOL_ARGS: dict[str, dict] = {}  # filled below per fixture


@pytest.fixture
def session(tmp_path: Path):
    """A session with one V1 video clip + one music clip + one text + one
    sticker. Most edit tools need at least these things to act on."""
    src = tmp_path / "src.mp4"
    keyed = tmp_path / "k.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", "color=c=blue:s=320x180:d=4:r=30",
         "-pix_fmt", "yuv420p", str(keyed)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(keyed),
         "-f", "lavfi", "-i", "sine=f=440:duration=4",
         "-c:v", "copy", "-c:a", "aac", "-shortest", str(src)],
        check=True, capture_output=True,
    )
    music = tmp_path / "music.mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", "sine=f=200:duration=8",
         "-c:a", "mp3", str(music)],
        check=True, capture_output=True,
    )
    sticker_png = tmp_path / "sticker.png"
    from PIL import Image
    Image.new("RGBA", (64, 64), (255, 0, 0, 255)).save(sticker_png)

    edl = EDL(
        canvas=Canvas(w=320, h=180, fps=30, loudness_lufs=-16.0),
        tracks=[
            Track(id="v1", type="video", clips=[
                Clip(src=str(src), in_=0, out=4, start=0, id="c1"),
            ]),
            Track(id="v2", type="video", z=1, clips=[]),
            Track(id="music", type="music", clips=[
                Clip(src=str(music), in_=0, out=4, start=0, id="m1"),
            ]),
            Track(id="vo", type="vo", clips=[]),
            Track(id="text", type="text", z=10, clips=[
                TextClip(id="t1", text="HELLO", start=0.0, end=2.0,
                         transform=Transform(x=160, y=40), role="super"),
            ]),
            Track(id="stickers", type="sticker", z=11, clips=[
                Sticker(id="s1", src=str(sticker_png), start=0.0, end=2.0),
            ]),
            Track(id="captions", type="captions"),
        ],
    )
    edl.recompute_duration()
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    # Seed a transcript so export_srt/vtt/ass have something to write.
    (tmp_path / "transcript.json").write_text(
        '{"language":"en","duration":4.0,"segments":['
        '{"id":0,"start":0.0,"end":2.0,"text":"hello world","words":[]}]}'
    )
    store = EDLStore(tmp_path)
    return store


def _args_for(tool: str, store: EDLStore, tmp_path: Path) -> dict | None:
    """Return valid args for `tool`, or None to skip-with-pass."""
    src = store.edl.tracks[0].clips[0].src
    music = store.edl.tracks[2].clips[0].src
    return {
        # Inspection
        "get_timeline": {},
        "get_clip": {"clip_id": "c1"},
        "get_transcript": {},
        "audit_aesthetic": {},
        "find_broll": {"bin": str(tmp_path), "query": "anything"},
        # search_media: spoken scope avoids loading the heavy CLIP model in the
        # generic smoke pass; the visual path has its own gated test.
        "search_media": {"query": "anything", "scope": "spoken"},
        "pyannote_status": {},
        # List tools
        "list_filters": {},
        "list_transitions": {},
        "list_text_styles": {},
        "list_shows": {},
        "list_luts": {},
        "list_templates": {},
        # Edits
        "add_clip": {"track": "v1", "src": src, "in": 0.0, "out": 1.0, "start": 4.0},
        "cut_range": {"track": "v1", "start": 1.0, "end": 1.5},
        "split_at": {"time": 2.0},
        "trim_clip": {"clip_id": "c1", "in": 0.5, "out": 3.5},
        "move_clip": {"clip_id": "c1", "new_start": 0.0},
        "reorder_clips": {"track": "v1", "order": ["c1"]},
        "ripple_delete": {"clip_id": "c1"},  # destructive — late
        "duplicate_clip": {"clip_id": "c1"},
        "set_speed": {"clip_id": "c1", "factor": 1.5},
        "bulk_delete": {"clip_ids": ["t1"]},
        "bulk_duplicate": {"clip_ids": ["c1"]},
        # Transform / keyframes
        "set_clip_transform": {"clip_id": "c1", "scale": 1.2},
        # Overlay timing: stickers/text only (s1 is the fixture sticker).
        "set_clip_timing": {"clip_id": "s1", "start": 0.5, "end": 3.0},
        "set_property": {"clip_id": "c1", "path": "audio.gain_db", "value": 2.0},
        "add_keyframe": {"clip_id": "c1", "prop": "scale", "time": 0.5, "value": 1.5},
        "remove_keyframe": {"clip_id": "c1", "prop": "scale", "time": 0.5},
        # Effects / color / masks
        "add_effect": {"clip_id": "c1", "type": "blur", "params": {"radius": 4}},
        "remove_effect": {"clip_id": "c1", "idx": 0},
        "color_grade": {"clip_id": "c1", "brightness": 0.05, "contrast": 1.1, "saturation": 1.1},
        "apply_lut": {"clip_id": "c1", "lut_path": os.devnull, "intensity": 0.5},
        "add_mask": {"clip_id": "c1", "type": "circle", "feather": 8.0},
        "chroma_key": {"clip_id": "c1", "color": "#00FF00", "similarity": 0.4,
                       "smoothness": 0.1, "spill_suppress": 0.4},
        "add_transition": {"at": 2.0, "type": "fade", "duration": 0.5},
        # Audio
        "set_volume": {"target": "music", "db": -12.0},
        "add_fade": {"clip_id": "c1", "in_s": 0.5, "out_s": 0.5},
        "add_music": {"src": music, "start": 0, "in": 0, "out": 4, "duck": True, "volume_db": -12},
        "set_loudness_target": {"lufs": -16.0},
        "set_track_muted": {"track": "music", "muted": True},
        "set_track_locked": {"track": "v1", "locked": True},
        "remove_silences": {"clip_id": "c1", "min_silence_s": 0.5, "noise_db": -35},
        "remove_fillers": {"clip_id": "c1"},
        "record_voiceover": {"src": music, "start": 0.0},  # reuses music as fake mic capture
        # Text & captions
        "add_super_text": {"text": "TEST", "start": 0.0, "end": 1.0},
        "add_text": {"text": "TEST", "start": 0.0, "end": 1.0, "role": "super"},
        "add_hook_overlay": {"text": "Hook?", "duration": 3.0},
        "add_caption_track": {"style": "default", "position": "bottom"},
        "import_srt": {"path": str(_make_srt(tmp_path))},
        "export_srt": {"path": str(tmp_path / "out.srt")},
        "export_vtt": {"path": str(tmp_path / "out.vtt")},
        "export_ass": {"path": str(tmp_path / "out.ass")},
        # Stickers
        "add_sticker": {"src": store.edl.tracks[5].clips[0].src,
                        "start": 0.0, "end": 1.0},
        # Templates / brand kit / shows
        "apply_template": {"name": "outfit_breakdown"},
        "apply_text_template": {"name": "big_question", "start": 0.0, "end": 2.0,
                                "fields": {"text": "What if?"}},
        "apply_brand_kit": {"handle": "@me", "hashtags": ["#tag"]},
        "apply_export_preset": {"name": "reels"},
        "save_show_template": {"name": "test_show"},
        "apply_show_template": {"name": "test_show"},
        # Markers + canvas + project
        "add_marker": {"time": 1.0, "label": "test"},
        "remove_marker": None,  # needs a marker_id we'll set up dynamically
        "set_canvas": {"w": 1080, "h": 1920, "fps": 30},
        "set_aspect_ratio": {"ratio": "9:16"},
        "name_speakers": {"mapping": {"SPEAKER_00": "Host"}},
        "add_lower_third": {"speaker": "Host", "name": "Test", "title": "@x",
                            "start": 0.0, "end": 2.0},
        # Project / undo
        "undo": {},
        "redo": {},
        "render_preview": {},  # gated below
        # Repair tools
        "repair_chunks": {},
        "repair_media_paths": {},
    }.get(tool, {})


def _make_srt(td: Path) -> Path:
    p = td / "in.srt"
    p.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n")
    return p


@pytest.fixture(autouse=True)
def _cleanup_global_show():
    """Remove any test_show.json the smoke suite created in the project-global
    presets dir, so consecutive test runs don't pollute the user's show list."""
    yield
    try:
        from video_ai_editor.show.templates import shows_dir
        p = shows_dir() / "test_show.json"
        if p.exists():
            p.unlink()
    except Exception:
        pass


@pytest.mark.parametrize("tool", sorted(DISPATCH.keys()))
def test_tool_smoke(tool: str, session: EDLStore, tmp_path: Path):
    if tool in NETWORK_OR_HEAVY:
        pytest.skip(f"{tool} has its own targeted test (heavy/network)")
    args = _args_for(tool, session, tmp_path)
    if args is None:
        pytest.skip(f"{tool} needs dynamic args")
    if tool == "render_preview":
        # Render is gated: only smoke-test if ffmpeg + a working clip
        pytest.skip("render covered by render_smoke")
    if tool == "remove_marker":
        # Add a marker first
        m = dispatch(session, "add_marker", {"time": 0.5, "label": "x"})
        args = {"marker_id": m["marker_id"]}
    if tool == "apply_show_template":
        # Save first so apply has something to load. The save uses the
        # project-global presets/shows dir; cleanup happens in finalizer.
        from video_ai_editor.show.templates import shows_dir
        saved = shows_dir() / "test_show.json"
        if not saved.exists():
            dispatch(session, "save_show_template", {"name": "test_show"})
    if tool == "remove_effect":
        # Add an effect first so there's an index 0 to remove.
        dispatch(session, "add_effect", {"clip_id": "c1", "type": "blur",
                                          "params": {"radius": 4}})

    res = dispatch(session, tool, args)
    assert isinstance(res, dict), f"{tool} should return a dict"
    # EDL must still be serialisable after every tool call
    serialized = session.edl.model_dump_json()
    assert serialized
