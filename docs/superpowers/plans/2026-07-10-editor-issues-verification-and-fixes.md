# AI Editor — Issue Verification & End-to-End Fix Plan

> Source: `AI_Editor_Issues.pages` (Mac testing session, 2026-07-08). Every issue below was verified against the actual source (frontend + backend + agent) by a 9-cluster parallel review with an adversarial re-check on every verdict (62 agents, 0 errors). Verdicts carry `file:line` evidence.
>
> **Goal anchor:** *CapCut-class editor (familiarity + functionality) + Claude agentic editing.* Fixes are prioritized by how far each moves the product toward that goal, not by report order.

---

## TL;DR — the 56 reported symptoms collapse into 7 root causes

| # | Root cause | File(s) | Symptoms it explains |
|---|---|---|---|
| **R1** | **`videoFingerprint` omits speed/effects/transform/audio** → edits commit to the EDL but the preview never re-renders. | `frontend/src/components/Preview.tsx:48-60` | 16.3 speed, 16.4–16.8 color, 16.9 gain, 16.10 fade, 16.11 transform-revert, 20 crop "not apparent" |
| **R2** | **Overlay PNGs written non-atomically + reused via bare `exists()`** → a 0-byte/torn `st_*.png` is fed to ffmpeg `-i` → `Invalid data` → 422 that surfaces as "music/upload failed". | `render/text_overlay.py:187,272,305,332` | 36 music error, 31 (crash variant), 37 "video stopped", the "Upload failed" toast |
| **R3** | **No live timeline/vision context per chat turn + chat history never resets on new upload** → Claude answers from the *previous* video. | `main.py:695`, `agent/loop.py:79-93`, `store.ts:208-243` | 47 "remembers old media", 48 "screen recording, no woman", 51 emoji miscount, 52 "couldn't remove all", 53 obscure |
| **R4** | **Ripple/cut re-times only the video track** → overlays stay at absolute seconds and drift to the end when V1 is shortened. | `dispatch.py:46-54,160-209` | 31 "emoji at end", 32/50 "emojis popped up at end" |
| **R5** | **Timeline `<canvas>` is sized to the viewport, not to content** → nothing to scroll; only zoom works. Playhead is a `pointer-events:none` overlay with no drag path. | `Timeline.tsx:145-165,348-397,558-593` | 21/22/23 no scroll, 24 playhead not draggable, 26/27/28 playback anomalies (partly) |
| **R6** | **Upload never infers a canvas from the source** → landscape footage letterboxed into hardcoded 1080×1920; user cycles aspect presets forever. | `main.py:402-485`, `edl/schema.py:151-153` | 6 inconsistent AR, 7 "distorts" (actually letterboxes), 37 canvas |
| **R7** | **Toolbar is `nowrap; overflow-x:auto` with non-shrinking children; dropdowns render inside `overflow:hidden`** → Export button scrolls off-screen on 13", session menu is "half-cut". | `styles.css:53-68`, `TopBar.tsx:216` | 9/10 export not visible, 11 dropdown half-cut, 8 panels not resizable (sibling) |

Fix R1–R7 and **~40 of the 56 reported symptoms resolve**. The rest are small, independent fixes (below).

---

## Verified classification (53 findings)

**Distribution:** 25 REAL · 14 PARTIAL · 11 INACCURATE · 2 CANNOT_VERIFY · 1 BY_DESIGN. Severity: 3 CRITICAL · 19 HIGH · 24 MEDIUM · 7 LOW.

### CRITICAL (3)

| Issue | Verdict | Root cause (evidence) |
|---|---|---|
| **21/22/23 — Timeline scroll** | REAL | Canvas is sized to the container (`size.w`), draw loop early-outs `if (x>size.w) break` (`Timeline.tsx:165`); wrapper is `overflow:hidden` (`styles.css:308`); `scrollLeft += e.deltaX` is a no-op because content isn't wider than the box and a mouse wheel emits `deltaY`. No true scroll exists. |
| **36 — Sticker-PNG render crash** | REAL | `cache_sticker_pngs` guards reuse with bare `if not dst.exists():` (`text_overlay.py:305`) and `img.save(dst)` is non-atomic (`:332`) → a torn/0-byte `st_*.png` is passed as `-i` (`:396`). 3 real 0-byte `st_*.png` found in the session cache. Raises `RuntimeError`→ `HTTPException(422, render_failed)` (`main.py:556`), caught by `store.ts:262` as an *upload* error toast. |
| **47/48/53 — Stale agent context** | REAL | `main.py:695 history=_load_history(sid)` replays the whole prior conversation; a new upload reuses the same session (`store.ts:208-243`) so `chat.json` still holds the old screen-recording exchange. Nothing injects the current EDL/media/vision per turn (`loop.py:79-93` sends only `SYSTEM_PROMPT + history`). |

### HIGH (19) — condensed

- **16.3 speed / 16.4-16.8 color / 16.9 gain / 16.10 fade / 16.11 transform / 20 crop** → **all R1**: controls dispatch correctly (`Properties.tsx:127,137,141,166,348`; backend handlers all set + commit), but `videoFingerprint` (`Preview.tsx:48-60`) excludes these fields so `renderPreview` never fires. 16.11 additionally clears `liveTransform` on release before any re-render → the CSS preview snaps back ("reverts on release"). 16.4-16.8 also **stack** a new color effect per tweak (`dispatch.py:1086`, acknowledged TODO `Properties.tsx:346`) and `ColorSlider` never seeds from stored state + uses `onMouseUp` not `onPointerUp`.
- **24 — Playhead not draggable** → REAL: single-click seek only in a 24px ruler strip (`Timeline.tsx:348-353`); `onMouseMove` is a no-op (`:395`); playhead overlay is `pointerEvents:'none'` (`:593`); `mouseup` bound to canvas not window so drags that leave the canvas silently fail.
- **26 — "plays where no clip exists"** → REAL: compositor concatenates V1 gaplessly (`compositor.py:184,387`) but the rAF clock sweeps full `edl.duration` incl. text/sticker end (`schema.py:208-217`, `Preview.tsx:197`).
- **27 — plays backward after add** → REAL: `previewHash` change reloads `<video>` → `currentTime` snaps to 0; the clock follows the media clock in *either* direction (`Preview.tsx:186-189`) and `onTimeUpdate` also writes `setPlayhead` → playhead yanked backward.
- **31/32/50 — emojis drift to end** → **R4**: `cut_range`/`ripple_delete`/`trim_clip` re-time only the video track (`dispatch.py:46-54,160-209`); overlays keep absolute start/end.
- **30/33/34 — sticker select/move only under playhead** → PARTIAL: `StickerLayer` hit-tests only stickers active at the playhead (`StickerLayer.tsx:53,92`); delete path itself works.
- **35 — VoiceOver getUserMedia** → REAL: unguarded `navigator.mediaDevices.getUserMedia` (`VoRecorder.tsx:53`); packaged `.app` has no `NSMicrophoneUsageDescription`/mic entitlement (`build_app.sh:21-26`, `.spec:72-76`).
- **37 — stickers vanish + video stops on AR change** → PARTIAL(R2+R6): `set_aspect_ratio` rewrites only `canvas.w/h` (`dispatch.py:341-347`), overlay coords now off-canvas; the "video stopped" half is the R2 render crash.
- **39 / 38 — duck toggle re-lays-out music** → REAL/PARTIAL: no `set_duck` tool; the checkbox does `add_music(start:0,out:0)` + `ripple_delete` (`MediaBin.tsx:166-175`) → loses trim/position.
- **7/6 canvas** → **R6**.
- **12/13 — undo/redo inconsistent** → REAL: `_redo_stack` is process-memory only (`snapshot.py:25`), wiped by LRU eviction/restart; `commit()` clears redo (`:68`); undo/redo routed through the 120ms debounce (`store.ts:278`).
- **9/10 — Export button clipped** → **R7**.
- **40 — dual captions** → REAL: server bakes captions into the preview mp4 (`compositor.py:530-543`, no `preview` guard) **and** `TextLayer` re-draws them client-side (`Preview.tsx:277`, `TextLayer.tsx:91`) at different sizing math → one big + one small.
- **41/42/43 — anything-anywhere lanes** → REAL: no clip-type/lane validation; media dropped on non-video lanes silently redirects to v1 (`Timeline.tsx:502-511`); backend `move_clip`/`add_clip` don't type-check (`dispatch.py:39-44`).

### MEDIUM (24) / LOW (7) — headline items
- **16.2** start survives until any trim repacks the track (`_ripple_close_gap` packs from 0). **17** no reset button + no x/y editor for media clips. **28** play-at-end doesn't rewind. **31(b)** emoji inserted near end collapses to ~0.1s. **8** panels are a fixed CSS grid, no splitters. **11** session dropdown clipped by `overflow:hidden` (portal fix). **14** BY_DESIGN: reopen resumes most-recent session (needs a "New project" affordance). **1/2/3/5** no global busy indicator / action locking during dispatch. **4** preview render has no timeout (export does). **18** duplicate/split give no selection/flash feedback (machinery exists, `flashClip` called from one place). **44** Save works but no toast/auto-download. **51/52** agent miscounts / stops early — add per-track counts to tool results + re-inject `get_timeline` after batch ops. **25** marker key has no `e.repeat` guard. **55** `nudgeLeft/Right` unbound in the CapCut preset.

### INACCURATE (11) — reports to correct, do NOT "fix"
- **16.1 In/Out** works end-to-end (`in/out` *are* in `videoFingerprint`). **29** emoji drag-place AND drag-move both exist (`StickerPanel.tsx:135`, `StickerLayer.tsx:140-224`) — the real blocker is the playhead-gated selection (30/33). **36** music *is* mixed into preview (`audio_mix.py:67-171`) — "no sound" is the R2 crash. **48** vision *is* session-scoped (`dispatch.py:956-969`); the stale answer is R3 (history), not a mis-keyed cache. **7** footage is letterboxed, not stretched (`compositor.py:196-198` uses `force_original_aspect_ratio=decrease`+`pad`). **46** export cancel *is* wired end-to-end and terminates ffmpeg (`store.ts:358`→`jobs.py:134`→`compositor.py:74`) — only the *chat* turn lacks a Stop button. **45** Open `.vae` works. **54** shortcut presets don't clash (only one active at a time). **15** export error banner is not cleared by clicking Reels.

### CANNOT_VERIFY (2) — need a runtime repro
- **19** 3-way split → preview stops at 5s (split logic is correct; instrument `edl.duration` vs served mp4 duration). **49** UI shows 8s after cut to 5s (likely stale cached preview or scrubber clamp).

---

## Fix plan — phased, root-cause first

Each phase is independently shippable and testable. Ordering maximizes symptom-resolution per unit of work and puts the two credibility-killers (properties feel broken; Claude sees the wrong video) first.

### Phase 0 — Stop the bleeding (CRITICAL, ~1 day)
1. **R2 — atomic overlay PNGs + validity guard.** In `render/text_overlay.py` (and mirror `effects.py:224`, `ai/emoji.py`): replace every `img.save(dst)` with save-to-temp + `_pu.replace_with_retry(tmp, dst)` (the pattern already used for mp4 `.part` files). Add `_png_is_valid(p)` (`exists() and st_size>67 and Image.open(p).verify()`) and change the three `if not dst.exists():` guards (`:187,272,305`) to `if not _png_is_valid(dst):`. One-time purge of 0-byte `st_/sa_/text_` files on session load. **Decouple upload success from preview success** in `store.ts:262` (a preview 422 must not become an "upload failed" toast). Soften `main.py:557` message when the failing `-i` is an app-generated overlay under `cache/`.
   - *Test:* concurrent preview renders with a sticker present; kill a render mid-save and re-render (must self-heal, not 422).
2. **R3 — ground every chat turn + reset on new footage.** In `agent/loop.py chat_turn`, before the tool loop, inject an ephemeral summary of `dispatch(store,'get_timeline',{summary:True})` (duration, per-track clip srcs + counts). On a first upload into an empty timeline, reset `chat.json` (`main.py upload()`). Strengthen `system_prompt.py`: "call `get_timeline`/`find_moments` before asserting what the video contains; never rely on memory of prior uploads." Add per-track counts to `add_sticker`/`get_timeline` results (fixes 51).
   - *Test:* upload A, chat; upload B into a fresh timeline; assert Claude's first answer references B's transcript, not A's.

### Phase 1 — Make Properties actually work (HIGH, ~1 day) — the biggest perception win
3. **R1 — broaden the preview trigger.** In `Preview.tsx:48-60`, key the render effect on `edl.hash()` (already the server render-cache key) **or** extend `videoFingerprint` to include per-clip `speed`, `effects`, `transform` (x/y/scale/rotation/opacity incl. keyframe state), and `audio` (gain/fade/mute). This single change fixes 16.3, 16.4-16.8, 16.9, 16.10, 16.11, 20.
4. **Transform revert (16.11):** keep `liveTransform` applied until the *new* preview hash loads, instead of `setLiveTransform(null)` immediately on release.
5. **Color grade dedupe (16.4-16.8):** in `dispatch.py color_grade`, find an existing `Effect(type='color')` and merge params instead of appending; seed `ColorSlider` from stored params and switch `onMouseUp`→`onPointerUp`.
6. **Reset + x/y (17):** add per-section Reset buttons (neutralize transform/speed/volume/fade/color) and numeric x/y inputs for media clips (mirror `StickerProps`).
   - *Test:* drag brightness → preview visibly changes and readout reflects stored value; drag scale, release → stays; Reset → returns to neutral.

### Phase 2 — Timeline as a real CapCut timeline (CRITICAL+HIGH, ~2-3 days)
7. **R5 — content-sized scrollable timeline.** In `Timeline.tsx`, set canvas CSS width = `labelWidth + (edl.duration+pad)*zoom`; make `.timeline-canvas-wrap` `overflow:auto`; freeze the label column (sticky overlay); translate mouse hit-tests by `scrollLeft`. Map plain vertical wheel → vertical track scroll (or zoom), shift/trackpad-x → horizontal scroll; drop the no-op `scrollLeft += deltaX`.
8. **Playhead drag (24):** add `dragRef.kind='playhead'` when clicking the ruler or near the playhead x; implement live scrub in the (currently dead) `onMouseMove`; attach `mousemove`/`mouseup` to `window` so drags survive leaving the canvas (also fixes clip-drag drop-outs).
9. **Playback correctness (26/27/28):** clock follows the media clock only in the play direction; ignore `onTimeUpdate→setPlayhead` while the rAF clock owns the playhead; on `previewHash` change seek the reloaded `<video>` to `clockRef.current`; on play-at-end rewind to 0. Decide gap semantics — recommend **ripple-contiguous V1** (matches the gapless concat) so timeline model == render.
10. **R4 — cross-track ripple.** Add `_ripple_overlays(edl, cut_start, cut_len)` called from `cut_range`/`ripple_delete`/`trim_clip`: shift text/sticker `start/end` by the removed interval (clamp straddlers). Fixes emoji drift (31/32/50). *Alternative:* anchor stickers to a source-clip id.
   - *Test:* place emoji at 6s, `remove_silences` shortening the clip; emoji stays on its content moment. Drag the playhead across a 60s timeline; scrub works.

### Phase 3 — Overlays, audio, agent polish (HIGH, ~1-2 days)
11. **Dual captions (40):** guard `build_overlay_chain` with `preview=False` in `compositor._render` so the server bakes text only for **export**; `TextLayer` is the sole preview text renderer (also speeds up preview).
12. **Sticker selectability (30/33/34):** allow selecting/dragging a sticker from the timeline row regardless of playhead; keep `pointerEvents:'auto'` when a sticker is selected; auto-seek playhead into its window on select.
13. **AR-change reposition (37) + R6 canvas inference (6/7):** in `set_aspect_ratio`/`set_canvas`, rescale overlay+clip transforms proportionally to the new canvas. On first upload, infer canvas from `probe.video.width/height` (`main.py upload()`); add a `match_source_aspect`/`fit_canvas_to_clip` tool + advertise it. Read rotation `side_data`/`rotate` tag in `probe.py`.
14. **`set_duck` tool (38/39):** first-class tool that flips only `track.duck`; rewire the checkbox (no add+delete dance).
15. **VoiceOver (35):** guard `navigator.mediaDevices?.getUserMedia` in `VoRecorder.tsx` with a friendly message; add `NSMicrophoneUsageDescription` + mic entitlement + WKUIDelegate media-permission to `build_app.sh` **and** `Video AI Editor.spec` (or document VO unsupported in the frozen app).
16. **Agent Stop button (46) + coverage (52):** `AbortController` + Stop button in `ChatOverlay`; backend checks `request.is_disconnected()` between tool calls. Raise `find_moments` limit for "all"-intent; re-inject `get_timeline` into tool_result after destructive batch ops.

### Phase 4 — Layout, lifecycle, feedback (HIGH+MEDIUM, ~2 days)
17. **R7 — toolbar + dropdowns:** pin Undo/Redo/Save/Open/**Export** in a right-side non-scrolling cluster (or collapse platform presets to a dropdown); render the session dropdown in a portal to `document.body`. Fixes 9/10/11.
18. **Resizable panels (8):** draggable splitters; store two column widths in Zustand (persist to localStorage); `.app` grid → `var(--left-w) 1fr var(--right-w)`.
19. **Undo/redo (12/13):** persist the redo stack to disk + rebuild on `EDLStore.__init__`; route undo/redo through an immediate (non-debounced) refresh; disable Redo when the stack is empty.
20. **Global busy + no silent failure (1/2/3/5/15):** store-level `pendingOps` counter → toolbar spinner + disable destructive buttons during dispatch; wrap `store.dispatch` in try/catch → `toast.error`; preview render timeout (4).
21. **Feedback (18/44):** select+flash the new clip after duplicate/split/paste (reuse `flashClip`); Save → toast + auto-download; drop the `!edl?.duration` guard.
22. **Lane validation (41/42/43):** reject or snap-to-nearest-compatible-lane on drop (frontend) + type-check in `add_clip`/`move_clip` (backend) with a 422/toast instead of silent redirect-to-v1.
23. **"New project" (14):** launch affordance / banner; if prior `edl.json` fails validation, start fresh.
24. **Small correctness:** `e.repeat` guard in `engine.ts` (25); bind `nudgeLeft/Right` in CapCut preset (55); min sticker span in `add_sticker` (31b); don't force-repack on pure trim (16.2).

### Phase 5 — Verify the CANNOT_VERIFY & regression pass
25. Reproduce **19** and **49** at runtime with logging (`edl.duration` vs served mp4 duration vs scrubber clamp); fix whichever layer diverges.
26. Full pass: `cd frontend && npx tsc --noEmit && npx vite build && npm run lint`; `uv run pytest`; push and watch the **windows-latest** CI job (source of truth for cross-platform). New backend behaviors (atomic PNG write, cross-track ripple, canvas inference, lane validation) need unit tests; `test_all_tools_smoke.py` auto-covers `set_duck`/`match_source_aspect`.

---

## Effort & sequencing
- **Phase 0** (crash + agent context) and **Phase 1** (properties) are the highest ROI and lowest risk — ship them first; together they neutralize the two "the app is broken" impressions.
- **Phase 2** (timeline) is the largest single effort but is the core of "CapCut familiarity."
- Everything rides the existing **single-mutation-path** architecture — R2/R4/R6/lane-validation are `dispatch.py`/render changes; R1/R5/R7 are frontend. No architectural rewrite is required. The out-of-scope items from the earlier `2026-07-03-capcut-parity-fixes.md` plan (realtime GPU preview, chunk cache under transitions) remain out of scope; this plan shrinks preview/export divergence rather than replacing the render-then-poll model.
