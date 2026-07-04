# CapCut-Parity Gap Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the five architecture gaps between this editor and its "CapCut-class + Claude AI" goal: (1) expose the 39 user-facing tools currently invisible to the chat LLM, (2) make tool-registry drift structurally impossible, (3) export keyframe easing curves instead of silently downgrading to linear, (4) export text `anim_in`/`anim_out` animations instead of dropping them, (5) make the chat-turn and undo-depth limits configurable.

**Architecture:** All changes ride the existing single-mutation-path design. Tool exposure is pure registration in `agent/tools.py` (handlers already exist and are smoke-tested). Registry integrity is enforced by a new AST-based test. Easing lands in `edl/keyframes.py:to_ffmpeg_expr` (one function, mirrored against the Python `sample()` oracle and `frontend/src/lib/overlay.ts`). Text animation export extends the existing `anim_text` geq-alpha path in `render/text_overlay.py`, and fixes a latent overlay timebase bug as a prerequisite. Limits become env vars read at call time.

**Tech Stack:** Python 3.13 / Pydantic v2 / FastAPI backend, ffmpeg filtergraphs for rendering, pytest (`uv run pytest`, `pythonpath=["src"]`, no conftest — per-file fixtures with ffmpeg-lavfi media).

---

## Context an engineer needs before starting

- **Run tests with** `uv run pytest tests/<file>.py -v` from the repo root. First run `uv sync --all-extras --group dev` (plain `uv sync` does NOT install pytest). Never launch the app with `uv run video-ai-editor` on macOS — use `bash run.sh` (see CLAUDE.md, hidden-`.pth` bug).
- **The dispatch engine:** `src/video_ai_editor/agent/dispatch.py` (~2700 lines) holds ~90 plain functions `fn(store, args) -> dict` plus a literal `DISPATCH = {"name": fn, ...}` dict at the bottom. `src/video_ai_editor/agent/tools.py` holds the JSON schemas advertised to the chat LLM (`ALL_TOOLS`, built by concatenating per-category lists of `_t(...)` calls). The two files are matched **by hand** — nothing enforces the pairing. Today `DISPATCH` has 95 entries (92 unique — 3 duplicated keys) and `ALL_TOOLS` has 49.
- **The `_t` helper** (tools.py:15):
  ```python
  def _t(name: str, description: str, category: str, properties: dict, required: list[str] | None = None) -> ToolSchema:
  ```
  `category` is internal (dropped before sending to Claude — see `_anthropic_tools()` in `agent/loop.py`), so new category strings are safe.
- **Every DISPATCH tool is already smoke-tested** by `tests/test_all_tools_smoke.py` (parametrizes over `sorted(DISPATCH.keys())`). Exposure tasks therefore need no new handler tests — only registry/schema tests.
- **Commit style** (from git log): conventional commits — `fix(scope): ...`, `feat(scope): ...`, `test(scope): ...`, `docs(scope): ...`.
- **Windows CI is the source of truth for Windows behavior.** Nothing in this plan shells out with new subprocess calls or embeds paths in filtergraphs, so the four Windows footguns in CLAUDE.md are not triggered — but the final task still pushes and watches CI.

## Out of scope (deliberate, with rationale)

- **Realtime GPU preview compositing.** The render-then-poll preview (ffmpeg keyed by `edl.hash()`) is a structural design choice; replacing it is a rewrite of `render/`, not a fix. The fidelity tasks here (easing + anim export) instead shrink the *divergence* between browser preview and export.
- **Chunk cache under transitions.** `xfade` needs both neighbor streams in one filtergraph; partial chunking around transition boundaries is a research task, not a bounded fix.
- **Effects/template marketplace, mobile app.** Product roadmap, not architecture defects.

## File map

| File | Change |
|---|---|
| `src/video_ai_editor/agent/dispatch.py` | Remove 3 duplicate `DISPATCH` keys + 3 dead shadowed function defs |
| `src/video_ai_editor/agent/tools.py` | +39 `_t(...)` schemas across existing + 3 new category lists |
| `src/video_ai_editor/agent/system_prompt.py` | One-line capability note for the newly reachable tool areas |
| `src/video_ai_editor/agent/loop.py` | `max_turns` default from `VAI_CHAT_MAX_TURNS` |
| `src/video_ai_editor/edl/keyframes.py` | Easing curves in `to_ffmpeg_expr` |
| `src/video_ai_editor/edl/snapshot.py` | Undo depth from `VAI_MAX_UNDO` (default raised 30 → 100) |
| `src/video_ai_editor/render/text_overlay.py` | anim_in/anim_out export + overlay timebase fix |
| `src/video_ai_editor/render/...` (located in Task 11) | Render-salt in preview cache key |
| `tests/test_tool_registry.py` | NEW — registry integrity (AST duplicate scan, parity, schema shape) |
| `tests/test_keyframes_easing.py` | NEW — easing parity vs `sample()` oracle |
| `tests/test_text_anim_export.py` | NEW — rendered-pixel assertions for anim export |
| `tests/test_limits_config.py` | NEW — env-configurable limits |
| `CLAUDE.md`, `.env.example` | Numbers + new env vars |

---

# Phase 1 — Registry integrity

### Task 1: Registry integrity test + de-duplicate DISPATCH

The three duplicate keys are `set_track_muted`, `vocal_isolate`, `instrumental_isolate`. Each is duplicated at **two layers**: the dict has two identical `"name": fn` lines, *and* the module has two top-level `def fn` with the same name (Python silently keeps the later def; the earlier is dead code). Current runtime behavior = the later def — so deleting the *earlier* def and the *later* dict line changes nothing by construction.

**Files:**
- Create: `tests/test_tool_registry.py`
- Modify: `src/video_ai_editor/agent/dispatch.py`

- [ ] **Step 1: Write the registry test**

Create `tests/test_tool_registry.py` with exactly this content:

```python
"""Registry integrity — DISPATCH and ALL_TOOLS must not drift.

DISPATCH (agent/dispatch.py) and ALL_TOOLS (agent/tools.py) are matched by
hand; these tests are the enforcement the pairing never had.
"""
import ast
from pathlib import Path

import video_ai_editor.agent.dispatch as dispatch_mod
from video_ai_editor.agent.dispatch import DISPATCH
from video_ai_editor.agent.tools import ALL_TOOLS

DISPATCH_SRC = Path(dispatch_mod.__file__)

# Tools deliberately NOT advertised to the chat LLM. Keep small and justified.
INTERNAL_ONLY = {
    "repair_chunks",       # cache maintenance; no user-facing semantics
    "repair_media_paths",  # session repair after media moved on disk
    "pyannote_status",     # setup diagnostics, meaningless to the LLM
    "record_voiceover",    # needs browser MediaRecorder; UI-only entry point
}

# TEMPORARY: tools awaiting exposure (Tasks 2-8 each delete their batch;
# Task 9 deletes this set entirely). New tools must NOT be added here.
NOT_YET_EXPOSED = {
    "add_keyframe", "add_marker", "add_sticker", "add_text",
    "apply_export_preset", "apply_hook_stack", "apply_text_template",
    "bulk_delete", "bulk_duplicate", "chroma_key", "diarize",
    "export_ass", "export_srt", "export_vtt", "find_broll", "import_srt",
    "list_filters", "list_luts", "list_shows", "list_text_styles",
    "list_transitions", "make_shorts", "motion_track", "multicam",
    "name_speakers", "noise_reduce", "object_erase", "remove_background",
    "remove_keyframe", "remove_marker", "set_clip_timing",
    "set_clip_transform", "set_loudness_target", "set_property",
    "set_speed", "set_track_locked", "smooth_slow_motion", "stabilize",
    "translate_captions",
}


def _module_tree() -> ast.Module:
    return ast.parse(DISPATCH_SRC.read_text(encoding="utf-8"))


def _dispatch_dict_keys() -> list[str]:
    for node in _module_tree().body:
        target = None
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        if target == "DISPATCH":
            return [k.value for k in node.value.keys if k is not None]
    raise AssertionError("DISPATCH dict literal not found in dispatch.py")


def test_no_duplicate_dispatch_keys():
    keys = _dispatch_dict_keys()
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    assert not dupes, f"duplicate DISPATCH keys (later entry silently wins): {dupes}"


def test_no_shadowed_handler_defs():
    names = [n.name for n in _module_tree().body if isinstance(n, ast.FunctionDef)]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"duplicate top-level defs (earlier one is dead code): {dupes}"


def test_every_advertised_tool_is_dispatchable():
    missing = sorted(t["name"] for t in ALL_TOOLS if t["name"] not in DISPATCH)
    assert not missing, f"advertised to Claude but not in DISPATCH: {missing}"


def test_no_duplicate_advertised_names():
    names = [t["name"] for t in ALL_TOOLS]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"duplicate ALL_TOOLS entries: {dupes}"


def test_schema_shape():
    for t in ALL_TOOLS:
        schema = t["input_schema"]
        assert schema["type"] == "object", t["name"]
        for req in schema["required"]:
            assert req in schema["properties"], (
                f"{t['name']}: required arg {req!r} missing from properties"
            )


def test_dispatch_coverage_is_explicit():
    """Every DISPATCH tool is either advertised, internal, or queued — no drift."""
    advertised = {t["name"] for t in ALL_TOOLS}
    unexposed = set(DISPATCH) - advertised
    expected = INTERNAL_ONLY | NOT_YET_EXPOSED
    assert unexposed == expected, (
        f"unexpected unexposed tools: {sorted(unexposed - expected)}; "
        f"stale allowlist entries: {sorted(expected - unexposed)}"
    )
```

- [ ] **Step 2: Run — expect the two duplicate tests to FAIL**

Run: `uv run pytest tests/test_tool_registry.py -v`
Expected: `test_no_duplicate_dispatch_keys` FAILS listing `['instrumental_isolate', 'set_track_muted', 'vocal_isolate']`; `test_no_shadowed_handler_defs` FAILS with the same three names; the other five tests PASS.

- [ ] **Step 3: Remove the duplicate DISPATCH dict entries**

Locate them (line numbers may have drifted; as of writing: 2617, 2644, 2645):

```bash
grep -n '"set_track_muted": set_track_muted\|"vocal_isolate": vocal_isolate\|"instrumental_isolate": instrumental_isolate' src/video_ai_editor/agent/dispatch.py
```

Each name appears twice in the dict. Delete the **second** occurrence of each line (both occurrences reference the same symbol, so either is safe; keeping the first preserves reading order).

- [ ] **Step 4: Remove the dead shadowed function defs**

Locate both defs of each:

```bash
grep -n 'def set_track_muted\|def vocal_isolate\|def instrumental_isolate' src/video_ai_editor/agent/dispatch.py
```

For each of the three names there are two `def` blocks (verified: earlier defs at lines 365 (`set_track_muted`), 2076 (`vocal_isolate`), 2103 (`instrumental_isolate`); later ones at 1199, 2495, 2519). Python binds the **later** def, so the earlier block is unreachable dead code. Delete the **earlier** `def` block of each — from its `def` line through the line before the next top-level statement. Behavior is unchanged by construction (runtime already used the later def).

- [ ] **Step 5: Run registry + smoke tests — expect PASS**

Run: `uv run pytest tests/test_tool_registry.py tests/test_all_tools_smoke.py tests/test_tools_dispatch.py -v`
Expected: all PASS (smoke test exercises `set_track_muted`/`vocal_isolate`/`instrumental_isolate` through the surviving defs).

- [ ] **Step 6: Commit**

```bash
git add tests/test_tool_registry.py src/video_ai_editor/agent/dispatch.py
git commit -m "fix(agent): dedupe DISPATCH keys + dead shadowed handlers; add registry integrity test"
```

---

# Phase 2 — Expose the 39 missing tools to Claude chat

Pattern for every task in this phase: append `_t(...)` entries to a category list in `src/video_ai_editor/agent/tools.py`, delete the same names from `NOT_YET_EXPOSED` in `tests/test_tool_registry.py`, run the registry test, commit. Handlers, undo, and smoke coverage already exist — this is registration only.

Two conventions:
- Descriptions are written **for the LLM** — say what the tool does, when to use it, and unit semantics. They come from the handler docstrings (verified during evidence-gathering; each task lists its source args).
- After each task, sanity-check the projected count:
  ```bash
  python3 -c "import sys; sys.path.insert(0,'src'); from video_ai_editor.agent.tools import ALL_TOOLS; print(len(ALL_TOOLS))"
  ```

### Task 2: Expose edit/timing tools (7)

Handler args (from dispatch.py): `set_speed(clip_id, factor)`, `set_clip_timing(clip_id, start, end)`, `set_clip_transform(clip_id, x, y, scale, rotation, opacity)`, `set_property(clip_id, path, value)`, `bulk_delete(clip_ids)`, `bulk_duplicate(clip_ids)`, `set_track_locked(track, locked)`.

**Files:**
- Modify: `src/video_ai_editor/agent/tools.py` (append inside the `EDIT_TOOLS` list, before its closing `]`)
- Modify: `tests/test_tool_registry.py` (shrink `NOT_YET_EXPOSED`)

- [ ] **Step 1: Append the schemas to `EDIT_TOOLS`**

```python
    _t("set_speed", "Set playback-speed on a clip. 1.0 = normal, 2.0 = 2x fast, "
       "0.5 = slow-mo. The renderer applies setpts/atempo (audio pitch preserved).",
       "edit",
       {"clip_id": {"type": "string"},
        "factor": {"type": "number", "description": "Speed multiplier, e.g. 0.5 or 2.0"}},
       ["clip_id", "factor"]),
    _t("set_clip_timing", "Set start and/or end (timeline seconds) of an OVERLAY clip — "
       "a text or sticker. Media clips have a computed end; use trim_clip/move_clip for those.",
       "edit",
       {"clip_id": {"type": "string"},
        "start": {"type": "number"}, "end": {"type": "number"}},
       ["clip_id"]),
    _t("set_clip_transform", "Adjust transform on a clip, text, or sticker: x/y in canvas "
       "pixels (centre), scale multiplier, rotation in degrees, opacity 0-1. "
       "Only the fields you pass are changed.",
       "edit",
       {"clip_id": {"type": "string"},
        "x": {"type": "number"}, "y": {"type": "number"},
        "scale": {"type": "number"}, "rotation": {"type": "number"},
        "opacity": {"type": "number"}},
       ["clip_id"]),
    _t("set_property", "Generic dotted-path mutator — the 'tweak any field' escape hatch. "
       "Example: path='audio.gain_db', value=-6. Prefer the dedicated tool when one exists.",
       "edit",
       {"clip_id": {"type": "string"},
        "path": {"type": "string", "description": "Dotted EDL field path, e.g. 'audio.gain_db'"},
        "value": {"description": "New value; type depends on the field"}},
       ["clip_id", "path", "value"]),
    _t("bulk_delete", "Ripple-delete multiple clips in one operation (one undo step, "
       "instead of dispatching ripple_delete N times).",
       "edit",
       {"clip_ids": {"type": "array", "items": {"type": "string"}}},
       ["clip_ids"]),
    _t("bulk_duplicate", "Duplicate multiple clips in one operation; copies are appended "
       "after their originals.",
       "edit",
       {"clip_ids": {"type": "array", "items": {"type": "string"}}},
       ["clip_ids"]),
    _t("set_track_locked", "Lock or unlock a track (prevents accidental UI edits; "
       "render is unaffected).",
       "edit",
       {"track": {"type": "string"}, "locked": {"type": "boolean"}},
       ["track", "locked"]),
```

- [ ] **Step 2: Delete these 7 names from `NOT_YET_EXPOSED`**

Remove `"set_speed"`, `"set_clip_timing"`, `"set_clip_transform"`, `"set_property"`, `"bulk_delete"`, `"bulk_duplicate"`, `"set_track_locked"` from the set in `tests/test_tool_registry.py`.

- [ ] **Step 3: Run — expect PASS, count 56**

Run: `uv run pytest tests/test_tool_registry.py -v` → all PASS.
Run the count one-liner → `56`.

- [ ] **Step 4: Commit**

```bash
git add src/video_ai_editor/agent/tools.py tests/test_tool_registry.py
git commit -m "feat(agent): expose speed/transform/bulk edit tools to chat LLM"
```

### Task 3: Expose text & sticker tools (4)

Handler args: `add_text(text, start, end, role, x, y, scale, rotation, opacity, font, size, color, stroke, stroke_w, anim_in, anim_out)`, `add_sticker(src, emoji, start, end, position, scale, rotation)`, `apply_text_template(name, fields, start, end)`, `list_text_styles()`.

**Files:**
- Modify: `src/video_ai_editor/agent/dispatch.py` (fix the add_text default-role crash)
- Modify: `src/video_ai_editor/agent/tools.py` (append inside `TEXT_TOOLS`)
- Test: `tests/test_text_tools.py` (regression for the crash)
- Modify: `tests/test_tool_registry.py`

**Pre-existing bug (verified live):** `dispatch(store, "add_text", {"text": ..., "start": ..., "end": ...})` with no `role` raises a Pydantic `ValidationError` — the handler defaults `role` to `"default"`, which is not in `TextClip`'s role Literal (`super|hook|lower_third|caption|label|watermark`; the field is `... | None`). The smoke test masks this by always passing `role="super"`. Exposing the tool to Claude without fixing it would make the most common call shape ("add text saying X") fail.

- [ ] **Step 1: Write the failing regression test**

Append to `tests/test_text_tools.py` (reuse that file's existing `EDLStore`/`dispatch` imports):

```python
def test_add_text_without_role(tmp_path):
    store = EDLStore(tmp_path)
    r = dispatch(store, "add_text", {"text": "HELLO", "start": 0.0, "end": 1.0})
    assert "id" in r
    # legacy callers may still send the docstring's old 'default' value
    r2 = dispatch(store, "add_text", {"text": "X", "start": 0.0, "end": 1.0,
                                      "role": "default"})
    assert "id" in r2
```

- [ ] **Step 2: Run — expect FAIL with ValidationError**

Run: `uv run pytest tests/test_text_tools.py::test_add_text_without_role -v`
Expected: FAIL — `pydantic ValidationError: Input should be 'super', 'hook', 'lower_third', 'caption', 'label' or 'watermark'`.

- [ ] **Step 3: Fix the handler default**

In `add_text` in `src/video_ai_editor/agent/dispatch.py`, replace `role = args.get("role", "default")` with:

```python
    role = args.get("role")
    if role in (None, "", "default"):  # 'default' accepted for legacy callers; not a valid Literal
        role = None
```

and in its docstring change "(super, hook, lower_third, caption, label, watermark, default)" to "(super, hook, lower_third, caption, label, watermark; omit for plain positioned text)".

- [ ] **Step 4: Run — expect PASS**

Run: `uv run pytest tests/test_text_tools.py -v`
Expected: all PASS.

- [ ] **Step 5: Append to `TEXT_TOOLS`** (match the category string used by existing entries in that list — check the third argument of the first `_t(` inside `TEXT_TOOLS`; use it verbatim below where `"text"` is written)

```python
    _t("add_text", "Full-control text overlay (vs add_super_text which uses canonical "
       "role defaults). x/y are canvas pixels (centre). anim_in/anim_out animate entrance/exit.",
       "text",
       {"text": {"type": "string"},
        "start": {"type": "number"}, "end": {"type": "number"},
        "role": {"type": "string",
                 "enum": ["super", "hook", "lower_third", "caption", "label", "watermark"],
                 "description": "Omit for plain positioned text"},
        "x": {"type": "number"}, "y": {"type": "number"},
        "scale": {"type": "number"}, "rotation": {"type": "number"},
        "opacity": {"type": "number"},
        "font": {"type": "string"}, "size": {"type": "number"},
        "color": {"type": "string", "description": "Hex, e.g. #FFFFFF"},
        "stroke": {"type": "string"}, "stroke_w": {"type": "number"},
        "anim_in": {"type": "string", "enum": ["pop", "fade", "slide_up", "slide_down"]},
        "anim_out": {"type": "string", "enum": ["pop", "fade", "slide_up", "slide_down"]}},
       ["text", "start", "end"]),
    _t("add_sticker", "Add a sticker overlay to the stickers track — either a PNG file "
       "(src) or an emoji character (emoji). position is [x, y] canvas pixels.",
       "text",
       {"src": {"type": "string", "description": "Path to a PNG (omit if using emoji)"},
        "emoji": {"type": "string", "description": "Emoji character (omit if using src)"},
        "start": {"type": "number"}, "end": {"type": "number"},
        "position": {"type": "array", "items": {"type": "number"},
                     "description": "[x, y] canvas pixels"},
        "scale": {"type": "number"}, "rotation": {"type": "number"}},
       ["start", "end"]),
    _t("apply_text_template", "Render a text overlay from a named preset bundle. fields "
       "fills the {handle}/{hashtag}/{text} slots.",
       "text",
       {"name": {"type": "string",
                 "enum": ["hashtag_chunky", "callout_arrow", "big_question",
                          "end_card_handle", "countdown_3_2_1", "watermark_handle"]},
        "fields": {"type": "object",
                   "description": "e.g. {\"handle\": \"@me\", \"hashtag\": \"#tips\", \"text\": \"...\"}"},
        "start": {"type": "number"}, "end": {"type": "number"}},
       ["name"]),
    _t("list_text_styles", "List built-in text/role presets plus user styles from "
       "presets/text_styles/.",
       "text", {}, []),
```

- [ ] **Step 6: Delete `"add_text"`, `"add_sticker"`, `"apply_text_template"`, `"list_text_styles"` from `NOT_YET_EXPOSED`**

- [ ] **Step 7: Run — expect PASS, count 60**

Run: `uv run pytest tests/test_tool_registry.py -v` → PASS; count one-liner → `60`.

- [ ] **Step 8: Commit**

```bash
git add src/video_ai_editor/agent/tools.py src/video_ai_editor/agent/dispatch.py tests/test_text_tools.py tests/test_tool_registry.py
git commit -m "feat(agent): expose text/sticker/template tools to chat LLM; fix add_text default-role crash"
```

### Task 4: Expose keyframe, marker & motion-tracking tools (5)

Handler args: `add_keyframe(clip_id, prop, time, value, interp)`, `remove_keyframe(clip_id, prop, time)`, `add_marker(time, label, color)`, `remove_marker(marker_id)`, `motion_track(clip_id, bbox, target_id, method, sample_every)`.

**Files:**
- Modify: `src/video_ai_editor/agent/tools.py` (new list + extend the `ALL_TOOLS` sum)
- Modify: `tests/test_tool_registry.py`

- [ ] **Step 1: `time` semantics — verified, no investigation needed**

`add_keyframe`'s docstring states `time` is **clip-local seconds ("0 = clip start")**, and the handler seeds a scalar property as `[(0.0, current)]`. The schema below already says so. (This fact also drives Task 12's `start_offset` handling.)

- [ ] **Step 2: Add a new `ANIMATION_TOOLS` list** after the `TEXT_TOOLS` list in tools.py:

```python
ANIMATION_TOOLS = [
    _t("add_keyframe", "Add (or update) a keyframe on a clip/text/sticker transform "
       "property. Time is clip-local seconds. interp sets the easing for the whole "
       "property's curve.",
       "animation",
       {"clip_id": {"type": "string"},
        "prop": {"type": "string", "enum": ["x", "y", "scale", "rotation", "opacity"]},
        "time": {"type": "number"},
        "value": {"type": "number"},
        "interp": {"type": "string",
                   "enum": ["linear", "ease-in", "ease-out", "ease-in-out",
                            "step", "back-out", "bounce"]}},
       ["clip_id", "prop", "time", "value"]),
    _t("remove_keyframe", "Remove the keyframe closest to `time` for a property.",
       "animation",
       {"clip_id": {"type": "string"},
        "prop": {"type": "string", "enum": ["x", "y", "scale", "rotation", "opacity"]},
        "time": {"type": "number"}},
       ["clip_id", "prop", "time"]),
    _t("add_marker", "Add a timeline marker (label + color) at a time.",
       "animation",
       {"time": {"type": "number"},
        "label": {"type": "string"}, "color": {"type": "string"}},
       ["time"]),
    _t("remove_marker", "Remove a timeline marker by id.",
       "animation",
       {"marker_id": {"type": "string"}}, ["marker_id"]),
    _t("motion_track", "Track a bounding box through a video clip and bake the path "
       "into x/y keyframes on a target overlay (text or sticker follows the object).",
       "animation",
       {"clip_id": {"type": "string", "description": "Video clip to analyse"},
        "bbox": {"type": "array", "items": {"type": "number"},
                 "description": "[x, y, w, h] in canvas pixels at the first frame"},
        "target_id": {"type": "string", "description": "Text/sticker clip to animate"},
        "method": {"type": "string"},
        "sample_every": {"type": "number"}},
       ["clip_id", "bbox", "target_id"]),
]
```

- [ ] **Step 3: Add `ANIMATION_TOOLS` to the `ALL_TOOLS` sum** (tools.py bottom):

```python
ALL_TOOLS: list[ToolSchema] = (
    INSPECTION_TOOLS + EDIT_TOOLS + PROJECT_TOOLS + TEXT_TOOLS
    + ANIMATION_TOOLS
    + AUDIO_TOOLS + SHOW_TOOLS + EFFECT_TOOLS + VISION_TOOLS + TTS_TOOLS
    + HEAVY_AI_TOOLS
)
```

- [ ] **Step 4: Delete the 5 names from `NOT_YET_EXPOSED`**; run `uv run pytest tests/test_tool_registry.py -v` → PASS; count → `65`.

- [ ] **Step 5: Commit**

```bash
git add src/video_ai_editor/agent/tools.py tests/test_tool_registry.py
git commit -m "feat(agent): expose keyframe/marker/motion-track tools to chat LLM"
```

### Task 5: Expose effect & keying tools (4)

Handler args: `chroma_key(clip_id, color, similarity, smoothness, spill_suppress)`, `list_filters()`, `list_luts()`, `list_transitions()`.

**Files:**
- Modify: `src/video_ai_editor/agent/tools.py` (append inside `EFFECT_TOOLS`)
- Modify: `tests/test_tool_registry.py`

- [ ] **Step 1: Append to `EFFECT_TOOLS`** (match that list's existing category string):

```python
    _t("chroma_key", "Green-screen keying: set (or clear) chroma key on a clip. "
       "Pass color=null to clear. similarity/smoothness/spill_suppress are 0-1.",
       "effect",
       {"clip_id": {"type": "string"},
        "color": {"type": ["string", "null"], "description": "Hex key color, e.g. #00FF00; null clears"},
        "similarity": {"type": "number"}, "smoothness": {"type": "number"},
        "spill_suppress": {"type": "number"}},
       ["clip_id"]),
    _t("list_filters", "List every effect type add_effect understands.",
       "effect", {}, []),
    _t("list_luts", "List all .cube LUTs available to apply_lut.",
       "effect", {}, []),
    _t("list_transitions", "The full transition catalog with categories, aliases and "
       "descriptions — consult before add_transition.",
       "effect", {}, []),
```

- [ ] **Step 2: Delete the 4 names from `NOT_YET_EXPOSED`**; run `uv run pytest tests/test_tool_registry.py -v` → PASS; count → `69`.

- [ ] **Step 3: Commit**

```bash
git add src/video_ai_editor/agent/tools.py tests/test_tool_registry.py
git commit -m "feat(agent): expose chroma-key and effect catalog tools to chat LLM"
```

### Task 6: Expose audio & speaker tools (4)

Handler args: `noise_reduce(clip_id, strength)`, `set_loudness_target(lufs)`, `diarize(num_speakers, fallback)`, `name_speakers(mapping)`.

**Files:**
- Modify: `src/video_ai_editor/agent/tools.py` (append inside `AUDIO_TOOLS`)
- Modify: `tests/test_tool_registry.py`

- [ ] **Step 1: Append to `AUDIO_TOOLS`** (match that list's category string):

```python
    _t("noise_reduce", "Spectrally denoise a clip's audio (hiss/fans/room tone). "
       "Replaces the clip's src with a cleaned file; video stream is copied.",
       "audio",
       {"clip_id": {"type": "string"},
        "strength": {"type": "number", "description": "0-1, default moderate"}},
       ["clip_id"]),
    _t("set_loudness_target", "Set the export speech-loudness target in LUFS. "
       "Reels/TikTok = -16, YouTube = -14, broadcast = -23. Pass null to disable loudnorm.",
       "audio",
       {"lufs": {"type": ["number", "null"]}},
       ["lufs"]),
    _t("diarize", "Speaker diarization on the main video's audio. Uses pyannote when a "
       "HuggingFace token is configured, else a local MFCC heuristic. Read-only; returns "
       "speaker segments for lower-thirds or per-speaker edits.",
       "audio",
       {"num_speakers": {"type": "integer"},
        "fallback": {"type": "boolean", "description": "Force the token-free heuristic"}},
       []),
    _t("name_speakers", "Save a speaker→display-name mapping (used by lower-thirds).",
       "audio",
       {"mapping": {"type": "object",
                    "description": "e.g. {\"SPEAKER_00\": \"Priya\", \"SPEAKER_01\": \"Arjun\"}"}},
       ["mapping"]),
```

- [ ] **Step 2: Delete the 4 names from `NOT_YET_EXPOSED`**; run `uv run pytest tests/test_tool_registry.py -v` → PASS; count → `73`.

- [ ] **Step 3: Commit**

```bash
git add src/video_ai_editor/agent/tools.py tests/test_tool_registry.py
git commit -m "feat(agent): expose audio cleanup and diarization tools to chat LLM"
```

### Task 7: Expose caption/subtitle I/O tools (5)

Handler args: `import_srt(path, language)`, `export_srt(path)`, `export_vtt(path)`, `export_ass(path)`, `translate_captions(target_lang, source_lang)` (alias `to` also accepted).

**Files:**
- Modify: `src/video_ai_editor/agent/tools.py` (new `CAPTION_IO_TOOLS` list + sum)
- Modify: `tests/test_tool_registry.py`

- [ ] **Step 1: Add a new `CAPTION_IO_TOOLS` list** after `AUDIO_TOOLS`:

```python
CAPTION_IO_TOOLS = [
    _t("import_srt", "Replace the project transcript with one parsed from an external "
       ".srt / .vtt / .ass file (e.g. user-provided professional subtitles).",
       "captions",
       {"path": {"type": "string"}, "language": {"type": "string"}},
       ["path"]),
    _t("export_srt", "Write the current transcript as .srt. Default destination is "
       "<session>/captions.srt.",
       "captions", {"path": {"type": "string"}}, []),
    _t("export_vtt", "Write the current transcript as WebVTT (.vtt).",
       "captions", {"path": {"type": "string"}}, []),
    _t("export_ass", "Write the current transcript as .ass (Advanced SubStation).",
       "captions", {"path": {"type": "string"}}, []),
    _t("translate_captions", "Translate the captions track in place via local Argos "
       "Translate (no cloud). target_lang is an ISO code like 'hi' or 'en'.",
       "captions",
       {"target_lang": {"type": "string"}, "source_lang": {"type": "string"}},
       ["target_lang"]),
]
```

- [ ] **Step 2: Add `+ CAPTION_IO_TOOLS` to the `ALL_TOOLS` sum** (after `+ AUDIO_TOOLS`).

- [ ] **Step 3: Delete the 5 names from `NOT_YET_EXPOSED`**; run `uv run pytest tests/test_tool_registry.py -v` → PASS; count → `78`.

- [ ] **Step 4: Commit**

```bash
git add src/video_ai_editor/agent/tools.py tests/test_tool_registry.py
git commit -m "feat(agent): expose subtitle import/export and translation tools to chat LLM"
```

### Task 8: Expose heavy-AI visual & show tools (10)

Handler args: `remove_background(clip_id, bg_color, model)`, `object_erase(clip_id, bbox, t_start, t_end)`, `stabilize(clip_id, ...)`, `smooth_slow_motion(clip_id, factor)`, `make_shorts(target_count, min_dur, max_dur, save_as_sessions)`, `multicam(srcs, window_s, total, replace_v1)`, `find_broll(query, bin, top_k, max_duration)`, `apply_export_preset(name)`, `apply_hook_stack(text, visual, audio, duration)`, `list_shows()`.

**Files:**
- Modify: `src/video_ai_editor/agent/tools.py` (new `AI_VISUAL_TOOLS` list + sum; show tools appended to `SHOW_TOOLS`)
- Modify: `tests/test_tool_registry.py`

- [ ] **Step 1: Confirm stabilize's optional args** (its docstring was truncated during planning):

Run: `sed -n "$(grep -n 'def stabilize' src/video_ai_editor/agent/dispatch.py | cut -d: -f1),+25p" src/video_ai_editor/agent/dispatch.py`
Add any optional `args.get(...)` names it accepts (e.g. a strength/smoothing knob) as optional number properties in the schema below.

- [ ] **Step 2: Add a new `AI_VISUAL_TOOLS` list** after `HEAVY_AI_TOOLS`:

```python
AI_VISUAL_TOOLS = [
    _t("remove_background", "Strip a clip's background via rembg (local model). Replaces "
       "the clip's src with the matted version, flattened onto green by default so a "
       "follow-up chroma_key finishes the composite.",
       "heavy_ai",
       {"clip_id": {"type": "string"},
        "bg_color": {"type": "string", "description": "Flatten color, default green"},
        "model": {"type": "string"}},
       ["clip_id"]),
    _t("object_erase", "LaMa inpainting: erase a bounding-box region across a time "
       "window of a clip (logos, passers-by, mic booms).",
       "heavy_ai",
       {"clip_id": {"type": "string"},
        "bbox": {"type": "array", "items": {"type": "number"},
                 "description": "[x, y, w, h] canvas pixels"},
        "t_start": {"type": "number"}, "t_end": {"type": "number"}},
       ["clip_id", "bbox"]),
    _t("stabilize", "Two-pass vidstab stabilization on a shaky clip. Slow (re-encodes "
       "the clip).",
       "heavy_ai",
       {"clip_id": {"type": "string"}},
       ["clip_id"]),
    _t("smooth_slow_motion", "RIFE frame interpolation for factor-times smooth slow "
       "motion on a clip (use instead of set_speed for slow-mo below ~0.5x).",
       "heavy_ai",
       {"clip_id": {"type": "string"}, "factor": {"type": "number"}},
       ["clip_id", "factor"]),
    _t("make_shorts", "Heuristically pick N highlight ranges from the main track and "
       "optionally save each as its own session (one short per session).",
       "heavy_ai",
       {"target_count": {"type": "integer"},
        "min_dur": {"type": "number"}, "max_dur": {"type": "number"},
        "save_as_sessions": {"type": "boolean"}},
       []),
    _t("multicam", "Multi-cam switcher: sync N takes by audio, pick the best take per "
       "window, and rewrite the main track as the resulting cut list.",
       "heavy_ai",
       {"srcs": {"type": "array", "items": {"type": "string"},
                 "description": "Paths of the synced camera takes"},
        "window_s": {"type": "number"}, "total": {"type": "number"},
        "replace_v1": {"type": "boolean"}},
       ["srcs"]),
    _t("find_broll", "Search a local b-roll folder for clips matching a text query; "
       "returns ranked candidates you can then add_clip from.",
       "heavy_ai",
       {"query": {"type": "string"},
        "bin": {"type": "string", "description": "B-roll folder path"},
        "top_k": {"type": "integer"}, "max_duration": {"type": "number"}},
       ["query"]),
]
```

- [ ] **Step 3: Append the three show/export tools to `SHOW_TOOLS`** (match its category string):

```python
    _t("apply_export_preset", "Set canvas + bitrate + loudness from a named platform "
       "preset (e.g. reels, tiktok, youtube). Check list_templates/list_shows for names.",
       "show",
       {"name": {"type": "string"}}, ["name"]),
    _t("apply_hook_stack", "Bake all three hook axes (text overlay + visual punch-in + "
       "audio sting) onto the first ~3 seconds. text defaults to a generated hook.",
       "show",
       {"text": {"type": "string"}, "visual": {"type": "string"},
        "audio": {"type": "string"}, "duration": {"type": "number"}},
       []),
    _t("list_shows", "List saved show templates (canvas + brand + captions + music "
       "look), reusable via apply_show_template.",
       "show", {}, []),
```

- [ ] **Step 4: Add `+ AI_VISUAL_TOOLS` to the `ALL_TOOLS` sum** (after `+ HEAVY_AI_TOOLS`).

- [ ] **Step 5: Delete the 10 names from `NOT_YET_EXPOSED`**; run `uv run pytest tests/test_tool_registry.py -v` → PASS; count → `88`.

- [ ] **Step 6: Commit**

```bash
git add src/video_ai_editor/agent/tools.py tests/test_tool_registry.py
git commit -m "feat(agent): expose heavy-AI visual, multicam, shorts and show tools to chat LLM"
```

### Task 9: Finalize parity — delete the crutch, refresh the system prompt

**Files:**
- Modify: `tests/test_tool_registry.py`
- Modify: `src/video_ai_editor/agent/system_prompt.py`

- [ ] **Step 1: Delete `NOT_YET_EXPOSED` entirely** from `tests/test_tool_registry.py` — remove the set definition and simplify the final test:

```python
def test_dispatch_coverage_is_explicit():
    """Every DISPATCH tool is either advertised to the LLM or explicitly internal."""
    advertised = {t["name"] for t in ALL_TOOLS}
    unexposed = set(DISPATCH) - advertised
    assert unexposed == INTERNAL_ONLY, (
        f"unexpected unexposed tools: {sorted(unexposed - INTERNAL_ONLY)}; "
        f"stale INTERNAL_ONLY entries: {sorted(INTERNAL_ONLY - unexposed)}"
    )
```

- [ ] **Step 2: Run — expect PASS**

Run: `uv run pytest tests/test_tool_registry.py -v` → all PASS. Count one-liner → `88`.

- [ ] **Step 3: Update the system prompt's tool-surface note**

In `src/video_ai_editor/agent/system_prompt.py`, the "Tool surface" section currently reads: *"The tools available are listed in this conversation. Call them by name and JSON args. Do not invent tools."* Extend it to steer the LLM through the enlarged surface. Replace that sentence with:

```
The tools available are listed in this conversation. Call them by name and JSON args. Do not invent tools.
You have the full editor: timeline edits, speed/slow-mo, text/stickers with entrance animations, keyframe animation, green-screen (chroma_key), background removal, object erase, stabilization, subtitles import/export + translation, diarization, multicam, and shorts generation. Prefer list_* tools (list_filters, list_luts, list_transitions, list_text_styles, list_shows) to discover valid names before applying effects/LUTs/transitions/templates. Heavy AI tools (remove_background, object_erase, smooth_slow_motion, stabilize, multicam) can take minutes — mention that to the user before invoking them.
```

- [ ] **Step 4: Sanity-run the chat projection** (schemas must serialize cleanly):

```bash
python3 -c "
import sys, json; sys.path.insert(0,'src')
from video_ai_editor.agent.tools import ALL_TOOLS
proj = [{'name': t['name'], 'description': t['description'], 'input_schema': t['input_schema']} for t in ALL_TOOLS]
print(len(proj), len(json.dumps(proj)))
"
```
Expected: `88` and a byte size printed (roughly 25-35 KB ≈ 7-9k tokens/turn; acceptable — note it in the CLAUDE.md task).

- [ ] **Step 5: Commit**

```bash
git add tests/test_tool_registry.py src/video_ai_editor/agent/system_prompt.py
git commit -m "feat(agent): full tool parity for chat LLM (88 advertised, 4 internal) + system prompt refresh"
```

---

# Phase 3 — Keyframe easing export parity

### Task 10: Implement ease curves in `to_ffmpeg_expr`

Today `to_ffmpeg_expr` (edl/keyframes.py:54) emits linear segments regardless of `interp`, while `sample()` (same file, the Python oracle used everywhere else) and the browser (`frontend/src/lib/overlay.ts:37-39`) implement `ease-in` (f²), `ease-out` (1-(1-f)²), `ease-in-out` (3f²-2f³), `back-out` (1-(1-f)³), `step` (hold v0). `bounce` falls through to linear in `sample()` — keep that parity. (The mini-evaluator approach below is pre-validated: run against the live linear expression, it reproduces `sample()` exactly, and the emitted expressions use only `if`/`lt` — no other functions.)

**Files:**
- Create: `tests/test_keyframes_easing.py`
- Modify: `src/video_ai_editor/edl/keyframes.py:54-88`

- [ ] **Step 1: Write the failing parity test**

Create `tests/test_keyframes_easing.py`:

```python
"""to_ffmpeg_expr must numerically match sample() for every interp mode."""
import pytest

from video_ai_editor.edl.keyframes import sample, to_ffmpeg_expr

INTERPS = ["linear", "step", "ease-in", "ease-out", "ease-in-out", "back-out", "bounce"]


def _eval_ffmpeg_expr(expr: str, t: float) -> float:
    """Tiny evaluator for the expression subset to_ffmpeg_expr emits:
    numbers, + - * / parens, if(cond, a, b), lt(a, b), gte(a, b), variable t.
    ffmpeg-eval's if() is lazy but ours is eager — fine, no side effects."""
    py = expr.replace("\\,", ",").replace("if(", "if_(")
    env = {
        "if_": lambda c, a, b: a if c else b,
        "lt": lambda a, b: 1.0 if a < b else 0.0,
        "gte": lambda a, b: 1.0 if a >= b else 0.0,
        "t": t,
    }
    return float(eval(py, {"__builtins__": {}}, env))  # noqa: S307 — test-only, known input


@pytest.mark.parametrize("interp", INTERPS)
def test_expr_matches_sample_two_keyframes(interp):
    kf = {"keyframes": [[1.0, 10.0], [3.0, 110.0]], "interp": interp}
    expr = to_ffmpeg_expr(kf)
    for t in [0.0, 0.5, 1.0, 1.2, 1.7, 2.0, 2.4, 2.9, 3.0, 3.5, 10.0]:
        assert abs(_eval_ffmpeg_expr(expr, t) - sample(kf, t)) < 1e-3, (interp, t)


@pytest.mark.parametrize("interp", INTERPS)
def test_expr_matches_sample_three_keyframes(interp):
    kf = {"keyframes": [[0.0, 0.0], [1.0, 50.0], [4.0, -20.0]], "interp": interp}
    expr = to_ffmpeg_expr(kf)
    for t in [0.0, 0.25, 0.5, 0.99, 1.0, 1.5, 2.5, 3.9, 4.0, 5.0]:
        assert abs(_eval_ffmpeg_expr(expr, t) - sample(kf, t)) < 1e-3, (interp, t)


def test_start_offset_still_shifts():
    kf = {"keyframes": [[2.0, 0.0], [4.0, 100.0]], "interp": "ease-in"}
    expr = to_ffmpeg_expr(kf, start_offset=2.0)
    # with the 2s shift, t=1.0 is halfway: ease-in f=0.5 -> 0.25 -> value 25
    assert abs(_eval_ffmpeg_expr(expr, 1.0) - 25.0) < 1e-3
```

- [ ] **Step 2: Run — expect ease/step cases to FAIL**

Run: `uv run pytest tests/test_keyframes_easing.py -v`
Expected: `linear` and `bounce` cases PASS (both are linear today); `step`, `ease-in`, `ease-out`, `ease-in-out`, `back-out` cases FAIL with numeric mismatches.

- [ ] **Step 3: Implement easing in `to_ffmpeg_expr`**

In `src/video_ai_editor/edl/keyframes.py`, add a module-level helper above `to_ffmpeg_expr`:

```python
def _eased_frac(F: str, interp: str) -> str:
    """Eased progress g(F) as an ffmpeg-eval expression. Mirrors the curves in
    sample() above and frontend/src/lib/overlay.ts so export == preview.
    F is a parenthesised subexpression in [0, 1]."""
    if interp == "ease-in":
        return f"({F}*{F})"
    if interp == "ease-out":
        return f"(1-(1-{F})*(1-{F}))"
    if interp == "ease-in-out":
        return f"({F}*{F}*(3-2*{F}))"
    if interp == "back-out":
        return f"(1-(1-{F})*(1-{F})*(1-{F}))"
    return F  # linear; bounce intentionally matches sample() (linear fallthrough)
```

Then, inside `to_ffmpeg_expr`: (a) read `interp` the same way `sample()` does, right where `kfs` is extracted:

```python
    interp = (value.get("interp", "linear") if isinstance(value, dict)
              else getattr(value, "interp", "linear"))
```

(b) replace the segment construction. The existing loop body builds:

```python
        # linear: v0 + (t-t0)/(t1-t0) * (v1-v0)
        seg = (
            f"({v0:.4f}+({time_var}-{t0:.4f})/({t1 - t0:.6f})*({v1 - v0:.4f}))"
        )
```

Replace those lines with:

```python
        if interp == "step":
            seg = f"{v0:.4f}"  # hold v0 until the next keyframe
        else:
            frac = f"(({time_var}-{t0:.4f})/{t1 - t0:.6f})"
            seg = f"({v0:.4f}+({v1 - v0:.4f})*{_eased_frac(frac, interp)})"
```

(c) update the docstring: delete the sentence "Linear-only for now; ease-* approximated with linear (renderer doesn't implement curves yet — they only animate in the browser preview)." and replace with "Implements the same easing curves as `sample()` (linear, step, ease-in/out/in-out, back-out; bounce falls back to linear)."

- [ ] **Step 4: Run — expect PASS, plus no regressions in expression consumers**

Run: `uv run pytest tests/test_keyframes_easing.py tests/test_render_overlay.py tests/test_render_smoke.py tests/test_render_correctness.py -v`
Expected: all PASS (render tests without keyframed easing produce identical linear expressions).

- [ ] **Step 5: Commit**

```bash
git add tests/test_keyframes_easing.py src/video_ai_editor/edl/keyframes.py
git commit -m "feat(render): export keyframe easing curves (was linear-only) with sample() parity"
```

---

# Phase 4 — Text animation export

### Task 11: Add a render salt to the preview cache key

Render output is cached purely by `edl.hash()` — after Tasks 10/12 change what the renderer produces for the *same* EDL, cached previews from older code would be served stale. Fold a renderer version into the cache key.

**Files:**
- Modify: located in Step 1 (the preview/export output-naming site in `render/`)

- [ ] **Step 1: Confirm the (verified) cache-key sites**

Verified as of writing — re-confirm line drift with:

```bash
grep -rn "hash()" src/video_ai_editor/render/ src/video_ai_editor/main.py | grep -v edl_hash
```

Three sites matter:
1. `render/compositor.py` ~654-661 (preview): `dst = out_dir / f"{h}.mp4"` and the inflight key `key = f"{session_dir.name}/{h}"`.
2. `src/video_ai_editor/main.py:615-616` (the preview GET endpoint): `p = store.dir / "previews" / f"{target_hash}.mp4"` — **this must stay in lockstep with the compositor filename or the endpoint 404s on every fresh preview.**
3. `render/compositor.py` ~805 (export): `name = filename or f"export_{h}.mp4"` — exports re-render unconditionally (no cached early-return), so no salt needed; skip unless you find a cache check nearby.

Also check `render/chunks.py`: if the chunk fingerprint has no renderer-version token, it needs the salt too.

- [ ] **Step 2: Add the salt**

In `render/compositor.py`, near the `_INFLIGHT` globals (~line 167):

```python
# Bump when the compiler changes what the same EDL renders to (easing curves,
# anim_in/out export, ...). Folded into cache keys so stale outputs are not served.
RENDER_SALT = "r2"
```

Then:
- compositor preview: `dst = out_dir / f"{h}_{RENDER_SALT}.mp4"` and `key = f"{session_dir.name}/{h}_{RENDER_SALT}"`
- `main.py`: add `RENDER_SALT` to the existing import from the render package (check how main.py imports render symbols — match that style) and change line 616 to `p = store.dir / "previews" / f"{target_hash}_{RENDER_SALT}.mp4"`
- `render/chunks.py`: append `RENDER_SALT` to the fingerprint input string if no version token exists.

- [ ] **Step 3: Run render + preview-endpoint tests**

Run: `uv run pytest tests/test_render_smoke.py tests/test_chunk_cache_recovery.py tests/test_chunk_parallel.py tests/test_audio_import_preview.py tests/test_api_e2e.py -v`
Expected: PASS (fresh tmp dirs — the salt only changes filenames; the endpoint test is the guard for the main.py/compositor lockstep).

- [ ] **Step 4: Commit**

```bash
git add src/video_ai_editor/render/ src/video_ai_editor/main.py
git commit -m "fix(render): salt render cache keys with renderer version to avoid stale previews"
```

### Task 12: Export text `anim_in`/`anim_out` (and fix the overlay timebase bug)

Verified: `anim_in`/`anim_out` are stored in the EDL (and set by built-ins like the `countdown_3_2_1` text template) but rendered **nowhere** — the frontend has zero references to them and export drops them. This task makes them real at export (a follow-up frontend task can later mirror the same envelopes in the browser preview). Also fix a latent bug this work exposes: animated overlay PNG streams are bounded with `-t (end-start)+0.5`, but their `T` clock starts at output t=0 — so for any overlay starting later than ~0s, `T` freezes before the overlay is even shown, and keyframed-opacity export evaluates a frozen `T`. Fix: bound with `-t end+0.5` so `T` tracks the output timeline for the whole display window.

Design (all inside `render/text_overlay.py::build_overlay_chain`):
- A text clip with `anim_in`, `anim_out`, **or** keyframed opacity is classified `anim_text` (today: keyframed opacity only).
- Alpha envelope, multiplied into the existing geq alpha expression, in output-timeline time `T`:
  - in-anim (`fade`/`slide_up`/`slide_down`: d=0.3s; `pop`: d=0.12s): `clip((T-start)/d,0,1)`
  - out-anim: `clip((end-T)/d,0,1)`
- `slide_up`/`slide_down` additionally animate the overlay `y` (the PNG is canvas-sized, overlaid at (0,0); sliding the whole layer by ±3% of output height): via the overlay filter's `t` (main-stream time).
- `pop` is exported as a fast fade (0.12s) — scale animation needs per-frame rescale ffmpeg can't do cheaply; the approximation is documented in the code.

**Files:**
- Create: `tests/test_text_anim_export.py`
- Modify: `src/video_ai_editor/render/text_overlay.py` (classification ~line 358-371, input bounding ~line 387-398, geq/overlay emission ~line 404-420)

- [ ] **Step 1: Write the failing render test**

Create `tests/test_text_anim_export.py`:

```python
"""anim_in/anim_out on text must be visible in the EXPORTED pixels."""
import io
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.platformutil import FFMPEG
from video_ai_editor.render import render_export


def _lavfi_clip(path: Path, dur: float = 6.0) -> None:
    subprocess.run(
        [FFMPEG, "-y",
         "-f", "lavfi", "-i", f"color=c=black:s=270x480:d={dur}:r=30",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
         "-t", str(dur), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         str(path)],
        check=True, capture_output=True)


def _center_brightness(video: Path, t: float) -> float:
    png = subprocess.run(
        [FFMPEG, "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
         "-f", "image2pipe", "-vcodec", "png", "-"],
        check=True, capture_output=True).stdout
    img = Image.open(io.BytesIO(png)).convert("L")
    w, h = img.size
    box = img.crop((int(w * 0.1), int(h * 0.35), int(w * 0.9), int(h * 0.65)))
    data = list(box.getdata())
    return sum(data) / len(data)


def _store_with_text(tmp_path: Path, **text_args) -> tuple[EDLStore, str]:
    sd = tmp_path / "session"
    sd.mkdir()
    src = sd / "src.mp4"
    _lavfi_clip(src)
    store = EDLStore(sd)
    dispatch(store, "add_clip", {"track": "v1", "src": str(src),
                                 "in": 0.0, "out": 6.0, "start": 0.0})
    r = dispatch(store, "add_text", {
        "text": "HELLO WORLD", "start": 2.0, "end": 5.0, "role": "hook",
        "x": 540, "y": 960, "size": 200, **text_args,
    })
    return store, r["id"]


def test_fade_in_out_exports(tmp_path):
    store, _tid = _store_with_text(tmp_path, anim_in="fade", anim_out="fade")
    out = render_export(store.edl, tmp_path / "session", height=480)
    base = _center_brightness(out.path, 1.0)          # before text: black
    early = _center_brightness(out.path, 2.06)        # ~20% through 0.3s fade-in
    full = _center_brightness(out.path, 3.5)          # steady state: full text
    late = _center_brightness(out.path, 4.94)         # ~80% through fade-out
    assert full > base + 20, "text never appeared in export"
    assert early < (base + full) / 2, f"fade-in missing: early={early} full={full}"
    assert late < (base + full) / 2, f"fade-out missing: late={late} full={full}"


def test_no_anim_text_is_fully_visible_immediately(tmp_path):
    store, _tid = _store_with_text(tmp_path)  # no anim_in/anim_out — static path
    out = render_export(store.edl, tmp_path / "session", height=480)
    at_start = _center_brightness(out.path, 2.06)
    steady = _center_brightness(out.path, 3.5)
    assert abs(at_start - steady) < 10, "static text must not fade"


def test_keyframed_opacity_respects_late_start(tmp_path):
    """Regression for the frozen-T/unshifted-time bug: keyframed opacity on a
    text that starts at t=2 must ramp correctly. Keyframe times are CLIP-LOCAL
    (verified: add_keyframe docstring, '0 = clip start')."""
    store, tid = _store_with_text(tmp_path)
    dispatch(store, "add_keyframe", {"clip_id": tid, "prop": "opacity",
                                     "time": 0.0, "value": 0.0})
    dispatch(store, "add_keyframe", {"clip_id": tid, "prop": "opacity",
                                     "time": 3.0, "value": 1.0})
    out = render_export(store.edl, tmp_path / "session", height=480)
    base = _center_brightness(out.path, 1.0)          # before text: black
    early = _center_brightness(out.path, 2.2)         # clip-local 0.2 → alpha ≈ 0.07
    late = _center_brightness(out.path, 4.8)          # clip-local 2.8 → alpha ≈ 0.93
    # Buggy behavior evaluates kf times against unshifted timeline T, making
    # `early` visibly bright (alpha ≈ 0.73) — the base comparison discriminates.
    assert early < base + 12, f"ramp should start invisible: early={early} base={base}"
    assert late > early + 15, f"opacity ramp missing in export: early={early} late={late}"
```

- [ ] **Step 2: Run — expect fade + keyframe-shift tests to FAIL**

Run: `uv run pytest tests/test_text_anim_export.py -v`
Expected: `test_no_anim_text_is_fully_visible_immediately` PASSES (static path already correct); the other two FAIL (`test_fade_in_out_exports`: no fade in export; `test_keyframed_opacity_respects_late_start`: `early` is bright because clip-local keyframe times are evaluated against unshifted timeline `T`). If `test_fade_in_out_exports` fails at the `full > base + 20` assert instead, the fixture is wrong — fix the fixture before touching the renderer.

- [ ] **Step 3: Implement in `render/text_overlay.py`**

Three edits inside `build_overlay_chain` (plus one tiny helper above it):

**(a) Helper** — add above `build_overlay_chain`:

```python
_ANIM_KINDS = {"fade", "pop", "slide_up", "slide_down"}


def _anim_alpha_terms(tc: "TextClip") -> list[str]:
    """Alpha-envelope factors (ffmpeg-eval, variable T = output-timeline seconds)
    for anim_in/anim_out. pop exports as a fast fade — per-frame scale animation
    is not economical in a single filtergraph; the browser previews the real pop."""
    terms: list[str] = []
    if tc.anim_in in _ANIM_KINDS:
        d = 0.12 if tc.anim_in == "pop" else 0.3
        terms.append(f"clip((T-{tc.start:.3f})/{d}\\,0\\,1)")
    if tc.anim_out in _ANIM_KINDS:
        d = 0.12 if tc.anim_out == "pop" else 0.3
        terms.append(f"clip(({tc.end:.3f}-T)/{d}\\,0\\,1)")
    return terms
```

**(b) Classification** (currently `is_keyframed(opa)` alone routes to `anim_text`) — extend:

```python
        needs_time = (
            is_keyframed(opa)
            or getattr(c, "anim_in", None) in _ANIM_KINDS
            or getattr(c, "anim_out", None) in _ANIM_KINDS
        )
        if needs_time:
            items.append({"kind": "anim_text", "text_clip": c, "png": png})
```

**(c) Input bounding — the frozen-T fix.** In the `anim_text` (and animated-sticker) input branches, the bound is currently `dur = max(0.5, tc.end - tc.start) + 0.5`. The looped PNG stream's `T` starts at output t=0, so bound it to the **end** of the display window instead:

```python
            dur = tc.end + 0.5   # T must track the output timeline through display end
```

(and for the animated-sticker branch: `dur = s.end + 0.5`).

**(d) Alpha emission.** The `anim_text` branch currently builds `aexpr = to_ffmpeg_expr(tc.transform.opacity, time_var="T")` unconditionally. Replace with a product of factors:

```python
            factors: list[str] = []
            if is_keyframed(tc.transform.opacity):
                # keyframe times are clip-local (verified); shift onto the T timeline
                factors.append(
                    f"({to_ffmpeg_expr(tc.transform.opacity, time_var='T', start_offset=-tc.start)})"
                )
            else:
                base_opa = _scalar_or_last(tc.transform.opacity, 1.0)
                if base_opa < 0.999:
                    factors.append(f"{base_opa:.3f}")
            factors += _anim_alpha_terms(tc)
            aexpr = "*".join(factors) or "1"
```

(the geq line consuming `aexpr` stays exactly as-is). Apply the same clip-local shift to the animated-**sticker** opacity a few lines below: change `to_ffmpeg_expr(tx.opacity, time_var="T")` to `to_ffmpeg_expr(tx.opacity, time_var="T", start_offset=-s.start)` — stickers share the bug (their x/y expressions already use clip-local `tvar = (t-s.start)`, only opacity was unshifted).

**(e) Slide offset.** The `anim_text` overlay line currently has no x/y. Compute a y expression when sliding (place just before the overlay `parts.append`):

```python
            y_terms: list[str] = []
            off = out_h * 0.03  # slide distance: 3% of output height
            if tc.anim_in in ("slide_up", "slide_down"):
                sgn = 1.0 if tc.anim_in == "slide_up" else -1.0
                y_terms.append(f"{sgn * off:.1f}*(1-clip((t-{tc.start:.3f})/0.3\\,0\\,1))")
            if tc.anim_out in ("slide_up", "slide_down"):
                sgn = -1.0 if tc.anim_out == "slide_up" else 1.0
                y_terms.append(f"{sgn * off:.1f}*(1-clip(({tc.end:.3f}-t)/0.3\\,0\\,1))")
            y_expr = "+".join(y_terms) if y_terms else "0"
```

and change that overlay emission to include it:

```python
            parts.append(
                f"{cur}{preprocessed}overlay=x=0:y='{y_expr}'"
                f":enable='between(t\\,{tc.start:.3f}\\,{tc.end:.3f})'{next_label}"
            )
```

- [ ] **Step 4: Run — expect PASS**

Run: `uv run pytest tests/test_text_anim_export.py tests/test_render_overlay.py tests/test_render_smoke.py tests/test_text_tools.py -v`
Expected: all PASS. If the fade asserts are marginal (codec smoothing), loosen thresholds by sampling further from the boundary (e.g. 2.04 → 2.08) — do not weaken the `< (base+full)/2` midpoint comparisons.

- [ ] **Step 5: Commit**

```bash
git add tests/test_text_anim_export.py src/video_ai_editor/render/text_overlay.py
git commit -m "feat(render): export text anim_in/anim_out; fix frozen-T timebase for late-start overlays"
```

---

# Phase 5 — Configurable limits + docs

### Task 13: Env-configurable chat turns and undo depth

`chat_turn` hard-codes `max_turns: int = 8` (agent/loop.py:66) — with 88 tools now advertised, multi-step edits (e.g. "make three shorts with captions and hooks") need headroom. `EDLStore.MAX_UNDO = 30` (edl/snapshot.py:16) is shallow for heavy manual editing. Both become env vars read at call time (testable without reloads), defaults raised to 16 and 100.

**Files:**
- Create: `tests/test_limits_config.py`
- Modify: `src/video_ai_editor/agent/loop.py:66,84`
- Modify: `src/video_ai_editor/edl/snapshot.py:16,80`
- Modify: `.env.example`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_limits_config.py`:

```python
"""VAI_CHAT_MAX_TURNS and VAI_MAX_UNDO env overrides."""
import tempfile
from pathlib import Path

from video_ai_editor.agent.loop import _resolve_max_turns
from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore


def test_max_turns_default_and_override(monkeypatch):
    monkeypatch.delenv("VAI_CHAT_MAX_TURNS", raising=False)
    assert _resolve_max_turns(None) == 16
    assert _resolve_max_turns(5) == 5           # explicit arg wins
    monkeypatch.setenv("VAI_CHAT_MAX_TURNS", "32")
    assert _resolve_max_turns(None) == 32
    monkeypatch.setenv("VAI_CHAT_MAX_TURNS", "garbage")
    assert _resolve_max_turns(None) == 16       # bad value falls back


def test_undo_depth_env_override(monkeypatch):
    monkeypatch.setenv("VAI_MAX_UNDO", "3")
    store = EDLStore(Path(tempfile.mkdtemp()))
    for i in range(8):
        dispatch(store, "add_marker", {"time": float(i), "label": f"m{i}"})
    snaps = sorted(store.snapshots_dir.glob("*.json"))
    assert len(snaps) == 3, f"expected pruning to 3 snapshots, got {len(snaps)}"


def test_undo_depth_default_is_100(monkeypatch):
    monkeypatch.delenv("VAI_MAX_UNDO", raising=False)
    store = EDLStore(Path(tempfile.mkdtemp()))
    assert store._max_undo() == 100
```

Verified facts this test relies on: `EDLStore` exposes `store.dir` (the session dir) and `store.snapshots_dir`; an initial `00000_<hash>.json` snapshot is written at construction and gets pruned like any other; `add_marker` commits (one snapshot per dispatch). So 1 initial + 8 commits pruned to the last 3 → exactly 3 files.

- [ ] **Step 2: Run — expect FAIL** (`_resolve_max_turns`/`_max_undo` don't exist)

Run: `uv run pytest tests/test_limits_config.py -v`
Expected: ImportError / AttributeError failures.

- [ ] **Step 3: Implement**

In `src/video_ai_editor/agent/loop.py` (already imports `os` — verified), above `chat_turn`:

```python
def _resolve_max_turns(explicit: int | None) -> int:
    """Tool-use rounds per chat turn. Explicit arg > VAI_CHAT_MAX_TURNS > 16."""
    if explicit is not None:
        return explicit
    try:
        return max(1, int(os.environ.get("VAI_CHAT_MAX_TURNS", "16")))
    except ValueError:
        return 16
```

Change the signature default `max_turns: int = 8` → `max_turns: int | None = None`, and immediately inside the function body (before the loop) add `max_turns = _resolve_max_turns(max_turns)`.

In `src/video_ai_editor/edl/snapshot.py` (add `import os` if absent), inside `EDLStore`:

```python
    MAX_UNDO = 100  # kept as a class attr for external references; runtime uses _max_undo()

    @staticmethod
    def _max_undo() -> int:
        try:
            return max(1, int(os.environ.get("VAI_MAX_UNDO", "100")))
        except ValueError:
            return 100
```

and change the pruning line (`snaps[:-self.MAX_UNDO]`) to `snaps[:-self._max_undo()]`. Check for other `MAX_UNDO` references first: `grep -rn "MAX_UNDO" src tests` — update any that assume the value 30.

In `.env.example`, append:

```
# Chat agent: max tool-use rounds per message (default 16)
#VAI_CHAT_MAX_TURNS=16
# Undo history depth in snapshots (default 100)
#VAI_MAX_UNDO=100
```

- [ ] **Step 4: Run — expect PASS**

Run: `uv run pytest tests/test_limits_config.py tests/test_ops_log.py tests/test_tools_dispatch.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_limits_config.py src/video_ai_editor/agent/loop.py src/video_ai_editor/edl/snapshot.py .env.example
git commit -m "feat(config): env-configurable chat max-turns (16) and undo depth (100)"
```

### Task 14: Documentation + full verification

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update CLAUDE.md** — four surgical edits:

1. In the dispatch-engine section, replace the tool-count sentence ("`DISPATCH` holds **92** unique tools, but `tools.py`/`ALL_TOOLS` schematizes only **49** ... (A few `DISPATCH` keys are duplicated, where the later definition silently wins.)") with:
   > **Adding a tool is still a two-file edit, but drift is now enforced:** `tests/test_tool_registry.py` AST-checks `DISPATCH` for duplicate keys/defs and requires every `DISPATCH` tool to be either schematized in `ALL_TOOLS` (**88** tools) or listed in its documented `INTERNAL_ONLY` set (**4**: repair_chunks, repair_media_paths, pyannote_status, record_voiceover). The 88 advertised schemas cost roughly 7-9k input tokens per chat turn.
2. In the EDL data-model section, update the keyframes bullet: `keyframes.to_ffmpeg_expr()` now emits the same easing curves as the browser (`linear`, `step`, `ease-in/out/in-out`, `back-out`; `bounce` exports linear).
3. In the render section's text-overlay bullet, note: text `anim_in`/`anim_out` (fade/slide; pop≈fast-fade) now export via time-varying geq alpha + overlay-y expressions; render cache keys carry `RENDER_SALT`.
4. In the config section's key-vars line, add `VAI_CHAT_MAX_TURNS` (default 16) and `VAI_MAX_UNDO` (default 100).

- [ ] **Step 2: Full backend suite**

Run: `uv run pytest`
Expected: PASS (~90s; AI-dependent tests skip cleanly as usual).

- [ ] **Step 3: Frontend checks (no frontend changes were made — CI parity only)**

Run: `cd frontend && npx tsc --noEmit && npx vite build && cd ..`
Expected: clean.

- [ ] **Step 4: Commit, push, watch CI**

```bash
git add CLAUDE.md
git commit -m "docs: registry enforcement, easing/anim export, and new limit env vars"
git push origin main
gh run watch
```

The **windows-latest** job is the real gate (CLAUDE.md: Windows regressions surface only there). Nothing here adds subprocess calls or filtergraph-embedded paths, but the render tests in Task 12 run on the Windows runner too — confirm they pass or skip cleanly.

---

## Self-review notes

- **Spec coverage:** gap 1 (49→88 exposure) = Tasks 2-9; gap 4 (registry drift) = Task 1 + the permanent test; gap 3a (easing) = Task 10; gap 3b (anim export) = Tasks 11-12; gap 5 (limits) = Task 13; docs = Task 14. Gap 2 (realtime preview) and marketplace breadth are explicitly out-of-scope with rationale.
- **Known judgment calls:** `pop` exports as fast-fade (documented approximation); `bounce` stays linear to match the `sample()` oracle; `record_voiceover` stays internal (needs the browser mic); token cost of 88 schemas (~7-9k/turn) accepted and documented.
- **Verify-before-write steps** remain only where evidence is still incomplete: `stabilize` optional args (Task 8 Step 1) and the `render/chunks.py` fingerprint version token (Task 11 Step 1). Everything else was verified live.
- **Verified against live code (2026-07-03), post-plan verification pass:**
  - Duplicate handler defs confirmed at dispatch.py 365/1199 (`set_track_muted`), 2076/2495 (`vocal_isolate`), 2103/2519 (`instrumental_isolate`) — Task 1's dead-code claim holds.
  - The keyframe oracle is **`sample()`**, not `evaluate` (plan corrected); the mini ffmpeg-expr evaluator reproduces the live linear expression exactly (t=2.0 → 60.0), and emitted expressions use only `if`/`lt`.
  - `add_keyframe` `time` is **clip-local** ("0 = clip start") — Task 12's tests and `start_offset=-clip.start` shifts are written to that, and animated-sticker opacity shares the unshifted-T bug (fix included).
  - **Live bug found:** `add_text` without `role` crashes (`role="default"` fails the `TextClip` Literal; smoke test masks it with `role="super"`) — fixed as Task 3 Steps 1-4 before exposure.
  - `anim_in`/`anim_out` currently render **nowhere** (zero frontend references) — Task 12 is net-new capability, not preview-parity.
  - Preview cache sites: `render/compositor.py` ~654-661 AND `main.py:615-616` must be salted together (Task 11 covers both).
  - `EDLStore` attributes are `dir`/`snapshots_dir` with an initial `00000` snapshot; `add_marker` commits; `loop.py` already imports `os`; `chat_turn` is called from `main.py:699` without `max_turns`.
