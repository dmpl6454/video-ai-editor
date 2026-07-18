# CapCut Feature Gap Analysis — Verification & Closure Plan

**Date:** 2026-07-17
**Status:** Analysis complete; implementation not started
**Method:** 52 independent verification agents (31 gap claims + 21 parity/beyond claims) against the
codebase at `9969af2`, with adversarial re-checks on every non-obvious verdict, plus direct
confirmation by hand of every claim that drives a work item below.

---

## 1. Verdict on the source document

The "Feature Gap Analysis: AI Video Editor vs CapCut" document is **substantially accurate on the
gaps** (25 of 31 gap claims confirmed) but **materially over-claims parity**, and it misses the
project's single largest real gap versus CapCut, which is not a missing feature at all (§3).

### 1.1 Scorecard

| Axis | Claims | Result |
|---|---|---|
| Claimed gaps | 31 | **25 confirmed absent**, 4 partial, **2 actually present (doc wrong)** |
| Claimed parity | 17 | **9 working**, 7 work-only-with-optional-dep, **1 broken claim** |
| Claimed "beyond CapCut" | 4 | 4 working (2 with caveats) |

### 1.2 Where the document is wrong

| # | Doc claim | Reality | Evidence |
|---|---|---|---|
| 1 | **Frame-accurate scrub / frame-by-frame stepping** is a *gap* | **Present and complete.** `FrameScrubber.tsx` is a 411-line WebCodecs + mp4box.js pipeline (demux → bisect to keyframe → `VideoDecoder` → exact-frame canvas paint) with a hidden-`<video>` fallback. `frameBack`/`frameForward` are bound in **all three** keymap presets (CapCut/Premiere/FCP). | `frontend/src/components/FrameScrubber.tsx`; `keymap/commands.ts:49-52`; `keymap/presets.ts:24-27,64-67,99-102`; `Preview.tsx:179-189` |
| 2 | **Curated sticker/element library** is a *gap* ("project has a sticker layer, but not a browsable library") | **Present.** `StickerPanel.tsx` is a real browsable 7-category gallery (Faces/Hands/Hearts/Symbols/Fashion/Food/Animals) with a localStorage "Recent" row, click-to-insert, drag-to-timeline, and custom PNG upload. Only a *remote/third-party marketplace* is absent. | `StickerPanel.tsx:5-15,61-79,132-161`; mounted at `MediaBin.tsx:141`; drop at `Timeline.tsx:977` |
| 3 | **"Speed ramp"** listed under *parity: already covered* | **Over-claim.** Only a **constant scalar** speed factor exists — `set_speed`'s own docstring says *"M3 stores it as a scalar; full speed-curve support comes later."* No rate-varies-across-clip ramp exists. The doc is right that a *speed-curve editor* is a gap; it should not simultaneously claim "speed ramp" as parity. | `dispatch.py:1216` (`set_speed`), scalar `Clip.speed` |
| 4 | 7 parity items presented unqualified | **Work only with an optional, unbundled dependency**, and **all are excluded from the packaged `.app`**: auto-captions, background removal, upscaling, stabilization, object erase, speaker diarization, multicam. A user of the shipped app does not have these. | `build_app.sh` excludes torch/pyannote/faster-whisper/librosa/demucs; `realesrgan`/`whisper-cli` never auto-download |

### 1.3 Where the document is right

25 gaps confirmed genuinely absent: built-in music library, SFX library, stock video/photo library,
smart-movie auto-edit, split-screen/collage templates, text-to-image, text-to-video, AI avatar,
image outpainting, AI background music, voice cloning, sky replacement, AI style/cartoon filters,
face/body reshape + beauty filters, voice changer/pitch-shift, custom speed-curve editor,
freeze-frame, doodle/pen annotation, in-app camera recording, in-app screen recording, cloud sync,
team collaboration, direct social publish, GIF export, green-screen background library.

Four are "partial" — primitives exist, the feature does not: community template marketplace (a
local template system exists with **zero frontend UI**), text animation preset packs (**dead
scaffolding**, §2.1), cloud draft backup (local `.vae` save/load exists; cloud absent *by design*),
batch export (single-export only; no loop).

---

## 2. Findings the document does not contain

These are the highest-value discoveries. **None are feature gaps — they are broken promises**:
code that accepts an input, stores it, and silently does nothing with it. This is worse than a
missing feature, because the UI/tool schema advertises it as working.

### 2.1 Dead fields — accepted, persisted, never read (all confirmed by hand)

| # | Field | Symptom | Evidence |
|---|---|---|---|
| 1 | `TextClip.anim_in` / `anim_out` | `add_text` documents *"pop, fade, slide_up, slide_down"*. **No renderer reads either field.** Setting `anim_in="pop"` produces zero motion. Worse: the **shipped `countdown_3_2_1` text template sets `anim_in:"pop", anim_out:"fade"`** — a bundled preset promising animation that never renders. | Only 5 refs repo-wide: `schema.py:99-100` (def), `dispatch.py:2530` (docstring), `:2552` (write), `:2604` (preset value). Zero reads in `render/` or `frontend/`. |
| 2 | `apply_lut` `intensity` | **Both branches of the intensity check return the identical string.** A LUT at `intensity=0.2` renders at full strength. The code comments admit it: *"Cleaner: just apply at full strength for v1."* | `render/effects.py:89-103` — `if intensity >= 0.999: return f"lut3d={src_arg}"` … `return f"lut3d={src_arg}"` |
| 3 | `BrandKit.end_card`, `BrandKit.palette`, `BrandKit.font` | `apply_brand_kit` **writes** all three into the EDL; **nothing reads them**. A user asking for a brand end-card image, colour palette, or brand font gets a silent no-op. | Written at `dispatch.py:961-962`; **0 read sites** in `render/` + `show/brand_kit.py` for `end_card`/`palette`; no `brand.font` read site anywhere. |
| 4 | `TextClip.speaker` | `diarize` produces speaker turns; **nothing consumes them**. Diarization never touches the EDL and never renders. | No `.speaker` reads in `render/` or `frontend/`. |
| 5 | `list_luts` | Always returns `{"luts": []}` — **`presets/luts/` does not exist** (only `presets/shows`). The LUT feature is unreachable out of the box. | `dispatch.py:2679`; `ls presets/` → `shows` only |

### 2.2 The reachability crisis — the real gap vs CapCut

**93 tools exist in `DISPATCH`. Only 50 are advertised to the chat LLM. Most have no UI at all.**

Verified not advertised in `tools.py` (invisible to Claude): `chroma_key`, `set_speed`,
`add_sticker`, `stabilize`, `remove_background`, `object_erase`, `motion_track`, `multicam`,
`diarize`, `list_transitions`, `apply_text_template`, `add_text`, `list_text_styles`, `find_broll`.

Consequences confirmed by the verification pass:

- **Transitions**: fully working, renders 29 verified transition types — but has **no frontend UI whatsoever**, and `tools.py:319` hardcodes a **12-value enum while the catalog has 45+**. `list_transitions` is not advertised, so Claude *cannot discover* `slideleft`/`zoomin`/`glitch`/`spin` and will realistically never emit them. An unknown name silently resolves to `fade`.
- **Chroma key**: renders correctly (verified by pixel diff) — **zero user-reachable surface**. Not in the UI, not advertised to chat. MCP-only.
- **Auto-captions**: works — but there is **no caption button anywhere in the UI**. Chat/MCP-only.
- **`add_sticker` not advertised** → "add a fire emoji at 3s" via chat **cannot work**, despite the sticker panel existing.
- Also UI-less: background removal, object erase, stabilization, motion tracking, multicam, diarization, upscaling, auto-reframe, beat-sync cutting, aesthetic scoring, LUTs.

**This is the finding that reframes the document.** The project already has ~93 working tools; CapCut's
practical advantage is not that it has more features, but that a user can *find and click* its
features. Closing the reachability gap converts existing, tested, working code into product — at a
fraction of the cost of building any item on the doc's gap list.

### 2.3 A claim I disproved

One verification agent asserted `SYSTEM_PROMPT` tells Claude *"You have the full editor: …
background removal, object erase …"*, implying Claude is told it has tools it cannot call. **This is
false** — I grepped for it: the phrasing exists nowhere, and the actual prompt says the opposite
(*"The tools available are listed in this conversation… Do not invent tools."*). Recorded here so it
does not get laundered into a work item. `system_prompt.py` is correct as written.

---

## 3. Feasibility ground truth

Verified against the local toolchain (`ffmpeg-full` 8.1.2 — the build CLAUDE.md instructs users to
install). **These are not assumptions; each filter was probed:**

| Capability | Filter/device | Status | Unlocks |
|---|---|---|---|
| Pitch shift / voice changer | `librubberband` | ✅ **enabled** | Voice changer — a direct filter, not a research project |
| GIF export | `palettegen`, `paletteuse` | ✅ | GIF export |
| Split-screen / collage | `hstack`, `vstack`, `xstack` | ✅ | Layout templates |
| Screen recording | avfoundation **`[1] Capture screen 0`** | ✅ present | In-app screen capture |
| Camera recording | avfoundation **`[0] FaceTime HD Camera`** | ✅ present | In-app camera capture |
| Speed ramp w/ interpolation | `minterpolate` | ✅ | Real speed curves without RIFE |
| Beauty / skin smoothing | `smartblur`, `gblur`, `unsharp` | ✅ | First-pass beauty filter |

**The camera/screen recording finding is significant**: `desktop.py`'s existing `_Api.vo_start`/
`vo_stop` voiceover bridge (`ffmpeg -f avfoundation` + app-process TCC authorization + POST to a
`/vo_record`-style endpoint) is *already the exact pattern* these need. The hard part (TCC
attribution under an ad-hoc-signed bundle — see CLAUDE.md's three-layer VO note) is **already solved
and shipped**; camera/screen capture is largely a re-application of it with a video device index and
an `NSCameraUsageDescription` / screen-recording entitlement.

**Doc-accuracy nit:** this ffmpeg build has `--enable-libass --enable-libfreetype`, contradicting
CLAUDE.md's *"Brew ffmpeg 8 lacks libass/libfreetype"*. The Pillow rasterization path should stay
regardless (portability across plain-`ffmpeg` installs), but the claim is stale.

---

## 4. Implementation plan

Ordered by **value ÷ cost**, not by the document's categories. Tier 0 and Tier 1 deliver more
user-visible capability than Tiers 2–3 combined, at a small fraction of the effort.

### Tier 0 — Truth-in-advertising (fix broken promises)

> *Nothing here adds a feature; each item removes a lie. Small, self-contained, individually
> testable. Do these first — shipping a feature list containing silent no-ops is the worst
> failure mode, and it's the one this project currently has.*

| # | Task | Approach | Est. |
|---|---|---|---|
| 0.1 | `anim_in`/`anim_out` dead | **Decide: implement or delete.** Implement = keyframed opacity/position presets in `text_overlay.py` (the `anim_text` geq path already animates opacity — extend it) + `TextLayer.tsx` for preview parity. Delete = drop fields from `schema.py`, `add_text`, and fix `countdown_3_2_1`. **Recommend implement** — `pop`/`fade`/`slide_*` are 4 presets over machinery that already exists. | M |
| 0.2 | `apply_lut` `intensity` dead | Implement the documented `split`+`blend` chain in `_lut()`, **or** reject `intensity < 1.0` with a clear error. Never accept-and-ignore. | S |
| 0.3 | `BrandKit.end_card`/`palette`/`font` dead | Implement `end_card` as an image clip appended at timeline end (reuse the sticker/PNG overlay path); wire `palette`→text style defaults and `font`→role-font override in `ROLE_STYLES`. Or remove from schema + tool. | M |
| 0.4 | `list_luts` returns `[]` | Ship a small bundled `presets/luts/*.cube` set, and surface the LUT picker. Without this, `apply_lut` is unreachable out of the box. | S |
| 0.5 | `TextClip.speaker` dead | Wire `diarize` → caption speaker labels (the field exists for exactly this), or remove the field. | M |
| 0.6 | Transition enum truncated | Generate `tools.py`'s transition enum **from `transitions.all_names()`** instead of hardcoding 12. Stop silently resolving unknown names to `fade` — raise. | S |

**Guardrail:** add a test that fails when a schema field has no reader — the class of bug in 0.1–0.5
recurs precisely because nothing detects it. A test asserting every `Effect`/`BrandKit`/`TextClip`
field is referenced outside `schema.py` would have caught all five.

### Tier 1 — Reachability (convert existing code into product)

> *~43 working tools are invisible. This is the cheapest capability-per-hour in the project.*

| # | Task | Approach | Est. |
|---|---|---|---|
| 1.1 | Advertise missing tools to chat | Add `_t(...)` schemas to `ALL_TOOLS` for `chroma_key`, `set_speed`, `add_sticker`, `stabilize`, `remove_background`, `object_erase`, `motion_track`, `multicam`, `diarize`, `upscale`, `list_transitions`, `find_broll`, `add_text`, `apply_text_template`. **`add_sticker` and `set_speed` are the most user-visible misses.** Note CLAUDE.md's warning: name strings must match `DISPATCH` by hand — nothing enforces it. | M |
| 1.2 | Transitions UI | Timeline affordance at clip boundaries (drag/right-click → picker over the real 45+ catalog). Highest-impact UI gap: fully working engine, zero surface. | L |
| 1.3 | Effects/Filters panel | Frontend picker over the existing `EFFECT_BUILDERS` registry (`vintage`, `vhs`, `glow`, `grain`, `vignette`, `blur`, `sharpen`, `rgb_split`) + LUT picker (after 0.4). Registry is open `{type, params}` — no schema churn. | M |
| 1.4 | Captions button | A single "Auto-caption" UI control. The feature works; there is no button. | S |
| 1.5 | AI tools panel | Surface background removal, object erase, upscale, stabilize, auto-reframe, multicam, diarize — with **honest dependency state** (see 1.6). | L |
| 1.6 | Dependency/capability probe | `GET /api/capabilities` reporting which optional deps are present, so the UI can disable-with-explanation instead of failing at click time with a 422. Directly addresses the 7 `works_if_dep` over-claims. | M |
| 1.7 | Packaged-app honesty | The `.app` excludes torch/pyannote/faster-whisper/librosa/demucs → 7 advertised features silently absent. Either bundle, or **degrade visibly** via 1.6. Today it's invisible. | M |

### Tier 2 — Genuine gaps, locally feasible (§3-verified)

> *Every item confirmed available in the local ffmpeg. No new model dependencies. All consistent
> with the on-device architecture.*

| # | Feature | Approach | Est. |
|---|---|---|---|
| 2.1 | **GIF export** | `palettegen`/`paletteuse` two-pass. Extend `render_export`'s `container` allowlist (currently `("mp4","mov")`) + `ExportRequest`. Self-contained. | S |
| 2.2 | **Voice changer / pitch shift** | `rubberband` (confirmed enabled) as a new audio effect in `audio_mix.py::_audio_clip_filter` — the same slot as `volume`/`afade`. Fallback: `asetrate`+`aresample`+`atempo`. | S |
| 2.3 | **Freeze-frame** | New EDL concept (still-frame clip or `speed=0` segment); render via `trim`+`tpad`/`loop`. Needs schema + timeline UI. | M |
| 2.4 | **Batch export** | Loop `render_export` over presets; `JOB_MANAGER` + `_EXPORT_PRESETS` (8 presets) already exist. Mostly wiring + UI. | M |
| 2.5 | **Split-screen / collage templates** | `xstack`/`hstack`/`vstack`; PIP primitive (`pip.py`) already proves multi-source compositing. Note `pip.py`'s known scale heuristic (long-edge, `h=-1`, cannot probe source aspect). | M |
| 2.6 | **Green-screen background library** | `chroma_key` already renders; bundle backgrounds + a picker. Mostly assets + UI. Pairs with 1.3. | S |
| 2.7 | **Screen recording** | `ffmpeg -f avfoundation -i "1"` (`Capture screen 0` confirmed). **Clone the `vo_start`/`vo_stop` bridge** — TCC/entitlement pattern already solved. Needs screen-recording entitlement; Windows needs `gdigrab`. | M |
| 2.8 | **Camera recording** | `ffmpeg -f avfoundation -i "0"` (`FaceTime HD Camera` confirmed). Same bridge + `NSCameraUsageDescription`. | M |
| 2.9 | **Speed curve / ramp** | The real gap behind the false "speed ramp" parity claim. `Clip.speed: float` → `KFNum`-style curve; `setpts` with a time expression (`keyframes.to_ffmpeg_expr()` is the precedent); `minterpolate` for smoothness. Audio via segmented `atempo`. Timeline curve editor. **Largest Tier-2 item.** | L |
| 2.10 | **Beauty / skin smoothing** | First pass: `smartblur`/`gblur` + `unsharp` as an effect builder. True face/body reshape needs landmarks (mediapipe is already a core dep) — treat as a follow-on. | M |
| 2.11 | **Doodle / pen annotation** | Canvas draw → PNG → existing sticker overlay path. `StickerLayer.tsx` + `add_sticker` are the template. | M |

### Tier 3 — Model-dependent (feasible, heavy)

Consistent with the local-AI architecture (lazy-import, degrade-don't-crash), but each adds a model
dependency and the packaging problem in 1.7 first.

- **Sky replacement** — segmentation model; `rembg`/`u2net` infrastructure is the precedent.
- **AI style / cartoon / anime filters** — a real model; an `edgedetect`+posterize ffmpeg approximation is a cheap interim.
- **AI-generated background music** — model + licensing questions.
- **Voice cloning** — model + **consent/abuse considerations**; Piper TTS exists, cloning is a different risk class.

### Tier 4 — Out of scope / non-goals (recommend documenting, not building)

These conflict with the project's stated architecture — CLAUDE.md: *"Everything runs on-device; only
Claude API calls leave the machine."* They should be **explicit non-goals**, not backlog debt:

- **Cloud sync / team collaboration / cloud draft backup** — contradicts on-device design. The local `.vae` project file is the intended answer; ship a good import/export story instead.
- **Stock libraries (music / SFX / video / photo)** — a **licensing** problem, not an engineering one. Feasible alternative: a *local folder* library (`find_broll` already does this for b-roll — it just isn't advertised to chat; see 1.1).
- **Text-to-image / text-to-video / AI avatar / outpainting** — external generation services; contradicts local-first and adds material cost/ToS surface.
- **Direct social publish** — OAuth + per-platform ToS + token custody. Export presets already cover the format side.
- **Community template marketplace** — needs hosted infra + moderation. The *local* template system is the on-architecture answer, and it currently has **zero UI** — building that (Tier 1) captures most of the value.

---

## 5. Recommended sequencing

1. **Tier 0** — stop shipping silent no-ops. Small, independently testable, no design debate. Add the dead-field guardrail test so the class of bug cannot recur.
2. **Tier 1.1 + 1.4 + 1.6** — advertise the tools, add the captions button, add the capability probe. Days of work; converts ~43 invisible tools into reachable product and makes the 7 dependency-gated features honest.
3. **Tier 2.1 + 2.2 + 2.6** — GIF export, voice changer, green-screen library. Each is small, ffmpeg-native, verified available.
4. **Tier 1.2 + 1.3** — transitions and effects UI. The largest genuine product wins.
5. **Tier 2.7 + 2.8** — screen/camera capture, reusing the solved VO bridge.
6. **Tier 2.9** — speed curves; the real answer to the false parity claim.
7. Re-evaluate Tier 3 only after 1.7 (packaging) is resolved — adding models to an app that already strips its ML deps compounds the problem.

**Explicitly decide Tier 4 as non-goals** and record it, so the gap document stops being read as a
backlog.

---

## 6. Verification notes

- Every claim driving a work item was confirmed **by hand**, not accepted from a subagent — the reachability counts (93 `DISPATCH` vs 50 `ALL_TOOLS`), the dead-field greps, the `_lut` branch, `BrandKit` write-with-no-read, and every ffmpeg filter/device in §3.
- One subagent claim (`SYSTEM_PROMPT` over-promising) was **investigated and disproved** (§2.3) rather than propagated.
- The parity sweep initially failed wholesale (`effort: xhigh` rejected when thinking is disabled) and was re-run at `effort: high` — 29/29 agents clean. The first run's gap half (31/31) was recovered from `journal.jsonl` rather than re-run.
- Not verified: runtime behaviour on Windows (CI is the source of truth per CLAUDE.md), and whether the 7 dependency-gated features actually run on a machine with the deps installed — only that the code path is real and the deps are absent here.
