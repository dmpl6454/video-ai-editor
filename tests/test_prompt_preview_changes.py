"""The preview card's change list (agent/prompt/changes.py), op by op.

0.8.0 "Preview, then apply": the key-free Prompt bar shows what a plan WOULD
change, from the EDL diff, before anything is committed. The one property
the card must never break: IT NEVER OMITS A CHANGE — every key of
`changes.flat_state` that differs between the two trees is claimed by some
line (`covered(summarize(b, a)) ⊇ diff_keys(b, a)`).

Proved three ways:
  * `test_every_plan_tool_is_covered` — every tool a plan may run
    (`schema.TOOL_STAGE`) is in exactly one table below, so a new plan tool
    without a summarizer test fails here;
  * `test_tool_change_is_summarized` — each tool dispatched for real on a
    session (three 4 s clips, a music bed, a title, a dissolve, captions,
    16:9) — or, for the heavy on-device AI tools no test may run (whisper,
    reframe, denoise, upscale, RIFE, Piper), its exact EDL effect applied by
    hand — gives the editor's line AND covers every changed key;
  * `test_random_mutations_are_always_covered` — hundreds of random edits
    to any leaf of the tree, and random structural edits.
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import no_downloads  # noqa: E402,F401

from video_ai_editor import config, storage  # noqa: E402
from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.agent.prompt import changes as C  # noqa: E402
from video_ai_editor.agent.prompt import preview as PV  # noqa: E402
from video_ai_editor.agent.prompt.schema import TOOL_STAGE  # noqa: E402
from video_ai_editor.edl.schema import EDL, Clip, Effect, Marker, Sticker, TextClip, Transition  # noqa: E402
from video_ai_editor.edl.snapshot import EDLStore  # noqa: E402


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    root = tmp_path_factory.mktemp("preview_changes")
    before = config._FORCED_RESTRICT
    config.enable_path_restriction(False)
    mp = pytest.MonkeyPatch()
    mp.setattr(storage, "WORKDIR", root)
    src = F.speech_clip(root)
    F.write_ingest(src)
    bed = F.music_bed(root, dur=12.0)
    angle = F.speech_clip(root, "angle")          # a second camera for apply_camera_plan
    yield {"root": root, "src": src, "bed": bed, "angle": angle,
           "ingest": (src.parent / "ingest.json").read_bytes()}
    mp.undo()
    config.enable_path_restriction(before)


_N = [0]


def _session(media: dict) -> tuple[EDLStore, dict[str, str]]:
    (media["src"].parent / "ingest.json").write_bytes(media["ingest"])
    _N[0] += 1
    st = EDLStore(media["root"] / f"s_pc{_N[0]:04d}")
    dispatch(st, "add_clip", {"track": "v1", "src": str(media["src"]), "in": 0, "out": F.CLIP_DUR, "start": 0})
    dispatch(st, "set_canvas", {"w": 1920, "h": 1080})
    dispatch(st, "split_at", {"track": "v1", "time": 4.0})
    dispatch(st, "split_at", {"track": "v1", "time": 8.0})
    dispatch(st, "add_music", {"src": str(media["bed"]), "start": 0.0, "volume_db": -14.0, "duck": False})
    dispatch(st, "add_text", {"text": "Summer Trip", "start": 0.0, "end": 3.0, "y": 810, "size": 96})
    dispatch(st, "add_transition", {"at": 4.0, "type": "dissolve", "duration": 0.5})
    dispatch(st, "add_caption_track", {"style": "default", "position": "bottom"})
    v1 = sorted(st.edl.get_track("v1").clips, key=lambda c: c.start)
    ids = {"A": v1[0].id, "B": v1[1].id, "C": v1[2].id,
           "title": next(c.id for t in st.edl.tracks if t.type == "text" for c in t.clips),
           "music": st.edl.get_track("music").clips[0].id}
    return st, ids


def _d(tool: str, **args: Any) -> Callable:
    """Dispatch `tool` for real; `$A`/`$B`/`$C`/`$title`/`$music` name clips."""
    def run(st: EDLStore, ids: dict[str, str], media: dict) -> None:
        def sub(v: Any) -> Any:
            if isinstance(v, str) and v.startswith("$") and v[1:] in ids:
                return ids[v[1:]]
            if isinstance(v, list):
                return [sub(x) for x in v]
            return v
        dispatch(st, tool, {k: sub(v) for k, v in args.items()})
    return run


def _cache_src(st: EDLStore, cid: str, kind: str, **extra: Any) -> None:
    """A heavy AI step's EDL effect: the clip now plays a render in the
    session cache (dispatch: `cache/<kind>_<hash>.mp4`), same timing."""
    found = st.edl.get_clip(cid)
    assert found, cid
    _, clip = found
    out = Path(st.dir) / "cache" / kind / f"{kind}_ab12cd34.mp4"
    clip.src = str(out)
    for k, v in extra.items():
        setattr(clip, k, v)


def _sim_reframe(st, ids, media):
    st.edl.canvas.w, st.edl.canvas.h = 1080, 1920
    for k in ("A", "B", "C"):
        _cache_src(st, ids[k], "reframe", fit="cover")


def _sim_caption(st, ids, media):
    cap = st.edl.get_track("captions")
    cap.clips = [TextClip(text=w, start=s, end=e, role="caption") for w, s, e in
                 (("so um hello", 0.2, 2.1), ("friends", 2.2, 2.8), ("uh today we start", 5.5, 7.6))]
    cap.config.enabled = True
    cap.config.style = "ig_chunky"


def _sim_tts(st, ids, media):
    vo = st.edl.get_track("vo")
    vo.clips.append(Clip(src=str(Path(st.dir) / "cache" / "tts" / "tts_ab12.wav"), in_=0.0, out=2.5, start=1.0))


def _sim_beats(st, ids, media):
    dispatch(st, "split_at", {"track": "v1", "time": 2.0})
    dispatch(st, "split_at", {"track": "v1", "time": 6.0})


def _sim_match_style(st, ids, media):
    for k in ("A", "B", "C"):
        _, c = st.edl.get_clip(ids[k])
        c.effects = [*c.effects, Effect(type="color_grade", params={"contrast": 1.1, "saturation": 1.2})]


def _sim_slowmo(st, ids, media):
    _cache_src(st, ids["B"], "rife", speed=0.5)


def _sim_translate(st, ids, media):
    cap = st.edl.get_track("captions")
    for c in cap.clips:
        c.text = c.text.upper() + " (hi)"
    cap.config.lang = "hi"


# tool → (how, words that must appear in the lines[, set-up run before the
# "before" tree is taken]). Words are the editor's own: a clip by number and
# name, a value before -> after, a timecode.
OPS: dict[str, tuple] = {
    # stage 0 — marks and locks
    "add_marker": (_d("add_marker", time=2.0, label="Hook"), ["Added marker 'Hook' at 00:00:02:00"]),
    "set_track_locked": (_d("set_track_locked", track="music", locked=True), ["Music track: locked"]),
    # stage 1
    "name_speakers": (_d("name_speakers", mapping={"SPEAKER_00": "Asha"}), []),
    # stage 2 — cuts and timing
    "cut_range": (_d("cut_range", track="v1", start=1.0, end=2.0),
                  ["Deleted 00:00:01:00-00:00:02:00 of the video (part of Clip 1 'talk.mp4')"]),
    "ripple_delete": (_d("ripple_delete", clip_id="$B"), ["Deleted Clip 2 'talk.mp4' (00:00:04:00-00:00:08:00)",
                                                         "Removed transition Dissolve at 00:00:04:00",
                                                         "1 later clip moves to keep the video continuous"]),
    "remove_silences": (_d("remove_silences"), ["Deleted", "of the video"]),
    "remove_fillers": (_d("remove_fillers"), ["Deleted"]),
    "set_speed": (_d("set_speed", clip_id="$B", factor=0.5), ["Clip 2 'talk.mp4': speed 1x -> 0.5x (4.0 s -> 8.0 s)",
                                                             "1 later clip moves to keep the video continuous"]),
    "freeze_frame": (_d("freeze_frame", clip_id="$A", time=1.0, duration=2.0),
                     ["freeze frame of Clip 1 'talk.mp4'"]),
    "set_clip_reverse": (_d("set_clip_reverse", clip_id="$C", reverse=True), ["Clip 3 'talk.mp4': plays backwards"]),
    "smooth_slow_motion": (_sim_slowmo, ["Clip 2 'talk.mp4': speed 1x -> 0.5x", "media"]),
    "stabilize": (lambda st, ids, m: _cache_src(st, ids["A"], "stabilize"), ["Clip 1 'talk.mp4': media"]),
    "upscale": (lambda st, ids, m: _cache_src(st, ids["A"], "upscale"), ["Clip 1 'talk.mp4': media"]),
    "trim_clip": (_d("trim_clip", clip_id="$C", out=11.0), ["Deleted 00:00:11:00-00:00:12:00 of the video"]),
    "split_at": (_d("split_at", track="v1", time=2.0), ["Split Clip 1 'talk.mp4' at 00:00:02:00"]),
    "set_clip_timing": (_d("set_clip_timing", clip_id="$title", start=1.0, end=4.0),
                        ["Title 'Summer Trip': 00:00:00:00-00:00:03:00 -> 00:00:01:00-00:00:04:00"]),
    "move_clip": (_d("move_clip", clip_id="$title", new_start=5.0), ["Title 'Summer Trip'", "00:00:05:00"]),
    "reorder_clips": (_d("reorder_clips", track="v1", order=["$C", "$A", "$B"]),
                      ["Reordered the video: clips now play in the order 3, 1, 2"]),
    "bulk_delete": (_d("bulk_delete", clip_ids=["$A", "$C"]),
                    ["Deleted Clip 1 'talk.mp4' (00:00:00:00-00:00:04:00)", "Deleted Clip 3 'talk.mp4'"]),
    "bulk_duplicate": (_d("bulk_duplicate", clip_ids=["$A"]), ["Duplicated Clip 1 'talk.mp4'"]),
    "duplicate_clip": (_d("duplicate_clip", clip_id="$B"), ["Duplicated Clip 2 'talk.mp4'"]),
    "detach_audio": (_d("detach_audio", clip_id="$A"), ["Clip 1 'talk.mp4': muted"]),
    # Editor Brain (EB1-B): source-range cuts read as deletions; an angle swap
    # is a camera line, never a deletion; the dialogue lane is one line plus
    # the muted camera sound (the fixture's bed stands in for the recorder,
    # and its music-lane copy is taken as the upload handoff's placement).
    "cut_source_ranges": (lambda st, ids, m: dispatch(st, "cut_source_ranges", {
                              "track": "v1", "ranges": [{"src": str(m["src"]), "start": 1.0, "end": 2.0},
                                                        {"src": str(m["src"]), "start": 9.0, "end": 9.5}]}),
                          ["Deleted 00:00:01:00-00:00:02:00 of the video (part of Clip 1 'talk.mp4')",
                           "Deleted 00:00:09:00-00:00:09:15 of the video (part of Clip 3 'talk.mp4')"]),
    "apply_camera_plan": (lambda st, ids, m: dispatch(st, "apply_camera_plan", {
                              "switches": [{"src": str(m["src"]), "at_src": 5.0, "until_src": 7.0,
                                            "angle_src": str(m["angle"])}],
                              "offsets": {str(m["src"]): 0.0, str(m["angle"]): 0.5}}),
                          ["Split Clip 2 'talk.mp4' at 00:00:05:00 and 00:00:07:00",
                           "Camera: 00:00:05:00-00:00:07:00 shows 'angle.mp4' instead of 'talk.mp4'"]),
    "sync_dialogue_lane": (lambda st, ids, m: dispatch(st, "sync_dialogue_lane", {
                               "src": str(m["bed"]), "lane": "a1", "offsets": {str(m["src"]): 0.0}}),
                           ["Dialogue from 'bed.wav' on the Main audio track: 3 pieces in step with the video",
                            "the recorder was on the Music lane, it is now the dialogue",
                            "Camera sound muted on 3 video clips: the dialogue plays from the Main audio track"]),
    # stage 3
    "make_shorts": (_d("make_shorts", target_count=2, max_dur=6.0, min_dur=3.0), []),
    # stage 4 — look
    "apply_lut": (_d("apply_lut", clip_id="$A", src="warm"), ["Clip 1 'talk.mp4': added look 'warm'"]),
    "color_grade": (_d("color_grade", clip_id="$B", contrast=1.3), ["Clip 2 'talk.mp4': added colour grade"]),
    "match_style": (_sim_match_style, ["All 3 clips of the video: added colour grade"]),
    "chroma_key": (_d("chroma_key", clip_id="$A", color="#00FF00"), ["Clip 1 'talk.mp4': green screen none -> set"]),
    "add_mask": (_d("add_mask", clip_id="$A", type="circle"), ["Clip 1 'talk.mp4': mask none -> circle"]),
    "remove_mask": (_d("remove_mask", clip_id="$A"), ["Clip 1 'talk.mp4': mask circle -> none"],
                    _d("add_mask", clip_id="$A", type="circle")),
    "remove_effect": (_d("remove_effect", clip_id="$A", index=0), ["Clip 1 'talk.mp4': removed look 'warm'"],
                      _d("apply_lut", clip_id="$A", src="warm")),
    "set_clip_transform": (_d("set_clip_transform", clip_id="$A", scale=1.5, rotation=90),
                           ["Clip 1 'talk.mp4': zoom 100% -> 150%", "Clip 1 'talk.mp4': rotation 0° -> 90°"]),
    "remove_effects": (_d("remove_effects", clip_ids=["$A"]), ["Clip 1 'talk.mp4': removed look 'warm'"],
                       _d("apply_lut", clip_id="$A", src="warm")),
    "flip_clip": (_d("flip_clip", clip_id="$B", axis="horizontal"), ["Clip 2 'talk.mp4': flipped horizontally"]),
    "set_blend_mode": (lambda st, ids, m: (dispatch(st, "add_clip", {"track": "v2", "src": str(m["src"]),
                                                                     "in": 0, "out": 2.0, "start": 1.0}),
                                           dispatch(st, "set_blend_mode", {
                                               "clip_id": st.edl.get_track("v2").clips[0].id, "mode": "screen"})),
                       ["Added overlay 'talk.mp4' at 00:00:01:00 (2.0 s)"]),
    "set_animation": (_d("set_animation", clip_id="$A", **{"in": "fade_in"}), ["Clip 1 'talk.mp4': intro animation"]),
    "add_keyframe": (_d("add_keyframe", clip_id="$A", prop="scale", time=1.0, value=1.4),
                     ["Clip 1 'talk.mp4': zoom 100% -> animated (100% to 140%, 2 keyframes)"]),
    "remove_keyframe": (_d("remove_keyframe", clip_id="$A", prop="scale", time=1.0),
                        ["Clip 1 'talk.mp4': zoom animated (100% to 140%, 2 keyframes) -> 100%"],
                        _d("add_keyframe", clip_id="$A", prop="scale", time=1.0, value=1.4)),
    "set_clip_z": (lambda st, ids, m: (st.edl.get_track("stickers").clips.append(
                       Sticker(id="st_fixed01", src=str(m["src"]), start=0.0, end=2.0, label="star")),
                       dispatch(st, "set_clip_z", {"clip_id": "st_fixed01", "z": 5})),
                   ["Added sticker 'star' (00:00:00:00-00:00:02:00)"]),
    "apply_template": (_d("apply_template", name="tech_tip"), []),
    "apply_show_template": (_d("apply_show_template", name="quicksolutions_techtip"), []),
    # stage 5 — transitions
    "add_transition": (_d("add_transition", at=8.0, type="slideleft", duration=0.8),
                       ["Added transition Slide Left 0.8 s at 00:00:08:00"]),
    "remove_transition": (_d("remove_transition", at=4.0), ["Removed transition"]),
    "set_video_fade": (_d("set_video_fade", clip_id="$A", in_s=1.0), ["Clip 1 'talk.mp4': fade in 0 s -> 1 s"]),
    # stage 6 — frame
    "auto_reframe": (_sim_reframe, ["Canvas 1920x1080 (16:9) -> 1080x1920 (9:16)", "fit to frame -> fill frame"]),
    "set_clip_fit": (_d("set_clip_fit", clip_id="$A", fit="cover"), ["Clip 1 'talk.mp4': fit to frame -> fill frame"]),
    "set_canvas": (_d("set_canvas", w=1080, h=1080), ["Canvas 1920x1080 (16:9) -> 1080x1080 (1:1)"]),
    "set_aspect_ratio": (_d("set_aspect_ratio", ratio="9:16"), ["Canvas 1920x1080 (16:9) -> 1080x1920 (9:16)"]),
    "set_pip_framing": (lambda st, ids, m: (dispatch(st, "add_clip", {"track": "v2", "src": str(m["src"]), "in": 0,
                                                                      "out": 2.0, "start": 1.0}),
                                            dispatch(st, "set_pip_framing", {
                                                "clip_id": st.edl.get_track("v2").clips[0].id, "zoom": 1.5})),
                        ["Added overlay 'talk.mp4' at 00:00:01:00"]),
    "set_canvas_background": (_d("set_canvas_background", clip_id="$A", type="blur"),
                              ["Clip 1 'talk.mp4': background none -> blur"]),
    # stage 7 — captions
    "auto_caption": (_sim_caption, ["Rebuilt the captions: 3 -> 3 lines (00:00:00:06-00:00:07:18)", "Captions style 'default' -> 'ig_chunky'"]),
    "add_caption_track": (_d("add_caption_track", style="ig_chunky", rebuild=True), ["Captions style 'default' -> 'ig_chunky'"]),
    "translate_captions": (_sim_translate, ["Changed the words of 3 caption lines", "Captions language default -> 'hi'"]),
    "set_caption_style": (_d("set_caption_style", color="#FFD400"), ["Captions colour default -> yellow", "3 caption lines: colour white -> yellow"]),
    # stage 8 — text
    "apply_hook_stack": (_d("apply_hook_stack", text="Wait for it", duration=2.0), ["Added title 'WAIT FOR IT'"]),
    "add_hook_overlay": (_d("add_hook_overlay", text="Watch this", duration=2.0), ["Added title 'WATCH THIS'"]),
    "generate_hook": (_d("generate_hook"), []),
    "add_text": (_d("add_text", text="Day One", start=4.0, end=6.0), ["Added title 'Day One' (00:00:04:00-00:00:06:00)"]),
    "add_super_text": (_d("add_super_text", text="BIG NEWS", start=5.0, end=7.0), ["Added title 'BIG NEWS'"]),
    "add_lower_third": (_d("add_lower_third", name="Asha Rao", start=1.0, end=4.0), ["Added title 'Asha Rao'"]),
    "set_text": (_d("set_text", clip_id="$title", text="Winter Trip"),
                 ["Title 'Summer Trip': words 'Summer Trip' -> 'Winter Trip'"]),
    "set_text_style": (_d("set_text_style", clip_id="$title", color="#FF3B30"),
                       ["Title 'Summer Trip': colour white -> red"]),
    "apply_text_template": (_d("apply_text_template", name="big_question", fields={"text": "Why?"},
                               start=1.0, end=3.0), ["Added title"]),
    "apply_brand_kit": (_d("apply_brand_kit", handle="@asha"), ["@asha"]),
    "tts_voiceover": (_sim_tts, ["Added voice-over 'tts_ab12.wav' at 00:00:01:00 (2.5 s)"]),
    # stage 9 — music
    "add_music": (_d("add_music", src="$bed", start=4.0, volume_db=-10.0, duck=False),
                  ["Added music 'bed.wav' at 00:00:04:00 (7.5 s, volume -10 dB)"]),
    "set_duck": (_d("set_duck", track="music", enabled=True, to_db=-20.0),
                 ["Music track: ducks to -20 dB under the voice"]),
    "auto_cut_to_beats": (_sim_beats, ["Split Clip 1 'talk.mp4' at 00:00:02:00", "Split Clip 2 'talk.mp4' at 00:00:06:00"]),
    "fit_music_to_video": (_d("fit_music_to_video", fade_out=2.0),
                           ["Music 'bed.wav': length 12.0 s -> 11.5 s (ends at 00:00:11:15)",
                            "Music 'bed.wav': sound fade out 1 s -> 2 s"]),
    # stage 10 — audio
    "noise_reduce": (lambda st, ids, m: _cache_src(st, ids["A"], "denoise"), ["Clip 1 'talk.mp4': media"]),
    "set_loudness_target": (_d("set_loudness_target", lufs=-14.0), ["Export loudness target -16 LUFS -> -14 LUFS"]),
    "set_volume": (_d("set_volume", target="$music", db=-20.0), ["Music 'bed.wav': volume -14 dB -> -20 dB"]),
    "set_clip_muted": (_d("set_clip_muted", clip_id="$B", muted=True), ["Clip 2 'talk.mp4': muted"]),
    "set_track_muted": (_d("set_track_muted", track="music", muted=True), ["Music track: muted"]),
    "add_fade": (_d("add_fade", clip_id="$music", out_s=2.0), ["Music 'bed.wav': sound fade out 1 s -> 2 s"]),
    "set_track_solo": (_d("set_track_solo", track="music", solo=True), ["Music track: solo on"]),
    "set_voice_effect": (_d("set_voice_effect", clip_id="$A", effect="robot"),
                         ["Clip 1 'talk.mp4': voice effect none -> 'robot'"]),
    # stage 11
    "apply_export_preset": (_d("apply_export_preset", name="tiktok"), ["Canvas 1920x1080 (16:9) -> 1080x1920 (9:16)"]),
}

#: Read-only tools: a plan may run them, they change nothing — the card has
#: no line for them (and the dry run skips the two that render).
READ_ONLY: frozenset[str] = frozenset({
    "get_timeline", "get_clip", "get_transcript", "check_features", "list_filters", "list_luts",
    "list_templates", "list_text_styles", "list_transitions", "find_moments", "audit_aesthetic",
    "render_preview", "remove_marker", "transcribe",
})


def test_every_plan_tool_is_covered():
    """A plan tool with no row here has no proof its change reaches the card."""
    tools = set(TOOL_STAGE)
    assert tools == set(OPS) | READ_ONLY, sorted(tools ^ (set(OPS) | READ_ONLY))
    assert not set(OPS) & READ_ONLY


@pytest.mark.usefixtures("no_downloads")
@pytest.mark.parametrize("tool", sorted(OPS))
def test_tool_change_is_summarized(media, tool):
    st, ids = _session(media)
    ids = {**ids, "bed": str(media["bed"])}
    how, words, *pre = OPS[tool]
    if pre:
        pre[0](st, ids, media)          # the thing a remove_* tool removes
    before = st.edl.model_copy(deep=True)
    how(st, ids, media)
    after = st.edl
    lines = C.summarize(before, after, session_dir=Path(st.dir))
    text = [c.text for c in lines]
    missing = C.diff_keys(before, after) - C.covered(lines)
    assert not missing, f"{tool}: changed keys with no line: {sorted(missing)}\n{text}"
    for w in words:
        assert any(w in line for line in text), f"{tool}: expected {w!r} in {text}"
    if C.diff_keys(before, after):
        assert text, f"{tool}: the tree changed and the card is empty"
    else:
        assert not text, f"{tool}: nothing changed and the card says {text}"


def test_unchanged_tree_has_no_lines(media):
    st, _ = _session(media)
    assert C.summarize(st.edl, st.edl.model_copy(deep=True)) == []


def test_change_list_caps_with_a_count():
    lines = [C.Change(group="Video", text=f"line {i}") for i in range(30)]
    cl = C.change_list(lines, cap=12)
    assert cl.total == 30 and len(cl.lines) == 11 and cl.more == 19
    assert cl.lines[0] == "line 0" and cl.all_lines == [f"line {i}" for i in range(30)]
    assert C.more_line(19) == "and 19 more changes" and C.more_line(1) == "and 1 more change"
    short = C.change_list(lines[:12], cap=12)
    assert short.more == 0 and len(short.lines) == 12


def test_many_deletions_group_into_one_line(media):
    st, ids = _session(media)
    before = st.edl.model_copy(deep=True)
    for a in (10.5, 9.0, 7.0, 5.0, 3.0, 1.0):          # latest first, like remove_silences
        dispatch(st, "cut_range", {"track": "v1", "start": a, "end": a + 0.5})
    lines = C.summarize(before, st.edl, session_dir=Path(st.dir))
    assert any(x.text.startswith("Deleted 6 parts of the video (3.0 s in total)") for x in lines), \
        [x.text for x in lines]
    assert not C.diff_keys(before, st.edl) - C.covered(lines)


def test_same_change_on_every_clip_is_one_line(media):
    st, ids = _session(media)
    before = st.edl.model_copy(deep=True)
    for k in ("A", "B", "C"):
        dispatch(st, "set_clip_muted", {"clip_id": ids[k], "muted": True})
    text = [c.text for c in C.summarize(before, st.edl)]
    assert "All 3 clips of the video: muted" in text, text


def test_shorts_are_listed_from_the_result():
    lines = PV.shorts_lines([{"shorts": [{"start": 1.0, "end": 7.0}, {"start": 8.0, "end": 12.0}]}], 30)
    assert lines == ["Create 2 new short projects (00:00:01:00-00:00:07:00, 00:00:08:00-00:00:12:00); "
                     "this timeline is not changed by them"]
    assert PV.shorts_lines([{"shorts": []}], 30) == []


# ------------------------------------------------------------------ the property


def _leaf_values(v: Any, rng: random.Random) -> Any:
    if isinstance(v, bool):
        return not v
    if isinstance(v, (int, float)):
        return type(v)(v + rng.choice((1, 2, 5, -1))) if not isinstance(v, bool) else v
    if isinstance(v, str):
        return v + "x" if not v.startswith("#") else "#FF3B30"
    if v is None:
        return rng.choice((1.0, "fade", True))
    return v


def _mutate_leaf(edl: EDL, rng: random.Random) -> EDL | None:
    """Change one random leaf of the model dump and re-validate it; None when
    the new value is not a valid tree (the mutation is skipped)."""
    dump = edl.model_dump(mode="json", by_alias=True)
    paths: list[list[Any]] = []

    def walk(v: Any, path: list[Any]) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                if k in ("id", "version", "duration"):
                    continue
                walk(x, path + [k])
        elif isinstance(v, list) and v and isinstance(v[0], (dict, list)):
            for i, x in enumerate(v):
                walk(x, path + [i])
        else:
            paths.append(path)

    walk(dump, [])
    path = rng.choice(paths)
    node = dump
    for p in path[:-1]:
        node = node[p]
    node[path[-1]] = _leaf_values(node[path[-1]], rng)
    try:
        return EDL.model_validate_json(json.dumps(dump))
    except Exception:  # noqa: BLE001 — an invalid value is not a tree to summarize
        return None


def _mutate_structure(edl: EDL, rng: random.Random) -> EDL:
    e = edl.model_copy(deep=True)
    tracks = [t for t in e.tracks if t.clips]
    choice = rng.randrange(7)
    if choice == 0 and tracks:
        t = rng.choice(tracks)
        t.clips.pop(rng.randrange(len(t.clips)))
    elif choice == 1:
        e.get_track("tx_super" if e.get_track("tx_super") else e.tracks[-1].id).clips.append(
            TextClip(text=f"T{rng.randrange(99)}", start=rng.uniform(0, 5), end=rng.uniform(6, 9)))
    elif choice == 2:
        e.get_track("v1").transitions.append(Transition(at=round(rng.uniform(1, 11), 2), type="wipeleft",
                                                        duration=0.4))
    elif choice == 3:
        e.markers.append(Marker(time=rng.uniform(0, 10), label="m"))
    elif choice == 4:
        v1 = e.get_track("v1")
        c = rng.choice(v1.clips)
        c.start = round(c.start + rng.uniform(-1, 3), 3)
    elif choice == 5:
        v1 = e.get_track("v1")
        v1.clips.reverse()
        for i, c in enumerate(sorted(v1.clips, key=lambda x: x.start)):
            c.start = float(i * 4)
    else:
        e.canvas.fps = rng.choice((24, 25, 60))
    return e


def test_random_mutations_are_always_covered(media):
    st, _ = _session(media)
    base = st.edl.model_copy(deep=True)
    rng = random.Random(8080)
    checked = 0
    for i in range(400):
        cur = base
        for _ in range(rng.randrange(1, 4)):
            nxt = _mutate_leaf(cur, rng) if rng.random() < 0.6 else _mutate_structure(cur, rng)
            cur = nxt if nxt is not None else cur
        lines = C.summarize(base, cur)
        missing = C.diff_keys(base, cur) - C.covered(lines)
        assert not missing, f"iteration {i}: unclaimed {sorted(missing)}"
        assert bool(lines) == bool(C.diff_keys(base, cur))
        checked += 1
    assert checked == 400


def test_canonical_ignores_new_ids_and_scratch_paths(media):
    """The dry run's fingerprint must equal the Apply's: the same plan makes
    clips with fresh random ids and writes renders under another dir."""
    st, ids = _session(media)
    before = st.edl.model_copy(deep=True)
    a, b = before.model_copy(deep=True), before.model_copy(deep=True)
    a.get_track("tx_super").clips.append(TextClip(text="Hi", start=0, end=1))
    b.get_track("tx_super").clips.append(TextClip(text="Hi", start=0, end=1))
    a.get_track("v1").clips[0].src = "/scratch/run/s_x/cache/reframe_1.mp4"
    b.get_track("v1").clips[0].src = "/work/s_x/cache/reframe_1.mp4"
    assert a.hash() != b.hash()
    assert C.canonical(a, before, {"/scratch/run/s_x": "/work/s_x"}) == C.canonical(b, before)
    b.get_track("tx_super").clips[-1].text = "Hello"
    assert C.canonical(a, before, {"/scratch/run/s_x": "/work/s_x"}) != C.canonical(b, before)


# ------------------------------------------------------------------ pieces


def test_split_and_fade_in_one_plan_are_two_lines(media):
    """A split claims only what it explains: a fade added in the same plan
    is its own line (found live: "split at 5 s and fade in at the start"
    came out as one line, the fade missing from the card)."""
    st, ids = _session(media)
    before = st.edl.model_copy(deep=True)
    dispatch(st, "split_at", {"track": "v1", "time": 2.0})
    dispatch(st, "set_video_fade", {"clip_id": ids["A"], "in_s": 1.0})
    text = [c.text for c in C.summarize(before, st.edl)]
    assert "Split Clip 1 'talk.mp4' at 00:00:02:00" in text, text
    assert "Clip 1 'talk.mp4': fade in 0 s -> 1 s" in text, text


def test_a_fade_that_moves_with_a_split_is_the_split(media):
    """A clip with a fade-out split in two: the fade now sits on the tail
    piece — that is the split, not a second change."""
    st, ids = _session(media)
    dispatch(st, "set_video_fade", {"clip_id": ids["A"], "out_s": 1.0})
    before = st.edl.model_copy(deep=True)
    dispatch(st, "split_at", {"track": "v1", "time": 2.0})
    lines = C.summarize(before, st.edl)
    assert [c.text for c in lines] == ["Split Clip 1 'talk.mp4' at 00:00:02:00"]
    assert not C.diff_keys(before, st.edl) - C.covered(lines)


def test_a_split_that_drops_a_fade_says_so(media):
    """If the fade is GONE from every piece, that is not a split's doing."""
    st, ids = _session(media)
    dispatch(st, "set_video_fade", {"clip_id": ids["A"], "out_s": 1.0})
    before = st.edl.model_copy(deep=True)
    dispatch(st, "split_at", {"track": "v1", "time": 2.0})
    for c in st.edl.get_track("v1").clips:
        c.video_fade_out = 0.0
    text = [c.text for c in C.summarize(before, st.edl)]
    assert any("fade out 1 s -> 0 s" in t for t in text), text


# ------------------------------------------------------------------ final sweep 3


def test_a_grade_change_with_a_new_look_is_on_the_card(media):
    """CRITICAL (final sweep 3): 'more contrast on clip 1 and make it black
    and white' changed the existing grade AND added a look; the card said
    only "added look 'mono'" — a parameter change was dropped whenever an
    effect was also added or removed."""
    st, ids = _session(media)
    dispatch(st, "color_grade", {"clip_id": ids["A"], "brightness": 0.1})
    before = st.edl.model_copy(deep=True)
    dispatch(st, "color_grade", {"clip_id": ids["A"], "brightness": 0.1, "contrast": 1.2})
    dispatch(st, "apply_lut", {"clip_id": ids["A"], "src": "mono", "intensity": 0.8})
    text = [c.text for c in C.summarize(before, st.edl)]
    line = next((t for t in text if t.startswith("Clip 1 ")), "")
    assert "added look 'mono'" in line and "contrast" in line, text


def test_an_added_grade_and_look_say_their_amounts(media):
    """LOW: 'added colour grade' hid the +0.1 brightness, "added look 'mono'"
    its strength."""
    st, ids = _session(media)
    before = st.edl.model_copy(deep=True)
    dispatch(st, "color_grade", {"clip_id": ids["A"], "brightness": 0.1})
    dispatch(st, "apply_lut", {"clip_id": ids["B"], "src": "mono", "intensity": 0.8})
    text = [c.text for c in C.summarize(before, st.edl)]
    assert any(t.startswith("Clip 1 ") and "brightness 0.1" in t for t in text), text
    assert any(t.startswith("Clip 2 ") and "80%" in t for t in text), text


def test_a_param_change_and_a_new_effect_in_random_mutations(media):
    """The never-omit property with an existing effect's params changed AND
    another effect added in the same mutation — and the params are SAID."""
    st, ids = _session(media)
    dispatch(st, "color_grade", {"clip_id": ids["B"], "saturation": 1.1})
    base = st.edl.model_copy(deep=True)
    rng = random.Random(3)
    for _ in range(40):
        cur = base.model_copy(deep=True)
        _, c = cur.get_clip(ids["B"])
        k = rng.choice(("saturation", "contrast", "brightness"))
        v = round(rng.uniform(0.5, 1.5), 2)
        c.effects = [Effect(type=c.effects[0].type, params={**c.effects[0].params, k: v}),
                     Effect(type="lut", params={"src": rng.choice(("warm", "mono")), "intensity": 1.0})]
        lines = C.summarize(base, cur)
        assert not C.diff_keys(base, cur) - C.covered(lines)
        line = next(x.text for x in lines if x.text.startswith("Clip 2 "))
        if base.get_clip(ids["B"])[1].effects[0].params.get(k) != v:
            assert k in line, line


@pytest.mark.parametrize("at,clip", [(5.0, "Clip 2"), (9.0, "Clip 3")])
def test_a_freeze_names_the_clip_it_freezes(media, at, clip):
    """CRITICAL: three pieces of one upload share the file; the freeze line
    named the FIRST clip with that file, not the one frozen."""
    st, ids = _session(media)
    before = st.edl.model_copy(deep=True)
    dispatch(st, "freeze_frame", {"time": at, "duration": 3.0})
    text = [c.text for c in C.summarize(before, st.edl)]
    line = next((t for t in text if "freeze frame of" in t), "")
    assert f"freeze frame of {clip} 'talk.mp4'" in line, text


@pytest.mark.parametrize("op", ["speed", "duplicate"])
def test_transitions_that_follow_their_cuts_are_one_move(media, op):
    """HIGH: a dissolve at 4 s and a fade at 8 s both move 4 s later; the fade
    landed on the dissolve's old time and the card said 'Fade -> Dissolve',
    'Removed Dissolve', 'Added Fade' for what was one move."""
    st, ids = _session(media)
    dispatch(st, "add_transition", {"at": 8.0, "type": "fade", "duration": 0.5})
    before = st.edl.model_copy(deep=True)
    if op == "speed":
        dispatch(st, "set_speed", {"clip_id": ids["A"], "factor": 0.5})
    else:
        dispatch(st, "duplicate_clip", {"clip_id": ids["A"]})
    text = [c.text for c in C.summarize(before, st.edl)]
    assert "2 transitions move with their cuts" in text, text
    assert not any(t.startswith(("Removed transition", "Added transition", "Transition at")) for t in text), text


def test_a_retyped_transition_is_still_a_retype(media):
    st, ids = _session(media)
    before = st.edl.model_copy(deep=True)
    tr = st.edl.get_track("v1").transitions[0]
    tr.type = "wipeleft"
    text = [c.text for c in C.summarize(before, st.edl)]
    assert any(t.startswith("Transition at 00:00:04:00: Dissolve") for t in text), text


def test_hundreds_of_cuts_summarize_in_linear_work(media, monkeypatch):
    """HIGH: 'remove silences' on a 10-minute clip spent 24 s writing one line —
    every merged span re-walked every piece of the one cut clip, and each
    walk scanned every pending key (N·(N+1) keys_of calls, cubic time)."""
    from video_ai_editor.agent.prompt import change_rules as R
    st, ids = _session(media)
    before = st.edl.model_copy(deep=True)
    v1 = before.get_track("v1")
    a = next(c for c in v1.clips if c.id == ids["A"])
    v1.clips = [a.model_copy(update={"in_": 0.0, "out": 600.0, "start": 0.0})]
    after = before.model_copy(deep=True)
    n = 300
    after.get_track("v1").clips = [
        a.model_copy(update={"id": a.id if i == 0 else f"c_{a.id[2:]}_{i:04d}", "in_": 2.0 * i,
                             "out": 2.0 * i + 1.0, "start": float(i)}) for i in range(n)]
    calls = [0]
    real = R.Summary.keys_of

    def counting(self, cid):
        calls[0] += 1
        return real(self, cid)
    monkeypatch.setattr(R.Summary, "keys_of", counting)
    lines = C.summarize(before, after)
    assert any(x.text.startswith(f"Deleted {n} parts of the video") for x in lines), [x.text for x in lines][:3]
    assert not C.diff_keys(before, after) - C.covered(lines)
    assert calls[0] <= 3 * n, calls[0]


def test_a_new_title_is_one_change_not_two(media):
    """LOW: 'Added the Text track' was counted as a change of its own."""
    (media["src"].parent / "ingest.json").write_bytes(media["ingest"])
    st = EDLStore(media["root"] / "s_pc_title")
    dispatch(st, "add_clip", {"track": "v1", "src": str(media["src"]), "in": 0, "out": F.CLIP_DUR, "start": 0})
    before = st.edl.model_copy(deep=True)
    dispatch(st, "add_text", {"text": "Buy now", "start": 8.0, "end": 11.0})
    lines = C.summarize(before, st.edl)
    # final sweep 3 r2: the line also says how the new title will look
    assert [c.text for c in lines] == ["Added title 'Buy now' (00:00:08:00-00:00:11:00): bottom centre; size 96; "
                                       "white text, black outline"], [c.text for c in lines]
    assert not C.diff_keys(before, st.edl) - C.covered(lines)


def test_a_moved_sticker_is_called_a_sticker(media):
    """LOW: '1 caption, title or sound moves with the video' when the only
    layer that moved is a sticker."""
    (media["src"].parent / "ingest.json").write_bytes(media["ingest"])
    st = EDLStore(media["root"] / "s_pc_sticker")
    dispatch(st, "add_clip", {"track": "v1", "src": str(media["src"]), "in": 0, "out": F.CLIP_DUR, "start": 0})
    before = st.edl.model_copy(deep=True)
    lane = next((t for t in before.tracks if t.type == "sticker"), None)
    if lane is None:
        lane = type(before.tracks[0])(id="stickers", type="sticker", clips=[])
        before.tracks.append(lane)
    lane.clips.append(Sticker(src=str(media["src"]), start=5.0, end=7.0, label="logo"))
    after = before.model_copy(deep=True)
    after.get_track("v1").clips[0].in_ = 2.0
    moved = next(t for t in after.tracks if t.type == "sticker").clips[0]
    moved.start, moved.end = 3.0, 5.0
    text = [c.text for c in C.summarize(before, after)]
    assert "1 sticker moves with the video" in text, text


def test_speed_unset_and_1x_are_the_same(media):
    """LOW: 'Clips 1, 3: speed 1x -> 1x' for a null -> 1.0 write."""
    st, ids = _session(media)
    before = st.edl.model_copy(deep=True)
    for k in ("A", "C"):
        st.edl.get_clip(ids[k])[1].speed = 1.0
    assert C.summarize(before, st.edl) == []


def test_a_keyframed_zoom_says_its_amounts(media):
    """LOW: 'zoom 100% -> animated (2 keyframes)' while the reply said 100% -> 115%."""
    st, ids = _session(media)
    before = st.edl.model_copy(deep=True)
    from video_ai_editor.edl.schema import Keyframe
    st.edl.get_clip(ids["B"])[1].transform.scale = Keyframe(keyframes=[(0.0, 1.0), (4.0, 1.15)])
    text = [c.text for c in C.summarize(before, st.edl)]
    assert "Clip 2 'talk.mp4': zoom 100% -> animated (100% to 115%, 2 keyframes)" in text, text


def test_an_all_caps_title_is_named_as_it_renders(media):
    """LOW: "Added title 'welcome'" for a title that renders WELCOME."""
    st, ids = _session(media)
    before = st.edl.model_copy(deep=True)
    dispatch(st, "add_text", {"text": "welcome", "start": 5.0, "end": 7.0})
    t = next(c for tr in st.edl.tracks for c in tr.clips if getattr(c, "text", None) == "welcome")
    t.style.upper = True
    text = [c.text for c in C.summarize(before, st.edl)]
    assert any(x.startswith("Added title 'WELCOME'") for x in text), text


def _reframe_the_main_lane(st: EDLStore, media: dict, *, record: bool) -> None:
    """auto_reframe's EDL effect on a SPLIT main lane: every piece plays the
    reframe render of the same upload (dispatch records `<render>.origin`)."""
    from video_ai_editor.agent import media_origin
    out = Path(st.dir) / "cache" / f"reframe_{'ab12cd34' if record else 'ee55ff66'}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"\0")
    if record:
        media_origin.record_origin(out, media["src"])
    for c in st.edl.get_track("v1").clips:
        c.src = str(out)


def test_a_reframe_of_a_split_lane_is_never_a_camera_change(media):
    """UX-14: with brain.enabled off the legacy 45-second reel's card said
    "Camera: 8 spans (…) show 'th_16x9.mp4 (reframed)' instead of
    'th_16x9.mp4'": eight pieces of one clip now play the reframe render, and
    the angle rule took a derivative of the SAME upload for another camera."""
    st, ids = _session(media)
    before = st.edl.model_copy(deep=True)
    for t in (2.0, 6.0, 10.0):                                    # the reel's cuts split the clips first
        dispatch(st, "split_at", {"track": "v1", "time": t})
    _reframe_the_main_lane(st, media, record=True)
    lines = [c.text for c in C.summarize(before, st.edl)]
    assert not any(t.startswith("Camera:") for t in lines), lines
    assert not any("instead of" in t for t in lines), lines
    assert any("media" in t for t in lines), lines               # said as the media change it is


def test_a_second_upload_is_still_a_camera(media):
    """The other side of UX-14: a piece that plays ANOTHER upload (no
    `.origin` tying it to the clip's own file) is the brain's angle swap."""
    st, ids = _session(media)
    before = st.edl.model_copy(deep=True)
    dispatch(st, "apply_camera_plan", {
        "switches": [{"src": str(media["src"]), "at_src": 5.0, "until_src": 7.0, "angle_src": str(media["angle"])}],
        "offsets": {str(media["src"]): 0.0, str(media["angle"]): 0.5}})
    lines = [c.text for c in C.summarize(before, st.edl)]
    assert any(t.startswith("Camera: ") and "'angle.mp4' instead of 'talk.mp4'" in t for t in lines), lines
