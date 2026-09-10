# fm-planner — Apple Intelligence as a network-free child process

The `apple_intelligence` brain of the Prompt Editor (spec §3.2). Python
(`agent/prompt/brains/fm.py`) spawns this binary with a scrubbed environment
(`PATH=/usr/bin:/bin`, `HOME`, `TMPDIR` — never `ANTHROPIC_API_KEY` or
`HUGGINGFACE_TOKEN`) inside an empty temporary working directory, writes one
JSON object to stdin and reads one single-line JSON object from stdout.

Zero SwiftPM dependencies. No `URLSession`, `Network`, sockets, `Process`,
`Data(contentsOf:)` — `tests/test_fm_helper_source_guard.py` greps for them.
The security boundary is still `validate_plan` on the Python side; this file
is a tripwire, not the wall.

## Build

```bash
cd tools/fm-planner
swift build -c release --arch arm64
swift build -c release --arch arm64 --show-bin-path > .binpath   # lets fm.py find the dev build
```

Requires Xcode 26 (`// swift-tools-version: 6.2`, `.macOS(.v26)`).
`build_app.sh` builds it when the macOS 26 SDK is present and bundles it with
`--add-binary`; `fm.py` resolves the binary as `VAI_FM_HELPER` → the frozen
bundle (`sys._MEIPASS/fm-planner`) → `.binpath` → `.build/**/release/fm-planner`
→ `PATH`. `.build/` and `.binpath` are git-ignored.

## Subcommands

### `fm-planner probe`

No stdin. Always exits 0 with:

```json
{"ok":true,"available":false,"state":"appleIntelligenceNotEnabled",
 "fix":"Turn on Apple Intelligence in System Settings → Apple Intelligence & Siri",
 "os":"26.6.2","languages":["de","en","es","fr","ja","ko","pt","zh"]}
```

`state` ∈ `available | deviceNotEligible | appleIntelligenceNotEnabled | modelNotReady`
(exactly the cases of `SystemLanguageModel.Availability`; `unsupportedOS` and
`helperMissing` are Python-side states). `fix` is `null` when available.
`languages` are the `languageCode` identifiers of
`SystemLanguageModel.default.supportedLanguages`; the adapter skips the helper
for a Devanagari prompt when `hi` is not listed.

### `fm-planner plan`

stdin (≤ 64 KB, else exit 4):

```json
{"prompt": "make it vertical for reels and add captions",
 "recipes": [{"name": "reframe", "description": "Change the aspect ratio…", "slots": {"ratio": "enum", "platform": "enum"}}],
 "timeline_summary": "36s 16:9 1920x1080 30fps; 1 clip on v1; transcript en 210 words; no captions; no music",
 "timeout_ms": 8000}
```

stdout on success (exit 0):

```json
{"ok":true,"model":"apple-fm","latency_ms":912,
 "draft":{"intents":[{"recipe":"reframe","ratio":"9:16","platform":"reels"},{"recipe":"captions"}],
          "exclusions":[],"needs_input":[],"confidence":0.9,"reply":"Reframing to 9:16 and adding captions."}}
```

`draft.intents[]` is the `@Generable IntentItem`: `recipe` plus the typed
optional slots `style target ratio platform mood look count duration_s factor
text handle name lufs words` (JSON omits nil fields). Python maps the non-nil
fields to recipe slots (`prompt_text.flatten_fm_item`), never an
`args_json` string. A `text` on a `hook` item that is not the user's own words
is dropped on the Python side (`content.strip_model_hook_text`): the plan
prompt carries no transcript, so a line written here can only be generic —
the router asks `fm-planner text` for grounded candidates instead (§3.7). Generation uses
`SystemLanguageModel(guardrails: .permissiveContentTransformations)` (the
timeline summary carries the user's own speech) and
`GenerationOptions(sampling: .greedy, maximumResponseTokens: 500)`.

### `fm-planner text`

stdin: `{"task": "hook_candidates", "payload": {"transcript_head": "…", "n": 3, "max_words": 7}, "timeout_ms": 5000}`
or `{"task": "rank_windows", "payload": {"windows": [{"start": 0, "end": 12, "text": "…"}], "k": 3}, …}`.
stdout: `{"ok":true,"items":["…"],"model":"apple-fm","latency_ms":400}` — strings for
`hook_candidates`, integer window indices (best first) for `rank_windows`.

## Failures

stdout `{"ok":false,"code":"<code>","reason":"…"}` and the exit status below.
Python reads the JSON `code` first (it distinguishes `refusal` from
`guardrail`) and falls back to the exit status when the body is missing.

| exit | code | `GenerationError` case(s) | router behaviour |
|---|---|---|---|
| 1 | — | usage error (no subcommand) | bug |
| 2 | `unavailable` | `assetsUnavailable`, Apple Intelligence off | mark unavailable 5 min |
| 3 | `timeout` | wall clock won the task-group race | fall through |
| 4 | `bad_input` | stdin > 64 KB or not the documented JSON | reported as `parse` (a bug) |
| 5 | `guardrail` / `refusal` | `guardrailViolation` / `refusal` | fall through |
| 6 | `decode` | `decodingFailure`, `unsupportedGuide`, anything unknown | fall through |
| 7 | `language` | `unsupportedLanguageOrLocale` | negative-cache that script for the process |
| 8 | `context` | `exceededContextWindowSize` | retry once with ≤ 8 recipe cards |
| 9 | `busy` | `rateLimited`, `concurrentRequests` | 60 s negative cache |

## Pre-flight ledger (build Mac, 2026-09-08)

`probe` → `state=appleIntelligenceNotEnabled`, macOS 26.6.2, languages
`da de en es fr it ja ko nb nl pt sv tr vi zh` (no `hi`). The generation
path is therefore untested against the live model on that machine; every
exit-code row is driven through a fake helper in
`tests/test_prompt_fm_adapter.py`, and `brains_report()` shows the rung
greyed out with the fix above until Apple Intelligence is switched on.
