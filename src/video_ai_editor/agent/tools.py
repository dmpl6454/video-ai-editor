"""Tool registry — JSON schemas for Claude tool use.

Tools are organised by category. M1 ships with the inspection + edit + project
tools that the timeline UI needs to function. Effects/AI tools land in M2+.
"""
from __future__ import annotations
from typing import Callable

# Each tool is registered with: name, category, schema (Anthropic tool format),
# and a handler function. Handlers live in dispatch.py and are bound at import time.

ToolSchema = dict


def _transition_names() -> list[str]:
    """The transition `type` enum, generated from the render catalog so the
    schema can never drift stale again (it used to hardcode 12 of 45+ names)."""
    from ..render.transitions import all_names
    return all_names()


def _effect_names() -> list[str]:
    """The `add_effect` type enum, generated from the render registry — the
    same source `list_filters` reports and `add_effect` now validates against.
    The hardcoded list this replaces had already drifted (it was missing
    `color_grade`), so the advertised contract and the real one disagreed."""
    from ..render.effects import EFFECT_BUILDERS
    return sorted(EFFECT_BUILDERS.keys())


def _speed_preset_ids() -> list[str]:
    """The `set_speed.preset` enum, from the ONE preset table
    (`edl/speed_presets.py`), which the Inspector also reads."""
    from ..edl.speed_presets import PRESET_IDS
    return list(PRESET_IDS)


def _speed_preset_list() -> str:
    from ..edl.speed_presets import PRESETS
    return ", ".join(f"{p.id} ({p.label}: {p.hint.lower()})" for p in PRESETS)


def _anim_ids(kind: str) -> list[str]:
    """`set_animation`'s enums, from the ONE preset table
    (`edl/clip_animations.py`), which the Inspector also reads."""
    from ..edl.clip_animations import PRESET_IDS
    return list(PRESET_IDS[kind])


def _anim_list(kind: str) -> str:
    from ..edl.clip_animations import PRESETS
    return ", ".join(f"{p.id} ({p.hint[0].lower() + p.hint[1:]})" for p in PRESETS[kind])


def _blend_ids() -> list[str]:
    """`set_blend_mode.mode`, from the ONE table (`edl/canvas_blend.py`)."""
    from ..edl.canvas_blend import BLEND_IDS
    return list(BLEND_IDS)


def _blend_list() -> str:
    from ..edl.canvas_blend import BLENDS
    # the porter-duff pair are also known by their Photoshop names
    return ", ".join(f"{b.id} ({b.aliases[0]})" if b.porter_duff and b.aliases else b.id for b in BLENDS)


def _canvas_kinds() -> list[str]:
    """`set_canvas_background.type`, from the ONE table, plus 'none'."""
    from ..edl.canvas_blend import CANVAS_KINDS
    return [*CANVAS_KINDS, "none"]


def _canvas_blur_max() -> int:
    from ..edl.canvas_blend import CANVAS_BLUR_LEVELS
    return len(CANVAS_BLUR_LEVELS)


def _anim_dur_range() -> tuple[float, float]:
    from ..edl.clip_animations import ANIM_DUR_RANGE
    return (float(ANIM_DUR_RANGE[0]), float(ANIM_DUR_RANGE[1]))


def _voice_effect_ids() -> list[str]:
    """The `set_voice_effect.effect` enum, from the ONE preset table
    (`edl/voice_effects.py`), which the Inspector also reads."""
    from ..edl.voice_effects import PRESET_IDS
    return list(PRESET_IDS)


def _voice_effect_list() -> str:
    from ..edl.voice_effects import PRESETS
    return ", ".join(f"{p.id} ({p.label}: {p.hint[0].lower() + p.hint[1:]})" for p in PRESETS)


def _t(name: str, description: str, category: str, properties: dict, required: list[str] | None = None) -> ToolSchema:
    return {
        "name": name,
        "description": description,
        "category": category,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
        },
    }


# --- Inspection ---

INSPECTION_TOOLS = [
    _t("get_timeline", "Return a summary of the current EDL (track + clip counts, duration). "
       "Pass summary=False to return the full EDL JSON.",
       "inspection",
       {"summary": {"type": "boolean", "default": True}}),
    _t("get_clip", "Return one clip's full state by id.", "inspection",
       {"clip_id": {"type": "string"}}, ["clip_id"]),
    _t("get_transcript", "Return the word-level transcript for the project.", "inspection", {}),
    _t("find_broll",
       "Keyword-search the local b-roll folder (filenames, folder names, sidecar "
       ".txt tags). Returns ranked candidate clips to add_clip onto v2.",
       "inspection",
       {"query": {"type": "string"},
        "bin": {"type": "string", "description": "Override the b-roll folder path"},
        "top_k": {"type": "integer", "default": 8},
        "max_duration": {"type": "number", "description": "Skip candidates longer than this"}},
       ["query"]),
]

# --- Timeline edits ---

EDIT_TOOLS = [
    _t("add_clip", "Add a new clip to a track. Source path must already be ingested.",
       "edit",
       {
           "track": {"type": "string", "description": "Track id, e.g. 'v1'"},
           "src": {"type": "string", "description": "Path to source media"},
           "in": {"type": "number", "description": "Source in-point seconds"},
           "out": {"type": "number", "description": "Source out-point seconds"},
           "start": {"type": "number", "description": "Timeline start seconds"},
       },
       ["track", "src", "in", "out", "start"]),
    _t("cut_range", "Remove a time range from a track and ripple-close the gap. "
       "Equivalent to selecting the range and pressing delete with ripple on.",
       "edit",
       {
           "track": {"type": "string"},
           "start": {"type": "number"},
           "end": {"type": "number"},
           "dry_run": {"type": "boolean", "default": False},
       },
       ["track", "start", "end"]),
    _t("split_at", "Split every clip on a track that contains the given time, into two clips.",
       "edit",
       {"track": {"type": "string"}, "time": {"type": "number"}},
       ["track", "time"]),
    _t("trim_clip", "Adjust a clip's source in/out (does not move its timeline start, unless "
       "move_start is set on a non-main lane: then a head trim moves the start with it so the "
       "kept frames stay where they play).",
       "edit",
       {"clip_id": {"type": "string"}, "in": {"type": "number"}, "out": {"type": "number"},
        "move_start": {"type": "boolean", "default": False}},
       ["clip_id"]),
    _t("move_clip", "Move a clip to a new timeline start (and optional new track).",
       "edit",
       {"clip_id": {"type": "string"}, "new_start": {"type": "number"}, "new_track": {"type": "string"},
        "close_gap": {
            "type": "boolean",
            "description": "Video lanes only: after the move, pull the remaining "
                           "media clips left so the slot the clip vacated is closed "
                           "and the timeline doesn't grow. Off by default — use it "
                           "for reordering, not for placing a clip at an absolute "
                           "time (which would shuffle its neighbours).",
        }},
       ["clip_id", "new_start"]),
    _t("reorder_clips", "Reorder the clips on a track by listing their ids in the new order.",
       "edit",
       {"track": {"type": "string"}, "order": {"type": "array", "items": {"type": "string"}}},
       ["track", "order"]),
    _t("ripple_delete", "Delete a clip by id and close the gap on its track.",
       "edit",
       {"clip_id": {"type": "string"}}, ["clip_id"]),
    _t("duplicate_clip", "Duplicate a clip; the copy is appended right after the original.",
       "edit",
       {"clip_id": {"type": "string"}}, ["clip_id"]),
    _t("set_speed",
       "Set a media clip's playback speed — give exactly ONE of: `factor`, a constant "
       "speed (1.0 = normal, 2.0 = double, 0.5 = half; 0.1-100x); `preset`, a CapCut "
       "speed curve by name (" + _speed_preset_list() + "); or `curve`, a custom speed "
       "curve as [[position, speed], ...] where position is 0-1 across the clip as it "
       "plays and speed is 0.1-10x, linear between points (2-32 points, e.g. "
       "[[0,1],[0.5,0.25],[1,1]] slows to quarter speed in the middle). The clip's "
       "length on the timeline follows the speed and later clips ripple. "
       "`keep_pitch` (default true) time-stretches the sound at its own pitch; "
       "false is varispeed — sample-exact timing, pitch follows the speed like tape. "
       "Works on the main video track (v1), on overlay (picture-in-picture, v2+) "
       "clips and on audio tracks. An overlay clip retimes in place, picture and "
       "sound: nothing else moves, except that a slow-down running into the next "
       "clip on the same overlay lane pushes that lane's later clips right.",
       "edit",
       {"clip_id": {"type": "string"},
        "factor": {"type": "number", "description": "Constant speed, 0.1-100x"},
        "preset": {"type": "string", "enum": _speed_preset_ids(),
                   # The handler also takes "Jump Cut" / "flash-in" and names
                   # the presets in its refusal (edl/speed_presets.preset_id).
                   "x-validated-by-handler": True,
                   "description": "A speed-curve preset (CapCut's Curve menu)"},
        "curve": {"type": "array", "items": {"type": "array", "items": {"type": "number"}},
                  "description": "Custom curve: [[position 0-1, speed 0.1-10], ...]"},
        "keep_pitch": {"type": "boolean",
                       "description": "Omit to keep the clip's current setting"}},
       ["clip_id"]),
    _t("set_voice_effect",
       "Voice changer (CapCut's Voice effects): put a voice effect on the SOUND of a clip "
       "— `clip_id`, several `clip_ids`, or every clip on a `track` (e.g. 'vo' for the "
       "voice-over, 'v1' for the main video, 'music', an overlay lane) — or remove it with "
       "effect 'none'. Effects: " + _voice_effect_list() + ". `intensity` 0-1 scales it "
       "(1 = the preset as designed, the default; 0.5 = half the pitch shift / echo / "
       "reverb / filtering). Pitch effects keep the clip's length and timing. One undo step.",
       "edit",
       {"clip_id": {"type": "string"},
        "clip_ids": {"type": "array", "items": {"type": "string"}},
        "track": {"type": "string", "description": "Every clip on this lane (v1, v2, music, vo, a1…)"},
        "effect": {"type": "string", "enum": [*_voice_effect_ids(), "none"],
                   # The handler also takes "Hall", "walkie talkie", "helium"
                   # (edl/voice_effects.preset_id) and names them in its refusal.
                   "x-validated-by-handler": True,
                   "description": "A voice-effect preset id, or 'none' to remove it"},
        "intensity": {"type": "number", "description": "0-1 (default 1, or unchanged for the same effect)"}},
       ["effect"]),
    _t("set_animation",
       "Clip animation (CapCut's Animation: In / Out / Combo) on a media clip — the main "
       "video track or an overlay (picture-in-picture) lane — or a sticker: `clip_id`, "
       "several `clip_ids`, or every clip on a `track`. `in` plays as the clip appears: "
       + _anim_list("in") + ". `out` plays as it leaves: " + _anim_list("out") + ". "
       "`combo` loops over the whole clip: " + _anim_list("combo") + ". A combo replaces "
       "In and Out (and choosing an In or Out removes a combo). 'none' removes that side; "
       "an argument you leave out is unchanged. `in_duration` / `out_duration` in seconds "
       "(0.1-3, default 0.5; each side is also capped at 40% of the clip, so they never "
       "overlap). It rides on top of the clip's own position/scale/rotation and keyframes. "
       "Text titles have their own anim_in/anim_out (add_text). One undo step.",
       "edit",
       {"clip_id": {"type": "string"},
        "clip_ids": {"type": "array", "items": {"type": "string"}},
        "track": {"type": "string", "description": "Every clip on this lane (v1, v2, stickers…)"},
        # The handler also takes "Zoom In" / "zoom-in" / "fade" and names the
        # presets in its refusal (edl/clip_animations.preset_id).
        "in": {"type": "string", "enum": [*_anim_ids("in"), "none"], "x-validated-by-handler": True,
               "description": "An In preset id, or 'none'"},
        "out": {"type": "string", "enum": [*_anim_ids("out"), "none"], "x-validated-by-handler": True,
                "description": "An Out preset id, or 'none'"},
        "combo": {"type": "string", "enum": [*_anim_ids("combo"), "none"], "x-validated-by-handler": True,
                  "description": "A Combo preset id, or 'none'"},
        "in_duration": {"type": "number", "description": "Seconds the In lasts (0.1-3)"},
        "out_duration": {"type": "number", "description": "Seconds the Out lasts (0.1-3)"}}),
    _t("freeze_frame",
       "Freeze frame (CapCut's Freeze): hold the frame at `time` (the playhead, "
       "timeline seconds) for `duration` seconds (default 3) on the main video "
       "track. The clip there is split and a still of that exact frame is "
       "inserted; later clips, overlays and transitions move right by the hold. "
       "Give `clip_id` to require a particular clip (it must be under `time`; "
       "without `time`, its first frame is held). An overlay (picture-in-picture) "
       "clip freezes too: give its `clip_id` (or `track`, e.g. 'v2'); then only that "
       "overlay lane's later clips move right. Freezing a freeze holds it longer. "
       "The still is silent.",
       "edit",
       {"time": {"type": "number", "description": "Timeline seconds of the frame to hold"},
        "clip_id": {"type": "string"},
        "track": {"type": "string",
                  "description": "Overlay lane to freeze on (default: the clip's lane, else v1)"},
        "duration": {"type": "number", "description": "Seconds to hold (default 3)"}},
       []),
    _t("set_clip_fit",
       "Choose how a clip reconciles its aspect ratio with the canvas. 'cover' scales "
       "the source UP and crops the overflow so it FILLS the frame with no black bars — "
       "this is the crop/fill mode, and it is what to use when a landscape clip is on a "
       "vertical canvas (or vice versa) and the user does not want letterboxing. "
       "'contain' (default) scales down and pads black. Pair 'cover' with "
       "set_clip_transform scale/x/y to choose WHICH part of the frame is visible.",
       "edit",
       {
           "clip_id": {"type": "string"},
           "fit": {"type": "string", "enum": ["contain", "cover"],
                   "description": "cover = fill + crop; contain = letterbox"},
           "mode": {"type": "string", "enum": ["contain", "cover"],
                    "description": "Alias of fit; prefer fit. Read only when fit is "
                                   "omitted (default 'cover' when both are absent)."},
       },
       ["clip_id", "fit"]),
    # Wave E, lane F2 — the ONE table is edl/canvas_blend.py.
    _t("set_canvas_background",
       "CapCut Canvas: fill the black bars of a letterboxed main-track clip. type 'blur' = a "
       f"blurred copy of the clip itself behind it (blur 1-{_canvas_blur_max()}, light to heavy); 'color' = a solid "
       "colour (#RRGGBB or a name like black/white); 'image' = a picture from the project "
       "(`image`, its path) scaled to cover the canvas; 'none' = back to black bars. Only "
       "clips that are letterboxed (fit 'contain') show it. all=true applies it to EVERY "
       "main-track clip in one undo step; all=true with a clip_id and no type copies that "
       "clip's background to all clips.",
       "edit",
       {
           "clip_id": {"type": "string", "description": "A main-track (v1) clip."},
           "type": {"type": "string", "enum": _canvas_kinds(),
                    "x-validated-by-handler": True},
           "color": {"type": "string", "description": "#RRGGBB or a colour name (type color)."},
           "blur": {"type": "integer",
                    "description": f"Blur strength 1-{_canvas_blur_max()} (type blur; default 2)."},
           "image": {"type": "string", "description": "Path of a picture file (type image)."},
           "all": {"type": "boolean", "description": "Apply to every main-track clip."},
       },
       []),
    _t("set_blend_mode",
       "CapCut blend mode of an OVERLAY (picture-in-picture, v2+) clip onto the video beneath "
       f"it: {_blend_list()}. Screen/add "
       "drop a dark background (light leaks, fire, flares); multiply drops a white one "
       "(paper, ink). The main track is the base layer and takes no blend mode.",
       "edit",
       {
           "clip_id": {"type": "string", "description": "An overlay (v2+) video clip."},
           "clip_ids": {"type": "array", "items": {"type": "string"}},
           "mode": {"type": "string", "enum": _blend_ids(), "x-validated-by-handler": True},
       },
       ["mode"]),
    _t("set_clip_transform",
       "Set transform properties on any clip (media, sticker, or text): x/y position, "
       "scale, rotation (degrees), opacity — this is THE tool for positioning things on "
       "the canvas. For text and stickers, x/y are ABSOLUTE CANVAS PIXELS (e.g. 540,960 "
       "= center of a 1080×1920 canvas). Without `time`, setting a value REPLACES any "
       "keyframes on that property with the scalar — that is how you flatten an "
       "animation back to a constant. Pass `time` to edit an animated property "
       "without destroying it.",
       "edit",
       {
           "clip_id": {"type": "string"},
           "x": {"type": "number", "description": "Canvas px (absolute for text/stickers)"},
           "y": {"type": "number", "description": "Canvas px (absolute for text/stickers)"},
           "scale": {"type": "number", "description": "1.0 = original size"},
           "rotation": {"type": "number", "description": "Degrees"},
           "opacity": {"type": "number", "description": "0..1"},
           "time": {
               "type": "number",
               "description": "Clip-local seconds. For any property that is ALREADY "
                              "keyframed, write the value as a keyframe here instead of "
                              "overwriting the animation with a scalar. Properties with "
                              "no keyframes are set normally, so it is always safe to "
                              "pass the playhead position.",
           },
           "raise_to_front": {
               "type": "boolean",
               "description": "Stickers only: also stack this sticker above every "
                              "sibling on its track, in the same undo step as the "
                              "move. Use when a repositioned sticker would land "
                              "underneath another one.",
           },
       },
       ["clip_id"]),
    _t("set_clip_timing",
       "Retime an OVERLAY clip (text/sticker/caption) by setting its timeline start "
       "and/or end in seconds — media clips must use trim_clip/move_clip instead. "
       "Enforces end > start (clamps to start+0.1s) and re-sorts the track by start.",
       "edit",
       {
           "clip_id": {"type": "string"},
           "start": {"type": "number", "description": "Timeline seconds"},
           "end": {"type": "number", "description": "Timeline seconds; must be > start"},
       },
       ["clip_id"]),
    _t("set_clip_z",
       "Change how overlapping STICKER overlays stack: set a sticker's per-clip "
       "z-order within its track. Higher z composites on top; ties keep the "
       "legacy order (later start wins). Pass an int, or 'front' (above every "
       "sibling sticker) / 'back' (below every sibling sticker).",
       "edit",
       {
           "clip_id": {"type": "string", "description": "Sticker clip id (st_…)"},
           "z": {"description": "int, or 'front' / 'back'",
                 "anyOf": [{"type": "integer"}, {"type": "string", "enum": ["front", "back"]}]},
       },
       ["clip_id", "z"]),
    _t("set_property",
       "LOW-LEVEL escape hatch: set any field on a clip by dotted path (e.g. "
       "transform.x, audio.gain_db, audio.fade_in, speed, reverse, in, out, start). "
       "Prefer the specific tool when one exists (set_speed, set_volume, "
       "set_clip_transform, trim_clip…) — this does no validation of the value.",
       "edit",
       {
           "clip_id": {"type": "string"},
           "path": {"type": "string", "description": "Dotted attribute path, e.g. 'transform.x'"},
           "value": {"description": "New value; type must match the field"},
       },
       ["clip_id", "path", "value"]),
    _t("bulk_delete",
       "Ripple-delete several clips in one operation (one undo step instead of N). "
       "Gaps are closed on every affected track; ids that don't exist are skipped.",
       "edit",
       {"clip_ids": {"type": "array", "items": {"type": "string"}}},
       ["clip_ids"]),
    _t("bulk_duplicate",
       "Duplicate several MEDIA clips in one operation; each copy is placed right "
       "after its original. Text/sticker overlay ids are silently skipped — use "
       "duplicate_clip semantics only for media.",
       "edit",
       {"clip_ids": {"type": "array", "items": {"type": "string"}}},
       ["clip_ids"]),
    _t("add_keyframe",
       "Add or update a keyframe on a clip's transform property to animate it over "
       "time (time is CLIP-LOCAL seconds, 0 = clip start; a keyframe within 1ms of an "
       "existing one replaces it). Exported renders interpolate LINEAR only — ease/"
       "bounce modes animate in the browser preview but bake as linear. "
       "prop 'audio.gain_db' keys the clip's VOLUME (value = level in dB at that "
       "time): volume automation, rendered on every lane.",
       "edit",
       {
           "clip_id": {"type": "string"},
           "prop": {"type": "string", "enum": ["x", "y", "scale", "rotation", "opacity", "audio.gain_db"]},
           "props": {"type": "array", "items": {"type": "string"},
                     "description": "Key several properties in ONE commit instead of "
                                    "'prop'. A property left out of 'values' keeps "
                                    "whatever it already reads as at 'time'."},
           "time": {"type": "number", "description": "Clip-local seconds (0 = clip start)"},
           "value": {"type": "number"},
           "values": {"type": "object",
                      "description": "{prop: value} — the 'props' form's per-property values"},
           "interp": {"type": "string",
                      "enum": ["linear", "ease-in", "ease-out", "ease-in-out",
                               "step", "back-out", "bounce"],
                      "default": "linear"},
       },
       ["clip_id", "prop", "time", "value"]),
    _t("remove_keyframe",
       "Remove the keyframe at a given clip-local time from a clip's transform "
       "property. When 1 key remains the property collapses to that scalar; when 0 "
       "remain it resets to 0.0.",
       "edit",
       {
           "clip_id": {"type": "string"},
           "prop": {"type": "string", "enum": ["x", "y", "scale", "rotation", "opacity", "audio.gain_db"]},
           "props": {"type": "array", "items": {"type": "string"},
                     "description": "Remove from several properties in ONE commit "
                                    "instead of 'prop'. Properties with no key at "
                                    "'time' are skipped; if none has one, nothing "
                                    "is committed."},
           "time": {"type": "number", "description": "Clip-local seconds of the key to remove"},
       },
       ["clip_id", "prop", "time"]),
]

# --- Project / canvas ---

PROJECT_TOOLS = [
    _t("set_canvas", "Set output canvas size and fps.", "project",
       {"w": {"type": "integer"}, "h": {"type": "integer"},
        "fps": {"type": "number", "description": "Project frame rate, e.g. 23.976, 24, 25, 29.97, 30, 50, 59.94, 60."}}),
    _t("set_aspect_ratio", "Switch canvas to a named aspect ratio.", "project",
       {"ratio": {"type": "string", "enum": ["9:16", "16:9", "1:1", "4:5"]}}, ["ratio"]),
    _t("undo", "Undo the last operation.", "project", {}),
    _t("redo", "Redo the last undone operation.", "project", {}),
    _t("render_preview", "Render a preview of the current EDL. No-op if cached.", "project", {}),
    _t("set_track_muted",
       "Mute or unmute a track (e.g. 'music', 'vo', 'tx_super'). Muted tracks are skipped at render time.",
       "project",
       {"track": {"type": "string"}, "muted": {"type": "boolean", "default": True}},
       ["track"]),
    _t("set_track_solo",
       "Solo or unsolo a track that carries sound (v1/v2, a1, music, vo). While any "
       "track is soloed only soloed tracks are heard, in the preview and the export; "
       "pictures are unaffected. Omit `solo` to toggle.",
       "project",
       {"track": {"type": "string"},
        "solo": {"type": "boolean", "description": "Omit to toggle"}},
       ["track"]),
    _t("set_track_locked",
       "Lock or unlock a track (a UI flag that prevents accidental edits in the "
       "timeline panel; it does not block tool calls). Omit `locked` to toggle the "
       "current state.",
       "project",
       {"track": {"type": "string"},
        "locked": {"type": "boolean", "description": "Omit to toggle"}},
       ["track"]),
    _t("add_marker",
       "Drop a marker on the timeline ruler at a time, with an optional label and "
       "color — useful for flagging moments to revisit. Returns the marker_id needed "
       "by remove_marker.",
       "project",
       {
           "time": {"type": "number", "description": "Timeline seconds"},
           "label": {"type": "string"},
           "color": {"type": "string", "description": "#RRGGBB, default amber #fbbf24"},
       },
       ["time"]),
    _t("remove_marker",
       "Remove a timeline marker by its id (as returned by add_marker or listed in "
       "the EDL's markers). Errors if the id doesn't exist.",
       "project",
       {"marker_id": {"type": "string"}},
       ["marker_id"]),
    _t("apply_export_preset",
       "One-call platform setup: sets canvas size, bitrate and loudness target (and "
       "fps, only when the project rate is outside the 23.976-60 fps platforms accept) "
       "from a named preset (reels/tiktok/story 1080×1920 -16 LUFS, shorts 1080×1920 "
       "-14, ig_feed_1x1 1080×1080, ig_feed_4x5 1080×1350, youtube_16x9 1920×1080 -14, "
       "youtube_4k 3840×2160 -14). Use before export when the user names a platform.",
       "project",
       {"name": {"type": "string",
                 "enum": ["reels", "shorts", "tiktok", "story", "ig_feed_1x1",
                          "ig_feed_4x5", "youtube_16x9", "youtube_4k"]}},
       ["name"]),
]

# --- text / captions / brand kit / audit (M2) ---

TEXT_TOOLS = [
    _t("add_super_text",
       "Add a bold on-screen text overlay (the 'super' look). Use role='hook' for a "
       "first-3-seconds curiosity hook, role='super' for mid-video punctuation, "
       "role='lower_third' for guest name/handle. By default, any prior overlay "
       "on this track with the same role whose time window overlaps this one's "
       "is replaced (so re-running with a new caption at the same moment updates "
       "it instead of stacking a second one on top). Pass allow_stack=true to "
       "instead keep both overlapping overlays.",
       "text",
       {
           "text": {"type": "string"},
           "start": {"type": "number"},
           "end": {"type": "number"},
           "role": {"type": "string", "enum": ["super", "hook", "lower_third", "label"], "default": "super"},
           "upper": {"type": "boolean", "default": False,
                     "description": "ALL CAPS. Defaults to false, i.e. the text renders "
                                    "exactly as written — pass true for the all-caps "
                                    "house-style look of a hook or super."},
           "allow_stack": {"type": "boolean", "default": False,
                            "description": "Keep prior same-role overlapping overlays instead of replacing them."},
       },
       ["text", "start", "end"]),
    _t("add_hook_overlay",
       "Convenience wrapper: add a hook overlay at 0..duration (default 3s).",
       "text",
       {"text": {"type": "string"}, "duration": {"type": "number", "default": 3.0}},
       ["text"]),
    _t("add_caption_track",
       "Burn the project transcript as a caption track. Uses the existing transcript "
       "from ingest. Style 'default' is single-line, 'ig_chunky' is the heavy white IG/Reels look.",
       "text",
       {
           "style": {"type": "string", "enum": ["default", "ig_chunky", "word_emphasis"], "default": "default"},
           "position": {"type": "string", "enum": ["bottom", "center", "top"], "default": "bottom"},
           "chunk_size": {"type": "integer", "default": 2, "minimum": 1,
                          "description": "style='word_emphasis' only: words per karaoke "
                                         "chunk (1-3 reads well; default 2). Ignored by "
                                         "the other styles."},
           "rebuild": {"type": "boolean", "default": False,
                       "description": "Re-lay every cue from the transcript even when "
                                      "cues were edited by hand. Without it, a style "
                                      "change on hand-edited captions restyles them in "
                                      "place and keeps the edits."},
       }),
    _t("set_caption_style",
       "Change how the captions LOOK and where they sit, for every cue at once, in one "
       "undo step, without re-laying them (hand edits are kept). Keys you pass are set; "
       "null clears one back to the caption default. Later caption builds keep this look.",
       "text",
       {
           "position": {"type": "string", "enum": ["bottom", "center", "top"]},
           "font": {"type": ["string", "null"], "description": "Bundled font file stem, e.g. Anton-Regular, Inter-Black"},
           "color": {"type": ["string", "null"], "description": "#RRGGBB or #RRGGBBAA text fill"},
           "size": {"type": ["number", "null"], "description": "Font size in canvas px"},
           "stroke": {"type": ["string", "null"], "description": "#RRGGBB outline colour"},
           "stroke_w": {"type": ["number", "null"], "description": "Outline width in canvas px"},
           "background": {"type": ["string", "null"], "description": "#RRGGBB[AA] box behind each cue; null = none"},
           "shadow_on": {"type": ["boolean", "null"]},
           "upper": {"type": ["boolean", "null"], "description": "ALL CAPS"},
       }),
    _t("auto_caption",
       "BEST-QUALITY auto captions for Hindi + English + Spanish (and Hinglish). "
       "Re-transcribes the video with the large-v3 Whisper model on Metal (far better "
       "than the fast upload model — clean Hindi, no hallucination loops), then formats "
       "the words into broadcast-grade cues (≤2 lines, reading-speed limited) and lays "
       "down a caption track. This is the tool to use when the user asks for accurate "
       "captions. `target` picks the language the CAPTIONS come out in and works even "
       "when the footage is in a third language (e.g. Chinese video → Hindi subtitles, "
       "or Japanese video → Spanish subtitles): 'en' uses Whisper's own translation, "
       "'hi'/'es' translate locally via Argos (pivoting through English when there is no "
       "direct package for the spoken language), 'hinglish' is Hindi romanised into Latin "
       "script. `language` is the separate, input-side hint for what is SPOKEN — leave it "
       "off to auto-detect.",
       "text",
       {
           "style": {"type": "string", "enum": ["default", "ig_chunky", "word_emphasis"], "default": "ig_chunky"},
           "position": {"type": "string", "enum": ["bottom", "center", "top"], "default": "bottom"},
           # x-validated-by-handler on `target` AND its two aliases below: the
           # handler normalises 'hindi'/'roman'/'english'/'spanish' itself and
           # raises a clear ValueError against CAPTION_TARGETS, so the generic
           # boundary enum check must stand down on all three spellings alike.
           # Before this flag, target='hindi' was a 400 while target_lang='hindi'
           # reached the normaliser — the alias was MORE permissive than the
           # canonical name it claimed to alias.
           "target": {"type": "string", "enum": ["hi", "en", "hinglish", "es"],
                      "x-validated-by-handler": True,
                      "description": "Language of the CAPTIONS: 'hi' Devanagari, 'en' English, "
                                     "'hinglish' romanised Hindi, 'es' Spanish. Omit to caption "
                                     "in whatever was spoken."},
           "language": {"type": "string", "description": "Force the SPOKEN language ('hi', 'zh', …); omit to auto-detect (Hinglish-friendly)"},
           "model": {"type": "string",
                     "description": "Override Whisper model (default large-v3, most accurate). "
                                     "'large-v3-turbo' is ~4x faster on Hindi/English/Hinglish "
                                     "transcription — offer it when the user asks for speed. It "
                                     "cannot translate, so requesting it with target='en' silently "
                                     "runs large-v3 instead; the returned `model` field says which "
                                     "one actually ran."},
           "max_chars": {"type": "integer", "default": 42},
           "max_cps": {"type": "number", "default": 17.0},
           # The two alias spellings the UI/MCP reach for. Enum-advertised and
           # handler-validated exactly like `target` (see the note above it).
           "target_lang": {"type": "string", "enum": ["hi", "en", "hinglish", "es"],
                           "x-validated-by-handler": True,
                           "description": "Alias of target; prefer target. Read only "
                                          "when target is omitted."},
           "caption_lang": {"type": "string", "enum": ["hi", "en", "hinglish", "es"],
                            "x-validated-by-handler": True,
                            "description": "Alias of target; prefer target. Read only "
                                           "when target and target_lang are omitted."},
           "chunk_size": {"type": "integer", "default": 2, "minimum": 1,
                          "description": "style='word_emphasis' only: words per karaoke "
                                         "chunk (1-3 reads well; default 2). Ignored by "
                                         "the other styles."},
       }),
    _t("apply_brand_kit",
       "Set the project's brand kit and auto-apply persistent watermark + end-card.",
       "brand",
       {
           "handle": {"type": "string"},
           "hashtags": {"type": "array", "items": {"type": "string"}},
           "end_card": {"type": "string"},
           "palette": {"type": "array", "items": {"type": "string"}},
           "font": {"type": "string"},
       }),
    _t("audit_aesthetic",
       "Run the house-style quality check. Returns a list of issues + a 0–100 score. "
       "Call this before declaring done.",
       "quality", {}),
    _t("add_text",
       "Full-control text overlay (vs add_super_text's canonical defaults): role, "
       "position, per-clip color/font, and entrance/exit animation presets.",
       "text",
       {
           "text": {"type": "string"},
           "start": {"type": "number"},
           "end": {"type": "number"},
           "role": {"type": "string",
                    "enum": ["super", "hook", "lower_third", "caption", "label", "watermark", "default"]},
           "x": {"type": "number"}, "y": {"type": "number"},
           "color": {"type": "string", "description": "#RRGGBB text fill override"},
           "font": {"type": "string", "description": "Bundled font file, e.g. BebasNeue-Regular. "
                                                    "NOTE: BebasNeue has no lowercase letterforms — "
                                                    "its lowercase slots are capitals, so text in it "
                                                    "renders all-caps whatever `upper` says."},
           "upper": {"type": "boolean", "default": False,
                     "description": "ALL CAPS. Defaults to false, i.e. the text renders exactly "
                                    "as written."},
           "anim_in": {"type": "string", "enum": ["pop", "fade", "slide_up", "slide_down"]},
           "anim_out": {"type": "string", "enum": ["pop", "fade", "slide_up", "slide_down"]},
           "scale": {"type": "number", "default": 1.0,
                     "description": "Transform scale multiplier (1.0 = as styled)"},
           "rotation": {"type": "number", "default": 0.0, "description": "Degrees, clockwise"},
           "opacity": {"type": "number", "default": 1.0, "description": "0 transparent .. 1 opaque"},
           "size": {"type": "number", "default": 96,
                    "description": "Font size in canvas px (TextStyle.size)"},
           "stroke": {"type": "string", "default": "#000000",
                      "description": "#RRGGBB outline colour (TextStyle.stroke)"},
           "stroke_w": {"type": "number", "default": 4,
                        "description": "Outline width in canvas px; 0 = no outline"},
           "background": {"type": "string", "description": "#RRGGBB[AA] box behind the text"},
           "align": {"type": "string", "enum": ["left", "center", "right"],
                     "description": "Line alignment inside the block"},
           "line_spacing": {"type": "number", "description": "Line height multiplier (1 = normal)"},
           "letter_spacing": {"type": "number",
                              "description": "Extra canvas px between letters (-20..100; 0 = normal). "
                                             "Hindi/Arabic runs are never spaced"},
           "shadow_on": {"type": "boolean", "description": "Drop shadow on/off (omit = the role's)"},
           "anim_dur": {"type": "number", "description": "Seconds each in/out animation lasts (default 0.35)"},
           "allow_stack": {"type": "boolean", "default": False,
                           "description": "By default a new clip REPLACES existing "
                                          "text clips of the same role whose time "
                                          "window overlaps. Pass true to keep both."},
       },
       ["text", "start", "end"]),
    _t("set_text",
       "Change what an existing text overlay (title, lower third, label) SAYS. Its style, "
       "position, timing and animation stay as they are.",
       "text",
       {
           "clip_id": {"type": "string", "description": "The text clip's id"},
           "text": {"type": "string", "description": "The new wording"},
       },
       ["clip_id", "text"]),
    _t("apply_text_template",
       "Render a text overlay from a named preset bundle. Options: hashtag_chunky, "
       "callout_arrow, big_question, end_card_handle, countdown_3_2_1, watermark_handle.",
       "text",
       {
           "name": {"type": "string",
                    "enum": ["hashtag_chunky", "callout_arrow", "big_question",
                             "end_card_handle", "countdown_3_2_1", "watermark_handle"]},
           "fields": {"type": "object",
                      "description": "Slot values: {text}, {handle}, {hashtag}"},
           "start": {"type": "number", "default": 0.0},
           "end": {"type": "number"},
       },
       ["name"]),
    _t("list_text_styles",
       "Text roles the renderer styles (super/hook/caption/…) + saved text presets.",
       "text", {}),
    _t("add_sticker",
       "Add a sticker overlay: an emoji character (fetched as Apple/iOS artwork) or a "
       "PNG file path, at a canvas position for a time window.",
       "text",
       {
           "emoji": {"type": "string", "description": "Emoji character, e.g. 🔥"},
           "src": {"type": "string", "description": "PNG path (alternative to emoji)"},
           "start": {"type": "number", "default": 0.0},
           "end": {"type": "number"},
           # Two shapes on purpose: captions have always taken a named position,
           # so a named anchor here keeps the tool surface consistent instead of
           # 400-ing on the obvious guess (QA round 5, VAI-04).
           "position": {"type": ["array", "string"], "items": {"type": "number"},
                        "description": "[x, y] canvas px, or a named anchor: "
                                       "center, top, bottom, left, right, "
                                       "top-left, top-right, bottom-left, bottom-right"},
           "scale": {"type": "number", "default": 1.0},
           "rotation": {"type": "number", "default": 0.0, "description": "Degrees"},
       }),
    _t("import_srt",
       "REPLACE the project transcript with one parsed from an external .srt/.vtt/.ass "
       "subtitle file — use when the user has a pre-edited or translated subtitle file. "
       "Follow with add_caption_track to burn the imported captions in.",
       "text",
       {
           "path": {"type": "string", "description": "Path to the .srt/.vtt/.ass file"},
           "language": {"type": "string", "default": "en"},
       },
       ["path"]),
    _t("export_srt",
       "Write the current transcript out as a .srt subtitle file (read-only; no "
       "timeline change). Default destination is <session>/captions.srt; returns the "
       "written path.",
       "text",
       {"path": {"type": "string", "description": "Destination file; omit for <session>/captions.srt"}}),
    _t("export_vtt",
       "Write the current transcript out as a WebVTT .vtt file (read-only; no "
       "timeline change). Default destination is <session>/captions.vtt.",
       "text",
       {"path": {"type": "string", "description": "Destination file; omit for <session>/captions.vtt"}}),
    _t("export_ass",
       "Write the current transcript out as an Advanced SubStation .ass file "
       "(read-only; no timeline change). Default destination is <session>/captions.ass.",
       "text",
       {"path": {"type": "string", "description": "Destination file; omit for <session>/captions.ass"}}),
    _t("translate_captions",
       "Translate the EXISTING captions track in place to a target language using "
       "local Argos Translate (no cloud) — run add_caption_track/auto_caption first if "
       "there are no captions yet. Source language defaults to the transcript's "
       "detected language.",
       "text",
       {
           "target_lang": {"type": "string", "default": "hi",
                           "description": "ISO code, e.g. 'hi', 'es', 'fr', 'en'"},
           "source_lang": {"type": "string",
                           "description": "Omit to use the transcript's detected language"},
           "to": {"type": "string",
                  "description": "Alias of target_lang; prefer target_lang. Read only "
                                 "when target_lang is omitted."},
       },
       ["target_lang"]),
]

AUDIO_TOOLS = [
    _t("add_music",
       "Add a background music clip on the music track. By default, ducks under speech "
       "(sidechain compressor against the V1 audio).",
       "audio",
       {
           "src": {"type": "string", "description": "Path to music file (mp3/wav/m4a). Use the path returned by /audio_upload."},
           "start": {"type": "number", "default": 0.0},
           "in": {"type": "number", "default": 0.0},
           "out": {"type": "number", "default": 0.0, "description": "0 = use full source duration"},
           "volume_db": {"type": "number", "default": -12.0},
           "duck": {"type": "boolean", "default": True},
           "loop": {"type": "boolean", "default": False,
                    "description": "Repeat a bed shorter than the video back to back until "
                                   "the video extent is covered (laid as consecutive clips; "
                                   "only the last one fades out)."},
       },
       ["src"]),
    _t("set_duck",
       "Turn sidechain ducking (music auto-lowers under speech) on or off for a track "
       "(default 'music'), without touching any clip's trim/position — use this instead "
       "of re-adding the music clip just to flip ducking.",
       "audio",
       {
           "track": {"type": "string", "default": "music"},
           "enabled": {"type": "boolean", "description": "Omit to toggle the current state"},
           "to_db": {"type": "number", "default": -18.0, "description": "How much to attenuate music under speech, dB"},
           "track_ref": {"type": "string", "default": "a1", "description": "Sidechain key track id (the speech track)"},
       }),
    _t("fit_music_to_video",
       "Trim the music bed so it ends with the video (drops loop pieces past the end, "
       "trims the one that straddles it) and set the bed's fade-in / fade-out. Omitted "
       "fades keep their value; a trimmed tail always gets a fade-out.",
       "audio",
       {"fade_in": {"type": "number", "description": "Seconds of fade-in on the first music piece"},
        "fade_out": {"type": "number", "description": "Seconds of fade-out on the last music piece"}}),
    _t("set_volume",
       "Set audio gain (dB) on a track id (e.g. 'a1', 'music', 'vo') or a clip id ('c_xxx').",
       "audio",
       {"target": {"type": "string"}, "db": {"type": "number"}},
       ["target", "db"]),
    _t("set_clip_muted",
       "Mute or unmute ONE clip's audio (clip-level, vs set_track_muted which "
       "silences a whole track). Preserves the clip's gain_db, so a volume trim "
       "survives a mute/unmute cycle. Omit `muted` to toggle.",
       "audio",
       {"clip_id": {"type": "string"},
        "muted": {"type": "boolean", "description": "Omit to toggle"}},
       ["clip_id"]),
    _t("set_clip_reverse",
       "Play ONE media clip backwards (picture and sound), or forwards again with "
       "reverse=false. Omit `reverse` to toggle. Does not change the clip's length.",
       "edit",
       {"clip_id": {"type": "string"},
        "reverse": {"type": "boolean", "description": "Omit to toggle"}},
       ["clip_id"]),
    _t("detach_audio",
       "Detach a video clip's sound onto an audio lane so picture and sound trim and "
       "move independently (J and L cuts). The video clip is muted; the audio clip "
       "keeps its source, trim, position, gain, fades and volume keyframes. `track` "
       "picks the audio lane (default: the first with room, else a new one). 1x clips only.",
       "audio",
       {"clip_id": {"type": "string"},
        "track": {"type": "string", "description": "Audio lane id, e.g. 'a1'"}},
       ["clip_id"]),
    _t("add_fade",
       "Add audio fade in / fade out to a clip.",
       "audio",
       {"clip_id": {"type": "string"}, "in_s": {"type": "number"}, "out_s": {"type": "number"}},
       ["clip_id"]),
    _t("set_video_fade",
       "Visual fade-from-black / fade-to-black on a clip's VIDEO (v1 media clips only; "
       "distinct from add_fade, which fades audio). Seconds are clip-local source time; "
       "an omitted side keeps its current value, negatives clamp to 0.",
       "edit",
       {"clip_id": {"type": "string"},
        "in_s": {"type": "number", "description": "Fade-from-black duration, seconds"},
        "out_s": {"type": "number", "description": "Fade-to-black duration, seconds"}},
       ["clip_id"]),
    _t("remove_silences",
       "Detect silences in a track and ripple-cut them out. Default thresholds work "
       "for normal talking-head speech.",
       "auto",
       {
           "track": {"type": "string", "default": "v1"},
           "threshold_db": {"type": "number", "default": -30},
           "min_dur": {"type": "number", "default": 0.5, "description": "Minimum silence duration to cut, seconds"},
           "keep_pad": {"type": "number", "default": 0.1, "description": "Seconds of silence to leave at each edge for breathing room"},
       }),
    _t("remove_fillers",
       "Find filler-word ranges in the transcript and ripple-cut them out (default: um, uh, "
       "umm, uhh, erm, hmm; pass `words` to add others such as 'like').",
       "auto",
       {
           "words": {"type": "array", "items": {"type": "string"}},
           "pad": {"type": "number", "default": 0.05},
           "track": {"type": "string", "default": "v1"},
       }),
    _t("auto_cut_to_beats",
       "Detect beats in the music track and split V1 every Nth beat (so cuts land "
       "on the music). Requires add_music first.",
       "auto",
       {"subdivision": {"type": "integer", "default": 4, "description": "Cut every Nth beat (4 = every bar in 4/4)"},
        "min_shot": {"type": "number", "default": 0.0, "minimum": 0,
                     "description": "Shortest shot to leave, seconds. A beat that would cut a "
                                    "shorter fragment (off a clip edge or the previous cut) is "
                                    "SKIPPED, not shifted, so kept cuts stay on the beat. "
                                    "0 = cut on every Nth beat regardless."}}),
    _t("auto_reframe",
       "Switch canvas aspect (9:16 / 16:9 / 1:1 / 4:5) and re-crop every V1 clip to "
       "it. With subject_track (default) each clip is re-rendered following the "
       "detected subject (OpenCV face detection, local, no model download); "
       "subject_track=false is a plain centre-crop with no detection at all.",
       "auto",
       {"ratio": {"type": "string", "enum": ["9:16", "16:9", "1:1", "4:5"]},
        "aspect": {"type": "string", "enum": ["9:16", "16:9", "1:1", "4:5"],
                   "description": "Alias of ratio; prefer ratio. Read only when ratio "
                                  "is omitted (default '9:16' when both are absent)."},
        "subject_track": {"type": "boolean", "default": True,
                          "description": "Follow the detected subject when cropping "
                                         "(needs the tracking feature, i.e. OpenCV); "
                                         "false = plain centre-crop, fastest."}},
       ["ratio"]),
    _t("noise_reduce",
       "Spectrally denoise a media clip's audio (hiss, fans, room tone) and replace "
       "the clip's source with the cleaned file (video stream copied untouched). "
       "Needs the local noisereduce package installed.",
       "audio",
       {
           "clip_id": {"type": "string"},
           "strength": {"type": "number", "default": 0.85, "description": "0..1 reduction amount"},
       },
       ["clip_id"]),
    _t("set_loudness_target",
       "Set the export loudness-normalisation target in LUFS (Reels/TikTok -16, "
       "YouTube -14, broadcast -23; default -16). Pass lufs=null to disable the "
       "loudnorm pass entirely; this only affects export, not preview.",
       "audio",
       {"lufs": {"type": ["number", "null"], "default": -16.0}}),
]


SHOW_TOOLS = [
    _t("apply_template",
       "Apply a built-in show template (outfit_breakdown, tech_tip, explainer). "
       "Each lays down a hook + caption style + relevant text labels appropriate "
       "for that style; you can refine afterwards.",
       "show",
       {
           "name": {"type": "string", "enum": ["outfit_breakdown", "tech_tip", "explainer"]},
           "inputs": {"type": "object", "description": "Template-specific inputs e.g. {hook: 'BUY NOW'} or {guest: 'Mrunal Thakur'}"},
           "with_hook_stack": {"type": "boolean", "default": True,
                               "description": "Also apply the hook stack (visual + "
                                              "text + audio hook) after the template, "
                                              "seeded from inputs.hook/inputs.text. "
                                              "Pass false to compose the hook yourself."},
       },
       ["name"]),
    _t("list_templates",
       "List built-in templates and the user's saved show templates.",
       "show", {}),
    _t("save_show_template",
       "Save the current project's brand kit + canvas + caption style + music seed "
       "as a reusable named show. Re-apply it next week with apply_show_template.",
       "show",
       {"name": {"type": "string"}},
       ["name"]),
    _t("apply_show_template",
       "Apply a previously-saved show template to the current project (drops in "
       "brand kit, canvas, captions, music).",
       "show",
       {"name": {"type": "string"}},
       ["name"]),
    _t("add_lower_third",
       "Drop a guest name + handle lower-third graphic. Speaker is informational "
       "until diarization lands in M5.",
       "show",
       {
           "name": {"type": "string"},
           "handle": {"type": "string"},
           "start": {"type": "number"},
           "end": {"type": "number"},
           "speaker": {"type": "string"},
       },
       ["name", "start"]),
    _t("generate_hook",
       "Ask Claude to draft 3 candidate hook lines from the project transcript. "
       "Pure suggestion — caller picks one and calls add_hook_overlay separately.",
       "show", {}),
]


EFFECT_TOOLS = [
    _t("add_effect",
       "Append a per-clip video effect. Call list_filters for the current type "
       "catalog (color, lut, blur, sharpen, vignette, grain, vintage, vhs, glow, "
       "hflip, vflip, rgb_split, …).",
       "effects",
       {
           "clip_id": {"type": "string"},
           "type": {"type": "string", "enum": _effect_names()},
           "params": {"type": "object", "description": "type-specific params"},
       },
       ["clip_id", "type"]),
    _t("remove_effect",
       "Remove the Nth effect from a clip's effect chain.",
       "effects",
       {"clip_id": {"type": "string"},
        "index": {"type": "integer", "description": "0-based position in the clip's effect chain"},
        "idx": {"type": "integer",
                "description": "Alias of index; prefer index. Read only when index is "
                               "omitted (default 0 when both are absent)."}},
       ["clip_id", "index"]),
    _t("remove_effects",
       "Take every effect of the given types off the given media clips in ONE step "
       "(one undo): e.g. types=['lut'] removes the filter/look, ['lut','color'] the "
       "whole colour grade. Use this rather than remove_effect by index to take a "
       "filter off; it fails when none of the clips carries such an effect.",
       "effects",
       {"clip_ids": {"type": "array", "items": {"type": "string"},
                     "description": "Media clip ids (main track or overlays)"},
        "types": {"type": "array", "items": {"type": "string", "enum": _effect_names()},
                  "description": "Effect types to remove; default ['lut'] (the filter)"}},
       ["clip_ids"]),
    _t("flip_clip",
       "Mirror a media clip or sticker: axis 'horizontal' (CapCut's Mirror, a "
       "left-right mirror image) or 'vertical' (top-bottom). `value` true/false "
       "sets it; omit it to toggle. Upside down is a 180° rotation "
       "(set_clip_transform rotation=180), not a flip.",
       "edit",
       {"clip_id": {"type": "string"},
        "axis": {"type": "string", "enum": ["horizontal", "vertical"]},
        "value": {"type": "boolean", "description": "Omit to toggle"}},
       ["clip_id", "axis"]),
    _t("color_grade",
       "Convenience: add a color effect with brightness/contrast/saturation/temp/tint. "
       "Applies to one clip or all V1 clips if clip_id omitted.",
       "effects",
       {
           "clip_id": {"type": "string"},
           "brightness": {"type": "number", "description": "-1..1, 0=neutral"},
           "contrast": {"type": "number", "description": "0..2, 1=neutral"},
           "saturation": {"type": "number", "description": "0..3, 1=neutral"},
           "sat": {"type": "number",
                   "description": "Legacy alias of saturation; prefer saturation and "
                                  "never send both. Once both are stored on the clip "
                                  "the export uses sat while the preview uses "
                                  "saturation, so they drift apart."},
           "gamma": {"type": "number", "description": "0.1..10, 1=neutral"},
           "temp": {"type": "number", "description": "-1 cool .. +1 warm"},
           "tint": {"type": "number", "description": "-1 magenta .. +1 green"},
       }),
    _t("apply_lut",
       "Apply a 3D LUT (.cube) to a clip or all V1 clips.",
       "effects",
       {"clip_id": {"type": "string", "description": "Omit to apply to every V1 clip"},
        "src": {"type": "string",
                "description": "Bundled LUT name from list_luts (e.g. 'warm.cube') or a "
                               "path to an existing .cube file"},
        "lut_path": {"type": "string",
                     "description": "Alias of src; prefer src. Read only when src is omitted."},
        "intensity": {"type": "number", "default": 1.0, "description": "0..1 blend amount"},
        "replace": {"type": "boolean", "default": False,
                    "description": "Swap out any LUT already on the clip(s) instead of stacking a second one"}},
       ["src"]),
    _t("add_transition",
       "Add a transition at a timeline boundary (t in seconds, between two adjacent V1 clips). "
       "Call list_transitions for the categorized catalog with descriptions.",
       "effects",
       {
           "at": {"type": "number"},
           # Enum generated from the render catalog — the previous hardcoded
           # 12-name list silently hid 45+ working transitions from the LLM.
           # `add_transition` validates this itself and answers with a
           # did-you-mean suggestion; the generic boundary check would fire
           # first and replace that with a wall of ~88 names.
           "type": {"type": "string", "enum": _transition_names(),
                    "x-validated-by-handler": True},
           # No `default` here on purpose: each transition has its own
           # (render.transitions.default_duration — a whip 0.25 s, a dip to
           # black 0.6 s) and the handler applies it when this is omitted.
           # `list_transitions.defaults` advertises the number per name.
           "duration": {"type": "number",
                        "description": "Seconds; omit for the transition's own default "
                                       "(see list_transitions → defaults)."},
       },
       ["at"]),
    _t("remove_transition",
       "Remove transition(s) on V1: pass `at` (cut time in seconds) to clear "
       "every transition at that boundary, or all=true to clear the track.",
       "effects",
       {
           "at": {"type": "number"},
           "all": {"type": "boolean", "default": False},
       },
       []),
    _t("list_transitions",
       "The full transition catalog: every accepted name, CapCut-style families "
       "with display names and descriptions (`entries`), and the per-transition "
       "default duration add_transition applies when none is given (`defaults`).",
       "effects", {}),
    _t("check_features",
       "What THIS install can actually do: which optional features (captions, "
       "stem isolation, background removal, upscaling, …) are available, and the "
       "exact fix for each one that is not. Cheap and read-only. Call this before "
       "telling the user that anything is missing or broken, and before "
       "suggesting any install command — never guess at either.",
       "project", {}),
    _t("add_mask",
       "Add a vector mask to a clip (everything outside is hidden / black-padded). "
       "On a PIP (v2+) clip this is the SHAPE of the picture-in-picture: 'circle' "
       "and 'rounded' are cut to the element itself, so a PIP can be a circle "
       "instead of a rectangle. 'rectangle' is the default frame shape — use "
       "remove_mask to go back to it.",
       "effects",
       {
           "clip_id": {"type": "string"},
           "type": {"type": "string", "enum": ["circle", "rounded", "rectangle", "linear"]},
           "feather": {"type": "number", "default": 8.0,
                       "description": "Soft edge, v1 clips only — a PIP shape is a hard cut."},
           "position": {"type": "array", "items": {"type": "number"}, "description": "[x, y] in canvas coords"},
           "invert": {"type": "boolean", "default": False},
       },
       ["clip_id", "type"]),
    _t("remove_mask",
       "Clear a clip's mask, returning it to the full rectangular frame.",
       "effects",
       {"clip_id": {"type": "string"}},
       ["clip_id"]),
    _t("set_pip_framing",
       "Pan/zoom/rotate the picture INSIDE a PIP's shape — which part of the source fills "
       "the circle or the cropped box. Separate from set_clip_transform, which places "
       "and sizes the PIP on the canvas. Only visible where the PIP is cropped (a "
       "circle shape, or fit='cover').",
       "edit",
       {
           "clip_id": {"type": "string"},
           "x": {"type": "number", "description": "-1..1, 0 = centred (horizontal pan)"},
           "y": {"type": "number", "description": "-1..1, 0 = centred (vertical pan)"},
           "zoom": {"type": "number", "description": ">= 1; 1 = just covers the box"},
           "rotation": {"type": "number",
                        "description": "degrees, -180..180; turns the PICTURE inside "
                                       "the shape. The shape itself does not move — "
                                       "use set_clip_transform's rotation for that."},
       },
       ["clip_id"]),
    _t("chroma_key",
       "Green/blue-screen key on a media clip (works on V1 and PIP clips). "
       "Pass color=null to clear an existing key.",
       "effects",
       {
           "clip_id": {"type": "string"},
           "color": {"type": ["string", "null"], "default": "#00FF00"},
           "similarity": {"type": "number", "default": 0.4},
           "smoothness": {"type": "number", "default": 0.1},
           "spill_suppress": {"type": "number", "default": 0.5},
       },
       ["clip_id"]),
    _t("list_filters",
       "List every effect type add_effect understands (read-only discovery; no "
       "timeline change). Call this before add_effect if unsure a type exists.",
       "effects", {}),
    _t("list_luts",
       "List the bundled .cube LUT files available to apply_lut (read-only "
       "discovery; no timeline change).",
       "effects", {}),
]


TTS_TOOLS = [
    _t("tts_voiceover",
       "Generate a Piper TTS voiceover line and drop it on the vo track. The voice "
       "model downloads on first use (~60MB).",
       "audio",
       {
           "text": {"type": "string"},
           "voice": {"type": "string", "default": "en_US-amy-medium",
                     "description": "Piper voice name; en_US-amy-medium is downloaded by default"},
           "start": {"type": "number", "default": 0.0},
           "volume_db": {"type": "number", "default": 0.0},
       },
       ["text"]),
]


HEAVY_AI_TOOLS = [
    _t("vocal_isolate",
       "Demucs: extract just the vocal stem from a clip's audio and put it on the vo "
       "track (the original clip audio is muted). ~30s for a 30s clip on CPU.",
       "audio",
       {"clip_id": {"type": "string"}},
       ["clip_id"]),
    _t("instrumental_isolate",
       "Demucs: extract everything except vocals (drums + bass + other) and place it "
       "on the music track. The original clip audio is muted.",
       "audio",
       {"clip_id": {"type": "string"}},
       ["clip_id"]),
    _t("upscale",
       "Real-ESRGAN GPU upscale a clip by an integer factor (2 or 4). Replaces the "
       "clip's source with a higher-resolution version. Takes ~1s/frame on Apple Silicon.",
       "ai",
       {"clip_id": {"type": "string"}, "factor": {"type": "integer", "default": 2, "enum": [2, 4]}},
       ["clip_id"]),
    _t("stabilize",
       "Two-pass libvidstab stabilization; replaces the clip's source with the "
       "stabilized render. Slow (two full passes over the clip).",
       "ai",
       {"clip_id": {"type": "string"}},
       ["clip_id"]),
    _t("remove_background",
       "Strip a clip's background (rembg/u2net, downloads ~170MB model on first "
       "use). By default flattens onto green so a follow-up chroma_key composites "
       "it; pass bg_color=null for true alpha.",
       "ai",
       {"clip_id": {"type": "string"},
        "bg_color": {"type": ["string", "null"], "default": "#00FF00"},
        "model": {"type": "string", "default": "u2net",
                  "description": "rembg session model name (default 'u2net'; e.g. "
                                 "'u2net_human_seg', 'isnet-general-use'). Each new "
                                 "model downloads its weights on first use."}},
       ["clip_id"]),
    _t("object_erase",
       "LaMa inpaint: erase a bbox region across a time window on a clip "
       "(downloads ~196MB model on first use).",
       "ai",
       {"clip_id": {"type": "string"},
        "bbox": {"type": "array", "items": {"type": "number"},
                 "description": "[x, y, w, h] normalized 0..1"},
        "t_start": {"type": "number", "default": 0.0},
        "t_end": {"type": "number"}},
       ["clip_id", "bbox"]),
    _t("motion_track",
       "Track a bounding box through a video clip and write the path as x/y "
       "keyframes on a target overlay (sticker recommended — sticker x/y "
       "keyframes animate in the render).",
       "ai",
       {"clip_id": {"type": "string"},
        "target_id": {"type": "string"},
        "bbox": {"type": "array", "items": {"type": "number"},
                 "description": "[x, y, w, h] normalized 0..1 in the source frame"},
        "method": {"type": "string", "enum": ["mil", "vit"], "default": "mil"},
        "sample_every": {"type": "integer", "default": 2}},
       ["clip_id", "target_id", "bbox"]),
    _t("multicam",
       "Multi-cam switcher: audio-sync N angle files, pick the best take per "
       "window, and rewrite V1 as the resulting cuts.",
       "ai",
       {"srcs": {"type": "array", "items": {"type": "string"},
                 "description": "Paths to the angle files; first = sync reference"},
        "window_s": {"type": "number", "default": 2.0},
        "total": {"type": "number",
                  "description": "Total programme length in seconds; omit to use the "
                                 "shortest angle's duration"},
        "replace_v1": {"type": "boolean", "default": True}},
       ["srcs"]),
    _t("diarize",
       "Speaker diarization of the V1 source (pyannote with an HF token, else a "
       "local heuristic). Read-only: returns speaker turns; use "
       "assign_caption_speakers to apply them to captions.",
       "ai",
       {"num_speakers": {"type": "integer", "default": 2},
        "fallback": {"type": "boolean", "default": True}},
       []),
    _t("assign_caption_speakers",
       "Tag caption clips with diarized speakers and color-code each speaker's "
       "captions (brand palette first). Runs diarize when `turns` is omitted.",
       "ai",
       {"num_speakers": {"type": "integer", "default": 2},
        "turns": {"type": "array", "items": {"type": "object"},
                  "description": "Optional pre-computed [{speaker,start,end}] turns"},
        "fallback": {"type": "boolean", "default": True,
                     "description": "Forwarded to diarize when turns is omitted: allow "
                                    "the local MFCC/KMeans heuristic when pyannote "
                                    "(HUGGINGFACE_TOKEN) is unavailable; false forces "
                                    "pyannote."}},
       []),
    _t("smooth_slow_motion",
       "RIFE optical-flow frame interpolation for buttery slow-mo on a media clip "
       "(unlike set_speed, which just stretches existing frames). Replaces the clip's "
       "source; its duration becomes original × factor, and the rife binary/model must "
       "be installed locally.",
       "ai",
       {
           "clip_id": {"type": "string"},
           "factor": {"type": "integer", "default": 2, "description": "Slow-down multiple, e.g. 2 or 4"},
       },
       ["clip_id"]),
    _t("transcribe",
       "Transcribe the first v1 clip's source with Whisper and persist the "
       "transcript (ingest.json or the session's transcript.json) WITHOUT laying "
       "captions or touching the timeline. Reuses an existing transcript unless "
       "force=true. Refuses a model that is not already downloaded.",
       "ai",
       {"model": {"type": "string", "enum": ["small", "large-v3-turbo", "large-v3"],
                  "description": "Whisper model; default WHISPER_MODEL (small). Must already be on disk."},
        "force": {"type": "boolean", "default": False,
                  "description": "Re-transcribe even when a transcript is already persisted."}},
       []),
    _t("make_shorts",
       "Heuristically pick N highlight ranges from the V1 source (transcript + audio "
       "energy) for cutting a long video into shorts. Default returns the ranges only; "
       "pass save_as_sessions=true to also create one NEW session per short.",
       "ai",
       {
           "target_count": {"type": "integer", "default": 3},
           "max_dur": {"type": "number", "default": 60.0, "description": "Cap each short at this many seconds"},
           "min_dur": {"type": "number", "default": 12.0, "description": "Pad each short to at least this long"},
           "save_as_sessions": {"type": "boolean", "default": False},
       }),
    _t("name_speakers",
       "Save a diarized-speaker → display-name mapping (e.g. {'SPEAKER_00': 'Host'}) "
       "to the session for lower-thirds. Informational only for now — it does not "
       "change the timeline; run diarize first to learn the speaker labels.",
       "ai",
       {"mapping": {"type": "object",
                    "description": "{SPEAKER_XX: 'Display Name'} pairs"}},
       ["mapping"]),
]


VISION_TOOLS = [
    _t("find_moments",
       "Find moments in the project's source clip(s) by natural-language query. "
       "Ranks transcript segments first; verifies top candidates with Claude vision. "
       "Returns up to top_k {start, end, transcript, shot_description} matches.",
       "vision",
       {"query": {"type": "string"}, "top_k": {"type": "integer", "default": 3}},
       ["query"]),
    _t("search_media",
       "Search the project's footage by content. scope='visual' uses a LOCAL CLIP "
       "model to match keyframes to the text query (e.g. 'a sunset over water') with "
       "no transcript needed; scope='spoken' searches the transcript; 'both' merges "
       "them. Returns clips ranked by relevance with timestamps. Frame embeddings "
       "are cached so repeat searches are instant.",
       "vision",
       {"query": {"type": "string"},
        "scope": {"type": "string", "enum": ["visual", "spoken", "both"], "default": "both"},
        "limit": {"type": "integer", "default": 10}},
       ["query"]),
    _t("match_style",
       "Analyze a reference video and return its style fingerprint: cuts/min, median "
       "shot length, BPM, dominant color palette. Use this to seed a new edit that "
       "mimics a viral reference's rhythm.",
       "vision",
       {"reference": {"type": "string", "description": "Absolute path to the reference video"}},
       ["reference"]),
]


ALL_TOOLS: list[ToolSchema] = (
    INSPECTION_TOOLS + EDIT_TOOLS + PROJECT_TOOLS + TEXT_TOOLS
    + AUDIO_TOOLS + SHOW_TOOLS + EFFECT_TOOLS + VISION_TOOLS + TTS_TOOLS
    + HEAVY_AI_TOOLS
)


# --- Numeric bounds (QA-041) -------------------------------------------------
#
# Every numeric argument used to be unbounded: `move_clip new_start=1e12`,
# `set_volume db=+1000000`, `color_grade brightness=1e9`, `add_marker
# time=1e300` and `set_speed factor=1e6` all returned 200. A typo of 100000 in
# Properties' "Start on timeline" built a 27-hour timeline and started an ffmpeg
# that rendered black for minutes, still running after Undo. The bounds live IN
# the schema (so Claude sees them as `minimum`/`maximum`), and
# `dispatch._validate_tool_args` enforces whatever the schema declares — one
# source, the same pattern as the enums.
#
# The ranges are "physically meaningful", not stylistic: the prompt validator's
# `ARG_BOUNDS` is deliberately tighter (a plan from a small local model should
# stay inside house style), while these only reject values no edit can mean.
# Time arguments get an UPPER bound only — handlers have always CLAMPED a
# negative time to 0 (`_num(min=0)`), and rejecting a UI's -0.0004 rounding
# residue would turn a harmless clamp into a failed edit.

#: Longest timeline any time argument may address: 6 hours. Well past a
#: feature-length edit or a long podcast, far short of a typo's 100000 s.
TIMELINE_MAX_SECONDS = 6 * 3600

#: Argument names that are timeline/source seconds, on every tool that has them.
_TIME_ARGS = frozenset({"start", "end", "new_start", "time", "at", "in", "out",
                        "t_start", "t_end", "in_s", "out_s", "max_dur", "min_dur",
                        "total", "max_duration"})

_GAIN = (-96.0, 24.0)
_UNIT = (0.0, 1.0)
_POS = (-100000.0, 100000.0)

#: (tool, arg) -> (minimum, maximum); None leaves that side open.
_ARG_BOUNDS: dict[tuple[str, str], tuple[float | None, float | None]] = {
    ("set_volume", "db"): _GAIN,
    ("add_music", "volume_db"): _GAIN,
    ("tts_voiceover", "volume_db"): _GAIN,
    ("set_duck", "to_db"): (-96.0, 0.0),
    ("set_loudness_target", "lufs"): (-70.0, -5.0),
    ("set_speed", "factor"): (0.1, 100.0),
    # the ONE tables (review RE: these were restated by hand)
    ("set_canvas_background", "blur"): (1.0, float(_canvas_blur_max())),
    ("set_voice_effect", "intensity"): _UNIT,
    ("set_animation", "in_duration"): _anim_dur_range(),
    ("set_animation", "out_duration"): _anim_dur_range(),
    ("freeze_frame", "duration"): (0.1, 60.0),
    ("color_grade", "brightness"): (-1.0, 1.0),
    ("color_grade", "contrast"): (0.0, 4.0),
    ("color_grade", "saturation"): (0.0, 3.0),
    ("color_grade", "sat"): (0.0, 3.0),
    ("color_grade", "gamma"): (0.1, 10.0),
    ("color_grade", "temp"): (-1.0, 1.0),
    ("color_grade", "tint"): (-1.0, 1.0),
    ("add_transition", "duration"): (0.0, 60.0),
    ("add_hook_overlay", "duration"): (0.1, 60.0),
    ("set_clip_transform", "x"): _POS,
    ("set_clip_transform", "y"): _POS,
    ("add_text", "x"): _POS,
    ("add_text", "y"): _POS,
    ("add_text", "size"): (1.0, 2000.0),
    ("add_text", "stroke_w"): (0.0, 200.0),
    # Same text bounds on the caption look (QA-107: size 1e6 / stroke_w 1e5
    # returned 200 and then every export 500'd). The models clamp too.
    ("set_caption_style", "size"): (1.0, 2000.0),
    ("set_caption_style", "stroke_w"): (0.0, 200.0),
    ("set_pip_framing", "x"): (-10.0, 10.0),
    ("set_pip_framing", "y"): (-10.0, 10.0),
    ("set_pip_framing", "zoom"): (0.1, 20.0),
    ("set_pip_framing", "rotation"): (-3600.0, 3600.0),
    ("chroma_key", "similarity"): _UNIT,
    ("chroma_key", "smoothness"): _UNIT,
    ("chroma_key", "spill_suppress"): _UNIT,
    # (apply_lut.intensity is deliberately absent: the handler CLAMPS it to
    # [0, 1] and test_apply_lut_clamps_intensity pins that contract.)
    ("noise_reduce", "strength"): _UNIT,
    ("add_mask", "feather"): (0.0, 1000.0),
    ("auto_cut_to_beats", "subdivision"): (1, 64),
    ("auto_cut_to_beats", "min_shot"): (0.0, 60.0),
    ("remove_silences", "threshold_db"): (-120.0, 0.0),
    ("remove_silences", "min_dur"): (0.0, 60.0),
    ("remove_silences", "keep_pad"): (0.0, 10.0),
    ("remove_fillers", "pad"): (0.0, 10.0),
    ("find_broll", "top_k"): (1, 1000),
    ("find_moments", "top_k"): (1, 1000),
    ("search_media", "limit"): (1, 1000),
    ("make_shorts", "target_count"): (1, 50),
    ("multicam", "window_s"): (0.1, 3600.0),
    ("diarize", "num_speakers"): (1, 32),
    ("assign_caption_speakers", "num_speakers"): (1, 32),
    ("auto_caption", "max_chars"): (1, 500),
    ("auto_caption", "max_cps"): (1.0, 100.0),
    ("motion_track", "sample_every"): (1, 1000),
    ("smooth_slow_motion", "factor"): (2, 16),
    ("upscale", "factor"): (1, 4),
}


def _is_numeric(spec: dict) -> bool:
    declared = spec.get("type")
    variants = declared if isinstance(declared, list) else [declared]
    return any(v in ("number", "integer") for v in variants)


def _apply_numeric_bounds(tools: list[ToolSchema]) -> None:
    """Write `minimum`/`maximum` into each numeric property that has a bound
    and does not already declare one (a hand-written bound always wins)."""
    for t in tools:
        props = t["input_schema"].get("properties") or {}
        for key, spec in props.items():
            if not isinstance(spec, dict) or not _is_numeric(spec):
                continue
            lo, hi = _ARG_BOUNDS.get((t["name"], key), (None, None))
            if key in _TIME_ARGS and (t["name"], key) not in _ARG_BOUNDS:
                hi = TIMELINE_MAX_SECONDS
            if lo is not None and "minimum" not in spec:
                spec["minimum"] = lo
            if hi is not None and "maximum" not in spec:
                spec["maximum"] = hi


_apply_numeric_bounds(ALL_TOOLS)


def list_tools(categories: list[str] | None = None) -> list[ToolSchema]:
    if categories is None:
        return ALL_TOOLS
    return [t for t in ALL_TOOLS if t["category"] in categories]


def input_schema_for(tool: str) -> dict | None:
    """The advertised `input_schema` of one tool, or None for an unknown name."""
    return next((t["input_schema"] for t in ALL_TOOLS if t["name"] == tool), None)


def unknown_args(tool: str, args: dict) -> list[str]:
    """Argument names in `args` that `tool`'s input_schema does not list.

    This is the check a key-free plan validator needs: a local model must never
    smuggle a path or an unfamiliar knob past the schema, and that is only safe
    to enforce because every argument a handler reads is now advertised
    (tests/test_tool_schema_completeness.py keeps it that way). It is
    deliberately NOT called from dispatch(): the dispatcher tolerates extra keys
    today (see _validate_tool_args), and flipping that is a behaviour change for
    every existing caller. Keeping the rule here means the validator and any
    future dispatch hook share one definition instead of drifting.
    """
    schema = input_schema_for(tool)
    if schema is None:
        return sorted(args)
    advertised = set(schema.get("properties") or {})
    return sorted(k for k in args if k not in advertised)
