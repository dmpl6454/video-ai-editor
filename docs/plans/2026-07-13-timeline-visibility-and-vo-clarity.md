# Timeline Visibility + VO Clarity — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. **These are layout/UX bugs — `tsc`/`vite build`/`pytest` will NOT catch them. Every task must be verified by driving the real app at multiple window heights via Playwright (`browser_resize` + computed-geometry assertions), not by reading code.**

**Goal:** Fix the regression where the timeline disappears at certain window sizes, and stop the voiceover UI from *looking* broken when it actually works (a stale error message + the invisible timeline combine to read as "nothing works"). Also document how to launch the app so VO can be tested where it's meant to run.

**Architecture:** CSS-grid layout (`.center` = `1fr 6px var(--timeline-h)` rows, `overflow:hidden`); the timeline is a fixed-px last row. The bug is that when the window is shorter than `preview-min-content + 6 + timeline-h`, the grid either collapses the `1fr` preview to 0 (timeline overflows off-screen bottom, clipped) or pushes the fixed timeline row past the clip boundary — either way one pane vanishes. No JS crash involved (verified: only console noise is a favicon 404).

**Tech stack:** React/TS/Zustand, CSS grid, Playwright for verification.

**Baseline:** Root causes below were all confirmed by LIVE reproduction on 2026-07-13 (after commit `fee6a22`), not inferred from the screenshot.

---

## Root causes (all reproduced live)

| # | Symptom | Root cause (verified live) |
|---|---|---|
| 1 | Timeline pane completely gone; preview fills everything | `.center` grid is `1fr 6px var(--timeline-h)` with `overflow:hidden`. When window height < (preview min-content + 6 + `--timeline-h`), CSS grid can't satisfy both the `1fr` preview's implicit min-content and the fixed timeline row. **Reproduced:** at 1400×420 with `--timeline-h:640`, grid computed to `0px 6px 640px` — preview collapsed to 0, timeline 640px overflowed to y=690 past the 420px viewport, clipped by `overflow:hidden`. The user's variant (timeline gone, preview huge) is the same failure at a different `--timeline-h`/window-height ratio. The `1fr` preview row lacks `min-height:0`, so it won't yield, and `--timeline-h` is a rigid px with no responsive cap. |
| 2 | "Can't record OR import voiceover" in browser | **Import actually WORKS** — verified live: uploading a real WAV via the Import button took the vo track from 1→2 clips, no error, a real ffprobe-valid clip landed. The *record* path fails in browser only because Chrome has the mic permission blocked/dismissed for this origin (not our bug — Chrome UI). BUT: the red "Microphone access was blocked…" error from a failed record attempt **persists on screen** (only cleared on the next record/import *start*, VoRecorder.tsx:99/277), and with the timeline invisible (bug #1) the user can't see the imported clip land — so a working import *looks* like it failed. Compounding perception bug, not a functional one. |
| 3 | "How do I run the application?" | `bash run.sh` (uses `PYTHONPATH=src` + `.venv/bin/python -m video_ai_editor.desktop`, per CLAUDE.md's hidden-.pth workaround). The `dist/Video AI Editor.app` bundle is currently empty/stale; the packaged bundle is rebuilt with `uv run bash build_app.sh`. |

---

## Task 0 (answer, no code): How to run the app

**Two ways:**
1. **Dev / native window (fastest, recommended for testing VO):**
   ```bash
   cd /Users/tabish/Desktop/dashmani-ai-editor
   bash run.sh
   ```
   This launches the in-process backend + a native pywebview window (the real WKWebView, where VO's native-capture path and TCC prompt actually apply). This is the environment to test packaged-app VO behavior without a full PyInstaller build.
2. **Packaged `.app` (distribution artifact):**
   ```bash
   uv run bash build_app.sh          # produces dist/Video AI Editor.app (signed, entitled)
   open "dist/Video AI Editor.app"
   ```
   Use this to test the fully-frozen bundle + hardened-runtime signing.

**Browser-dev mode** (`localhost:5173`) is NOT where packaged-app VO should be judged — getUserMedia there depends on Chrome's per-site mic permission, which is what's currently blocking the user's record attempts (reset via the address-bar site-settings icon → Microphone → Allow → reload).

---

## Task 1: Fix the disappearing timeline (the real bug)

**Files:**
- `frontend/src/styles.css` (`.center` ~145-153, `.preview-pane` ~337, `.timeline-pane` ~371)
- Possibly `frontend/src/store.ts` (`--timeline-h` clamp / responsive cap)
- Test: Playwright at multiple window heights

**Root cause:** `1fr` preview row has no `min-height:0`, and `--timeline-h` is a rigid px with no cap relative to available height. Under `overflow:hidden`, a short window clips one pane out entirely.

**Step 1: Reproduce live FIRST (before editing).** Playwright: set `localStorage['vai.timelineH']='640'`, resize to 1400×420, reload, read `getComputedStyle('.center').gridTemplateRows` → confirm it computes to `0px 6px 640px` (preview collapsed, timeline overflowing). Also test the user's variant: a mid-size `--timeline-h` (e.g. 280) at a short height where the timeline's bottom exceeds the viewport → confirm the timeline pane's `getBoundingClientRect().bottom > window.innerHeight` (clipped).

**Step 2: Make the layout responsive.** Two complementary changes:

(a) **Cap the timeline row to a fraction of available height** so it can never exceed what fits. Change `.center`'s grid so the timeline row is `minmax(0, ...)` and bounded — e.g.:
```css
.center {
  grid-template-rows: minmax(120px, 1fr) 6px minmax(0, var(--timeline-h, 280px));
  /* preview gets minmax(120px,1fr): always keeps ≥120px but yields the rest;
     timeline gets minmax(0, var): takes up to its stored px but shrinks below
     that when the window can't fit it, instead of overflowing the clip box. */
}
```
The critical part is `minmax(0, ...)` on the timeline track (lets it shrink below `--timeline-h` when space is tight) and a floored `minmax(120px, 1fr)` on preview (keeps preview usable but yields space). Confirm the exact values by live testing at several heights.

(b) **Add `min-height: 0` to `.preview-pane`** (and confirm `.timeline-pane` too) so the flex/grid children can actually shrink rather than forcing overflow:
```css
.preview-pane { min-height: 0; }
.timeline-pane { min-height: 0; }
```

**Step 3: Clamp `--timeline-h` responsively (defense-in-depth).** Even with (a)+(b), a persisted `timelineH:640` on a 500px window is silly. Consider clamping the *effective* `--timeline-h` at render time in `App.tsx` to `min(storedTimelineH, viewportHeight - somePreviewFloor)`, or rely purely on the CSS `minmax(0,...)` if live testing shows that fully solves it. Prefer the CSS-only solution if it works (simpler, no JS resize listener needed) — test first, add JS clamp only if CSS alone leaves an edge case.

**Step 4: Verify live at MULTIPLE heights (the verification that matters).** Playwright, for each of e.g. 900px, 700px, 560px, 460px, 400px window heights (and both `timelineH:280` and `timelineH:640`):
- Assert `.timeline-pane` is visible: `rect.height > 0 && rect.bottom <= window.innerHeight + 1 && rect.top >= centerTop`.
- Assert `.preview-pane` is visible and ≥ its floor.
- Assert neither is clipped out by `.center`'s `overflow:hidden` (both rects fully within the center's rect).
- Screenshot at the smallest height to eyeball it.
This is the test that proves the regression is gone — a single-height check would miss it (it only manifests when short).

**Step 5:** Commit. `git commit -m "fix(layout): timeline no longer clipped out of view on short windows (responsive grid rows + min-height:0)"`.

---

## Task 2: Stop the VO UI from looking broken when it works

**Files:** `frontend/src/components/VoRecorder.tsx`

**Root cause:** A failed *record* leaves a persistent red error; *import* works but its result (a) was invisible due to bug #1, and (b) doesn't visibly clear the stale record error until its own start. Perception, not function.

**Step 1: Clear the error the moment import succeeds AND show a success confirmation.** In the import path (`importFile`, ~line 277+), after a successful upload+dispatch, explicitly `setError(null)` on success (not just at start) and show a brief success toast ("Voiceover imported ✓") so the user gets positive feedback even if the timeline is scrolled/small. (The app has a `toast` util already used elsewhere.)

**Step 2: Make the record-failure error dismissible / auto-clearing.** The blocked-mic error at line 221 is accurate but sticky. Add a dismiss affordance (an × on the error) or auto-clear it when the user successfully imports or when they retry. Minimal: clear `error` at the start of `importFile` too (so clicking Import wipes a prior record error immediately), and confirm it's cleared on a successful record.

**Step 3: Reduce confusion between "record" and "import".** Since browser record depends on Chrome mic permission (outside our control) but import always works, consider making the import button visually primary/more prominent when a record error is showing, with a one-liner like "Recording blocked? Import an audio file instead →". This turns a dead-end into a clear next step.

**Step 4: Verify live.** Playwright: (a) simulate a blocked getUserMedia (deny mic permission in the browser context) → click Record → confirm the error shows; (b) then click Import and upload a file → confirm the error is GONE, a success toast appears, and the vo clip lands. Assert the error text is no longer in the DOM after a successful import.

**Step 5:** Commit. `git commit -m "fix(vo): clear stale record error + confirm on successful import (import worked, only looked broken)"`.

---

## Task 3: Confirm packaged-app VO + timeline fix end-to-end (rebuild + hand-off)

**Files:** none (verification + rebuild)

**Step 1:** Rebuild the bundle so it contains Task 1 + Task 2 fixes: `rm -rf "dist/Video AI Editor.app" && uv run bash build_app.sh`. Confirm the build succeeds, the mic entitlement + hardened runtime are still present (`codesign -d --entitlements -`), and the bundled frontend JS contains the new layout CSS / VO strings.

**Step 2:** Launch via `bash run.sh` (dev native window — faster than the full bundle and exercises the same WKWebView + native VO path). At a deliberately short window height, confirm the timeline is visible (Task 1). Then click Record Voiceover and confirm the macOS mic-permission prompt appears attributed to the app — **this interactive mic step still requires the human**; ship the code and hand this final click-through to the user, being explicit about what was and wasn't verified.

**Step 3:** No commit (verification only), or commit any doc update.

---

## Verification discipline

- Task 1 is the one most likely to be "fixed" falsely — it ONLY reproduces at short window heights, so the verification MUST resize the window across a range, not test once at a comfortable size. Use `browser_resize` + geometry assertions.
- Standard gate after: `cd frontend && npx tsc --noEmit && npx vite build && npm run lint`; `uv run pytest`; revert `uv.lock` drift; remove `.playwright-mcp/` scratch.
- **Local commits only, no push.**

---

## Scope note

The VO *record* failure in browser is a Chrome per-site permission state (reset in site settings), not a code bug — Task 2 makes that recoverable/clear rather than "fixing" it. The packaged-app native VO capture (from prior round) is unchanged here; this plan only ensures the timeline is visible so users can *see* VO results, and that a working import doesn't read as broken.
