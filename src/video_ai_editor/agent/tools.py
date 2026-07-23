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
    _t("trim_clip", "Adjust a clip's source in/out (does not move its timeline start).",
       "edit",
       {"clip_id": {"type": "string"}, "in": {"type": "number"}, "out": {"type": "number"}},
       ["clip_id"]),
    _t("move_clip", "Move a clip to a new timeline start (and optional new track).",
       "edit",
       {"clip_id": {"type": "string"}, "new_start": {"type": "number"}, "new_track": {"type": "string"}},
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
       "Set a media clip's playback speed factor (1.0 = normal, 2.0 = double, "
       "0.5 = half). Constant speed only — no per-clip speed curves yet.",
       "edit",
       {"clip_id": {"type": "string"}, "factor": {"type": "number"}},
       ["clip_id", "factor"]),
    _t("set_clip_transform",
       "Set transform properties on any clip (media, sticker, or text): x/y position, "
       "scale, rotation (degrees), opacity — this is THE tool for positioning things on "
       "the canvas. For text and stickers, x/y are ABSOLUTE CANVAS PIXELS (e.g. 540,960 "
       "= center of a 1080×1920 canvas), and setting a value replaces any keyframes on "
       "that property with the scalar.",
       "edit",
       {
           "clip_id": {"type": "string"},
           "x": {"type": "number", "description": "Canvas px (absolute for text/stickers)"},
           "y": {"type": "number", "description": "Canvas px (absolute for text/stickers)"},
           "scale": {"type": "number", "description": "1.0 = original size"},
           "rotation": {"type": "number", "description": "Degrees"},
           "opacity": {"type": "number", "description": "0..1"},
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
       "bounce modes animate in the browser preview but bake as linear.",
       "edit",
       {
           "clip_id": {"type": "string"},
           "prop": {"type": "string", "enum": ["x", "y", "scale", "rotation", "opacity"]},
           "time": {"type": "number", "description": "Clip-local seconds (0 = clip start)"},
           "value": {"type": "number"},
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
           "prop": {"type": "string", "enum": ["x", "y", "scale", "rotation", "opacity"]},
           "time": {"type": "number", "description": "Clip-local seconds of the key to remove"},
       },
       ["clip_id", "prop", "time"]),
]

# --- Project / canvas ---

PROJECT_TOOLS = [
    _t("set_canvas", "Set output canvas size and fps.", "project",
       {"w": {"type": "integer"}, "h": {"type": "integer"}, "fps": {"type": "integer"}}),
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
       "One-call platform setup: sets canvas size/fps, bitrate, and loudness target "
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
       }),
    _t("auto_caption",
       "BEST-QUALITY auto captions for Hindi + English (and Hinglish). Re-transcribes "
       "the video with the large-v3 Whisper model on Metal (far better than the fast "
       "upload model — clean Hindi, no hallucination loops), then formats the words "
       "into broadcast-grade cues (≤2 lines, reading-speed limited) and lays down a "
       "caption track. Auto-detects language by default; pass language='hi' or 'en' to "
       "force. This is the tool to use when the user asks for accurate captions.",
       "text",
       {
           "style": {"type": "string", "enum": ["default", "ig_chunky", "word_emphasis"], "default": "ig_chunky"},
           "position": {"type": "string", "enum": ["bottom", "center", "top"], "default": "bottom"},
           "language": {"type": "string", "description": "Force 'hi'/'en'; omit to auto-detect (Hinglish-friendly)"},
           "model": {"type": "string", "description": "Override Whisper model (default large-v3)"},
           "max_chars": {"type": "integer", "default": 42},
           "max_cps": {"type": "number", "default": 17.0},
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
           "font": {"type": "string", "description": "Bundled font file, e.g. BebasNeue-Regular"},
           "anim_in": {"type": "string", "enum": ["pop", "fade", "slide_up", "slide_down"]},
           "anim_out": {"type": "string", "enum": ["pop", "fade", "slide_up", "slide_down"]},
       },
       ["text", "start", "end"]),
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
       "Add a sticker overlay: an emoji character (fetched as Twemoji artwork) or a "
       "PNG file path, at a canvas position for a time window.",
       "text",
       {
           "emoji": {"type": "string", "description": "Emoji character, e.g. 🔥"},
           "src": {"type": "string", "description": "PNG path (alternative to emoji)"},
           "start": {"type": "number", "default": 0.0},
           "end": {"type": "number"},
           "position": {"type": "array", "items": {"type": "number"},
                        "description": "[x, y] canvas px, default center"},
           "scale": {"type": "number", "default": 1.0},
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
    _t("add_fade",
       "Add audio fade in / fade out to a clip.",
       "audio",
       {"clip_id": {"type": "string"}, "in_s": {"type": "number"}, "out_s": {"type": "number"}},
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
       "Find filler-word ranges in the transcript and ripple-cut them out (um, uh, like, …).",
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
       {"subdivision": {"type": "integer", "default": 4, "description": "Cut every Nth beat (4 = every bar in 4/4)"}}),
    _t("auto_reframe",
       "Switch canvas aspect (9:16 / 16:9 / 1:1 / 4:5). M3 does a center-crop; "
       "subject-tracked reframing lands in M5.",
       "auto",
       {"ratio": {"type": "string", "enum": ["9:16", "16:9", "1:1", "4:5"]}},
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
       "Append a per-clip video effect. Types: color, lut, blur, sharpen, vignette, "
       "grain, vintage, vhs, glow, hflip, vflip, rgb_split.",
       "effects",
       {
           "clip_id": {"type": "string"},
           "type": {"type": "string", "enum": [
               "color", "lut", "blur", "sharpen", "vignette",
               "grain", "vintage", "vhs", "glow", "hflip", "vflip", "rgb_split",
           ]},
           "params": {"type": "object", "description": "type-specific params"},
       },
       ["clip_id", "type"]),
    _t("remove_effect",
       "Remove the Nth effect from a clip's effect chain.",
       "effects",
       {"clip_id": {"type": "string"}, "index": {"type": "integer"}},
       ["clip_id", "index"]),
    _t("color_grade",
       "Convenience: add a color effect with brightness/contrast/saturation/temp/tint. "
       "Applies to one clip or all V1 clips if clip_id omitted.",
       "effects",
       {
           "clip_id": {"type": "string"},
           "brightness": {"type": "number", "description": "-1..1, 0=neutral"},
           "contrast": {"type": "number", "description": "0..2, 1=neutral"},
           "saturation": {"type": "number", "description": "0..3, 1=neutral"},
           "gamma": {"type": "number", "description": "0.1..10, 1=neutral"},
           "temp": {"type": "number", "description": "-1 cool .. +1 warm"},
           "tint": {"type": "number", "description": "-1 magenta .. +1 green"},
       }),
    _t("apply_lut",
       "Apply a 3D LUT (.cube) to a clip or all V1 clips.",
       "effects",
       {"clip_id": {"type": "string"}, "src": {"type": "string"}, "intensity": {"type": "number"}},
       ["src"]),
    _t("add_transition",
       "Add a transition at a timeline boundary (t in seconds, between two adjacent V1 clips). "
       "Call list_transitions for the categorized catalog with descriptions.",
       "effects",
       {
           "at": {"type": "number"},
           # Enum generated from the render catalog — the previous hardcoded
           # 12-name list silently hid 45+ working transitions from the LLM.
           "type": {"type": "string", "enum": _transition_names()},
           "duration": {"type": "number", "default": 0.5},
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
       "The full transition catalog: categories, aliases, and descriptions.",
       "effects", {}),
    _t("add_mask",
       "Add a vector mask to a clip (everything outside is hidden / black-padded).",
       "effects",
       {
           "clip_id": {"type": "string"},
           "type": {"type": "string", "enum": ["circle", "rectangle", "linear"]},
           "feather": {"type": "number", "default": 8.0},
           "position": {"type": "array", "items": {"type": "number"}, "description": "[x, y] in canvas coords"},
           "invert": {"type": "boolean", "default": False},
       },
       ["clip_id", "type"]),
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
        "bg_color": {"type": ["string", "null"], "default": "#00FF00"}},
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
                  "description": "Optional pre-computed [{speaker,start,end}] turns"}},
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


def list_tools(categories: list[str] | None = None) -> list[ToolSchema]:
    if categories is None:
        return ALL_TOOLS
    return [t for t in ALL_TOOLS if t["category"] in categories]
