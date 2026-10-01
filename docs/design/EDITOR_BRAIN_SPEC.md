# Editor Brain — Phase 1 specification (authoritative)

Status: SYNTHESIS of the three architect designs (`designs/engineering-first.md`, `designs/brain-first.md`, `designs/product-first.md`), the three judges' verdicts and the three readers' maps (`understand/{prompt-machinery,ai-tools,edl-render-tests}.md`), written 2026-09-29 against `<repo root>` at `262bf3b` plus the uncommitted 0.8.0 release-candidate work. The requirement is the owner's brief (`BRIEF.md`, all 50 sections); the scope is its §46. Nothing in the repository was changed or run while this was written; every claim about existing code cites `file:line` as the readers measured it on the working tree (paths relative to `src/video_ai_editor/` unless they begin with `tests/`, `frontend/`, `tools/`, `docs/`, `presets/` or `/`), and a handful of those lines were re-read read-only for this synthesis (`executor.py:705-719`, `preview.py:273-289`, `schema.py:213-226`, `schema.py:331-338`, `edl/schema.py:518`, `live.py:306-338`, `validate.py:833`).

**Revision 2 (final editor, 2026-09-29 evening)** after the critique round (one editor's review, four BLOCKING and seventeen MAJOR findings, two MINOR). Every BLOCKING and MAJOR finding is applied in the section that owns it; §16 lists each finding, where it landed, and the two that were applied differently from the reviewer's wording, with the reason. Lines re-read for this revision: `main.py:1322-1345` (`_handoff_audio_only` → music lane), `edl/schema.py:948-975` (`Track.type`, `sound_lane`), `render/clock.py:124-135` (sound windows play whole across v1 seams), `dispatch.py:1616-1636` (`cut_range` on any lane, overlay ripple only on v1), `dispatch.py:5024-5040` (`_free_audio_lane` names lanes `a1`, `a2`, …), `dispatch.py:5043-5090` and `9053-9100` (`detach_audio`, `linked_to`, `_follow_pictures` follows MOVES only), `dispatch.py:3016-3035` (`word_emphasis` = 2-word chunks, no per-word colour), `dispatch.py:7175-7205` (`make_shorts` children = one `Clip` each), `ingest/transcribe.py:41, 299, 390-435, 552-556` (`prob = 1.0`, `_VAD_FRAME_S = 0.01`, `_refine_words`, no `--prompt`), `edl/schema.py:176-178` (ONE `interp` per keyframed property; `step` exists), `edl/schema.py:818-945` (`TextClip`, `CaptionLook`, `MusicDuck.track_ref = "a1"` unread), `presets/music/*.json` (four procedural 180 s loops at −18 LUFS), `brains/content.py:617-629` (`_sentence_score` +1.0 per digit).

Reading order for a build lane: §0 (what and why), §2 (architecture), §5 (the mapping table your lane implements), §14 (your lane's files and exit criteria), then the section that owns your rules.

---

## 0. Decision and scope

### 0.1 The decision

**The Editor Brain is a deterministic planner over a cached, on-device Content Graph. Its output is a frozen Edit Decision Plan (EDP) that a pure compiler turns into an ordinary Prompt-Editor `Plan` of existing dispatch tools plus one declared sentinel family, `$brain:<kind>`, resolved in `agent/prompt/live.py` against the live store through `agent/timemap` immediately before each step's dispatch.** The plan travels the unchanged road: `validate_plan` → dry run on the scratch store → K3 net → change card → Apply → one `commit("prompt", …)` → verify. The EDP is written to disk before the dry run, both the dry run and Apply read the same file, so `preview.apply_check` (`agent/prompt/preview.py:273-289`) passes by construction.

This is the engineering-first backbone (all three judges: 41/50, 39/50, 43/50) with the best ideas of the other two grafted on and every fatal flaw the judges named resolved:

| Grafted from | Idea | Lands in |
|---|---|---|
| brain-first §8.1 | Camera Director rule table in priority order (0.4 s backchannel guard, snap to a word gap, min-shot by energy, max-hold wide reset at a sentence boundary, reaction cutaway only during answers ≥ 8 s, overlap and laughter → wide, never into a face-lost angle, a switch that coincides with a jump cut lands AT the cut) | §4.3 |
| brain-first §9.1 | Punch-in rules incl. the question punch-out, scale alternation, face anchoring | §4.4 |
| brain-first §4.4 | Every score a documented pure formula with named terms and an `evidence` list per scene | §3.4 |
| brain-first §5.2 | Closed `reason.code` vocabulary with ONE human template shared by card, run log and OpsLog | §5.2 |
| brain-first §6.2, §7 | Story Planner per mode (reel / podcast / interview) and the nine-axis Hook Engine | §4.1, §4.2 |
| brain-first §11.3 | Seven-axis, mode-weighted Editing Score | §6.3 |
| brain-first §4.8, §12 | Model digest budgets per rung and typed, grounded drafts | §9 |
| product-first §1.6 | Protected set from `ops.json` since the last brain version via `contract_diff.diff`, enforced by a BLOCKING `protected_untouched` check with the three-option clarify | §7.2 |
| product-first §1.5 | Versions as named labels on snapshots `commit()` already wrote; restore is an ordinary undoable op | §7.1 |
| product-first §3.4 | Style Profiles as editable JSON with genuinely distinct knobs and a recorded `touched` set | §8.3 |
| product-first §1.1-1.4, §1.7 | "Edit for me" rail tab, the Plan tab beside Changes, the Inspector's "Edited by the brain", `deferred[]` honesty | §8 |
| product-first §3.7 | Preference signals written from day one | §12 |

Fatal flaws resolved (each judged fatal or near-fatal in at least one design):

1. **Literal clip ids at plan time (product-first §4.3).** `Clip.id` is `uuid4` at creation (`edl/schema.py:518-519`); the clips a punch-in or a reframe key must address are created by the same plan's cuts and splits. Resolved: every clip-addressed or time-addressed decision is a `$brain:*` sentinel resolved per step against the live store (§5.3).
2. **Per-dispatch resolution inside one fan-out (engineering-first §7.2 prose on `$brain:story`).** `resolve_live_args` runs once per STEP and the whole fan-out list is guarded before any dispatch (`executor.py:705-719`); a fan-out is one tool. Resolved: `open_on` and `cold_open` compile to SEPARATE steps (`split_at` fan-out, then `reorder_clips` / `duplicate_clip` / `move_clip`, each resolved against the post-split tree), §5.3.4.
3. **Plan shape that does not scale (brain-first §0 decision 5: 240 steps; `Plan.steps` is `Field(max_length=24)` on a frozen `extra="forbid"` model that IS the wire schema `vai://plan/1`, `schema.py:217-223`).** Resolved: the step cap, `Plan`, `Step`, `BrainId` and `PLAN_DENY` are unchanged; a 450-cut podcast is one `cut_source_ranges` step whose `ranges` arg fans out inside the one batch.
4. **Opening `PLAN_DENY` (brain-first: five tools; product-first: four).** Resolved: no tool leaves `PLAN_DENY` (`schema.py:331-338`). Four narrow, guarded tools (three plan-able, one UI-only) replace what was wanted from `multicam`, `add_clip`, `diarize` and `assign_caption_speakers` (§5.4).
5. **No protected set for manual edits after v1 (engineering-first §12.1).** Resolved by graft §7.2.
6. **Preview cost of up to three dry runs plus a verify render per preview (brain-first §11.1).** Resolved: at most ONE revision round (two dry runs) and the verify render only where the plan already needs one (§6.2).
7. **Destructive `multicam(replace_v1)` and re-encoding `auto_reframe(subject_track=true)`.** Never emitted by a brain plan (§5.4, §4.5).
8. **No dialogue sound lane (critique, BLOCKING).** Revision 1 analysed the recorder WAV but never said what PLAYS: today an audio-only upload lands on the MUSIC lane (`main.py:1322-1345`, `routed_to: "music"`, −12 dB), and every sound lane starts at `render_time(start)` and plays its whole duration across v1 seams (`edl/schema.sound_lane`, `render/clock.sound_windows`), so the reference audio desynchronises at the first cut and every camera switch would otherwise switch microphones. Resolved: the dialogue lane is a first-class decision (`dialogue`, §4.6.1) executed by one new idempotent tool, `sync_dialogue_lane` (§5.4), which mutes the camera-mic audio of every v1 angle piece and REBUILDS lane `a1` from the v1 layout (one abutting `Clip` of the dialogue source per v1 piece, offset-mapped, 5 ms fades at every internal seam) as the LAST stage-2 step of every brain plan. Every v1 structure change (keep, cuts, splits, reorder, duplicate, move, angle swaps) therefore needs no lane pairing inside five tools; `dialogue_in_sync` is a BLOCKING EDL-only check (§6.1); a brain plan never adds a v1 transition while a dialogue lane exists (a cross-fade shortens the picture but not a sound lane).

### 0.2 Scope: Phase 1 exactly as brief §46

| §46 item | Delivered as | Section |
|---|---|---|
| speech transcription | whisper.cpp first: the brain's own analysis pass runs with a disfluency-biased `--prompt` and reads token probabilities (`p`) into `w.prob`; the caption pass uses the best cached model (§4.7.4); the speech layer of the graph | §3.2, §4.7.4, §9 |
| silence removal | `tighten` pass → `cut_source_ranges` via `$brain:cuts`; every cut edge sits on an energy trough of the 10 ms envelope outside every kept word (§4.6.2); protected pauses are kept with a reason (§4.6.3) | §4.6, §5.3 |
| filler-word removal | same step, reason codes `filler` / `soft_filler` / `filler_acoustic` (lexical + acoustic detector, §3.4) | §4.6 |
| dialogue sound (owner constraint, critique) | `dialogue` decision → `sync_dialogue_lane`: the recorder (else the reference angle's own microphone) on lane `a1`, camera-mic audio muted, 5 ms seam fades, rebuilt from v1 as the last stage-2 step | §4.6.1, §5.4, §6.1 |
| smart jump cuts | false starts, verbatim repeats, dead air, weak questions, technical stretches — each a `cut_range` decision with a reason | §4.6 |
| speaker detection | speakers layer: numpy MFCC + k-means (exists in the `.app`), pyannote adapter when a token exists; roles host/guest; angle hints | §3.3 |
| multicam switching | Camera Director → `apply_camera_plan` via `$brain:camera` (never clears v1); anticipatory switches at the first-word onset minus 3 frames, jump cuts hidden behind an angle change, angles as ordered file lists with per-file offsets | §4.3, §5.4 |
| hook detection | nine-axis Hook Engine (a number counts once; candidacy needs a claim, contrast, question or imperative; flat delivery penalised) → `cold_open` / `open_on` (reels only) / `hook_card` | §4.2 |
| highlight detection | `select` pass over graph scores; `virality` per scene with range + score + reason | §4.1, §3.4 |
| automatic short creation | `edit` with `count ≥ 2`: one EDP with `children[]`, `make_shorts(from_timeline=true, ranges=)` copies the parent's v1 AND a1 pieces inside each window into a child session, `_finish_children` runs each child's compiled sub-plan | §4.1.4, §5.3 |
| dynamic captions | caption modes → `add_caption_track` + `set_caption_style` + speaker turns through `timemap`; Viral = per-word highlight (`TextClip.emphasis`, lane EB-11b), Dynamic = meaning-based keyword accent; a "words to check" list on the Plan tab | §4.7 |
| smart punch-ins | emphasis pass → `add_keyframe` via `$brain:punch_ins`: push in at the clause start, hold through the sentence, release as a step at the next seam | §4.4 |
| music selection | mood from the graph → the four bundled beds or the named upload; episodes get intro + outro (+ chapter stings), a bed under speech only when asked; levels relative to measured speech loudness | §4.8 |
| beat detection | bed sidecar grids; numpy onset autocorrelation for uploads in the `.app`; alignment of punch-ins and the hook card only | §4.8 |
| basic B-roll suggestions | `broll` pass → markers + `suggestions.json` + panel "Place" (a person places) | §4.9 |
| natural-language timeline revisions | `revise(graph, previous_edp, request)` → delta EDP, protected set, contract licence | §7.3 |
| automatic 9:16 reframing | `auto_reframe(subject_track=false)` + `set_clip_fit(cover)` + face-follow `x`/`y` keyframes via `$brain:reframe_pans`; no re-encode | §4.5 |

Target content: podcasts, interviews, talking-head reels. The `classify` pass reports `project_type`; anything else ("this looks like a product demo") is edited as a talking head and the card says so.

### 0.3 The INBUILT constraint, made concrete

With no cloud key, no HF token, no `mlx_lm`, on the packaged macOS `.app` (which excludes librosa, torch, pyannote, mediapipe, faster-whisper — ai-tools map §3, `build_app.sh:152-173`), "edit this podcast like a premium podcast" must produce a complete edit on: ffmpeg, whisper.cpp + Metal (the only transcription route in the `.app`, ai-tools map §2.1), OpenCV Haar (bundled with cv2), numpy, and Apple Intelligence when enabled (`agent/prompt/brains/fm.py`; answering on this Mac, `bench/reports/latest.md` 2026-09-28 cases 8, 11, 19, 20). Therefore:

- The **recipes rung is a complete editor, not a degraded one**: every score in §3.4 is a pure formula over transcript + FLAC PCM + Haar boxes; the Story Planner, Hook Engine, Camera Director, Framer, captions, music and B-roll passes run on rules alone.
- Apple Intelligence, the MLX model and Claude contribute only typed, schema-checked, grounded annotations (§9) that are frozen into the semantic layer or the EDP at plan time. A "model off" test pins that stubbed models yield the recipes EDP byte for byte (§13.5).
- Nothing new is downloaded. The only first-use download a brain run may trigger is the existing consented `downloads_needed` gate for whisper ggml `small`.
- Diarization and sync are ported to numpy so they exist in the `.app` (`ai/diarize._heuristic_diarize` at `ai/diarize.py:186` needs librosa today; `ai/multicam._audio_offset` at `ai/multicam.py:39` returns 0.0 without librosa).

### 0.4 ON the existing editor, made concrete

- Every decision becomes an existing dispatch op or one of four new narrow tools (§5.4), executed by `executor.run_plan` (`executor.py:906`) inside ONE `EDLStore.batch()` (`edl/snapshot.py:318`) ending in ONE `commit("prompt", …)` (`executor.py:1107`): one op, one snapshot, one ⌘Z.
- The change card stays diff-derived (`changes.summarize`, `changes.py:180`; `covered ⊇ diff_keys` proved in `tests/test_prompt_preview_changes.py`); decisions add a "why" column keyed by the same entities, never replace the list.
- Nothing flattens: a brain run writes clips (`in`/`out`/`start`/`src` for angle pieces), transform keyframes, caption cues and config, text clips, music clips, duck, markers and canvas. No `Clip.src` is rewritten to a `cache/` derivative by a brain plan (`tests/test_brain_never_flattens.py`).
- `edl/schema.py`, `RENDER_BEHAVIOR_VERSION` (31), the frame-map goldens and the `.vae` manifest are unchanged in Phase 1 except that `.vae` bundles a new `brain/` directory and lane EB-11b adds two optional text fields (`TextClip.emphasis`, `CaptionLook.accent`) with one RBV bump whose renders differ only when a field is set (§4.7.3).

### 0.5 Deferred to Phases 2-4, and where the hooks live

| Brief § | Deferred capability | Phase | Hook left in Phase 1 |
|---|---|---|---|
| 8, 9, 10, 11 | Entertainment, tech, business, news editors | 2 / 3 | `classify` reports `project_type` from a closed enum that already reserves the names; Style Profiles carry `content_types[]`; news-mode safeguards designed in §11.6 |
| 16 | Graphics engine (quote/stat/topic cards) | 2 | the `topics` layer with titles; `graphics_min` pass owns lower thirds and the hook card and is where cards will be added |
| 17 | Emojis in captions | 2 | per-word highlight and meaning-based keyword accent are PHASE 1 (lane EB-11b, §4.7.3, gated before E3); only emojis defer |
| 18 | Blink / expression awareness | 2 | `visual` layer stores `face_lost_frac` per span; the `faces` capability is a Gateway provider slot (mediapipe is installed and unused, ai-tools map §2.7) |
| 20 | Dialogue isolation, de-reverb, EQ, per-class mixing | 2 | the dialogue LANE exists in Phase 1 (`a1`, §4.6.1) so a Phase-2 dialogue chain has one lane to process; `audio` layer stores noise floor and clipping; `finish` pass owns `noise_reduce` / loudness |
| 6, 18 | Stacked two-up ("split-screen") for vertical multicam resets | 2 | vertical multicam reels never use the wide (§4.5); a two-up needs an overlay placement tool that stays denied in Phase 1 |
| 22 | SFX | 2 | the `sfx` control exists (disabled with its reason) so `BrainBrief.sfx` is stable |
| 23 (user-defined), 24 | User style profiles UI, "Learn my style" | 2 | profiles are files a person can copy today; `feedback.jsonl` is written from day one (§12) |
| 25 | Multiple brand brains | 2 | existing brand kit applied by `finish` |
| 5 | Music structure (downbeats, drops, sections, modes) | 2 | `music` layer stores `energy_curve` and `sections` (quiet/normal/loud) |
| 15 | Stock / generated B-roll, CLIP ranking, automatic placement | 2 | `broll_suggest` decisions carry `candidates[]` with scores; placement is the panel's `add_clip` UI dispatch; a narrow `place_broll` tool is designed but not built (§4.9) |
| 35 | Semantic asset search | 2 | `search_media` stays denied; the B-roll bin index is the seed |
| 33 | Per-platform sets (10 Reels + 10 Shorts + …) | 2 | `children[]` with the no-repeat rule generalises to sets |
| 7 | J/L cuts | 2 | needs `detach_audio` + per-cut offsets; the sound-lane rules exist (RBV 28-31, edl map §1.2) |
| 49 | Versions A/B/C from one prompt | 4 | `versions.json` + `restore_version` + "Try another" (next-ranked story) are the primitives |
| 28 | Claim / visual mismatch review | 3 | §11.6 |

---

## 1. Goals and non-goals

### 1.1 Goals

- **G1 INBUILT.** §0.3 holds on a packaged `.app`; the E11 eval (§13.4) simulates it with `VAI_BRAIN=recipes`, `ANTHROPIC_API_KEY=""`, `HF_HUB_OFFLINE=1` and librosa/torch/mlx import-blocked.
- **G2 One run, one op.** Every brain run is one `store.batch()`, one `commit("prompt", …)`, one ⌘Z, one change card, judged by the K3 net before its single commit, exactly as a recipe plan is today (prompt-machinery map §6.1, §6.2).
- **G3 Never flattens.** §0.4.
- **G4 Deterministic.** `plan(graph, controls, style, seed)` is pure; planner goldens pin the EDP byte for byte; Apply reproduces the preview's fingerprint by construction.
- **G5 Analyse once.** Per-source layers are computed once per (file identity, layer, params, engine version), read from the instant-preview proxies (`ingest/proxy.py:16`, key `sha256(realpath, size, mtime_ns, recipe)`), cached across sessions and across the child sessions `make_shorts` creates. A second reel from the same podcast plans in seconds.
- **G6 Reasons.** Every decision carries `{kind, ref, reason{code, facts, text}, score, confidence}` (brief §45); `facts` are ids of real graph nodes; the reasons survive into the session, the card, the Inspector, the run log and the `prompt` op's args.
- **G7 Budget.** A 60-minute, 3-camera + audio-recorder podcast analyses in ≤ 10 min cold on an M1 Pro (≤ 6 min on the M4 Max; the extra 2 min over revision 1 is the prompted ASR pass of §4.7.4 when the upload's unprompted transcript cannot be reused), plans in ≤ 3 s, compiles in ≤ 1 s, dry-runs in ≤ 60 s, peak analysis RSS ≤ 2.5 GB (§10.3).
- **G8 Revisable and protected.** "Remove the joke", "keep Guest B on screen longer", "make the first 10 seconds faster", "convert this into a 30-second version" re-plan against the same graph and the previous EDP, touch only what they name, and never move anything the person changed by hand — since the last brain version, or, on the first run, anything they placed before it (§7.2).
- **G9 Judged, on real footage.** An Editing Score is computed on-device for every run (§6.3, with its planner-objective axes labelled as such), and Phase 1 EXITS only through the real-footage tier of §13.6: ≥ 3 real episodes and ≥ 5 real talking-head reels with an editor's marked cut list, hooks and angle choices, blind-rated. The lavfi/TTS fixtures prove the machinery; the real tier proves the editing.
- **G10 Honest.** The card names the rung that wrote each contribution, lists what was not done and why (`deferred[]`), and every degradation (no faces, no speakers, sync unverified, semantic layer partial, dialogue lane stale, angle assignment uncertain) is a fact in the graph and a line on the card.
- **G11 One voice.** When a recorder or a reference microphone exists, dialogue plays from ONE source on lane `a1` for the whole programme; a camera switch never switches microphones; every dialogue seam has a fade and no seam falls inside a word (§4.6.1, §4.6.2, §6.1).

### 1.2 Non-goals (Phase 1)

- **N1.** No new LLM planner that emits tools; on-device brains keep answering recipe cards and content tasks only; the `claude` rung keeps its existing privileges (`schema.py:13-17`, `cloud_plan.py:1`).
- **N2.** No change to the prompt machinery's limits: 24 steps, 4 questions, 20 postconditions, one plan per run, sentinel-only late binding.
- **N3.** No change to `PLAN_DENY`; `multicam`, `diarize`, `assign_caption_speakers`, `find_broll`, `add_clip`, `search_media`, `set_property`, `add_sticker`, `add_effect`, `motion_track` stay unreachable from any plan.
- **N4.** No automatic B-roll insertion; suggestions only.
- **N5.** No new ML weights; no download beyond whisper ggml through the existing gate.
- **N6.** No change to `edl/schema.py`'s renderable fields or `RENDER_BEHAVIOR_VERSION` except lane EB-11b's two optional text fields (`TextClip.emphasis`, `CaptionLook.accent`) and their one RBV bump (§4.7.3); the `Clip`, `Track`, `Transform` and keyframe models are untouched (the dialogue lane is an ordinary `audio` track of ordinary `Clip`s).
- **N7.** No change to how the key chat (`agent/loop.py`) or MCP reach `dispatch()`.
- **N8.** No blink, expression or shot-size classification beyond face count and size; no J/L cuts; no SFX; no graphics beyond lower thirds and the hook card.
- **N9.** No Versions A/B/C in one run; "Try another" plans the next-ranked story as a second pending card, never two applied timelines.

---
## 2. Architecture — brief §39 mapped onto the app's real modules

### 2.1 The brief's boxes and where each lives

| Brief §39 box | Phase-1 module(s) | Existing code it stands on |
|---|---|---|
| USER PROMPT | Prompt bar sentence or the "Edit for me" panel (§8); both arrive as `POST …/prompt` with `ui_state.brain_controls` | `agent/prompt/service.py` (prompt-machinery map §0, §6.3) |
| ORCHESTRATOR AGENT | `agent/prompt/planner.py` + the router ladder, unchanged; the new `edit` recipe's expander `_x_edit` (`agent/prompt/brain_expanders.py`) orchestrates analyse-gate → plan → compile | `planner.plan` (`planner.py:2399-2570`), `brains/router.py:191-350` |
| VIDEO BRAIN | `brain/analysis/visual.py` (Haar faces, motion, shots on 720p proxy frames, lazy by range), `brain/analysis/sync.py` | `ai/reframe._detect_subject_centers` (`ai/reframe.py:26`), `ingest/scenes.detect_shots` (`ingest/scenes.py:21`), `ai/multicam._audio_offset` (`ai/multicam.py:39`) |
| AUDIO BRAIN | `brain/analysis/{pcm,audio,speakers,music}.py` (10 ms RMS envelope, VAD, silences, loudness, own-mic energy, laughter proxy, acoustic filler detector, numpy MFCC diarization, beats) | `ingest/proxy.py` FLAC chunks, `ingest/transcribe.py:299-435` (`_VAD_FRAME_S = 0.01`, the voicing frames), `ai/diarize.py:186-271` (k-means seed 42), `presets/music/*.json` grids |
| LANGUAGE BRAIN | `brain/analysis/{speech,semantic}.py` + `brain/gateway.py` (`llm_json` capability) | `ingest/transcribe.py:882`, `ai/shorts._sentences` (`ai/shorts.py:82`), `brains/content.py:635-723`, `fm.py`, `mlx_brain.py`, `cloud_plan.py` |
| CONTENT GRAPH | `brain/graph.py`, `brain/schema.py`, `brain/store.py` → `WORKDIR/analysis/<src_key>/…` + `<session>/brain/graph/<gid>.json` | `agent/prompt/artefacts.py` key rule (`artefacts.py:123 file_identity`), `render/cache_budget.py` LRU |
| STORY PLANNER | `brain/planner/{classify,tighten,select,story,hooks}.py` | `ai/shorts._score_window` (`ai/shorts.py:200`), `content.heuristic_hook_candidates` (`content.py:635`) |
| EDITOR AGENT | `brain/planner/{camera,emphasis,captions,graphics,music,reframe,broll,finish}.py` → `brain/compile.py` | `TOOL_STAGE` (`schema.py:346-393`), `recipes.pc` postconditions |
| EDIT DECISION LIST | the EDP, `<session>/brain/decisions/<did>.json`, frozen before the dry run | — (new) |
| TIMELINE ENGINE | `validate.validate_plan` → `executor.run_plan` → `dispatch()`; `$brain:*` resolved by `brain/resolve.py` from `live.resolve_live_args` | `validate.py:791-837`, `executor.py:906-1115`, `live.py:306-338`, `agent/timemap.py:177-256` |
| FIRST EDIT | the dry run's scratch tree (`preview.scratch_store`, `preview.py:125`) | `executor._preview_run` (`executor.py:1468`) |
| REVIEWER AGENT | `brain/review.py` + `brain/checks.py` (new `CHECK_SPECS` names) + `brain/score.py` | `verify.py:1709-1781`, `show/audit.py:82`, `render/verify_render.py` |
| REVISION AGENT | `brain/revise.py` (bounded: one round pre-Apply; NL revisions post-Apply) | `executor._pause_after_rollback` pattern (`executor.py:1259-1319`) |
| EXPORT | unchanged | `render/compositor.py` |

### 2.2 The pipeline, end to end

```
 uploads ─► ingest (normalize, ingest.json, background whisper `small`, main.py:1300) ─► ingest/proxy.py (720p all-intra spans + FLAC chunks)
                                                                                              │ the ONLY media the analysers read
                                                                                              ▼
 ┌──────────── brain/analysis (numpy + ffmpeg + cv2 + whisper.cpp; jobs.py on the ANALYSIS cancel scope, niced, ≤ 2 concurrent) ────────────┐
 │ sync.py      offsets per FILE vs the reference (FFT cross-correlation on FLAC, anchors every 5 min, drift)      → angles.json              │
 │ speech.py    words (prompted pass, token p) → sentences, fillers (lexical + acoustic), false starts, repeats,   → speech/<params>.json    │
 │              weak questions, dead air, words to check (reference transcript)                                                             │
 │ speakers.py  utterances → 13 numpy MFCC → k-means(seed 42) → turns, roles, angle hints (own-mic first)           → speakers/<params>.json  │
 │ audio.py     RMS envelope 100 Hz (10 ms), VAD, silences, loudness, per-angle own-mic energy, laughter proxy      → audio/<params>.json     │
 │ visual.py    Haar boxes 1-2 Hz, motion, shots — LAZY by kept range (graph.ensure_visual)                        → visual/<params>.json    │
 │ music.py     bed grids (sidecar), beats for uploads (librosa | numpy onset), energy curve, sections             → music/<params>.json     │
 │ semantic.py  heuristic scores ALWAYS; Gateway llm_json annotations with provenance, budgeted                    → semantic/<params>.json  │
 └───────────────────────────────────────────────────┬────────────────────────────────────────────────────────────────────────────────────┘
                                                     ▼  brain/graph.py: assemble layers + scenes + project overlay, validate, digest
                            WORKDIR/analysis/<src_key>/<layer>/<params>.json (per SOURCE, shared)      <session>/brain/graph/<gid>.json
                                                     │
 prompt + controls ─► router ladder (recipes / FM / MLX / claude) ─► IntentDraft{recipe:"edit", slots} ─► brain_expanders._x_edit
                                                     │  (analysis gate: required layers present? else `gate_analysis` question)
                                                     ▼
                            brain/planner.plan(graph, controls, style, seed) ─► EDP ─► <session>/brain/decisions/<did>.json  (FROZEN)
                                                     │
                                                     ▼
                            brain/compile.py  EDP ─► Plan: existing tools + `$brain:<kind>` steps (≤ 24), postconditions, questions
                                                     │
 validate_plan ─► dry run on preview.scratch_store (live.py → brain/resolve.py reads the frozen EDP, maps refs through timemap, fans out)
                                                     │
                            brain/review.py on (after-tree, graph, verify render if ≤ 600 s) ─► issues + Editing Score
                                                     │  high-confidence fixes? ─► brain/revise.amend ─► EDP' ─► compile ─► ONE more dry run
                                                     ▼
                            K3 net (blocking checks + contract with the EDP as licence) ─► change card: Changes (diff) + Plan (decisions, why, score)
                                                     │  Apply: same plan, same EDP file, live store, base_hash + apply_check
                                                     ▼
                            ONE commit("prompt", {prompt, plan_id, steps, decisions: "<did>", score}) ─► versions.json ─► verify ─► reply
                                                     │
                            feedback.py: later human ops diffed against the EDP's resolved footprint (§12)
```

### 2.3 The core trick, stated once

The brain never decides at run time. It decides once, on the graph, writes the EDP, and the plan contains only:

1. literal steps of existing tools with literal args (`auto_reframe(subject_track=false)`, `set_clip_fit`, `add_caption_track`, `set_caption_style`, `apply_hook_stack`, `add_music`, `set_duck`, `fit_music_to_video`, `set_loudness_target`, `apply_export_preset`, `audit_aesthetic`, `make_shorts`), and
2. `$brain:<kind>` sentinel steps whose args name the EDP id (`plan_ref`) and the decision kind. `live.py` resolves each against the LIVE store immediately before that step's dispatch by mapping the EDP's source-clock references through `timemap.source_to_timeline` / `source_range_to_timeline` (`agent/timemap.py:177, 219`) per source, and returns either one arg dict or a LIST of arg dicts — the fan-out shape `resolve_live_args` already returns for `$fit_best` (`live.py:306-325`) and the executor already loops over, guarding each (`executor.py:705-719`).

So a podcast edit is ~12 steps, the card shows every change (the diff, not the plan), every decision is visible in the EDP and the Plan tab, and Apply is deterministic because the EDP is a file.

### 2.4 Two clocks, made explicit

The graph has ONE clock: **reference seconds**, the source seconds of the reference audio (the audio-recorder upload when present, else angle A / the first v1 clip's file — the transcript of record, ai-tools map §0.4, `dispatch.py:1146, 6664`). Every other source carries `sync_offset_s` (positive = that file lags the reference). Every EDP reference is `{src, t0, t1}` in THAT source's own file seconds, derived from reference seconds by the offset at plan time, so the compiler and `brain/resolve.py` only ever call `timemap.source_range_to_timeline(edl, "v1", s0, s1, src=path)` per source. Nothing in the brain stores a timeline second; a timeline second exists only inside a resolved arg dict for one dispatch.

**The dialogue lane, on the same clock.** The reference audio is not a bed: when a brain plan runs on a multicam group, or on a single camera whose own audio is the transcript of record, `sync_dialogue_lane` (§5.4) lays the dialogue source on lane `a1` (type `audio`, label "Dialogue"; `_free_audio_lane`'s naming, `dispatch.py:5024-5040`; also the lane `MusicDuck.track_ref` already names by default) as ONE `Clip` per v1 piece — `src` = the dialogue file, `in_` / `out` = the piece's source span mapped through the angle→dialogue offset, `start` = the piece's `start`, `audio.fade_in = fade_out = 0.005` at every internal seam, `linked_to = None` — and mutes the camera-mic audio of every v1 angle piece (`audio.mute = True`, gain preserved, the `set_clip_muted` semantic). Because a sound lane's run "starts where its first clip's start plays and stays back to back" (`render/clock.sound_windows`) and a brain plan adds no v1 transitions while `a1` exists, layout time equals render time on both lanes and a1 plays exactly the reference seconds the pictures show. The lane is REBUILT, never edited: the tool owns every `a1` clip whose `src` is the dialogue file, drops them, and lays the new set from the current v1 layout, so it is idempotent and runs last in stage 2 after every structural step (§5.5). Captions, `no_cut_mid_word` and the speech facts map the reference transcript through lane `a1` when it exists (`timemap(track_id="a1", src=<dialogue>)`, offset 0 — one clock, no shifted copies needed) and through v1 otherwise.

Two existing readers change so multi-source timelines keep working after `apply_camera_plan`: `dispatch._load_transcript_with_source` (`dispatch.py:6664`) gains a sibling `_transcripts_by_source(store) → {src: (Transcript, offset)}` that also reads `angles.json`, and `add_caption_track`'s `_timeline_segments` plus `remove_fillers` (`dispatch.py:2961, 4156`) map each v1 `src`'s transcript through `timemap(src=)`. The sync stage additionally writes the reference transcript, time-shifted, into each angle's `ingest.json` (`transcript_origin: "brain_sync:<gid>"`), so every pre-existing consumer that reads "the first v1 clip's transcript" still finds words on whichever angle sits first. `assign_caption_speakers` (`dispatch.py:6247`, which compares source-time turns with timeline-time cues at `:6273-6281`) is not used by the brain; the `speakers` arg of `add_caption_track` tags cues at build time through `timemap` (§4.7).

### 2.5 New package layout (`src/video_ai_editor/brain/`, no file over 800 lines)

| File | Responsibility |
|---|---|
| `__init__.py` | `ANALYSIS_VERSION`, `PLANNER_VERSION`, `EDP_VERSION`; public `analyse`, `plan`, `compile`, `revise` |
| `schema.py` | Pydantic layer models, `Graph`, `Scene`, `Decision`, `EDP`, `Review`, `Version`; canonical digest; size caps; `extra="forbid"` everywhere |
| `store.py` | `WORKDIR/analysis/<src_key>/…` atomic read/write, identity + content keys, `refs.json`, LRU class registration, `.vae` bundle helpers |
| `graph.py` | assemble layers + scenes + project overlay, `ensure_visual(src_key, ranges)`, `load(gid)`, per-layer status |
| `gateway.py`, `tasks/*.schema.json` | capability registry, providers, probes, budgets, JSON tasks with schema + repair + provenance (§9) |
| `digest.py`, `drafts.py` | per-rung digests; typed drafts and grounding |
| `analysis/{pcm,sync,speech,speakers,audio,visual,music,semantic}.py` | the analysers (§3) |
| `lexicon.py`, `energy.py`, `reasons.py` | per-language lexicons; the Energy 1-10 threshold table; the closed reason-code vocabulary and templates |
| `planner/{__init__,classify,tighten,seams,dialogue,select,story,hooks,camera,emphasis,captions,graphics,music,reframe,broll,finish,revise}.py` | one pure pass per file (§4); `seams.py` = trough snapping and protected pauses (§4.6.2-3), `dialogue.py` = the `dialogue` decision (§4.6.1) |
| `analysis/fillers.py` | the acoustic filler detector (§3.4), consumed by `speech.py` |
| `styles/{premium_podcast,viral_reel,luxury,clean_professional}.json`, `styles.py` | Style Profiles (§8.3) |
| `compile.py` | EDP → steps, postconditions, questions, notes; asserts ≤ 24 steps |
| `resolve.py` | `$brain:*` resolution against the live store through `timemap`; footprint recording; caps and notices |
| `review.py`, `score.py`, `revise.py`, `checks.py` | Reviewer, Editing Score, bounded revision, verifier implementations for the new `CHECK_SPECS` names |
| `protect.py`, `versions.py`, `feedback.py`, `capabilities.py` | protected set; versions; preference signals; the honest capability table |
| `jobs.py` | analysis job on the `ANALYSIS` cancel scope, progress, niceness, per-layer resume |

Changed modules are listed per lane in §14.

---
## 3. The Content Graph

### 3.1 Layout, keys, lifetime

```
WORKDIR/analysis/<src_key>/                      per SOURCE, shared by every session (like WORKDIR/proxies/)
  source.json            {src_key, content_key, leaf, duration, fps, has_audio, has_video, proxy_key}
  speech/<params>.json    words (with prob), sentences, fillers (lexical + acoustic), flags, words_to_check
                          params = backend+model+lang+prompt(disfluency|none)+ANALYSIS_VERSION
  speakers/<params>.json  turns, speakers, roles, angle hints         params = k|auto, feature set, seed, engine
  audio/<params>.json     env_10ms (100 Hz int8 dBFS, base64; ≈ 360 KB per hour), silences, loudness, vad, own-mic energy, laughter events
  visual/<params>.json    faces, motion, shots — sparse, by analysed ranges
  music/<params>.json     beats, bpm, energy curve, sections (music files and beds only)
  semantic/<params>.json  heuristic scores + LLM annotations with provenance
  refs.json               session ids that use this source (the proxies' access rule)

<session>/brain/                                 per PROJECT (bundled in .vae)
  graph/<gid>.json        assembled graph: sources, roles, offsets, layer digests, scenes, project overlay
  decisions/<did>.json    immutable EDPs, one per brain run (preview or applied)
  reviews/<did>.json      Reviewer output per EDP (issues, score, fixes taken)
  versions.json           named versions (§7.1)
  suggestions.json        B-roll suggestions with state offered|placed|dismissed
  feedback.jsonl          preference signals (§12)
  angles.json             the angle group: reference source, dialogue source, members[] each an ORDERED list of files
                          with per-file sync offsets and drift anchors, role guesses, names
  timings.jsonl           {layer, engine, media_s, wall_s, rss_peak} per layer run
```

- `src_key = sha256(realpath, size, mtime_ns)[:24]` (the proxy key rule without the recipe, `ingest/proxy.py:190-196`); a second `content_key = sha256(size + 1 MiB head + 1 MiB tail)` (the artefact rule, `artefacts.py:123`) so a `.vae` re-import (new realpath, same bytes) hits the cache.
- `gid = sha256(sorted layer digests + sync offsets + roles + ANALYSIS_VERSION)[:12]`. A model annotation changes the semantic digest and therefore the graph id: "the same graph" means the same annotations.
- Size caps: a layer file above 32 MB is refused and marked `failed:size`; a graph above 64 MB likewise. Expected for 60 minutes: speech ≈ 1.2 MB, audio ≈ 0.8 MB, speakers ≈ 60 KB, semantic ≈ 0.4 MB, visual ≈ 0.3 MB per analysed 10 minutes at 2 Hz.
- Eviction: `WORKDIR/analysis/` is its own LRU class in `render/cache_budget.py` (`VAI_ANALYSIS_CACHE_MB`, default 1024), whole layer files touched on read. A session's `brain/` is project state and is never evicted.
- `.vae`: `storage_project.save_project` (`storage_project.py:117`) bundles `brain/` and, for each referenced `src_key`, the `speech`, `speakers`, `audio`, `music` and `semantic` layer files; `load_project` (`:189`) writes them under the new `src_key` after the media remap. The lazy `visual` layer is rebuilt.
- Staleness: a graph is stale when any source identity or the transcript key changes; `facts.brain_graph_id` is `None` then and the analysis gate asks.

### 3.2 Layers (schemas with examples)

All times are reference seconds; ids are stable within a layer file and prefixed by kind (`w_`, `s_`, `u_`, `f_`, `r_`, `q_`, `d_`, `sil_`, `t_`, `sc_`, `ev_`). Every layer model is `extra="forbid"` with monotone time checks (`t0 < t1`, non-overlapping sentences within a speaker), id-reference checks (`w.sent` exists, `flags.repeats.of` exists) and the size caps. A layer that fails validation is treated as missing (`status: failed:<reason>`), never partially trusted.

`graph/<gid>.json` header:

```json
{"version": 1, "id": "g_7c1e4b2a9d03", "analysis_version": 1, "clock": "reference", "reference": "src_a41f…",
 "sources": [
   {"key": "src_a41f…", "role": "reference_audio", "leaf": "zoom_recorder.wav", "duration": 3612.4, "sync_offset_s": 0.0,
    "has_video": false, "layers": {"speech": "ok", "speakers": "ok", "audio": "ok", "semantic": "partial"}},
   {"key": "src_9b02…", "role": "angle", "angle": "A", "leaf": "cam_wide.mp4", "duration": 3610.1, "sync_offset_s": 0.342,
    "fps": "30000/1001", "layers": {"audio": "ok", "visual": "lazy"}, "angle_guess": {"kind": "wide", "sees": ["S1", "S2"], "confidence": 0.71, "by": "own_mic"},
    "files": [{"key": "src_9b02…", "leaf": "cam_wide.mp4", "ref_t0": -0.342, "ref_t1": 3609.8, "sync_offset_s": 0.342,
               "drift": {"anchors_ref_s": [0, 300, 600, "…"], "offsets_s": [0.342, 0.343, 0.345, "…"], "max_dev_ms": 41}}]},
   {"key": "src_c77d…", "role": "angle", "angle": "B", "leaf": "cam_host_001.mp4 + cam_host_002.mp4", "sync_offset_s": -0.118,
    "files": [{"key": "src_c77d…", "leaf": "cam_host_001.mp4", "ref_t0": 0.118, "ref_t1": 1799.9, "sync_offset_s": -0.118},
              {"key": "src_c780…", "leaf": "cam_host_002.mp4", "ref_t0": 1800.4, "ref_t1": 3611.0, "sync_offset_s": -1800.4}],
    "angle_guess": {"kind": "close", "sees": ["S1"], "confidence": 0.83, "by": "own_mic"}},
   {"key": "src_a41f…", "role": "reference_audio", "dialogue": true},
   {"key": "src_e310…", "role": "angle", "angle": "C", "leaf": "cam_guest.mp4", "sync_offset_s": 0.905, "angle_guess": {"kind": "close", "sees": ["S2"], "confidence": 0.79, "by": "own_mic"}},
   {"key": "src_1ab3…", "role": "broll", "leaf": "broll/office_desk.mp4", "tags": ["office", "desk", "laptop"]}],
 "speakers": [
   {"id": "S1", "label": "SPEAKER_00", "name": null, "role_guess": "host", "share": 0.38, "questions": 41, "angle_hint": "B"},
   {"id": "S2", "label": "SPEAKER_01", "name": null, "role_guess": "guest", "share": 0.62, "questions": 6, "angle_hint": "C"}],
 "content_type": {"guess": "interview", "confidence": 0.82, "evidence": ["2 speakers", "question ratio 0.31 on S1", "answer share 0.67 on S2"]},
 "layers": {"speech": "speech/whispercpp-small-en-v1.json", "speakers": "speakers/k2-mfcc13-s42-v1.json", "…": "…"},
 "digests": {"speech": "sha256:…", "speakers": "sha256:…", "audio": "sha256:…", "semantic": "sha256:…"},
 "scenes": "scenes.json", "topics": [{"id": "t_007", "t0": 780.0, "t1": 930.5, "title": "The first no", "by": "apple_intelligence", "sents": ["s_00312", "…"]}],
 "music_hint": {"mood": "cinematic", "energy": 0.42, "evidence": ["interview", "wpm 140", "emotion mean 0.3"]},
 "project": {"canvas": [1920, 1080], "fps": 30, "session_language": "en", "controls_seen": ["premium_podcast"]},
 "timings_s": {"transcript": 0.0, "audio": 11.2, "speakers": 24.8, "visual": 96.0, "semantic": 41.0}}
```

`speech/<params>.json`:

```json
{"params": {"backend": "whisper_cli", "model": "small", "language": "en", "prompt": "disfluency", "analysis_version": 1},
 "words": [{"id": "w_004211", "t0": 812.41, "t1": 812.66, "text": "the", "prob": 0.97, "spk": "S2", "sent": "s_00318"},
           {"id": "w_004212", "t0": 812.70, "t1": 813.02, "text": "um", "prob": 0.81, "spk": "S2", "sent": "s_00318", "filler": true},
           {"id": "w_004213", "t0": 813.40, "t1": 813.62, "text": "Priya", "prob": 0.44, "spk": "S2", "sent": "s_00318", "check": "low_prob"}],
 "acoustic_fillers": [{"id": "af_0007", "t0": 902.11, "t1": 902.39, "confidence": 0.82, "evidence": {"pitch_range_st": 0.9, "flux": 0.06, "word_overlap": 0.0}}],
 "words_to_check": [{"word": "w_004213", "why": "low_prob"}, {"word": "w_005102", "why": "digit"}, {"word": "w_005871", "why": "capitalised_unknown"}],
 "sentences": [{"id": "s_00318", "t0": 812.41, "t1": 818.92, "spk": "S2",
                "text": "The thing nobody tells you about raising money is that the first no is the useful one.",
                "kind": "statement", "is_question": false, "answer_of": null, "complete": true, "weak_start": false, "topic": "t_007",
                "features": {"wpm": 168, "fillers": 1, "has_number": false, "claim": true, "conclusion_marker": true, "story_marker": false,
                             "contrast_words": 1, "anaphora_start": false, "len_words": 17}}],
 "turns": [{"id": "u_0142", "spk": "S2", "t0": 809.9, "t1": 861.3, "sents": ["s_00316", "s_00317", "s_00318"]}],
 "flags": {"false_starts": [{"id": "f_0031", "t0": 822.1, "t1": 823.4, "kept": "s_00320", "text": "so the— so the second"}],
           "repeats": [{"id": "r_0006", "dup": "s_00402", "of": "s_00398", "similarity": 0.93}],
           "weak_questions": [{"id": "q_0009", "sent": "s_00211", "spk": "S1", "answer": "s_00212", "removable": true, "why": "answer restates the question"}],
           "dead_air": [{"id": "d_0057", "t0": 1401.2, "t1": 1404.9}],
           "technical": [{"id": "x_0002", "t0": 2210.0, "t1": 2213.1, "why": "clipping 0.9 s, wpm 22"}]}}
```

`audio/<params>.json`: `{"hz": 100, "env_10ms": "<base64 int8 dBFS, 1 byte per 10 ms frame, the voicing frames of transcribe._VAD_FRAME_S>", "vad": [[t0, t1], ...], "silences": [{"id": "sil_0021", "t0": 1401.2, "t1": 1404.9}], "loudness_i": -19.4, "noise_floor_db": -58.0, "clipping": [[t0, t1]], "own_mic_energy": {"src_9b02…": [...], "src_c77d…": [...]}, "events": [{"id": "ev_0012", "kind": "laughter", "t0": 611.2, "t1": 612.9, "src": "src_c77d…", "confidence": 0.7}]}`.

`speakers/<params>.json`: `{"engine": "numpy-mfcc13-kmeans", "k": 2, "k_method": "silhouette", "silhouette": 0.31, "utterances": [{"t0", "t1", "spk"}], "turns": [...], "speakers": [{"id": "S1", "label": "SPEAKER_00", "share": 0.38, "questions": 41, "role_guess": "host"}], "overlaps": [[t0, t1]], "flip_risk": [{"t": 1290.4, "why": "cluster margin 0.04 after a 6 s pause"}]}`.

`visual/<params>.json` per angle: `{"hz": 2, "ranges": [[800, 930]], "faces": [{"t": 812.5, "boxes": [[0.41, 0.33, 0.12, 0.21]], "n": 1}], "motion": [{"t": 812.5, "v": 0.03}], "quality": [{"t": 812.5, "sharp": 0.71, "expo": 0.55, "shake": 0.02}], "shots": [], "face_lost": [[t0, t1]]}` — boxes normalised 0..1 and clamped on write and on read (the `_norm_bbox` OOM lesson, ai-tools map §2.16).

`music/<params>.json` (music files and beds): `{"bpm": 120, "grid_offset": 0.05, "beats": [...], "downbeat_every": 4, "energy_curve": [per 2 s], "sections": [{"t0", "t1", "level": "quiet|normal|loud"}], "engine": "sidecar|librosa|numpy-onset"}`.

`semantic/<params>.json`: `{"scores": {"s_00318": {"hook": 0.86, "importance": 0.74, "standalone": 0.9, "quotable": 0.81, "humour": 0.05, "emotion": 0.31, "virality": 0.71, "quality": 0.78, "evidence": {"hook": ["contrast_words", "claim", "rms_z>0.8"], "importance": ["answer", "topic_peak"]}}}, "annotations": [{"sent": "s_00318", "by": "apple_intelligence", "model": "FoundationModels-3B", "task": "rank_moments", "prompt_hash": "sha256:…", "at": 1790700000, "hook": 0.86, "quotable": 0.81, "kind": "statement", "why": "counter-intuitive claim with a concrete referent"}], "topics": [...], "budget": {"calls": 25, "spent_s": 41.0, "partial_from": null}}`.

### 3.3 Scenes — the unit reasons and digests refer to

`brain/graph.py` derives `scenes.json` from the layers (pure, deterministic):

1. **Speech scenes**: sentences (`ai/shorts._sentences`' rule: terminal punctuation, else 0.8 s pauses; `ai/shorts.py:82`) merged forward while the same speaker continues and the merged unit stays ≤ 12 s; split at speaker changes and pauses ≥ 1.2 s. Typical podcast: 6-10 s per scene, 250-450 scenes per 45 minutes.
2. **Pause scenes**: any gap ≥ 1.5 s between speech scenes (`kind: "pause"`).
3. **Reaction scenes**: a laughter or applause event outside any word span (`kind: "reaction"`).
4. **Topics** over scenes: TextTiling-style cosine drop over a bag-of-lemmas window of 6 scenes (stop-words per `agent/prompt/langs.py`), minimum 45 s, plus a hard boundary at any question following ≥ 3 answer scenes; lexical title = top-3 nouns; the Gateway's `title_topics` may relabel (provenance recorded).
5. Each scene carries: `id, kind, t0, t1, spk, topic, sents[], text, features (from speech), shot {angles: {src: {faces, largest_face, role}}, active_angle, motion, face_lost_frac}, quality, scores, evidence, labels {emotion, function}` — the record shape in Appendix A.

### 3.4 Every scorer: on-device method and golden test

Weights are named constants in `brain/analysis/semantic.py` with a docstring per term. Goldens pin top-k behaviour and monotonicity on the fixtures (§13.2), not the exact numbers, so weights can be tuned without churn. All inputs are the transcript, the FLAC-proxy PCM, Haar boxes and the turns; nothing needs torch, librosa, a network or a model beyond whisper.

| Feature / score | Inputs | Method (pure, deterministic) | Golden test (fixture → assertion) |
|---|---|---|---|
| sentences, fillers, wpm | words | `_sentences` rule; `_DEFAULT_FILLERS` (`um uh umm uhh erm hmm`, `dispatch.py:4153`) + verbatim-repeat rule (`the the` → first is filler) + `soft_filler` (`like`, `you know`, `I mean`, `sort of`, `kind of`) counted, never removed by default. The brain's transcript of record is whisper.cpp run WITH `--prompt "Um, uh, hmm, you know, like, I mean, so…"` (a disfluency-biased initial prompt; whisper is trained on subtitles and drops "uh" on real speech — the bench narration's own docstring measured it dropping every spelling of "uh"), stored under a distinct `params.prompt` so it never replaces the upload's unprompted transcript that the ordinary editor reads; token `p` from `-ojf` is read into `w.prob` (min over a word's tokens; `transcribe.py:41` defaults it to 1.0 today) | `narration_en.json`: 9/9 fillers by time, the content "like" (36.73-37.96 s) is NOT a filler, 12 sentences ± 0; `prob` values are not all 1.0 |
| **acoustic fillers** (`analysis/fillers.py`) | 10 ms envelope + PCM + words | voiced islands of 0.15-0.6 s (VAD run-lengths) with flat pitch (autocorrelation pitch range < 2 st over the island), low spectral flux (mean frame-to-frame spectral difference below the speaker's 20th percentile) and no transcript word overlapping ≥ 50 % of the island; `confidence = 0.4·flatness + 0.3·(1 − flux_norm) + 0.3·(1 − overlap)`; merged with the lexical list (a lexical filler that also passes the acoustic test gets `confidence 1.0`); removed at `confidence ≥ 0.7` when `energy ≥ 5`, otherwise listed as "possible fillers" on the Plan tab with seek buttons | fixture 3 plants three "uh" islands the ASR is known to drop: all three found by TIME within 60 ms, 0 false positives on the content sentences; fixture 1: the per-speaker "um"s are found by both routes |
| false start | words | a run of ≤ 4 words ending in a cut-off (whisper `—`/`-` or an abrupt 0.25-0.8 s gap) whose first two lemmas match the start of the following run within 2 s | interview fixture: the one planted false start found, 0 false positives |
| repeat_of | sentences | Jaccard of lemma sets ≥ 0.8 with a sentence of the same speaker within 90 s, length ≥ 6 tokens, ≤ 1.3× its length; the later is `dup` unless the earlier was a false start's victim | the planted repeat pair (Jaccard 0.9) → `dup` is the later; the retake in the emphasis reel → dropped |
| question / answer | text + speaker | `?`, wh-word or aux inversion at start; `answer_of` = the next scene by a different speaker within 2 s | `narration_en.json`: only sentence #36 is a question; interview fixture: 12/12 Q/A pairs |
| weak question | Q/A | host question whose answer restates ≥ 60 % of its content tokens in its first sentence, or a question < 4 words followed by a ≥ 6 s answer; `removable` ONLY when the answer's first sentence has `standalone ≥ 0.7` | fixture: the 3 removable weak questions flagged removable, the 4th (non-standalone answer) flagged NOT removable |
| dead air | VAD + words | gaps ≥ 1.2 s (podcast) / ≥ 0.6 s (reel) inside a turn; silence layer spans ≥ 0.5 s elsewhere (`silencedetect −35 dB / 0.4 s` thresholds as `ai/diarize.py:186`) | `narration_en.json`: exactly the 7 pauses (2.0 s each) within 0.1 s |
| technical | audio | ≥ 2 s with clipping fraction > 0.02 or dropout (RMS < noise floor + 3 dB while words exist) | audio fixture with a planted 3 s clip |
| rms_z, pitch_range_st, stretch | FLAC PCM | 20 ms RMS frames z-scored WITHIN speaker on speech frames only; pitch by numpy autocorrelation on voiced 40 ms frames (60-400 Hz), range in semitones per scene; stretch = speaker median wpm / scene wpm | emphasis reel: the +6 dB, 15 % slower sentence has rms_z ≥ 1.5 and stretch ≥ 1.1; no other sentence does |
| laughter / applause | RMS + spectral flatness (numpy FFT) | bursts 0.3-3 s with flatness > 0.4 and 3-8 Hz periodicity (laughter) or flatness > 0.6 with no periodicity (applause), outside any word span, on the LISTENER's mic when angles have distinct audio | synthetic burst → one event ± 0.2 s; pure speech → 0 events |
| speakers, turns | PCM | utterances from VAD run-lengths ≥ 0.4 s; 13 MFCC (pre-emphasis, 25 ms Hann, 26 mel, DCT-II) in numpy; k-means++ seed 42, 25 iters (`ai/diarize.py:260-271`); k from `controls.speakers` else best silhouette in 1..4 with "single speaker" when the 2-cluster silhouette < 0.12; turns = utterances merged per speaker across gaps < 0.6 s; words take the speaker of the turn they overlap most | two-voice fixture: k = 2, turn boundaries within 0.3 s of truth for ≥ 90 % of speech time (DER ≤ 15 %); numpy MFCC equals librosa within 1e-3 where librosa exists (dev only); run twice → identical |
| roles | turns + questions | `host` = higher question share AND lower speech share; `guest` otherwise; `unknown` when within 10 % | interview fixture: S1 host, S2 guest |
| angle hints (`sees`) | own-mic + faces + turns | signals in PRIORITY order, each recorded as `by`: (1) **own-mic energy correlation** with each speaker's talking mask is PRIMARY whenever ≥ 2 angles carry distinct audio (Pearson r over the 100 Hz envelope, speech frames only); accepted when the best `r ≥ 0.5` with a margin ≥ 0.25 over the runner-up; (2) **largest-face size** per angle (an angle whose largest face is ≥ 0.25 frame height while another angle's is < 0.15 is the close); (3) **mouth motion** only from a 20 s probe decoded at ≥ 8 Hz around 3 long turns per speaker (lower third of the largest face box, frame-difference energy vs the talking mask) — never the 1 Hz samples, which are noise for mouths; Hungarian assignment (numpy) over speakers × angles; an angle that correlates with both → `wide`; confidence = final margin. **Margin < 0.3 → the plan asks** ("Which camera shows Priya?" one question with one thumbnail per angle) before the camera pass; unanswered, the camera pass uses the wide for that speaker and says so | multicam fixture: B→S1, C→S2, A→wide with margin ≥ 0.3 by own-mic alone; with own-mic disabled the face-size route agrees; the dead angle → `unknown`; a fixture with two same-pitch voices on a mixed recorder still assigns by own-mic |
| sync offsets (per FILE) | FLAC PCM | numpy FFT cross-correlation at 16 kHz against the reference, anchors of 20 s every 5 min (≥ 3 per file: start, middle, end); the file offset is the median; `confidence` = peak-to-median ratio / 6, capped 1; `unverified` below 0.5. **Angles are ordered file lists** (`angles.json.members[].files[]`: cameras split at 30 min / 4 GB and start at different times): each file aligns independently and gets `ref_t0`/`ref_t1`; gaps between files are `angle_gap` spans the Camera Director never switches into. **Drift**: when the anchors' offsets deviate > 40 ms across a file, a piecewise-linear offset (the anchor table) is stored and `apply_camera_plan` uses the offset at the piece's midpoint; the card says "camera B drifts 41 ms/h; corrected" | planted +0.35 / −0.20 s recovered within 10 ms; a two-file angle (split at 30:00 with a 0.5 s gap) yields two aligned files and one `angle_gap`; a planted 60 ppm drift is recovered within 10 ms at every anchor; dead angle `unverified` |
| faces, largest_face, role | 720p proxy frames (1 Hz; 2 Hz where a face is present < 3 s) | Haar frontal (`ai/reframe._detect_subject_centers` refactored to return boxes); 2+ faces → `wide`; 1 face ≥ 0.25 frame height → `close:<spk>`; else `medium` | reframe fixture: box centre within 4 % of the planted blob; wide frame → 2 faces |
| motion, face_lost_frac | proxy frames | mean abs frame difference at 1 Hz; fraction of samples with no face | static vs moving lavfi: moving > static |
| sharpness, exposure, shake | proxy frames | Laplacian variance (normalised per source), mean luma distance from 0.45, median `cv2.phaseCorrelate` shift | blurred twin scores lower on sharpness |
| noise_db, clipping | PCM | 10th percentile of RMS frames in dBFS; fraction of samples at ±0.999 | planted clipping → fraction > 0 |
| **number class** | text | `strong_number` = a quantity ≥ 10, a percentage, a currency amount, or any number paired with a comparative/superlative (`than`, `most`, `-est`, `only`, `first`) within 4 words; `weak_number` = a year, a date, a count < 10 without such a pairing ("in 2019 with three people") | "eight million" → strong; "2019" → weak; "three people" → weak; "three times faster" → strong |
| **importance** | features | `0.30·answer_len_norm + 0.22·claim + 0.08·strong_number + 0.15·topic_peak + 0.10·conclusion + 0.15·rms_z⁺ − 0.25·repeat − 0.20·filler_rate − 0.20·false_start`, clipped 0..1; a weak number adds nothing; `topic_peak` = the scene with the highest lexical centrality in its topic | monotone: adding a strong number raises it, a year does not, a repeat lowers it; interview: the planted key statement is top-1 |
| **hook** | features | **candidacy** first: a sentence is a hook candidate only if it has a claim, a contrast word, a question or an imperative (a bare number or a pleasantry never qualifies). Score = `0.25·is_question + 0.20·(claim or conclusion) + 0.15·strong_number + 0.15·delivery + 0.10·contrast_words (never/nobody/secret/mistake/wrong/best/worst/only/stop, the `_sentence_score` list, `content.py:635`) + 0.10·standalone − 0.30·weak_start − 0.30·anaphora_start − 0.20·(len > 20 words)`, where `delivery = max(emotion, rms_z⁺)` and a candidate with `delivery < 0.3` AND no contrast word is scaled by 0.7 (flat delivery of a fact is not a hook). **A number is counted ONCE across the whole stack**: the hook formula, the nine axes (§4.2) and `quotable` read `strong_number` through one term each and never sum it twice; the legacy `_sentence_score` (+1.0 per digit) stays for the `auto_edit` recipe only | `narration_en.json`: the question #36 tops heuristic hook; the superlative #10 ("earns its price") is top-2 among statements; "thanks for watching" is not a candidate; fixture 1's throwaway "we started in 2019 with three people" ranks below the quotable line by ≥ 0.2 |
| **standalone** | text + Q/A | `1 − anaphora_start − 0.5·conjunction_start − 0.3·(answer whose first 3 words lack the question's noun)` | pronoun-start sentence < 0.5; the quotable line ≥ 0.9 |
| **humour** | events + features | `0.5·laughter_within_2s + 0.2·rms_z⁺ of the next scene + 0.2·(scene ≤ 3 s after one ≥ 8 s) + 0.1·lexicon (joke/funny/kidding)`; a model may raise it from text (provenance) | planted laugh → the preceding scene's humour ≥ 0.6; no laugh → ≤ 0.2 |
| **emotion** (arousal proxy) | audio | `0.5·rms_z⁺ + 0.3·pitch_range_norm + 0.2·stretch_dev`; label by quadrant (calm / emphatic / tense / warm) from arousal × (laughter or positive lexicon) | emphasis sentence labelled `emphatic` |
| **quotable** | text | `1` when ≤ 18 words, complete, claim or strong number, no anaphora; else scaled | the planted quotable line = 1; a year-only sentence < 0.5 |
| **words to check** | words | `prob < 0.6`, a capitalised token not in the lexicon and not sentence-initial, any digit run, and any token in the panel's speaker names that the transcript spells differently (Levenshtein ≤ 2) | fixture 1: the planted mis-heard name and the two numbers are listed; ≤ 3 false entries per 10 min of clean speech |
| **virality** | scores | `0.30·hook + 0.25·importance + 0.15·humour + 0.10·emotion + 0.10·quotable + 0.10·standalone` | top-3 by virality on the interview fixture contains the quotable line and the story's payoff |
| **quality** | quality block | `0.35·sharpness + 0.20·exposure + 0.15·(1−shake) + 0.15·noise_ok + 0.15·(1−clipping)` of the active angle | the blurred/clipped span scores lower |
| topics | scenes | §3.3 step 4 | scripted topic switch → boundary within 1 scene |
| music_hint | scores + speech | reel with `energy ≥ 6` → `upbeat`; podcast/interview → `chill`; emotional share ≥ 0.3 → `cinematic`; else `lofi`; `controls.music` overrides | interview → cinematic or chill per emotion share |

The Gateway's `rank_moments` (§9) runs over the top-quartile sentences by heuristic hook score and is merged as `0.6·model + 0.4·heuristic`, recorded with provenance; when the budget runs out the remaining sentences carry heuristic scores and the layer is `partial` (the card says "moments ranked by the built-in heuristics past 42:10").

### 3.5 Triggering, incremental rule, laziness

Analysis is triggered three ways, all OUTSIDE a plan: (a) eagerly after an upload's background transcript lands (`main.py:1300`), at low priority, for the cheap layers (audio, speech from the existing transcript, sync); (b) by the panel's "Read footage" button; (c) by a brain prompt, whose `_x_edit` expander finds `facts.brain_graph_id` missing or stale and returns a `gate_analysis` question ("Read the footage first (≈ 4 min) / Edit with what is known (today's checklist) / Stop"); a "yes" starts the job and the prompt re-plans when it lands (the `RunBus` already carries progress frames). A plan never contains an analysis step.

Incremental: a layer's key includes file identity, so replacing a source re-analyses that source only; adding a fourth angle analyses one file; changing controls or style re-plans in seconds with no analysis. The `visual` layer is lazy by range: face and motion samples are computed only over ranges the planner keeps (`graph.ensure_visual(src_key, ranges)` from the camera, emphasis and reframe passes), so a 45-second reel from a 60-minute podcast never scans 60 minutes of frames.

---
## 4. The planner: rules a lane can implement and a test can check

`brain/planner/__init__.py` composes passes in a fixed order; each pass is a pure function `(graph, ctx, decisions) → decisions` with its own goldens. `ctx` = `{controls, style, energy table, seed, previous_edp | None, scope_range | None}`. Pass order: `classify → tighten → select → story → hooks → seams → camera → emphasis → captions → graphics_min → music → reframe → broll → dialogue → finish` (`seams` snaps every cut edge to an energy trough and applies the protected-pause rules once the story is known, §4.6.2-3; `dialogue` emits the lane decision last so it sees every structural decision, §4.6.1). Every threshold below scales with **Editing Energy 1-10** through ONE table, `brain/energy.py` (§4.10); a Style Profile (§8.3) sets defaults the controls override.

Ordering and tie-breaks are total: decisions sort by `(kind_rank, ref.src, ref.t0, id)`; every scorer breaks ties by `(round(score, 6), −t0, id)`; the only randomness is the k-means seed (fixed 42) inside analysis and the reel scorer's optional exploration mode (off by default; `seed` recorded in the EDP so "Try another" is reproducible).

### 4.0 `classify` — content type

- speakers = 1 → `talking_head`; 2 with question ratio ≥ 0.25 on one speaker and answer share ≥ 0.6 on the other → `interview`; 2-4 with talk shares within 0.35-0.65 → `podcast`. `controls.content_type` or a family word in the prompt (`podcast`, `interview`, `reel`) overrides; a model may reclassify (`classify_content` task, §9) only within {talking_head, interview, podcast} and only when it agrees with the rules or the rules' confidence < 0.6.
- Target: `reel` when `controls.duration_s ≤ 90` or the platform is vertical; `episode` otherwise.
- Test: fixture 1 → `interview` (0.8+); `narration_en` → `talking_head`; an override wins.

### 4.1 Story Planner (`story.py`, `select.py`)

#### 4.1.1 Reel / short (≤ 90 s): Hook → Context → Information → Payoff

1. **Hook** = the Hook Engine's top scene (§4.2) if `standalone ≥ 0.75`; else the reel opens chronologically on the best window and only a `hook_card` is added.
2. **Body** = the best contiguous sentence run around the highest-importance cluster within the hook's topic (else the global top cluster), scored by `_score_window`'s shape (`ai/shorts.py:200`) with `importance` replacing `density`: `0.28·importance_mean + 0.20·energy + 0.24·hook(first) + 0.10·ending + 0.18·length_fit − 0.30·dead_air − 0.25·fillers`, plus `+0.10·standalone(first)` and, for interviews, `+0.10·speaker_balance`.
3. **Duration fit** (greedy, deterministic): drop the lowest-importance WHOLE scenes from the middle of the body, never a scene that is `answer_of` a kept question, never the `topic_peak`, keep ≥ 60 % of the body contiguous, stop at `asked_s ± 8 %`, end on a complete sentence (`complete = true`). If the fit cannot be met with whole scenes, the last kept scene is trimmed to its last complete sentence and the card says "44.2 s (asked 45)".
   **Antecedent guard (every mode, every join).** Any join of two scenes that were not adjacent in the source — a dropped middle scene, a removed hook position, a removed question, a cold open's return — requires the FOLLOWING scene's `standalone` RECOMPUTED against its new predecessor to be ≥ 0.6 (the anaphora / conjunction-start tests and the noun test run against the new predecessor's lemmas, not the original one's). When it fails, the planner drops a different scene (the next-lowest importance whose join passes), or keeps the bridge sentence (the shortest scene that restores the antecedent), in that order; a join that cannot be repaired is not made and the duration fit reports the shortfall. The guard is one function (`story.join_ok(prev, next, graph)`) shared by reels, episodes and interviews and tested on hand-built scene lists.
4. **Payoff** = the last kept scene with `conclusion_marker`, else the run's end.
5. **Placement**: when the hook scene is not already inside the first 8 s of the body, `open_on` (move it to the front; the original position is removed) for reels ≤ 60 s; `cold_open` (duplicate a ≤ 6 s hook statement at the front, then continue chronologically) for 60-90 s shorts and when the style says `cold_open`. `open_on` is skipped when the scene AFTER the hook's original position fails the antecedent guard against the scene before it, and when the hook scene's removal would orphan a non-standalone answer.
6. **Shorts N ≥ 2** (`controls.count`): pick windows by the same scorer with a **no-repeat rule**: picks separated by ≥ 1 sentence and ≤ 20 % sentence overlap across the set (and against the main edit's hook when one exists); each child gets its own sub-EDP.

Tests: fixture 3 (emphasis reel) → 45 s reel whose first kept sentence is #10 within 0.2 s, duration within ±1.0 s, ends on a complete sentence; `narration_en` 30 s → the clean run `31.05-61.43` that `tests/test_c6_reel_best_window.py` expects; 3 shorts → distinct, non-overlapping, each opening sentence `hook ≥ 0.7`.

#### 4.1.2 Podcast (full episode): chronology with a cold open and chapters

- Cold open = the strongest quotable scene ≤ 10 s, **DUPLICATED** at the front (`cold_open`) when `energy ≥ 5` and `standalone ≥ 0.75` — an episode NEVER uses `open_on`: moving the line tears it out of the guest's answer and leaves a jump exactly where the point was. The teaser is separated from the chronological start by the `hook_card` (it plays as the separator, over the last second of the teaser and the first of the chronology) or, when captions/graphics are off, by a 6-frame dip to black on both sides (`set_video_fade` on the teaser's out and the first chronological piece's in; the dialogue lane gets the matching 0.2 s fades). Reason `cold_open` cites the quotable scene AND the separator. For `energy < 5` the profile knob `story.cold_open: "title_only"` yields a `hook_card` over the first kept sentence and no duplicate.
- Lower thirds start at each speaker's first kept turn ≥ 3 s AFTER the cold open ends (never at t = 0 over the teaser); a lower third never overlaps the hook card in time.
- Chapters = topic segments → `add_marker(label=<topic title>)` when `controls.markers` (default on for episodes).
- Removals (all through `tighten`, §4.6): pauses ≥ the energy threshold (keep-pad by energy), fillers, false starts, repeats, dead conversation (a topic with mean importance < 0.25 and length > 60 s, only at `energy ≥ 6`), technical interruptions.
- Duration control on an episode caps by dropping whole lowest-importance topics first, never mid-topic.

Tests: fixture 2 (multicam podcast) → false start, repeat and the planted dead-air pause removed; every quotable sentence kept; chapters = topic count ± 1.

#### 4.1.3 Interview: Intro → Q/A pairs → key statement surfaced → close

- Guest priority means the GUEST is never cut for the host, not that questions vanish. **Default (`interview.questions: "keep"`)**: only `weak_question.removable` scenes are removed; every other question is kept, and these are kept even if flagged weak: a question with `hook ≥ 0.5` or `importance ≥ 0.4`, a question the emphasis pass punches OUT on, and the first question of each topic. **"Answers only" is a style knob** (`interview.questions: "answers_only"`, documentary), off in every shipped profile.
- When a question is removed, the answer's head markers ("yeah, so", "well", "yes", "right, so") of ≤ 0.6 s are trimmed with it when the remaining answer is `complete` and passes the antecedent guard; otherwise the question stays.
- Repeated answers removed (`repeat_of`); the key statement (top `importance × quotable`) gets a punch-in and, at `energy ≥ 5`, is the cold open (a duplicate, §4.1.2); a guest lower third at the guest's first kept turn after the cold open.
- Continuity is the antecedent guard of §4.1.1 (shared function): a removal never leaves a non-standalone scene immediately after a join; the guard runs after `tighten` and re-adds the smallest preceding scene that restores an antecedent.

Tests: fixture 1 → the 3 removable weak questions gone, the 4th kept, the two well-asked questions whose answers are standalone are KEPT, no answer whose question was removed has `standalone < 0.7`; sentence survival ≥ 0.98 of the truth set; with `questions: "answers_only"` only the hook ≥ 0.5 question survives.

#### 4.1.4 Model contributions (typed drafts)

`StoryDraft` (`brain/drafts.py`, Pydantic, `extra="forbid"`): `{content_type, hook: scene id, beats: [{role, scenes[]}], drop: [{scene, why}], topic_labels: {}, confidence}`. Grounding (`drafts.ground`): unknown scene ids dropped; a beat whose scenes are not standalone and whose source predecessor is missing is rejected; `drop` entries must cite a scene with `importance ≤ 0.5` or `repeat_of` set; a draft that drops more than the duration fit requires is trimmed to the fit. What survives is applied over the rules' plan; what does not is in the run log ("the model suggested …, not applied because …"). The recipes rung has no draft. Test: a garbage draft → plan byte-identical to recipes.

### 4.2 Hook Engine (`hooks.py`)

Candidates = speech scenes that pass §3.4's candidacy (claim, contrast, question or imperative), `hook ≥ 0.5`, ≤ 20 words, `standalone ≥ 0.6`, `quality ≥ 0.4`, `delivery ≥ 0.3` OR a contrast word, not in the first 3 s of the source ("hey everyone" is penalised by `weak_start`). Nine axes per brief §14, each 0..1 from the graph; **a number feeds exactly one axis** (`surprise` when it is paired with a superlative/comparative, else `authority`), never both, and a weak number (year, date, count < 10) feeds neither:

| Axis | Signal |
|---|---|
| curiosity | question or contrast word |
| surprise | a strong number paired with a superlative/comparative, `nobody`/`never`, arousal spike vs the previous scene (rms_z delta ≥ 1) |
| emotion | arousal |
| authority | claim + (a strong number not already counted by surprise) + speaker is the guest |
| conflict | `but`, `wrong`, disagreement with the previous speaker's lemmas |
| value | `how to`, `the trick`, imperative verb first |
| question | is_question |
| strong statement | short (≤ 12 words), claim, conclusion marker |
| visual impact | motion or face-size change on the active angle (0 when the visual layer is absent) |

Score = mean of the top 4 axes × standalone. The content brain may re-rank the top 8 (`rank_hooks` task, order sanitised like `rank_windows`, `content.py:711`) and may rewrite the overlay line (`hook_candidates`, existing). The factual rule of brief §14 holds by construction: the spoken hook is the scene's own words moved to the front; only the overlay text is paraphrased and it must share ≥ 60 % of its content lemmas with the scene (`drafts.hook_text_grounded`), ≤ 7 words / 60 chars (`sanitize_hook_items`, `content.py:516`).

Output: `hook.scene`, the `open_on` / `cold_open` decision, the `hook_card` decision, up to 3 alternates the card offers under "Other openings" and "Try another" uses in order.

Tests: fixture 1 → the quotable line is top-1 and the year-and-headcount throwaway is not in the top 5; `narration_en` → #10 or #36 is top-1 and "thanks for watching" is never a candidate; a flat-delivery fact (planted at −3 dB, no contrast word) ranks below the same words delivered with emphasis; a rung's re-rank may not lower the truth hit rate below the rules' (else its re-rank is disabled per task, §13.5).

### 4.3 Camera Director (`camera.py`) — for angle groups with ≥ 2 video members

Inputs: turns (reference clock), `speakers[].angle_hint` (own-mic first, §3.4), per-scene `shot`, energy, the cut list from `tighten` (so switches know where jump cuts are), the emphasis and hook scenes (so cutaways never steal them). The planner is OFFLINE with the whole turn table, so nothing is decided "after 0.4 s of the new speaker": every switch is placed with lookahead. A dynamic program over turns chooses the angle per span with `SWITCH_COST(style)` and `MIN_SHOT_S(style)`; the rules below are constraints and post-passes, in priority order, and every switch decision records the turn, the rule and the snapped gap. With a dialogue lane (§4.6.1) a picture switch is audio-neutral, so the "never mid-word" constraint applies to AUDIO seams, not to camera changes.

| # | Rule | Constant (energy 5, premium podcast) | Test on the multicam fixture |
|---|---|---|---|
| 1 | **Follow the speaker, anticipating.** The active angle is the speaker's close. **Backchannel is a lookahead test**: a turn < 0.6 s, or whose words are all in the backchannel lexicon (mm-hm, yeah, right, sure, okay, uh-huh, exactly, `lexicon.py` per language), never switches. Otherwise the switch lands at the new turn's first-word onset MINUS `LEAD_FRAMES` (3 frames at the project rate, ≤ 0.15 s — the viewer sees the guest a beat before hearing them, the way an editor cuts), snapped to the preceding word gap ≥ 0.12 s when one exists within 0.25 s before the onset; when the previous speaker is still mid-word at that instant (a true overlap), the switch lands at the onset exactly | `BACKCHANNEL_MAX_S 0.6`, `LEAD_FRAMES 3`, `SNAP_S 0.25`, `GAP_MIN_S 0.12` | ≥ 90 % of talking time on the speaker's close or the wide; every switch lands 0-0.15 s BEFORE the truth onset of its turn (never after); the planted "mm-hm" and "yeah" turns never switch; bar-code decode at 3 frames per span |
| 1b | **Hide the jump cuts.** For every `tighten` seam whose removed span is ≥ 0.4 s, switch AT the seam to another angle that still sees the speaker (their other close, else the wide), reason `at_cut` ("one seam, two reasons"); these switches count toward the rate cap and relax min-shot to 1.2 s for the piece they open. On a SINGLE-angle timeline the same seams get a step scale change instead: the incoming piece opens at 1.08 (energy ≤ 5) / 1.12 (energy ≥ 6) when the outgoing one was at 1.0, and vice versa (alternating), anchored on the face, as one `add_keyframe` step key at clip-local 0 with reason `jump_cut_hide`; a seam already covered by a punch-in's release (§4.4) needs nothing | `HIDE_CUT_MIN_S 0.4` | fixture 2: every tighten seam ≥ 0.4 s coincides with a `src` change; fixture 3 (single camera): every such seam changes scale by ≥ 0.08 with alternation; the Reviewer's `jump_cut_hidden ≥ 0.9` |
| 2 | **Minimum shot** by energy; a shorter turn stays on the previous angle unless it is a question ≥ 1.2 s (questions are shown) | `MIN_SHOT_S 2.5` (1.6 at energy 9; 1.2 for reels and `at_cut` pieces) | no v1 piece shorter than min-shot except the last and `at_cut` pieces |
| 3 | **Maximum hold → wide reset** at a topic or sentence beat, holding ≥ 3.0 s, then to a DIFFERENT angle than the one it left (the listener's close or the medium) unless none sees the speaker — a reset that returns to the same close reads as a purposeless cutaway; with no wide, to the listener's close as a reaction (rule 4) | `MAX_HOLD_S 25` podcast (14 at energy 7), `RESET_HOLD_MIN_S 3.0` | a 40 s answer yields ≥ 1 wide insert, none mid-sentence, none returning to the same close |
| 4 | **Reaction cutaway** only during an answer ≥ 8 s, placed right AFTER a laughter event or in the low-importance middle of a long answer (`importance < 0.4` for the covered sentences); 1.5-2.5 s ending on a sentence boundary; at most one per 20 s. **Never cut away** during a scene with `importance ≥ 0.6`, over the emphasis word of a punch-in, or over the hook scene — the payoff belongs to the speaker | `REACT_MIN_ANSWER_S 8`, `REACT_GAP_S 20`, `CUTAWAY_IMPORTANCE_MAX 0.6` | the planted laugh produces exactly one reaction cut, after it; none over the quotable line or the punch-in |
| 5 | **Wide for overlap and laughter**: a scene with `overlap_s ≥ 0.6` or a `reaction` scene plays on the wide | — | the planted 0.8 s overlap plays on A |
| 6 | **Never switch into a face-lost angle** (`face_lost_frac` over the target span > 0.3) or into an `angle_gap` (between a member's files); a switch that is not AT a seam stays ≥ 1.0 s from one | `NEAR_CUT_S 1.0` | the dead angle never appears; no seam pair closer than 1.0 s unless coincident |
| 7 | **Avoid repeating compositions**: after two consecutive returns to the same close from the wide, prefer the medium when one exists | — | rule-table unit test |
| 8 | **Continuity**: no AUDIO seam mid-word (hard constraint on a1 seams, or on v1 seams when no dialogue lane exists; verified by `no_cut_mid_word`); switches/min ≤ the style cap including `at_cut` switches | `SWITCH_RATE_MAX 8/min` (6 for premium) | `camera_switch_rate_leq` holds |

Score per candidate angle per second (used by the DP when hints are weak): `0.55·sees_active_speaker + 0.20·own_mic_energy_norm + 0.10·face_present + 0.10·shot_size_pref(style, moment) + 0.05·motion_norm`. With no speech (montage) the existing `plan_multicam` scorer decides per window (`ai/multicam.py:72`) and the reason is `energy_motion_fallback`. With one angle: only rule 1b's scale steps, one note. With `speakers` missing: own-mic energy + faces drive the DP and the card says "speakers: unavailable; switched by microphone energy". With an angle hint below margin 0.3 and the question unanswered: that speaker plays on the wide and the card says so.

Output: a switch table on the reference clock → per member, per FILE (the file whose `[ref_t0, ref_t1]` covers the span; a span that crosses a file boundary is split there) by that file's offset (drift-corrected at the span midpoint) → `switch_angle {src, at_src, angle_src, until_src}` decisions, compiled to `apply_camera_plan` via `$brain:camera` (§5.4). Blink and expression (brief §18) are Phase 2; Phase 1 prefers cut points where the target angle had a face in both neighbouring samples.

### 4.4 Smart punch-ins (`emphasis.py`)

Candidates: an **emphasis peak** = a word with `emphasis = 0.5·rms_z⁺ + 0.3·pitch_peak + 0.2·stretch ≥ 0.7` in a scene with `importance ≥ 0.5` or `conclusion_marker`, or the hook sentence of a reel (always one candidate). A punch-in is a SHOT, not a pump on the last word: it starts at the beginning of the CLAUSE that contains the peak and stays in through the sentence.

| Rule | Value |
|---|---|
| Scale by role | conclusion 1.12; number or claim 1.10; hook 1.12; **question punch-out**: a held 1.08 returns to 1.0 on the question's last word; alternate 1.08 / 1.12 on consecutive punch-ins to avoid repetition; scale by energy ±0.02 |
| Trigger (in-point) | the start of the clause containing the peak (clause boundary = the nearest preceding comma / conjunction / pause ≥ 0.25 s), at least `LEAD_S 0.8` before the peak; if the clause start is < 0.8 s before the peak, the previous clause start is used; never before the sentence start |
| Push | energy ≥ 5: over `PUSH_S 0.4` with `ease-out`; energy ≤ 4: a slow push over 2.0-3.0 s to 1.06-1.08 (a creep, not a cut) |
| Hold | through the end of the sentence that contains the peak |
| Release | as a **STEP** at the next seam after that sentence end — a tighten cut, a camera switch, or the clip end — which costs no key at all: the piece ends at its held scale and the next piece starts at 1.0 (or at the alternated `jump_cut_hide` scale, §4.3 rule 1b). When no seam lies within `RELEASE_WINDOW_S 8` after the sentence end, ease out over ≥ 1.2 s ending on a sentence boundary |
| One interpolation per property | `Keyframe.interp` is ONE mode for a whole property (`edl/schema.py:176-178`), so "ease-out in, ease-in out" on the same `scale` is not expressible. The envelope therefore uses `ease-out` when the release is a step (only the push is animated) and `ease-in-out` when a timed release exists (both segments, symmetric); a punch-in never emits two interpolation modes on one property |
| Keys | on ONE clip: `[t_in, scale_from]` (only when `t_in > 0`), `[t_in + push, scale_to]`, and for a timed release `[t_rel, scale_to]`, `[t_rel + ≥ 1.2, scale_from]` — on `props ["scale","x","y"]` together so the anchor moves with the scale; no implicit t = 0 anchor |
| Cadence | at most one per `PUNCH_MIN_GAP_S` (podcast 20 s → 6 s at energy 9; reel 6 s); never two on the same clip within 4 s; never on a clip already at `scale > 1.15` or with a keyframed scale the person set; never on a `wide`; `jump_cut_hide` steps do not count toward the cadence |
| Anchor | zoom about the largest face centre when the face is off-centre by > 12 % of the frame (adds `x`/`y` keys computed in the compositor's cover-pan units, `+x` moves the picture right, CLAUDE.md "Framing"); else about the centre; scale-only when the visual layer has no face |
| Beat alignment | when music is on, the push's START snaps to the nearest grid beat within ±0.15 s (never across a word boundary) |
| Reason | `emphasis_peak`, `question_punch_out` or `hook_emphasis` with evidence `{word, clause_start, rms_z, pitch, scene, release: "step@<seam>" | "ease@<t>"}` |

Tests: fixture 3 → exactly one punch-in whose in-point is ≥ 0.8 s before the +6 dB word and at a clause start, held to that sentence's end, released as a step at the next seam (no out-keys on the clip); none on the retake; the hook sentence gets one; a hand-built case with no seam within 8 s gets a ≥ 1.2 s timed release ending on a sentence boundary; every emitted `Keyframe` has a single `interp`; the Reviewer's `punch_on_emphasis` ≥ 0.8 and `punch_min_hold_geq(0.8)` holds.

### 4.5 Automatic 9:16 reframing (`reframe.py`)

- Canvas: `auto_reframe {ratio, subject_track: false}` (a canvas change + overlay rescale, no re-encode; `dispatch.py:8681`) + `set_clip_fit {clip_id: "$v1_all", fit: "cover"}` (the pair `validate._platform_rules` already inserts, `validate.py:687-725`).
- Per kept v1 span, from the 1-2 Hz face boxes of its active angle: a **dead-zone follower** (no pan while the face centre stays within ±10 % of the crop centre; pan speed ≤ 0.15 canvas-widths/s; hysteresis 0.5 s; reset instantly at a cut), sampled into keys only at direction changes, ≤ 6 keys per clip. A static talking head yields ONE `x` value (a single key) and no pan.
- **Two faces on one angle.** On a 16:9 two-shot cropped to 9:16 only 31 % of the width survives, so "frame the midpoint" shows the gap between two half-faces — the best-known vertical-podcast failure. Rules, in order: (1) **multicam reels never use the wide vertically** — the camera pass output (the closes) is the source of every vertical piece, and a wide reset is skipped (the speaker's close holds) because the stacked two-up needs an overlay placement tool that stays denied in Phase 1 (§0.5); (2) **a single wide two-shot follows the ACTIVE speaker** from the turn table with the dead-zone follower: the crop moves only at turn changes that open a shot ≥ min-shot, snapped to a word gap, at most one move per min-shot, with the pan speed cap; (3) **both faces fit** (both boxes plus a 4 % margin inside the 9:16 window at scale 1) → the midpoint; (4) **a face is never cut at the crop edge**: when the active face's box plus margin does not fit at the current scale, the crop scales up (cover `scale` up to 1.15) so that it does, and if it still cannot, the reason is `subject_moved_median` and the card says which clip.
- Captions position `bottom` becomes `center` when a face box would overlap the caption band; the Reviewer re-checks after the reframe.
- A clip whose follower needs more than 6 keys or whose subject leaves the dead zone more than 4 times in 10 s is framed at the median centre with the reason `subject_moved_median` and a card note ("the subject moves faster than a pan follows in 2 clips; reframe those with tracking if you want"). A brain plan never emits `auto_reframe(subject_track=true)` (it re-encodes every v1 clip to CRF 20 and swaps `src`, measured 6.5 s for 85 s of 1080p, ai-tools map §2.13); the person can run the existing `reframe` recipe with tracking afterwards.
- No visual layer → centred crop, said on the card.

Tests: reframe fixture → canvas 1080×1920, `fit = cover`, x keyframes track x(t) within 6 % of the canvas width at every key, pan speed ≤ 0.15 widths/s, ≤ 6 keys per clip, no `src` change; `no_letterbox` and `reframe_effective` hold; E7 decodes the export and checks the subject stays inside the frame ≥ 95 % of frames. Two-shot: fixture 1's wide (two pulsing blocks 60 % of the width apart) reframed vertically follows the talking block at every turn ≥ min-shot and never shows the midpoint gap (decoded frames: the active block's centroid inside the frame ≥ 95 %, no frame with both blocks cut at the edges); fixture 2 vertical → no piece from angle A.

### 4.6 `tighten` — silence, fillers, smart jump cuts

| Reason code | Rule | Energy scaling |
|---|---|---|
| `silence` | silence spans ≥ `min_silence_s` between speech, keeping `keep_pad_s` on each side, unless protected (§4.6.3) | min 1.5 s → 0.35 s; pad 0.25 s → 0.06 s (energy 1 → 10) |
| `filler` / `filler_acoustic` | lexical fillers (`_DEFAULT_FILLERS` + verbatim repeats) and acoustic fillers with `confidence ≥ 0.7`: the removal takes the filler's VOICED span plus its trailing breath/gap up to the next word's onset trough minus the pad, and never the preceding word's tail (coarticulation belongs to the neighbour); edges per §4.6.2 | lexical always; acoustic at `energy ≥ 5` (else listed) |
| `soft_filler` | `like`, `you know`, `I mean`, `sort of`, `kind of` when followed by a pause ≥ 0.25 s | only at `energy ≥ 7` or style `fillers: "strict"` |
| `false_start` | §3.4 | always |
| `repeat` | the `dup` sentence with its run-up pause | always |
| `dead_air` | in-turn gaps ≥ 1.2 s (podcast) / 0.6 s (reel) keeping the pad | thresholds scale as silence |
| `weak_question` | interviews only, `removable = true` | at `energy ≥ 3` |
| `dead_conversation` | a topic with mean importance < 0.25 and length > 60 s | at `energy ≥ 6` |
| `technical` | clipping/dropout stretch ≥ 2 s | always |
| `duration_fit` | complement of `keep_window` / `keep_segments` | when a duration is asked |

Every removal is frame-quantised by the tool; the compiler merges adjacent removals and never emits a range < `_MIN_CUT_S`. Test: E1 (fillers 11/11 by time, the content "like" survives, `split_inside_word = 0`), E2 (interview tighten), `cut_source_ranges` equals `remove_silences` + `remove_fillers` on the bench fixture when the trough snap is disabled (`seams.snap=false`, a test-only switch).

#### 4.6.1 The dialogue lane (`dialogue.py` → `sync_dialogue_lane`)

- **When**: every brain plan whose timeline has a dialogue source: the audio-recorder upload (role `reference_audio`) when present; else the reference angle's own file (its microphone is then the one voice, so an angle switch never switches microphones); on a single camera, its own file. `controls.dialogue = "camera_mics"` (panel, off by default) disables the lane for people who mixed on camera.
- **Decision** `dialogue {src, lane: "a1", offsets: {angle_src: seconds}, seam_fade_s: 0.005}`, reason `dialogue_lane` ("dialogue from {leaf} on lane a1; camera microphones muted; {n} seams faded"). It is emitted LAST by the planner and compiled as the LAST stage-2 step so it sees the final v1 layout (§5.5).
- **Executed by** `sync_dialogue_lane` (§5.4): idempotent, rebuilds `a1` from v1, mutes camera-mic audio on v1 angle pieces, moves the recorder off the music lane if the upload's `_handoff_audio_only` put it there (said on the card: "the recorder was on the Music lane; it is now the dialogue"), and applies `audio.fade_in = fade_out = seam_fade_s` at every internal a1 seam (0 at the programme's head and tail unless the story asks for a dip). No render change: the mixer already applies per-clip `afade` (`render/audio_mix.py:694-700`).
- **What stays true afterwards**: `dialogue_in_sync` (§6.1, BLOCKING) — for every v1 media piece whose `src` is an angle member or the dialogue file, the a1 clip covering `piece.start` plays the same reference second within half a frame, a1 has no clip over a v1 gap, and every v1 angle piece is muted. A brain plan adds no v1 transition while a1 exists (luxury's "smooth" becomes `set_video_fade` dips at the head and tail only).
- **Later hand edits**: the person may trim or move v1 pieces afterwards; `_follow_pictures` (`dispatch.py:9053-9100`) follows MOVES of linked sounds only, and a1 pieces are deliberately unlinked (the brain owns the pairing), so a1 can go stale. `facts.dialogue_lane = {src, in_sync}` is computed on every prompt run; when stale, the Prompt bar's reply and the panel show "Dialogue lane out of step with the picture — Re-sync" (a button that dispatches `sync_dialogue_lane` alone, one op), and every later brain run re-syncs as its last stage-2 step. A person who edits a1 BY HAND (a fade, a gain trim, a J-cut) puts those clips in the protected set; the next run's `sync_dialogue_lane` would replace them, so the run asks (the §7.2 three-option clarify) before rebuilding.
- **Shorts**: `make_shorts(from_timeline=true)` copies the a1 pieces inside each window with the v1 pieces, so a child opens in sync and needs no re-sync until its own passes run (they end with one).
- Tests: `tests/test_sync_dialogue_lane_tool.py` (rebuild after keep + cuts + splits + reorder + duplicate + move + angle swaps on bar-coded, click-tracked sources: the a1 clicks line up with the picture's flashes within half a frame at every seam, `av_offsets_ms` from `tests/timing_fixtures.py`; idempotent twice; the music-lane copy removed once; v1 muted; fades set; a hand-faded a1 clip triggers the clarify).

#### 4.6.2 Where a cut edge lands (`seams.py`)

Whisper word boundaries (token end times + the 10 ms voicing snap, `transcribe._refine_words`) are the right neighbourhood and the wrong instant: a cut exactly at a token edge lands on the decay of a plosive or the onset of a breath and clicks or "upcuts". Every audio cut edge is therefore placed by the 10 ms envelope:

1. **Trough snap**: the edge = the frame of minimum `env_10ms` within ±80 ms of the word boundary that lies OUTSIDE every kept word's voiced span (`[w.t0 + 0.02, w.t1 − 0.02]`); ties (within 1 dB) break toward the LATER trough for an out-point and the EARLIER trough for an in-point (leave the decay, take the onset clean).
2. **Pad from the trough**: `keep_pad_s` is measured from the trough, not from the token time; the pad is never less than 40 ms of air before a kept word's onset (`AIR_MIN_S 0.04`) — a word that starts "in the cut" reads as clipped even when no sample of it is missing.
3. **Level match**: when the RMS over 200 ms on each side of a seam differs by > 6 dB, the pad on the louder side grows by up to 60 ms toward its next trough; if the step remains, the Reviewer's `audio_jump` fix sets `add_fade` 0.02 s on both a1 pieces (0.05 on v1 pieces when no dialogue lane exists).
4. **Fades**: every internal dialogue seam carries `seam_fade_s` (5 ms) on both sides (§4.6.1); a v1-only timeline (no a1) gets the same fades on the v1 pieces through `add_fade` in the same step group — the render applies no automatic micro-fade at a cut (`audio_mix.py`), so the EDL must.
5. **Verified**: `no_cut_mid_word(tol=0.02)` on the audio seams (BLOCKING, §6.1) and `seam_click` (decoded verify render: the first-difference peak within ±2 ms of every seam < 0.1 full scale; advisory, 0 expected).

Tests: `tests/test_brain_seams.py` — on the bench narration with planted breaths (Piper's own pauses) every cut edge sits on an envelope minimum ≤ −40 dBFS and ≥ 40 ms before the next kept onset; ties break as specified on a synthetic double-trough; the decoded render of E1 has zero clicks at 11 seams.

#### 4.6.3 Protected pauses (`seams.py`)

Not every pause is waste; the moments an editor protects are the beat before a hard answer, the silence after a punchline and the air after an emotional statement. A pause is **protected** when it (a) follows a scene with `emotion ≥ 0.6` or a laughter event, or (b) precedes an answer whose question has `hook ≥ 0.6`, or (c) follows a scene with `conclusion_marker` and `importance ≥ 0.7`. A protected pause keeps `max(keep_pad_s, 45 % of its length)` capped at 1.2 s, reason `pause_kept:{emotion|laughter|hard_question|conclusion}` on the card (a kept pause is a decision with `kind: keep_pause`, so it appears in the Plan tab and can be undone like a cut). Turn-boundary pauses (the gap between two speakers) keep ≥ 0.3 s at `energy ≤ 5` and ≥ 0.15 s above; in-turn pauses follow the table. Reels at `energy ≥ 8` protect only laughter (`pause_kept:laughter`) and say so.

Tests: fixture 1's pause after the planted laugh keeps ≥ 0.45 × its length; the 2.4 s mid-turn pause is cut to the table value; the turn-boundary gaps never fall below 0.3 s at energy 4; the card lists each `pause_kept` with its why.

#### 4.6.4 Long episodes: never a loose tail

A 60-minute episode at `energy ≥ 8` can exceed 2,000 removals. The compiler splits `cuts` into time-ordered `cut_source_ranges` steps of ≤ 2,000 ranges (the 24-step cap has room; each is one step, all inside the one batch) and the tool's own cap rises to 5,000 once the fast path is measured (§15.3). If the budget is still exceeded, the compiler drops the SHORTEST silences first, uniformly across the episode (every 5-minute bucket loses the same share), and records `deferred[]` with the count and the time range affected. `tighten_uniform` (§6.1) verifies that the removal density of the last quarter is within 30 % of the first quarter; a miss is on the card ("the last 12 minutes are looser: 140 of 2,340 pauses left").

### 4.7 Caption intelligence (`captions.py`)

| Mode | `add_caption_track` | `set_caption_style` look | position | when |
|---|---|---|---|---|
| Viral | `word_emphasis`, `chunk_size 4`, `highlight: "current_word"` (§4.7.3: one cue per WORD interval carrying the whole ≤ 4-word / 18-char chunk with `emphasis=[k]`) | upper, size 96, stroke 8, shadow, `accent` = brand accent else `#ffd166` | center | reel, energy ≥ 7 |
| Dynamic | `ig_chunky` (16 chars / 2 lines / 3.0 s, `dispatch.py:2824`) with `emphasis` = the cue's `emphasis_words` (meaning-based keyword accent, no per-word cadence) | upper, size 72, stroke 6, `accent` as above | bottom, or center when a face overlaps the band | reel, energy 4-6 (default for reels) |
| Podcast | `default`, `max_chars 32` | size 56, background `#000000AA`, no upper | bottom | podcast, interview |
| Minimal | `default` | size 48, thin stroke | bottom | corporate / luxury words or the Minimal control |
| Off | no caption steps | — | — | `controls.captions = off` |
| News, Corporate, Luxury, Documentary | as Minimal with the brand font/colour when a brand kit exists | | | labelled, not distinct until Phase 2 |

- **Speaker colours**: `add_caption_track.speakers: [{src, start, end, speaker}]` (source-timed turns, ≤ 4,000, tagged at cue build time through `timemap`) + `set_caption_style.speaker_colors: {S1: "#ffffff", S2: "#ffd166"}` (brand palette first, then the existing 5-colour cycle). This never compares clocks, which is the `assign_caption_speakers` drift fixed by not using it.
- **Sentence breaks and timing** are the existing `cues_from_segments` (`ingest/caption_format.py:371`: phrase cuts at punctuation and 0.35 s pauses, extend-don't-split for reading speed).
- **Emphasis words** (`captions.emphasis_words`): the top-importance content words per sentence (strong numbers, contrast words, topic-peak nouns; ≤ 2 per cue) are computed and stored in the EDP and rendered in the accent colour in Dynamic mode (§4.7.3).
- Every mode is a decision with `reason.code: caption_mode` and evidence (content type, energy, platform, control).
- Cues map the reference transcript through lane `a1` when the dialogue lane exists (§2.4), else through v1 per `src`.

#### 4.7.3 Per-word highlight and keyword accent (lane EB-11b, Wave C, gated before EB-12's E3)

The brief's reel look is "captions with highlighted words" (§4, §17). Today `word_emphasis` emits UPPERCASE 2-word chunks in the `hook` role at canvas centre with no current-word colour (`dispatch.py:3016-3035`) — a chunked subtitle, not a highlighted one. Phase 1 ships the highlight:

- **Schema** (`edl/schema.py`, both fields optional, default `None`, so every existing EDL and every existing render is byte-identical): `TextClip.emphasis: list[int] | None` — indices of the whitespace-separated tokens of `text` to draw in the accent; `CaptionLook.accent: str | None` — the accent colour (`#rrggbb[aa]`, validated like `color`). One `RENDER_BEHAVIOR_VERSION` bump (31 → 32) because a render WITH `emphasis` set differs; the frame-map goldens do not change (no golden case sets it) and a new golden case is added that does.
- **The look**: the highlighted token(s) in `accent` (brand palette's accent when a brand kit exists, else `#ffd166`), drawn at 1.08 × the cue size with the same stroke; the other tokens in `color`; no per-token animation in Phase 1 (a `pop` in-animation on the cue is the existing `anim_in`).
- **Viral mode = karaoke by static cues.** `add_caption_track(style="word_emphasis", chunk_size=4, highlight="current_word")` groups words into chunks of ≤ 4 words / 18 characters that never cross a sentence end, and emits ONE `TextClip` PER WORD INTERVAL: `text` = the whole chunk, `start` = that word's `t0`, `end` = the next word's `t0` (the chunk's end for the last word), `emphasis = [k]`, `role = "caption"`. A 45 s reel is ≈ 130 cues; Viral is a reel-only mode by the table, so the count is bounded (`add_caption_track` refuses `highlight` over 4,000 cues with a clear message).
- **Dynamic mode = meaning-based accent.** `ig_chunky` cues get `emphasis` = the indices of the cue's `emphasis_words` (≤ 2), no per-word cadence.
- **Renderers**: `render/text_overlay.py` (PIL: per-token advances already exist for emoji clusters, `_emoji_words`; the accent is a per-token fill), `render/ass_writer.py` (export: `{\c&H..&}` override tags per token), and the desktop's `frontend/src/lib/textLayout.ts` + `captionRun.ts` (canvas: per-token fill; `textLayout.test.ts` gains the cases). Both pictures must agree: the WK parity test measures PSNR ≥ 35 dB on 3 sample frames per cue AND a colour-mass test (the accent's pixel share sits under the highlighted token's advance, nowhere else).
- **Card**: "Captions: Viral — word-by-word highlight in {accent}". Until EB-11b lands, the mode table's Viral row falls back to today's chunks and the card says "word-by-word highlight is not available yet"; EB-12's E3 asserts the highlight, so EB-11b is a hard gate for Milestone C, not an item allowed to slip.

Tests: `tests/test_caption_emphasis_render.py` (decoded PNG from `text_overlay` for `"THE FIRST NO"` with `emphasis=[1]`: accent pixels only under token 1; the same through `ass_writer`; RBV bump pinned; an EDL without the fields renders byte-identical to RBV 31); `tests/test_caption_highlight_cues.py` (one cue per word, chunk never crosses a sentence, the 4,000 refusal, cues after cuts through `timemap` as `test_auto_caption_word_emphasis_after_cuts_also_stays_inside_v1` does); vitest `textLayout.test.ts` per-token fill; Playwright WK parity.

#### 4.7.4 Caption quality on client work

- **Model**: the caption pass reads the transcript of record from the BEST cached whisper model — `large-v3-turbo` when its ggml is on disk (`Path.exists`, never a download), else `small` — even when analysis ran on `small`; the two transcripts are aligned by word time (±0.15 s) so speaker tags, cut ranges and scores stay on the analysis clock while the WORDS come from the better model. When only `small` exists, the card says "captions from whisper small; a larger model on disk improves names and numbers" (the existing `downloads_needed` gate is offered, once, and never triggered silently).
- **Probabilities**: `w.prob` is the min token `p` from `-ojf` (§3.4); a cue whose words include one with `prob < 0.6` is not flagged on the video (no visual noise) but appears in **"Words to check"** on the Plan tab: `prob < 0.6`, capitalised unknowns, digit runs, and speaker-name near-misses, each a seek button to the cue; the count is on the card ("Captions: Podcast — 14 words to check").
- **Naming**: the axis formerly called `caption_accuracy` is `caption_timing` (coverage × sync × readability × no overlap); the brain does not know if a word is RIGHT, only whether it is on time, and the score must not imply otherwise. `words_to_check` is a separate count on the card and in the score record.

Tests: mode table as a data-driven test; E5 (≥ 95 % of cues carry the right speaker on fixture 2, two colours); `captions_cover ≥ 0.9`; a cue never overlaps a face box after the reframe (`subject_in_crop` + caption band check); with a stubbed `large-v3-turbo` on disk the cue WORDS come from it while cut ranges are unchanged; the planted mis-heard name is in "Words to check".

### 4.8 Music and beats (`music.py`)

- Bed by mood (`music_hint` refined by content type and control): interview → `cinematic_70bpm`; podcast → `lofi_85bpm` or `chill_90bpm`; reel `energy ≥ 7` → `upbeat_120bpm`; an uploaded track in `allowed_paths` wins when the prompt names it. The four beds are procedural 180 s loops at −18 LUFS (`presets/music/*.json`, `source: procedural`); the card always says "built-in bed — replace it with a licensed track in the Music panel".
- **Episodes never get a looping bed under 35 minutes of conversation.** For `target = episode`: music = an **intro** under the cold open / hook card (≤ 12 s, 1.5 s fade-out at the first chronological sentence) + an **outro** (the last 8 s, fading in over 2 s) + optional **chapter stings** (2 s at each chapter marker, `energy ≥ 6` only). A continuous bed under speech is laid ONLY when the Music control is Standard or High energy set EXPLICITLY (a `touched` control, not a profile default) and the card then says "bed under speech, as asked". `controls.music = none` removes the pass; Subtle on an episode = intro + outro only.
- **Reels** keep a continuous bed (≤ 90 s, the loop is not heard as one).
- **Levels are relative to measured speech, not absolute.** With `speech_lufs` = the integrated loudness of the dialogue lane's kept speech (from `audio.loudness_i` over the kept ranges): Subtle → bed at `speech − 24 LU`, ducked a further 6 LU under speech; Standard → `speech − 18`, duck 6; High energy → `speech − 14`, duck 4 (today's fixed −14 / −18 with a 4 dB duck, `dispatch.py:3615`, left music only 4 dB under speech, and "Subtle"'s 2 dB duck was inaudible as ducking). `set_duck.to_db` is derived so that music-under-speech sits at least 18 LU below speech for Subtle; the Reviewer's `music_too_loud` reads the verify render's speech-band and music-band LUFS.
- Beats: bed sidecar grids (`presets/music/*.json`: `bpm`, `beat_grid_offset`); uploaded music: librosa `beat_track` when importable, else the numpy onset-envelope autocorrelation (new, for the `.app`). Beat use in Phase 1 is **alignment only**: punch-ins and the hook card snap to the nearest grid beat within ±0.15 s; speech is never split on beats (that stays the explicit `beat_sync` recipe). `beat_pulse` on B-roll/montage spans only at `energy ≥ 8`.
- Reason cites arousal mean, wpm and the control (`music_mood`, `music_intro`, `music_outro`, `music_sting`, `beat_grid`).
- Tests: interview → cinematic; the structured bed's grid recovered within 20 ms; punch-in times snap when music is on and not when off; E.2's episode at Subtle yields exactly an intro clip (≤ 12 s) and an outro clip (8 s) on the music lane and nothing between; with Music = Standard touched, one continuous bed and the card's "as asked" line; the verify render's music-under-speech is ≥ 18 LU below speech for Subtle.

### 4.9 Basic B-roll suggestions (`broll.py`)

- When: a scene with a concrete noun phrase (`has_number`, product/place nouns from the lexicon) and `importance ≥ 0.4`, during a single-angle stretch ≥ 12 s.
- Search: `ai/broll.search_broll`'s index scoring (`ai/broll.py:99`) over `<session>/uploads/broll/` and folders the person registered in the panel (each added to `allowed_paths` by the upload rule); never `VAI_BROLL_BIN` implicitly; `.txt` sidecars read with a 4 KB cap.
- Rank by relevance; **no candidate above 0.35 → no suggestion** (never a random keyword match); ≤ 1 per 45 s; the reason carries the matched tokens and a suggestion whose matched tokens are all stop-words is rejected.
- Output: `broll_suggest {at, dur, query, candidates[]}` → `add_marker` via `$brain:markers` + `suggestions.json`; the panel's "Place" runs the normal UI `add_clip` dispatch on v2 (muted, 2.5-4 s, starting ≥ 0.8 s into the scene so the speaker is seen first). Phase 1 never inserts. Phase-2 hook: a narrow `place_broll {suggestion_id}` tool that reads `suggestions.json` and the `allowed_paths` rule.
- Tests: B-roll bin fixture → the planted keyword sentence gets its clip as a marker; sentences with no match get nothing; `broll_suggestions_leq` holds.

### 4.10 The Energy table (`brain/energy.py`)

| Knob | energy 1 | 3 | 5 | 7 | 9 | 10 |
|---|---|---|---|---|---|---|
| min removable silence (s) | 1.5 | 1.2 | 0.8 | 0.5 | 0.4 | 0.35 |
| silence keep-pad (s) | 0.25 | 0.20 | 0.15 | 0.10 | 0.07 | 0.06 |
| dead-air in-turn (podcast, s) | 2.0 | 1.6 | 1.2 | 0.9 | 0.7 | 0.6 |
| soft fillers removed | no | no | no | yes | yes | yes |
| importance floor to keep (episode) | 0.10 | 0.15 | 0.20 | 0.28 | 0.35 | 0.40 |
| camera min shot (s) | 4.0 | 3.5 | 2.5 | 2.0 | 1.6 | 1.4 |
| camera max hold (s) | 40 | 30 | 25 | 14 | 10 | 8 |
| switch cost | 0.45 | 0.40 | 0.35 | 0.25 | 0.15 | 0.12 |
| punch-in min gap (s) | 40 | 30 | 20 | 12 | 6 | 5 |
| punch-in scale | 1.08 | 1.08 | 1.10 | 1.12 | 1.14 | 1.15 |
| caption mode default (reel) | Minimal | Podcast | Dynamic | Dynamic | Viral | Viral |
| cold open | title only | title only | if quotable | yes | yes | yes |
| turn-boundary pause floor (s) | 0.30 | 0.30 | 0.30 | 0.15 | 0.15 | 0.15 |
| protected pauses | all | all | all | all | laughter | laughter |
| acoustic fillers removed | no | no | ≥ 0.7 | ≥ 0.7 | ≥ 0.6 | ≥ 0.6 |
| jump-cut hide scale (single camera) | 1.06 | 1.08 | 1.08 | 1.12 | 1.12 | 1.12 |
| punch-in push (s) | 3.0 | 2.0 | 0.4 | 0.4 | 0.3 | 0.3 |

Data-driven test: every row monotone in the expected direction; every pass reads only this table.

---
## 5. The Edit Decision Plan and its mapping to dispatch

### 5.1 EDP schema (`brain/schema.py`, `extra="forbid"`, canonical JSON, 4-decimal times)

```
EDP {
  version: 1, id: "d_[0-9a-f]{8}", planner_version, created,
  graph: {id, digest},                       # the graph this EDP was planned on; a stale graph is refused at resolve time
  controls: {content_type, energy, captions, broll, sfx, music, duration_s, platform, ratio, count, speakers?, names?},
  style: <profile id>, seed: 0, previous: "d_…" | null, scope: {src, t0, t1} | null,   # previous+scope for revisions
  brain: BrainId, content_brain: BrainId | null,
  summary: {project_type, target, duration_s, hook {sent, src, t0, t1, quote}, story [{beat, sents[]}],
            dialogue {src, lane, offsets, seams} | null, camera {angles, switches, at_cut}, pauses_kept,
            music {bed|src, shape: intro_outro|bed|none, rel_lu, duck_lu}, captions {mode, style, position, speakers, highlight, words_to_check},
            graphics {lower_thirds, hook_card}, broll_suggestions, estimated_seconds, deferred [{asked, why}]},
  decisions: [Decision],
  children: [EDP-lite for shorts],           # each with its own decisions and window
  compiled: {plan_id, steps, sentinels[]},   # filled by compile()
  score: {before, after} | null              # filled by review()
}
Decision {
  id: "k_[0-9]{4}", kind, ref: {src, t0, t1} | null, params: {} ,
  reason: {code, facts: [graph ids], text}, score 0..1, confidence 0..1, optional: bool, by: BrainId,
  produced: {clip_ids[], keyframe_times[], marker_ids[], cue_range} | null   # filled after Apply from the resolver's footprint
}
```

`reason.facts` MUST resolve to ids in the graph the EDP names; `brain/schema.py` validates this on write and the eval asserts it on every step (E8). `reason.text` renders timecodes with `live.smpte` on the project grid so it reads like the ruler.

### 5.2 The closed reason-code vocabulary (`brain/reasons.py`)

One human template per code, used by the card, the Inspector, the run log, OpsLog "Why" rows and the reply, so they all say the same thing.

| code | template |
|---|---|
| `silence` | "silence of {dur} at {tc}" |
| `pause_kept:{why}` | "kept {dur} of the pause at {tc}: {after the laugh | before the answer to a hard question | after an emotional line | after the conclusion}" |
| `filler` / `soft_filler` / `filler_acoustic` | "filler “{word}” at {tc}" / "an “uh” the transcript missed, at {tc} ({confidence})" |
| `dialogue_lane` | "dialogue from {leaf} on lane a1; camera microphones muted; {n} seams faded" |
| `jump_cut_hide` | "scale step at the cut at {tc} to hide the jump" |
| `music_intro` / `music_outro` / `music_sting` | "{mood} under the opening, out at the first sentence" / "{mood} under the last 8 s" / "sting at chapter “{topic}”" |
| `question_kept` | "question kept: {hook ≥ 0.5 | opens the topic | the answer needs it}" |
| `false_start` | "false start “{text}” before “{kept}” at {tc}" |
| `repeat` | "repeats what was said at {tc_of}" |
| `dead_air` | "{dur} of dead air inside {speaker}'s answer at {tc}" |
| `weak_question` | "question adds nothing the answer does not say; the answer stands alone" |
| `dead_conversation` | "{topic}: {dur} with little content" |
| `technical` | "{why} at {tc}" |
| `duration_fit` / `best_window` | "{kept} of whole sentences to fit {asked}; opens on the strongest standalone statement" |
| `hook_strongest_opening` | "strongest opening: {axes} ({score})" |
| `cold_open` | "quotable line moved to the front; the original order resumes" |
| `speaker_turn` | "{speaker} starts speaking" |
| `question_shown` | "a question is shown even though it is short" |
| `reset_wide` | "wide reset after {dur} on one angle, at a sentence boundary" |
| `reaction` | "{listener} reacts ({motion|laughter})" |
| `overlap_wide` | "both speak; the wide holds" |
| `at_cut` | "camera change placed on the jump cut at {tc} (one seam, two reasons)" |
| `energy_motion_fallback` | "no speech here; switched by sound and motion" |
| `emphasis_peak` | "speaker delivers a key point ({rms_z}σ louder, {stretch}× slower)" |
| `question_punch_out` | "pull back on the question" |
| `hook_emphasis` | "punch in on the hook statement" |
| `subject_follow` / `subject_moved_median` | "follows the face" / "subject moves too fast to pan; framed at its median position" |
| `caption_mode` | "Captions: {mode} ({content_type}, energy {energy})" |
| `lower_third_intro` | "{name}'s first appearance" |
| `music_mood` | "{mood} bed: {content_type}, arousal {arousal}, {control}" |
| `beat_grid` | "aligned to the beat at {tc}" |
| `broll_reference` | "“{phrase}” during {dur} on one angle; {candidate} matched {tokens}" |
| `control` | "{control}: {value}" |
| `review_fix:<issue>` | "fixed after review: {issue}" |
| `user_named` | "you asked: “{clause}”" |
| `protected_skip` | "left alone: you edited this after V{n}" |

### 5.3 Mapping table: brain decision → op → args → reason template

Rule: an existing tool when one exists; otherwise a new tool narrower than the denied one, guarded, staged, covered by `tests/test_all_tools_smoke.py` and `tests/test_tool_schema_completeness.py` like any other. No tool leaves `PLAN_DENY`.

| Decision kind | Dispatch op (stage) | Args (literal or sentinel) | Resolution | Reason codes |
|---|---|---|---|---|
| `cut_range` (all `tighten` codes) | **NEW** `cut_source_ranges` (2) | `{track: "v1", ranges: "$brain:cuts", plan_ref}` | one arg dict per step: `ranges = [{src, start, end}]` merged, ≤ 2,000 per step (the compiler splits a longer list into time-ordered steps, §4.6.4); the tool maps through the live EDL before every cut (`dispatch._cut_source_ranges`, `dispatch.py:3982`); edges already trough-snapped at plan time (§4.6.2) | `silence` … `technical` |
| `keep_pause` | none (a kept pause is the ABSENCE of a cut) | — | recorded in the EDP so the card, the Inspector and "Undo this decision" (which plans the cut) can show it | `pause_kept:*` |
| `dialogue` | **NEW** `sync_dialogue_lane` (2, emitted LAST in stage 2) | `{src, lane: "a1", offsets: {angle_src: s}, seam_fade_s: 0.005, mute_camera_mics: true}` | literal; the handler rebuilds a1 from the live v1 (§4.6.1, §5.4) | `dialogue_lane` |
| `keep_window` / `keep_segments` | `cut_source_ranges` (2) | `{track: "v1", ranges: "$brain:keep", plan_ref}` | the complement of the kept set, tail first | `best_window`, `duration_fit` |
| `open_on` | `split_at` (2) then `reorder_clips` (2) — TWO steps | step A `{track: "v1", time: "$brain:story_splits", plan_ref}`; step B `{track: "v1", order: "$brain:story_order", plan_ref}` | A: fan-out of split times mapped from the hook scene's edges through `timemap` against the pre-split tree (`split_at` on v1 ripples nothing, ai-tools map §2.10, so every time in the list stays valid); B: resolved against the post-split tree: the clip whose `(src, in, out)` equals the hook span goes first, others keep order | `hook_strongest_opening` |
| `cold_open` | `split_at` (2), `duplicate_clip` (2), `move_clip` (2) — THREE steps | A as above; B `{clip_id: "$brain:story_dup", plan_ref}`; C `{clip_id: "$brain:story_move", new_start: 0, close_gap: true, plan_ref}` | B: the v1 clip whose source span equals the hook span; C: the clip with that span and the greatest `start` (the duplicate) | `cold_open` |
| `switch_angle` | **NEW** `apply_camera_plan` (2, after the cuts by stable stage order, `validate.py:833`) | `{switches: "$brain:camera", offsets: {src: s}, plan_ref}` | one arg dict: `switches = [{src, at_src, until_src, angle_src}]` on the live tree; the handler splits at the live time of `at_src` and swaps the piece to `angle_src` (§5.4) | `speaker_turn`, `question_shown`, `reset_wide`, `reaction`, `overlap_wide`, `at_cut`, `energy_motion_fallback` |
| `punch_in` / `punch_out` / `jump_cut_hide` | `add_keyframe` (4) | `{clip_id: "$brain:punch_ins", plan_ref}` | fan-out: per punch `{clip_id, props: ["scale","x","y"], values, time (clip-local), interp}` — 2 keys for a step release, 4 for a timed one, 1 step key at clip-local 0 for a `jump_cut_hide` (§4.4, §4.3 rule 1b); one `interp` per emitted Keyframe; clip id + clip-local time from `timemap.source_to_timeline` on the live EDL; a punch whose instant was cut away is dropped with a notice | `emphasis_peak`, `question_punch_out`, `hook_emphasis`, `jump_cut_hide` |
| `reframe` | `auto_reframe` (6) + `set_clip_fit` (6) | `{ratio, subject_track: false}`; `{clip_id: "$v1_all", fit: "cover"}` | literal (the existing pair) | `control` |
| `reframe_pan` | `add_keyframe` (6, after `auto_reframe`) | `{clip_id: "$brain:reframe_pans", plan_ref}` | fan-out per key `{clip_id, props: ["x","y"], values, time}` | `subject_follow`, `subject_moved_median` |
| `captions` | `add_caption_track` (7) + `set_caption_style` (7) | `{style, position, chunk_size?, max_chars?, speakers: [{src, start, end, speaker}]}`; `{look…, speaker_colors: {}}` | literal (turns are in the args, ≤ 4,000, validated) | `caption_mode` |
| `lower_third` | `add_lower_third` (8) | `{name, handle?, start: "$brain:lower_thirds", end, speaker, plan_ref}` | fan-out per speaker: live time of the first kept turn; `end = start + 4` | `lower_third_intro` |
| `hook_card` | `apply_hook_stack` (8) | `{text, duration, visual: "none" when a punch-in opens the reel else "punch_in", audio: "fade_boost"}` | literal; text frozen in the EDP | `hook_strongest_opening` |
| `music` (reel bed, or an episode's explicit bed), `beat_align` | `add_music` (9), `set_duck` (9), `fit_music_to_video` (9) | `{src: <bed path or upload>, volume_db: speech_lufs − rel_lu, duck: true, loop: true}`; `{track: "music", enabled: true, to_db}`; `{}` | literal; levels computed at plan time from the measured speech loudness (§4.8); `beat_align` only shifts other decisions' `at` at plan time | `music_mood`, `beat_grid` |
| `music_intro` / `music_outro` / `music_sting` (episodes) | `add_music` (9) ×1 per piece with `start`, `duration`, `fade_out`/`fade_in` (existing args), `set_duck` (9) | `{src, start: 0, duration: ≤ 12, fade_out: 1.5, volume_db}`; `{src, start: "$brain:outro_start", duration: 8, fade_in: 2, plan_ref}`; stings `{src, start: "$brain:markers", duration: 2}` | the intro is literal; the outro's start and the stings' times are live (`$brain:outro_start` = committed duration − 8 after the cuts; `$brain:markers` fan-out) | `music_intro`, `music_outro`, `music_sting` |
| `chapter` / `broll_suggest` | `add_marker` (0) | `{time: "$brain:markers", label, color, plan_ref}` | fan-out per marker: live time | `broll_reference`, `control` |
| `shorts` (children) | `make_shorts` (3) | `{target_count, max_dur, min_dur, save_as_sessions: true, from_timeline: true, windows: [{start, end, hook}]}` (**additive args**; ≤ 20 windows in TIMELINE seconds of the parent after its cuts) | literal. Today a child is ONE `Clip(src, in, out)` (`dispatch.py:7175-7205`), which cannot hold a window that spans a camera switch or a tighten cut; `from_timeline: true` instead COPIES the parent's v1 pieces (and the a1 dialogue pieces, creating the child's a1) that intersect the window, clipped at its edges, source spans preserved, re-based to `start = 0` — so a child keeps the parent's tighten and multicam and opens in sync. Each child EDP then runs its own reel passes over the copied layout (punch-ins, Viral/Dynamic captions, 9:16 keyframed reframe from the closes, bed, hook card, `open_on` when standalone, and a final `sync_dialogue_lane`). `_finish_children` (`executor.py:1190`) runs each child's compiled sub-plan from `children[i]` instead of the fixed `tighten + reframe + captions + hook` draft. `TERMINAL_RECIPES` (`recipes.py:231`) ends a parent plan only for the `shorts` CARD; the `edit` card is one recipe, so later-stage steps still run on the parent and the children are finished after the parent's commit, as today | `best_window` |
| `loudness`, `export_preset`, `audit` | `set_loudness_target` (10), `apply_export_preset` (11), `audit_aesthetic` (12) | literal | — | `control` |
| `restore` (revision inverse of a cut) | `cut_source_ranges` cannot restore; Phase 1 restores a cut by ⌘Z of the run or by the panel's "Undo this decision" which plans `add_clip`-free inverse: `trim_clip` widening the neighbour piece (`dispatch.py:1850`) | `{clip_id: "$brain:restore", out: +Δ}` | resolved to the piece whose source span borders the cut | `user_named` |

Tools the brain deliberately does not use: `multicam` (clears v1, `dispatch.py:6418`), `auto_reframe(subject_track=true)` (re-encode), `remove_silences` / `remove_fillers` (they read only the first v1 clip's transcript; `cut_source_ranges` is strictly more general and they remain the recipes' tools), `diarize` / `assign_caption_speakers` (superseded by the speakers layer and the `speakers` arg), `add_clip` / `find_broll` (a person places B-roll; the graph indexes the bin read-only through `ai/broll`).

### 5.4 The three new tools, exactly

| Tool | Schema (`agent/tools.py`) | Handler behaviour (`agent/dispatch.py`) | Guards (`validate.py` + `guard_step`) |
|---|---|---|---|
| `cut_source_ranges {track: "v1", ranges: [{src, start, end}] ≤ 2000, why?}` | stage 2; `ARG_BOUNDS`: each range ≥ `_MIN_CUT_S`, ≤ 600 s, inside the source's duration | a public wrapper over `_cut_source_ranges(store, track_id, [(src, s0, s1)])`: merges overlapping ranges per `src`, cuts last-first, re-maps through the live EDL before every cut; one commit (a `batch()`); returns `{cuts, removed_s, skipped}`; a fast path (map once, cut last-first, re-map only when a leading gap exists) is taken when the measured loop on the 60-minute fixture exceeds 15 s (open decision §15.3) | `src` must be a v1 clip's file or its `.origin` ancestor AND inside `facts.allowed_paths`; ≤ 2,000 ranges |
| `apply_camera_plan {switches: [{src, at_src, until_src, angle_src}] ≤ 600, offsets: {src: seconds}}` | stage 2 | per switch: `split_at` at the live times of `at_src` and `until_src` on the piece whose source span contains them, then a private `_set_clip_angle`: swap `src` to `angle_src`, shift `in_`/`out` by `offsets[angle_src] − offsets[src]`, keep transform/effects/audio/keyframes/fades, record a `derived_from` note in the op summary; never clears v1; inside one `batch()`; clamps to the angle's extent and keeps the reference angle for any remainder | every path an ingested media upload in `facts.allowed_paths`, probed as media; ≤ 600 switches; refused when `until_src ≤ at_src` |
| `restore_version {id}` | UI-only (in `PLAN_DENY`, like `paste_clips`) | reads `versions.json`, loads the snapshot `snapshots/{op_seq:05d}_{edl_hash}.json`, `commit("restore_version", {id, label}, "Restored V…")` of that tree; refuses when the snapshot was pruned (`no longer restorable`) | id must exist; the snapshot must exist under the session |
| `sync_dialogue_lane {src, lane: "a1", offsets: {path: seconds}, seam_fade_s: 0.005, mute_camera_mics: true}` | stage 2 (last of the stage by the compiler's order); `ARG_BOUNDS`: `seam_fade_s` 0-0.05, ≤ 16 offsets | idempotent, inside one `batch()`: (1) `lane` = the `audio` track of that id, created by `_free_audio_lane`'s rule if absent (label "Dialogue"); (2) every clip on `lane` whose `src` is `src` is dropped (the tool owns them; a clip of another src on the lane is left and reported); (3) if the music lane holds a clip of `src` (the `_handoff_audio_only` placement) it is removed once and the summary says so; (4) for every v1 media `Clip` in timeline order whose `src` is in `offsets` or equals `src`: append `Clip(src, in_ = piece.in_ + offsets[src] − offsets[piece.src] (EB1 as built: the convention is ONE — an event at reference second r is at file second r + offsets[file]; measured on the two-camera fixture), out likewise, start = piece.start, audio.gain_db = 0, fade_in = fade_out = seam_fade_s, linked_to = None)`, clamped to the source's extent, quantised to the fps grid; the first/last clip's outer fade is 0; (5) when `mute_camera_mics`, `audio.mute = True` on every v1 piece whose `src` is an angle member (gain preserved); v1 pieces whose `src` is not in `offsets` (B-roll on v1, a title card) are skipped and a1 has a gap there; (6) returns `{lane, clips, muted, removed_from_music, gaps}`; never touches transforms, keyframes, captions or markers | every path an ingested media upload in `facts.allowed_paths` with audio (`source_has_audio`); ≤ 4,000 v1 pieces; refuses a lane that is not `type == "audio"` or is locked; refuses when a1 holds hand-edited clips of `src` in the protected set (the run clarifies, §7.2) |

Additive changes to existing tools (each with schema, `EXTRA_ARGS`/`ARG_BOUNDS`, `change_rules.py` claim, `opLabels.ts` label, smoke coverage): `add_caption_track.speakers[]`, `.max_chars` and `.highlight` (`"current_word" | null`, `word_emphasis` only); `set_caption_style.speaker_colors{}` and `.accent`; `add_lower_third.speaker` read from `speakers.json`; `make_shorts.from_timeline` + `.windows[]`; `add_music.start`/`.duration`/`.fade_in`/`.fade_out` where not already accepted; `_transcripts_by_source` + the two multi-src readers (§2.4) + the a1 mapping for captions.

### 5.5 The `$brain:<kind>` sentinel family (`live.py` → `brain/resolve.py`)

A sentinel step is an ordinary `Step`:

```json
{"tool": "cut_source_ranges", "args": {"track": "v1", "ranges": "$brain:cuts", "plan_ref": "d_5e2c9a17"},
 "why": "silence, fillers, false starts and one repeated answer — 31 cuts", "stage": 2}
```

- **Declared pairs only.** `resolve.BRAIN_SENTINELS = {"cuts": ("cut_source_ranges", "ranges"), "keep": ("cut_source_ranges", "ranges"), "story_splits": ("split_at", "time"), "story_order": ("reorder_clips", "order"), "story_dup": ("duplicate_clip", "clip_id"), "story_move": ("move_clip", "clip_id"), "camera": ("apply_camera_plan", "switches"), "punch_ins": ("add_keyframe", "clip_id"), "reframe_pans": ("add_keyframe", "clip_id"), "lower_thirds": ("add_lower_third", "start"), "markers": ("add_marker", "time"), "outro_start": ("add_music", "start"), "restore": ("trim_clip", "clip_id")}`. `sync_dialogue_lane` takes only literal args (its inputs are files and offsets, known at plan time) and needs no sentinel. `validate_plan` accepts `$brain:<kind>` only for that (tool, arg) pair, requires `plan_ref` to match `^d_[0-9a-f]{8}$` and to exist under `<session>/brain/decisions/`, and requires the EDP's `graph.id` to be the session's current graph (a stale EDP is refused with a `replan` question). `executor.guard_step` (`executor.py:253`) re-checks the same subset at the last line, as it does for paths.
- **Per-step resolution.** `live.resolve_live_args` gains one branch: `if any arg startswith "$brain:" → brain.resolve.resolve(store, tool, args)`, which loads the EDP (cached per run), maps each relevant decision's `ref` through `timemap` against the live EDL AT THAT MOMENT, and returns one arg dict or a fan-out list plus notices ("3 punch-ins dropped: their moments were cut away earlier in this plan"). This is exactly the contract the executor already implements for `$fit_best` (`executor.py:705-719`: `resolved` may be a list; each element is guarded; the step is one `StepOutcome`). The resolver never mutates.
- **Footprint.** The resolver records, per decision, the entities it addressed (clip ids and source spans, keyframe times, cue ranges, marker ids) into `<scratch>/brain/footprint.json`; the pending record carries it to Apply, `preview.lines_for` uses it to attach `why` to card lines, `feedback.py` uses it to match later human ops, and `Decision.produced` is filled from it after Apply.
- **Caps.** Fan-out per sentinel step ≤ `MAX_BRAIN_FANOUT = 4,096` dispatches (a 60-minute podcast at energy 8 ≈ 450 cuts + ≈ 120 switches + ≈ 360 keyframe dispatches); the cap is said, never silent. Every dispatch is inside the one batch, so the single-undo promise holds regardless of count.
- **Ordering inside stage 2** relies on the validator's stable stage sort (`validate.py:833`, `# stable`): the compiler emits `keep` → `cuts` (one or more steps) → `story_splits` → `story_order`/`story_dup`/`story_move` → `camera` → `sync_dialogue_lane` in that order and a test pins it. Stage 4's `add_keyframe` and everything after never restructure v1, so a1 stays valid to the commit.

### 5.6 Compilation (`brain/compile.py`, pure)

1. Sort decisions by kind rank; group by sentinel kind; emit one step per kind present plus the literal steps (`cuts` splits into ≤ 2,000-range steps, §4.6.4; the `dialogue` decision is always the last stage-2 step); assert ≤ 24 steps (a 3-camera podcast with shorts is 16; a reel is 10-13) and otherwise drop the lowest-scored `optional` decision groups with a note in `deferred[]`.
2. `Step.why` = the group's aggregate template ("Removed 31 stretches: 14 silences, 9 fillers, 5 false starts, 2 repeats, 1 weak question"); `stage` from `TOOL_STAGE`.
3. Postconditions from `CHECK_SPECS` (existing + §6.1), capped at 20 by priority (blocking first, then headline).
4. Questions (≤ 4): only genuine ones: the analysis gate, a `go` over `LONG_RUN_SECONDS` (90 s, `planner.py:1652-1655`), a `downloads` entry for whisper ggml, an unnamed speaker when lower thirds were asked (non-blocking, default names "Host"/"Guest"), a B-roll folder when B-roll was asked and no bin exists.
5. `Plan.intent = "edit"` (or `"edit_revise"`), `title` from the summary ("Interview → 45 s Reel: 7 cuts, 3 angle changes, 2 punch-ins, captions, music"), `estimated_seconds` from `costs.py` (+ per-tool estimates for the new ops), `brain` and `content_brain` as recorded in the EDP.

---

## 6. Editor → Reviewer → Revision, and the Editing Score

### 6.1 New verifier checks (`CHECK_SPECS` + `brain/checks.py`)

| Check | Measures (from the after-tree + `timemap` + graph) | Blocks? |
|---|---|---|
| `no_cut_mid_word(tol=0.02)` | every AUDIO seam created by this run — a1 seams when the dialogue lane exists, v1 seams otherwise — lies outside `[w.t0 + tol, w.t1 − tol]` of every kept word, words mapped through the lane that plays them; camera switches over a continuous a1 are exempt | **yes** (EDL + the transcript in the side-effect snapshot, measurable in-batch; open decision §15.2 if the K3 review disagrees) |
| `dialogue_in_sync(tol=half a frame)` | when `a1` holds the dialogue source: for every v1 media piece whose `src` is an angle member or the dialogue file, the a1 clip covering `piece.start` plays the same reference second (`in_` mapped by the offsets) within `tol`; no a1 clip over a v1 gap; every v1 angle piece muted; no v1 transition | **yes** (EDL-only) |
| `protected_untouched` | no entity of the protected set (§7.2) changed | **yes** (EDL-only: a diff of two trees against an id/range set) |
| `seam_click(max=0.1)` | verify render: the first-difference peak within ±2 ms of each audio seam < `max` full scale | no (render-based; 0 expected) |
| `tighten_uniform(max_ratio=1.3)` | removal density (removed seconds per kept minute) of the last quarter vs the first quarter | no |
| `jump_cut_hidden(min_ratio=0.9)` | share of tighten seams ≥ 0.4 s that coincide with a `src` change (multicam) or a scale step ≥ 0.08 (single camera) | no |
| `punch_min_hold_geq(s=0.8)`, `single_interp_per_key` | every punch-in holds ≥ 0.8 s before its release; every emitted Keyframe has one `interp` | no / **yes** (EDL-only, trivially) |
| `pauses_protected(min_ratio=1.0)` | every `keep_pause` decision's span is still present at ≥ its kept length | no |
| `min_shot_geq` (existing), `camera_switch_rate_leq(per_min)` | v1 pieces; `src` changes per minute | no |
| `speaker_on_screen(min_ratio=0.85)` | share of each speaker's kept talking time during which the chosen angle's hint is that speaker or `wide` | no |
| `punch_in_gap_geq(s)`, `punch_on_emphasis(min_ratio=0.8)` | keyframe groups vs the style gap and switches; emphasis at the key time after cuts | no |
| `reframe_pan_speed_leq(widths_per_s=0.15)`, `subject_in_crop(min_ratio=0.95)` | keyframed pan; face boxes inside the crop window | no |
| `reel_duration_within(target_s, tol_s=1.0)` | committed EDL duration (the existing `duration_between` is stale across cuts by design, `schema.py:535-538`) | no |
| `hook_is_strongest(delta=0.05)` | the first kept sentence's hook within 0.05 of the max among `standalone ≥ 0.75` | no |
| `captions_speakers_tagged(min_ratio=0.95)`, `captions_cover` (existing), `caption_face_clear`, `caption_highlight_present` (Viral: every cue has `emphasis`) | cues with a speaker; coverage; no cue over a face box; the highlight exists | no |
| `broll_suggestions_leq(per_45s=1)` | markers per span | no |
| `speaker_share_between(speaker, min, max)` | interview balance (guest ≥ 0.55) | no |
| `editing_score_geq(total)` | §6.3 | no (headline) |

### 6.2 The bounded loop (inside `service._run_and_stream`, preview mode, `intent == "edit"`)

```
edp0 = plan(graph, controls, style, seed); write(edp0)
plan0 = compile(edp0)
r0 = executor._preview_run(plan0)                      # dry run on a fresh scratch store; K3 net included; rollback → clarify as today
review0 = brain.review.review(r0.after, graph, edp0, verify_render(r0.after) if any render-based postcondition and duration ≤ 600 s)
score0 = brain.score.editing_score(review0)
fixes = brain.revise.amend(edp0, review0)              # only high-confidence, rule-backed fixes (table below)
if fixes:
    edp1 = apply(fixes); write(edp1, previous=edp0.id); plan1 = compile(edp1)
    r1 = executor._preview_run(plan1); review1 = …; score1 = …
    final = (edp1, r1, review1) if score1.total ≥ score0.total else (edp0, r0, review0)   # a revision never lowers the score
card = lines_for(before, final.after) + decisions + score before/after + open issues; pending.preview.decisions = final.edp.id
```

At most ONE revision round (two dry runs) per preview; never after Apply; the plan's `estimated_seconds` × (rounds + 1) drives the progress ticker and the `go` gate. Apply re-runs the FINAL frozen EDP. Post-Apply `verify_plan` runs the same checks on the committed store and the reply says "revised once: reel was 48.3 s, now 45.4 s" when a round was applied.

| Issue (brief §28) | Reads | High-confidence auto-fix |
|---|---|---|
| bad cut mid-word | after-tree seams × mapped words | move the cut to the nearest word gap (`cut_range` bounds adjusted) |
| awkward silence (pause ≥ 1.2 s inside a kept scene, or < 0.15 s of air before speech after a cut) | words × seams | extend or shrink the cut by the pause rule |
| duplicate shot / repeated sentence survived | `repeat_of` × kept scenes | add the `repeat` removal |
| caption overlap or > 2 cues visible | captions track | rebuild with `max_chars − 4` |
| caption coverage < 0.9 | `captions_cover` | rebuild captions after the cuts (stage order guarantees this; a miss means an earlier tool failed) |
| off-screen subject after reframe (face outside crop > 1 s) | boxes × transform | add a pan key or frame at the median |
| camera switches too frequent (> 1 per min-shot) or too rare (hold > 2× max) | v1 `src` changes | re-run `camera` with the switch cost adjusted |
| punch-in on nothing (emphasis < 0.5 at the key time after cuts) | keys × graph | remove the punch decision |
| speaker balance off (interview: guest < 0.55) | kept scenes × speaker | drop the lowest-importance host scenes |
| music too loud (music-under-speech less than the mode's `rel_lu` below speech; verify render) | render | `set_duck(to_db − 4)`, then `add_music.volume_db − 4` |
| audio jump (level step > 6 dB across a cut, 200 ms RMS each side) | PCM at seams | grow the louder side's pad to its next trough (≤ 60 ms); if it remains, `add_fade(0.02, 0.02)` on the a1 pair (0.05 on v1 pieces when no dialogue lane) |
| seam click (`seam_click` > 0.1) | verify render | move the edge to the next trough within ±30 ms; else the fade above |
| dialogue lane out of step (`dialogue_in_sync` would fail) | after-tree | never reached by a brain plan (the lane is rebuilt last); a hand-edited a1 triggers the §7.2 clarify instead |
| loose tail (`tighten_uniform` > 1.3) | after-tree | none automatic; the card names the range and offers "Tighten the rest" as a scoped revision |
| black frame / gap on v1 | after-tree | close the gap |
| hook not in the first 3 s (first speech scene hook < 0.5 and no overlay) | after-tree × graph | `open_on` the top alternate or add the `hook_card` |
| B-roll marker on a stop-word-only match | suggestions | remove |
| safe zones, brand kit | existing checks | as today |
| unsupported claim vs visual (news) | — | not in Phase 1 (§11.6) |

Each issue is `{code, severity, at, evidence, fix: decision | None}`; open issues (no confident fix) are listed on the card and in the reply ("done with issues" language exists in `summary.py`). A fix never widens the change set beyond the issue (asserted with `changes.summarize` on the two after-trees).

### 6.3 Editing Score (`brain/score.py`, 0-100 per axis, deterministic from after-tree + graph + optional render)

The score has two kinds of axis and the card says which is which (critique, MINOR): **measured** axes read the after-tree and the render and would be the same numbers for a hand edit; **objective** axes re-measure the planner's own targets (hook, pacing, story) and are therefore "did the plan do what it set out to do", not an independent judgement. "Before/after" is shown for measured axes; objective axes show "target met / not met" and never as an improvement claim. The real-footage tier (§13.6) is the independent judgement.

| Axis | Kind | Measure |
|---|---|---|
| story_flow | objective | `100 − 15·(broken Q/A links) − 10·(non-standalone scene after a join) − topic zig-zag penalty (topic changes/min above the source's own rate)` |
| hook_strength | objective | `100·hook(first speech scene)`, +10 with an overlay hook, −30 if the first 3 s are a pause |
| pacing | objective | speech density vs the mode's target (podcast 0.75, reel 0.9), pause budget met (protected pauses count as met), filler count 0, kept-scene length distribution |
| visual_variety | measured | shot-change rate vs the energy target (0 at none or > 2× target) + punch-ins present + angle entropy + `jump_cut_hidden` |
| audio_quality | measured | noise floor, clipping, seam clicks, level steps across cuts, one voice (`dialogue_in_sync`), music/speech separation in LU (render when available, else EDL: duck present, fades at seams) |
| caption_timing | measured | coverage × sync (`captions_sync` tol 0.1) × readability (cps ≤ 17) × no overlap; **`words_to_check` is reported beside it as a count, never folded into the score** |
| brand_compliance | measured | brand kit applied when one exists, fonts/colours match, safe zones; 70 flat when no kit |
| **total** | — | mode-weighted: reel `hook 0.25, pacing 0.20, visual 0.15, captions 0.15, audio 0.10, story 0.10, brand 0.05`; podcast `story 0.25, audio 0.20, pacing 0.15, captions 0.15, visual 0.10, hook 0.10, brand 0.05`; interview `story 0.25, audio 0.15, pacing 0.15, captions 0.15, hook 0.15, visual 0.10, brand 0.05`; the card shows the total with the label "plan score", the measured sub-total separately |

Stored in the EDP, the `prompt` op's args (`score: {before, after}`), `versions.json` and the benchmark report per rung. Used advisorily (`editing_score_geq` is a headline check, never blocking). `audit_aesthetic` (`show/audit.py:82`) remains the stage-12 gate and feeds `brand_compliance`.

---

## 7. Versions, protected manual edits, natural-language revisions

### 7.1 Versions (`brain/versions.py`, `<session>/versions.json`)

A Version is a named label on a snapshot `commit()` already wrote (edl map §2.6 recommendation adopted): `{id: "v_N", label, op_seq, edl_hash, decisions_id | null, brief, style, kind: origin|brain|manual|restore, created, pinned}`. Written after every applied brain run ("V1 Premium Podcast") and by "Save as version" (manual; no EDP). Nothing is copied.

- **Restore** = `restore_version {id}` (UI-only dispatch, §5.4) → `commit("restore_version", …)` of that snapshot's tree: an ordinary op, one ⌘Z, a History row; the instant-preview engine shows it at once. Restoring never deletes later snapshots.
- **Compare** = a two-column card from `changes.summarize(a, b)` (`changes.py:180`) plus both versions' summaries (duration, cuts, switches, hook) and Editing Scores. Video side-by-side is Phase 2.
- **Prune**: `edl/snapshot.py` (`MAX_UNDO = 500`, `UNDO_DISK_BUDGET_BYTES = 64 MiB`, `:41-42`) gains a one-line exemption set read from `versions.json` for `pinned` versions; an unpinned version whose snapshot was pruned is shown greyed "no longer restorable". A brain version is pinned by default; a manual one is not.
- The version whose hash equals the live EDL carries the dot; after any hand edit the strip shows "V1 + 3 edits". Undo of the run marks the version `undone` (the OpsLog horizon rule).
- `.vae` bundles `versions.json`; both `op_seq` and the hash it references are bundled today (`storage_project.py:1-20`).
- `make_shorts` child sessions are written directly to `edl.json` with no ops (`dispatch.py:7175-7205`, edl map §2.6); the brain's children instead record their own `v_1` after `_finish_children` commits, and never reuse the store-bypassing shortcut for versions.

### 7.2 Protected manual edits (`brain/protect.py`)

- **Protected set** = every clip id, overlay id, track and time range touched by any op after the latest brain version whose `by` is not the brain's run (`ops.json`, `Op.by`, `edl/ops_log.py:8-19`), computed as `contract_diff.diff(version_snapshot, live_edl)` (`contract_diff.py:252`) restricted to those ops' before/after hashes. Pure; cached per (version, live hash).
- **The first run protects too (critique, MAJOR).** With no brain version yet, the baseline is the session's ingest state: the protected set = every overlay (text, sticker, caption cue), music clip, keyframe, transition, gain/fade, lock, and every v1 reorder or trim, that was NOT created by an ingest placement (`upload`, `add_clip` from the upload flow, `_add_uploaded_music`, `init`) — read from `ops.json` since `init`. When that set is non-empty (the person has already worked), the analysis-gate card gains a third pair of options: **"Keep my edits and edit around them"** (default; the set is enforced and the plan's `keep_window`, `auto_reframe`, music and caption steps route around it — e.g. an existing caption track is restyled, never rebuilt; existing music is left and no bed is added; a manual title is untouched by `_ripple_overlays` only if the cuts avoid its span, else the run clarifies) / **"Start from the raw footage"** (the run's licence covers replacing overlays, music and captions; the pre-run state is recoverable by ⌘Z and is recorded as version "Original + edits"). Nothing runs over a timeline full of manual work without one of these answers.
- **Planner side**: `revise()` receives the protected set and marks any decision whose `ref` or resolved footprint would intersect it `protected_skip` (emitted as a notice, not a step).
- **Net side**: the BLOCKING check `protected_untouched` compares the dry-run after-tree with the before-tree on the protected entities; any change rolls the run back to a three-option clarify: **"That would move the title you placed at 00:00:04. Keep your title and revise around it / Let the brain move it / Cancel."** "Keep" re-plans with the set enforced; "Let the brain move it" re-plans with `protected = ∅` for the named entities only (recorded in the EDP as `controls.override_protected[]`); "Cancel" does nothing.
- What survives a revision by construction: everything the person did after the version (protected), plus every decision the revision does not name (a delta EDP re-emits only the decisions it adds or replaces; unchanged decisions are not re-run because every op is source-range or live-time resolved against the current EDL, never a replay of v1).
- **Undo this decision** (Inspector): a single decision is reverted without undoing the run: the brain plans the inverse as a normal short plan (still ONE op) with its own card. A cut → `trim_clip` widening the bordering piece by the removed source span, THEN `sync_dialogue_lane` (the restored span needs its dialogue back) and a caption rebuild over the restored span only (`add_caption_track` re-lays cues inside `[t0, t1]` through `timemap`; the existing cues elsewhere are untouched) and the re-placement of any punch-in key whose clip-local time moved (`produced.keyframe_times`) — a half-restore that brings back picture without sound or words is not a restore. A punch-in → `remove_keyframe` of the keys in `produced`; an angle switch → `apply_camera_plan` of one span back to the original `src` (+ `sync_dialogue_lane`, a no-op when nothing moved); a kept pause → the cut it withheld; a marker → `remove_marker`. Test: undoing one cut on E4's result leaves `dialogue_in_sync` true and `captions_cover` unchanged.

### 7.3 Natural-language revision semantics (`brain/planner/revise.py`)

A revision prompt on a session with an applied EDP is read by the existing grammar/`semantics.py` (prompt-machinery map §2, §6.2) into one of a small set of revision intents; each re-runs specific passes with changed parameters over a scoped reference range and emits a delta EDP `{previous: "d_…", scope, decisions: only added/replaced}`.

| Sentence family | Reading | Re-planned passes / parameters | Emitted ops |
|---|---|---|---|
| "remove the joke" / "cut the story about X" | kept scenes with `humour ≥ 0.6` (or a laughter event), or a topic-title match; > 1 cluster → one question with options by time | `tighten` + `cut_range{reason: user_named}` | `cut_source_ranges` |
| "make the first 10 seconds faster" | `TimeRef` first 10 s (timeline) → reference range through `timemap` | `tighten` at `energy + 3`, `emphasis` over the range | `cut_source_ranges`, `add_keyframe` |
| "keep Guest B on screen longer" | speaker S2 + direction longer | `camera` with `MIN_SHOT_S[S2] × 1.6`, switch cost into others × 1.5, `MAX_HOLD` + 6 s | `apply_camera_plan` over the affected spans only |
| "add more reactions" | camera | `camera` with the reaction thresholds lowered (`motion ≥ 0.10`, gap 12 s) | `apply_camera_plan` |
| "use less B-roll" | suggestions | `broll` with the cap halved | `remove_marker` for dropped suggestions |
| "make captions smaller" / "change music" | existing recipes | none / `music` with the next mood or a named upload | `set_caption_style` / `remove_music` + `add_music` + `set_duck` |
| "make this feel more premium" | style | switch style to `premium_podcast`'s knobs (`energy − 2`, captions Podcast, switch cost up, punch-ins scale 1.10) over the whole kept set | as the passes emit |
| "convert this into a 30-second version" | duration target | `select` + `story` over the CURRENT kept set only | `cut_source_ranges` (complement), `split_at` / `reorder_clips` |
| "keep it chronological" | story | re-plan without `story` | `reorder_clips` back to source order |
| selection + "make this section more interesting" (brief §31) | `$selected` → reference range | `tighten` (+2 energy), `emphasis` (cadence ×2), `camera` (one reaction), `broll` (one marker), within the range | as above, scoped |

The contract still judges the words: `contract_hint = {"decisions": "<did>"}` maps each decision kind to the `_LICENSE` categories it may touch (`contract.py:596-673`: `cut_range` → v1 removal/retime; `punch_in` → clip transform keyframes; `captions` → captions track; …). A diff category no decision claims is `unasked` and rolls the run back to a clarify card, exactly as for a recipe plan. For a revision the licence is the previous EDP's footprint plus the delta, so "remove the joke" that also moved a punch-in is caught. `edit_revise` is not a composite family word (`contract.py:53-62`), so `scope`/`unasked` protection stays on for it. Calibrate in shadow mode (`VAI_CONTRACT_SHADOW`) over the editorial suite before enabling (risk §15.9).

---
## 8. User controls and UI surfaces

### 8.1 Where it lives

| Surface | File(s) | What is there |
|---|---|---|
| **Rail tab `brain` — "Edit for me"** (9th item after `ai`, `Clapperboard` icon, chord `Alt+KeyE` shown as ⌥E, verified in the packaged WKWebView as LEFT_RAIL R4 did; fallback ⌥⇧E) | `frontend/src/components/rail/railModel.ts` (one row: `{id: 'brain', label: 'Edit for me', icon: 'clapperboard', tip: 'A professional first cut you can change', command: 'panelBrain'}`), `ToolPanel.tsx`, `keymap/{commands,presets}.ts`, new `components/panels/BrainPanel.tsx` + `brainPanel.css` | the brief field, the seven controls of brief §37, the Style Profile picker, the footage-analysis status per layer with the engine that answered, "Read footage" / "Edit for me", the Versions strip, the B-roll suggestions list |
| **Prompt bar** | `PromptBar.tsx` unchanged; grammar rows for the `edit` family | "Edit this like a premium podcast", "make a 45-second reel", "make the first 10 seconds faster" travel as today; composite family words route to `edit` when a graph exists |
| **Prompt preview card** | `PromptPreviewCard.tsx` gains a **Plan** tab beside **Changes**; new `components/brain/EditPlanTab.tsx`, `lib/brainDecisions.ts`, `lib/editingScore.ts` | Plan tab = the EDP summary (type, target, hook quote with a seek button, story beats, cuts grouped by reason, camera, punch-ins, captions, music, B-roll, "Not done this time", score before/after, open issues); Changes tab = the existing diff list, each line with a "why" when the footprint matches; Apply ↵ / Change ⎋ / **Try another** |
| **Inspector** | `Properties.tsx` mounts new `components/inspector/BrainDecisions.tsx` when a clip's id is in a version's footprint | "Edited by the brain": the decisions that touched this clip with reasons, "Undo this decision" |
| **History (OpsLog)** | `OpsLog.tsx`, `lib/opLabels.ts` | a brain run is one row "Edit for me — Premium Podcast (V1)", expandable to "Why" rows from the op's `decisions` id; restore rows; the Versions strip above History |
| **Top bar ActivityChip** | `components/topbar/ActivityChip.tsx`, `lib/activityStore.ts` | "⟳ Reading footage 42 % · 1:20 · Cancel" during analysis (the chip's threshold rule: start, 25/50/75 %, done, cancelled); "⟳ Planning"; "⟳ Previewing (round 2)" |
| **Timeline** | `Timeline.tsx` + `lib/brainLane.ts` | a thin decision lane above v1 drawing applied decisions as coloured ticks (cut, switch, punch-in, suggestion) with hover reasons, read from `GET …/brain/decisions/{id}`; chapter and B-roll markers are ordinary `Marker`s (excluded from `render_hash`, edl map §1.9); toggle "Show brain notes" in Settings |
| **Settings** | `SettingsDialog.tsx`, `lib/settingsModel.ts`, `prompt_setting.py` sibling `brain_setting.py` | Editor Brain on/off (`brain.enabled`, default on once EB-15 lands; `VAI_BRAIN_ENABLED` override), default controls, default profile, show notes, "Re-rank with Claude" when keyed, "reset signals" |
| **Help** | `Help.tsx` | "What the brain cannot do yet" from `GET /api/brains/capabilities` |
| **Phone** | `mobile/lib/sse.ts` drops prompt events as today; the reply text carries the summary | unchanged |

### 8.2 The panel and the controls (brief §37)

```
Edit for me                                                  ⌥E
──────────────────────────────────────────────────────────────
Brief   [ Edit this podcast like a premium business podcast.   ]
        Style   ( Premium Podcast ▾ )   [Copy & edit…]
Content type   ( Auto ▾ )      Auto · Podcast · Interview · Talking-head reel
                                (Tech · Business · Entertainment · News · Tutorial · Documentary — "Phase 2", shown, not selectable)
Editing energy  ●━━━━━━━━○  4/10   "Calm cuts, longer shots"
Captions        ( Dynamic ▾ )   Off · Minimal · Dynamic · Viral
B-roll          ( Low ▾ )       Low · Medium · High     "Suggests from your bin; you place"
SFX             ( Off )         "Sound effects arrive in Phase 2"  (disabled, reason in aria-describedby)
Music           ( Subtle ▾ )    None · Subtle · Standard · High energy
Duration        ( Auto ▾ )      Auto · 15 · 30 · 45 · 60 · Custom [  ] s
Also make       [ ] 8 short clips for Reels/Shorts
Speakers        Host [ Priya ]  Guest [ Arjun ]          (names → name_speakers; lower thirds)
Dialogue        ( Recorder: zoom_recorder.wav ▾ )   Recorder · Camera B mic · Camera microphones as recorded
Footage  ✓ Transcript (en, 4,812 words, whisper.cpp small, 14 words to check)  ✓ 2 speakers (built-in, no token)
         ✓ 3 angles synced (±10 ms; B is 2 files; drift 41 ms/h corrected)   ✓ Camera B = Priya, C = Arjun (by microphones, margin 0.6)
         ✓ Energy, shots, beats   ◐ Moments ranked by Apple Intelligence to 42:10, heuristics after   ↻ Read 2 min ago · Re-read
                                                        [ Edit for me ]
──────────────────────────────────────────────────────────────
Versions   Original · V1 Premium Podcast ●  · V2 faster open
B-roll suggestions (4)   00:01:12 "the office in Bangalore" → office_wide.mp4 (0.67)  [Place] [Dismiss]
```

| Control | Values | Effect on the EDP (one table, `brain/planner/controls.py`, unit-tested) |
|---|---|---|
| Content type | Auto / Podcast / Interview / Talking-head reel | Auto = `classify`; picks the story template (§4.1), the default profile, and whether the camera pass runs |
| Editing energy | 1-10 | the §4.10 table |
| Captions | Off / Minimal / Dynamic / Viral | §4.7 modes |
| B-roll | Low / Medium / High | Low = suggestions only where a concrete noun phrase has a bin match ≥ 0.5; Medium = ≥ 0.35; High = ≥ 0.35 and up to 1 per 30 s; never inserts |
| SFX | Off | disabled with the reason; `BrainBrief.sfx` stable for Phase 2 |
| Music | None / Subtle / Standard / High energy | §4.8 levels relative to speech; on an episode Subtle = intro + outro, Standard/High (touched) = a bed under speech |
| Dialogue | Recorder / <angle> mic / Camera microphones as recorded | the `dialogue` decision's source (§4.6.1); the third value disables the lane |
| Duration | Auto / 15 / 30 / 45 / 60 / Custom | Auto keeps what the story keeps; a number drives `select` |
| Also make N shorts | 0-10 | `count` → `children[]` with the no-repeat rule |
| Style profile | shipped or a user copy | fills every knob the controls do not set; a touched control wins and is recorded in `touched[]` |

The panel composes the controls into `ui_state.brain_controls` and submits through the existing `POST …/prompt` (the run is an ordinary prompt run, so the K3 net, the run log, cancel and resume apply unchanged). Analysis is its own job (`POST …/brain/analyse`, `202 {job_id}`, `GET /api/jobs/{id}` as today) and never takes the session lock; a run takes it like every prompt run (`api/locks.py:54-68`).

### 8.3 Style Profiles as data (`brain/styles/*.json`; user copies in `<user data>/Video AI Editor/brains/`)

Four shipped profiles: `premium_podcast`, `viral_reel`, `luxury`, `clean_professional`. Shipped ones are read-only; "Copy & edit…" writes a user copy (`based_on`) and opens a form (every field a labelled control; a JSON text view with schema validation on save; 422 names the field).

```json
{"id": "premium_podcast", "name": "Premium Podcast", "version": 1, "based_on": null, "content_types": ["podcast", "interview"],
 "pacing": {"energy": 4, "target_shot_s": 6.0, "min_shot_s": 3.5, "silence_min_dur_s": 0.6, "silence_keep_pad_s": 0.12, "fillers": "strict", "false_starts": true, "repeats": true, "dead_air_max_s": 45},
 "story": {"template": "hook_setup_dev_payoff_close", "cold_open": "if_quotable", "cold_open_max_s": 10, "reorder": "hook_only"},
 "interview": {"questions": "keep"},
 "pauses": {"protect": "all", "turn_floor_s": 0.3},
 "camera": {"mode": "speaker_led", "lead_frames": 3, "hide_jump_cuts": true, "reset_wide_every_s": 90, "max_switches_per_min": 6, "min_shot_s": 2.5, "switch_cost": 0.35, "reactions": true},
 "punch_ins": {"per_min": 0.3, "scale": 1.12, "push_s": 0.4, "lead_s": 0.8, "release": "step_at_seam", "question_punch_out": true},
 "captions": {"mode": "podcast", "position": "bottom", "speaker_colours": true, "highlight": "none"},
 "music": {"level": "subtle", "mood": "cinematic", "episode_shape": "intro_outro", "rel_lu": -24, "duck_lu": -6, "beat_align": true},
 "dialogue": {"lane": true, "seam_fade_s": 0.005},
 "transitions": {"look": "none"}, "broll": {"level": "low"}, "sfx": {"level": "off"},
 "reframe": {"vertical": "keyframed"}, "brand": {"use_brand_kit": true, "lower_thirds": "speakers"},
 "deferred_ok": ["reaction_shots_from_memes", "karaoke"]}
```

Differences that make the four distinct: `viral_reel` — energy 8, `target_shot_s` 2.2, `min_shot_s` 0.8, `cold_open_max_s` 3, captions Viral centre with `highlight: current_word`, music `high_energy` upbeat at `rel_lu −14` with `beat_align` and `beat_pulse` on B-roll, punch-ins 2.0/min at 1.15 with `push_s 0.3`, pauses protect `laughter` only, reframe 9:16 keyframed; `luxury` — energy 2, shots 8 s, captions Minimal, music cinematic at `rel_lu −26` (episodes: intro + outro), no punch-ins, `hide_jump_cuts: false` (a luxury cut is a dissolve-free, sparse cut), head and tail `set_video_fade` dips instead of transitions, no cold open; `clean_professional` — energy 5, captions Minimal, music `none`, punch-ins 0.15/min at 1.08, lower thirds on, no beat alignment. `interview.questions` is `keep` in all four; a user copy may set `answers_only`.

`BrainBrief` (per session, `<session>/brain/brief.json`; defaults per user in `settings.json brain.defaults`): `{content_type, energy, captions, broll, sfx, music, duration, custom_duration_s, shorts, profile, text, names: {}, touched: []}`.

### 8.4 The honest UX (`brain/capabilities.py`)

"Not done this time" on the card and "What the brain cannot do yet" in Help are generated from ONE table with `available` per item from `check_features` (`ai/features.py:144`), consumed by the planner's `deferred[]`. It is never empty for a request that used words like "reactions", "memes", "SFX", "karaoke", "stock". Phase-1 truths it states: captions in four looks with word-by-word highlight (Viral) and keyword accent (Dynamic), no emojis; the words are whisper's and "words to check" lists the doubtful ones; speaker-led camera with anticipatory switches, hidden jump cuts, reset wides and listener reactions, no blink/expression awareness; angles may be several files per camera from the same take (drift corrected), never different takes; dialogue plays from one source on its own lane (recorder or a chosen microphone); 2-4 speakers by voice clustering, names asked not recognised, pyannote optional with a token; cuts = silences, fillers (heard and transcribed), false starts, verbatim repeats, dead air, weak questions, low-content topics, with the pauses that matter kept; the hook is the strongest source sentence, meaning never rewritten; episodes get an intro and outro, not a bed, unless asked; B-roll = suggestions from your bin, you place; four bundled procedural beds; SFX/graphics/memes/news/two-up split screens are Phase 2/3; in the packaged `.app`: no MLX, no pyannote, no CLIP.

### 8.5 A run as the person sees it

1. Press **Edit for me** (or Enter in the Prompt bar). If the graph is missing: the `gate_analysis` card ("Read the footage first (≈ 4 min) / Edit with what is known / Stop"). Reading footage shows in the chip with ETA from `costs.py`; Cancel keeps the layers that finished; the person keeps editing (no lock).
2. First-use honesty: a missing whisper ggml is the existing `downloads_needed` question once, with bytes.
3. Planning takes seconds; content tasks go to Apple Intelligence or MLX under the budgets of §9 and fall back to the heuristics, attributed on the card ("moments ranked by Apple Intelligence; cuts by rules").
4. Previewing: the dry run (and at most one revision round); the chip says which round.
5. The card: Plan tab first for brain runs (Changes first for ordinary prompts); `previewAnnouncement` reads the plan summary line first either way; Esc = Change; times are buttons ("Seek to 00:12:41", the timeline is untouched so the card says "in the current timeline"); **Try another** plans Version B with the next-ranked story and hook without applying either.
6. Apply re-runs the same plan on the live store (`service._resume_preview`, base-hash check, `apply_check`), one commit, recorded as **V1**; the instant-preview engine shows the new cut in one frame from the post-op EDL.
7. Verify runs the postconditions; the reply is the honest summary: "via recipes · moments by Apple Intelligence — V1 Premium Podcast: 14 camera switches, 38 cuts (4:12 removed), 2 speakers named, captions, subtle bed. Score 78 (was 41). 2 of 3 punch-ins verified; the loudness check did not run over 600 s. Undo with ⌘Z."

### 8.6 Accessibility and keyboard

The rail tab follows `ToolRail`'s APG tabs pattern (roving tabindex, ↑/↓/Home/End, `aria-keyshortcuts`, exact accessible name "Edit for me"); the panel is one form (every control has a `<label>`, the slider is a native range input with `aria-valuetext` "4 of 10 — calm cuts", disabled controls carry their reason in `aria-describedby`); the card's Plan/Changes is a `tablist` inside the existing card with the existing focus rules ("take focus a frame later, only if focus is on nothing or the Prompt bar"); the Versions strip is `role="radiogroup"`; progress announcements follow the chip's threshold rule; reduced motion via `lib/useReducedMotion`; disabled state through the design tokens (`--text-disabled`, never `opacity`).

### 8.7 Routes (`api/brain_routes.py`; loopback + same-origin for every write, `PairAuthMiddleware`; `sid` validated by `is_valid_session_id`)

`POST …/brain/analyse {layers?, force?} → 202 {job_id}` · `GET …/brain/graph → {id, layers, speakers, angles, topics, timings}` (summary; sentences paginated `…/graph/sentences?from=&to=`; leaf names only, never absolute paths) · `GET …/brain/decisions/{did}` · `GET …/brain/reviews/{did}` · `GET/PUT …/brain/brief` · `GET …/brain/versions` · `POST …/brain/versions {label}` · `POST …/brain/versions/{id}/restore` (dispatches `restore_version`) · `PUT …/brain/versions/{id} {pinned, label}` · `GET/PUT …/brain/suggestions/{id}` · `POST …/brain/speakers {names}` (→ `name_speakers`) · `POST …/brain/broll_bin {path}` (adds to `allowed_paths` by the upload rule) · `GET /api/brains/profiles`, `GET/PUT/DELETE /api/brains/profiles/{id}` · `GET /api/brains/capabilities`. The Edit action itself is `POST …/prompt` with `ui_state.brain_controls`. One new SSE event type `analysis {layer, pct, eta_s}` joins `EVENT_TYPES`; a brain run's first `text_delta` carries the EDP summary (brief §27) before the dry run.

---

## 9. Model Gateway and brain tiers

### 9.1 The Gateway (`brain/gateway.py`)

One registry of **capabilities**, each an ordered list of **providers** with `probe() → Availability` (cheap, never raises; the `ai/features.py` rule), `cost(media_s) → est_s`, `run(payload) → result`, a hard timeout, a `deterministic` flag and an `engine_version` string that becomes part of the layer's cache key.

| Capability | Providers, in order | Deterministic | Timeout | Fallback when all fail |
|---|---|---|---|---|
| `asr` | whisper.cpp (`ingest/transcribe.transcribe`, `backend=auto`, `ingest/transcribe.py:882`) with two variants: the ANALYSIS pass (`small`, `--prompt` disfluency-biased, token `p` read) and the CAPTION pass (the best ggml on disk, `large-v3-turbo` else `small`, same prompt) — both cached by their own params; faster-whisper (dev only) | per backend + model + prompt | `costs.estimate_seconds × 3` | the upload's unprompted `small` transcript when no prompted pass can run in budget (fillers then come from the acoustic detector only, said on the card); else `downloads_needed` for ggml `small`; no speech → §9.5 |
| `sync` | numpy FFT cross-correlation on FLAC (port of `ai/multicam._audio_offset`), per FILE, anchors every 5 min, drift table | yes | 30 s per file | offset 0, `sync: unverified`, card warning, a `sync_offset` question offering "enter the clap time per angle" |
| `vad_energy` | ffmpeg `silencedetect` + `astats`/`ebur128` over FLAC chunks | yes | `2 × media / 100` | required (ffmpeg is a hard dependency) |
| `speakers` | pyannote via `ai/diarize.diarize` when a token AND weights exist (`Path.exists`, `facts.first_use` pattern); numpy MFCC + k-means (seed 42) | yes / yes | `4 × media / 60` | single-speaker mode: `spk: null`; camera by own-mic energy + faces; no lower thirds or colours |
| `faces` | OpenCV Haar frontal on ffmpeg-decoded 480-px grey proxy frames (never `cv2.VideoCapture` on user paths); a `mouth_probe` variant decodes 20 s at ≥ 8 Hz around 3 long turns per speaker for angle assignment only | yes | per range | centre framing; scale-only punch-ins; angle assignment by own-mic and face size only |
| `shots` | ffmpeg `select=gt(scene,0.3)` on the proxy (`ingest/scenes.detect_shots`) | yes | `1.5 × media / 30` | one shot per source |
| `beats` | bed sidecar grid; librosa `beat_track` when importable; numpy onset-envelope autocorrelation | yes | 60 s | no beat alignment |
| `llm_json` | `apple_intelligence` (`fm-planner text`, new `@Generable` shapes), `local_model` (MLX, greedy), `claude` (key + `VAI_PROMPT_CLOUD ≠ 0`); `recipes` never | **no**; results cached and frozen with provenance | FM 8 s (`FM_ATTEMPT_CAP_S`), MLX 12 s, cloud 20 s (`router.py:60-68`); per layer `SEMANTIC_BUDGET_S = 180` | heuristic scores with `by: "recipes"` |

Rules the Gateway encodes:

- **Path-free payloads.** An `llm_json` payload is words, times, ids and counts; the `TextTask` builder asserts no `/`, `\\`, `~` or `.mp4`-like token reaches a model (a unit test greps every payload builder), mirroring `BrainRequest`'s rule (prompt-machinery map §4.1).
- **JSON schemas and repair.** Each task has `brain/tasks/<task>.schema.json`; MLX and cloud outputs go through `jsonfix.repair` (`jsonfix.py:203-241`) then `jsonschema`; one retry quoting the validation error; then the heuristic. The FM rung is constrained-decoded by a matching `@Generable` struct (valid by construction; a Swift rebuild is lane EB-9).
- **Token budgets.** A call carries ≤ 24 sentences (≈ 1,200 tokens in) and asks ≤ 400 tokens out (`TEXT_MAX_TOKENS` 200 today, `mlx_brain.py`; raised to 400 for `rank_moments` only, capped at 300 if a 7B call exceeds 6 s). A 60-minute podcast ≈ 600 sentences → ≈ 25 calls; FM at ~1-2 s per call fits the 180 s layer budget; past it the layer is `partial` and the card says so.
- **Language.** Devanagari never reaches Apple Intelligence (`fm.py` pre-check); Hindi/Hinglish tasks go to MLX when present, else the heuristic (`lexicon.py` per language: `en`, `hi-Latn` via `ai/romanize`).
- **Determinism policy.** A model's answer is written to `semantic.json` with provenance and never re-asked while the sentence text and prompt hash are unchanged; the planner reads scores from the graph only; "Re-rank with Claude" is an explicit action producing a new semantic layer → new `gid` → new EDP.
- **Content tasks are cached in the pending record** (the `BrainRequest.hook_text` precedent) so the second dry run and Apply never regenerate.

### 9.2 What each tier provides

| Decision | recipes (floor) | apple_intelligence | local_model (MLX) | claude |
|---|---|---|---|---|
| Content type | rules (§4.0) | `classify_content` over the digest; accepted if it agrees or rules' confidence < 0.6 | same | same |
| Moment scores | heuristic formulas (§3.4) | `rank_moments` over the top quartile, blended 0.6/0.4 | same | same, over more sentences |
| Story beats | rules | `story_draft` over 24 scenes: may pick the hook and drop ≤ 3 scenes | may reorder within a topic | may author the full beats incl. reorder |
| Hook ranking / overlay text | axes; heuristic line | `rank_hooks` re-rank top 8; `hook_candidates` text (existing) | same | same |
| Topic labels | top-3 nouns | `title_topics` (≤ 20) | same | same |
| Q/A classification | regex | `classify_qa` on adjacent host/guest sentences | same | same |
| Camera, punch-ins, reframe, captions, music | rules only | rules only | rules only | rules only (a model may set energy, never a switch time) |
| NL revision reading | grammar + `edit_revise` slots | `read_revision` → the same slots, grounded like `ground_to_prompt` (`content.py:178`) | same | may emit `recipe:edit_revise` with slots |
| Shorts picks | scores + dedupe | `rank_windows` (existing task, finally wired; tie-breaks only) | same | same |

Digest budgets (`brain/digest.py`, path-free, id-anchored): Apple Intelligence ≤ 2,400 tokens (header + top 24 scenes by max(hook, importance, virality) with `id, t0, speaker, 14-word gist, h/i/v tenths` + controls; stdin ≤ 64 KB, `fm.py`); MLX 7B ≤ 6,000 tokens (+ every topic's top-3 scenes + pause/reaction scenes); Claude ≤ 60,000 tokens (full scene list with text, quality and shot blocks; never words arrays). New `TextTaskKind`s (`brains/base.py:137`): `rank_moments`, `title_topics`, `classify_qa`, `classify_content`, `story_draft`, `rank_hooks`, `read_revision` (+ the existing `hook_candidates`, `rank_windows`). The FM helper's `IntentItem` gains `content_type, energy, captions, broll, music` (5 optional slot fields; `_FM_SLOT_FIELDS` mirrored, `prompt_text.py:144`) and four `@Generable` task structs. Honesty: the reply names the rung per contribution; rejected drafts are in the run log; `brains_report()` gains a per-task `enabled` flag (§13.5).

### 9.3 Recipe surface

One new card the on-device brains can name (`recipes.py`):

```
edit — "Edit the whole video like a professional editor (podcast, interview or talking-head reel): cuts, cameras, captions, music, hook; explains every decision."
       slots: content_type ∈ auto|reel|podcast|interview · style ∈ premium_podcast|viral_reel|luxury|clean_professional · energy number
              captions ∈ off|minimal|dynamic|viral · broll ∈ off|low|medium|high · music ∈ none|subtle|standard|high_energy
              duration_s number · platform ∈ _PLATFORMS · ratio ∈ _RATIOS · count number
edit_revise — "Change the AI edit: remove a moment, change pace, captions, music, camera or length."
       slots: what · change · amount · speaker · range · duration_s
```

`_x_edit` reads `facts.brain_graph_id` and `facts.brain_layers` (two new frozen facts; the graph itself is never in facts and `facts_to_prompt_block` gains one line "analysed: 312 scenes, 2 speakers"), calls `plan()` and `compile()`, writes the EDP, returns steps, postconditions and questions. `expand_auto_edit` (`expanders.py:2290`) delegates to `edit` when a graph exists and keeps its checklist otherwise (the fallback the brain degrades to). The grammar gains `edit` family rows ("edit this podcast", "premium podcast", "make N viral clips", "cut this like an interview", "talking head reel", Hinglish equivalents) in the `composite` family so the contract's licence rule is unchanged for people who never open the panel; the EDP licence (§7.3) then narrows it.

### 9.4 Fallback ladder for a brain run

recipes answers `edit` outright at ≥ 0.75 as today (`RECIPES_CONFIDENT`); an on-device rung's `IntentDraft{recipe: "edit", slots}` is grounded by `ground_to_prompt` and expanded through the same `_x_edit`; `claude` may emit `recipe:edit` or raw steps that `validate_plan` judges like any other. The brain itself never depends on which rung named the recipe: the EDP records `brain` (who read the intent) and `content_brain` (who annotated) separately.

### 9.5 Failure handling (every degradation is a graph fact or a run notice)

| Situation | Behaviour |
|---|---|
| No speech (VAD < 3 % or ASR < 20 words) | `project_type: no_speech`; `tighten` on silence only; `select` by shots and energy (`_shot_shorts`' rule, `ai/shorts.py:283`); no captions, no hook text; card: "no speech was found; edited by picture and sound" |
| Transcript pending | cheap layers run; the plan waits ≤ `TRANSCRIPT_WAIT_S` 30 s (`service.py:68`) as today, else the analysis gate |
| whisper ggml missing | `downloads_needed` through the existing gate |
| Diarization unavailable or single cluster | single-speaker mode with a note |
| Sync failed | offsets 0, `unverified`, warning, the clap-time question |
| No faces on an angle | `angle_guess: unknown`; scale-only punch-ins; centred reframe |
| Angle assignment margin < 0.3 | one question with thumbnails ("Which camera shows Priya?"); unanswered → that speaker on the wide, said on the card |
| A camera's second file missing (split recording, one part not uploaded) | an `angle_gap` for its span; the Camera Director never switches into it; the card names the missing minutes |
| Dialogue lane stale (hand edits to v1 after the run) | `facts.dialogue_lane.in_sync = false` → reply notice + panel "Re-sync" button; every later brain run re-syncs last |
| a1 hand-edited by the person | its clips join the protected set; a brain run that would rebuild a1 clarifies first (§7.2) |
| Whisper prompted pass over budget | the upload's unprompted transcript + the acoustic filler detector; the card says "fillers by ear" |
| Semantic budget exhausted / brain unavailable | heuristic scores; `by: recipes`; the "text by" line names the writer |
| A decision's moment was cut away earlier in the batch | resolver drops it with a notice, counted in `StepOutcome.notices` |
| Fan-out cap reached | the step stops at the cap and says so; still one op |
| A kept range straddles a shorter angle file | compiler clamps to the angle's extent; reference angle for the remainder |
| Analysis cancelled / app quit mid-layer | layer files written atomically (`ingest/proxy._write_atomic` pattern, `proxy.py:207`); the next run resumes per layer |
| Disk full during analysis | `507 disk_full`; the layer held 10 s (`DISK_FULL_HOLD_S`, `proxy_queue.py:73`) |
| Stale EDP (graph changed since planning) | validator refuses the sentinel with a `replan` question |
| Verify render skipped (> 600 s) | render-based checks unmeasured, said; EDL-based brain checks still run |

---
## 10. Pipeline staging, caching, performance budgets, storage

### 10.1 Stages (brief §42: proxy → analyse proxy → edit → proxy preview → final render against originals)

| Stage | What | Where | Cached by | Budget (60-min 3-cam + recorder podcast, M1 Pro 16 GB; M4 Max figures ÷ 2) |
|---|---|---|---|---|
| 0 proxy | 720p all-intra spans + FLAC chunks per source | `ingest/proxy_queue.py`, eager after normalize | file identity (`proxy.py:190`) | background; ≥ 12× realtime per source (INSTANT_PREVIEW_SPEC §15.1); the brain waits only for FLAC (≥ 100× realtime) |
| 1 analyse | the layers of §3, per source, from proxies only | `brain/jobs.py` on the `ANALYSIS` cancel scope (new in `render/cancel.py`), niced, ≤ 2 concurrent, suspended during export, wall-clock deadline `3 × media + 300 s` | `(src_key, layer, params_hash, engine_version)` | ≤ 10 min cold (≤ 8 when the upload's transcript is reusable), ≤ 20 s warm |
| 2 graph | assemble + scenes + digest | `brain/graph.py` | `gid` | ≤ 1 s |
| 3 plan | `plan(graph, controls, style, seed) → EDP` | `brain/planner/*` | deterministic, not cached | ≤ 3 s (camera DP is O(turns × angles²) over ≈ 1,100 turns) |
| 4 compile | EDP → Plan | `brain/compile.py` | — | ≤ 1 s |
| 5 dry run | `executor.run_plan(dry_run=True)` on `preview.scratch_store` | existing | artefacts (transcript); analysis read-only | ≤ 60 s (≈ 450 cuts through `_cut_source_ranges`' O(n²) re-mapping ≈ 5-10 s; ≈ 120 splits + swaps; ≈ 360 keyframe dispatches; captions from transcript ≈ 25 s cue build for 45 min) |
| 5b review (+ one revision) | §6.2 | new | verify render keyed by EDL hash | ≤ 60 s more, only when a fix was found |
| 6 preview | change card with reasons | existing + Plan tab | — | instant |
| 7 apply | same plan, same EDP, live store | existing | — | ≈ dry run |
| 8 render | export against originals | `render/compositor.py` | `render_hash` | unchanged |

Per-layer budgets (cold, 60 min): ASR (reference audio only, `small` prompted, whisper.cpp Metal) ≤ 3.0 min, plus ≤ 2.0 min when the upload's unprompted pass is still running and cannot be reused (the two passes never run concurrently; the brain's pass waits for the background transcriber's slot); the caption pass on `large-v3-turbo`, when present, runs LAZILY at the captions step of the first plan (≤ 4 min, under the `go` gate's estimate) and is cached (≈ 25× RT on M1 Pro; 13× RT measured for faster-whisper CPU on the 85 s fixture and whisper.cpp measured 4-5× faster, ai-tools map §2.1); VAD/energy/silence/loudness for 4 sources ≤ 40 s; sync ≤ 10 s; speakers (numpy MFCC + k-means over ≈ 1,100 utterances) ≤ 60 s; semantic ≤ 180 s budgeted, typically ≤ 60 s; faces (lazy) ≤ 5 s for a reel, ≤ 90 s for a full-episode keep-set at 1 Hz × 3 angles (≈ 8,100 frames at 5-10 ms each); shots only for B-roll files, ≤ 30 s per 10 min. Memory: peak RSS ≤ 2.5 GB (whisper `small` ≈ 1 GB; PCM windows of 10 min at 16 kHz f32 ≈ 40 MB; Haar frames streamed); graph on disk ≤ 5 MB; plan-time RAM ≤ 60 MB.

Instrumentation: every layer run appends `{layer, engine, media_s, wall_s, rss_peak}` to `<session>/brain/timings.jsonl`; `tests/test_brain_budgets.py` enforces the table on the synthetic 60-minute fixture (`slow`; informational in CI, blocking in the release gate on the Mac runner); the benchmark report gains a "brain" table (analysis wall, plan wall, decisions by kind, notices, score per rung).

### 10.2 Cache keys and eviction, in one place

| Store | Key | Eviction |
|---|---|---|
| `WORKDIR/analysis/<src_key>/<layer>/<params>.json` | identity `sha256(realpath,size,mtime_ns)[:24]` OR content `sha256(size+head+tail)`; params = engine + model + language + `ANALYSIS_VERSION` | LRU class `VAI_ANALYSIS_CACHE_MB` (1024) in `render/cache_budget.py`; `refs.json` access rule as proxies |
| `<session>/brain/graph/<gid>.json` | layer digests + offsets + roles | never (project state) |
| `<session>/brain/decisions/<did>.json` | immutable | never; a `.vae` bundles them |
| `.prompt_preview/artefacts/` | unchanged (whisper + six derived tools, `artefacts.py:66-68`) | `VAI_PROMPT_ARTEFACT_CACHE_MB` (2048) |
| `<session>/cache/verify/` | EDL hash | existing |
| model annotations | `(sentence text hash, prompt_hash, model)` inside `semantic/<params>.json` | with the layer |

### 10.3 Storage layout inside the session (recap) and `.vae`

`<session>/brain/{graph/, decisions/, reviews/, versions.json, suggestions.json, feedback.jsonl, angles.json, brief.json, timings.jsonl}`. `.vae` bundles the whole `brain/` directory plus the small layer files for each referenced source; the lazy `visual` layer is rebuilt on demand. Nothing the renderer reads lives here; `render_hash` is unaffected.

---

## 11. Security and safety

1. **The command API stays `validate.py`-gated.** `validate_plan` (`validate.py:791-837`) remains THE boundary: the brain adds three plan-able tools narrower than the denied ones (`cut_source_ranges`, `apply_camera_plan`, `sync_dialogue_lane`) and one UI-only tool (`restore_version`); no path a plan may name is outside `facts.allowed_paths`; `guard_step` (`executor.py:253-283`) re-checks the deny list, unresolved placeholders, unknown tools/args, the path rule, the no-download rule and the sentinel rule at the last line. `PLAN_DENY` is unchanged.
2. **No raw-file access for models.** Gateway payloads are words, times, ids and counts (asserted path-free by test); the FM helper's env is scrubbed and its cwd empty (`fm.py:129-141`, `tests/test_fm_helper_source_guard.py`); model outputs enter only `semantic.json` and typed drafts, which the planner reads as numbers, ids and short strings; no model output ever becomes a path, a tool name or an arg value except `hook_card.text` (`sanitize_hook_items`, ≤ 60 chars) and topic titles (≤ 48 chars, same sanitiser).
3. **Sentinels are declared.** `$brain:<kind>` is accepted only for the declared (tool, arg) pair; `plan_ref` is regex-checked and must exist under the session; the EDP's graph id must be current; the EDP file is validated with `extra="forbid"` on load; `reason.facts` must resolve.
4. **Analysis reads proxies only** and writes only under `WORKDIR/analysis/` and `<session>/brain/`; subprocesses are ffmpeg with `_pu.ffmpeg_filter_path`-escaped filter paths and `encoding="utf-8", errors="replace"`; Haar runs in-process on frames ffmpeg decoded (no `cv2.VideoCapture` on user paths); `.txt` sidecars are read with a 4 KB cap; layer files have size caps.
5. **Routes**: loopback + same-origin for every write (`PairAuthMiddleware`, covered automatically by `tests/test_same_origin_writes.py`); `sid` validated; graph reads paginated and path-free (leaf names only); decision/graph/version ids are regex-checked path components; profile ids are `^[a-z0-9_]{2,40}$`.
6. **B-roll bin**: `find_broll`'s `VAI_BROLL_BIN` default is not used; suggestions index only `<session>/uploads/broll/` and folders added through the panel (each added to `allowed_paths` by the upload rule).
7. **No network**: the only fetch a brain run can trigger is the existing consented `downloads_needed` path (whisper ggml); the eval's `EgressGuard` (`tests/benchmark/harness.py`) proves it; `HF_HUB_OFFLINE=1` in every eval.
8. **Resource bounds**: fan-out cap 4,096; ranges ≤ 2,000; switches ≤ 600; turns in caption args ≤ 4,000; layer ≤ 32 MB, graph ≤ 64 MB; analysis deadline `3 × media + 300 s`; niced subprocesses; ≤ 2 concurrent analyses; suspended during export.
9. **One writer**: the brain never mutates the EDL outside `dispatch()`; the resolver is a pure read; `restore_version` is a commit like any other.
10. **News-mode safeguards, designed and deferred (brief §11, §28 "unsupported claims / visual mismatch").** The graph already gives each visual decision `reason.facts` naming the source sentence it illustrates. Phase 3 adds: (a) a `claim` layer (sentences with `claim = true` carrying the entities they name); (b) a rule that any inserted visual (B-roll, image, card) must cite a `claim` or `topic` fact whose tokens overlap the asset's tags ≥ 0.5, else `unsupported_visual` blocks; (c) `source_attribution` decisions for any asset not from the person's uploads; (d) the Reviewer's `claim_visual_mismatch` check. None of this changes Phase-1 shapes: `Decision.reason.facts` and `suggestions.json.candidates[].matched` are the hooks.

---

## 12. Preference-signal capture (brief §44; Phase-2 "learn my style", live from day one)

After an EDP `d` is applied at op `n`, `brain/feedback.py` observes the session's later ops (a poll on `ops.json` growth from the brain routes plus a post-commit hook in `service.py`; no hook into `EDLStore`). For each later op it diffs the committed EDL against the previous snapshot with `contract_diff.diff` and matches the changed entities against `d`'s resolved footprint (`Decision.produced`).

```json
{"ts": 1790000700.2, "version": "v_1", "profile": "premium_podcast", "controls": {"energy": 4, "music": "subtle"},
 "decision": "k_0061", "kind": "punch_in", "reason_code": "emphasis_peak", "signal": "punch_in_removed", "detail": {"clip_id": "c_7f1a…", "scale": 1.12}, "by": "user", "op_seq": 44, "edl_hash": "0c77…"}
```

| Human change | Signal |
|---|---|
| undo of the whole run | `rejected_run` (decisions: all) |
| a removed source range back on v1 (via a widened piece) | `restore_cut {decision, reason_code}` |
| a cut edge moved > 0.1 s | `move_cut_edge {decision, delta_s}` |
| a punch-in's keys deleted / scaled | `punch_in_removed` / `punch_in_scaled {scale}` |
| an angle piece swapped (`src` change on a camera-plan piece) | `angle_override {decision, from, to, speaker}` |
| caption style / look / position changed | `caption_style {field, from, to}` |
| music removed / replaced, duck depth changed | `music_removed` / `music_replaced` / `duck_changed` |
| a suggestion placed / dismissed | `broll_placed` / `broll_dismissed {suggestion, query, candidate}` |
| a lower third removed / renamed | `lower_third_removed` / `lower_third_renamed` |
| a hook card edited / removed | `hook_text_edited` / `hook_removed` |
| a control set differently from the profile | `control_override {control, profile_value, chosen}` (written at run time) |
| a version restored | `version_restored {from, to}` |

Signals are appended to `<session>/brain/feedback.jsonl`; an account-level copy per profile lives under `platformutil.user_data_dir()/brain/signals/<profile>.jsonl`; nothing is sent anywhere. Phase 1 only WRITES them and shows a count in the profile editor ("31 signals since V1"). Phase 2's learner aggregates them per profile into knob adjustments periodically, never per action (the brief's rule), and the raw+final-pairs learner (brief §24) reads the same schema; locked brand rules are never changed by signals (brief §36). Test: `tests/test_brain_feedback.py` replays a scripted human session after E3 and asserts the exact rows.

---

## 13. Test and eval plan

Principles inherited from the benchmark and the frame-map goldens: **truth by construction**, measured back from the persisted EDL through `timemap` and from decoded renders (bar codes, PSNR, loudness), never from the app's own verdict (which is asserted to agree, `verifier_drift`); zero network egress; never two pytest processes; TDD per module; coverage of `brain/` ≥ 80 %; WK only for what a person sees.

### 13.1 Fixtures with known ground truth (extend `tests/benchmark/{narration,media}.py`; lavfi + Piper/`say`, egress-guarded, ≤ 60 s to build all)

1. **Two-speaker interview** (podcast core): Piper `amy` as guest, macOS `say` (or a pitch-shifted Piper render — open decision §15.1) as host. Script by construction: 12 host questions (3 planted weak with standalone answers, 1 weak with a non-standalone answer that must be KEPT, 2 well-asked questions with `hook ≥ 0.5` whose answers are standalone and that must ALSO be kept), guest answers with one quotable statement (the only sentence with a strong number + a superlative), one throwaway with a year and a small count ("we started in 2019 with three people") that must rank below it, one fact delivered flat (−3 dB, no contrast word) and the same words delivered with emphasis, one 4-sentence story with a topic shift, one repeated answer (Jaccard 0.9), one false start, per-speaker `um`s (6 host, 5 guest) plus 3 `uh` islands the ASR is known to drop (acoustic truth by time), one mis-heard-by-construction name (a rare surname), one 0.8 s overlap, one 2.4 s mid-turn pause, one 1.0 s pause after an emotional line (must be protected), one planted laugh on the host mic. Sound: a **recorder WAV** (clean, the reference) AND per-camera mic tracks (the same speech 12 dB down with room noise and 40 ms of acoustic delay) so the dialogue lane and own-mic assignment are exercised on the core fixture. Picture: one wide frame with two coloured blocks 60 % of the width apart that pulse when their speaker talks (the two-shot reframe truth). Truth JSON: utterances with speaker and kind, Q/A pairs, quotable id, repeat pair, weak-question ids and removability, kept-question ids, filler times (lexical and acoustic), the pauses with their protection class, the laugh, the name.
2. **Multicam podcast** (brief §38): three renders of the same audio — A wide, B host close, C guest close — bar-code source ids 1/2/3 burned in and a **click track** on every camera mic (`tests/timing_fixtures.make_clap`'s pattern) so `av_offsets_ms` can measure the dialogue lane against the picture at any seam; B +0.35 s and C −0.20 s at file level; **B is TWO files** split at 30:00 with a 0.5 s gap between them (the split-recording case); C carries a planted 60 ppm clock drift; a fourth **dead** angle (black + room tone); a separate recorder WAV as the reference (12 dB cleaner than camera mics). Truth: the turn table ⇒ expected angle per span with the min-shot floor; per-file offsets and the drift table; the seams where a jump cut must coincide with an angle change.
3. **Talking-head reel with emphasis**: the bench narration (`narration_en.json`: 9 fillers, 7 × 2.0 s pauses, 5 scene cuts, the only question #36, the only superlative #10; edl map §8.3) with one sentence at +6 dB and 15 % slower and one retake (0.8 s gap), plus 3 `uh` islands (0.2-0.4 s, flat pitch, synthesised by Piper and known to be dropped by whisper-small — the module docstring's table) with truth by concat offset; the known 30 s best window `31.05-61.43`; 16:9 and 9:16 variants; a moving high-contrast subject on a known piecewise-linear x(t). Its truth also carries the clause starts (script punctuation) so the punch-in in-point can be asserted.
4. **Structured bed**: `synth_bed_pcm` extended with intro (8 quiet bars) → build → loud → break; truth = section boundaries and the grid `0.05 + 0.6n`.
5. **B-roll bin**: 6 lavfi clips with `.txt` sidecars ("laptop on a desk", "city street", …) and bar-code ids; truth = sentence → clip by the planted noun, and the sentences that must get NO suggestion.
6. **60-minute synthetic podcast** (fixture 1 looped with varied scripts, 3 angles at 1080p) for budgets only (`slow`).
7. **Hinglish twin** of fixture 3 (`say -v Lekha`, as the bench's `scene_hi_16x9`).
8. Reuse as-is: `scene_16x9`/`scene_9x16`, `bench_bed_100bpm.wav`, `tests/test_b8_shorts.py:_SCRIPT`, `WEAK_THEN_STRONG`, bar-coded sources and clap/click tracks (`tests/frame_map_golden_lib.py`, `tests/timing_fixtures.py`).
9. **Real-footage manifest** (`tests/brain_real/manifest.json`, the ASSETS outside the repo under `VAI_REAL_FOOTAGE_DIR`; every test skips cleanly when the directory is absent): the §13.6 tier — ≥ 3 real episodes (one 2-camera podcast with a recorder, one single-wide interview, one remote/Zoom-style recording) and ≥ 5 real talking-head reels, owner-supplied or CC-BY with the licence recorded per item, each with an editor's marked cut list (removed source spans, ±80 ms), top-3 hooks (sentence ids), the angle per turn for the multicam episode, the "must keep" pauses, and a caption word-check list. The manifest schema is Pydantic (`tests/brain_real/schema.py`) so a mislabelled truth file fails loudly, not silently.

### 13.2 Unit goldens per scorer and per pass

| Test | Pins |
|---|---|
| `tests/test_brain_speech_layer.py` | sentences, fillers (incl. verbatim repeats, `like` kept), false starts, repeats, weak questions, dead air, words to check on scripted transcripts — exact id lists; `prob` read from a recorded `-ojf` JSON; the prompted pass is a distinct params key |
| `tests/test_brain_fillers_acoustic.py` | fixture 3's three `uh` islands found by time within 60 ms with 0 false positives; confidence monotone in flatness; a lexical filler that passes acoustically → 1.0 |
| `tests/test_brain_speakers.py` | numpy MFCC = librosa within 1e-3 (dev only); two-voice fixture → k = 2, turns within 0.3 s, seed stability; angle hints by own-mic first (fixture 1's camera mics), face size second, mouth probe third; margin < 0.3 → the question |
| `tests/test_brain_sync.py` | +0.35 / −0.20 s within 10 ms per FILE; the two-file angle → two aligned files + one `angle_gap`; the 60 ppm drift recovered within 10 ms at every anchor; dead angle `unverified` |
| `tests/test_brain_seams.py` | §4.6.2 trough snap, tie-breaks, 40 ms air floor, level match; §4.6.3 protected pauses per class; the E1 render has 0 clicks |
| `tests/test_brain_semantic.py` | §3.4 goldens; provenance recorded; all-brains-unavailable fallback |
| `tests/test_brain_gateway.py` | path-free payloads (grep); schema + retry + fallback; budgets with a fake slow brain; Devanagari never reaches the FM stub; a cached annotation never re-asked |
| `tests/test_brain_camera.py` | rule table 1-8 on a scripted turn table; single angle → no decisions; determinism |
| `tests/test_brain_emphasis.py` | §4.4 rules; question punch-out; face anchor vs centre |
| `tests/test_brain_select_story.py` | reel window; `open_on` only when the hook is not already early; continuity guard; N reels no-repeat |
| `tests/test_brain_planner_goldens.py` | `plan()` over `tests/goldens/brain/graphs/*.json` = `tests/goldens/brain/edp/*.json` byte for byte; `PLANNER_VERSION` bump required (`tests/gen_brain_goldens.py`) |
| `tests/test_brain_compile.py` | every decision kind → the mapped tool(s); ≤ 24 steps for a 60-minute graph with shorts; stage order incl. the stage-2 sentinel order; `plan_ref` present; `validate_plan` accepts the compiled plan on `TimelineFacts.minimal()` + the fixture store |
| `tests/test_brain_resolve.py` | `$brain:*` through `timemap` after prior cuts; `story_order` after splits; `story_move` finds the duplicate; dropped decisions → notices; fan-out cap said; stale EDP refused; footprint written |
| `tests/test_cut_source_ranges_tool.py` | equals `remove_silences` + `remove_fillers` on the bench fixture; multi-src ranges; 200 random cut sets over 1×, 2× and reversed clips equal `_cut_source_ranges` (the `tests/test_transcript_timemap.py` style); path guard; 2,000-range cap; one commit |
| `tests/test_apply_camera_plan_tool.py` | never clears v1; keeps transform/effects/audio/keyframes on the swapped piece; per-file offsets and the drift table applied; a span crossing a file boundary is split there; bar-codes prove the source frame per span |
| `tests/test_sync_dialogue_lane_tool.py` | §4.6.1: rebuild after every structural op on click-tracked sources, `av_offsets_ms` ≤ half a frame at every seam; idempotent; music-lane copy removed once; v1 angle pieces muted, gain kept; seam fades set, outer fades 0; B-roll on v1 leaves an a1 gap; hand-edited a1 → refusal with the clarify shape |
| `tests/test_make_shorts_from_timeline.py` | a window spanning a camera switch and a cut yields a child with ≥ 2 v1 sources and matching a1 pieces, re-based to 0, `dialogue_in_sync` true in the child |
| `tests/test_caption_emphasis_render.py`, `tests/test_caption_highlight_cues.py` | §4.7.3: PIL and ASS renders put the accent under the highlighted token only; RBV bump; byte-identical without the fields; one cue per word; the 4,000 refusal |
| `tests/test_brain_checks.py` | every new `CHECK_SPECS` name has a verifier with declared args; `BLOCKING_CHECKS` gains exactly `no_cut_mid_word`, `dialogue_in_sync`, `protected_untouched` and `single_interp_per_key` |
| `tests/test_brain_protect.py` | a title placed after V1 survives "make the first 10 s faster"; the clarify offers the three options; "let the brain move it" re-plans |
| `tests/test_brain_versions.py` | record / restore / pin; `.vae` round-trip; pruning exemption; greyed state |
| `tests/test_brain_review_loop.py` | a plan seeded with a mid-word cut and a punch on a filler is repaired in one round; a revision never lowers the score; at most two dry runs; no fix widens the change set |
| `tests/test_brain_apply_determinism.py` | preview fingerprint == Apply fingerprint with every task stubbed and with tasks answering from the pending cache |
| `tests/test_brain_one_op.py` | `undo_depth` +1, `ops[-1].tool == "prompt"`, `decisions` and `score` in args, undo restores `hash_before` |
| `tests/test_brain_never_flattens.py` | no new `cache/` `src` after a run; `auto_reframe` emitted with `subject_track=false` only |
| `tests/test_brain_schema.py`, `tests/test_brain_storage.py` | `extra=forbid`, id references, size caps, canonical digest; identity + content keys; LRU class; `.vae` bundle; `refs.json` |
| `tests/test_brain_packaged_imports.py` | analysers, diarize and beats produce a graph with librosa/torch/mlx blocked in `sys.modules` |
| `tests/test_brain_energy_table.py`, `tests/test_brain_controls.py`, `tests/test_brain_profiles.py` | monotone rows; control → knob mapping; profiles load/validate/copy round-trip |

Existing guards that cover the new tools automatically: `tests/test_all_tools_smoke.py`, `tests/test_tool_schema_completeness.py`, `tests/test_wave3_correctness.py`, `tests/test_prompt_preview_changes.py` (new tools op by op + 400 random mutations still `covered ⊇ diff_keys`), `tests/test_same_origin_writes.py`, `tests/test_fm_helper_source_guard.py`, `tests/test_prompt_contracts.py` (the plan schema is unchanged and still pinned), `tests/test_k3_prompt_corpus.py` (the `edit` and `edit_revise` phrasings join; wrong-commit rate stays 0), `tests/test_prompt_preview_corpus.py`.

### 13.3 Benchmark: editorial cases (`tests/benchmark/test_brain_editorial.py`, `-m benchmark`, per rung `VAI_BRAIN=recipes|fm|mlx|cloud`)

Each case runs the real `/prompt` route with the controls in `ui_state`, previews, applies, and measures from the persisted EDL / transcript / ops / a 360p render.

| Case | Prompt (fixture) | Assertions (all measured) | Pass bar |
|---|---|---|---|
| E1 fillers | "cut the ums" (1, and 3 for the acoustic set) | every planted filler gone by TIME — lexical 11/11 on fixture 1 and the 3 acoustic `uh`s on fixture 3 — content `like` survives, `split_inside_word = 0`, every cut edge on an envelope trough with ≥ 40 ms of air before the next onset, 0 seam clicks in the decoded render, one op, `undo_depth` +1 | all |
| E2 interview tighten | "tighten this interview" (1) | 3 removable weak questions gone, the 4th kept, the 2 well-asked questions kept, `dup` gone and `of` kept, false start gone, the emotional pause kept ≥ 45 %, sentence survival ≥ 0.98, `no_cut_mid_word`, `dialogue_in_sync` (recorder on a1, camera mics muted) | all |
| E3 reel | "make a 45-second reel" (3) | first kept sentence is #10 within 0.2 s; duration within ±1.0 s; the punch-in starts at a clause start ≥ 0.8 s before the +6 dB word and releases as a step at the next seam; every tighten seam ≥ 0.4 s hidden by a scale step; Viral captions: one cue per word with `emphasis`, the accent under the current word in 3 decoded frames per cue (sampled); captions cover ≥ 0.9; music present at `rel_lu` and ducked | all |
| E4 multicam | "edit this podcast like a premium podcast" (2) | ≥ 90 % of talking time on the speaker's close or the wide; dead angle never; every speaker switch lands 0-0.15 s BEFORE the truth onset; the "mm-hm" turns never switch; every tighten seam ≥ 0.4 s coincides with an angle change; min shot ≥ 2.5 s (1.2 for `at_cut`); switches/min ≤ 8; per-file offsets within 10 ms and the drift corrected; bar-codes at 3 sample frames per span match; the dialogue lane's clicks line up with the picture's flashes within half a frame at every seam (`av_offsets_ms`); music = intro ≤ 12 s + outro 8 s, nothing between | all |
| E5 speakers + captions | same run | ≥ 95 % of cues carry the right speaker, two colours, lower thirds at each speaker's first kept turn ≥ 3 s after the cold open, the planted name in "words to check" | all |
| E6 shorts | "make 3 viral clips" (2) | 3 child sessions from the parent's timeline: a child spanning a turn holds ≥ 2 v1 sources (both closes) and its a1 pieces, `dialogue_in_sync` true, ≤ 20 % sentence overlap pairwise, each child's first sentence `hook ≥ 0.7`, each child one commit with its EDP referenced and `v_1` recorded | all |
| E7 reframe | "make it vertical" (3) | canvas 1080×1920, `fit = cover`, x keys track x(t) within 6 % of width, pan ≤ 0.15 widths/s, no `src` change, subject inside the frame ≥ 95 % of decoded frames | all |
| E8 reasons | every case | every `reason.facts` resolves in the named graph; every applied change on the card has a `why`; `reason.text` timecodes match `live.smpte`; decisions never claim more than the diff shows | all |
| E9 revisions | after E4: "keep guest B on screen longer", "remove the story about X", "make the first 10 seconds faster"; after a manual title: "make the first 10 seconds faster" | guest close share +15 points and nothing else in the diff; the story's sentences gone, nothing else; cuts only inside the first 10 s of reference time; the title untouched (blocking check) | all |
| E10 no speech | a music-only clip | plan completes, no captions, no hook text, honest note | pass |
| E11 INBUILT | E1-E7 with `VAI_BRAIN=recipes`, `ANTHROPIC_API_KEY=""`, `HF_HUB_OFFLINE=1`, librosa/torch/mlx import-blocked | same assertions; `content_brain ∈ {recipes, apple_intelligence}` | all |
| E12 determinism | E3 three times | identical EDP digest, identical `edl.hash()` after Apply | byte-equal |
| E13 preview parity | E3 with `VAI_PROMPT_CONFIRM=1` | preview + Apply commits exactly the auto-apply hash | equal |
| E14 B-roll | "suggest B-roll" (3 + 5) | markers only at planted references, none elsewhere; nothing on v2 | all |
| E15 score | E3, E4 | Editing Score after ≥ 70 and > before; the per-rung table written | pass |
| E16 budgets | fixture 6 | analysis ≤ 10 min cold (M1 Pro scale), plan ≤ 3 s, compile ≤ 1 s, dry run ≤ 60 s, RSS ≤ 2.5 GB; at energy 9 the cuts split into ≤ 2,000-range steps and `tighten_uniform` ≤ 1.3 | report; gate on the Mac runner |
| E17 two-shot reframe | "make it vertical" (1's wide) | the active block's centroid inside the frame ≥ 95 % of decoded frames; no frame with both blocks cut at the edges; ≤ 1 crop move per min-shot | all |
| E18 keep my edits | a manual title + music + a trim BEFORE the first run, then "edit this like a premium podcast" | the gate offers "Keep my edits / Start from the raw footage"; with Keep, the title, the music clip and the trim survive untouched (blocking check); with Start, the pre-run state is version "Original + edits" and ⌘Z restores it | all |
| E19 real footage (§13.6) | the manifest's items, each with the profile the editor used | the §13.6 bars; skipped when `VAI_REAL_FOOTAGE_DIR` is unset; REQUIRED for the Phase-1 exit | bars |

### 13.4 Rung evals

- Draft acceptance rate and rejection reasons per rung on the fixture prompts (logged by `drafts.ground`).
- **Model-off equivalence**: with every task stubbed to time out, EDPs equal the recipes EDPs byte for byte.
- Hook top-1 agreement between rules and each rung against truth; a rung may not lower the hit rate below the rules' on the fixtures, else its re-rank is disabled by default for that task (a per-task `enabled` in `brains_report`).
- Apple Intelligence latency per task measured and reported (unmeasured today, ai-tools map §4).
- FM stdin ≤ 64 KB and digest ≤ 2,400 tokens measured on the 45-minute synthetic.

### 13.5 UI tests

Vitest for `brainDecisions`, `editingScore`, `brainControls`, `brainStore`, `brainCopy`; Playwright Chromium + WebKit (`tests/test_brain_panel_ui.py`, the `test_prompt_preview_ui.py` pattern): the rail tab exists with the exact name and ⌥E, controls labelled and round-trip, "Read footage" streams progress into the chip and Cancel works, Edit produces the plan card then the change card with grouped reasons, Plan/Changes tabs keep the focus rules, "Every change" opens, Apply commits the fingerprint, Try another yields a different hook, the decision lane draws ticks with reasons, Versions lists the run, Restore commits one op, a clip's Inspector shows its decisions; the WK harness only for the decision lane and markers (DOM, not the engine). `tsc -b --force` and `vite build` clean; the lint baseline re-measured, not exceeded.

### 13.6 The real-footage tier — the Phase-1 EXIT gate (critique, BLOCKING)

Every acceptance case above runs on lavfi/TTS fixtures whose truths are planted by the same rules that score them (the quotable line IS "the only sentence with a strong number and a superlative"; the fillers are scripted; the word boundaries are clean). They prove the machinery is correct; they cannot say whether the edit is good. "Exceptionally good at these first" (brief §46) is therefore measured on real footage, by editors, and Phase 1 does not exit without it.

**Assets** (EB-2 owns the manifest, loader and schema; the owner supplies or approves each item): ≥ 3 real episodes — one 2-camera podcast with a recorder, one single-wide interview, one remote/Zoom-style recording — and ≥ 5 real talking-head reels (phone and camera, at least two with music under speech in the source, at least one Hinglish), owner-supplied or CC-BY with the licence recorded; stored outside the repo under `VAI_REAL_FOOTAGE_DIR`; never bundled in the `.app` or the tests' egress-guarded fixtures.

**Truth per item** (one editor marks each; a second editor marks the two podcasts so inter-editor agreement is known): the removed source spans (±80 ms), the top-3 hooks by sentence, the angle per turn (multicam), the pauses that must stay, the words a caption pass must get right (names, numbers, terms), and the target length used.

**Bars** (EB-18 owns the gate, `tests/benchmark/test_brain_real.py`, `-m "benchmark and real"`, run per rung with `VAI_BRAIN=recipes` first):

| Measure | How | Bar (recipes rung) |
|---|---|---|
| Cut agreement | removed-span precision and recall against the editor's list at ±80 ms, by time | precision ≥ 0.85, recall ≥ 0.70 on episodes; ≥ 0.80 / ≥ 0.75 on reels; never below the inter-editor agreement minus 0.1 |
| Protected pauses | share of the editor's "must stay" pauses kept ≥ 45 % | ≥ 0.9 |
| Hook | brain top-3 ∩ editor top-3 | ≥ 1 on every item; the brain's top-1 in the editor's top-3 on ≥ 6 of 8 |
| Angle agreement | share of speaking time on the editor's angle or the wide | ≥ 0.85 on the multicam podcast; anticipatory switches within 0-0.15 s before the onset on ≥ 0.9 of turns ≥ 0.6 s |
| Seams | a listening pass over 30 random seams per item by an editor + `seam_click` on the render | 0 audible clicks or upcuts; 0 mid-word seams |
| One voice | the dialogue lane in sync at every seam (`av_offsets_ms` where a clap exists, else `dialogue_in_sync`) | 100 % |
| Captions | the word-check list vs the caption cues | ≥ 0.9 of listed words correct with the best on-disk model; every wrong one present in "words to check" |
| Blind preference | the owner + 2 editors view brain vs editor first cut (label-free, order randomised) for the 5 reels and 3 episode excerpts (5 min each) | "acceptable as a first cut without re-editing the structure" on ≥ 7 of 8 by ≥ 2 of 3 raters; no item rated "would start over" by 2 raters |
| Time saved | editor's time to finish from the brain's V1 vs from raw (self-reported, one reel and one excerpt) | ≥ 50 % less |

Results per rung go into `bench/reports/brain_real.md` with the per-item numbers and the raters' notes; a failing bar is a Phase-1 blocker, not a note. The lavfi fixtures stay the CI gate; the real tier runs on the Mac runner before the flip (`brain.enabled` default on) and before any release that ships the brain.

---
## 14. Phased delivery: lanes EB-1 … EB-18

House rules per lane (as the D/E waves ran): a lane owns the files it lists and no other (two lanes never edit one file in the same wave; where a shared file is unavoidable the gate says who lands first); tests land in the same change; files ≤ 800 lines, functions ≤ 50 lines; no EDL mutation outside `dispatch()`; `main` stays green after every lane; every wave ships behind `brain.enabled` (default off until EB-18) with the kill switch `VAI_BRAIN_ENABLED=0` returning the `auto_edit` checklist and hiding the panel. Sizing: a lane is 3-8 working days for one engineer; a wave is 1-2 weeks with its lanes in parallel. The release gate on this machine must have finished before EB-0.

### Wave A — contracts, tools, fixtures (foundation; ≈ 1.5 weeks)

| Lane | Owns | Delivers | Gate | Exit criteria |
|---|---|---|---|---|
| **EB-0 Preconditions** | nothing | release gate finished; `VERSION` says 0.8.0; the readers' line numbers re-checked against `main`; open decisions §15 settled | — | checklist signed |
| **EB-1 Schemas + store** | `brain/{__init__,schema,store}.py`, `render/cache_budget.py` (LRU class), `storage_project.py` (bundle `brain/`), `tests/test_brain_schema.py`, `tests/test_brain_storage.py` | layer, graph, scene, decision, EDP, review, version models; canonical digest; identity + content keys; `.vae` round-trip | EB-0 | schema tests green; a hand-written graph and EDP validate; `.vae` round-trips `brain/` |
| **EB-2 Fixtures + goldens** | `tests/benchmark/{narration,media}.py` (fixtures 1-5, 7 as amended: recorder + camera mics + click tracks, the two-file angle, the drift, the `uh` islands, the kept questions, the flat/emphatic pair, the throwaway year), `tests/brain_fixtures.py`, `tests/goldens/brain/graphs/*.json` (hand-built from truth), `tests/gen_brain_goldens.py`, `tests/brain_real/{manifest.json,schema.py,loader.py}` (the §13.6 tier's manifest, schema and skip-when-absent loader) | §13.1 fixtures with truth JSON; deterministic, egress-guarded, ≤ 60 s to build; the real-footage manifest format the owner fills | EB-0 | truth files documented; generators deterministic (two builds byte-equal); the manifest validates and the loader skips cleanly without `VAI_REAL_FOOTAGE_DIR` |
| **EB-3 New tools** | `agent/dispatch.py` (+ `cut_source_ranges`, `apply_camera_plan` with per-file offsets, `_set_clip_angle`, `sync_dialogue_lane`, `restore_version`, `_transcripts_by_source`, `add_caption_track.speakers/max_chars/highlight`, `set_caption_style.speaker_colors/accent`, `add_lower_third.speaker`, `make_shorts.from_timeline/windows`), `agent/tools.py`, `agent/prompt/change_rules.py` + `change_words.py` claims, `frontend/src/lib/opLabels.ts`, `tests/test_cut_source_ranges_tool.py`, `tests/test_apply_camera_plan_tool.py`, `tests/test_sync_dialogue_lane_tool.py`, `tests/test_make_shorts_from_timeline.py`, `tests/test_assign_caption_speakers.py` (timemap case), `tests/test_transcript_timemap.py` (multi-src and a1 cases) | the four tools and the additive args, schemas with bounds, card claims, labels | EB-0 (the only lane touching `dispatch.py` in wave A) | `test_all_tools_smoke`, `test_tool_schema_completeness`, `test_prompt_preview_changes` (new tools op by op) green; 200 random cut sets equal `_cut_source_ranges`; bar-coded angle swap proven; the dialogue lane's clicks meet the picture's flashes within half a frame after every structural op |
| **EB-4 Plan contract + sentinels** | `agent/prompt/schema.py` (`TOOL_STAGE` for the three plan-able tools; new `CHECK_SPECS`; `BLOCKING_CHECKS` += 4; `PLAN_DENY` untouched — a test asserts it), `agent/prompt/validate.py` (`$brain:*` rule, `ARG_BOUNDS`, path rule for `ranges[].src`, `switches[].*` and `sync_dialogue_lane.src/offsets`), `agent/prompt/live.py` (one branch), `agent/prompt/executor.py` (`guard_step` sentinel re-check only), `brain/resolve.py`, `brain/checks.py` (the EDL-only blocking checks `dialogue_in_sync`, `single_interp_per_key`; the rest in EB-13), `tests/test_brain_resolve.py`, `tests/test_prompt_contracts.py` additions | declared sentinel family; resolution through `timemap`; footprint; caps; the stage-2 order ending in `sync_dialogue_lane` | EB-1, EB-3 | a hand-written EDP for fixture 1 compiles by hand into a plan, validates, dry-runs and applies as ONE op with `decisions` in the commit args and `dialogue_in_sync` green; `test_prompt_contracts` still pins `vai://plan/1` unchanged |

Milestone A: the hand-compiled EDP round trip (EB-4 exit) on this Mac.

### Wave B — analysers, graph, Gateway (≈ 2 weeks)

| Lane | Owns | Delivers | Gate | Exit criteria |
|---|---|---|---|---|
| **EB-5 Audio, sync, speakers** | `brain/analysis/{pcm,audio,sync,speakers,fillers}.py`, `ai/diarize.py` (numpy MFCC path importable without librosa; `num_speakers` in the cache key), `ai/features.py` (`diarize` probe no longer requires librosa; `angles_sync` hybrid), `tests/test_brain_{speakers,sync,fillers_acoustic}.py`, audio goldens | the 100 Hz envelope, VAD, silences, loudness, own-mic energy, laughter proxy, the acoustic filler detector, per-file offsets + drift, numpy diarization, roles, angle hints in the §3.4 order (own-mic → face size → mouth probe) | EB-1, EB-2 | fixture 1: k = 2, DER ≤ 15 %, hints by own-mic; fixture 2: per-file offsets within 10 ms, the two-file angle and the drift recovered, dead angle `unverified`; fixture 3: 3/3 `uh`s; all with librosa blocked |
| **EB-6 Speech + semantic + Gateway** | `brain/analysis/{speech,semantic}.py`, `brain/gateway.py`, `brain/tasks/*.schema.json`, `brain/{digest,drafts,lexicon}.py`, `ingest/transcribe.py` (the `--prompt` variant behind a parameter, token `p` → `prob`; the unprompted default path byte-identical), `agent/prompt/brains/base.py` (task kinds), `content.py` (tasks + sanitisers), `mlx_brain.py` prompts, `cloud_plan.py`, `tests/test_brain_{speech_layer,semantic,gateway}.py` | sentences, fillers (lexical + merged acoustic), false starts, repeats, weak questions, dead air, words to check; every score of §3.4 with evidence (number class, candidacy, delivery); the Gateway with budgets, repair, provenance | EB-1, EB-2; runs on heuristic + MLX/cloud until EB-9 | §3.4 goldens; the year-and-headcount throwaway ranks below the quotable line; path-free payload grep; model-off = heuristic byte for byte; `tests/test_whisper_cpp_no_dtw_collapse.py` still green |
| **EB-7 Visual, music, graph, jobs** | `brain/analysis/{visual,music}.py`, `ai/reframe.py` (`_detect_subject_centers` returns boxes), `brain/{graph,jobs}.py`, `render/cancel.py` (`ANALYSIS` scope), `api/brain_routes.py` (analyse + graph reads only), `main.py` (mount; background start after `_background_transcriber`; `analysis` in `EVENT_TYPES` via `service.py`), `tests/test_brain_graph_golden.py`, `tests/test_brain_packaged_imports.py` | Haar boxes lazy by range, motion, quality, shots; beats (sidecar / librosa / numpy); scenes and topics; graph assembly and digest; the job with progress/cancel/resume | EB-5, EB-6 | fixture 2 analyses end to end with no key and librosa blocked; graph bytes identical across two builds; digest for FM ≤ 2,400 tokens on the 45-minute synthetic; build ≤ 15 s with the transcript cached on `scene_16x9` |
| **EB-8 Energy table, styles, controls, profiles routes** | `brain/{energy,styles}.py`, `brain/styles/*.json`, `brain/planner/controls.py`, `brain/capabilities.py`, `brain_setting.py`, `api/brain_routes.py` (brief + profiles + capabilities), `tests/test_brain_{energy_table,controls,profiles}.py`, `tests/test_brain_routes.py` | the §4.10 table, four profiles, control mapping, capability table, settings, routes | EB-1 | profiles load/validate/copy; 422 shapes; same-origin covered |
| **EB-9 FM helper** | `tools/fm-planner/Sources/…/IntentDraft.swift` (5 slot fields + 4 `@Generable` task shapes), `fm.py`, `prompt_text.py` (`_FM_SLOT_FIELDS`), `tests/test_fm_helper_source_guard.py` | typed FM shapes | EB-6 schemas frozen | source guard passes; probe + one live `rank_moments` on this Mac; stdin ≤ 64 KB |

Milestone B: fixture 2's graph matches truth (speakers, offsets, angle hints) on this Mac with no key and no librosa.

### Wave C — planner, compiler, recipe, checks, card (≈ 2 weeks)

| Lane | Owns | Delivers | Gate | Exit criteria |
|---|---|---|---|---|
| **EB-10 Story, hooks, tighten, seams, select, emphasis, dialogue** | `brain/planner/{__init__,classify,tighten,seams,dialogue,select,story,hooks,emphasis,finish}.py`, `tests/test_brain_{select_story,emphasis,seams}.py`, `tests/test_brain_planner_goldens.py` | the three narrative modes with the shared antecedent guard, episode cold open as a duplicate only, interview `questions: keep`, nine-axis hooks with a number counted once, jump cuts with reasons, trough-snapped edges, protected pauses, the long-episode split, clause-start punch-ins with step releases, the `dialogue` decision, shorts with the no-repeat rule, typed-draft grounding | EB-7 (or the hand-built golden graphs of EB-2 to start) | fixture 1: weak questions handled per truth, the well-asked ones kept, repeat and false start removed, quotable line = hook top-1, the emotional pause kept; fixture 3: 45 s reel per E3 with the punch-in at the clause start; planner goldens pinned |
| **EB-11 Camera, captions, graphics, music, reframe, broll** | `brain/planner/{camera,captions,graphics,music,reframe,broll}.py`, `ai/multicam.py` (scorer exposed as the fallback), `tests/test_brain_camera.py`, mode-table tests | the rule table with anticipatory switches and hidden jump cuts, per-file switch tables, caption modes (highlight args), lower thirds after the cold open, episode intro/outro/stings and speech-relative levels, keyframed reframe with the active-speaker two-shot rule, suggestions | EB-7 | fixture 2: ≥ 90 % agreement, switches 0-0.15 s before onsets, backchannels never, every tighten seam ≥ 0.4 s hidden, no shot < min-shot, wide on the overlap, dead angle never; fixture 1's wide reframed follows the talker; B-roll fixture markers exact |
| **EB-11b Per-word caption highlight** | `edl/schema.py` (`TextClip.emphasis`, `CaptionLook.accent`, RBV 31 → 32), `render/text_overlay.py`, `render/ass_writer.py`, `frontend/src/lib/{textLayout,captionRun}.ts` (+ tests), `agent/dispatch.py` `add_caption_track` `highlight` branch ONLY (a section-header-delimited block; EB-3 has landed), `tests/test_caption_emphasis_render.py`, `tests/test_caption_highlight_cues.py`, the new frame-map/overlay golden case | §4.7.3: the schema fields, both renderers and the ASS export, karaoke-by-static-cues, keyword accent | EB-3 | renders byte-identical without the fields; accent under the right token in PIL, ASS and the WK canvas (PSNR ≥ 35 dB + colour mass); one cue per word; **gates EB-12's E3** |
| **EB-12 Compile, recipe, facts, grammar, costs** | `brain/compile.py`, `agent/prompt/brain_expanders.py`, `recipes.py` (cards), `expanders.py` (`expand_auto_edit` delegation), `facts.py` (`brain_graph_id`, `brain_layers`, `dialogue_lane` + `minimal()` stubs + one prompt-block line), `grammar.py`, `semantics.py`, `semantic_fixups.py` (edit family rows; revision readings), `costs.py`, `tests/test_brain_compile.py`, `tests/test_k3_prompt_corpus.py` additions | EDP → Plan ≤ 24 steps (cuts split, `sync_dialogue_lane` last in stage 2); the `edit` card; analysis gate question incl. "Keep my edits / Start from raw"; grammar rows | EB-4, EB-10, EB-11, EB-11b | E1-E5, E8, E11-E13, E18 pass through the real `/prompt` route with `VAI_BRAIN=recipes`; K3 wrong-commit rate 0 with `brain.enabled` on and off |
| **EB-13 Checks, contract licence, review loop, card payload** | `brain/{checks,review,score,revise}.py` (the remaining checks: `seam_click`, `tighten_uniform`, `jump_cut_hidden`, `punch_min_hold_geq`, `pauses_protected`, `caption_highlight_present`), `agent/prompt/verify.py` (register), `contract.py` / `contract_rules.py` (decision-kind licence, shadow mode), `preview.py` / `changes.py` (footprint → `why`, grouped lines, "words to check"), `executor.py` (`contract_hint["decisions"]`, the single revision round wiring, commit args `decisions`/`score`), `service.py` (loop), `summary.py` (rung attribution, "revised once", objective vs measured labels), `tests/test_brain_{checks,review_loop,apply_determinism,one_op,never_flattens}.py` | the Reviewer, the Editing Score with labelled axes, one bounded revision, reasons on the card, the EDP licence | EB-12 (lands after it; the only other lane touching `executor.py` in wave C) | seeded bad plan repaired in one round; score never lowered; determinism and one-op tests green; shadow-mode contract log over E1-E9 shows 0 false `unasked` |

Milestone C: E1-E5, E8, E11-E13, E18 green on the fixtures with `VAI_BRAIN=recipes` on this Mac, the Viral highlight included.

### Wave D — shorts, revisions, versions, protection, feedback, UI (≈ 2 weeks)

| Lane | Owns | Delivers | Gate | Exit criteria |
|---|---|---|---|---|
| **EB-14 Shorts + NL revisions** | `brain/planner/revise.py`, `executor._finish_children` (child EDPs), `contract_rules.py` (`edit_revise` families), `tests/test_brain_revise.py`, E6, E9 | children with their own compiled sub-plans; the §7.3 sentence families | EB-13 | E6 and E9 pass; corpus block "edit revise" ≥ 40 phrasings with wrong-commit rate 0 |
| **EB-15 Versions, protection, feedback** | `brain/{versions,protect,feedback}.py`, `edl/snapshot.py` (pin exemption), the remaining `api/brain_routes.py` (versions, suggestions, speakers, broll_bin, `dialogue/resync`), `tests/test_brain_{versions,protect,feedback}.py` | named versions, `restore_version` route, the protected set (first-run baseline included) + blocking check + three-option clarify, "Undo this decision" incl. the re-cover of a restored cut, the Re-sync button's route, signals (+ `dialogue_lane_edited`) | EB-13 | a hand edit after V1 survives "make the first 10 s faster"; a title, music and a trim placed BEFORE the first run survive "Keep my edits"; restore = one op; undoing one cut leaves `dialogue_in_sync` and `captions_cover` intact; signals exact for a removed punch-in, an adjusted caption size, a restored cut, a control override |
| **EB-16 Frontend: panel, rail, chip, settings, help** | `frontend/src/components/panels/BrainPanel.tsx` + css, `lib/{brainStore,brainCopy,brainControls}.ts`, `rail/railModel.ts` (+ test), `rail/ToolPanel.tsx`, `keymap/{commands,presets}.ts` (`panelBrain`), `topbar/ActivityChip.tsx`, `lib/activityStore.ts`, `SettingsDialog.tsx`, `lib/settingsModel.ts`, `Help.tsx`, `api.ts`, `types.ts`, `store.ts` (`brain_controls` in `ui_state`) | the panel, ⌥E, analysis progress with cancel, settings, help | EB-8 (routes) | Playwright Chromium + WebKit: tab with the exact name, chord verified in WKWebView, controls round-trip, chip streams and cancels; vitest for the libs |
| **EB-17 Frontend: card, inspector, versions, lane** | `components/brain/EditPlanTab.tsx`, `PromptPreviewCard.tsx` (tabs, Try another), `lib/promptEvents.ts`, `lib/promptStore.ts`, `components/inspector/BrainDecisions.tsx`, `Properties.tsx`, `components/brain/{VersionsStrip,VersionCompare,SuggestionsPanel,SpeakersRow}.tsx`, `OpsLog.tsx`, `lib/opLabels.ts` (Why rows), `Timeline.tsx` + `lib/brainLane.ts`, `lib/{brainDecisions,editingScore}.ts`, `tests/test_brain_panel_ui.py` | Plan tab, reasons on lines, score, Try another, Inspector decisions, versions UI, suggestions, decision lane | EB-13 (card payload), EB-15 (versions) | `test_prompt_preview_ui.py` still green; Plan/Changes tabs keep focus rules; Apply commits the fingerprint; Restore is an op in History; "Undo this decision" previews a one-step inverse |

### Wave E — eval, packaging, docs, flip (≈ 1 week)

| Lane | Owns | Delivers | Gate | Exit criteria |
|---|---|---|---|---|
| **EB-18 Evals, budgets, packaging, docs, the real-footage gate, default on** | `tests/benchmark/test_brain_editorial.py` (E1-E18), `tests/benchmark/test_brain_real.py` (E19, the §13.6 bars, `-m "benchmark and real"`), `tests/benchmark/report.py` (brain table + `brain_real.md`), fixture 6, `tests/test_brain_budgets.py`, `build_app.sh` (no new exclusions; the packaged-shape test), `docs/EDITOR_BRAIN.md` (user doc + reason-code table), `docs/design/EDITOR_BRAIN_SPEC.md` (this document, promoted), `docs/PROMPT_EDITOR.md`, `docs/BENCHMARK.md`, CLAUDE.md ("Editor Brain" section: EDP frozen before the dry run, sentinels resolve through `timemap`, no `PLAN_DENY` change, never flattens, the dialogue lane is rebuilt last), `brain.enabled` default on | the full suite green on macOS 26 and 27 runners; budgets met on the M4 Max; packaged `.app` smoke (analyse + `edit` on fixture 1 with librosa/torch/mlx absent); the real-footage tier run with the owner's assets and the raters' notes filed | all | E1-E18 green per rung; **every §13.6 bar met on the recipes rung** (the Phase-1 exit); `-m benchmark` in the release gate; docs landed |

### EB1 as built (the first build wave, 2026-09-29)

The slice of `EB1_BRIEF.md` runs on this Mac with no cloud key, behind `brain.enabled = false`: `tests/benchmark/test_eb1_slice.py` (`-m "benchmark and eb1"`, 14 tests) drives both demo prompts through the real `/prompt` route and measures every claim from the persisted EDL, the fixture truth and the decoded render. What differs from the text above, each by measurement, is listed in `EB1_BRIEF.md` "EB1 as built" (13 items): the single offset convention, removals on the frame grid by the planner, the asked length met in air, removals merged by union, fillers as voiced islands, the camera rules (every change a speaker's switch before a sustained onset; scale steps for in-turn seams; the rate cap reported), punch-ins by delivery and in programme time, `no_cut_mid_word` measured by the Content Graph, the second angle leaving the main lane, `facts.timeline_paths`, the analysis gate answered in the turn, version names, and model provenance on the EDP. Open after EB1: a kept pause after a line that is emotional only in its WORDS (the detector is acoustic), speech whisper dropped entirely, the false start on a camera microphone's transcript in a live session (found once the opener rule was relaxed; see the report), per-decision seek buttons, the Settings switch.

Sequencing: A → B and C in parallel (EB-10/11 start on the hand-built golden graphs while B analyses real fixtures); EB-11b starts as soon as EB-3 lands and must land before EB-12's E3; EB-12 needs EB-4; EB-13 after EB-12; D after C; EB-9 (Swift) is the only lane with a toolchain risk and is not on the critical path (EB-6 runs on MLX/heuristic until it lands). Shared files by wave: `dispatch.py` (EB-3 only in A; EB-11b's one delimited `highlight` block in C after EB-3; none in B or D), `executor.py` (EB-4 in A; EB-13 in C; EB-14 in D, after EB-13), `schema.py`/`validate.py`/`live.py` (EB-4 only), `edl/schema.py` (EB-11b only), `ingest/transcribe.py` (EB-6 only), `api/brain_routes.py` (EB-7 analyse/graph, EB-8 brief/profiles, EB-15 the rest — three disjoint route groups, landing in that order), `PromptPreviewCard.tsx` (EB-17 only). The FIRST build wave is briefed separately in `EB1_BRIEF.md` (a demonstrable slice through EB-1/2/3/4/5/10 sized like the D/E waves).

### Wave EB1 as built

Written after the fix wave, from the six build-lane reports, the integrator's report, the seven fix-lane reports, the finalizer's and the closer's, and the three re-verifiers' findings. The build wave landed on 2026-09-29; three independent re-testers then found 41 defects, a fix wave closed them, three re-verifiers found 25 more, and a closer fixed those. The text above this heading (the lane table and the short "EB1 as built" paragraph) is the plan and the integrator's first summary; this section is the record of what the tree holds. Where the two differ, this section wins. The wave's brief, with its own "EB1 as built" list, is `docs/design/editor-brain/EB1_BRIEF.md`.

#### What landed

Behind `brain.enabled` (default **off**; `VAI_BRAIN_ENABLED=0` hides every surface and returns the 0.8.0 Prompt bar), on this Mac, with no cloud key:

| Spec lane | State | What exists |
|---|---|---|
| EB-1 schemas + store | landed | `brain/schema.py` (frozen Pydantic: graph header, four layers, scenes, angles, EDP; `extra=forbid`, 4-decimal monotone times, closed reason codes, canonical digest, 32/64 MB caps), `brain/store.py` (identity and content keys, atomic temp+replace writes with a per-write temp name, immutable EDPs, the current-graph pointer, which retires a speech-less pin once a transcript exists), `brain/versions.py`, `storage_brain.py` (a whitelisted `brain/` section and the pinned snapshots travel in a `.vae`). |
| EB-2 fixtures + goldens | landed (TH and P2 only) | `tests/brain_fixtures.py`, two golden graphs and four pinned EDPs; two fixtures with truth JSON, TH (a 69.9 s talking head) and P2 (a 168.6 s two-camera recording plus a recorder), byte-equal across two cold builds in 21.5 s. The other fixtures of §13.1 and the real-footage tier were not built. |
| EB-3 new tools | landed (three of four) | `cut_source_ranges`, `apply_camera_plan`, `sync_dialogue_lane`, plus `add_caption_track.cues` (additive, added by the fix wave). `restore_version` is a route over `brain.versions.restore` (one op), not a dispatch tool. |
| EB-4 contract + sentinels | landed | `$brain:*` for six declared (tool, arg) pairs plus `captions` (seven; the fix wave added `('captions', 'add_caption_track', 'cues')` and both frozen-table pins say so), `brain/resolve.py` through `agent/timemap`, `brain/plan_rules.py`, two EDL-only blocking checks (`no_cut_mid_word`, `dialogue_in_sync`) and, from the fix wave, `removal_within_plan` and a second-camera-left-on-v1 check enforced in `executor.safety_net`. `PLAN_DENY`, `Plan.steps <= 24` and `vai://plan/1` are unchanged and pinned. |
| EB-5 audio, sync, speakers | landed (no librosa) | the 100 Hz envelope, VAD, acoustic filler detector (three planted "uh"s found within 30 ms, no false positive on TH), per-file offsets within 10 ms, numpy diarisation, roles, angle hints. Multi-file angles, drift and the dead-angle verdict are not built. |
| EB-6 speech + semantic + Gateway | landed (one model task) | `brain/analysis/{speech,semantic,fillers,word_timing,transcripts}.py`, `brain/gateway.py`, `brain/digest.py` (path-free payloads), words repaired onto the sound, unheard-voice flags. The Gateway ranks hooks through `fm.py`'s existing text path; a model that does not answer changes no byte of the EDP. The Swift `@Generable` shapes (EB-9) and the prompted whisper pass are not built. |
| EB-7 visual, music, graph, jobs | graph and jobs only | `brain/graph.py` (`analyse`, single flight per session, cancel honoured at every layer boundary, waits cancellably for the upload's transcript), `api/brain_routes.py` (analyse, graph, decisions, versions, restore). No visual or music layers. |
| EB-8 energy, styles, controls | table only | `brain/energy.py` (the §4.10 rows the slice uses) and `brain_setting.py` (the flag and its settings route); no style profiles, controls panel, profiles routes or capability table. |
| EB-10 planner | landed | classify, tighten, seams, select, story (reel and episode), hooks, emphasis, finish, dialogue, captions; the retake rule keeps the later take in both recipes. |
| EB-11 camera, captions, graphics, music, reframe | partly | camera (rules 1, 2, 5 and the report of rule 8), podcast and reel captions laid from the graph's words through the resolver, a reel bed, the 9:16 crop and export preset. No lower thirds, intro/outro, face-follow pans, B-roll suggestions. |
| EB-11b per-word highlight | not started | |
| EB-12 compile, recipe, facts, grammar, costs | landed | `brain/compile.py` (EDP to Plan, at most 24 steps, the stage-2 order imported from `brain/resolve.py`), the `edit` recipe (hidden from the model-facing recipe table while the flag is off), `agent/prompt/{brain_expanders,facts_brain,edit_grammar,brain_seams}.py`, grammar rows read only when the flag is on, the analysis gate answered inside the turn. |
| EB-13 checks, licence, review loop | partly | the blocking checks above, the contract's composite reading of the edit asks (flag-gated), `brain_card.py` + `brain_card_marks.py` (the Plan tab payload; a decision is "not applied" only when its footprint is absent from the diff), `brain_reply.py`. No Reviewer, no revision round, no Editing Score. |
| EB-14 shorts + revisions, EB-15 protection + feedback | not started | `brain/versions.py` and the restore route exist (EB-15's first half). |
| EB-16 / EB-17 frontend | slice only | the Plan tab (a seek button and timecode on every decision with a source range), the Versions strip (keyboard Restore keeps focus and announces), the analysis progress line with Cancel, card layout that keeps Apply and Change hittable at 900x640 to 1920x1080 in Chromium and WebKit. No panel, rail entry, chip, Settings switch or Inspector decisions. |
| EB-18 | not started | the flag stays off; `tests/test_brain_schema.py` pins `brain.enabled` false, `RENDER_BEHAVIOR_VERSION` 32 and `PLANNER_VERSION` 2. |

#### What was deferred, and why

| Item | Why | Where it goes |
|---|---|---|
| General one-key keyframe preview/export mismatch | `RENDER_BEHAVIOR_VERSION` and the renderer are frozen this wave; the planner emits two keys, so its own output is right (below) | a product bug with a render-version bump (`eb1-followups.md`) |
| Switch-rate cap enforced | P2 runs at 14 turns a minute; no subset under the premium cap of 6 keeps 90 % of the talk on the speaker's close (measured: enforcing 6 a minute left 7 switches and well under 90 %) | reported on the card; enforce by holding the wide once a third angle exists (rules 3-7) |
| Kept pause after an emotional line (P2) | the detector is acoustic and the two synthetic voices carry none (score 0.30, floor 0.6); whisper merged the line with the next sentence | slice test is a strict xfail; a lexical emotion cue or the Gateway's label |
| Speech whisper dropped entirely | no new ASR pass this wave; the planner now refuses to cut sound no word names (three P2 stretches, 33.70-35.10, 38.14-39.46, 71.05-71.51 s), proven with the words removed from the transcripts | the prompted whisper pass |
| Dialogue lane after a hand edit | by the brief nothing pairs lanes inside the tools; the facts now report whether `a1` is in step, a non-sync plan on a stale lane says so, and "sync the dialogue" re-lays it | an automatic rebuild or ripple after structural v1 ops (EB2) |
| Analysis layers in a `.vae` | large and re-derivable; the manifest says so | a reopened project reads fully but a new brain edit reads the footage again |
| `add_caption_track.max_chars`, per-word highlight, speaker colours, lower thirds, music beyond the reel bed, reframe pans, shorts children, NL revisions, protection, feedback, the controls panel, the Reviewer and Score, the real-footage tier | the brief's "Explicitly NOT in EB1" list; each is named on the card in `deferred[]` | EB2 onward |
| Settings switch and Activity chip for the flag and the read | outside the brief's F list | the gate's `analysis` frames and `api.setBrainSettings` are ready for them |

#### Measured slice numbers

`tests/benchmark/test_eb1_slice.py` and `tests/benchmark/test_eb1_planner_export.py`, `-m "benchmark and eb1"`, `VAI_BRAIN=recipes`, through the real `/prompt` route, `ANTHROPIC_API_KEY=""`, `HF_HUB_OFFLINE=1`, librosa/torch/mlx blocked.

| | Integrator (build wave) | After the fix wave and the closer |
|---|---|---|
| Tests | 16 passed, 1 strict xfail, 38 s | 57 passed, 1 strict xfail, 285.7 s (the finalizer's run: 293 s) |
| TH reel length (45 asked) | 44.733 s | 44.033 s: pauses give back at most 0.3 s and never leave a hole over 0.6 s; longest hole between sentences 0.786 s (was 1.00 and 1.15 s) |
| TH opening | 0.274 s before the quotable line | 0.274 s before |
| TH fillers planted | 9 of 9 gone by time | 9 of 9 |
| TH punch-in | in 1.7 ms from the clause start, 1.00 to 1.10 | in 1.7 ms from the clause start, 1.08 to 1.13 (a push of at least 0.05) |
| TH seams in the decoded export | not measured; 7 of 8 hides had been dropped before integration | every seam of 0.4 s or more changes the zoom by 0.08-0.10 |
| TH captions cover | 96.8 % | 98.1 %; every kept, non-filler word is in a cue |
| TH cut edges | 18, 0 off a trough, loudest -59.3 dBFS | the slice's trough assertion still passes and `no_cut_mid_word` is blocking |
| P2 time on the speaker's close | 92.8 % (24 changes, 28 pieces) | 99.5 % (31 changes, 35 pieces); 34 full turns each at least 90 % on their speaker |
| P2 switch lead over the heard onset | 0.080-0.129 s | 0.009-0.127 s |
| P2 captions cover | host only (18 cues) before the fix | S1 97.3 %, S2 99.7 %; camera A 97.3 %, camera B 99.7 % |
| P2 click vs flash (35 pairs) | worst 0.04 ms at 20 fps | 20 fps 0.29 ms, 25 fps 19.96 ms (half frame 20.0), 29.97 fps 16.55 ms (16.68), 30 fps 16.96 ms (16.67; see the EX-08 row) |
| Re-running the brain on its own output | committed a wrong 47.4 s result (107.55 s cut) | commits nothing; one sentence |
| Rungs | FM stubbed: EDP equals recipes EDP byte for byte; FM live: only `content_brain` and the hook `by` differ | unchanged |

The closer's last runs on the tree (one pytest process at a time): the brain files 556 passed; the three tool files 33; contracts, smoke, schema completeness, preview changes, K3 corpus and safety net, same-origin, path guards, project I/O, verify and facts 1492 passed and 23 skipped; the prompt suites 2067 passed and 1 skipped. The full suite, `vite build`, lint and the `wk` suite belong to the release gate.

#### Recorded deviations, each with its measurement

Build wave (from the lane reports and the integrator):

1. **Offset sign (corrected formula).** The brief's `sync_dialogue_lane` text had the two offset terms the wrong way round, contradicting `apply_camera_plan`'s `offsets[angle_src] - offsets[src]` and §2.4. The convention is one: *an event at reference second r is at file second r + offsets[file]*. The lane lays `in_ = piece.in_ + offsets[src] - offsets[piece.src]`; §5.4 row 4 above carries the corrected text. Measured on fixture P2 (+0.35 / -0.20 s): clicks meet flashes within 0.04 ms with this sign and sit 0.8 s apart with the other (`tests/test_sync_dialogue_lane_tool.py::test_wrong_offset_sign_is_measurably_out_of_step`). `dialogue_in_sync` checks the same and accepts the tool's clamp where the dialogue file has not begun (a camera that rolled 0.35 s before the recorder); one frame late fails.
2. **Removals on the frame grid, by the planner.** The tools round a removal outward; that moved trough-placed edges up to a frame into kept sound (planned 43.81 s, applied 43.57 s). The planner rounds every removal inward onto the grid (a filler's reaches outward so the whole filler goes), so the tool's rounding is a no-op. The fix wave moved the grid from the file's rate to the project canvas rate (`Ctx.fps`, through `edl/timebase.rate_of`, 29.97 as 30000/1001) and, for the reels preset, to the rate the preset will set (a 20 fps project conformed to 30 fps had planned pieces at half-frame starts, sound leading picture by up to 50 ms).
3. **The asked length is met in air.** Whole sentences land within +-1 s; shortened pauses give back time. Build wave: up to 0.45 s each, within 0.2 s of the ask. Fix wave: at most 0.3 s each, spread evenly, and never a hole over `min(min_silence_s, dead_air_s / 2)` (0.6 s at energy 5), because 0.45 s left 1.0 s and 1.15 s holes between sentences. The reel is 44.03 s of 45 and says so; it holds the +-1 s bound with 0.03 s to spare.
4. **Removals merge by union**, with the more specific reason; two separated by under 0.5 s of air with no kept word are one cut. A gap is cut as silence only where the audio layer is unvoiced.
5. **A filler is its voiced island** (the VAD run of at most 0.6 s it sits in), not its token: whisper timed the next word into the tail of an "um" ("we" at 24.20-24.30, real onset 24.49).
6. **Camera.** Every change is a speaker's switch before the first sustained sound (at least -45 dB for 50 ms; the fixtures' one-frame click sits 20-50 ms ahead of the voice). The lead is planned at `min(3 frames, 0.15 - 0.03 s)`: the onset estimate is good to -27...+36 ms (33 P2 turns), which at 3 frames gave leads up to 0.177 s; leads are now 0.009-0.127 s (`ONSET_TOL_S` 0.03 to 0.04 at finalize). In-turn seams get a scale step on two cameras as on one; the listener's-close cover would cut back away from every onset and belongs to reaction cutaways. The fix wave made a whole turn of 0.6 s or more (not a backchannel, not a true overlap) take its speaker's close whatever the 2.5 s minimum shot says, trimmed the episode head to 0.3 s before the first sound (it had 0.87 s of nothing), and snapped a switch to a cut edge within two frames (a one-frame piece of the old angle had survived).
7. **Punch-ins.** A line both at least 1.5 sigma louder and emotion at least 0.6 is a candidate by delivery (lane D's importance tops out at 0.30 on TH, so the 0.5 floor never fired); the peak is the loudest content word (by mean envelope it was "the", -22 dB, 0.14 s); cadence is counted in programme time on a reel (in source time the two punches were 4.8 s apart and one was dropped).
8. **`no_cut_mid_word` measures by the Content Graph** when one is current (whisper.cpp timed "Um," 0.2 s past its island and the check rolled back a clean cut); the upload transcript is the stick only without a graph. It reads cut edges in the source clock, because `agent/timemap` clips a straddling word to its surviving part. Closer: 53 of 192 plans on the real recorded graphs had an edge inside a word because the frame grid sits under words that are not on it; a removal edge now moves to the word's far side (`Ctx.word_safe`), leaving 3 of 192, all from overlapping recogniser word times, which the safety net refuses with one sentence.
9. **Two cameras on the timeline.** The second angle's own clip leaves the main lane first (a literal `cut_source_ranges` step in whole-file pieces of at most 600 s), then returns through the camera plan. The fix wave made `Graph.primary` the angle that carries the dialogue (it had been `members[0]`, the guest camera, when no recorder exists) and added the reference camera as a member for an `angles.json` written before.
10. **Files already on the timeline** may be named by the three brain tools only (`facts.timeline_paths`).
11. **The analysis gate is answered in the turn** ("read" starts the job through the one function the route also calls, streams `analysis` frames, re-plans the sentence); "Wait for it" survives the transcript landing (the gate's facts hash leaves the transcript bit out).
12. **Version names** come from what the edit made ("Reel", "Premium Podcast"); the label is computed at record time, so the same plan applied twice records V1, V2. `Compiled.plan_id` is optional; `repeat` is in `REASON_CODES`.
13. **Provenance.** A model's hook ranking is named on the EDP (`content_brain`, the hook decisions' `by`).
14. **Fixtures.** The "uh" islands are formant-synthesised schwas (0.24 s) because whisper transcribes Piper's "Uh." as "Ah,"; spacing 0.25 s before and 0.35 s after by measurement of 14 spacings under both whisper backends; Piper `length_scale` 1.25 (1.15 stretched only 7 %, 1.25 measures +16 %, the brief's "15 % slower"); P2 runs at 20 fps and 44.1 kHz (the +0.35 and -0.20 s offsets are whole frames only at 20 or 60 fps), TH at 30 fps and 22.05 kHz; TH is 69.9 s and P2 168.6 s; every TH spoken part is levelled to -29 dBFS so the planted +6 dB is +6 dB over every other sentence; the 40 ms camera-mic delay is a reflection, not a shift, so each file has one offset.
15. **Analysis.** For a recorder reference there is no `ingest.json`, so v1's transcript moves onto the reference clock by the measured offset; word times are repaired toward the voiced runs; voiced sound under no word is tiled or flagged `unheard_voice` (unclaimed voiced seconds: TH 2.0 to 0, P2 recorder 12.28 to 0.84, P2 with camera A's transcript 28.62 to 3.27); sentence-edge goldens use +-0.25 s because whisper.cpp's repaired edges measured -0.22/+0.21 s.
16. **Contract plumbing.** `plan_rules.py` is a seventh module because `validate.py` was already 841 lines; `versions.json` lives at `<session>/brain/versions.json`, not `<session>/versions.json`; a pinned snapshot is parked under `snapshots/pinned/` so it neither inflates `undo_depth` nor becomes `snaps[-2]`; `EVENT_TYPES` is unchanged (11) and the `analysis` frame lives in `service.BRAIN_EVENT_TYPES`; new tools are registered with `DISPATCH.update(...)` after the literal; list caps are enforced in the handlers (`CUT_RANGES_MAX`, `CUT_RANGE_MAX_S`, `CAMERA_SWITCHES_MAX`, `OFFSETS_MAX`, now `CAPTION_CUES_MAX` in `agent/tools.py`) because `_validate_tool_args` checks numbers, not list lengths; the card words for the new tools are `change_rules_brain.py`.
17. **Export honours easing.** `tools.py`'s note that exported renders interpolate linearly only is stale: the keyframe matrix golden shows ease-out ahead of linear. A punch-in uses ease-out and releases as a step at the seam.

Fix wave:

18. **The one-key keyframe decision (EX-01).** The compositor treats a one-key `Keyframe` as static 1.0 (`is_keyframed` needs at least two keys; `sc_static` is 1.0 for a non-number), while the browser preview (`lib/overlay.ts` `sampleKF`) draws the key's value, so every single-key `jump_cut_hide` (scale 1.08, `step`) rendered at 1.0 in the export: three TH seams and every P2 hide read 1.0 before and after in decoded frames, and the same shape comes from the props form's Keyframe button. `RENDER_BEHAVIOR_VERSION` is frozen, so this wave fixes the planner side only: `emphasis._hide_decision` emits two keys holding one value, the second `HIDE_KEY_LAG_FRAMES = 2` frames after the first, interp `step`. Preview and export now agree to 0.012 at every seam of the decoded reel, and the card says what happened (`from_scale`, `scale`, `step`). The general case (a user's own one-key keyframe) is **not fixed**: `render/compositor.py:841-861` should treat a one-key keyframe as static via `keyframes.sample(tx.scale, 0.0)` for scale, x, y, rotation and opacity, with a `RENDER_BEHAVIOR_VERSION` bump to 33.
19. **Hides alternate against the neighbour's effective scale** (decode the chain piece by piece: an unkeyed piece is 1.0, a punched piece ends at its held release), so every seam of 0.4 s or more steps by at least `HIDE_STEP_MIN = 0.06`; a seam no level can change is left plain and not claimed. A punch on a piece that opens on a hold raises to at least hold + `HIDE_PUNCH_ROOM` (0.05).
20. **Captions come from the graph, not the first file's transcript.** A new sentinel `$brain:captions` and an additive `cues` argument on `add_caption_track` lay cues from the graph's kept words on the reference clock for every speaker, mapped through the live v1 layout by file and offset. Backchannels and words under no speaker turn are not captioned for a podcast; a single speaker's word that falls between two turns is still captioned. This changed the frozen sentinel table (both pins updated) and the EDP bytes (`PLANNER_VERSION` 2).
21. **The resolver splits ranges.** Every resolved range is cut into abutting pieces of at most `CUT_RANGE_MAX_S - 0.5` (599.5 s) so float subtraction can never trip the tool's "at most 600 s"; the union is unchanged.
22. **Removal check.** `removal_within_plan` (in `brain/checks.py`, run by `executor.safety_net`, not a `CheckSpec`, because `agent/prompt/schema.py` was not the lane's) blocks a run that removes more picture than its decisions name plus one frame per cut. N-19 pins the tolerance at a boundary (a run that removes the named picture plus one second is blocked), not five seconds off.
23. **A re-run on an edited timeline is refused in one sentence** by the expander (`facts_brain.brain_edit`: four states), with the resolver's `AlreadyEdited` as the last line.
24. **Reply and card.** The brain reply is composed by `agent/prompt/brain_reply.py` (flag-off replies are byte-identical): the length once, the hook quote whole or cut at a word with an ellipsis, "Not done this time" once, the advisory aesthetic audit as a note, never a failed headline; a rolled-back brain plan is one plain sentence naming the check that did not hold, with no intent picker.
25. **30 fps click tolerance.** `render/audio_mix.py` places audio with `adelay` in whole milliseconds, so at 30 fps (33.333 ms frames) a click lands on a 0.333 ms lattice (-16.625, -16.958, -16.292 ms for the same half frame). The slice allows 0.5 ms over the half-frame bound only where the frame is not a whole number of milliseconds (0.1 ms elsewhere). Planner and EDL positions are frame-exact; the half-frame bound itself was not loosened. The renderer fix (fractional or sample-accurate `adelay`, as `ATEMPO_LAG` already does) needs a render-version bump and is a follow-up.
26. **`contract.py` reads the edit asks as composite only with the flag on** (outside the lane's list; git-clean before the edit), which removed the validator prose ("It added captions, which the request did not ask for") for "edit this" and "make it punchier".
27. **Upload transcript marker.** `main.upload` writes `transcript_status: "skipped"` for `transcribe=false` and the failed whisper thread writes `"failed"`; `transcript_state` and `facts.transcript_pending` honour both (found by the card UI run: the gate had called a no-transcript upload "still being transcribed"). With the flag off the Prompt bar reads `ingest.json` as 0.8.0 did.

#### The 41 review findings and what happened to each

Statuses: **fixed** (a test fails on the unfixed behaviour and passes now), **partly**, **deferred**, **closed** (no code defect). Where the re-verifiers found a first fix incomplete, the later fix is named in brackets (N-nn are the verifiers' new findings, below).

| Id | Finding | Disposition |
|---|---|---|
| SC-01 | a double quote in a hook sentence fails the whole analysis (PathLeak) | fixed: the guard walks string values; the semantic layer degrades to heuristics, marked partial |
| SC-02 | resolver emits ranges over the tool's 600 s cap | fixed: `split_range`; a 45-minute reel resolves and dispatches |
| SC-03 | the second angle leaves the main lane for only its first 600 s | fixed: whole-file ranges; a 45-minute two-angle EDL test |
| SC-04 | flag off is not 0.8.0 in grammar and replies | fixed: edit rows live in `edit_grammar.py`, read only with the flag on; a frozen copy of 0.8.0's `grammar.py` is the differential baseline (about 570 phrases, 0 differences; wrong-commit rate 0 with the flag on, 1027 K3 tests, and off, 643). Residuals closed with N-10 and N-24 |
| SC-05 | path rule not applied to resolved brain args | fixed: `declared_paths` at validate time, the same rule on resolved args in `guard_step` |
| SC-06 | the same plan applied twice records two equal labels | fixed: label computed at record time [N-18: the wiring is now pinned through `_run_and_stream`] |
| SC-07 | no single flight per session; shared temp name | fixed: unique temp names, joined requests, `AnalysisBusy` 409 [N-08: a cancelled job no longer absorbs the next request] |
| SC-08 | `RENDER_BEHAVIOR_VERSION` 32 in the tree beside CI-triage work | closed: 32 is the committed CI change, not an EB1 edit; the wave was measured at 32 and a test pins it with `PLANNER_VERSION` 2 and `brain.enabled` false |
| SC-09 | file and function size limits crossed | partly: `facts.py` 815 to 736 (brain code in `facts_brain.py`, 302), `overlays_and_audio` back to 34 lines, every new source function at most 50 lines, every new source file under 725. Files that were over 800 at HEAD and grew again stay over (see N-21 and the notes below) |
| SC-10 | narration diff is not additive | fixed: the Hindi branch (`say` and `piper`) is compared byte for byte with a verbatim copy of the old one |
| SC-11 | duplicated filler lexicon, stage-2 order, tool caps, floor-to-frame | fixed for the order, the caps and `Ctx.q` (deleted); the filler lexicon is still two literals, now pinned equal by a test (N-20; `eb1-followups.md`) |
| SC-12 | `DialogueLaneFact.in_sync` always None | fixed: measured by `c_dialogue_in_sync` on the live EDL, computed only with the flag on |
| SC-13 | the graph route has no size cap and falls back to the newest file | fixed: capped reads, `current_graph_id` only, 413 and 404 answers |
| SC-14 | absolute paths in failure text | fixed: leaf-name scrub in the job record and the gate reply [N-23: file: spellings and paths inside sentences] |
| SC-15 | `sync_dialogue_lane` creates a lane before the batch | fixed: the lane is created inside `store.batch()` |
| SC-16 | wrong reason in the resolver's drop notices | fixed: one notice per distinct reason from each dropped row |
| SC-17 | three slice assertions cannot fail | fixed: punch-in interp equals the decision's, `hidden_by` needs the zoom to change by 0.05, the 600 s truncation is asserted; a dozen mutation runs each fail the test they protect |
| EX-01 | hide keys dropped by the export | fixed for the planner's own output (two keys, item 18); the **general one-key mismatch is deferred** |
| EX-02 | a re-run on an edited podcast cut 107.55 s | fixed: expander refusal, resolver `AlreadyEdited`, `removal_within_plan` |
| EX-03 | `.vae` carries no `brain/` | fixed: `storage_brain.py`; save, open, restore V1 round trip; hostile entries refused |
| EX-04 | which seams get a scale step | fixed: chain-decoded levels, steps of at least 6 %, decoded export 0.08-0.10 at all 8 seams |
| EX-05 | labels on a repeated plan | fixed with SC-06 |
| EX-06 | a1 stale after a hand edit | **deferred** (EB2); facts report the stale lane, plans say so, "sync the dialogue" re-lays it; undo, redo and restore round trips are clean; the versions row carries no "out of step" hint |
| EX-07 | five whole turns never on the speaker's close | fixed: 99.5 % talking share, 34 of 34 full turns |
| EX-08 | planner grid versus project grid | partly: planner on the project grid; 20, 25 and 29.97 fps within half a frame with every hide key kept; at 30 fps 9 of 35 pairs read 0.3 ms past half a frame because of the renderer's whole-millisecond `adelay` (frozen); reels from a project below 23.976 fps are planned on the preset's grid [N-12] |
| EX-09 | verified green, no defect | closed |
| UX-01 | analysis races the transcript; a cut-less reel is offered | fixed: cancellable wait, a speech-less graph is never pinned, refusal in one sentence [N-02: the gate survives the transcript landing; N-07: a silent clip gets no edit] |
| UX-02 | Apply and Change unreachable under the viewer | fixed: 16 Playwright cases in Chromium and WebKit; 6 of 16 failed before |
| UX-03 | podcast captions only for the host | fixed: captions from the graph for every speaker [N-04, N-05: words in turn gaps, orphan and lower-case guest cues] |
| UX-04 | a second run on an edited timeline offers a wrong edit | fixed with EX-02 |
| UX-05 | Plan-tab lines read as telemetry | fixed: reasons rewritten, a timecode and a name on every camera line, display names only [N-16: the last leaks] |
| UX-06 | every reel strikes through the opening | fixed: `open_on` proved from the tree the run leaves; whys matched by footprint |
| UX-07 | cancel does nothing; no progress | fixed: backend cancel at every layer boundary, the progress line, a card after a confirmed cancel is dropped |
| UX-08 | focus lost after keyboard Restore; orphan listitems | fixed |
| UX-09 | reel captions: removed filler, late cue, a missing word | partly: no removed filler and no cue past its piece; every word the graph holds is captioned [N-04]; "Because" is never captioned because the recogniser never heard it (the prompted whisper pass, EB2) |
| UX-10 | five whole turns on the wrong camera | fixed with EX-07 |
| UX-11 | unhidden seams and ~1 s holes | fixed: seams as EX-04; longest hole 0.786 s; the length trade is item 3 |
| UX-12 | 22 phrasings: seven wrong or unclear | fixed in two rounds: 21 of 22 right in the verifiers' run, the last [N-06, N-14] now ends in one honest sentence with no intent picker; every card states its length |
| UX-13 | garbled reply and Details text | fixed [N-17: length said twice, step noise, the advisory row] |
| UX-14 | flag-off card calls a reframe a camera | fixed: a derivative of the clip's own upload is a media change |
| UX-15 | the reel keeps the first take of a retake | fixed: the later take stays unless its delivery is more than 0.1 lower, in both recipes |

#### What the re-verifiers found, and the closer's disposition

| Id | Sev | Finding | Disposition |
|---|---|---|---|
| N-01 | high | two cameras and no recorder: `Graph.primary` was the guest camera | fixed: primary is the angle that carries the dialogue; a check blocks a second camera left on v1; real 169 s no-recorder session commits 156.3 s, 7 of 7 checks |
| N-02 | high | the "Wait for it" gate refused as stale once the transcript lands | fixed: the analysis-gate record leaves `has_transcript` out of its facts hash |
| N-03 | med | a keep window starting inside a filler refused the whole 45 s reel | fixed: edges move to a word's far side; the 45-minute recording now commits a 44.8 s reel, 13 of 13 checks |
| N-04 | med | kept words dropped from single-speaker captions in turn gaps | fixed: the under-no-turn skip applies only with more than one speaker; every kept word of TH is in a cue |
| N-05 | med | orphan one-word podcast cues, lost first words, guest lower-case | fixed: first word clamped to its turn, same casing and punctuation for both speakers |
| N-06 | med | UX-12 not closed for the podcast | fixed: root cause was cut edges inside words (53 of 192 plans, now 3); the rollback is a sentence |
| N-07 | med | a silent clip still gets an edit | fixed: a layer with next to no words is empty, recogniser sound annotations are dropped, "no speech" is one sentence |
| N-08 | med | after a cancel the next request joins the cancelled job | fixed |
| N-09 | med | a malformed `brain/versions.json` makes Undo raise | fixed: tolerant readers, shape check at import |
| N-10 | med | the flag-off recipe table still contains `edit` | fixed: hidden from `recipes.cards()` unless the flag is on, pinned against a frozen list |
| N-11 | low | a switch one frame after a cut leaves a one-frame piece | fixed: the cut goes to the switch within two frames; no piece under the minimum |
| N-12 | low | a project below 23.976 fps conformed by the preset lands on half frames | fixed: planned on the preset's grid; the canvas is conformed before the first cut |
| N-13 | low | `speech_preserved` false failure after the "3 minutes" confirmation | fixed: a brain plan does not add the legacy postcondition set; a word lost outside the plan's removals still fails |
| N-14 | low | rollback after "yes" says "I did not offer that plan" | fixed with N-06 |
| N-15 | low | "clean this up" returns a full episode edit | fixed: a clean-up plans removals only (two cameras keep their camera plan and dialogue lane) |
| N-16 | low | internal identifiers and a removed filler in Plan lines | fixed |
| N-17 | low | reply says the length twice, carries step noise | fixed |
| N-18 | low | two mutants of the version-label fix survive | fixed: driven through `_run_and_stream` |
| N-19 | low | two guards loosen with no test failing | fixed: a non-brain tool naming an unoffered on-timeline path is refused; a run removing named + 1 s is blocked |
| N-20 | low | SC-11 not touched; new copies of caps | fixed except the filler lexicon (pinned equal, two literals) |
| N-21 | low | new files over 800; old ones grew | partly: `tests/test_brain_resolve.py` is back under the limit (711); `tests/benchmark/test_eb1_slice.py` stays 1209 lines (one module-scoped app fixture), `service.py` 1334, `executor.py` 1661, `dispatch.py` 9982 and the other files that were over 800 at HEAD; the gate extraction to `brain_gate.py` is a follow-up |
| N-22 | low | leftovers with no caller | fixed: deleted; the `B_LANDED` and `xfail` branches are gone |
| N-23 | low | path scrubs leak some spellings | fixed |
| N-24 | low | flag off is not byte-identical in three places | fixed: the `reorder_clips` History text and the `ingest.json` pending rule are flag-gated and pinned; the five new `TimelineFacts` fields are default-valued and are stated here, not hidden |
| N-25 | low | the owner's workdir was written at 07:11 on 2026-09-30 by an unidentified backend | closed: not a code defect; every lane started its backends with a scratch `WORKDIR`; recorded as unattributed for the owner to confirm it was their own app |

The closer found and fixed four more by running the real sessions end to end: cut edges inside words (above), a prompt turn that raised a validation error when a dropped-question note plus a long brain reply passed the 400-character reply cap, every camera A caption dropped for a session analysed before the angle fix (`angles.json` listed only camera B), and a 45-minute recording listing 40 left-in-place pauses one by one ("Not done" is now one line with a count and a span).

Files still over the 800-line limit and why: `agent/dispatch.py` (about 9.98k), `agent/tools.py`, `agent/prompt/{service,executor,validate,grammar,expanders,planner,verify}.py`, `main.py` and `change_rules.py` were over it at HEAD and the brief's ownership tables forced appends; `tests/benchmark/test_eb1_slice.py` (1209) shares one app fixture. Every function the wave added is at most 50 lines; every new source file is under 725.

---

## 15. Risks and open decisions

### 15.1 Risks

| # | Risk | L / I | Mitigation |
|---|---|---|---|
| 1 | Numpy k-means diarization is weak on real two-mic podcasts (flips after pauses, overlap) | H / H | own-mic energy as a second signal; `flip_risk` spans fall back to the wide; k estimate + panel override; pyannote adapter for anyone with a token; DER gate; `speaker_on_screen` surfaces damage on the card |
| 2 | Haar frontal misses profiles and turned heads → wrong hints, anchors, reframes | H / M | own-mic correlation for hints; scale-only punch-ins; dead-zone follower holds the last good centre; median framing for fast movers; Phase-2 provider swap behind the `faces` capability |
| 3 | `_cut_source_ranges` O(n²) at 450+ cuts is slow in the dry run | M / M | measured in EB-3 on fixture 6; the merged-ranges fast path when > 15 s; `estimated_seconds` includes it so the `go` gate is honest |
| 4 | The 24-step or 4-question cap is hit by shorts with children or revisions | L / M | children compile into their own plans; same-kind sentinels merge; the analysis gate is the first question; the compiler drops optional groups with a note |
| 5 | A model's story draft breaks continuity | M / H | grounding rejects reorders that break `answer_of` or move a non-standalone scene; the contract and blocking checks still run; model-off equivalence |
| 6 | Apply mismatches the preview because a step depends on run-time variance | L / H | every model result frozen in the semantic layer or the pending record; analyses are files; determinism test E12/E13 |
| 7 | Multi-source timelines break consumers that assume "the first v1 clip's transcript" | M / H | `_transcripts_by_source` + per-angle synced transcripts in `ingest.json`; multi-src cases in `test_transcript_timemap.py`; `facts.build_facts` maps every v1 `src` |
| 8 | `open_on` reorders in a way the person did not expect | M / M | only for reels ≤ 60 s with a standalone hook; said first on the plan card; "keep it chronological" revision; `cold_open` alternative |
| 9 | The decision-kind licence produces false `unasked` rollbacks (e.g. `fit_music_to_video` after cuts) | M / M | shadow mode over E1-E9 before enabling; tool side effects pre-declared in the licence table |
| 10 | Keyframed pans read as the reframe but the export's crop-pan differs from the preview | L / H | the engine and export share the geometry goldens (RD2, RBV 18+); E7 decodes the export |
| 11 | Two dry runs + verify render make preview slow on long podcasts | M / M | analysis before planning; verify render skipped > 600 s; round 2 only when a fix exists; `go` gate |
| 12 | Apple Intelligence latency or refusal makes the semantic layer mostly heuristic | M / L | the layer is explicitly `partial`; the card names the writer; heuristics are the goldens' baseline |
| 13 | Real footage differs from lavfi fixtures (noise, crosstalk, camera mics only, disfluencies whisper drops, split files, drift) | H / H | the dialogue source falls back to the loudest angle's own audio; the acoustic filler detector and the prompted pass; per-file sync with drift; **the human-labelled real-footage tier is the Phase-1 EXIT gate (§13.6), not a Phase-2 eval**; every degradation said on the card |
| 14 | Emphasis fires on background noise or music | M / M | z-scores within speaker on speech frames only; `punch_on_emphasis`; cadence caps |
| 15 | Hindi / Hinglish: FM refuses Devanagari, lexicons are English | M / M | `lexicon.py` per language (`hi-Latn` via `ai/romanize`); acoustic-only scores with lower confidence, stated |
| 16 | The Swift helper rebuild slips | M / L | not on the critical path; MLX/heuristic carry waves B-C |
| 17 | Analysis cache grows unbounded | L / L | the LRU class; a session's `brain/` bounded by its graph |
| 18 | `dispatch.py` (~9.5 k lines) line drift across lanes | M / M | one lane per wave touches it; handlers added at the end under a section header; `test_wave3_correctness` guards duplicates |
| 19 | The change card's lines and the Plan tab disagree | L / M | decisions never claim more than the diff shows (E8 asserts every decision's footprint keys are covered by `changes.summarize`) |
| 20 | Keyword highlight (`TextClip.emphasis`) slips | M / M | its own lane EB-11b gating Milestone C; modes render without it; the card says so until it lands; no Phase-1 exit without it |
| 21 | The dialogue lane goes stale after hand edits to v1 (a1 pieces are unlinked by design) | H / M | `facts.dialogue_lane.in_sync` on every prompt run; the Re-sync button; every brain run re-syncs last; the Inspector marks a1 "Dialogue (follows the picture)"; Phase 2 may teach `_follow_pictures` about split pieces |
| 22 | The prompted whisper pass transcribes some words differently from the upload's pass, so the ordinary editor and the brain disagree on a word | M / L | the two passes are distinct cache keys and the brain never overwrites the upload's transcript; captions come from ONE pass (the caption pass); the card names it |
| 23 | Camera-mic muting removes room tone the mix relied on when the recorder is thin | L / M | `controls.dialogue = "camera_mics"` disables the lane; the Reviewer's `audio_quality` compares noise floors and says "recorder is quieter than the room; consider camera microphones" |
| 24 | The real-footage tier's bars are missed on the recipes rung while Apple Intelligence passes them | M / H | the recipes rung is the INBUILT floor and must pass on its own; a model rung may raise the bar, never replace it; the tier reports per rung so the gap is visible |

### 15.2 Open decisions (settle in EB-0/EB-1)

1. **Interview fixture's second voice**: macOS `say` (offline, already the Hindi bench fallback) or pitch-shifted Piper. Default: `say`.
2. **`no_cut_mid_word` as blocking**: it is transcript-measured; the K3 blocking set is EDL-only by rule. The side-effect snapshot makes the transcript available in-batch; if the review disagrees it becomes advisory and the compiler's plan-time constraint (which already exists) is the guarantee.
3. **`cut_source_ranges` fast path**: measure on fixture 6 first; keep the proven loop if ≤ 15 s.
4. **Analysis store location**: `WORKDIR/analysis/` (beside `proxies/`, LRU) vs `platformutil.user_cache_dir()/analysis/`. Default: `WORKDIR/analysis/`, keyed by content as well as identity.
5. **Speaker-name question**: non-blocking with defaults "Host"/"Guest" (chosen) vs blocking. Names editable in the panel; a revision "add name cards" plans them.
6. **Reel `open_on` default**: on for `viral_reel`, off for `clean_professional` and `luxury`; confirm with the owner's taste on the first real footage.
7. **Rail chord**: ⌥E by `code` (`Alt+KeyE`) verified in the packaged WKWebView; fallback ⌥⇧E.
8. **Plan tab as the default tab** for brain runs (proposed yes; Changes first for ordinary prompts).
9. **`TEXT_MAX_TOKENS` 400 for `rank_moments` on MLX**: measure the 7B at 400 on the M4 Max; cap at 300 if a call exceeds 6 s.
10. **Face sampling source**: proxy span packs (`unpack_span` + a one-frame ffmpeg decode) when `index.json` says a span is ready, else the master at 1 Hz.
11. **`brain.enabled` default**: off until EB-17 lands, on at EB-18.
12. **Keyframe export easing**: the edl map says keyframed values export with real easing (RBV v3); the ai-tools map's "linear only" note appears stale; confirm on the keyframe matrix goldens before relying on `ease-out` for punch-ins (the envelope works either way).
13. **Dialogue-lane fallback with no recorder on a single camera**: always lay a1 (uniform seam fades, muting v1) or keep v1 audio and fade the v1 pieces directly (`add_fade` at seams)? Default: always lay a1 — one code path, one check, and the J/L cuts of Phase 2 need the lane anyway; `controls.dialogue = "camera_mics"` is the escape.
14. **Whisper `--prompt` text per language**: the English disfluency prompt is fixed above; the Hindi/Hinglish one ("अं, हम्म, मतलब…") is chosen by EB-6 after measuring on fixture 7 that it does not worsen the 'ॐ'-collapse case (`tests/test_whisper_cpp_no_dtw_collapse.py`).
15. **Anticipation lead**: 3 frames at the project rate (2 at 24 fps, 4 at 60 fps) or a fixed 0.1 s? Default: frames, clamped to 0.15 s, so the switch sits on the grid.
16. **The real-footage raters**: the owner + 2 editors; if two editors are not available before the flip, the owner + 1 editor with the bar "no item rated would-start-over" unchanged.

---
## 16. Critique log

The critique round (one editor's review of revision 1) returned 4 BLOCKING, 17 MAJOR and 2 MINOR findings. Disposition of each:

| # | Severity | Finding (short) | Disposition | Where |
|---|---|---|---|---|
| B1 | BLOCKING | The dialogue sound lane is unspecified; the recorder lands on the music lane and desyncs at the first cut; camera switches switch microphones | **Applied, differently.** The reviewer proposed `cut_source_ranges.tracks: ["v1","a1"]` so both lanes are cut together. That pairs two lanes inside five tools (cuts, splits, reorder, duplicate, move) and breaks the moment `reorder_clips` repacks v1 only (`dispatch.py:2269`: the magnetic repack is main-lane only). Instead `a1` is DERIVED from v1 by one idempotent tool, `sync_dialogue_lane`, as the last stage-2 step: the same invariant (`dialogue_in_sync`, BLOCKING), one code path, and it also carries the 5 ms seam fades B3 asked for. `make_shorts.from_timeline` copies a1 into children | §0.1 #8, §2.4, §4.6.1, §5.3, §5.4, §5.5, §6.1, §7.2, §9.5, §13 |
| B2 | BLOCKING | "Exceptionally good" is not measurable: every case is lavfi/TTS with truths planted by the scoring rules | **Applied.** §13.6 real-footage tier as the Phase-1 EXIT gate with assets, truth, bars and blind rating; EB-2 owns the manifest, EB-18 the gate; G9 and risk 13 rewritten | §1.1 G9, §13.1 #9, §13.3 E19, §13.6, §14 EB-2/EB-18, §15.1 |
| B3 | BLOCKING | Cuts land on whisper token edges with a 0.05 s pad and no seam treatment; the render micro-fades nothing | **Applied.** 100 Hz envelope in the audio layer (`env_10ms`, the `_VAD_FRAME_S` frames), trough snap ±80 ms outside every kept word with the tie rules, 40 ms air floor, filler removal takes the filler and its trailing gap, never the neighbour's tail; 5 ms fades on every dialogue seam; `no_cut_mid_word` tol 0.02 on AUDIO seams; `seam_click` on the render | §3.1, §3.2, §4.6 table, §4.6.2, §6.1, §6.2, §13 |
| B4 | BLOCKING | "Viral" captions have no per-word highlight; `TextClip.emphasis` was allowed to slip | **Applied.** Lane EB-11b (Wave C, gates E3): `TextClip.emphasis` + `CaptionLook.accent`, both renderers + ASS, RBV 31 → 32, karaoke by static cues, keyword accent in Dynamic, goldens | §0.5, §1.2 N6, §4.7 table, §4.7.3, §5.4, §14 EB-11b |
| M1 | MAJOR | Filler detection is transcript-only and whisper drops disfluencies | **Applied.** Acoustic filler detector (`analysis/fillers.py`), the prompted ASR pass as a distinct cache key, token `p` → `prob`; fixture 3 plants `uh`s the ASR drops | §3.2, §3.4, §4.6, §9.1, §13 |
| M2 | MAJOR | Every camera switch is 0.4 s late | **Applied.** Backchannel is a lookahead test (< 0.6 s or lexicon); switches land at the onset minus 3 frames, snapped to the preceding gap; at the onset exactly in a true overlap | §4.3 rule 1 |
| M3 | MAJOR | Jump cuts avoided instead of hidden | **Applied.** Rule 1b `hide_jump_cuts` (angle change AT the seam; scale step 1.0 ↔ 1.08/1.12 on a single camera), `jump_cut_hidden` check; `NEAR_CUT_S` only for non-coincident switches | §4.3 rule 1b/6, §5.3, §6.1 |
| M4 | MAJOR | Punch-in envelope is a sub-second pump and asks for two easings on one property | **Applied.** In at the clause start ≥ 0.8 s before the peak, hold through the sentence, release as a STEP at the next seam (else ≥ 1.2 s ease at a sentence boundary); one `interp` per Keyframe stated and checked | §4.4, §5.3, §6.1 |
| M5 | MAJOR | A looping procedural bed under a whole episode; "Subtle" ducking inaudible | **Applied.** Episodes: intro + outro (+ stings); a bed only when Standard/High is touched; levels relative to measured speech (Subtle −24 LU, duck 6); the card says "built-in bed" | §4.8, §5.3, §8.2, §8.3 |
| M6 | MAJOR | Angle assignment from 1 Hz mouth motion + MFCC k-means flips on same-gender voices | **Applied.** Own-mic correlation primary (r ≥ 0.5, margin ≥ 0.25) → face size → an ≥ 8 Hz mouth probe; margin < 0.3 asks with thumbnails | §3.4, §9.1, §9.5, §13.2 |
| M7 | MAJOR | One file per angle; split recordings become single-camera after 30 min; no drift | **Applied.** `angles.json.members[].files[]` with per-file offsets and `ref_t0/ref_t1`, `angle_gap`, piecewise-linear drift > 40 ms; `apply_camera_plan` picks the file per span | §3.1, §3.2, §3.4, §4.3 output, §5.4, §13.1 #2 |
| M8 | MAJOR | Episode cold open by MOVE tears the answer; no separator; lower thirds at t = 0 | **Applied.** Episodes duplicate only (`cold_open`), the hook card or a 6-frame dip as separator, lower thirds ≥ 3 s after the cold open | §4.1.2 |
| M9 | MAJOR | Interview guest priority deletes good questions | **Applied.** Default removes only `removable`; keeps hook ≥ 0.5 / importance ≥ 0.4 / punched-out / first-of-topic questions; `answers_only` is a style knob; head-marker trim when a question goes | §4.1.3, §8.3 |
| M10 | MAJOR | Every pause is treated as waste | **Applied.** Protected pauses (emotion, laughter, hard question, conclusion) keep max(pad, 45 %, ≤ 1.2 s) with `pause_kept:{why}`; turn-boundary floors by energy; `keep_pause` is a visible decision | §4.6.3, §4.10, §5.2, §5.3, §6.1 |
| M11 | MAJOR | Two faces on a wide framed at the midpoint | **Applied.** Vertical multicam never uses the wide (resets skipped; two-up is Phase 2); a single wide follows the ACTIVE speaker; scale-up before any face is cut | §4.5, §0.5 |
| M12 | MAJOR | Shorts children lose multicam and tighten (one `Clip` per child) | **Applied.** `make_shorts.from_timeline` copies v1 + a1 pieces inside the window; E6 on fixture 2 asserts ≥ 2 sources in a child | §5.3, §5.4, §13.3 E6 |
| M13 | MAJOR | Numbers counted four times in hook/importance | **Applied.** `strong_number` / `weak_number` classes; one term per formula, one axis per number; candidacy and flat-delivery rules; `_sentence_score` stays legacy-only | §3.4, §4.2 |
| M14 | MAJOR | Joins without an antecedent guard outside interviews | **Applied.** One `join_ok` for every mode and every join; `open_on` skipped when the scene after the hook's origin fails it | §4.1.1 step 3/5, §4.1.3 |
| M15 | MAJOR | Nothing protected before the first brain version; "Undo this decision" half-restores | **Applied.** First-run baseline protected set; "Keep my edits / Start from raw" on the gate; undoing a cut re-covers dialogue, captions and moved keys | §7.2, §13.3 E18, §14 EB-15 |
| M16 | MAJOR | Caption words come from `small` with `prob = 1.0`; nothing can be flagged | **Applied.** Caption pass on the best on-disk model, token `p`, "Words to check" on the Plan tab, axis renamed `caption_timing` | §4.7.4, §6.3, §8.2 |
| M17 | MAJOR | `cut_source_ranges ≤ 2,000` truncates long episodes into a tight head and loose tail | **Applied.** Compiler splits into ≤ 2,000-range steps; tool cap → 5,000 with the fast path; shortest silences dropped uniformly with `deferred[]`; `tighten_uniform` check | §4.6.4, §5.3, §5.6, §6.1 |
| m1 | MINOR | Wide resets by timer and reactions by motion steal payoffs | **Applied** (cheap): never cut away over importance ≥ 0.6, the emphasis word or the hook; reactions after laughter or in the low-importance middle; resets hold ≥ 3 s and return to a different angle | §4.3 rules 3-4 |
| m2 | MINOR | The Editing Score is circular and presented as improvement | **Applied in part.** Axes labelled objective vs measured; before/after only for measured; "plan score" wording; `words_to_check` outside the score. The remainder of the finding (its text was cut off in the round) is treated as covered by B2: the independent judgement is the real-footage tier, not the score | §6.3, §13.6 |

Nothing was rejected outright. Two findings were applied differently from the reviewer's wording (B1's lane pairing → a derived lane; m2's missing tail → the real-footage tier), each with its reason above.

---
## Appendix A — a Content Graph scene (from `scenes.json` of the interview fixture)

```json
{"id": "sc_031", "kind": "speech", "t0": 412.84, "t1": 421.30, "spk": "S2", "speaker_name": "Guest", "topic": "t_007",
 "sents": ["s_00088", "s_00089"],
 "text": "And honestly that is where it earns its price: seventy minutes of four K before it shut down.",
 "features": {"wpm": 168, "fillers": 0, "false_start": false, "repeat_of": null, "is_question": false, "answer_of": "sc_029",
              "has_number": true, "claim": true, "conclusion_marker": true, "story_marker": false,
              "rms_z": 0.9, "pitch_range_st": 7.2, "stretch": 1.18, "dead_air_s": 0.0, "laughter": false, "overlap_s": 0.0},
 "shot": {"angles": {"src_9b02…": {"faces": 2, "largest_face": 0.11, "role": "wide"},
                     "src_c77d…": {"faces": 1, "largest_face": 0.34, "role": "close:S1"},
                     "src_e310…": {"faces": 1, "largest_face": 0.31, "role": "close:S2"}},
          "active_angle": "src_e310…", "motion": 0.08, "face_lost_frac": 0.0},
 "quality": {"sharpness": 0.71, "exposure": 0.55, "shake": 0.02, "noise_db": -58.0, "clipping": 0.0},
 "scores": {"hook": 0.74, "importance": 0.81, "humour": 0.05, "virality": 0.62, "emotion": 0.58, "quality": 0.78, "standalone": 0.9, "quotable": 1.0},
 "evidence": {"hook": ["has_number", "conclusion_marker", "rms_z>0.8"], "importance": ["answer", "topic_peak", "claim"]},
 "labels": {"emotion": "emphatic", "function": "conclusion"},
 "by": {"scores": "recipes", "annotations": ["apple_intelligence:rank_moments"]}}
```

The graph header, the speech layer and the semantic layer are shown in §3.2.

## Appendix B — an Edit Decision Plan (the 45 s Reel; abridged)

```json
{"version": 1, "id": "d_5e2c9a17", "planner_version": 1, "created": "2026-09-29T18:40:11Z",
 "graph": {"id": "g_7c1e4b2a9d03", "digest": "sha256:…"},
 "controls": {"content_type": "auto", "energy": 7, "captions": "dynamic", "broll": "low", "sfx": "off", "music": "subtle",
              "duration_s": 45, "platform": "instagram_reels", "ratio": "9:16", "count": 1},
 "style": "viral_reel", "seed": 0, "previous": null, "scope": null, "brain": "recipes", "content_brain": "apple_intelligence",
 "summary": {"project_type": "talking_head", "target": "reel", "duration_s": 44.6,
             "hook": {"sent": "s_00010", "src": "src_a41f…", "t0": 23.37, "t1": 28.47, "quote": "this is where it earns its price"},
             "story": [{"beat": "hook", "sents": ["s_00010"]}, {"beat": "context", "sents": ["s_00014", "s_00018"]},
                       {"beat": "information", "sents": ["s_00022", "s_00024", "s_00028"]}, {"beat": "payoff", "sents": ["s_00032"]}],
             "camera": {"angles": 1, "switches": 0}, "music": {"bed": "upbeat_120bpm", "volume_db": -22, "duck_db": -24},
             "captions": {"mode": "dynamic", "style": "ig_chunky", "position": "bottom", "speakers": false},
             "graphics": {"lower_thirds": 0, "hook_card": true}, "broll_suggestions": 1, "estimated_seconds": 24,
             "deferred": [{"asked": "emojis", "why": "caption emojis arrive in Phase 2"}]},
 "decisions": [
  {"id": "k_0001", "kind": "keep_window", "ref": {"src": "src_a41f…", "t0": 31.05, "t1": 68.92},
   "reason": {"code": "best_window", "facts": ["s_00014", "s_00032", "t_002"], "text": "37.9 s of whole sentences after the hook; topic “the camera”"},
   "score": 0.81, "confidence": 0.86, "optional": false, "by": "recipes"},
  {"id": "k_0002", "kind": "open_on", "ref": {"src": "src_a41f…", "t0": 23.37, "t1": 28.47},
   "reason": {"code": "hook_strongest_opening", "facts": ["s_00010"], "text": "strongest opening: surprise + strong statement + authority (0.83)"},
   "score": 0.83, "confidence": 0.8, "optional": false, "by": "apple_intelligence"},
  {"id": "k_0003", "kind": "cut_range", "ref": {"src": "src_a41f…", "t0": 36.17, "t1": 36.61},
   "reason": {"code": "filler", "facts": ["w_000212"], "text": "filler “um” at 00:00:13;04"}, "score": 1.0, "confidence": 0.95, "optional": false, "by": "recipes"},
  {"id": "k_0004", "kind": "cut_range", "ref": {"src": "src_a41f…", "t0": 46.97, "t1": 48.82},
   "reason": {"code": "silence", "facts": ["sil_0005"], "text": "silence of 1.9 s at 00:00:23;27"}, "score": 1.0, "confidence": 0.95, "optional": false, "by": "recipes"},
  {"id": "k_0007", "kind": "punch_in", "ref": {"src": "src_a41f…", "t0": 26.90, "t1": 28.40},
   "params": {"scale": 1.12, "anchor": [0.50, 0.42], "interp": "ease-out", "beat_aligned": true},
   "reason": {"code": "hook_emphasis", "facts": ["s_00010", "w_000131"], "text": "punch in on the hook statement (1.6σ louder)"},
   "score": 0.86, "confidence": 0.8, "optional": true, "by": "recipes"},
  {"id": "k_0009", "kind": "punch_in", "ref": {"src": "src_a41f…", "t0": 44.80, "t1": 46.90},
   "params": {"scale": 1.10, "anchor": [0.50, 0.42], "interp": "ease-out"},
   "reason": {"code": "emphasis_peak", "facts": ["s_00022", "w_000301"], "text": "speaker delivers a key point (1.9σ louder, 1.15× slower)"},
   "score": 0.79, "confidence": 0.77, "optional": true, "by": "recipes"},
  {"id": "k_0011", "kind": "captions", "params": {"style": "ig_chunky", "position": "bottom", "look": {"size": 72, "upper": true, "stroke_w": 6},
   "emphasis_words": ["w_000131", "w_000301"]},
   "reason": {"code": "caption_mode", "facts": [], "text": "Captions: Dynamic (talking head, energy 7)"}, "score": 1.0, "confidence": 1.0, "optional": false, "by": "recipes"},
  {"id": "k_0012", "kind": "hook_card", "params": {"text": "WHERE IT EARNS ITS PRICE", "duration": 2.5},
   "reason": {"code": "hook_strongest_opening", "facts": ["s_00010"], "text": "quotes the opening statement"}, "score": 0.83, "confidence": 0.8, "optional": false, "by": "apple_intelligence"},
  {"id": "k_0013", "kind": "reframe", "params": {"ratio": "9:16"}, "reason": {"code": "control", "facts": [], "text": "Platform: Instagram Reels"}, "score": 1.0, "confidence": 1.0, "optional": false, "by": "recipes"},
  {"id": "k_0014", "kind": "reframe_pan", "ref": {"src": "src_a41f…", "t0": 31.05, "t1": 68.92}, "params": {"keys": [[0.0, 0.0, 0.0]]},
   "reason": {"code": "subject_follow", "facts": ["face_src_a41f_31.5"], "text": "one face, centred; no pan needed"}, "score": 1.0, "confidence": 0.9, "optional": true, "by": "recipes"},
  {"id": "k_0015", "kind": "music", "params": {"bed": "upbeat_120bpm", "volume_db": -22, "duck_db": -24, "loop": true},
   "reason": {"code": "music_mood", "facts": ["music_hint"], "text": "upbeat bed: talking head, arousal 0.51, Music: Subtle"}, "score": 0.9, "confidence": 0.9, "optional": false, "by": "recipes"},
  {"id": "k_0016", "kind": "broll_suggest", "ref": {"src": "src_a41f…", "t0": 40.52, "t1": 46.97},
   "params": {"query": "battery", "candidates": [{"leaf": "battery_closeup.mp4", "score": 0.58}]},
   "reason": {"code": "broll_reference", "facts": ["s_00022", "src_1ab3…"], "text": "“battery life” during 38 s on one angle; battery_closeup.mp4 matched battery"}, "score": 0.58, "confidence": 0.6, "optional": true, "by": "recipes"},
  {"id": "k_0017", "kind": "export_preset", "params": {"platform": "instagram_reels"}, "reason": {"code": "control", "facts": [], "text": "Platform: Instagram Reels"}, "score": 1.0, "confidence": 1.0, "optional": false, "by": "recipes"}
 ],
 "children": [],
 "compiled": {"plan_id": "p_9a3f01bc", "steps": 16, "sentinels": ["markers", "keep", "cuts", "story_splits", "story_order", "punch_ins", "reframe_pans"]},
 "score": {"before": {"total": 38}, "after": {"total": 79, "story_flow": 76, "hook_strength": 88, "visual_variety": 70, "audio_quality": 80, "caption_timing": 90, "pacing": 82, "brand_compliance": 70}}}
```

The compiled plan (16 steps, stage-sorted; `fit_music_to_video` is folded into `add_music`'s handler behaviour here and `audit_aesthetic` closes every plan):

```json
{"version": 1, "intent": "edit", "title": "45 s reel — viral reel style", "brain": "recipes", "content_brain": "apple_intelligence",
 "steps": [
  {"tool": "add_marker", "args": {"time": "$brain:markers", "plan_ref": "d_5e2c9a17", "label": "B-roll: battery", "color": "#7dd3fc"}, "why": "1 B-roll suggestion", "stage": 0},
  {"tool": "cut_source_ranges", "args": {"track": "v1", "ranges": "$brain:keep", "plan_ref": "d_5e2c9a17"}, "why": "keep the hook and the best 37.9 s of whole sentences", "stage": 2},
  {"tool": "cut_source_ranges", "args": {"track": "v1", "ranges": "$brain:cuts", "plan_ref": "d_5e2c9a17"}, "why": "3 fillers, 2 pauses", "stage": 2},
  {"tool": "split_at", "args": {"track": "v1", "time": "$brain:story_splits", "plan_ref": "d_5e2c9a17"}, "why": "isolate the hook statement", "stage": 2},
  {"tool": "reorder_clips", "args": {"track": "v1", "order": "$brain:story_order", "plan_ref": "d_5e2c9a17"}, "why": "open on “this is where it earns its price”", "stage": 2},
  {"tool": "sync_dialogue_lane", "args": {"src": "<upload>/talking_head.mp4", "lane": "a1", "offsets": {"<upload>/talking_head.mp4": 0.0}, "seam_fade_s": 0.005, "mute_camera_mics": true}, "why": "dialogue on its own lane; 5 ms fades at 6 seams", "stage": 2},
  {"tool": "add_keyframe", "args": {"clip_id": "$brain:punch_ins", "plan_ref": "d_5e2c9a17"}, "why": "2 punch-ins (clause start → step release at the next seam) and 4 jump-cut hides", "stage": 4, "optional": true},
  {"tool": "auto_reframe", "args": {"ratio": "9:16", "subject_track": false}, "why": "vertical canvas, no re-encode", "stage": 6},
  {"tool": "set_clip_fit", "args": {"clip_id": "$v1_all", "fit": "cover"}, "why": "fill the frame", "stage": 6},
  {"tool": "add_keyframe", "args": {"clip_id": "$brain:reframe_pans", "plan_ref": "d_5e2c9a17"}, "why": "centre on the face", "stage": 6, "optional": true},
  {"tool": "add_caption_track", "args": {"style": "ig_chunky", "position": "bottom", "emphasis_words": ["w_000131", "w_000301"]}, "why": "Captions: Dynamic, keyword accent", "stage": 7},
  {"tool": "set_caption_style", "args": {"size": 72, "upper": true, "stroke_w": 6, "accent": "#ffd166"}, "why": "reel look", "stage": 7},
  {"tool": "apply_hook_stack", "args": {"text": "WHERE IT EARNS ITS PRICE", "duration": 2.5, "visual": "none", "audio": "fade_boost"}, "why": "hook card over the opening statement", "stage": 8},
  {"tool": "add_music", "args": {"src": "<bundled>/upbeat_120bpm.wav", "volume_db": -22, "duck": true, "loop": true}, "why": "subtle upbeat bed", "stage": 9},
  {"tool": "set_duck", "args": {"track": "music", "enabled": true, "to_db": -24}, "why": "duck under speech", "stage": 9},
  {"tool": "apply_export_preset", "args": {"platform": "instagram_reels"}, "why": "platform preset", "stage": 11},
  {"tool": "audit_aesthetic", "args": {}, "why": "quality gate", "stage": 12}
 ],
 "postconditions": [{"check": "reel_duration_within", "args": {"target_s": 45, "tol_s": 1.0}}, {"check": "hook_is_strongest", "args": {"delta": 0.05}},
                    {"check": "no_cut_mid_word", "args": {"tol": 0.02}}, {"check": "dialogue_in_sync", "args": {}}, {"check": "captions_cover", "args": {"min_share": 0.9}}, {"check": "music_ducked", "args": {}},
                    {"check": "punch_in_gap_geq", "args": {"gap_s": 6}}, {"check": "reframe_pan_speed_leq", "args": {"widths_per_s": 0.15}},
                    {"check": "no_letterbox", "args": {}}, {"check": "editing_score_geq", "args": {"total": 70}}, {"check": "audit_ok", "args": {}}],
 "needs_input": [], "confidence": 0.86,
 "reply": "Opens on “this is where it earns its price” (0:23), keeps 44.6 s of whole sentences, two punch-ins, dynamic captions, subtle upbeat bed. One B-roll suggestion is marked at 0:18."}
```

(17 steps, under the 24-step cap with room for a `set_loudness_target` or a second sentinel group. Revision 2 added the `sync_dialogue_lane` step and the caption accent; the step count in the `compiled` block above reads 16 from revision 1 and is 17 here.)

## Appendix C — a review report (`<session>/brain/reviews/d_5e2c9a17.json`)

```json
{"edp": "d_5e2c9a17", "round": 0, "after_hash": "0c77a1…", "render": {"used": true, "duration_s": 44.6},
 "issues": [
  {"code": "awkward_silence", "severity": "medium", "at": {"src": "src_a41f…", "t": 61.43}, "evidence": {"air_before_speech_s": 0.11},
   "fix": {"kind": "cut_range", "ref": {"src": "src_a41f…", "t0": 61.43, "t1": 61.60}, "reason": {"code": "review_fix:awkward_silence", "facts": ["sil_0009"], "text": "fixed after review: 0.17 s of extra air before “If you shoot…”"}}},
  {"code": "music_too_loud", "severity": "low", "at": null, "evidence": {"speech_lufs": -18.2, "music_under_speech_lufs": -23.1, "separation_lu": 4.9},
   "fix": {"kind": "music", "params": {"duck_db": -28}, "reason": {"code": "review_fix:music_too_loud", "facts": [], "text": "fixed after review: ducked 4 dB deeper"}}},
  {"code": "caption_face_overlap", "severity": "low", "at": {"src": "src_a41f…", "t": 31.5}, "evidence": {"overlap_frac": 0.0}, "fix": null}
 ],
 "score": {"total": 74, "story_flow": 76, "hook_strength": 88, "visual_variety": 70, "audio_quality": 66, "caption_timing": 90, "pacing": 78, "brand_compliance": 70},
 "checks": [{"check": "no_cut_mid_word", "passed": true}, {"check": "reel_duration_within", "passed": true, "measured": 44.6}, {"check": "hook_is_strongest", "passed": true},
            {"check": "punch_on_emphasis", "passed": true, "measured": 1.0}, {"check": "captions_cover", "passed": true, "measured": 0.97}],
 "revision": {"applied": true, "next": "d_5e2c9a18", "fixes": 2, "score_after": 79}}
```

## Appendix D — a Style Profile (`brain/styles/viral_reel.json`)

```json
{"id": "viral_reel", "name": "Viral Reel", "version": 1, "based_on": null, "content_types": ["talking_head", "podcast", "interview"],
 "pacing": {"energy": 8, "target_shot_s": 2.2, "min_shot_s": 0.8, "silence_min_dur_s": 0.35, "silence_keep_pad_s": 0.06, "fillers": "strict", "false_starts": true, "repeats": true, "dead_air_max_s": 0.6},
 "story": {"template": "hook_context_information_payoff", "cold_open": "open_on", "cold_open_max_s": 3, "reorder": "hook_only"},
 "camera": {"mode": "speaker_led", "reset_wide_every_s": 14, "max_switches_per_min": 12, "min_shot_s": 1.2, "switch_cost": 0.15, "reactions": true},
 "punch_ins": {"per_min": 2.0, "scale": 1.15, "ease": "ease-out", "hold_s": 1.8, "never_within_s_of_switch": 1.0, "question_punch_out": true},
 "captions": {"mode": "viral", "position": "center", "speaker_colours": true},
 "music": {"level": "high_energy", "mood": "upbeat", "volume_db": -12, "duck_to_db": -16, "beat_align": true, "beat_pulse_on_broll": true},
 "transitions": {"look": "none"}, "broll": {"level": "medium"}, "sfx": {"level": "off"},
 "reframe": {"vertical": "keyframed"}, "brand": {"use_brand_kit": true, "lower_thirds": "guest_only"},
 "deferred_ok": ["emojis", "memes", "pattern_interrupts", "sfx"]}
```

## Appendix E — three worked examples from brief §3

### E.1 "Make this into an engaging 45-second Instagram Reel." (talking head, one camera, 16:9 source)

1. **Read**: grammar → `edit` (composite family) with `platform = instagram_reels`, `duration_s = 45`; `ui_state.brain_controls` (panel defaults: energy 7 from `viral_reel`, captions Dynamic, music Subtle). Graph present (the upload's background transcript + cheap layers ran at ingest; the visual layer is lazy).
2. **Plan** (pure): `classify` → `talking_head`; `tighten` at energy 7 → 3 fillers, 2 pauses, no repeats; `select` → the best 45 s window by the reel scorer; `story` → the Hook Engine's top scene (#10, `standalone 0.9`) is outside the first 8 s of the window → `open_on`; `emphasis` → the hook sentence and one emphasis peak; `captions` → Dynamic; `graphics_min` → `hook_card` from `hook_candidates` (Apple Intelligence, 1.4 s, frozen); `music` → upbeat bed, Subtle; `reframe` → 9:16, one face, centred (one key); `broll` → one suggestion (bin match 0.58); `finish` → export preset, audit. EDP `d_5e2c9a17` written (Appendix B).
3. **Compile** → 17 steps (Appendix B), 11 postconditions.
4. **Dry run**: `$brain:keep` fans to one `cut_source_ranges` call (2 ranges: the tail then the head); `$brain:cuts` → 5 ranges; `$brain:story_splits` → 2 split times mapped through `timemap` (the hook sentence sits inside the kept window); `$brain:story_order` resolved after the splits → the hook piece first; `$brain:punch_ins` → 2 punch-ins × 4 keys; captions; hook card; music. Reviewer: two fixes (Appendix C) → ONE revision round → score 74 → 79.
5. **Card**: Plan tab — "Talking head → 45 s Reel · opens on “this is where it earns its price” (0:23) · 5 cuts · 2 punch-ins · Dynamic captions · subtle upbeat bed · 1 B-roll suggestion · Score 79 (was 38) · Not done this time: emojis (Phase 2)"; Changes tab — the diff lines with a "why" each. Apply → one op → **V1 Reel**. Reply as §8.5 step 7.
6. Later, "make the first 10 seconds faster": `revise` at energy 10 over reference seconds mapping to the first 10 s → 2 more `cut_range` + 1 punch-in; the protected set is empty; delta EDP `previous = d_5e2c9a18`; one op; V2.

### E.2 "Edit this podcast like a premium business podcast. Remove boring parts, use multicam intelligently, add captions and make 8 viral clips." (3 cameras + recorder, 41 min)

1. **Read**: `edit` with `style = premium_podcast`, `count = 8`, captions on. Graph: the recorder WAV is the reference; angles A/B/C synced (+0.342 / −0.118 / +0.905 s, confidence 0.9); speakers k = 2, S1 host (38 %, 41 questions), S2 guest; angle hints B→S1, C→S2, A→wide; 312 scenes, 23 topics; semantic layer `partial` past 42:10 (FM budget) — said on the card.
2. **Plan**: `classify` → `podcast` (balanced shares) — the family word confirms; `tighten` at energy 4 → 38 removals (22 silences ≥ 0.8 s, 9 fillers, 4 false starts, 1 repeat, 1 dead-conversation topic of 1:30 "scheduling small talk", 1 technical); `story` (episode) → cold open = the quotable line at 12:41 ("We lost eight million before we noticed", quotable 1.0, standalone 0.92) at energy ≥ 5? energy is 4 → `title_only`: a `hook_card` over the first kept sentence, no footage move (the card says which); chapters = 23 topic markers; `camera` → the DP over 284 turns with premium constants (min shot 2.5 s, max hold 25 s → wide reset, switch cost 0.35, reactions on): 121 switches incl. 9 wide resets and 3 listener reactions, every switch snapped to a word gap, 4 placed AT jump cuts; `emphasis` → 6 punch-ins (one per ≥ 20 s, top importance, none within 1 s of a switch); `captions` → Podcast mode with speaker colours (turns in the args, mapped through `timemap`); `graphics_min` → 2 lower thirds at each speaker's first kept turn (names from the panel); `music` → cinematic bed −22 / −24; `broll` → 4 suggestions from `uploads/broll/`; `finish` → loudness −16, audit. **Children**: 8 windows of 25-60 s by the reel scorer with the no-repeat rule (≤ 20 % sentence overlap), each opening on a `hook ≥ 0.7` sentence, dedup against the episode's hook; each child EDP = a reel plan (`open_on` when standalone, punch-in, Dynamic captions, 9:16 keyframed, upbeat bed, hook card).
3. **Compile** (16 steps): `add_marker $brain:markers` (chapters + B-roll) · `cut_source_ranges $brain:cuts` · `apply_camera_plan $brain:camera` (121 speaker switches + 17 `at_cut` switches hiding every tighten seam ≥ 0.4 s; camera B's two files picked per span) · `sync_dialogue_lane {src: zoom_recorder.wav, offsets: {A: 0.342, B1: −0.118, B2: −1800.4, C: 0.905}}` (the recorder off the Music lane and onto a1, camera mics muted, 176 seams faded) · `make_shorts {from_timeline: true, windows: 8, save_as_sessions: true}` (stage 3; `TERMINAL_RECIPES` at `recipes.py:231` ends a parent plan only for the `shorts` CARD — the `edit` card is one recipe, so the compiled plan continues into the later stages on the parent, and `_finish_children` still runs after the parent's commit as today) · `add_keyframe $brain:punch_ins` · `add_caption_track {default, max_chars 32, speakers: […]}` · `set_caption_style {size 56, background, speaker_colors}` · `add_lower_third $brain:lower_thirds` (≥ 3 s after the cold open) · `apply_hook_stack` · `add_music {intro ≤ 12 s, fade_out 1.5}` · `add_music {start: $brain:outro_start, duration 8}` · `set_duck` · `set_loudness_target` · `audit_aesthetic`.
4. **Dry run** (≈ 50 s on the M4 Max): 38 cuts in one call, every edge on a trough; 138 switches → 276 splits + 138 swaps inside one batch, never clearing v1; the dialogue lane rebuilt (≈ 180 a1 clips); keyframes; captions from the recorder's transcript through a1 (≈ 25 s); `make_shorts` with `save_as_sessions=False` in the dry run as today. Reviewer: `speaker_on_screen` 0.93, `dialogue_in_sync` true, `jump_cut_hidden` 1.0, `camera_switch_rate_leq` 3.4/min, one `audio_jump` → pad grown to the next trough; one revision round; score 71 → 76 (measured sub-total 74 → 78).
5. **Card**: "Podcast · 2 speakers (Priya, Arjun) · 3 angles (B in 2 files, drift corrected) · dialogue from zoom_recorder.wav · 41:20 → 34:50 · HOOK title only (energy 4) · CUTS 38 (4:12) grouped by reason; 3 pauses kept (after the laugh, before two hard answers) · CAMERA 138 switches, speaker-led and anticipating, 17 on jump cuts, 9 wide resets, 3 reactions, no shot under 2.5 s · PUNCH-INS 6 · CAPTIONS Podcast, speaker colours, 14 words to check · MUSIC Subtle: intro + outro, no bed · B-ROLL 4 suggestions, none placed · 8 clips will be made as new projects · Plan score 76 (measured 78)". Apply → one op on the parent → **V1 Premium Podcast**; `_finish_children` runs each child's compiled sub-plan, one commit per child, each child records its `v_1`.
6. Later, "keep Arjun on screen longer": `revise` → `camera` with `MIN_SHOT_S[S2] × 1.6` over the affected spans → `apply_camera_plan` of 31 spans; the diff's only category is v1 `src` on camera-plan pieces (licensed by the delta); a lower third the person had dragged since V1 is in the protected set and untouched; E9 asserts the guest share rises ≥ 15 points and nothing else changes.

### E.3 The interview ("cut this like an interview", host + guest, single wide camera, 28 min)

1. **Read**: `edit` with `content_type = interview` (the family word); Graph: one angle (no camera pass), k = 2, S1 host with question share 0.31, S2 guest with answer share 0.67; `content_type.guess = interview (0.82)`.
2. **Plan**: `tighten` at energy 5 → silences, fillers (two of them heard, not transcribed), 1 false start, 1 repeat; the pause after the guest's emotional line kept at 45 % (`pause_kept:emotion`); `story` (interview, `questions: keep`) → 3 weak questions removed with their run-up pauses (their answers open standalone and pass the antecedent guard), the 4th weak question KEPT because its answer starts "It was, yes — …" (`standalone 0.4`), the two well-asked questions kept (`question_kept:hook`); the key statement (top `importance × quotable`) gets a punch-in and, at energy 5 with `standalone 0.9`, is the cold open (`cold_open`: duplicate ≤ 10 s at the front, the hook card as the separator, chronology resumes); `emphasis` → 4 punch-ins from their clause starts incl. one question punch-out on the host's longest question; single camera, so every tighten seam ≥ 0.4 s gets a `jump_cut_hide` scale step; `captions` → Podcast mode, speaker colours, 9 words to check; `graphics_min` → guest lower third at the guest's first kept answer ≥ 3 s after the cold open, host lower third at the host's first kept turn; `music` → cinematic intro + outro (Subtle, episode); `broll` → 2 suggestions; `dialogue` → the camera's own file on a1 (one microphone anyway; the lane brings the seam fades); `finish`.
3. **Compile** (15 steps): `add_marker` · `cut_source_ranges $brain:cuts` · `split_at $brain:story_splits` · `duplicate_clip $brain:story_dup` · `move_clip $brain:story_move {new_start: 0, close_gap: true}` · `sync_dialogue_lane` · `add_keyframe $brain:punch_ins` · `add_caption_track {speakers}` · `set_caption_style {speaker_colors}` · `add_lower_third $brain:lower_thirds` · `apply_hook_stack` · `add_music {intro}` · `add_music {outro}` · `set_duck` · `audit_aesthetic`.
4. **Dry run**: the three story steps resolve one after another against the live tree (splits → the clip with the key statement's span → its duplicate, the one with the greatest `start`, moved to 0); `no_cut_mid_word` holds; Reviewer finds nothing high-confidence; score 73.
5. **Card**: "Interview · Host and Guest · 28:04 → 24:31 · opens on “{key statement}” then returns to the start · 3 weak questions removed (their answers stand alone); 1 kept because its answer needs it · 1 repeated answer, 1 false start · 4 punch-ins · captions with speaker colours · 2 name straps · Subtle music · Score 73 (was 40)". Apply → **V1 Interview**.
6. Later, "remove the joke": `revise` → the one kept scene with `humour ≥ 0.6` (the planted laugh) → one `cut_range{user_named}`; the contract's licence is the previous footprint plus this delta; nothing else changes; V2.

---

*End of specification.*
