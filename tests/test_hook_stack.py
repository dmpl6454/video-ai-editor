"""Tests for the 3-axis hook stack.

A short-form video gets ~3 seconds to keep someone watching. The editor
treats the hook stack as three independent axes that work in concert:

  👁 visual — bold opening (motion / cut / speed change in first 3s)
  ✍ text   — overlay so muted-autoplay viewers get the topic instantly
  🎧 audio  — fade-in / music start / vo / gain shaping

`apply_hook_stack` installs all three; `audit_aesthetic` reports per-axis
presence and the 0-3 score.
"""
from __future__ import annotations
from pathlib import Path

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import (
    EDL, Track, Clip, Canvas, TextClip, Keyframe,
)
from video_ai_editor.agent.dispatch import dispatch


def _seed(tmp: Path) -> EDLStore:
    """One-clip V1 with no hook bits — empty stack baseline."""
    tmp.mkdir(parents=True, exist_ok=True)
    src = tmp / "src.mp4"
    src.write_bytes(b"fake")
    edl = EDL(
        canvas=Canvas(w=320, h=180, fps=30),
        tracks=[Track(id="v1", type="video", clips=[
            Clip(src=str(src), in_=0, out=10, start=0, id="c1"),
        ])],
    )
    edl.recompute_duration()
    (tmp / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(tmp)


# ---------- axis presence (audit only) -----------------------------------

def test_audit_reports_empty_hook_on_bare_project(tmp_path: Path):
    store = _seed(tmp_path)
    audit = dispatch(store, "audit_aesthetic", {})
    assert audit["hook"]["hook_score"] == 0
    assert audit["hook"]["visual"] is False
    assert audit["hook"]["text"] is False
    assert audit["hook"]["audio"] is False
    # An empty hook is a blocking error.
    assert any(i["key"] == "hook_missing" and i["level"] == "error"
               for i in audit["issues"])


# ---------- apply_hook_stack installs all three --------------------------

def test_apply_hook_stack_installs_all_three_axes(tmp_path: Path):
    store = _seed(tmp_path)
    res = dispatch(store, "apply_hook_stack", {"text": "Watch this until the end."})
    assert res["axes"]["text"] is True
    assert res["axes"]["visual"] is True
    assert res["axes"]["audio"] is True

    audit = dispatch(store, "audit_aesthetic", {})
    assert audit["hook"]["hook_score"] == 3
    # No more hook_missing error.
    assert not any(i["key"] == "hook_missing" for i in audit["issues"])


def test_visual_axis_uses_keyframed_scale_on_first_clip(tmp_path: Path):
    store = _seed(tmp_path)
    dispatch(store, "apply_hook_stack", {"text": "x", "visual": "punch_in"})
    first = store.edl.tracks[0].clips[0]
    assert isinstance(first.transform.scale, Keyframe)
    assert len(first.transform.scale.keyframes) == 2
    assert first.transform.scale.keyframes[0][1] == 1.0
    assert first.transform.scale.keyframes[1][1] > 1.0


def test_audio_axis_sets_fade_in_on_first_clip(tmp_path: Path):
    store = _seed(tmp_path)
    dispatch(store, "apply_hook_stack", {"text": "x", "audio": "fade_boost"})
    first = store.edl.tracks[0].clips[0]
    assert first.audio.fade_in >= 0.1


def test_text_axis_writes_hook_overlay_at_zero(tmp_path: Path):
    store = _seed(tmp_path)
    dispatch(store, "apply_hook_stack", {"text": "Big bold claim"})
    text_track = store.edl.get_track("tx_hook")
    assert text_track is not None
    hooks = [c for c in text_track.clips
             if isinstance(c, TextClip) and c.role == "hook"]
    assert len(hooks) == 1
    assert hooks[0].text == "Big bold claim"
    assert hooks[0].start == 0.0
    assert hooks[0].end >= 2.5


# ---------- idempotency: rerunning replaces, doesn't double-stack --------

def test_apply_hook_stack_is_idempotent(tmp_path: Path):
    store = _seed(tmp_path)
    dispatch(store, "apply_hook_stack", {"text": "first"})
    dispatch(store, "apply_hook_stack", {"text": "second"})
    text_track = store.edl.get_track("tx_hook")
    hooks = [c for c in text_track.clips
             if isinstance(c, TextClip) and c.role == "hook"]
    # Only one hook overlay, with the latest text.
    assert len(hooks) == 1
    assert hooks[0].text == "second"


# ---------- audit grading at 1/3 and 2/3 ---------------------------------

def test_audit_warns_when_only_text_axis_present(tmp_path: Path):
    store = _seed(tmp_path)
    # Text only — no visual motion, no audio shaping.
    dispatch(store, "add_super_text", {
        "text": "hook", "start": 0.0, "end": 3.0, "role": "hook",
    })
    audit = dispatch(store, "audit_aesthetic", {})
    assert audit["hook"]["hook_score"] == 1
    assert audit["hook"]["text"] is True
    assert audit["hook"]["visual"] is False
    assert audit["hook"]["audio"] is False
    assert any(i["key"] == "hook_partial" for i in audit["issues"])


def test_audit_warns_when_two_of_three_axes_present(tmp_path: Path):
    store = _seed(tmp_path)
    # Add text + visual motion only.
    dispatch(store, "add_super_text", {
        "text": "hook", "start": 0.0, "end": 3.0, "role": "hook",
    })
    dispatch(store, "add_keyframe", {
        "clip_id": "c1", "prop": "scale", "time": 0.0, "value": 1.0,
    })
    dispatch(store, "add_keyframe", {
        "clip_id": "c1", "prop": "scale", "time": 3.0, "value": 1.05,
    })
    audit = dispatch(store, "audit_aesthetic", {})
    assert audit["hook"]["hook_score"] == 2
    assert audit["hook"]["missing"] == ["audio"]
    assert any(i["key"] == "hook_two_of_three" for i in audit["issues"])


# ---------- music starting at 0 also counts as audio axis ----------------

def test_music_at_zero_satisfies_audio_axis(tmp_path: Path):
    store = _seed(tmp_path)
    music = tmp_path / "song.mp3"
    music.write_bytes(b"fake")
    # add_music probes duration — patch via direct EDL mutation.
    from video_ai_editor.edl.schema import Track as _T, Clip as _C
    store.edl.tracks.append(_T(id="music", type="music", clips=[
        _C(src=str(music), in_=0, out=30, start=0, id="m1"),
    ]))
    store.commit("seed_music", {}, "test fixture")
    audit = dispatch(store, "audit_aesthetic", {})
    assert audit["hook"]["audio"] is True
