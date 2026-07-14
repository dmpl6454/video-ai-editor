# Timeline & Overlay Drag-and-Drop Visual Feedback — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make timeline drag/drop visible and direct — clip follows the cursor with a drop-target highlight, insertion line, and live overlap warning + snap preview — and wire edge-drag to the correct existing backend tool per clip kind (media trim / Alt-speed / text-sticker retime), plus a dragging distinction on preview stickers.

**Architecture:** All live drag chrome is drawn on the EXISTING pointer-events:none overlay canvas via `requestAnimationFrame`, active only during a drag — the heavy main canvas is never re-tessellated per frame (mirrors the existing playhead-overlay pattern). Drag→outcome math lives in a new pure, unit-tested module (`dragResolve.ts`); drag styling lives in a new constants module (`dragVisuals.ts`). Nothing commits to the EDL until release; the preview position is computed by the SAME math the release handler uses, so preview and outcome cannot drift. Frontend-only — the three backend tools (`trim_clip`, `set_speed`, `set_clip_timing`) already exist and already clamp correctly.

**Tech Stack:** React 19 + TypeScript, Zustand store, HTML `<canvas>` 2D, Vite. New: Vitest (added in Task 1) for the pure-logic unit tests. Verification via the running app (`bash run.sh`) per the repo's "green checkmark ≠ working" rule.

**Spec:** `docs/superpowers/specs/2026-07-14-timeline-dragdrop-visual-feedback-design.md`

---

## File Structure

| File | Responsibility |
|---|---|
| `frontend/vitest.config.ts` | **New.** Minimal Vitest config (node env, `src/**/*.test.ts`). |
| `frontend/package.json` | **Modify.** Add `vitest` devDep + `"test": "vitest run"` script. |
| `.github/workflows/ci.yml` | **Modify.** Add a `npx vitest run` step to the frontend job. |
| `frontend/src/lib/dragResolve.ts` | **New.** Pure functions: snapped landing, overlap detection, and edge-drag→outcome (media trim in/out, media speed factor, text/sticker start/end). No React, no canvas. |
| `frontend/src/lib/dragResolve.test.ts` | **New.** Vitest unit tests for every `dragResolve` function. |
| `frontend/src/lib/dragVisuals.ts` | **New.** Style constants + tiny helpers shared by Timeline + StickerLayer. |
| `frontend/src/components/Timeline.tsx` | **Modify.** Extend `dragRef`; capture grab offset + modifier + clip kind; live overlay draw (move + trim + speed); kind-aware release; panel-drop insertion preview; Escape-cancel. |
| `frontend/src/components/StickerLayer.tsx` | **Modify.** Distinct dragging box + per-corner resize cursors; consume `dragVisuals`. |

**No backend source changes.** No changes to `build_app.sh`, `.spec`, or dependencies beyond the Vitest devDep.

---

## Interface Contract (locked — every task must match these exactly)

`frontend/src/lib/dragResolve.ts` exports:

```ts
import type { AnyClip, Track } from '../types'

// Half-open interval overlap with the 1e-9 epsilon used everywhere else.
export function rangesOverlap(aStart: number, aEnd: number, bStart: number, bEnd: number): boolean

// The first free start >= preferredStart on `track` for a media clip of
// `duration`, ignoring `ignoreClipId`. Identical algorithm to Timeline's
// existing `firstFreeGap` / dispatch.py's `_first_free_gap`.
export function snapToFreeGap(
  track: Track, duration: number, preferredStart: number, ignoreClipId: string,
): number

// True if placing [start, start+duration) on `track` would overlap any media
// clip except ignoreClipId.
export function wouldOverlap(
  track: Track, duration: number, start: number, ignoreClipId: string,
): boolean

// Media trim from an edge. side='l' moves in_; side='r' moves out.
// Returns the new in/out clamped so out > in and in >= 0 (min span 0.1s).
export function resolveMediaTrim(
  clip: { in: number; out: number }, side: 'l' | 'r', deltaSec: number,
): { in: number; out: number }

// Media speed-retime from an edge (Alt-drag). `sourceDur` = clip.out - clip.in
// (the untimed source span). `currentSpeed` = the clip's speed (default 1).
// newFootprint = current footprint +/- delta (right-edge drag changes the end;
// left-edge changes the start-side, both change footprint length). Returns the
// speed factor clamped to [0.25, 4]. factor = sourceDur / newFootprint.
export function resolveMediaSpeed(
  sourceDur: number, currentSpeed: number, side: 'l' | 'r', deltaSec: number,
): number

// Text/sticker window retime. side='l' moves start; side='r' moves end.
// Returns new start/end clamped so end > start and start >= 0 (min span 0.1s).
export function resolveOverlayTiming(
  clip: { start: number; end: number }, side: 'l' | 'r', deltaSec: number,
): { start: number; end: number }
```

`frontend/src/lib/dragVisuals.ts` exports:

```ts
export const ACCENT = '#5b8dff'
export const GHOST_ALPHA = 0.6
export const DROP_OK = 'rgba(91,141,255,0.10)'
export const DROP_BAD = 'rgba(255,77,109,0.12)'
export const OVERLAP_TINT = 'rgba(245,158,11,0.18)'
export const DRAG_BORDER_W = 2
export const INSERTION_W = 2
// Cursor for a corner handle at local sign (sx, sy) ∈ {-1,1}².
export function cursorForCorner(sx: number, sy: number): string
```

`Timeline.tsx` `dragRef` extended shape (Task 5):

```ts
const dragRef = useRef<null | {
  kind: 'move' | 'trim-l' | 'trim-r' | 'playhead'
  clipId: string
  trackId: string
  startX: number
  origStart: number
  origIn: number
  origOut: number
  offsetX: number          // NEW: grab point within the clip (px)
  pointerX: number         // NEW: live cursor X (px, canvas-space)
  pointerY: number         // NEW: live cursor Y (px, canvas-space)
  modifier: boolean        // NEW: altKey at grab-time (media speed vs trim)
  clipKind: 'media' | 'text' | 'sticker'  // NEW: cached from the hit clip
}>(null)
```

The `speed` field is read from a media clip via the repo's established cast pattern: `(c as unknown as { speed?: number | null }).speed ?? 1`.

---

## Task 1: Add Vitest tooling

**Files:**
- Create: `frontend/vitest.config.ts`
- Modify: `frontend/package.json`
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Add the Vitest dev dependency**

Run (from repo root):
```bash
cd frontend && npm install -D vitest@^3 && cd ..
```
Expected: `package.json` `devDependencies` gains a `vitest` entry; `package-lock.json` updates.

- [ ] **Step 2: Add the `test` script to `frontend/package.json`**

Change the `"scripts"` block from:
```json
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build",
    "lint": "eslint .",
    "preview": "vite preview"
  },
```
to:
```json
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build",
    "lint": "eslint .",
    "preview": "vite preview",
    "test": "vitest run"
  },
```

- [ ] **Step 3: Create `frontend/vitest.config.ts`**

```ts
import { defineConfig } from 'vitest/config'

// Pure-logic unit tests only (no DOM/canvas). Kept deliberately minimal —
// the frontend otherwise has no test runner; CI's real gate stays tsc + build.
export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
})
```

- [ ] **Step 4: Create a trivial smoke test so the runner has something to run**

Create `frontend/src/lib/dragResolve.test.ts`:
```ts
import { describe, it, expect } from 'vitest'

describe('vitest wiring', () => {
  it('runs', () => {
    expect(1 + 1).toBe(2)
  })
})
```

- [ ] **Step 5: Run the test runner to verify the toolchain works**

Run: `cd frontend && npx vitest run`
Expected: PASS — 1 test passed. (Confirms vitest + config resolve.)

- [ ] **Step 6: Verify tsc still passes (config file must type-check)**

Run: `cd frontend && npx tsc --noEmit`
Expected: no new errors versus the pre-change baseline. (`vitest.config.ts` uses `vitest/config`, which ships its own types.)

- [ ] **Step 7: Add the CI step**

In `.github/workflows/ci.yml`, in the `frontend` job, insert a new step immediately AFTER the `Type-check` step (the one running `npx tsc --noEmit`) and BEFORE `Build`:
```yaml
      - name: Unit tests
        run: npx vitest run
```
(The job already has `defaults.run.working-directory: frontend`, so no `cd` is needed.)

- [ ] **Step 8: Commit**

```bash
git add frontend/package.json frontend/package-lock.json frontend/vitest.config.ts frontend/src/lib/dragResolve.test.ts .github/workflows/ci.yml
git commit -m "test: add Vitest for pure frontend logic + CI step"
```

---

## Task 2: `dragVisuals.ts` — shared style constants

**Files:**
- Create: `frontend/src/lib/dragVisuals.ts`

- [ ] **Step 1: Create the module**

```ts
// Shared visual language for drag/selection/overlap feedback across the
// Timeline canvas and the preview StickerLayer, so "dragging" / "drop-ok" /
// "overlap" read identically wherever they appear. Colors come from the
// existing palette (--accent-2 blue, --accent red, the amber #f59e0b already
// used for the persisted-overlap dashed border) so nothing clashes.

export const ACCENT = '#5b8dff'            // --accent-2, the interactive blue
export const GHOST_ALPHA = 0.6             // dragged-clip ghost opacity
export const DROP_OK = 'rgba(91,141,255,0.10)'    // compatible drop-target wash
export const DROP_BAD = 'rgba(255,77,109,0.12)'   // incompatible-lane wash (--accent red)
export const OVERLAP_TINT = 'rgba(245,158,11,0.18)' // would-overlap region (amber)
export const DRAG_BORDER_W = 2             // ghost / dragging-box border px
export const INSERTION_W = 2               // landing/insertion line px

// Cursor for a corner handle at local sign (sx, sy) ∈ {-1,1}². Top-left and
// bottom-right share the NWSE diagonal; top-right and bottom-left share NESW.
export function cursorForCorner(sx: number, sy: number): string {
  return sx * sy > 0 ? 'nwse-resize' : 'nesw-resize'
}
```

- [ ] **Step 2: Verify it type-checks**

Run: `cd frontend && npx tsc --noEmit`
Expected: no new errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/dragVisuals.ts
git commit -m "feat: dragVisuals shared style constants for drag feedback"
```

---

## Task 3: `dragResolve.ts` — overlap + snap helpers (TDD)

**Files:**
- Create: `frontend/src/lib/dragResolve.ts`
- Test: `frontend/src/lib/dragResolve.test.ts` (replace the smoke test)

- [ ] **Step 1: Write the failing tests**

Replace the entire contents of `frontend/src/lib/dragResolve.test.ts` with:
```ts
import { describe, it, expect } from 'vitest'
import { rangesOverlap, snapToFreeGap, wouldOverlap } from './dragResolve'
import type { Track } from '../types'

function mediaTrack(clips: { id: string; start: number; dur: number }[]): Track {
  return {
    id: 'v1', type: 'video', z: 0,
    clips: clips.map((c) => ({ id: c.id, src: 'x.mp4', in: 0, out: c.dur, start: c.start })),
  }
}

describe('rangesOverlap', () => {
  it('detects a true overlap', () => {
    expect(rangesOverlap(0, 5, 3, 8)).toBe(true)
  })
  it('treats exact edge-abutment as non-overlapping', () => {
    expect(rangesOverlap(0, 5, 5, 10)).toBe(false)
  })
  it('returns false for disjoint ranges', () => {
    expect(rangesOverlap(0, 2, 4, 6)).toBe(false)
  })
})

describe('wouldOverlap', () => {
  const track = mediaTrack([{ id: 'a', start: 0, dur: 5 }, { id: 'b', start: 10, dur: 5 }])
  it('is true when the placement lands inside an existing clip', () => {
    expect(wouldOverlap(track, 3, 2, 'ignore')).toBe(true)
  })
  it('is false in a free gap', () => {
    expect(wouldOverlap(track, 3, 6, 'ignore')).toBe(false)
  })
  it('ignores the clip being moved', () => {
    expect(wouldOverlap(track, 5, 0, 'a')).toBe(false)
  })
})

describe('snapToFreeGap', () => {
  const track = mediaTrack([{ id: 'a', start: 0, dur: 5 }, { id: 'b', start: 10, dur: 5 }])
  it('returns the preferred start when the slot is free', () => {
    expect(snapToFreeGap(track, 3, 6, 'ignore')).toBe(6)
  })
  it('snaps forward past a collided clip to its end', () => {
    expect(snapToFreeGap(track, 3, 2, 'ignore')).toBe(5)
  })
  it('clamps a negative preferred start to 0', () => {
    expect(snapToFreeGap(mediaTrack([]), 3, -4, 'ignore')).toBe(0)
  })
  it('excludes the moving clip from occupancy', () => {
    // Dropping clip "a" onto its own original slot must not snap.
    expect(snapToFreeGap(track, 5, 0, 'a')).toBe(0)
  })
})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd frontend && npx vitest run`
Expected: FAIL — cannot resolve `./dragResolve` exports (`rangesOverlap`, `snapToFreeGap`, `wouldOverlap` not defined).

- [ ] **Step 3: Implement the three helpers**

Create `frontend/src/lib/dragResolve.ts`:
```ts
// Pure drag→outcome math for the timeline. NO React, NO canvas — everything
// here is unit-tested (dragResolve.test.ts). The snap/overlap functions are
// the single source of truth used BOTH by the live drag preview and by the
// release handler, so what you see while dragging equals where it lands.
//
// The snap algorithm is identical to Timeline's existing `firstFreeGap` and
// dispatch.py's `_first_free_gap` (same 1e-9 epsilon), consolidated here.

import type { AnyClip, Track } from '../types'
import { isMediaClip } from '../types'

const EPS = 1e-9

export function rangesOverlap(
  aStart: number, aEnd: number, bStart: number, bEnd: number,
): boolean {
  return aStart < bEnd - EPS && aEnd > bStart + EPS
}

function occupiedRanges(track: Track, ignoreClipId: string): [number, number][] {
  return track.clips
    .filter(isMediaClip)
    .filter((c) => c.id !== ignoreClipId)
    .map((c): [number, number] => [c.start, c.start + (c.out - c.in)])
    .sort((a, b) => a[0] - b[0])
}

export function wouldOverlap(
  track: Track, duration: number, start: number, ignoreClipId: string,
): boolean {
  const end = start + duration
  return occupiedRanges(track, ignoreClipId).some(
    ([oStart, oEnd]) => rangesOverlap(start, end, oStart, oEnd),
  )
}

export function snapToFreeGap(
  track: Track, duration: number, preferredStart: number, ignoreClipId: string,
): number {
  const occupied = occupiedRanges(track, ignoreClipId)
  let candidate = Math.max(0, preferredStart)
  const overlaps = (start: number) => {
    const end = start + duration
    return occupied.some(([oStart, oEnd]) => rangesOverlap(start, end, oStart, oEnd))
  }
  if (!overlaps(candidate)) return candidate
  for (const [oStart, oEnd] of occupied) {
    if (candidate < oEnd && candidate + duration > oStart) candidate = oEnd
  }
  for (let i = 0; i < occupied.length; i++) {
    if (!overlaps(candidate)) break
    for (const [oStart, oEnd] of occupied) {
      if (candidate < oEnd && candidate + duration > oStart) candidate = oEnd
    }
  }
  return candidate
}

// eslint keeps AnyClip imported for the resolve* functions added in Task 4.
export type { AnyClip }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd frontend && npx vitest run`
Expected: PASS — all `rangesOverlap` / `wouldOverlap` / `snapToFreeGap` tests green.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/dragResolve.ts frontend/src/lib/dragResolve.test.ts
git commit -m "feat: dragResolve overlap + snap-to-gap helpers (TDD)"
```

---

## Task 4: `dragResolve.ts` — kind-aware edge-drag resolution (TDD)

**Files:**
- Modify: `frontend/src/lib/dragResolve.ts`
- Test: `frontend/src/lib/dragResolve.test.ts` (append)

- [ ] **Step 1: Write the failing tests (append to the existing test file)**

Append to `frontend/src/lib/dragResolve.test.ts`:
```ts
import { resolveMediaTrim, resolveMediaSpeed, resolveOverlayTiming } from './dragResolve'

describe('resolveMediaTrim', () => {
  const clip = { in: 2, out: 8 } // 6s source span
  it('right-edge drag extends out', () => {
    expect(resolveMediaTrim(clip, 'r', 2)).toEqual({ in: 2, out: 10 })
  })
  it('left-edge drag moves in', () => {
    expect(resolveMediaTrim(clip, 'l', 1)).toEqual({ in: 3, out: 8 })
  })
  it('clamps so out stays > in (min span 0.1)', () => {
    expect(resolveMediaTrim(clip, 'r', -100)).toEqual({ in: 2, out: 2.1 })
  })
  it('clamps in to >= 0', () => {
    expect(resolveMediaTrim(clip, 'l', -100)).toEqual({ in: 0, out: 8 })
  })
})

describe('resolveMediaSpeed', () => {
  // source span 6s at speed 1 → footprint 6s.
  it('right-edge drag OUT slows down (speed < 1)', () => {
    // new footprint 12s → factor 6/12 = 0.5
    expect(resolveMediaSpeed(6, 1, 'r', 6)).toBeCloseTo(0.5, 5)
  })
  it('right-edge drag IN speeds up (speed > 1)', () => {
    // new footprint 3s → factor 6/3 = 2
    expect(resolveMediaSpeed(6, 1, 'r', -3)).toBeCloseTo(2, 5)
  })
  it('clamps to a maximum of 4x', () => {
    expect(resolveMediaSpeed(6, 1, 'r', -5.9)).toBe(4)
  })
  it('clamps to a minimum of 0.25x', () => {
    expect(resolveMediaSpeed(6, 1, 'r', 100)).toBe(0.25)
  })
  it('accounts for an already-retimed clip footprint', () => {
    // source 6s at speed 2 → current footprint 3s; drag out +3 → 6s → factor 1
    expect(resolveMediaSpeed(6, 2, 'r', 3)).toBeCloseTo(1, 5)
  })
})

describe('resolveOverlayTiming', () => {
  const clip = { start: 5, end: 8 }
  it('right-edge drag extends end', () => {
    expect(resolveOverlayTiming(clip, 'r', 4)).toEqual({ start: 5, end: 12 })
  })
  it('left-edge drag moves start', () => {
    expect(resolveOverlayTiming(clip, 'l', -3)).toEqual({ start: 2, end: 8 })
  })
  it('clamps so end stays > start (min span 0.1)', () => {
    expect(resolveOverlayTiming(clip, 'r', -100)).toEqual({ start: 5, end: 5.1 })
  })
  it('clamps start to >= 0 and keeps end > start', () => {
    expect(resolveOverlayTiming(clip, 'l', -100)).toEqual({ start: 0, end: 8 })
  })
})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd frontend && npx vitest run`
Expected: FAIL — `resolveMediaTrim` / `resolveMediaSpeed` / `resolveOverlayTiming` not exported.

- [ ] **Step 3: Implement the three resolvers**

In `frontend/src/lib/dragResolve.ts`, remove the temporary `export type { AnyClip }` line and append:
```ts
const MIN_SPAN = 0.1
const SPEED_MIN = 0.25
const SPEED_MAX = 4

export function resolveMediaTrim(
  clip: { in: number; out: number }, side: 'l' | 'r', deltaSec: number,
): { in: number; out: number } {
  if (side === 'l') {
    const newIn = Math.min(Math.max(0, clip.in + deltaSec), clip.out - MIN_SPAN)
    return { in: newIn, out: clip.out }
  }
  const newOut = Math.max(clip.out + deltaSec, clip.in + MIN_SPAN)
  return { in: clip.in, out: newOut }
}

export function resolveMediaSpeed(
  sourceDur: number, currentSpeed: number, side: 'l' | 'r', deltaSec: number,
): number {
  const currentFootprint = sourceDur / (currentSpeed || 1)
  // Dragging either edge outward lengthens the footprint; inward shortens it.
  // deltaSec is signed in timeline space: +delta on the right edge or -delta on
  // the left edge both lengthen. We pass the already-signed footprint delta.
  const footprintDelta = side === 'r' ? deltaSec : -deltaSec
  const newFootprint = Math.max(MIN_SPAN, currentFootprint + footprintDelta)
  const factor = sourceDur / newFootprint
  return Math.min(SPEED_MAX, Math.max(SPEED_MIN, factor))
}

export function resolveOverlayTiming(
  clip: { start: number; end: number }, side: 'l' | 'r', deltaSec: number,
): { start: number; end: number } {
  if (side === 'l') {
    const newStart = Math.min(Math.max(0, clip.start + deltaSec), clip.end - MIN_SPAN)
    return { start: newStart, end: clip.end }
  }
  const newEnd = Math.max(clip.end + deltaSec, clip.start + MIN_SPAN)
  return { start: clip.start, end: newEnd }
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd frontend && npx vitest run`
Expected: PASS — all resolver tests green (and the Task 3 tests still green).

- [ ] **Step 5: Verify tsc passes**

Run: `cd frontend && npx tsc --noEmit`
Expected: no new errors.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/lib/dragResolve.ts frontend/src/lib/dragResolve.test.ts
git commit -m "feat: dragResolve kind-aware edge-drag resolution (trim/speed/overlay) (TDD)"
```

---

## Task 5: Timeline — extend `dragRef` and capture grab context

**Files:**
- Modify: `frontend/src/components/Timeline.tsx:104-112` (dragRef shape)
- Modify: `frontend/src/components/Timeline.tsx:480-550` (onMouseDown)

This task only extends state capture; the live draw + release wiring come in Tasks 6–8. After this task the app still behaves exactly as before (the new fields are captured but unused).

- [ ] **Step 1: Add the imports**

At the top of `Timeline.tsx`, add to the existing import from `../types` and add the two new lib imports. Change:
```ts
import { isMediaClip, type AnyClip, type Track } from '../types'
```
to:
```ts
import { isMediaClip, isTextClip, type AnyClip, type Track } from '../types'
import * as dragResolve from '../lib/dragResolve'
import * as dv from '../lib/dragVisuals'
```

Then add an `isTextClip` guard to `frontend/src/types.ts` right after `isMediaClip` (it does not exist yet):
```ts
export function isTextClip(c: AnyClip): c is TextClip {
  return 'text' in c && 'end' in c
}
```

- [ ] **Step 2: Extend the `dragRef` shape**

Replace `Timeline.tsx:104-112`:
```ts
  const dragRef = useRef<null | {
    kind: 'move' | 'trim-l' | 'trim-r' | 'playhead'
    clipId: string
    trackId: string
    startX: number
    origStart: number
    origIn: number
    origOut: number
  }>(null)
```
with:
```ts
  const dragRef = useRef<null | {
    kind: 'move' | 'trim-l' | 'trim-r' | 'playhead'
    clipId: string
    trackId: string
    startX: number
    origStart: number
    origIn: number
    origOut: number
    offsetX: number          // grab point within the clip (px)
    pointerX: number         // live cursor X (canvas-space px)
    pointerY: number         // live cursor Y (canvas-space px)
    modifier: boolean        // altKey at grab-time (media speed vs trim)
    clipKind: 'media' | 'text' | 'sticker'  // cached from the hit clip
  }>(null)
```

- [ ] **Step 3: Populate the new fields in the playhead branch of `onMouseDown`**

Replace the playhead `dragRef.current = {...}` assignment (`Timeline.tsx:496-500`):
```ts
      dragRef.current = {
        kind: 'playhead',
        clipId: '', trackId: '',
        startX: e.clientX, origStart: 0, origIn: 0, origOut: 0,
      }
```
with:
```ts
      dragRef.current = {
        kind: 'playhead',
        clipId: '', trackId: '',
        startX: e.clientX, origStart: 0, origIn: 0, origOut: 0,
        offsetX: 0, pointerX: x, pointerY: y, modifier: false, clipKind: 'media',
      }
```

- [ ] **Step 4: Populate the new fields in the clip branch of `onMouseDown`**

Replace the clip `dragRef.current = {...}` assignment (`Timeline.tsx:526-534`):
```ts
      dragRef.current = {
        kind,
        clipId: c.id,
        trackId: hit.trackId,
        startX: e.clientX,
        origStart: 'start' in c ? c.start : 0,
        origIn: isMediaClip(c) ? c.in : 0,
        origOut: isMediaClip(c) ? c.out : 0,
      }
```
with:
```ts
      const clipKind: 'media' | 'text' | 'sticker' = isMediaClip(c)
        ? 'media'
        : (isTextClip(c) ? 'text' : 'sticker')
      dragRef.current = {
        kind,
        clipId: c.id,
        trackId: hit.trackId,
        startX: e.clientX,
        origStart: 'start' in c ? c.start : 0,
        origIn: isMediaClip(c) ? c.in : 0,
        origOut: isMediaClip(c) ? c.out : 0,
        offsetX: x - hit.x,
        pointerX: x,
        pointerY: y,
        modifier: e.altKey,
        clipKind,
      }
```

- [ ] **Step 5: Verify tsc passes and the app still builds**

Run: `cd frontend && npx tsc --noEmit && npx vite build`
Expected: no new errors; build succeeds. (Behavior unchanged — new fields captured but not yet read.)

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/Timeline.tsx frontend/src/types.ts
git commit -m "feat: capture grab offset + modifier + clip kind in timeline dragRef"
```

---

## Task 6: Timeline — live drag overlay draw (the core visual)

**Files:**
- Modify: `frontend/src/components/Timeline.tsx` (window mousemove listener ~565-595; add a drag-overlay draw effect + a `dragTick` state)

This task makes the ghost/drop-target/insertion/overlap chrome appear DURING a drag. Release still uses the current commit logic (rewired in Task 7).

- [ ] **Step 1: Add a `dragTick` state to drive overlay redraws**

Immediately after the `flashClipId` line (`Timeline.tsx:101`), add:
```ts
  // Bumped on each pointer move during a clip drag to trigger the drag-overlay
  // redraw (rAF-coalesced). 0 when idle so no per-frame work happens unless a
  // drag is active — same "only redraw while interacting" posture as playhead.
  const [dragTick, setDragTick] = useState(0)
  const dragRafRef = useRef<number | null>(null)
```

- [ ] **Step 2: Extend the window `mousemove` listener to track clip drags**

Replace the `onWindowMouseMove` function body (`Timeline.tsx:566-574`):
```ts
    function onWindowMouseMove(e: MouseEvent) {
      const drag = dragRef.current
      if (!drag || drag.kind !== 'playhead' || !canvasRef.current) return
      const rect = canvasRef.current.getBoundingClientRect()
      const x = e.clientX - rect.left
      const raw = Math.max(0, (x - labelWidth) / zoom)
      const dur = edl?.duration ?? raw
      setPlayhead(Math.min(raw, dur))
    }
```
with:
```ts
    function onWindowMouseMove(e: MouseEvent) {
      const drag = dragRef.current
      if (!drag || !canvasRef.current) return
      const rect = canvasRef.current.getBoundingClientRect()
      const x = e.clientX - rect.left
      const y = e.clientY - rect.top
      if (drag.kind === 'playhead') {
        const raw = Math.max(0, (x - labelWidth) / zoom)
        const dur = edl?.duration ?? raw
        setPlayhead(Math.min(raw, dur))
        return
      }
      // Clip move/trim: record live pointer, request one overlay redraw/frame.
      drag.pointerX = x
      drag.pointerY = y
      if (dragRafRef.current == null) {
        dragRafRef.current = requestAnimationFrame(() => {
          dragRafRef.current = null
          setDragTick((n) => n + 1)
        })
      }
    }
```

- [ ] **Step 3: Add the drag-overlay draw effect (draws onto the playhead overlay canvas)**

Add this effect immediately AFTER the existing playhead-overlay effect (right after `Timeline.tsx:476`, the closing `}, [playhead, zoom, contentW, contentH, dpr])`):
```ts
  // Live drag chrome — drawn on the SAME overlay canvas as the playhead (which
  // this effect runs after, so drag chrome layers on top), gated on an active
  // clip drag. Idle (no dragRef) → this contributes nothing; the playhead
  // effect's clearRect already ran. Keyed on dragTick so it re-runs each frame
  // only while dragging. The heavy main canvas is never touched here.
  useEffect(() => {
    const drag = dragRef.current
    const cv = playheadCanvasRef.current
    if (!cv || !drag || drag.kind === 'playhead') return
    const ctx = cv.getContext('2d')!
    // The playhead effect already sized + cleared + drew the playhead this
    // frame; do NOT clear (that would erase the playhead). We overlay on top.
    ctx.save()
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)

    // Resolve the hovered target track index from live pointerY.
    let targetIdx = -1
    for (let i = 0; i < tracks.length; i++) {
      const ty = trackY(i)
      if (drag.pointerY >= ty && drag.pointerY <= ty + trackHeight) { targetIdx = i; break }
    }
    const originIdx = tracks.findIndex((t) => t.id === drag.trackId)

    if (drag.kind === 'move') {
      const destIdx = targetIdx >= 0 ? targetIdx : originIdx
      const destTrack = tracks[destIdx]
      const originType = tracks[originIdx]?.type
      const destType = destTrack?.type
      // Lane compatibility (mirrors onMouseUp): a media clip may only follow
      // onto a media-family lane; a sticker/text only onto its own type.
      const originIsMedia = originType ? laneAcceptsMediaClip(originType) : true
      const compatible = destType
        ? (originIsMedia ? laneAcceptsMediaClip(destType) : destType === originType)
        : true

      // 1) Drop-target row wash.
      if (destIdx >= 0) {
        const ty = trackY(destIdx)
        ctx.fillStyle = compatible ? dv.DROP_OK : dv.DROP_BAD
        ctx.fillRect(labelWidth, ty, contentW - labelWidth, trackHeight)
        ctx.strokeStyle = compatible ? dv.ACCENT : '#ff4d6d'
        ctx.lineWidth = 1
        ctx.strokeRect(labelWidth + 0.5, ty + 0.5, contentW - labelWidth - 1, trackHeight - 1)
      }

      // Compute the raw (cursor-following) and snapped (landing) starts.
      const rawLeftX = drag.pointerX - drag.offsetX
      const rawStart = Math.max(0, (rawLeftX - labelWidth) / zoom)
      const durSec = drag.origOut - drag.origIn
      let landStart = rawStart
      let overlapping = false
      if (compatible && destTrack && drag.clipKind === 'media') {
        overlapping = dragResolve.wouldOverlap(destTrack, durSec, rawStart, drag.clipId)
        landStart = dragResolve.snapToFreeGap(destTrack, durSec, rawStart, drag.clipId)
      }

      if (destIdx >= 0 && compatible) {
        const ty = trackY(destIdx)
        // 2) Overlap tint at the raw (pre-snap) position, if it would collide.
        if (overlapping) {
          ctx.fillStyle = dv.OVERLAP_TINT
          ctx.fillRect(labelWidth + rawStart * zoom, ty + 4, Math.max(2, durSec * zoom), trackHeight - 8)
        }
        // 3) Landing / insertion line at the snapped start.
        const lx = labelWidth + landStart * zoom
        ctx.strokeStyle = dv.ACCENT
        ctx.lineWidth = dv.INSERTION_W
        ctx.beginPath(); ctx.moveTo(lx, ty); ctx.lineTo(lx, ty + trackHeight); ctx.stroke()
      }

      // 4) Drag ghost at the raw cursor position, on the destination row.
      const ghostIdx = destIdx >= 0 ? destIdx : originIdx
      if (ghostIdx >= 0) {
        const gy = trackY(ghostIdx)
        const gx = labelWidth + rawStart * zoom
        const gw = Math.max(2, durSec * zoom)
        ctx.globalAlpha = dv.GHOST_ALPHA
        ctx.fillStyle = TRACK_COLORS[tracks[ghostIdx].type] ?? dv.ACCENT
        roundRect(ctx, gx, gy + 4, gw, trackHeight - 8, 4); ctx.fill()
        ctx.globalAlpha = 1
        ctx.strokeStyle = dv.ACCENT
        ctx.lineWidth = dv.DRAG_BORDER_W
        roundRect(ctx, gx, gy + 4, gw, trackHeight - 8, 4); ctx.stroke()
      }
    } else if (drag.kind === 'trim-l' || drag.kind === 'trim-r') {
      // Edge-drag: live edge line + mode/result label.
      const oi = originIdx >= 0 ? originIdx : 0
      const ty = trackY(oi)
      const dt = (drag.pointerX - (drag.startX - (canvasRef.current!.getBoundingClientRect().left))) / zoom
      const side = drag.kind === 'trim-l' ? 'l' : 'r'
      let edgeSec: number
      let label: string
      if (drag.clipKind === 'media' && drag.modifier) {
        const sourceDur = drag.origOut - drag.origIn
        const factor = dragResolve.resolveMediaSpeed(sourceDur, 1, side, dt)
        const footprint = sourceDur / factor
        edgeSec = side === 'l' ? drag.origStart : drag.origStart + footprint
        label = `speed ${factor.toFixed(2)}× · ${footprint.toFixed(2)}s`
      } else if (drag.clipKind === 'media') {
        const r = dragResolve.resolveMediaTrim({ in: drag.origIn, out: drag.origOut }, side, dt)
        const footprint = r.out - r.in
        edgeSec = side === 'l' ? drag.origStart + (r.in - drag.origIn) : drag.origStart + footprint
        label = `trim · ${footprint.toFixed(2)}s`
      } else {
        const r = dragResolve.resolveOverlayTiming({ start: drag.origStart, end: drag.origStart + (drag.origOut - drag.origIn) }, side, dt)
        edgeSec = side === 'l' ? r.start : r.end
        label = `${(side === 'l' ? r.start : r.start).toFixed(2)}s → ${(side === 'l' ? (drag.origStart + (drag.origOut - drag.origIn)) : r.end).toFixed(2)}s`
      }
      const ex = labelWidth + Math.max(0, edgeSec) * zoom
      ctx.strokeStyle = dv.ACCENT
      ctx.lineWidth = dv.DRAG_BORDER_W
      ctx.beginPath(); ctx.moveTo(ex, ty); ctx.lineTo(ex, ty + trackHeight); ctx.stroke()
      ctx.fillStyle = dv.ACCENT
      ctx.font = '10px var(--font-ui)'
      ctx.fillText(label, ex + 4, ty + 12)
    }
    ctx.restore()
  }, [dragTick, tracks, zoom, contentW, dpr])
```

Note: the edge-drag label is a live PREVIEW approximation — it uses the raw (un-snapped) pointer `dt` and assumes speed=1 for the speed factor. The AUTHORITATIVE values are recomputed in Task 7's release handler using the snapped `edgeDelta` and the clip's actual current speed, so for an already-retimed clip the committed factor can differ slightly from the mid-drag label. This is expected, not a bug — the label is a hint, the release is the source of truth. (For the text-clip label both branches intentionally show start→end.) If you prefer exactness, factor the label math out of `dragResolve` too; not required for this plan.

- [ ] **Step 4: Clear the drag chrome when a drag ends — force a playhead-effect repaint**

The playhead effect repaints (and clearRects) whenever `playhead/zoom/contentW/contentH/dpr` change, but a drag ending changes none of those. Add a repaint trigger: in Task 7 the release handler already calls `setDragTick`. For this task, add — at the END of the drag-overlay effect's dependency-free path — nothing; the cleanup is: after a drag ends, `dragRef.current` is null so this effect early-returns, but the last-drawn chrome remains until the playhead effect re-clears. To force an immediate clear, bump the playhead effect: change the playhead effect's dependency array from `[playhead, zoom, contentW, contentH, dpr]` to `[playhead, zoom, contentW, contentH, dpr, dragTick]` so the final `setDragTick` on release re-runs the clearing playhead draw.

Concretely, edit `Timeline.tsx:476`:
```ts
  }, [playhead, zoom, contentW, contentH, dpr])
```
to:
```ts
  }, [playhead, zoom, contentW, contentH, dpr, dragTick])
```

- [ ] **Step 5: Build and verify live in the running app**

Run: `cd frontend && npx tsc --noEmit && npx vite build`
Expected: no new errors; build succeeds.

Then restart the desktop app to pick up the new bundle (per CLAUDE.md the packaged/desktop app does NOT hot-reload):
```bash
pkill -f video_ai_editor.desktop; nohup bash run.sh > /tmp/vae_run.log 2>&1 & disown
```
Manually verify (drag a clip on the timeline): the clip ghost follows the cursor at ~60% opacity with a blue border; the hovered track row gets a blue wash; a blue insertion line shows where it will land; dragging over the captions/text row turns the wash red. Dragging onto an occupied span shows an amber tint at the cursor and the insertion line at the free gap. (Release still behaves as before — rewired next task.)

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/Timeline.tsx
git commit -m "feat: live drag ghost + drop-target + insertion + overlap overlay on timeline"
```

---

## Task 7: Timeline — kind-aware release

**Files:**
- Modify: `frontend/src/components/Timeline.tsx:624-705` (onMouseUp)

- [ ] **Step 1: Rewrite the trim branches of `onMouseUp` to route by clip kind**

Replace the `trim-l` / `trim-r` branches (`Timeline.tsx:692-704`):
```ts
    } else if (drag.kind === 'trim-l') {
      // Trim-left snaps the visible clip start (= origStart + delta) to neighbors.
      const newClipStart = snapTime(drag.origStart + dt, drag.clipId)
      const deltaApplied = newClipStart - drag.origStart
      const newIn = Math.max(0, drag.origIn + deltaApplied)
      await dispatch('trim_clip', { clip_id: drag.clipId, in: newIn })
    } else if (drag.kind === 'trim-r') {
      const origRight = drag.origStart + (drag.origOut - drag.origIn)
      const newRight = snapTime(origRight + dt, drag.clipId)
      const deltaApplied = newRight - origRight
      const newOut = Math.max(drag.origIn + 0.1, drag.origOut + deltaApplied)
      await dispatch('trim_clip', { clip_id: drag.clipId, out: newOut })
    }
```
with:
```ts
    } else if (drag.kind === 'trim-l' || drag.kind === 'trim-r') {
      const side: 'l' | 'r' = drag.kind === 'trim-l' ? 'l' : 'r'
      // The signed edge delta AFTER snapping the moved edge to neighbors.
      // side='l' → the edge is origStart; side='r' → the edge is origEnd. In
      // both cases `edgeDelta` is (snapped new edge position − old edge
      // position), exactly the `deltaSec` contract every resolve* function
      // expects (positive = edge moved right).
      const origEnd = drag.origStart + (drag.origOut - drag.origIn)
      const edgeDelta = side === 'l'
        ? snapTime(drag.origStart + dt, drag.clipId) - drag.origStart
        : snapTime(origEnd + dt, drag.clipId) - origEnd
      if (drag.clipKind === 'text' || drag.clipKind === 'sticker') {
        // Overlay clips have no source to trim — edge-drag retimes the window
        // (start/end) via set_clip_timing. resolveOverlayTiming clamps end>start.
        const r = dragResolve.resolveOverlayTiming(
          { start: drag.origStart, end: origEnd }, side, edgeDelta)
        if (side === 'l') await dispatch('set_clip_timing', { clip_id: drag.clipId, start: r.start })
        else await dispatch('set_clip_timing', { clip_id: drag.clipId, end: r.end })
      } else if (drag.modifier) {
        // Alt + edge-drag on media = speed retime (keep whole source, change
        // the timeline footprint). resolveMediaSpeed handles the side→footprint
        // sign internally, so pass the raw edgeDelta. Clip speed defaults to 1
        // (types.ts omits `speed`; read via the repo's cast pattern).
        const sourceDur = drag.origOut - drag.origIn
        const c = hits.find((h) => h.clip.id === drag.clipId)?.clip
        const curSpeed = (c as unknown as { speed?: number | null })?.speed ?? 1
        const factor = dragResolve.resolveMediaSpeed(sourceDur, curSpeed, side, edgeDelta)
        await dispatch('set_speed', { clip_id: drag.clipId, factor })
      } else {
        // Plain media edge-drag = trim (show less/more of the source). Reuse
        // the same snapped `edgeDelta`; resolveMediaTrim clamps out>in, in>=0.
        const r = dragResolve.resolveMediaTrim({ in: drag.origIn, out: drag.origOut }, side, edgeDelta)
        if (side === 'l') await dispatch('trim_clip', { clip_id: drag.clipId, in: r.in })
        else await dispatch('trim_clip', { clip_id: drag.clipId, out: r.out })
      }
    }
```

- [ ] **Step 2: Trigger overlay clear on release**

At the very top of `onMouseUp`, right after `dragRef.current = null` (`Timeline.tsx:629`), add:
```ts
    setDragTick((n) => n + 1)  // repaint overlay (clears drag chrome)
```
(Placed after the `const drag = dragRef.current` capture and the null-set; `drag` is already saved to the local.)

- [ ] **Step 3: Build and verify tsc**

Run: `cd frontend && npx tsc --noEmit && npx vite build`
Expected: no new errors; build succeeds.

- [ ] **Step 4: Live verification (restart app first)**

```bash
pkill -f video_ai_editor.desktop; nohup bash run.sh > /tmp/vae_run.log 2>&1 & disown
```
Verify each in the running app:
- Drag a text clip's right edge to span the whole video → the caption's end extends, NO error toast (previously errored).
- Alt/Option + drag a video's right edge → the clip's timeline footprint stretches/compresses; playing back confirms the video is slowed/sped.
- Plain-drag a video edge → still trims (shows less/more of source).
- Drag a clip onto an occupied range → snaps to the free gap + the existing "Snapped to the nearest free gap" toast.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/Timeline.tsx
git commit -m "feat: kind-aware timeline edge-drag (media trim / Alt-speed / overlay retime)"
```

---

## Task 8: Timeline — panel-drop insertion preview + Escape-cancel

**Files:**
- Modify: `frontend/src/components/Timeline.tsx` (onCanvasDragOver ~708-715; add Escape keydown effect; add drag-over state)

- [ ] **Step 1: Add a native-DnD hover state**

After the `dragTick`/`dragRafRef` lines added in Task 6, add:
```ts
  // Live drop position for a native HTML5 drag from the media/sticker panels
  // (separate from pointer-drag `dragRef`). null when no panel drag is over
  // the canvas. Drives the same insertion-line/target-row overlay.
  const dndOverRef = useRef<null | { x: number; y: number }>(null)
```

- [ ] **Step 2: Update `onCanvasDragOver` to record position + request redraw**

Replace `onCanvasDragOver` (`Timeline.tsx:708-715`):
```ts
  function onCanvasDragOver(e: React.DragEvent) {
    if (e.dataTransfer.types.includes('application/x-vai-src')
        || e.dataTransfer.types.includes('application/x-vai-emoji')
        || e.dataTransfer.types.includes('text/plain')) {
      e.preventDefault()
      e.dataTransfer.dropEffect = 'copy'
    }
  }
```
with:
```ts
  function onCanvasDragOver(e: React.DragEvent) {
    if (e.dataTransfer.types.includes('application/x-vai-src')
        || e.dataTransfer.types.includes('application/x-vai-emoji')
        || e.dataTransfer.types.includes('text/plain')) {
      e.preventDefault()
      e.dataTransfer.dropEffect = 'copy'
      const rect = (e.currentTarget as HTMLElement).getBoundingClientRect()
      dndOverRef.current = { x: e.clientX - rect.left, y: e.clientY - rect.top }
      if (dragRafRef.current == null) {
        dragRafRef.current = requestAnimationFrame(() => {
          dragRafRef.current = null
          setDragTick((n) => n + 1)
        })
      }
    }
  }
  function onCanvasDragLeave() {
    dndOverRef.current = null
    setDragTick((n) => n + 1)
  }
```

Then add `onDragLeave={onCanvasDragLeave}` to the wrapper div (`Timeline.tsx:919-920`), and clear `dndOverRef` at the top of `onCanvasDrop` (add `dndOverRef.current = null` right after its `e.preventDefault()` at `Timeline.tsx:718`).

- [ ] **Step 3: Draw the panel-drop insertion preview in the drag-overlay effect**

In the drag-overlay effect from Task 6, change the early-return guard so it also runs for a native DnD hover. Replace:
```ts
    const drag = dragRef.current
    const cv = playheadCanvasRef.current
    if (!cv || !drag || drag.kind === 'playhead') return
```
with:
```ts
    const drag = dragRef.current
    const dnd = dndOverRef.current
    const cv = playheadCanvasRef.current
    if (!cv) return
    if (dnd && (!drag || drag.kind === 'playhead')) {
      // Native panel drag: draw target-row wash + insertion line at snapTime.
      const ctx2 = cv.getContext('2d')!
      ctx2.save(); ctx2.setTransform(dpr, 0, 0, dpr, 0, 0)
      let ti = -1
      for (let i = 0; i < tracks.length; i++) {
        const ty = trackY(i)
        if (dnd.y >= ty && dnd.y <= ty + trackHeight) { ti = i; break }
      }
      if (ti >= 0 && dnd.x > labelWidth) {
        const ty = trackY(ti)
        ctx2.fillStyle = dv.DROP_OK
        ctx2.fillRect(labelWidth, ty, contentW - labelWidth, trackHeight)
        const lx = labelWidth + Math.max(0, (dnd.x - labelWidth) / zoom) * zoom
        ctx2.strokeStyle = dv.ACCENT; ctx2.lineWidth = dv.INSERTION_W
        ctx2.beginPath(); ctx2.moveTo(lx, ty); ctx2.lineTo(lx, ty + trackHeight); ctx2.stroke()
      }
      ctx2.restore()
      return
    }
    if (!drag || drag.kind === 'playhead') return
```
(The rest of the effect — the pointer-drag branch — is unchanged. Add `dragTick` already covers redraws; keep the dependency array as `[dragTick, tracks, zoom, contentW, dpr]`.)

- [ ] **Step 4: Add the Escape-cancel keydown effect**

Add this effect after the drag-overlay effect:
```ts
  // Escape cancels an in-progress clip drag with NO commit (mousedown captured
  // state, but we simply drop it and repaint to clear the ghost). Only active
  // while a non-playhead drag is live.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== 'Escape') return
      const drag = dragRef.current
      if (!drag || drag.kind === 'playhead') return
      dragRef.current = null
      setDragTick((n) => n + 1)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
```

- [ ] **Step 5: Build + verify tsc**

Run: `cd frontend && npx tsc --noEmit && npx vite build`
Expected: no new errors; build succeeds.

- [ ] **Step 6: Live verification (restart app)**

```bash
pkill -f video_ai_editor.desktop; nohup bash run.sh > /tmp/vae_run.log 2>&1 & disown
```
Verify: dragging a clip from the Media bin over the timeline shows a blue target-row wash + insertion line following the cursor before you drop; dropping lands the clip there. Starting a clip drag then pressing Escape leaves the clip unchanged (no ghost stuck, no commit).

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/Timeline.tsx
git commit -m "feat: panel-drop insertion preview + Escape-cancel for timeline drags"
```

---

## Task 9: StickerLayer — dragging distinction + resize cursors

**Files:**
- Modify: `frontend/src/components/StickerLayer.tsx` (imports; draw loop ~143-159; hover cursor ~219-231)

- [ ] **Step 1: Import the shared visual constants**

Add to the imports at the top of `StickerLayer.tsx`:
```ts
import * as dv from '../lib/dragVisuals'
```

- [ ] **Step 2: Give the selection box a distinct "dragging" style**

Replace the selection-chrome block (`StickerLayer.tsx:143-159`):
```ts
        if (sk.id === selection) {
          const h = g.size / 2
          ctx.save()
          ctx.translate(g.cx, g.cy)
          ctx.rotate(g.rot)
          ctx.globalAlpha = 1
          ctx.strokeStyle = '#5b8dff'
          ctx.lineWidth = 1.5
          ctx.setLineDash([4, 3])
          ctx.strokeRect(-h, -h, g.size, g.size)
          ctx.setLineDash([])
          ctx.fillStyle = '#5b8dff'
          for (const [sx, sy] of [[-1, -1], [1, -1], [1, 1], [-1, 1]] as const) {
            ctx.fillRect(sx * h - HANDLE, sy * h - HANDLE, HANDLE * 2, HANDLE * 2)
          }
          ctx.restore()
        }
```
with:
```ts
        if (sk.id === selection) {
          const h = g.size / 2
          const isDragging = dragRef.current?.id === sk.id
          ctx.save()
          ctx.translate(g.cx, g.cy)
          ctx.rotate(g.rot)
          ctx.globalAlpha = 1
          ctx.strokeStyle = dv.ACCENT
          if (isDragging) {
            // Solid, thicker box + soft shadow while actively dragging/resizing
            // — visually distinct from the resting dashed selection box.
            ctx.lineWidth = dv.DRAG_BORDER_W
            ctx.setLineDash([])
            ctx.shadowColor = 'rgba(0,0,0,0.5)'
            ctx.shadowBlur = 8
          } else {
            ctx.lineWidth = 1.5
            ctx.setLineDash([4, 3])
          }
          ctx.strokeRect(-h, -h, g.size, g.size)
          ctx.setLineDash([])
          ctx.shadowBlur = 0
          ctx.fillStyle = dv.ACCENT
          for (const [sx, sy] of [[-1, -1], [1, -1], [1, 1], [-1, 1]] as const) {
            // Highlight the corner being resized (brighter/larger).
            const active = isDragging && dragRef.current?.mode === 'resize'
            const pad = active ? HANDLE + 1 : HANDLE
            ctx.fillRect(sx * h - pad, sy * h - pad, pad * 2, pad * 2)
          }
          ctx.restore()
        }
```

- [ ] **Step 3: Add per-corner resize cursors in the hover branch**

Replace the hover-cursor block inside `onMove` when there is no active drag (`StickerLayer.tsx:222-231`):
```ts
      if (!d) {
        // Hover cursor feedback.
        const { px, py } = posOf(e)
        const t = now()
        const hit = interactableStickers(t).some((sk) => {
          const g = geomFor(sk, t)
          const { lx, ly } = toLocal(px, py, g)
          return Math.abs(lx) <= g.size / 2 && Math.abs(ly) <= g.size / 2
        })
        cv.style.cursor = hit ? 'move' : 'default'
        return
      }
```
with:
```ts
      if (!d) {
        // Hover cursor feedback: resize cursor over a selected sticker's corner
        // handle, move cursor over any sticker body, default otherwise.
        const { px, py } = posOf(e)
        const t = now()
        const sel = stateRef.current.selection
        const stickers = interactableStickers(t)
        let cursor = 'default'
        const selSk = stickers.find((s) => s.id === sel)
        if (selSk) {
          const g = geomFor(selSk, t)
          const { lx, ly } = toLocal(px, py, g)
          const h = g.size / 2
          for (const [sx, sy] of [[-1, -1], [1, -1], [1, 1], [-1, 1]] as const) {
            if (Math.hypot(lx - sx * h, ly - sy * h) <= HANDLE_HIT) {
              cursor = dv.cursorForCorner(sx, sy)
              break
            }
          }
        }
        if (cursor === 'default') {
          const overBody = stickers.some((sk) => {
            const g = geomFor(sk, t)
            const { lx, ly } = toLocal(px, py, g)
            return Math.abs(lx) <= g.size / 2 && Math.abs(ly) <= g.size / 2
          })
          if (overBody) cursor = 'move'
        }
        cv.style.cursor = cursor
        return
      }
```

- [ ] **Step 4: Build + verify tsc**

Run: `cd frontend && npx tsc --noEmit && npx vite build`
Expected: no new errors; build succeeds.

- [ ] **Step 5: Live verification (restart app)**

```bash
pkill -f video_ai_editor.desktop; nohup bash run.sh > /tmp/vae_run.log 2>&1 & disown
```
Verify in the running app: select a sticker on the preview → dashed blue box. Start dragging it → the box becomes solid with a shadow (visibly "picked up"). Hover a corner handle → the cursor becomes a diagonal resize cursor; drag it → the resized corner handle is highlighted.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/StickerLayer.tsx
git commit -m "feat: distinct dragging box + per-corner resize cursors for preview stickers"
```

---

## Task 10: Optional backend invariant test + full verification sweep

**Files:**
- Create (optional): `tests/test_set_clip_timing.py`

- [ ] **Step 1: (Optional) Add the `set_clip_timing` invariant regression**

Only if judged non-redundant with `test_all_tools_smoke` (see spec §7.2). Create `tests/test_set_clip_timing.py`:
```python
"""set_clip_timing enforces a positive window and rejects media clips.

This is the invariant the timeline's kind-aware edge-drag (text/sticker →
set_clip_timing) now relies on: a left-edge (start) drag past the end, or a
right-edge (end) drag before the start, must never produce end <= start.
"""
from __future__ import annotations
import tempfile
from pathlib import Path

import pytest

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Canvas, Track, TextClip
from video_ai_editor.agent.dispatch import dispatch


def _store_with_text() -> EDLStore:
    tmp = tempfile.mkdtemp()
    edl = EDL(
        canvas=Canvas(w=1080, h=1920, fps=30),
        tracks=[Track(id="tx_super", type="text",
                      clips=[TextClip(id="t1", text="hi", start=5.0, end=8.0)])],
    )
    edl.recompute_duration()
    (Path(tmp) / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(Path(tmp))


def test_left_edge_past_end_clamps_to_positive_span():
    store = _store_with_text()
    dispatch(store, "set_clip_timing", {"clip_id": "t1", "start": 100.0})
    _, c = store.edl.get_clip("t1")
    assert c.end > c.start


def test_end_before_start_clamps_to_positive_span():
    store = _store_with_text()
    dispatch(store, "set_clip_timing", {"clip_id": "t1", "end": 1.0})
    _, c = store.edl.get_clip("t1")
    assert c.end > c.start


def test_start_clamped_non_negative():
    store = _store_with_text()
    dispatch(store, "set_clip_timing", {"clip_id": "t1", "start": -10.0})
    _, c = store.edl.get_clip("t1")
    assert c.start >= 0.0
```

- [ ] **Step 2: Run the backend test**

Run: `uv run pytest tests/test_set_clip_timing.py -v`
Expected: PASS (3 tests). If it fails, the backend clamp regressed — investigate before proceeding (should not happen; verified present at `dispatch.py:2372-2377`).

- [ ] **Step 3: Run the full frontend gate (matches CI)**

Run: `cd frontend && npx vitest run && npx tsc --noEmit && npx vite build`
Expected: unit tests pass; no new tsc errors; build succeeds.

- [ ] **Step 4: Run the backend suite to confirm no regression**

Run: `uv run pytest -q`
Expected: all pass (or the same pre-existing skips as baseline).

- [ ] **Step 5: Cross-platform / cross-browser verification sweep**

Per spec §6, verify parity across surfaces:
- **macOS packaged app** (`bash run.sh`): run the full interactive checklist from Tasks 6–9 (ghost, drop-target, overlap tint + snap, Alt-speed, text-stretch, panel-drop preview, Escape-cancel, sticker dragging box + resize cursors).
- **Browser-dev** (`cd frontend && npm run dev`, open `http://localhost:5173`): repeat the same interactive checklist to confirm the identical code path works in Chrome/Safari.
- **Windows (WebView2):** rely on the `windows-latest` CI job for the backend + build gate; the drag code is pure canvas/DOM using `MouseEvent`/`altKey`/HTML5 DnD (all WebView2-supported). Note in the PR description that the interactive canvas checks were done on macOS + browser-dev, and Windows shares the identical bundle.
- Use the `verify` skill / Playwright (`uv run playwright install chromium` already documented) for an automated smoke of the browser path if desired.

- [ ] **Step 6: Commit (if the optional test was added)**

```bash
git add tests/test_set_clip_timing.py
git commit -m "test: set_clip_timing positive-window invariant (backs timeline overlay retime)"
```

- [ ] **Step 7: Revert any incidental `uv.lock` churn before finishing**

Per project memory, `uv run` can silently rewrite `uv.lock`. Check and revert if it drifted:
```bash
git checkout uv.lock 2>/dev/null || true
git status --short
```
Expected: no unintended `uv.lock` modification in the final diff.

---

## Self-Review notes (author)

- **Spec §3 (live move feedback):** Tasks 5, 6 (ghost, drop-target, insertion, overlap). ✓
- **Spec §3.4/§4 (kind-aware resize):** Tasks 5 (kind capture), 6 (edge label preview), 7 (release routing to trim/set_speed/set_clip_timing). ✓
- **Spec §3.6 (Escape / cleanup):** Task 8 (Escape), Tasks 6/7 (overlay clear via dragTick repaint). ✓
- **Spec §5.1 (sticker dragging distinction + cursors):** Task 9. ✓
- **Spec §5.2 (dragVisuals):** Task 2. ✓
- **Spec §6 (cross-platform):** Task 10 Step 5 sweep; MouseEvent/altKey/HTML5-DnD choices honored (no PointerEvent migration for Timeline). ✓
- **Spec §6.2 (panel-drop insertion via onDragOver coords):** Task 8. ✓
- **Spec §7.1 (Vitest for dragResolve):** Tasks 1, 3, 4. ✓
- **Spec §7.2 (optional backend test):** Task 10. ✓
- **Type consistency:** `dragResolve` signatures identical across Tasks 3/4/6/7; `dv.*` constants identical across Tasks 2/6/9; extended `dragRef` shape (Task 5) matches all reads in Tasks 6/7. `speed` read via the repo's cast pattern.
- **No backend source change** (verified `set_clip_timing`/`set_speed`/`trim_clip` already clamp) — plan touches only tests + frontend + CI.
