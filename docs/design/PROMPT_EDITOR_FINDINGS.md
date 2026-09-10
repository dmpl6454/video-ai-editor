# Prompt Editor — findings established by EXECUTION before implementation (2026-09-08)

Media: 36 s 1920×1080 stand-in (4 lavfi scenes) + Piper narration with 2.5 s pauses and fillers ("um","uh","like","you know") + 90 s 120-BPM beat bed. Run against the 0.6.0 backend with NO API key.

## What the existing tools already do with no key
auto_caption (faster-whisper large-v3): 9 cues in 13 s. remove_silences: 4 cuts. remove_fillers: 4 cuts. v1 36.0 → 24.2 s. auto_reframe 9:16 with subject tracking → real vertical frame (540×960, no side bars). audio_upload lands music on the music track at −12 dB with ducking. apply_hook_stack adds text + punch-in + fade. set_loudness_target records −16 LUFS (applied at export, not preview).

## Gaps vs CapCut (recipe / ordering / brain — not missing tools)
1. Music bed is auto-fitted to the v1 extent AT ADD TIME and never re-fitted after ripple cuts → run add_music after all cuts (or re-fit); `duration`/`gain_db` args are silently ignored — the arg is `volume_db`; bed default ≈ −18 dB, duck on.
2. Captions generated before cuts keep filler TEXT; captions generated after cuts were laid at SOURCE time (fixed at BASE d06d1c7 via agent/timemap.py; see below).
3. Hook heuristic without a brain rotates canned clickbait ("THIS MISTAKE COSTS MOST PEOPLE EVERYTHING" on a camera review) → hooks must come from the transcript (brain or transcript-derived heuristic).
4. Preview loudness −17.7 LUFS vs target −16 (LRA 16.3): the target applies at export — the verifier/benchmark must measure the EXPORT and say so.
5. Speech vs music balance is close (−18.9 vs −17.9 LUFS windows): ducking exists but the bed is loud; CapCut ≈ 12–15 dB under speech and a quieter bed.
6–7. (fixed at BASE) auto_caption/add_caption_track after cuts overhung the video by 9.7 s and inflated edl.duration; overlays past the v1 extent render over black — verifier postcondition: no overlay ends after the v1 extent.
8. Handlers silently ignored unknown args (add_music gain_db) — plan validation must reject unknown args; schemas are complete at BASE (tests/test_tool_schema_completeness.py pins the census at zero).
9–11. (fixed at BASE) remove_fillers used SOURCE word times as TIMELINE times: after remove_silences it removed 1/4 fillers and cut two stretches of real speech. Now every transcript consumer goes through timemap; cuts by source range go through `_cut_source_ranges` (re-maps through the live EDL after every cut).
12. MLX Qwen2.5-7B-Instruct-4bit (cached in ~/.cache/huggingface/hub): cold load 127 s incl. download, warm load 2.4 s, plan 2.8–6 s. WITHOUT schemas in the prompt: right intent and step order, but every arg name invented and `output_ratio: "16:9"` for TikTok. WITH the real input_schemas + one platform rule: zero unknown args, `ratio: "9:16"`, chose generate_hook, and emitted needs_input for the music path it cannot know. Schema-in-prompt suffices; no constrained decoding. The 0.5B model emits degenerate repeated steps — never select it.
13. Validator rules the 7B run exposed: default filler list (um, uh, like, you know, so basically, hmm) since the model sent ["um"] only; strip/replace any model-authored file path ("/path/to/upbeat_music.mp3") with a needs_input or the library picker — never pass a model-authored path to a tool.
14. Apple FoundationModels: the Swift helper compiles and runs on this Mac (Xcode 26, macOS 26.6.2) but SystemLanguageModel.default.availability == unavailable(appleIntelligenceNotEnabled) — Apple Intelligence is OFF in System Settings; brains_report must say exactly that with the fix.
15. export_srt/vtt/ass deliberately stay SOURCE-timed (WHY block above export_srt_tool); no benchmark case may assert an exported .srt lines up with a cut render — assert on the captions TRACK instead.
16. `_cut_source_ranges(store, track_id, [(src, s, e)])` is the only correct way to remove footage by transcript/source time.
17. remove_fillers decides "already removed" on the UNPADDED word range and reports `words` / `already_removed` alongside `cuts`.

Also: Piper en_US-amy-medium is cached (~17 s of narration in 0.8 s); tests/conftest.py isolates the suite from the developer's settings.json (LAN mode on this Mac would otherwise arm the path allowlist and fail 14 tests); never run two pytest processes concurrently (basetemp pruning).
