# Timeline & Overlay Drag-and-Drop — Visual Feedback + Kind-Aware Resize

**Status:** Design approved (2026-07-14). Ready for implementation-plan authoring.
**Scope:** Frontend rendering/interaction only. No backend changes, no new dispatch tools, no EDL schema changes. (The three backend tools this wires up — `trim_clip`, `set_speed`, `set_clip_timing` — all already exist and already clamp correctly; verified against current code, see §4.)
**Platforms in scope (explicit):** macOS packaged app (WKWebView), Windows packaged app (WebView2), and browser-dev mode (Chrome/Safari via Vite `:5173`). All three must behave identically.

---

## 1. Problem statement

The editing engine's *functional* drag/drop layer is already correct — backend `_first_free_gap` snap-to-gap, `_v_track_for_media` lane-type validation, and their client-side mirrors in `Timeline.tsx` all work. **The defect is entirely one of visual apparency and a small set of un-wired resize operations.** Concretely, verified against current code:

1. **Dragging a timeline clip shows nothing until release.** `Timeline.tsx`'s `onMouseMove` is an intentional no-op for clip move/trim (commit-on-release, `Timeline.tsx:552-558`). The clip sits frozen while the cursor moves away, then jumps to the new position only after mouse-up + a server render round-trip. There is no ghost, no follow, no drop-target highlight, no insertion indicator. Only the playhead scrubs live.
2. **No drop-target indication.** During a drag there is no highlight of which track row the clip will land on, nor where on that row.
3. **No live overlap warning.** The amber dashed border (`Timeline.tsx:339-346`, the only `setLineDash` in the file) is drawn **only for already-persisted overlaps** after a refresh — never during a drag to warn "this position would collide." Overlap resolution (snap-to-gap) is only communicated by a `toast.info` *after* release.
4. **Edge-drag resize is media-only and half-broken.** The edge-zone hit-test (`Timeline.tsx:520-524`) is *not* gated by clip kind, so grabbing the edge of a **text clip or sticker** starts a `trim-l`/`trim-r` drag whose release calls `dispatch('trim_clip', …)`. But `trim_clip` **hard-rejects non-media clips** (`dispatch.py:438`: `raise ValueError("trim_clip only supports media clips")`), surfacing as a silent `toast.error`. So "stretch a caption to cover the whole video" currently **errors**.
5. **No drag-to-retime for video.** A video's *timeline footprint* can be changed by speed (`set_speed`, `dispatch.py:1519`, renderer applies `setpts`/`atempo`), but that tool is only reachable via chat/Properties — never by dragging. There is no way to "compress/stretch a video by hand" on the timeline.
6. **Preview stickers lack a dragging distinction.** `StickerLayer.tsx` already live-follows a dragged sticker, but a dragged sticker is drawn identically to a selected-idle one (same dashed `#5b8dff` box, `StickerLayer.tsx:143-159`); resize handles use a flat `default` cursor.

**Root observation:** the correct backend tools already exist (`trim_clip`, `set_speed`, `set_clip_timing`) — they are simply not connected to the right drag gestures, and there is no live overlay to show what any drag is doing.

---

## 2. Guiding principle & architecture

> **Live, per-frame drag feedback is drawn on a pointer-events:none overlay canvas via `requestAnimationFrame`, active only while a drag is in progress. The heavy main canvas (clips + waveforms) is never re-tessellated per frame. Nothing commits to the EDL until release — the ghost is a pure visual preview. The preview position is computed by the SAME snap/resize math the release handler uses, so preview and outcome can never drift.**

This mirrors the existing playhead pattern exactly (playhead already lives on a separate overlay canvas updated via a window-level listener).

### 2.1 Canvas topology (unchanged)

`Timeline.tsx` renders three stacked `<canvas>` elements inside `div.timeline-canvas-wrap` (`Timeline.tsx:915-946`):

1. **Main canvas** (`canvasRef`) — bg, ruler, tracks, clips, waveforms, markers, in/out shading. Redraws only on data change (React effect keyed on `edl`/`selection`/`zoom`/…). Owns `onMouseDown`/`onMouseMove`/`onMouseUp`/`onContextMenu`.
2. **Overlay canvas** (`playheadCanvasRef`) — `position:absolute`, `pointerEvents:'none'`. Currently draws only the playhead. **This is where all new live-drag chrome is drawn.**
3. **Sticky label canvas** (`labelCanvasRef`) — track labels + mute toggles, kept pinned on horizontal scroll.

We do **not** add a fourth canvas. The overlay canvas's draw path is extended.

### 2.2 New / changed units

| Unit | File | Responsibility | Depends on |
|---|---|---|---|
| Drag-visual constants | `frontend/src/lib/dragVisuals.ts` (**new**) | Single source of truth for the *style* of every interactive state (ghost alpha, drop-ok/-bad wash, overlap tint, accent, border widths, insertion-line spec). Pure constants + tiny style helpers. **No snap math.** | nothing |
| Drag-resolution helpers | `frontend/src/lib/dragResolve.ts` (**new**) | Pure, unit-testable functions that turn a drag (pointer position, delta, clip kind, track occupancy) into a concrete outcome: snapped landing start, media trim in/out, media speed factor, text/sticker start/end, overlap flag. Extracted from the logic currently inline in `onMouseUp` + `snapTime`/`firstFreeGap`. | `types.ts` |
| Timeline live overlay | `frontend/src/components/Timeline.tsx` (**modified**) | rAF overlay draw during drag; extended `dragRef`; kind-aware release; Escape-cancel. | both new libs |
| Preview sticker drag state | `frontend/src/components/StickerLayer.tsx` (**modified**) | Distinct "dragging" box + per-corner resize cursors. | `dragVisuals.ts` |
| `set_clip_timing` left-edge clamp | `src/video_ai_editor/agent/dispatch.py` (**modified, small**) | Ensure a left-edge (`start`) move clamps `0 ≤ start < end` and never inverts. | — |

**Isolation test:** `dragVisuals.ts` answers "what does 'overlap' look like?" in one place; `dragResolve.ts` answers "where does this drag land?" independently of any canvas. Timeline/StickerLayer consume both without re-implementing either.

### 2.3 What explicitly does NOT change

- The commit path: still `dispatch('move_clip' | 'trim_clip' | 'set_speed' | 'set_clip_timing' | 'add_clip' | 'add_sticker' | 'set_clip_transform')` on release, funnelling through `store.dispatch()` → `refreshSoon()`.
- The backend snap/validation logic (`_first_free_gap`, `_v_track_for_media`) and its client mirrors (`snapTime`, `firstFreeGap`) — these stay the authoritative landing-position source; `dragResolve.ts` *calls* the same math, it does not fork it.
- The 3-canvas structure and coordinate math (`zoom` px/sec, `trackY(i)`, `labelWidth`).
- Existing resting states: selection ring (white, `Timeline.tsx:331-335`), persisted-overlap amber dash, new-clip flash (`Timeline.tsx:350-362`). These remain the idle states; interactive states are layered on top only during a drag.

---

## 3. Timeline live drag feedback (core)

### 3.1 Extended drag state

`dragRef` (currently `Timeline.tsx:104-112`) gains fields it lacks:

```ts
const dragRef = useRef<null | {
  kind: 'move' | 'trim-l' | 'trim-r' | 'playhead'
  clipId: string
  trackId: string          // origin track
  startX: number
  origStart: number
  origIn: number
  origOut: number
  // NEW:
  offsetX: number          // grab point within the clip (px), so the ghost's
                           // left edge doesn't teleport to the cursor
  pointerX: number         // live cursor X (updated by the window mousemove)
  pointerY: number         // live cursor Y → resolve hovered target track/frame
  modifier: boolean        // Alt/Option held at grab-time (media speed vs trim)
  clipKind: 'media' | 'text' | 'sticker'   // cached from the hit clip
}>(null)
```

`offsetX`, `modifier`, and `clipKind` are captured in `onMouseDown`; `pointerX`/`pointerY` are updated live.

### 3.2 Live tracking

The window-level `mousemove` listener (currently playhead-only, `Timeline.tsx:565-595`) is extended: for a non-playhead active drag it updates `dragRef.current.pointerX/Y` and requests an overlay redraw (rAF-coalesced — at most one draw per frame). The main canvas is untouched. This keeps working when the pointer leaves the canvas (the existing reason for window-level binding).

### 3.3 Overlay draw — `move` drag

Each frame, on the overlay canvas, in this z-order:

1. **Drop-target track highlight.** Resolve the hovered track from `pointerY` (same linear scan as the release handler, `Timeline.tsx:636-642`). Wash that row:
   - compatible lane → `DROP_OK = rgba(91,141,255,0.10)` fill + 1px accent top/bottom border.
   - incompatible lane (media over captions/sticker/text, per `laneAcceptsMediaClip`) → `DROP_BAD = rgba(255,77,109,0.12)` — a live version of the post-drop `toast.error`.
2. **Overlap tint (if applicable).** Compute the would-be landing via `dragResolve`. If the *raw* pointer position overlaps an existing media clip on the target track (before snap resolves it), tint the colliding interval `OVERLAP_TINT = rgba(245,158,11,0.18)`.
3. **Landing / insertion line.** A solid `2px` `ACCENT` vertical line at the **snapped** start position (from `snapTime` + `firstFreeGap`, the exact values the release will use). When snapping to a neighbour edge, the line sticks to that edge.
4. **Drag ghost.** The clip redrawn at the live position: track color at `GHOST_ALPHA = 0.6`, solid `2px` `ACCENT` border, small downward shadow (lifted look). Label rides along. Distinct from the resting 0.85-alpha borderless clips.

Simultaneous (2)+(3) is the "collision + resolved landing" behavior: the ghost shows where the cursor is, the amber tint shows the conflict, and the insertion line shows the free-gap it will actually drop into. On release, existing snap + `toast.info` fire unchanged.

### 3.4 Overlay draw — `trim-l` / `trim-r` / speed drag

During any edge-drag, draw the live new edge as a bright `2px` `ACCENT` vertical handle-line, plus a small text label near the cursor stating **mode and result**:

- media trim → `"trim · 3.42s"`
- media + Alt (speed) → `"speed 1.5× · 2.30s"`
- text/sticker → `"5.00s → 12.00s"` (new window)

Neighbour-edge snap applies to all. Minimum-size clamp is enforced in the preview (so the ghost never shows an impossible zero/negative or out-of-range result) — see §4.

### 3.5 Release (kind-aware) — replaces the current trim/move branch logic

`onMouseUp` (`Timeline.tsx:624-705`) routes by `clipKind` + `modifier`, calling `dragResolve` for the numbers:

| kind | clipKind | modifier | dispatch |
|---|---|---|---|
| `move` | any | — | `move_clip { clip_id, new_start, [new_track] }` (unchanged) |
| `trim-l`/`trim-r` | `media` | no Alt | `trim_clip { clip_id, in \| out }` (unchanged) |
| `trim-l`/`trim-r` | `media` | **Alt** | `set_speed { clip_id, factor }` where `factor = clamp(orig_source_duration / new_footprint, 0.25, 4)` |
| `trim-l`/`trim-r` | `text`/`sticker` | — | `set_clip_timing { clip_id, start \| end }` |

The `< 3px` "treat as click" guard (`Timeline.tsx:630`) stays.

### 3.6 Abort & cleanup

- **Escape** during a drag (new `keydown` listener active only while `dragRef` is set) clears `dragRef` and the overlay with **zero commit**.
- **Release outside canvas** — already handled by the window `mouseup` listener (`Timeline.tsx:575-587`); overlay cleanup hooks the same path.
- On any drag end (commit or abort), the overlay is cleared and the rAF loop idles (no per-frame draw when not dragging — matches the playhead-overlay's existing idle behavior).

---

## 4. Kind-aware "stretch & compress" (edge-drag semantics)

Edge-drag does the sensible thing per clip kind, reaching the correct **already-existing** backend tool instead of erroring.

| Clip kind | Edge-drag | Tool | Semantics |
|---|---|---|---|
| Media (video/audio/music/vo) | plain drag | `trim_clip` (in/out) | Show less/more of the source; footprint changes, content unchanged. *Current behavior, now with live feedback.* |
| Media | **Alt/Option + drag** | `set_speed` | Keep the whole source; stretch/compress its *timeline footprint*. Right-edge out → slower (speed<1); in → faster (speed>1). `factor = source_dur / footprint`. |
| Text / Sticker | plain drag | `set_clip_timing` (start/end) | Move the window start (left) or end (right). "Stretch this caption across the whole video." Free-form, no source to trim. |

**Disambiguation rationale (video trim vs. speed):** both are legitimate right-edge drags. CapCut/Premiere disambiguate with a modifier ("rate stretch"). Plain drag stays trim (safe, non-destructive, current default); **Alt/Option = speed**. The live label makes the active mode explicit, so it is never ambiguous.

**Clamps (enforced in `dragResolve`, shown in preview, re-checked at dispatch):**
- text/sticker: `0 ≤ start`, `end > start` (minimum window e.g. 0.1s).
- media trim: `out > in` (minimum 0.1s), `in ≥ 0`.
- media speed: `factor ∈ [0.25, 4]`.

**Backend touch: NONE — already complete (verified against current code).** `set_clip_timing` (`dispatch.py:2358-2381`) already: sets `start` clamped to `≥ 0` (line 2373), sets `end` (line 2375), and prevents a zero/negative span via `if c.end <= c.start: c.end = c.start + 0.1` (lines 2376-2377). `set_speed` (`dispatch.py:1519-1535`) already validates `factor > 0` and stores it. `trim_clip` (`dispatch.py:432`) already clamps for media. So the left-edge/`start` clamp is present; the entire change is front-end wiring to reach these tools with the right gestures. No new tool, no schema change, no handler edit.

**Modifier detection cross-platform:** use the DOM event's `altKey` (present and consistent on `MouseEvent` in WKWebView, WebView2, and browsers). Option on macOS === Alt on Windows === `altKey:true` — so one code path covers both. Captured at `onMouseDown` (so mid-drag modifier changes don't flip the operation, matching NLE convention).

---

## 5. Preview stickers & shared visual language

### 5.1 `StickerLayer.tsx` dragging distinction

While `dragRef.current` is active (`StickerLayer.tsx:170-254`):
- The dragged sticker's box switches from the resting dashed `#5b8dff` (`StickerLayer.tsx:149-152`) to a **solid `2px`** box + subtle drop-shadow / alpha lift — reusing `dragVisuals.ts` so "dragging" reads identically to the timeline ghost.
- The corner handle being resized highlights (brighter fill).
- Resize handles get per-corner cursors (`nwse-resize` / `nesw-resize`) instead of the flat `default` (`StickerLayer.tsx:229`).

Commit path unchanged: `dispatch('set_clip_transform', …)` on pointer-up (`StickerLayer.tsx:249-253`). **No snap/alignment guides** (explicit non-goal — free-form positioning stays).

### 5.2 `dragVisuals.ts` tokens

```ts
export const ACCENT       = '#5b8dff'
export const GHOST_ALPHA  = 0.6
export const DROP_OK      = 'rgba(91,141,255,0.10)'
export const DROP_BAD     = 'rgba(255,77,109,0.12)'
export const OVERLAP_TINT = 'rgba(245,158,11,0.18)'
export const DRAG_BORDER_W = 2
export const INSERTION_W   = 2
// + tiny helpers e.g. dashFor(state), cursorForCorner(cx,cy)
```

Colors are drawn from the existing palette (`--accent`/`--accent-2`, `TRACK_COLORS`, the amber `#f59e0b` already used for overlap) so nothing clashes with the current look.

---

## 6. Cross-platform / cross-browser correctness (explicit)

The user requires this to work on **all platforms, browsers, and the packaged application**. Concrete requirements and rationale:

### 6.1 Event model
- **Timeline keeps `MouseEvent`** (`onMouseDown/Move/Up` + window-level `mousemove`/`mouseup`). Rationale: `MouseEvent` behaves byte-identically in WKWebView (macOS), WebView2 (Windows/Chromium), and browser-dev; the window-level listeners already solve the "no pointer capture" gap. Migrating to `PointerEvent` is explicitly **out of scope** (higher risk, no functional gain here, would perturb the macOS-preserved path).
- **StickerLayer keeps `PointerEvent`** + `setPointerCapture` (already cross-platform-safe; capture is wrapped in try/catch for synthetic/edge pointers, `StickerLayer.tsx:187`).
- All new listeners (Escape keydown, rAF) use standard DOM APIs present in all three webviews. `requestAnimationFrame` is universally available (WKWebView, WebView2, browsers).

### 6.2 Native HTML5 drag-drop (panel → timeline)
- The media/sticker panels use `draggable` + `dataTransfer` (`MediaBin.tsx:115-119`, `StickerPanel.tsx:142-146`), consumed by `Timeline.tsx`'s `onCanvasDragOver`/`onCanvasDrop` (`Timeline.tsx:708-783`). HTML5 DnD is fully supported in WKWebView, WebView2, and browsers.
- **Insertion preview during a panel drag** must be driven by `onDragOver`'s `clientX/clientY` (reliably provided by all three webviews), **not** by the OS drag-image (whose live position is not queryable). Implementation: `onCanvasDragOver` (which already fires continuously) computes the same drop-target highlight + insertion line as an in-timeline move and draws them on the overlay canvas; cleared on `drop`/`dragleave`/`dragend`.
- Panel-drop overlap: backend `add_clip` has **no** `_first_free_gap` guard (only `move_clip` does), but `Timeline.tsx`'s drop handler already snaps `start` client-side via `snapTime` (`Timeline.tsx:780-782`). The insertion preview uses the same snapped value, so panel-drops show their true landing. *(Optional low-priority follow-up, flagged not core: add `_first_free_gap` to backend `add_clip` for Claude/MCP parity — those callers bypass the frontend snap.)*

### 6.3 Rendering / DPI
- The overlay canvas uses the same `dpr` (device-pixel-ratio) scaling and coordinate transforms as the main + playhead canvases, so ghost/insertion line stay pixel-aligned on Retina (macOS) and Windows high-DPI displays, at any zoom (`zoom` px/sec) and horizontal scroll offset.

### 6.4 Packaged-app specifics
- No `getUserMedia`/TCC/native-bridge concerns (this is pure canvas/DOM). The feature works identically whether served from `frontend/dist` in the packaged FastAPI process or from Vite dev — same code path.
- No new dependencies, no build-config changes, so `build_app.sh` (macOS) / `build_win.ps1` (Windows) / `npx tsc --noEmit && npx vite build` (CI) are unaffected.

---

## 7. Testing strategy

Canvas pixels aren't unit-testable, so tests target the *logic* that feeds the drawing — the part that must stay correct — plus live verification for the visuals.

### 7.1 Frontend unit (Vitest) — `dragResolve.ts`
- Media trim: left-edge delta → new `in`; right-edge delta → new `out`; `out > in` clamp; `in ≥ 0`.
- Media speed (Alt): `factor = source_dur / footprint`; clamp to `[0.25, 4]`; right-edge-out slows, in speeds up.
- Text/sticker: left-edge → `start`; right-edge → `end`; `end > start` clamp; `start ≥ 0`.
- Snap landing: given pointer + track occupancy, result **equals** `firstFreeGap` for the same inputs (guarantees preview == outcome).
- Overlap detection: half-open interval test with the `1e-9` epsilon matches the draw-loop `seenRanges` test and `firstFreeGap`'s `overlaps`.

### 7.2 Backend (pytest)
No backend code changes, so no new backend behavior to test. The relevant clamps already exist and `test_all_tools_smoke` already parametrizes every `DISPATCH` key (so `set_clip_timing`/`set_speed`/`trim_clip` are smoke-covered). **Optional (documents the invariant the frontend now relies on, not a regression against a change):** a small `set_clip_timing` test asserting `end > start` is enforced on a left-edge (`start`) move and that it rejects a media `Clip`. Skip if judged redundant with the existing smoke coverage.

### 7.3 Live / manual verification (the `verify` skill, running app)
Per this project's "green checkmark ≠ working feature" rule, all canvas UX must be eyeballed in the running app (`bash run.sh`), and — because the requirement is cross-platform — the Windows path is verified via the `windows-latest` CI job for anything testable there, with the interactive canvas checks done in the packaged app where possible:
- Move a clip → ghost follows, drop-target row highlights (blue), insertion line at landing.
- Drag a clip over the captions row → red drop-bad wash; release leaves it on origin.
- Drag a clip onto an occupied range → amber collision tint + insertion line at the free gap; release snaps + toast.
- Alt-drag a video's right edge → speed label; footprint stretches/compresses; playback confirms retiming.
- Drag a text clip's right edge out to span the whole video → `set_clip_timing`, no error.
- Drag a sticker on the preview → solid dragging box + per-corner cursor.
- Escape mid-drag → no change committed.
- Repeat the interactive smoke in browser-dev (`:5173`) to confirm parity.

---

## 8. Edge cases & non-goals

**Edge cases handled:**
- Escape aborts a drag with no commit (new).
- Drag released outside the canvas (existing window-listener path).
- Muted/ghost clips, keyframed transforms — overlay uses the same coordinate math, stays aligned.
- Any zoom level / horizontal scroll offset — overlay shares `zoom`/`trackY`/`scrollLeft` handling with the main canvas.
- Zero/negative/out-of-range resize prevented in preview and re-checked at dispatch.

**Non-goals (explicitly excluded):**
- Multi-clip drag / multi-select drag.
- Ripple-on-drag (shifting downstream clips as you drag).
- Alignment / centering / snap guides on the *preview* (sticker positioning stays free-form).
- Migrating Timeline to PointerEvents.
- Backend EDL schema changes or new dispatch tools.
- Touch/pen input (the app is desktop-only; mouse/trackpad only).

---

## 9. File-change summary

| File | Change |
|---|---|
| `frontend/src/lib/dragVisuals.ts` | **New** — style constants + tiny helpers. |
| `frontend/src/lib/dragResolve.ts` | **New** — pure drag→outcome resolution + overlap/snap helpers (unit-tested). |
| `frontend/src/components/Timeline.tsx` | Extend `dragRef`; live overlay draw (move + trim + speed); kind-aware release; panel-drop insertion preview; Escape-cancel. |
| `frontend/src/components/StickerLayer.tsx` | Distinct dragging box + per-corner resize cursors; consume `dragVisuals`. |
| `frontend/src/**/__tests__/dragResolve.test.ts` | **New** — Vitest unit tests. |
| `tests/test_set_clip_timing.py` | **Optional** — `end > start` invariant regression (see §7.2); skip if redundant with smoke. |

**No backend source changes.** `src/video_ai_editor/agent/dispatch.py` is unchanged — the tools it exposes already behave correctly (verified). No changes to `build_app.sh`, `.spec`, CI config, or dependencies. Because the change is confined to two new pure-TS libs + two component files, it cannot regress the Python/render pipeline, the packaging scripts, or the cross-platform binary paths.
