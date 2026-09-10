# Prompt Editor — Implementation Blueprint (v0.7.0, revision 2)

Repo: `/Users/sudhanshu/video-ai-editor`. **Baseline is NOT `f547ac3`** — see §0.0. Implementers follow symbol names, never line numbers (every dispatch.py line number in the previous draft was already stale; several were 50–100 lines off). Five implementers (P, B, X, D, K) work in parallel; every cross-owner contract is defined in exactly one place (§0.2) and frozen before anyone writes code.

Verification note for implementers: everything marked **[verified]** below was checked in the working tree on 2026-09-08 while this revision was written. Where a review claim was wrong or unverifiable it is marked **[review corrected]** with the evidence.

---

## 0. BASELINE, OWNERSHIP, CONTRACTS

### 0.0 Day-0 baseline (blocking; nothing else starts before this)

The working tree is dirty **[verified]**: `git status` shows `M agent/dispatch.py` (+216/−64), `M agent/tools.py` (+117), untracked `agent/timemap.py`, `tests/test_transcript_timemap.py`, `tests/test_tool_schema_completeness.py`. The uncommitted work routes `remove_fillers`, `add_caption_track` and `auto_caption` through `timemap.source_range_to_timeline` / `map_segments_to_timeline` **[verified in the diff]**. timemap.py's own docstring records that at HEAD, `remove_fillers` after `remove_silences` removed 1/4 fillers and cut two ranges of real speech, and `auto_caption` after cuts laid cues 10 s past the end. **The `tighten` and `auto_edit` recipes in this spec are only correct on top of that change.**

Day-0 steps, in order:
1. The owner of the timemap work commits it (dispatch.py + tools.py + timemap.py + its two tests) as `fix(transcript): one source↔timeline mapping for every transcript consumer`. Call the SHA **BASE**. This spec is pinned to BASE; X, P and K rebase onto it before touching dispatch.py.
2. `agent/timemap.py` enters the shared-contracts table (§0.2) as a frozen API: `source_to_timeline`, `timeline_to_source`, `source_range_to_timeline`, `map_words_to_timeline`, `map_segments_to_timeline`, `clamp_to_extent`, `media_clips` **[verified: these are the public functions]**.
3. P publishes `agent/prompt/schema.py` + `facts.py` stubs and `recipes.py::RECIPE_NAMES`; B publishes `brains/base.py`; X publishes `service.py::EVENT_TYPES` + the route table; X also publishes `agent/path_args.py` (§1.3 rule 6) because both `tests/test_path_guards.py` and `validate.py` import it.
4. Facts about this machine, corrected **[verified]**: `~/.cache/huggingface/hub` already holds `mlx-community/Qwen2.5-7B-Instruct-4bit` (4.0 GB, full snapshot), `Qwen2.5-0.5B-Instruct-4bit`, `faster-whisper-small/tiny/tiny.en`, laion CLIP; it does **not** hold `large-v3` or MADLAD. Piper `en_US-amy-medium` is cached in `VOICES_DIR`. `mlx_lm` is **not** importable in the project venv; `transformers` is **not** in `uv.lock`. Swift 6.3.3, `xcode-select -p` = Xcode.app, `xcrun --sdk macosx --show-sdk-version` = 26.5. Whether Apple Intelligence is currently enabled is disputed (the brief says on; one reviewer's probe returned `appleIntelligenceNotEnabled`) — the design does not depend on it; B's pre-flight records the real state and `brains_report` shows it.
5. Counts, corrected **[verified]**: `DISPATCH` has **102** entries (not 108); `list_tools()` advertises 96; `ChatOverlay.tsx` exports `ChatOverlay` (not `ChatPanel`); `mcp_server._EXTRA_SCHEMAS` contains only `list_transitions`; `compositor._render` takes `on_progress` (not `set_progress`); `_EXPORT_PRESETS["shorts"].lufs == -14`, `tiktok/reels == -16`.

### 0.1 Who owns what

| Owner | Scope | New files (under `src/video_ai_editor/` unless noted) | Existing files touched |
|---|---|---|---|
| **P** — planner | Intent grammar, slot extraction, timeline facts, recipe table, Plan schema + Pydantic, `TOOL_STAGE`, `DEFAULT_POSTCONDITIONS`, `CHECK_SPECS`, plan validation library, presets (`text_styles`, `transitions`, `templates`, `music` generator, `SAFE_ZONES`), `IntentDraft` → Plan expansion | `agent/prompt/{__init__,schema,facts,slots,grammar,recipes,planner,validate,presets}.py`, `presets/text_styles/*.json`, `presets/transitions/*.json`, `presets/templates/*.json`, `scripts/gen_music_beds.py`, `tests/test_prompt_{schema,slots,grammar,recipes,validate,presets}.py` | none |
| **B** — brains | `Brain` protocol, recipes-brain adapter, FM Swift helper + adapter, MLX adapter + model manager, JSON repair, router, `brains_report`, `Brain.text()` content tasks, helper packaging | `agent/prompt/brains/{__init__,base,recipes_brain,fm,mlx_brain,cloud_plan,router,jsonfix,prompt_text,content}.py`, `agent/prompt/models.py`, `tools/fm-planner/{Package.swift,Sources/fm-planner/{main,IntentDraft,Availability,Errors}.swift,README.md}`, `tests/test_prompt_{brains_router,fm_adapter,mlx_adapter,jsonfix,models,content}.py`, `tests/test_fm_helper_source_guard.py` | `pyproject.toml` (extra `local-llm`), `uv.lock`, `build_app.sh`, `build_notarize.sh` (post-build codesign assertion), `.gitignore` (`tools/fm-planner/.build/`, `presets/music/*.wav`) |
| **X** — executor / verifier / chat / dispatch additions | Plan execution inside `EDLStore.batch()`, run lifecycle decoupled from SSE, postcondition implementations, pending clarification + resume, no-key branch of `chat_turn`, `/api/prompt*` routes, lock registry + 409 policy, verify render, **three dispatch handler changes** (§4.9): new `transcribe` tool, `add_music.loop`, platform-aware caption/lower-third y | `agent/path_args.py`, `agent/prompt/{executor,verify,pending,service,summary,runlog}.py`, `api/locks.py`, `api/prompt_routes.py`, `render/verify_render.py`, `tests/test_prompt_{executor,verify,chat_nokey,routes,pending,security_boundary,sse_contract,runlog}.py`, `tests/test_dispatch_transcribe.py` | `agent/loop.py` (`chat_turn` no-key branch; module docstring), `agent/dispatch.py` (§4.9 only), `agent/tools.py` (schemas for §4.9), `main.py` (`_session_lock` → `api/locks`, 409 on `/dispatch` while a prompt run holds the lock, `_save_history` under history lock, `_validate_ai_config` copy, mount router), `ai/features.py` (`cached_feature_report`), `tests/test_path_guards.py` (import table from `agent/path_args.py`) |
| **D** — desktop UI | Prompt bar, clarification card, run log, brain indicator, download affordance, reconnect-to-run, ChatOverlay compatibility, 409 handling | `frontend/src/components/{PromptBar,PromptRunLog,ClarifyCard,BrainBadge}.tsx`, `frontend/src/components/promptBar.css`, `frontend/src/lib/{promptEvents,promptStore,clarifyDefaults}.ts` + `.test.ts`, `frontend/src/lib/__fixtures__/prompt_stream.txt` | `frontend/src/App.tsx`, `frontend/src/components/ChatOverlay.tsx`, `frontend/src/api.ts`, `frontend/src/keymap/{commands,presets}.ts`, `frontend/src/styles.css` (tokens only if unavoidable) |
| **K** — benchmark + docs | Media synthesis, ~22 prompts with machine-checkable assertions, report, markers, docs, version bump | `tests/benchmark/{__init__,media,narration,prompts,harness,report,test_capcut_parity,test_media_synthesis}.py`, `tests/benchmark/README.md`, `docs/PROMPT_EDITOR.md`, `docs/BENCHMARK.md` | `pyproject.toml` (markers, version), `VERSION`, `frontend/package.json` + lock, `mobile/package.json`, `mobile/app.json`, `CHANGELOG.md`, `README.md`, `CLAUDE.md`, `.env.example`, `mobile/lib/sse.ts` (one comment line) |

### 0.2 Shared contracts — single definition, single owner

| Contract | Defined in | Owner | Consumers |
|---|---|---|---|
| `agent/timemap.py` API | itself (frozen at BASE) | timemap owner | P (facts), X (verifier, transcribe), K (independent assertions) |
| Plan JSON schema + `Plan`, `Step`, `NeedsInput`, `Postcondition`, `IntentDraft`, `TOOL_STAGE`, `DEFAULT_POSTCONDITIONS`, `CHECK_SPECS` | `agent/prompt/schema.py` | P | B, X, D, K |
| `TimelineFacts` | `agent/prompt/facts.py` | P | B, X |
| Recipe names + slot names + `recipes.cards()` + `recipes.from_intents(IntentDraft, facts) -> Plan` | `agent/prompt/recipes.py` | P | B, K |
| `validate_plan(plan, facts) -> Plan` raising `PlanRejected` | `agent/prompt/validate.py` | P | X (boundary), B (router fall-through) |
| Path-arg table `PATH_ARGS: dict[(tool,arg) -> "read"|"write"|"exempt"]` | `agent/path_args.py` | X | P (validate), `tests/test_path_guards.py` |
| `Brain` protocol, `BrainResult`, `BrainUnavailable`, `BRAIN_IDS` | `agent/prompt/brains/base.py` | B | X, D, K |
| `brains_report()` payload | `agent/prompt/brains/router.py` | B | X, D, K |
| SSE event contract (existing 6 + `brain`, `plan`, `step`, `verify`, `clarify`) | `agent/loop.py` docstring + `service.py::EVENT_TYPES` | X | D, mobile (ignores), K |
| Prompt routes | `api/prompt_routes.py` | X | D, K |
| Run record `<session>/prompt_run.json` | `agent/prompt/runlog.py` | X | D (reconnect), K |
| FM helper stdin/stdout JSON | `tools/fm-planner/README.md` | B | B |
| Presets formats + `SAFE_ZONES` | `agent/prompt/presets.py` | P | X (verifier + §4.9 handlers import `SAFE_ZONES`), K |
| Benchmark case format | `tests/benchmark/prompts.py` | K | K, P (grammar fixtures) |

---

## 1. THE PLAN CONTRACT

### 1.1 Plan schema (`schema.py::PLAN_JSON_SCHEMA`; a test asserts `Plan.model_json_schema()` ⊇ it)

```json
{
  "$id": "vai://plan/1",
  "type": "object",
  "required": ["version", "intent", "steps", "needs_input", "postconditions", "confidence", "brain"],
  "additionalProperties": false,
  "properties": {
    "version": {"const": 1},
    "id": {"type": "string", "pattern": "^p_[0-9a-f]{8}$"},
    "intent": {"type": "string", "maxLength": 64},
    "title": {"type": "string", "maxLength": 80},
    "steps": {"type": "array", "maxItems": 24, "items": {
      "type": "object", "required": ["tool", "args", "why"], "additionalProperties": false,
      "properties": {
        "tool": {"type": "string", "pattern": "^(recipe:)?[a-z_]{2,48}$"},
        "args": {"type": "object"},
        "why": {"type": "string", "maxLength": 160},
        "optional": {"type": "boolean", "default": false},
        "stage": {"type": "integer", "minimum": 0, "maximum": 12}}}},
    "needs_input": {"type": "array", "maxItems": 4, "items": {
      "type": "object", "required": ["key", "question", "required"], "additionalProperties": false,
      "properties": {
        "key": {"type": "string", "pattern": "^[a-z_]{2,32}$"},
        "question": {"type": "string", "maxLength": 200},
        "kind": {"enum": ["choice", "number", "text", "duration", "path", "confirm"], "default": "choice"},
        "options": {"type": "array", "maxItems": 12, "items": {"type": "object", "required": ["value", "label"],
                    "properties": {"value": {}, "label": {"type": "string"}, "hint": {"type": "string"}, "synonyms": {"type": "array", "items": {"type": "string"}}}}},
        "default": {}, "required": {"type": "boolean"},
        "min": {"type": "number"}, "max": {"type": "number"}, "unit": {"type": "string"}}}},
    "postconditions": {"type": "array", "maxItems": 20, "items": {
      "type": "object", "required": ["check", "args", "human"], "additionalProperties": false,
      "properties": {"check": {"type": "string", "pattern": "^[a-z_]{2,40}$"}, "args": {"type": "object"},
                     "human": {"type": "string", "maxLength": 140}, "needs_render": {"type": "boolean", "default": false},
                     "headline": {"type": "boolean", "default": true}}}},
    "downloads_needed": {"type": "array", "maxItems": 6, "items": {"type": "object", "required": ["what", "bytes", "tool"],
                         "properties": {"what": {"type": "string"}, "bytes": {"type": "integer"}, "tool": {"type": "string"}}}},
    "estimated_seconds": {"type": "number"},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "brain": {"enum": ["recipes", "apple_intelligence", "local_model", "claude"]},
    "content_brain": {"enum": ["recipes", "apple_intelligence", "local_model", "claude", null]},
    "reply": {"type": "string", "maxLength": 400}
  }
}
```

Pydantic (P): `Step`, `NeedsInput`, `Postcondition`, `Plan` with `ConfigDict(extra="forbid", frozen=True)`. `Plan.new(...)` assigns `id = f"p_{uuid4().hex[:8]}"`. `content_brain` names the brain that wrote content (hook text, shorts ranking) when it differs from the planning brain (§3.7).

**`IntentDraft`** (what the on-device LLM brains emit — §3.1): `{"intents":[{"recipe": str, "slots": {str: str|number|bool|null}}], "exclusions": [str], "needs_input": [ {key, question, options:[str], default} ], "confidence": float, "reply": str}`. `recipes.from_intents(draft, facts)` turns it into a `Plan` through the same recipe expansion the grammar uses — so an LLM plan gets stages, postconditions, prerequisites and idempotence rules for free. Only the `claude` brain may emit raw tool steps.

**`TOOL_STAGE: dict[str, int]`** and **`DEFAULT_POSTCONDITIONS: dict[str, list[Postcondition]]`** (P, `schema.py`): every allow-listed tool has a stage and at least one natural postcondition (`auto_reframe → canvas_aspect + reframe_effective`, `add_music → music_present + music_covers`, `set_speed → speed_equals`, `cut_range → duration_between`, `add_transition → transitions_count_geq`, `apply_lut → effect_present`, … ; `tool_ok` for the rest). `validate_plan` fills `stage`/postconditions for any raw step lacking them, so "every executed plan is verified" holds for `claude`-brain plans too. Test: a raw-tool plan from a fake LLM brain ends with ≥1 non-`tool_ok` postcondition.

Semantics:
- `steps[].tool` is a dispatch tool or `recipe:<name>`; `validate_plan` expands macros before validation, so the executor only sees real tools.
- `optional=true` → a raising step is recorded `skipped`, execution continues; `false` → the plan aborts and `EDLStore.batch()` rolls the tree back.
- `stage` is the composition stage (§2.5); `validate_plan` sorts by `(stage, original index)`.
- **Clip-ref sentinels**: `args.clip_id` (and `clip_ids`) may be `"$v1_all"`, `"$v1_first"`, `"$v1_last"`, `"$selected"`, `"$playhead"`. `validate_plan` accepts sentinels; the executor resolves them against `store.edl` immediately before each dispatch, inside the batch (so per-clip steps after cuts see the fragments `cut_range` created with new ids — **[verified]** `_clone_clip` assigns `c_…_<uuid>`; a pre-plan `clip_id` would grade only the first fragment). A step with `$v1_all` fans out into one dispatch per clip; all share the step index and one `step` event with fractional progress.
- `needs_input[]` with `default` present → pre-filled and the executor runs immediately; `required && default is None` pauses (§4.3). `kind:"confirm"` is a two-option choice whose `value`s are `"yes"`/`"no"`.
- `downloads_needed` non-empty → the planner appends a `needs_input{key:"downloads", kind:"confirm", required:true, default:null}` whose question lists every artefact and size. **Nothing in the prompt path ever downloads without this answer** (§1.4).
- `estimated_seconds > 90` → the planner appends `needs_input{key:"go", kind:"confirm", required:true, default:null, question:"This will take about N minutes (auto captions on a 10-minute clip). Start?"}`. Short work asks nothing.
- `postconditions[].needs_render=true` → one verify render (§4.4) shared by all render-based checks. `headline=false` marks checks that are reported but not counted toward the parity headline (§4.4).

### 1.2 How each brain produces a Plan
- **recipes** (P): `planner.plan(prompt, facts) -> Plan` — grammar → intents → recipes → steps.
- **apple_intelligence** (B): helper emits `@Generable IntentDraft` (typed slot fields, no JSON-in-a-string) → `recipes.from_intents`.
- **local_model** (B): Qwen emits `IntentDraft` JSON → `jsonfix.repair` → `IntentDraft.model_validate` → `recipes.from_intents`.
- **claude** (B, only with `ANTHROPIC_API_KEY` and `VAI_PROMPT_CLOUD != "0"`): one `messages.create` with forced tool `emit_plan` (`input_schema` = `PLAN_JSON_SCHEMA` minus `brain/id/content_brain/downloads_needed/estimated_seconds`), may use raw tools.

### 1.3 Strict validation (`validate.py`, P; X calls it before any dispatch — THIS is the security boundary)

`validate_plan(plan: Plan, facts: TimelineFacts) -> Plan`:
1. `Plan.model_validate` (`extra="forbid"`).
2. Expand `recipe:` macros; unknown macro → `PlanRejected`.
3. **Tool allowlist**: `PLAN_TOOLS = ({t["name"] for t in tools.list_tools()} ∪ EXTRA_TOOL_SCHEMAS) − PLAN_DENY`.
   `PLAN_DENY = {undo, redo, repair_media_paths, repair_chunks, save_show_template, record_voiceover, import_srt, export_srt, export_vtt, export_ass, multicam, find_broll, object_erase, motion_track, remove_background, set_property, add_clip, add_sticker, add_effect, pyannote_status, list_shows}`.
   Reasons **[verified]**: `set_property(path="src", value=…)` routes `value` through `_safe_src`, which is a no-op when `restrict_paths_active()` is false (the desktop case) — a plan could re-point a clip at any file; `matte_src`/other leaves are not guarded at all. `add_clip.src` adds media plans must not source. `add_sticker(emoji=…)` fetches PNGs from cdn.jsdelivr.net (`ai/emoji.py`) — network egress. `add_effect(type="lut", params={"src": …})` carries a path inside `params`. `undo/redo` are handled as their own intent (§4.6).
   `EXTRA_TOOL_SCHEMAS` (**new**, P; not a mirror of anything — `apply_hook_stack` has **no** schema anywhere today, so `_validate_tool_args` passes it through unvalidated):
   ```python
   EXTRA_TOOL_SCHEMAS = {"apply_hook_stack": {"type":"object","properties":{
       "text":{"type":"string"},"duration":{"type":"number"},
       "visual":{"type":"string","enum":["punch_in","ken_burns","none"]},
       "audio":{"type":"string","enum":["fade_boost","none"]}}, "required": ["text"]}}
   EXTRA_ARGS = {"auto_reframe": {"subject_track": {"type":"boolean"}},
                 "apply_template": {"with_hook_stack": {"type":"boolean"}},
                 "add_caption_track": {"chunk_size": {"type":"integer"}},
                 "add_music": {"loop": {"type":"boolean"}}}          # §4.9
   ```
   Note `apply_hook_stack.text` is **required in plans** (§3.7: the recipe always supplies text so the handler never calls `generate_hook`, which calls Anthropic whenever a key is set **[verified]** even if the badge says Recipes).
4. Schema source: `tools._schema_for(tool)` (now in tools.py after BASE **[verified in diff]**) + `EXTRA_TOOL_SCHEMAS` + `EXTRA_ARGS`.
5. **Unknown arg → reject**; required args present; types/enums via `dispatch._arg_type_ok` (import, don't duplicate). Validation works on a **copy** of args; `_validate_tool_args` mutates `args[key] = hit` in place **[verified]**, so the executor dispatches `dict(step.args)` and a test asserts `plan` is unchanged after `run_plan`.
6. **Path rule** — `PATH_ARGS` comes from `agent/path_args.py` (X moves `EXPECTED_GUARDS` from `tests/test_path_guards.py` there verbatim, 21 rows **[verified]**; the test imports it and keeps `test_the_guard_count_is_pinned`). For every `(tool, arg)` marked `read` or `write` plus the aliases `apply_lut.lut_path` **[verified: handler reads `src or lut_path`]**: `write` → reject outright; `read` → the value must be an exact member of `facts.allowed_paths` after `Path.resolve()` (session uploads, `presets/music/*.wav`, `presets/end_cards/*`, session `cache/tts/*.wav` produced earlier in the same plan). LUTs are passed as **bare names** (`src="teal_orange.cube"`, which `apply_lut` resolves from the bundled dir) and validated against `presets/luts` basenames — never as absolute paths. Then `config.assert_path_allowed()` when `restrict_paths_active()`. Not offered → `PlanRejected("path not offered")`.
7. `clip_id`/`track` must exist in `facts.clip_ids`/`facts.track_ids` **or** be a sentinel (§1.1).
8. **`ARG_BOUNDS`** — numeric bounds and, critically, **enum whitelists for every free string that reaches the filesystem or the network**:
   - `auto_caption.model`, `transcribe.model` ∈ `{"small","large-v3-turbo","large-v3"}` and only when that model is on disk (§1.4); recipes never set `model` except from a slot.
   - `tts_voiceover.voice` ∈ `VOICE_IDS = {en_US-amy-medium, en_US-ryan-medium, en_GB-alan-medium, hi_IN-priyamvada-medium, hi_IN-pratham-medium}` — `ensure_voice` downloads any name from HF **[verified]**, so a voice not in `ai.tts.voice_paths()` on disk becomes a `downloads_needed` entry, never a silent fetch.
   - `add_text.font`, `apply_brand_kit.font` ∈ basenames of `fonts/` (Anton-Regular, BebasNeue-Regular, Inter-Black, Inter-Bold, InterVariable, Montserrat-Bold, Montserrat-Regular, NotoSansArabic-VF, NotoSansDevanagari-VF, NotoSansSC-VF **[verified]**).
   - `translate_captions.target_lang`, `auto_caption.target` ∈ `dispatch.CAPTION_TARGETS` (`hi, en, hinglish, es` **[verified]**).
   - `apply_template.name`/`apply_show_template.name` ∈ `show.templates.TEMPLATES` keys / `presets/shows/*.json` stems; `apply_export_preset.name` ∈ `_EXPORT_PRESETS`.
   - `add_transition.type` ∈ `render.transitions.all_names()`; `apply_lut.src` ∈ LUT names; `set_clip_fit.fit` ∈ `{contain, cover}`.
   - Numerics: `set_speed.factor ∈ [0.25,4]`, `add_music.volume_db ∈ [-40,0]`, `set_loudness_target.lufs ∈ [-24,-9]`, `auto_caption.max_chars ∈ [16,60]`, `add_transition.duration ∈ [0.1,2]`, `make_shorts.target_count ∈ [1,10]`, `make_shorts.max_dur ∈ [5,180]`, `tts_voiceover.text ≤ 600 chars`, `add_text.text ≤ 120`, `apply_hook_stack.text ≤ 60`, `apply_hook_stack.duration ∈ [1,6]`, `auto_cut_to_beats.subdivision ∈ [1,16]`.
9. `len(steps) ≤ 24`; `estimated_seconds = Σ RECIPE_COST[tool](facts)`.
10. Returns a **new** Plan sorted by stage with `id` set.

`PlanRejected(ValueError)` carries `.reasons`.

### 1.4 First-use downloads are a question, never a side effect
`facts.first_use` (P, from cache probes; cheap `Path.exists` checks only):
| Artefact | Probe | Size | Triggered by |
|---|---|---|---|
| faster-whisper `small` | `HF_HOME/hub/models--Systran--faster-whisper-small/snapshots/*/model.bin` | 480 MB | `transcribe`, `auto_caption` (default model) |
| faster-whisper `large-v3` / `large-v3-turbo` | same pattern | 3.1 GB / 1.6 GB | explicit "accurate captions" slot only |
| whisper-cli ggml | `config._WHISPER_CPP_MODEL_DIRS` | — | packaged app path |
| MADLAD | `ai.translate` model dir probe | ~3 GB | `target ∈ _TRANSLATED_TARGETS` (`hi, hinglish, es`), `translate_captions` |
| Piper voice | `ai.tts.voice_paths(name)[0].exists()` | 60 MB | `tts_voiceover` |
The planner sums the missing ones into `plan.downloads_needed`; the confirm question is the plan's first `needs_input`. On the phone the `text_delta` says: "First use downloads MADLAD translation model (3 GB). Reply **download** or **skip**." "skip" removes the dependent steps (captions fall back to as-spoken language and the reply says so). "No cloud key" is not "no network"; the rule is **no network without a yes**.

---

## 2. RECIPE PLANNER (P)

### 2.1 `TimelineFacts` (`facts.py`)
`build_facts(store, ui_state, *, feature_report=None) -> TimelineFacts` — pure reads. `tools_available` comes from `ai.features.cached_feature_report()` (X adds it: the memo currently in `main._FEATURE_REPORT_CACHE` **[verified at main.py `_FEATURE_REPORT_CACHE`]** moves into `ai/features.py`; `/api/features` uses it; `build_facts` never triggers the 2.2 s cold path twice). Probe of the first v1 clip is cached per `src` in `store` (`store.probe_cache: dict[str, dict]`).

```python
class SpeechSpan(BaseModel): start: float; end: float          # TIMELINE seconds
class TimelineFacts(BaseModel):
    session_id: str; duration: float; canvas_w: int; canvas_h: int; fps: int
    aspect: Literal["9:16","16:9","1:1","4:5","other"]; source_aspect: ... | None
    v1_clip_ids: list[str]; clip_ids: list[str]; track_ids: list[str]
    selection: str | None; playhead: float | None
    has_transcript: bool; transcript_pending: bool; transcript_backend: Literal["faster_whisper","whisper_cli",None]
    language: str | None; words: int
    speech_spans: list[SpeechSpan]; speech_seconds: float      # via timemap.map_segments_to_timeline(edl,"v1",segments)
    filler_count: int                                          # tokens in FILLERS_STRICT after mapping (only words that survive on the timeline)
    has_music: bool; music_ducked: bool; has_captions: bool; caption_style: str | None
    v1_boundaries: list[float]; hook_axes: dict; brand_handle: str | None; loudness_lufs: float | None
    uploads_audio: list[str]; uploads_images: list[str]; allowed_paths: set[str]
    tools_available: set[str]; first_use: dict[str, int]       # missing artefacts → bytes
    ingest_json_path: str | None                               # for the executor's transcript snapshot
```
`transcript_pending` = the first v1 upload's `ingest.json` exists without a `transcript` key and its mtime is < 10 min old (upload transcription is a `BackgroundTask` that swallows exceptions **[verified `_bg_transcribe`]**, so "pending" is the honest word for the first minute after upload). All speech facts are in **timeline** seconds; test: cut `[5,10)` on v1, then a word at source 12 s appears in facts at timeline 7 s.

### 2.2 Slot extraction (`slots.py`) — pure, table-tested (≥ 140 cases)
| Slot | Rule | Notes |
|---|---|---|
| `duration_s` | `(\d+(?:\.\d+)?)\s*(s|sec|secs|second(s)?|m|min(s)?|minute(s)?)\b`; `half a minute`=30, `a minute`=60; `under/max/at most N` → `max`; `around/about N` → target ±10% | |
| `count` | `(\d+|one…ten)\s+(shorts?|clips?|highlights?|reels?|parts?)` — **not** when preceded by `youtube` (`youtube shorts` is a platform) | |
| `platform` | reels/instagram/ig→`reels`; tiktok→`tiktok`; `youtube shorts`/shorts→`shorts`; story/stories→`story`; youtube/yt→`youtube_16x9`; 4k→`youtube_4k`; square/feed/1:1→`ig_feed_1x1`; 4:5/portrait feed/linkedin→`ig_feed_4x5` | |
| `ratio` | explicit `9:16|16:9|1:1|4:5`; vertical/portrait→9:16; landscape/horizontal/wide→16:9; square→1:1; else from platform | Extracted **before** `speed` so `1080x1920` never reads as a speed |
| `upscale_factor` | `(2|4)x\s*(upscale|resolution|res)` or `upscale … (2|4)x` | before `speed` |
| `speed` | `(\d+(?:\.\d+)?)\s*x(?![\dx:])\b` (boundary after `x`), `double`=2, `half`=0.5, `slow(-| )mo(tion)?`=0.5, bare `faster/speed up`=1.25, `slower`=0.8; ignored when the clause matched `count`/`ratio`/`upscale` | |
| `language` | `in (hindi|hinglish|english|spanish)`, `hindi captions`, codes `hi/en/es`, roman(ised) hindi→hinglish, devanagari→hi | |
| `caption_style` | chunky/bold/ig→`ig_chunky`; karaoke/word by word/highlight→`word_emphasis`; plain/simple/clean→`default`; top/center/bottom→position; `accurate/better/precise captions` → `model_upgrade=true` | |
| `mood` | chill/lofi/calm→`chill`; upbeat/energetic/hype→`upbeat`; cinematic/epic→`cinematic`; lofi→`lofi` | |
| `look` | cinematic/teal→`teal_orange.cube`; warm/golden→`warm.cube`; cool/cold→`cool.cube`; punchy/vivid→`punch.cube`; faded/film/vintage→`faded.cube`; b&w/mono→`mono.cube` | |
| `color_hex`, `handle`, `hashtags`, `name`, `quoted_text`, `clip_ref`, `range`, `filler_words` | as before; `name` = up to 3 capitalised tokens after `for|named|called|by`, or quoted | |
| `lufs` | explicit `-14 LUFS`; else **`_EXPORT_PRESETS[platform]["lufs"]`** (single source; `shorts` is −14, `tiktok/reels/story` −16 **[verified]**); else `facts.loudness_lufs` or −16 | |
| `voice` | female→`en_US-amy-medium`, male→`en_US-ryan-medium`, british→`en_GB-alan-medium`, indian/hindi→`hi_IN-priyamvada-medium` (female) / `hi_IN-pratham-medium` (male) | not-cached voices → `downloads_needed` |
Normalisation: lower-case, collapse whitespace, strip trailing punctuation, `&`→`and`, `w/`→`with`, `HINGLISH_VERBS` (30 entries: `kar do`, `laga do`, `hata do`, `banao`, `chhota karo`, `tez karo`, …) → `add/remove/make/shorten/speed`.

### 2.3 Intent grammar (`grammar.py`)
`detect(prompt) -> list[IntentHit]`; clause split on `,`/`and`/`then`/`aur`/`phir`; per clause phrase-table + regex scored 1.0 / 0.85 / 0.5; **clause precedence table** resolves `cut`: `cut to the beat` → `beat_sync`; `cut the first N s` / `cut from A to B` → `trim`; `cut out the ums` → `remove_fillers`; `cut into N` → `shorts`; bare `cut` → `trim` only with a range. Intent table as in the draft (`auto_edit, captions, translate_captions, remove_silences, remove_fillers, tighten, shorts, reframe, music, duck, beat_sync, hook, color_look, clean_audio, loudness, speed, trim, title, brand, end_card, transitions, export_preset, voiceover, stabilize, upscale, undo, redo, ask`), plus template names/descriptions from `presets/templates` as `auto_edit` variants. Negation guard (`no captions`, `without music`, `bina music`) → `exclusions`.

### 2.4 Recipe table (`recipes.py`) — every difference from the draft is deliberate
Stages: `(n)`; `?` = optional. Postconditions name §4.4 checks.

| Recipe | Slots (default) | Steps | Postconditions |
|---|---|---|---|
| `transcribe` (prerequisite; auto-inserted) | `model` (`WHISPER_MODEL`=small) | (1) **`transcribe(model)`** — the new non-mutating tool (§4.9); never `auto_caption`. If `facts.transcript_pending`, the executor first waits ≤ 30 s for the upload transcript (§4.2) and drops this step when it arrives. | `transcript_present` |
| `captions` | `style`(ig_chunky), `position`(bottom), `target`(None), `max_chars`(42), `model_upgrade`(false) | **Default = `add_caption_track(style, position, max_chars)` from the persisted transcript** (fast, no model). Use (7) `auto_caption(style, position, target, max_chars[, model])` only when `target` is set (language change), `model_upgrade`, or no transcript exists and the prompt itself asked for captions. `auto_caption` default model stays `WHISPER_CAPTION_MODEL` (large-v3) only if cached; else `large-v3-turbo` if cached; else `small` with `reply` note "captions from the small model — say 'accurate captions' to download large-v3 (3.1 GB)". | `captions_cover(0.9)`, `captions_nonempty`, `captions_within_extent`, `captions_language(target)` if target, `captions_style(style)` |
| `translate_captions` | `target_lang` (required → needs_input choice `hi/en/hinglish/es`) | prepend `captions` if none; (7) `translate_captions(target_lang)` | `captions_language(target)` |
| `remove_silences` | `threshold_db`(−30), `min_dur`(0.5), `keep_pad`(0.1) | (2) `remove_silences(track="v1", …)` | `duration_shrank(min_ratio=0.02)` iff facts predict silence, `speech_preserved`, `silence_total_leq(max_total_s=1.0, needs_render)` ("no long pauses remain" — the verifier reads the step's own `threshold_db`/`min_dur`/`keep_pad`) |
| `remove_fillers` | `words` (**`FILLERS_STRICT = um, uh, hmm, erm, uhh, umm`** + user-quoted words; `like`/`you know`/`so basically` only when quoted — `remove_fillers` matches **single tokens only** **[verified]** so multi-word entries never matched anyway, and unquoted `like` is a content word) | (2) `remove_fillers(words, pad=0.05, track="v1")` | `fillers_remaining_leq(words, 0)`, `duration_shrank(min_seconds=0.1)` iff `facts.filler_count>0`, `speech_preserved` |
| `tighten` | — | `remove_silences` + `remove_fillers` (prereq `transcribe`) | union |
| `shorts` | `count`(3), `max_dur`(60/90/30 by platform), `min_dur`(min(12,max_dur/2)), `finish`(true iff a platform was named) | (3) `make_shorts(target_count, max_dur, min_dur, save_as_sessions=true)` — **no transcript prerequisite** (`ai/shorts.py` takes `transcript | None` **[verified]**). When `finish`: after the parent commit, the executor runs a child plan per new session — `reframe(9:16)` + `captions` + `hook` — each its own one-op commit in that session (§4.2). Terminal for the parent. | `shorts_created(count, max_dur, min_dur)`, `shorts_finished` when finish |
| `reframe` | `ratio`, `subject_track` (true iff `tracking` feature available — the gate is cv2 **[verified: `ai/reframe.py` imports cv2]**) | (6) `auto_reframe(ratio, subject_track)`; **then (6) `set_clip_fit(clip_id="$v1_all", fit="cover")`** as a guaranteed no-letterbox fallback (`Clip.fit` defaults to `contain` **[verified schema.py]**, and `auto_reframe` swallows per-clip failures into `"(skipped: …)"` and commits anyway **[verified]**). Skip both when `facts.aspect == ratio`. | `canvas_aspect(ratio)`, `reframe_effective`, `no_letterbox`, `overlays_inside_safe_zone(ratio)` |
| `music` | `src` (needs_input `path`, default = the only upload, else the mood bed), `volume_db`(−14), `duck`(true) | (9) `add_music(src, start=0, volume_db, duck=true, loop=true)` (§4.9); skip with note if music exists and no "another/replace" | `music_present(ducked)`, `music_covers(0.95)`, `music_within_video_extent` |
| `duck` | `to_db`(−18) | (9) `set_duck(track="music", enabled=true, to_db)` | `music_ducked(−12)` |
| `beat_sync` | `subdivision`(4), `pulse`(true) | needs music; (9) `auto_cut_to_beats(subdivision)` — split-only **[verified: it only calls `split_at`]**, invisible on one continuous clip — **then** (9) `set_clip_transform(clip_id="$v1_all", scale_keyframes=alternating 1.0/1.05 per fragment, snap=word_boundaries)`: the recipe computes the split list itself from `ingest.beats.detect_beats` on the music src, drops candidates within 120 ms of a word interior (`facts.speech_spans` word timings) and any that would create a shot < 0.8 s, then emits `split_at` steps and per-fragment punch-in keyframes (reusing `apply_hook_stack`'s keyframe shape `[[0,1.0],[d,1.05]]` **[verified]**). `auto_cut_to_beats` is used only when no transcript exists. | `beat_splits_geq(2)`, `beat_pulse_present(n≥2)`, `min_shot_geq(0.8)` |
| `hook` | `text` (quoted, else `Brain.text("hook")` §3.7, else heuristic), `duration`(3.0) | (8) `apply_hook_stack(text, duration, visual="punch_in", audio="fade_boost")` — `text` always passed | `hook_text_starts_leq(0.5)`, `hook_axes_geq(3, headline=false)` |
| `color_look` | `look`, `intensity`(0.8) | (4) `apply_lut(clip_id="$v1_all", src="<look>.cube", intensity)` | `effect_present("lut","v1",all)` |
| `clean_audio` | `strength`(0.85) | (10) `noise_reduce?(clip_id="$v1_all", strength)`; (10) `set_loudness_target(lufs)` **unless** an `export_preset` step follows and no explicit LUFS was given (the preset sets it) | `clip_src_changed` if ran, `loudness_target_set`, `loudness_within(±1, needs_render)` |
| `loudness` | `lufs` | (10) or (11, after preset) `set_loudness_target(lufs)` | same |
| `speed` | `factor`, `clip_ref` | (2) `set_speed(clip_id=<ref>, factor)`; `<1 and "smooth"` → `smooth_slow_motion?` | `speed_equals`, `duration_between(±5%)` |
| `trim` | `range` | (2) `cut_range(track="v1", start, end)` | `duration_between(±0.1)` |
| `title` | `text`/`name`, `handle`, `at`, `dur`(3) | (8) `add_lower_third(name, handle?, start, end)` or `add_text(...)` with text-style preset | `text_present`, `overlays_inside_safe_zone` |
| `brand` | `handle` (required → needs_input text, default `facts.brand_handle`), `hashtags`, `palette` | (8) `apply_brand_kit(handle, hashtags, palette)` — creates watermark **and** an end-card text in the last 3 s **[verified brand_kit.py]** | `brand_watermark_present`, `brand_kit_set`, `text_present(contains=handle, start_geq=dur−3.5)` |
| `end_card` | `handle` | **no-op with note when `brand` is in the same plan or `edl.brand_kit.handle` is set** (otherwise two handles overlap); else (8) `apply_text_template("end_card_handle", …)` | `text_present(...)` |
| `transitions` | `look`(smooth) | **(5)** `add_transition(at, type, duration)` per v1 boundary (≤12) — stage 5 is **before** reframe/captions/text because `add_transition` shortens the timeline by the overlap **[verified: "shortened_by" in its result]** and does not ripple overlays | `transitions_count_geq`, `captions_within_extent` |
| `export_preset` | `platform` | (11) `apply_export_preset(name)`; **if the preset aspect ≠ `facts.aspect`, the planner inserts `reframe(ratio)` first** — `apply_export_preset` changes canvas w/h without `_rescale_overlays_for_canvas_change` **[verified: no call in the handler]**, so alone it letterboxes and leaves overlays at old coordinates | `export_preset_applied`, `no_letterbox` |
| `voiceover` | `text` (required), `voice`, `start` | (8) `tts_voiceover(text, voice, start, volume_db=0)` | `vo_present` |
| `stabilize` / `upscale` | `clip_ref` | (2) optional, feature-gated (`stabilize` is unavailable on this Mac: brew ffmpeg lacks libvidstab) | `clip_src_changed` |
| `auto_edit` | platform, language, mood, look; exclusions | `tighten` (if speech) → `color_look?` (only if look said) → `transitions` (only if ≥2 v1 clips) → `reframe` (if platform/ratio differs) → `captions` → `hook` → `brand` (if handle known) → `music` → `clean_audio` → `export_preset` → (12) `audit_aesthetic` | union + `audit_ok` (`audit.ok`, no error-level issues, `hook_score==3`; **not** `score ≥ 80` — `long_shot` warns for every >6 s shot on short-form and each warning costs 5 **[verified audit.py]**, so a raw score punishes a correct talking-head edit) |
| `ask` | — | no steps; reply from facts | none |

### 2.5 Composition rules (`planner.py::compose`)
Stages: `0` inspection/undo · `1` prerequisites (`transcribe`) · `2` cuts (trim, silences, fillers, speed, stabilize, upscale) · `3` structure (shorts; terminal) · `4` look · `5` transitions · `6` reframe (+fit) · `7` captions/translate · `8` text (hook, title, brand, end card, voiceover) · `9` music (add, duck, beats) · `10` audio (noise, loudness) · `11` export preset (+ explicit loudness after it) · `12` audit.
- Dedupe: same tool+args once. **Never two transcription passes**: the prerequisite is `transcribe`; the captions step is `add_caption_track` unless a language change/model upgrade forces `auto_caption`, in which case `transcribe` is dropped (auto_caption transcribes) **and** stage-2 steps that need the transcript are moved after it — i.e. the planner emits `auto_caption` at stage 1 and `add_caption_track` is not needed. A `remove_fillers`/`remove_silences` result with `cuts == 0` is reported `ok · no effect` in the step summary (the handler commits "no transcript" with `cuts=0` rather than raising **[verified]**), so the run log is honest before verify.
- Conflicts: `export_preset` aspect ≠ `reframe` ratio → reframe wins, preset re-derived. `lufs`: one source (§2.2).
- Idempotence: existing captions + `captions` → replace; existing music + `music` → skip with note; hook → run (idempotent).
- Feature gates: tool absent from `facts.tools_available` → dropped if optional, else `needs_input` choice `[skip, abort]` with the feature's `fix` as hint. In the **packaged Mac app** faster-whisper is excluded (`features.PACKAGED_FIX` **[verified]**), so `transcribe/auto_caption` are available only via whisper-cli + a ggml model; `facts.tools_available` reflects that and `brains_report` shows the ggml fix.
- Downloads and duration gates: §1.1 (`downloads`, `go`).
- Terminal: `shorts` ends the parent plan; `finish` children are separate one-op commits.

### 2.6 Smart defaults — as in the draft, plus: `music.src` = the only upload, else mood bed; `hook.text` = quoted → `Brain.text` → heuristic (`reply` says which); `captions.model` = cached-best (§2.4).

### 2.7 Confidence & clarification policy
`confidence = mean(clause_scores) × coverage`. `≥ 0.75` → recipes run; `0.4–0.75` → LLM brains normalise (§3.4), else recipes with "I read that as: …"; `< 0.4` → `clarify{key:"intent"}` with top-3 guesses. Zero-question rule holds except: `translate.target_lang`, `brand.handle` (unknown), `voiceover.text`, `reframe.ratio` (source already 9:16, no platform), `downloads`, `go`.

### 2.8 Presets (P)
**`presets/text_styles/*.json`** — `bold_pop`, `clean_lower`, `hook_shout`, `label_tag`, `caption_karaoke`, `caption_ig` as drafted; `y_frac` values come from `SAFE_ZONES` (below), not hard-coded.
**`presets/transitions/*.json`** — `smooth {crossdissolve, 0.4}`, `cinematic {fadeblack, crossdissolve, 0.6}`, `punchy {whip, zoomin, slideleft, 0.25}`, `clean {fade, 0.3}`; loader validates types against `render.transitions.all_names()`.
**`presets/templates/*.json`** — `talking_head_reel`, `youtube_explainer`, `tiktok_hype`, `story_teaser`, `lecture_cleanup` (recipe lists as drafted; stage order comes from the recipes, not the file).
**`presets/music/`** — beds are **generated, not committed**: `scripts/gen_music_beds.py` (deterministic lavfi, 48 kHz, **180 s**, −18 LUFS, exact kick grid) writes `chill_90bpm.wav`, `upbeat_120bpm.wav`, `cinematic_70bpm.wav`, `lofi_85bpm.wav` + `.json` sidecars `{bpm, mood, source:"procedural", beat_grid_offset}`. `.gitignore` gets `presets/music/*.wav`; `build_app.sh` runs the script before `--add-data presets:presets`; `tests/conftest.py` gets a session fixture that generates them into a tmp `PRESETS_DIR` override when missing; the loader returns `[]` (and the `music` recipe asks for an upload) when none exist. 180 s + `add_music(loop=true)` (§4.9) covers any length.
**`SAFE_ZONES`** (`presets.py`, imported by the verifier **and** by the §4.9 handler defaults): 9:16 → text `y ∈ [0.10, 0.78]`, `x ≤ 0.85` (TikTok/Reels UI covers the bottom ~20% and the right rail); 1:1/4:5 → `y ∈ [0.08, 0.90]`; 16:9 → `y ∈ [0.05, 0.92]`. Exempt roles: `watermark` (lives in the margin by design). Positions are evaluated exactly as the renderer resolves them (`render.text_overlay._y_for_role` + `resolve_anchor_overrides` **[verified: captions anchor at `canvas_h − 0.16·h` = 0.84, `add_text` default 0.85]**) — which is why §4.9 changes the *product* defaults on vertical canvases instead of loosening the check.

### 2.9 Unit tests (P)
`test_prompt_slots.py` (≥140 cases incl. `1080x1920`, `4x upscale`, `make 2 shorts`, `youtube shorts`), `test_prompt_grammar.py` (all benchmark prompts ≥ 0.75; 15 adversarial ≥ 0.6; precedence table), `test_prompt_recipes.py` (three fact fixtures; only allow-listed tools; monotonic stages; **`tighten` on the smoke fixture cuts no kept word — oracle `timemap.source_range_to_timeline`**; `transitions` precede `captions`; `export_preset` on 16:9→reels inserts reframe+fit; `end_card` no-ops with brand; `music` gets `loop=true`; `apply_hook_stack` always has `text`), `test_prompt_validate.py` (unknown tool/arg; every `PLAN_DENY` member; `set_property(path="src")`, `add_effect(params.src)`, `apply_lut(lut_path=…)`, `tts_voiceover(voice="xx_XX-evil")`, `auto_caption(model="someone/huge-repo")`, `add_text(font="../x.ttf")`, `translate_captions(target_lang="fr")`; paths `/etc/passwd`, `~`, `../`, a real `$HOME` file, a WORKDIR file outside the session; sentinels accepted), `test_prompt_presets.py` (beds probe 180 s ± 0.1, BPM ±2 via `ingest.beats.detect_beats`, fonts/LUTs/transitions exist), `test_prompt_schema.py`.

---

## 3. BRAINS (B)

### 3.1 Interface (`brains/base.py`)
```python
BRAIN_IDS = ("recipes", "apple_intelligence", "local_model", "claude")
class BrainUnavailable(RuntimeError): reason: str; fix: str | None
@dataclass(frozen=True)
class BrainRequest: prompt: str; facts: TimelineFacts; recipes: list[RecipeCard]; tool_cards: list[ToolCard]; prior_clarification: dict | None
@dataclass(frozen=True)
class BrainResult: plan: Plan | None; brain: str; ok: bool; reason: str = ""; latency_ms: int = 0; model: str = ""
   # reason ∈ unavailable | timeout | guardrail | refusal | language | context | busy | decode | parse | rejected:<why>
class Brain(Protocol):
    id: str
    def availability(self) -> dict            # {"available", "detail", "fix", "action", "model"}
    def plan(self, req: BrainRequest, *, timeout_s: float) -> BrainResult
    def text(self, task: TextTask, *, timeout_s: float) -> TextResult | None   # §3.7; recipes brain returns None
```
On-device brains (FM, MLX) receive **recipe cards only** (name, one line, slot names + allowed values) and emit `IntentDraft`; tool cards go only to the `claude` brain. `facts_to_prompt_block()` emits basenames only, never paths; ≤ 400 chars.

### 3.2 (a) Apple FoundationModels helper — `tools/fm-planner/`
`// swift-tools-version: 6.2` (required for `.macOS(.v26)`), `platforms: [.macOS(.v26)]`, zero dependencies, `import FoundationModels`. No `URLSession`, `Network`, sockets, `Data(contentsOf:)`.

**Subcommands**
- `fm-planner probe` → `{"ok":true,"available":bool,"state":"available|deviceNotEligible|appleIntelligenceNotEnabled|modelNotReady","fix":…|null,"os":"26.6.2","languages":["en","hi",…]}`. `SystemLanguageModel.Availability` has exactly `.available` and `.unavailable(deviceNotEligible|appleIntelligenceNotEnabled|modelNotReady)` **[verified in the SDK swiftinterface]**; `unsupportedOS` is a Python-side state only. `languages` from `SystemLanguageModel.default.supportedLanguages` **[verified]**.
- `fm-planner plan` ← one JSON object on stdin (≤ 64 KB, else exit 4): `{"prompt", "recipes":[{name, description, slots:{name: "enum|number|text"}}], "timeline_summary", "timeout_ms"}` → `{"ok":true,"draft":{IntentDraft},"model":"apple-fm","latency_ms"}` or `{"ok":false,"code":…,"reason":…}`.

**Generation**: `let model = SystemLanguageModel(guardrails: .permissiveContentTransformations)` **[verified exists]** (the timeline summary contains user speech; the default guardrails refuse ordinary transcript snippets). `LanguageModelSession(model: model, instructions: …)`; `session.respond(to:, generating: IntentDraft.self, options: GenerationOptions(sampling: .greedy, maximumResponseTokens: 500))` **[verified: `sampling:` and `maximumResponseTokens` exist; `temperature: 0` is not greedy]**.
```swift
@Generable struct IntentDraft {
  @Guide(description: "Ordered edits") var intents: [IntentItem]
  @Guide(description: "Things the user said NOT to do") var exclusions: [String]
  @Guide(description: "Only when a required value is missing") var needs_input: [Question]
  @Guide(description: "0..1") var confidence: Double
  @Guide(description: "One sentence to the user") var reply: String
}
@Generable struct IntentItem {
  @Guide(description: "One of the recipe names given") var recipe: String
  var style: String?; var target: String?; var ratio: String?; var platform: String?; var mood: String?
  var look: String?; var count: Int?; var duration_s: Double?; var factor: Double?; var text: String?
  var handle: String?; var name: String?; var lufs: Double?; var words: [String]?
}
@Generable struct Question { var key: String; var question: String; var options: [String]; var default_value: String? }
```
Typed scalars keep constrained decoding honest — no `args_json` string. Python maps `IntentItem` non-nil fields → slots.

**Error map** — exhaustive `switch` over `LanguageModelSession.GenerationError` **[verified: 9 cases]** with `@unknown default`:
| case | code | exit | router |
|---|---|---|---|
| `guardrailViolation` | `guardrail` | 5 | fall through |
| `refusal` | `refusal` | 5 | fall through |
| `unsupportedLanguageOrLocale` | `language` | 7 | fall through; negative-cache that script for the process |
| `exceededContextWindowSize` | `context` | 8 | retry once with ≤ 8 recipe cards, then fall through |
| `rateLimited`, `concurrentRequests` | `busy` | 9 | fall through; 60 s negative cache (a CLI child of a PyWebView app is never the foreground app and Apple rate-limits background callers) |
| `decodingFailure`, `unsupportedGuide` | `decode` | 6 | fall through |
| `assetsUnavailable` | `unavailable` | 2 | mark unavailable 5 min |
| timeout (task group race) | `timeout` | 3 | fall through |
Pre-check: if the prompt contains Devanagari and `"hi"` ∉ `languages`, the adapter skips FM without spawning.

**Python adapter (`brains/fm.py`)**: `availability()` = cached `probe` (60 s); short-circuits when `sys.platform != "darwin"` or `tuple(int(x) for x in platform.mac_ver()[0].split(".")[:2]) < (26, 0)` (tuple compare — `"9.x" < "26"` is False as strings). `plan()` = `subprocess.run([helper,"plan"], input=…, timeout=timeout_s+2, env={"PATH":"/usr/bin:/bin","HOME":home,"TMPDIR":tmp}, cwd=<empty tempdir>, **_pu.SUBPROCESS_FLAGS)` — no `ANTHROPIC_API_KEY`, no `HUGGINGFACE_TOKEN`. `helper_path()`: `VAI_FM_HELPER` → frozen `Path(sys._MEIPASS)/"fm-planner"` → dev `swift build --show-bin-path` output cached in `tools/fm-planner/.binpath` → `platformutil.find_binary`.

**Packaging** (`build_app.sh`, before pyinstaller):
```bash
SDK_MAJOR=$(xcrun --sdk macosx --show-sdk-version 2>/dev/null | cut -d. -f1 || echo 0)
if [ "${SDK_MAJOR:-0}" -ge 26 ] && xcode-select -p | grep -q Xcode.app; then
  (cd tools/fm-planner && swift build -c release --arch arm64) || { echo "fm-planner build failed"; exit 1; }
  FM_BIN="$(cd tools/fm-planner && swift build -c release --arch arm64 --show-bin-path)/fm-planner"
else
  echo "note: macOS 26 SDK/Xcode not present — building without the Apple Intelligence helper"
fi
… ${FM_BIN:+--add-binary "$FM_BIN:."}
```
Loud failure when the SDK is present; explicit skip otherwise. arm64-only (PyInstaller validates collected binary arch). `build_notarize.sh` gains a post-sign assertion: `codesign -dvv "$APP/Contents/Frameworks/fm-planner" 2>&1 | grep -q runtime` when the file exists (`list_nested_machos` already signs it; the assertion proves the hardened signature survived PyInstaller's ad-hoc re-sign order). No entitlements needed. `.gitignore` += `tools/fm-planner/.build/`.

### 3.3 (b) MLX adapter — `brains/mlx_brain.py` + `models.py`
- `pyproject.toml`: `local-llm = ["mlx-lm==<pinned>; sys_platform == 'darwin' and platform_machine == 'arm64'"]`. B's **first** task: `uv lock --extra local-llm` (mlx-lm ≥ 0.31 requires `transformers>=5`, absent from the lock today **[verified]**) and confirm the lock still resolves with the extra unselected on ubuntu/windows; commit the lock. PyInstaller: `--exclude-module mlx --exclude-module mlx_lm --exclude-module transformers`; frozen builds report `fix="Run from source: uv sync --extra local-llm"`.
- **Model resolution honours the default HF cache**: `status(model_id)` uses `huggingface_hub.scan_cache_dir()` / `try_to_load_from_cache(repo_id, "config.json")` under `HF_HOME` (default `~/.cache/huggingface`) — the 7B snapshot already on this Mac **[verified 4.0 GB]** is found, not re-downloaded. `installed` = a `snapshots/<rev>/config.json` exists **and** every `*.safetensors` listed in `model.safetensors.index.json` (or the single file) exists — not `bytes_on_disk > 0`. Sizes are read from the HF metadata when available; the tier table only carries approximate labels ("~4.3 GB", "~1.8 GB").
- Tiers by RAM: ≥ 24 GB → `Qwen2.5-7B-Instruct-4bit`; 12–24 → `Qwen2.5-3B-Instruct-4bit`; < 12 → unavailable with fix. `VAI_MLX_MODEL` overrides.
- **Load is always offline**: `os.environ.setdefault("HF_HUB_OFFLINE","1")` in-process before `mlx_lm.load(str(snapshot_dir))` — never a repo id. `download()` is the only network path, via the loopback-only route: `snapshot_download(repo_id, allow_patterns=["*.json","*.safetensors","*.txt","*.model","*.tiktoken"])` — **no `*.py`** (remote code; Qwen2.5 ships none), `tqdm_class` progress → `set_progress`, `cancel_event` → `JobCancelled`, free-space pre-flight ×1.2.
- **One generation at a time**: a process-wide `threading.Lock` around load+generate; if held → `BrainResult(ok=False, reason="busy")`. Timeout: worker thread + wall clock; on timeout the lock stays held until the abandoned generate returns (MLX has no cooperative cancel), and the brain reports `busy` meanwhile. Unload on `MemoryError`.
- Prompt: chat template, system = rules + recipe cards + compact `IntentDraft` schema; `max_tokens=500`, greedy sampler. Output → `jsonfix.repair` → `IntentDraft` → `recipes.from_intents`.
- **`jsonfix.py`**: `json.loads` first; only on failure apply repairs one at a time, re-parsing after each (fence strip → first balanced object → trailing commas → Python literals → unquoted keys → single-quote conversion **last**, string-aware). Tests include apostrophes (`"don't scroll"`) and Devanagari values.

### 3.4 Router — `brains/router.py`
```python
def plan(req, *, order=None, emit=None) -> RoutedPlan   # emit streams brain{status:"trying"|"answered"|"failed"} per attempt
```
1. `recipes_brain.plan(req)` — instant. `confidence ≥ 0.75` → answered.
2. Else, within a **12 s total planning budget**, try `apple_intelligence` then `local_model` (availability probes run once per process, refreshed every 60 s, and are launched **concurrently with the grammar** so the common path never waits). Each on-device result → `recipes.from_intents` → `validate_plan`; reject → `reason="rejected:…"`, continue; `plan.confidence < 0.5` → continue.
3. `claude` (key set, `VAI_PROMPT_CLOUD != "0"`) may emit raw steps; also validated.
4. Fallback: recipes ≥ 0.4 → run with "I read that as: …"; else `clarify_intent`.
`VAI_BRAIN=recipes|fm|mlx|cloud|auto` pins. Every attempt is emitted as a `brain` event (§4.1) and recorded in `RoutedPlan.attempts`.

`brains_report()` — as drafted, with `detail` counts computed from the live sets (`len(RECIPES)`, `len(PLAN_TOOLS)`), `local_model.detail` distinguishing "not installed (pip)", "installed; model cached at ~/.cache/huggingface", "installed; model not downloaded", and `apple_intelligence.detail` = the probe `state`. Honesty rules as `/api/features`.

### 3.5 Security posture (B + X)
- Brains get no secrets, no absolute paths, no tool cards (except `claude`). The FM helper is a plain child process (stdin/stdout JSON, scrubbed env, empty `cwd`); the source guard (§3.6) is a tripwire — **the boundary is `validate_plan`**, called inside the executor immediately before dispatch.
- Model files stay in the HF cache outside `WORKDIR`; downloads only via loopback-only routes.
- No new middleware/listener; `/api/prompt/*` sits behind `PairAuthMiddleware` and the rate limiter.

### 3.6 Tests (B)
As drafted, plus: `test_prompt_fm_adapter.py` covers every exit code and the Devanagari pre-check; `test_prompt_mlx_adapter.py` proves `HF_HUB_OFFLINE` is set and `load()` receives a directory, the busy lock, and cache discovery against a fake `HF_HOME` tree; `test_prompt_models.py` asserts `allow_patterns` excludes `*.py`; `test_fm_helper_source_guard.py` uses word-bounded patterns (`\bURLSession\b`, `\bNWConnection\b`, `^import Network`, `\bProcess\(`, `\bsocket\(`, `\bconnect\(`, `CFSocket`, `NSURL`, `Data\(contentsOf`, `String\(contentsOf`) — `ProcessInfo` no longer false-positives — and asserts `Package.swift` declares zero dependencies.

### 3.7 Content tasks — `Brain.text()` (`brains/content.py`)
Where an LLM actually adds value over a grammar is content, not routing. `TextTask ∈ {hook_candidates(transcript_head: str, n=3, max_words=7), rank_windows(windows:[{start,end,text}], k)}`; `TextResult{items:[str] | [int], brain}`. Used by: `hook` recipe (when no quoted text) → `apply_hook_stack(text=<candidate 0>)`; `shorts` recipe → passes `rank_hint` (ordered window indices) to `make_shorts` via a new optional arg only if the timemap owner's tools.py exposes it — otherwise the recipe re-orders `make_shorts` output by renaming nothing and simply records the ranking in `reply` (K measures whether it matters). Fallback: `generate_hook`'s heuristic (first sentence upper-cased) with `reply` "hook text: heuristic (no local model available)". `plan.content_brain` records which brain wrote the text; the badge shows "Recipes · text by Apple Intelligence" when they differ. FM `text()` uses `@Generable struct Candidates { var items: [String] }` and the same error map.

---

## 4. EXECUTOR + VERIFIER + CHAT INTEGRATION (X)

### 4.1 SSE event contract (`service.py::EVENT_TYPES`; loop.py docstring updated)
Existing, unchanged shapes: `text_delta{text}`, `tool_use{name,args,id}`, `tool_result{name,result,id,is_error?}`, `op{op}`, `done`, `error{message}`.
New:
- `brain{status:"trying"|"answered"|"failed", brain, label, model?, detail?, latency_ms?}` — one per attempt; the last `answered` is authoritative. Emitted within 100 ms of turn start (recipes is instant).
- `plan{plan}` — validated Plan incl. `downloads_needed`, `estimated_seconds`; re-sent after a clarification.
- `step{index,total,tool,status:"running"|"ok"|"failed"|"skipped",progress?,summary?,effect?:"none",error?}` + per-step `tool_use`/`tool_result` (ids `f"{plan.id}_s{index}"`) so ChatOverlay and the phone render steps as a Claude turn (`chatLog.ts` pairs by id **[verified]**).
- `verify{plan_id, checks:[{check,human,pass:bool|null,measured,expected,unit?,detail?,headline}], passed,total,rendered}`.
- `clarify{token, plan_id, questions:[NeedsInput…], expires_in_s}` then `done`.
- `op` exactly once, after verify (so the preview render does not compete with the verify render for `_RENDER_SLOTS`).
**The first `text_delta` of every turn starts with `"via <Brain label> — "`** so the phone shows which brain answered with zero mobile changes (a stated hard constraint; `brain` events are invisible there). Phone compatibility **[verified]**: `mobile/lib/sse.ts` returns `{kind:"empty"}` for types not in `KNOWN_TYPES`; frames must stay single-line `json.dumps`; `done` last. Desktop `ChatOverlay.tsx` is an else-if chain with no else **[verified]**.

### 4.2 Executor (`executor.py`) — decoupled from the connection
```python
def start_run(store_resolver, sid, plan, facts, *, prompt, history_writer) -> RunHandle   # spawns a daemon thread
def run_plan(store, plan, facts, *, emit, cancel_event, prompt) -> ExecResult
```
- **Execution outlives the SSE stream.** The run thread publishes events to a `RunBus` (in-memory ring + condition variable) and appends them to `<session>/prompt_run.json` (`runlog.py`: `{run_id, plan_id, status, started, steps, verify, reply, op}`) every event. The SSE generator subscribes and yields until `done`; a client disconnect (`GeneratorExit`/`CancelledError`) **only unsubscribes**. Cancellation happens **only** via `POST /prompt/cancel`. Rationale **[verified]**: `mobile/lib/chat.ts` documents "RECOVERY IS ALWAYS history, NEVER A REPLAY … edits its tools applied are already on the Mac's disk" — iOS suspends the app on lock, so cancel-on-disconnect would roll back a 3-minute caption run and lie to the phone's history.
- Lock: acquires `api.locks.session_lock(sid)` for the run and registers `locks.prompt_run(sid) = run_id`. `api/locks.py` = the registry moved verbatim from `main.py` (`_SESSION_LOCKS`, `_SESSION_LOCKS_GUARD`, `_session_lock` **[verified]**), plus `history_lock(sid)`. `main.dispatch` / `_dispatch_async` check `locks.prompt_run(sid)` **before** blocking and return `409 {"code":"prompt_running","run_id":…}`; the desktop shows "Prompt running — wait or cancel". Job workers keep blocking (documented). The store is re-resolved **inside** the lock via `store_resolver(sid)` (the LRU `main._STORES` may evict a captured object; `_dispatch_async` already re-resolves for this reason).
- **Transcript pending**: if `facts.transcript_pending` and the plan has a `transcribe` step, poll `ingest.json` every 2 s for ≤ 30 s (emitting `step{tool:"transcribe", status:"running", summary:"waiting for upload transcript"}`); when it lands, drop the step and rebuild facts.
- **Side-effect snapshot**: before the batch, copy `facts.ingest_json_path` and `<session>/transcript.json` (if present) to `<session>/cache/prompt_snap/<run_id>/`. On rollback (failure or cancel) restore them byte-for-byte. `auto_caption` rewrites `ingest.json` (possibly translating a Hindi transcript to English) before commit **[verified handler order]**, and `batch()` only rolls back the in-memory EDL.
- `validate_plan` again inside the lock; then `with store.batch():` for each step: resolve sentinels against `store.edl` **now**; emit `step running` + `tool_use`; `dispatch(store, tool, dict(args), set_progress=…, cancel_event=…)`; success → `tool_result` + `step ok` (`effect:"none"` when the result reports `cuts==0`/`words==0`/`reframed==[]`/`n==0`); exception → optional: `skipped` + continue; required: re-raise → rollback → restore snapshot → `error{"Step 3/6 auto_reframe failed: <msg>. Timeline unchanged; transcript restored."}` (+ "Created sessions kept: …" if `make_shorts` ran). `cancel_event` between steps → `JobCancelled` → same path with "Cancelled — timeline unchanged."
- After the block: one `store.commit("prompt", {...}, f"Prompt: {title} ({applied} steps)")` — **one op, one undo step**. Then verify (§4.4), then emit `op`, `text_delta` summary, `done`.
- `shorts.finish` children: after the parent commit, for each new session: build facts, `recipes.from_intents([reframe, captions, hook])`, `run_plan` in that session (own lock, own commit, own snapshot). Reported in `exec_result.child_runs`.
- Read-only plans commit nothing; `undo`/`redo` dispatch directly (own snapshots).
- Not in `ASYNC_DISPATCH_TOOLS` and taking no `set_progress` **[verified: `auto_reframe`]** → the executor emits a synthetic time-based progress (`elapsed / RECIPE_COST`) capped at 0.9.

### 4.3 Clarification + resume (`pending.py`, `service.py`)
- Pause → write `<session>/prompt_pending.json {token, plan, prompt, facts_hash, created, expires:+600s, brain}`; emit `text_delta` with the question **including the options and how to answer** ("Which language for the captions? Reply **hi**, **en**, **hinglish** or **es**."), then `clarify`, then `done`. History: user prompt + assistant question text (alternation preserved).
- Resume paths → `service.resume(store, token, answers, ui_state)`:
  1. `POST …/prompt/answer {token, answers}` (desktop card).
  2. Next chat/prompt message on that session: `pending.try_parse_answer(message, pending)` requires a **whole-message match** after normalisation (trim, punctuation, lower-case, Hinglish yes/no `haan/nahi`): exact `value`/`label`/`synonyms` (from `slots.py` tables), ordinals (`first`, `2nd`, `the second one`), `yes/no/go/skip/download` for `confirm` kinds, numbers/durations via the slot extractors for `number/duration` kinds. Anything else **drops** the pending plan (`text_delta "Dropped the earlier question — planning your new request."`) and plans fresh. A partial-string match (`"hi"` inside "make it hi-res") never counts.
  3. Any `op` committed by another route (`/dispatch`, jobs) changes `facts_hash` → pending is invalidated on next read.
- `GET …/prompt/pending`, `POST …/prompt/cancel {token?}` as drafted; cancel also sets the running plan's `cancel_event`.

### 4.4 Verifier (`verify.py`) — `CheckResult(check, human, passed: bool|None, measured, expected, unit, detail, headline)`
`VerifyCtx(store, facts_before, facts_after, render_path|None, exec_result)`; `facts_after = build_facts(store, ui_state, feature_report=facts_before_report)` after commit. All `CHECK_SPECS` names implemented (test).

**Two clocks.** Every number in this table is in one of two coordinate spaces, tagged per row. **Layout** = the EDL's own coordinates: every clip `start`/`end`, caption cue, `Transition.at`, marker and every tool argument. **Render** = the file's clock: `edl.duration`, ffprobe, the frames and samples of the verify render. They differ by the overlap the v1 cross-fades consume — `render_time(t) = t − Σ{d_i : seam s_i ≤ t}` over `EDL.v1_seam_table()` (`render/clock.py`; the desktop's `lib/timelineLayout.renderTime` is the same function). A check may compare layout with layout (`captions_within_extent`) or render with render (`duration_leq` against ffprobe), never one with the other: the EDL-level checks were blind to a 2.4 s overlay drift precisely because both sides of every comparison were layout while the renderer had moved v1 to the render clock.

| check | clock | measured how |
|---|---|---|
| `transcript_present` | — | `dispatch._load_transcript(store)` and `words>0` |
| `captions_nonempty` | — | captions track (or text-track `role=="caption"`) has ≥ 1 clip |
| `captions_cover(min_ratio)` | layout | union of caption `[start,end]` ∩ `facts_after.speech_spans` (both **timeline** seconds — spans come from `timemap.map_segments_to_timeline`) / `speech_seconds` |
| `captions_within_extent` | layout | last cue end ≤ `edl.video_extent() + 0.05` |
| `captions_sync(tol=0.1)` | layout | for the first cue after each v1 seam: the cue moved ≤ `tol` relative to its OWN first word's `timemap`-mapped timeline time between `edl_before` and `edl` (a cue with no pre-run counterpart: within `tol` of that word). Measures drift the run introduced, not Whisper's cue padding — the absolute form flagged 1.6 s on every unchanged cue whose segment starts before its first word |
| `captions_language(target)` | — | script ratios as drafted; `hinglish` needs `facts_before.language=="hi"` else `pass=None` |
| `captions_style(style)` | — | `captions.config.style` **and** `captions_nonempty` |
| `speech_preserved` | layout (source→layout via timemap) | every word that was on the timeline before and is not a filler token is still on the timeline after (`timemap.map_words_to_timeline` before vs after, keyed by source time). **Energy is ground truth, timestamps are estimates:** a vanished word whose SOURCE span lies ≥ 70% inside a `silencedetect` run of the source (the plan's `remove_silences` noise/min-duration, else −30 dB / 0.5 s; measured once per verify, cached on the ctx) is reported as "N transcript words sat inside measured silence (misaligned timestamps) — not counted" and does not fail the check. Measured: the TikTok run's transcript held "than, it, looks, in, photos." at 14.29–16.28 s as five back-to-back 0.40 s spans (whisper's uniform-spacing fallback) over a stretch silencedetect read as silent; the cut was right, the timestamps were not |
| `fillers_remaining_leq(words,max)` | layout | `timemap.map_words_to_timeline(edl,"v1",words)` → count surviving tokens ∈ words |
| `duration_shrank`, `duration_between`, `duration_leq` | **render** | `edl.duration` (= layout end − `transition_overlap()`; what ffprobe reports) |
| `silence_total_leq(max_total_s=1.0)` — "no long pauses remain" | **render** | speech-only verify render (music muted) + `silencedetect` runs; only runs ≥ `min_run_s = min_dur + 2×keep_pad + 0.15 s` (the plan's `remove_silences` args, defaults → 0.85 s) count; `measured` = total of those long runs, `detail` names the longest remaining run. **Not** "≤ 1.0 s of any silence": after `remove_silences` every cut pause keeps 2×`keep_pad` of air by design and sub-`min_dur` breaths were never its job — the TikTok run read 7 × 0.2 s of kept air plus natural pauses as 3.79 s of failure with every dead-air stretch gone |
| `canvas_aspect(ratio)` | — | canvas dims |
| `reframe_effective` | — | `auto_reframe` result `reframed` has no `(skipped` entry **and** ≥1 clip `src` changed, **or** every v1 clip `fit=="cover"` |
| `no_letterbox` | — | for each v1 clip: probe dims aspect == canvas aspect (±2%) **or** `fit=="cover"` |
| `overlays_inside_safe_zone(ratio)` | — | every non-exempt TextClip/Sticker position resolved via `render.text_overlay._y_for_role` + `resolve_anchor_overrides`, inside `presets.SAFE_ZONES[ratio]`; **vacuous when no overlays → `pass=None`** |
| `music_present`, `music_ducked`, `music_within_video_extent` | layout | as drafted |
| `music_covers(min_ratio)` | layout | Σ music clip durations / `video_extent` ≥ ratio |
| `beat_splits_geq(n)`, `min_shot_geq(s)` | layout | v1 boundary count delta; min fragment duration |
| `beat_pulse_present(n)` | layout | ≥ n v1 clips carry scale keyframes whose start lies within 60 ms of a bed beat |
| `hook_text_starts_leq(t)`, `hook_axes_geq(n)` (`headline=false`: satisfiable by `apply_hook_stack` regardless of content **[verified]**) | layout | as drafted |
| `effect_present`, `clip_src_changed`, `speed_equals` (`clip.speed_factor` **[verified property]**), `text_present`, `brand_*`, `transitions_count_geq`, `export_preset_applied`, `vo_present`, `tool_ok` | — | as drafted |
| `loudness_target_set`, `loudness_within(tol)` | render | canvas field; verify render + `ebur128` |
| `shorts_created(count,max_dur,min_dur)` | render (child `edl.duration`) | `exec_result.new_sessions`; each child duration ∈ `[min_dur−0.5, max_dur+0.5]` |
| `shorts_finished` | — | each child has captions + a hook TextClip + 9:16 canvas |
| `audit_ok` | — | `show.audit.audit(edl)`: `ok`, zero error-level issues, `hook_axes.hook_score==3`; the numeric score is reported, not gated |

**Verify render** (`render/verify_render.py::render_for_verify`): `compositor._render(edl, dst, height=360, fps=…, preview=False, cache_dir=…, crf=30, on_progress=…, cancel_event=…)` **[verified kwarg name `on_progress`]**; once per plan; skipped (checks `pass=None`) when `edl.duration > 600`; emitted as `step{tool:"verify_render"}`.

### 4.5 Honest summary (`summary.py`)
`compose_reply(plan, exec_result, verify_result) -> str`, always prefixed `"via <Brain> — "` (+ `"· text by <content_brain>"`). Failed checks first with measured vs expected and a next step; `effect:"none"` steps named ("remove_fillers: nothing to cut"); downloads that happened; child sessions; "Undo with ⌘Z (created sessions are kept)".

### 4.6 Chat integration — `agent/loop.py`
In `chat_turn`, replace the `if not ANTHROPIC_API_KEY: yield {"type":"error", …}` block **[verified at the `ANTHROPIC_API_KEY` check in `chat_turn`]** with delegation to `prompt.service.prompt_turn(store, user_message, history, ui_state=ui_state)`. `prompt_turn`: facts (thread) → pending check → `router.plan` (emits `brain` events) → clarify-intent or `plan` → `start_run` → subscribe → yield events → on `done` return. History handling: on start append `{"role":"user","content":prompt}` and a provisional `{"role":"assistant","content":[{"type":"text","text":"via Recipes — working on: <title> (run p_xxxx)…"}]}`; at completion the run thread calls `history_writer.finalize(sid, run_id, final_text)` which, under `history_lock(sid)`, rewrites the assistant block containing `run p_xxxx` (or appends) — idempotent, so it is correct whether the route's `finally: _save_history` ran before or after completion. `main._save_history` takes `history_lock(sid)`. No tool blocks in history. Cloud path untouched. `_validate_ai_config` copy: "ANTHROPIC_API_KEY is not set — chat and the Prompt bar run on local brains (recipes / Apple Intelligence / local model). Add a key to enable Claude." at `info`.

### 4.7 Routes (`api/prompt_routes.py`, mounted after the chat route; `prompt_routes.configure(resolve_store=_store, history_writer=…)`)
| Route | Body | Returns |
|---|---|---|
| `POST /api/sessions/{sid}/prompt` | `{message, selection?, multi_selection?, playhead?, brain?}` | SSE |
| `POST …/prompt/answer` | `{token, answers}` | SSE |
| `GET …/prompt/pending` | — | `{pending}` |
| `GET …/prompt/run` | — | `prompt_run.json` (current or last) — desktop reconnect |
| `POST …/prompt/cancel` | `{token?}` | `{cancelled}` |
| `GET /api/prompt/brains?refresh=1` | — | `brains_report()` (memo 60 s) |
| `GET /api/prompt/models` | — | `{tier, models:[{id, installed, snapshot_path, bytes_on_disk, expected_bytes, free_bytes}]}` |
| `POST /api/prompt/models/download` `{id}` / `POST …/delete` | loopback-only via `api.auth._is_loopback` **[verified]** → else 403 | 202 `{job_id}` via `JOB_MANAGER.submit(kind="model_download")` |

### 4.8 Tests (X)
`test_prompt_executor.py` (one op; rollback on required failure with `edl.hash()` and `ingest.json` byte-identical after a failing step following `auto_caption(target=en)`; optional skip; cancel via route; **disconnect does not cancel** — drop the subscriber mid-run, assert the commit lands and `prompt_run.json` reaches `done`; sentinels resolve to post-cut fragment ids; `plan` object unchanged; 409 from `/dispatch` while running; `shorts.finish` children each one op), `test_prompt_verify.py` (every check pass/fail on constructed EDLs; timemap clock test; lavfi renders for silence/loudness; `no_letterbox` on contain vs cover), `test_prompt_chat_nokey.py`, `test_prompt_pending.py` (whole-message rule: `"hi"` resumes, `"make it hi-res"` drops; ordinals; `haan`; invalidation by a foreign `op`), `test_prompt_routes.py`, `test_prompt_security_boundary.py` (every §1.3 rule-8 case + `set_property(path="src", value="/etc/hosts")`, `add_effect(params.src)`, `apply_lut(lut_path="/x.cube")`, `add_sticker(emoji=…)`, `tts_voiceover(voice="xx_XX-evil")`, `auto_caption(model="someone/huge-repo")` — rejected before `dispatch` (spy) with `restrict_paths_active()` False, and `huggingface_hub.snapshot_download` / `piper.download_voices.download_voice` patched to fail loudly), `test_prompt_sse_contract.py` (types ∈ `EVENT_TYPES`, single-line frames, `done` last/unique, first `text_delta` starts with `via `), `test_prompt_runlog.py`, `test_dispatch_transcribe.py`.

### 4.9 Dispatch additions (X, after BASE; each with a schema in `tools.py` so `test_tool_schema_completeness.py` stays green)
1. **`transcribe(model="small", force=false)`** — non-mutating: runs `ingest.transcribe` on the first v1 clip's source, writes `ingest.json["transcript"]`, honours `set_progress`/`cancel_event`, refuses models not on disk (`ValueError("model not downloaded")`), commits nothing (no EDL change → returns `{"summary", "words", "language", "backend"}`). Added to `ASYNC_DISPATCH_TOOLS`.
2. **`add_music(loop: bool=false)`** — when the bed is shorter than `video_extent`, lay consecutive clips of the same source back-to-back (`start = k·bed_len`) until the extent is covered, last one trimmed; only the final clip carries `fade_out` (`render/audio_mix.py` has no loop support **[verified: no `aloop`]**, so looping is expressed in the EDL).
3. **Platform-aware overlay defaults**: `auto_caption`/`add_caption_track` `position="bottom"` → `y = SAFE_ZONES[aspect].caption_y × canvas.h` (0.76 on 9:16, unchanged 0.85 on 16:9); `add_lower_third` → 0.74 on 9:16; watermark untouched. The renderer's role anchor for captions (`canvas_h − 0.16·h`) is overridden by the explicit y the handlers now set — X verifies `resolve_anchor_overrides` honours it and adds a render test.

---

## 5. DESKTOP UI (D)

### 5.1 Placement & structure — as drafted (`PromptBar` above `.preview-pane`; `.center` grid gains an `auto` row capped at 38vh), plus:
- `promptStore` reduces `brain` events by `status` (last `answered` wins; `trying` rows show as a ladder in the badge popover), `plan.downloads_needed` and `estimated_seconds` render in the plan header before the first step.
- On mount and on `op` from elsewhere, `GET …/prompt/run` restores a running/finished run (reconnect after reload); the stream is re-attached by calling `POST …/prompt` with `{resume_run: run_id}` — X's route replays the bus from the last seen index.
- `409 prompt_running` from any dispatch → toast "Prompt running — wait or cancel" with a Cancel button (`promptCancel`).
- `lib/promptEvents.ts::streamPrompt` shares the fetch/reader loop with `ChatOverlay` (extract, don't duplicate).

### 5.2 Interaction — as drafted, plus: confirm-kind questions (`downloads`, `go`) render as a two-button card with the byte total / time estimate; Esc during a run **asks** ("Cancel the run? The timeline is unchanged until it finishes.") rather than aborting silently; the brain badge shows `content_brain` when different ("Recipes · text by Apple Intelligence").

### 5.3 Visual direction, 5.4 A11y & tests — as drafted; `promptEvents.test.ts` is driven by a recorded stream (`__fixtures__/prompt_stream.txt`) that includes `brain{trying}`×2, unknown event types, an `effect:"none"` step, and a `clarify` with a confirm kind. `ChatOverlay.tsx` extends its `ChatEvent` union and handles `brain`/`clarify`; header "Chat with Claude" → "Chat" + brain pill.

---

## 6. BENCHMARK (K) — `tests/benchmark/`

### 6.1 Media synthesis
- **Narration**: 12 sentences (~75 s), **single-token** fillers only for the count assertion (`um`×4, `uh`×3, `hmm`×2 = 9) plus one **content** "like" ("I like this part") that must survive; pauses as `[pause 2.0]`. Piper `en_US-amy-medium` (cached); Hindi variant via Piper `hi_IN-priyamvada-medium` if cached, else `say -v Lekha`, else skip. Ground truth from concat offsets.
- **Video**: lavfi 6-scene 1920×1080@30 (hard cuts at known times) + a pre-split variant + 9:16 variant + 20 s b-roll.
- **Music**: `presets/music/upbeat_120bpm.wav` (generated by P's script in the fixture) and a session-uploaded `bench_bed_100bpm.wav`; the fixture stores **librosa's own detected beats** of each bed (`ingest.beats.detect_beats`) alongside the synthesis grid.
- **Harness**: TestClient with `WORKDIR` redirected; `ANTHROPIC_API_KEY=""`; `WHISPER_BACKEND=faster_whisper` pinned (whisper-cli word timings are synthetic — spread evenly per segment **[per transcribe.py]**); `WHISPER_MODEL=small`; **one** uploaded + transcribed fixture session per media variant, **cloned by copying the session dir** for each case (no re-upload, no re-transcribe); `VAI_BRAIN` from env. Clarifications auto-answered from defaults; `downloads` answered **skip** unless the case declares `allow_downloads` (none do by default — the suite must run offline after the fixture models are present); `go` answered `yes`.

### 6.2 The prompts (`prompts.py::CASES`) — assertions computed from the EDL / transcript / ffprobe / verify render using `timemap` directly (a pure function over the EDL — independent of the app's `verify` event, which is asserted to agree)
| # | Prompt | Assertions | Parity note |
|---|---|---|---|
| 1 | "add captions" | captions clips ≥1; cover ≥ 90% of ground-truth speech (timeline); style `ig_chunky`; **no** `auto_caption` step ran (transcript reused); wall < 10 s | Auto captions |
| 2 | "add chunky captions in hinglish" (Hindi variant; `tts` mark; requires MADLAD cached else skip with reason) | ≥ 90% Latin chars; language rewritten | Captions + translate |
| 3 | "remove the silences" | duration shrank ≥ 70% of planted pause time; render silence ≤ 1.0 s | Remove silence |
| 4 | "cut out the ums" | ≥ 8/9 planted fillers gone; content "like" survives; `speech_preserved` | Filler removal (parity+) |
| 5 | "tighten it up and add captions" (fresh clone with transcript **deleted** to force the prerequisite) | 3 + 4 hold; captions cover ≥ 90% **in timeline time**; exactly one transcription step (`transcribe`), zero `auto_caption`; `captions_within_extent` | Smart cut |
| 6 | "make 3 shorts under 30 seconds for tiktok" | 3 sessions; each duration ∈ [11.5, 30.5]; each child 9:16 with captions + hook; parent has one op | Auto shorts |
| 7 | "make it vertical for reels" | 1080×1920; `no_letterbox`; captions (added in a prior step of the case) inside 9:16 safe zone | Auto reframe |
| 8 | "turn this into a tiktok" | 1080×1920; `no_letterbox`; `bitrate_kbps=8000`; `loudness_lufs=-16` | Export preset (+implied reframe) |
| 9 | "add chill background music and duck it under my voice" | music present; `duck.to_db ≤ -12`; **music covers ≥ 95% of the extent** (75 s > one bed loop is not needed at 180 s, but the loop path is exercised on a 200 s fixture in `test_media_synthesis`) | Music + ducking |
| 10 | "cut to the beat of the music" (after 9) | v1 boundaries +≥ 8; ≥ 80% of new boundaries within ±1 frame (33 ms) of **librosa's detected beats** (tempo-octave equivalence allowed); scale keyframes present on ≥ 8 fragments; **frame hash at two beat boundaries differs from the unsplit render**; min shot ≥ 0.8 s; no split inside a ground-truth word | Beat sync |
| 11 | "add a hook in the first 3 seconds" | hook TextClip start ≤ 0.5 s; `apply_hook_stack` received `text` explicitly (event args) | Hook |
| 12 | "give it a cinematic look" | every v1 clip has `lut` effect `teal_orange.cube`, intensity ∈ [0.6,1] — **checked after a preceding silence cut so fragments exist** | LUT |
| 13 | "clean up the audio and normalize to -14 LUFS" | `loudness_lufs == -14`; render within ±1 LU (±1.5 allowed with a written reason in BENCHMARK.md); `noise_reduce` skipped-with-reason if missing | Audio enhance |
| 14 | "speed it up 1.5x" | every v1 clip `speed_factor == 1.5`; duration ≈ 75/1.5 ± 5% | Speed |
| 15 | "cut the first 5 seconds" | duration 70 ± 0.1; first kept word source time ≥ 5.0 | Trim |
| 16 | "add a lower third for Priya Sharma @priya.codes at the start" | lower_third contains name; start ≤ 1 s; handle present; inside safe zone | Lower thirds |
| 17 | "apply my brand kit @quicksolutions.in with #techtips and add an end card" | `brand_kit.handle`; watermark; **exactly one** end-card text in the last 3.5 s | Brand |
| 18 | "add smooth transitions between the clips" (pre-split + captions laid first) | transitions ≥ 5, all `crossdissolve`; captions re-laid after transitions: `captions_sync(0.1)` and `captions_within_extent` | Transitions |
| 19 | "make it good for youtube" | 1920×1080; captions; `loudness_lufs == -14`; hook; `audit_ok` | Auto edit |
| 20 | "complete the video for instagram reels with hindi captions and upbeat music" (MADLAD required → runs with `allow_downloads=false`: captions fall back to as-spoken **and the reply says so**; a second variant with MADLAD cached asserts Devanagari ≥ 70%) | 9:16; `no_letterbox`; music present + ducked + covers; hook ≤ 0.5 s; `loudness_lufs == -16`; `audit_ok`; **one** op; `undo` restores hash; captions in safe zone | Full pipeline |
| 21 | "add a voiceover saying 'Thanks for watching' at the end" | `vo` clip start ≥ duration−4; wav > 0.5 s | TTS (parity+) |
| 22 | "undo that" | ops log `undo`; hash equals pre-prompt | Undo |
| 23 | "make it pop" on a cloned 12-minute fixture (slow tier) | a `go` clarification is emitted with `estimated_seconds > 90` **before** any step; answering `no` leaves the EDL unchanged | Run-time gate |
| 24 | "remove the ums" on a clone whose `ingest.json` has no transcript and `transcript_pending` | the run waits for the (simulated) upload transcript, inserts no caption track, cuts ≥ 8/9 | Prerequisite honesty |
Every case: `brain{answered}` present; first `text_delta` starts with `via `; one `done`; one `op` for mutating cases; app `verify` agrees with the case (`verifier_drift` fails the case); no HTTP egress (a socket-level guard in the harness fails any outbound connection).

### 6.3 Scoring report — per case `{id, prompt, brain, content_brain, pass, assertions, verify_agreement, wall_s, steps, skipped, downloads_needed}`; totals per brain; two headline numbers (fast tier 1–5, 9–18, 21, 22; slow tier 6, 7, 8, 19, 20, 23, 24); the machine line; the **first-use download list** (small: cached; large-v3 3.1 GB; MADLAD 3 GB; Piper hi_IN 60 MB) copied into README.

### 6.4 Markers — as drafted (`benchmark`, `slow`, `tts`, `addopts = "-m 'not benchmark'"`); `test_media_synthesis.py` (unmarked, < 20 s) covers synthesis, ground truth, the 200 s loop fixture and the socket guard.

### 6.5 The honest claim — written verbatim from the report: fast/slow tier numbers per brain; "not covered: object removal, background removal, motion tracking, multicam, licensed music (beds are procedural)"; the first-use download list; "beat sync places pulses and word-boundary-safe splits on detected beats; it does not re-arrange footage".

---

## 7. VERSION + DOCS (K)
Version `0.7.0` in `VERSION`, `pyproject.toml`, `frontend/package.json` (+lock), `mobile/package.json`, `mobile/app.json` (+`ios.buildNumber`). `CHANGELOG.md` **Added / Changed / Security / Docs** as drafted, plus **Changed**: "`add_music` gains `loop`; caption and lower-third defaults sit higher on vertical canvases (safe zone); new non-mutating `transcribe` tool"; **Security**: "plans are validated against an explicit tool/arg/path/enum allowlist before any dispatch; the prompt path never downloads a model or voice without a yes; model downloads are loopback-only; the Apple Intelligence helper is a network-free child process". README section "Edit with a prompt, no API key" states the brain ladder, the offline rule, the download-once model, and the benchmark line. `CLAUDE.md`: sections for `agent/prompt/`, `brains/` + `tools/fm-planner/`, `agent/timemap.py` (two clocks), `tests/benchmark/`; update "Chat loop" and "Testing conventions". `.env.example`: key optional; `VAI_BRAIN`, `VAI_MLX_MODEL`, `VAI_PROMPT_CLOUD`. `docs/PROMPT_EDITOR.md` (examples; the questions you may get; answering from the phone: whole-word replies). `mobile/lib/sse.ts`: one comment naming the five dropped event types.

---

## 8. RISKS + PRE-FLIGHT PER IMPLEMENTER

**All** — rebase on BASE; cite symbols; run `uv run pytest` before and after.

**P** — Pre-flight: dump `/api/tools` from BASE and pin arg tables; confirm `apply_lut` effect type string and `Clip.speed_factor`; **verify whether `auto_reframe(subject_track=False)` actually centre-crops** (one reviewer says it only changes the canvas; its docstring says centre-crop — unverified here; either way the recipe appends `set_clip_fit(cover)` and `no_letterbox` measures the truth); generate beds and check BPM ±2. Risks: grammar over-matching (precedence table + benchmark prompts as fixtures); Hinglish verbs (small tested table).

**B** — Pre-flight: `swift build` the probe on this Mac and record the real availability state; if Apple Intelligence is off, either enable it in System Settings for the spike or ship the FM lane marked "untested on the build machine" in `brains_report`/CHANGELOG; measure `respond(generating: IntentDraft)` latency and refusal rate on the 24 prompts; `uv lock --extra local-llm` **first** and confirm ubuntu/windows resolve; load the cached 7B via a directory path with `HF_HUB_OFFLINE=1` and measure `IntentDraft` validity (target ≥ 18/24 after `jsonfix`); PyInstaller placement + `codesign -dvv … runtime` after `--sign-only`. Risks: FM `busy` under bursts (negative cache); MLX memory next to whisper (unload rule; planning finishes before execution); `transformers` bloat in the resolution (excluded from the .app).

**X** — Pre-flight: write `test_prompt_executor` first (one op, rollback, snapshot restore, disconnect-does-not-cancel, 409); confirm `set_progress` injection from a thread; time the 360p verify render (< 15 s target); confirm `resolve_anchor_overrides` honours an explicit caption `y`; confirm `_is_loopback` under `TestClient`. Risks: the lock held for minutes (409 policy + Cancel button); history finalize races (idempotent rewrite under `history_lock`; test both orders); `shorts.finish` child runs (own commits; documented as not undone by the parent's undo); SSE shapes frozen.

**D** — Pre-flight: record a real stream fixture; `.center` grid at the 900 px floor; `/` binding vs inputs (`engine.ts` capture rules); reconnect via `GET …/prompt/run`. Risks: bar vs ChatOverlay contention (shared `busy`); download button only on loopback origins (`location.hostname` check + `action.loopback_only`); never abort on Esc without confirmation.

**K** — Pre-flight: time `add_caption_track` (< 5 s) and `transcribe(small)` on the 75 s fixture; confirm `detect_shots` finds the 5 cuts; confirm `silencedetect` sees planted pauses ±50 ms; confirm librosa beats on the bed and store them; verify the socket guard trips on a deliberate `urlopen`. Risks: Whisper text variance (≥ 8/9, ground-truth timing); loudness tolerance on short content (widen to ±1.5 with a written reason, never the product check); wall clock — fast tier < 12 min, slow tier reported separately.

Cross-cutting go/no-go: default `uv run pytest` green on macOS/ubuntu/windows; `npx tsc -b --force && npx vite build`; mobile `npm test` untouched and green; `VAI_BRAIN=recipes uv run pytest -m benchmark` ≥ 16/20 fast-tier on this machine with **zero network egress**; `bash build_notarize.sh --sign-only` passes with the helper present; `/api/prompt/brains` honest with Apple Intelligence off and with the 7B already in the HF cache (must say "cached", must not offer a download).