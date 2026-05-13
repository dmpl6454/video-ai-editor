"""Tool registry — JSON schemas for Claude tool use.

Tools are organised by category. M1 ships with the inspection + edit + project
tools that the timeline UI needs to function. Effects/AI tools land in M2+.
"""
from __future__ import annotations
from typing import Callable

# Each tool is registered with: name, category, schema (Anthropic tool format),
# and a handler function. Handlers live in dispatch.py and are bound at import time.

ToolSchema = dict


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
]

# --- text / captions / brand kit / audit (M2) ---

TEXT_TOOLS = [
    _t("add_super_text",
       "Add a bold on-screen text overlay (the 'super' look). Use role='hook' for a "
       "first-3-seconds curiosity hook, role='super' for mid-video punctuation, "
       "role='lower_third' for guest name/handle.",
       "text",
       {
           "text": {"type": "string"},
           "start": {"type": "number"},
           "end": {"type": "number"},
           "role": {"type": "string", "enum": ["super", "hook", "lower_third", "label"], "default": "super"},
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
    _t("set_volume",
       "Set audio gain (dB) on a track id (e.g. 'a1', 'music', 'vo') or a clip id ('c_xxx').",
       "audio",
       {"target": {"type": "string"}, "db": {"type": "number"}},
       ["target", "db"]),
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
       "Add a transition at a timeline boundary (t in seconds, between two adjacent V1 clips).",
       "effects",
       {
           "at": {"type": "number"},
           "type": {"type": "string", "enum": [
               "fade", "fadeblack", "fadewhite", "wiperight", "wipeleft", "slideright", "slideleft",
               "circleopen", "circleclose", "dissolve", "pixelize", "radial",
           ]},
           "duration": {"type": "number", "default": 0.5},
       },
       ["at"]),
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
]


VISION_TOOLS = [
    _t("find_moments",
       "Find moments in the project's source clip(s) by natural-language query. "
       "Ranks transcript segments first; verifies top candidates with Claude vision. "
       "Returns up to top_k {start, end, transcript, shot_description} matches.",
       "vision",
       {"query": {"type": "string"}, "top_k": {"type": "integer", "default": 3}},
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
