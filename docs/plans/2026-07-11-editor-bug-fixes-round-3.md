# Editor Bug-Fixes Round 3 — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. **Every task here is UI/interaction- or platform-gated — `tsc`/`vite build`/`pytest` passing does NOT prove any of these work. Each task's verification MUST drive the real running app (browser-dev mode via Playwright at :5173, and where noted the packaged `.app`). Do not mark a task done on a green typecheck alone — that is exactly the mistake that shipped these as "fixed" when they weren't.**

**Goal:** Make four reported-as-still-broken items actually work: (1) panel resizing, (2) a clear Save-vs-Export story plus real export file/format options, (3) voiceover recording in the packaged app, (4) all show/hide/collapse toggles.

**Architecture:** Frontend is React 19 / Zustand / CSS-grid layout; backend is FastAPI + ffmpeg render pipeline; the desktop app is pywebview (WKWebView) with a Python `js_api` bridge already present in `desktop.py`. No architectural rewrite. Route new backend work through the existing `dispatch`/render paths and `platformutil.py`.

**Tech Stack:** React/TS/Zustand/CSS-grid, FastAPI/Pydantic, ffmpeg (H.264/AAC/mp4 today), Pillow, PyInstaller + pywebview 6.2.1 (pyobjc Cocoa backend), whisper.

**Baseline note:** All line numbers verified against the working tree on 2026-07-11 (after Round-2 commits `bedbc97`..`eed3933`). The Round-2 code for panels/export/VO is PRESENT and, for panels, byte-identical in the shipped `dist/Video AI Editor.app` bundle (confirmed via md5) — so these are **not** stale-build problems. They are genuine functional/UX/platform gaps.

---

## Root-cause summary (what the investigation actually found)

| # | Report | Root cause (verified) | Nature |
|---|---|---|---|
| 1 | "Stretching of windows does not work whatsoever" | Splitter code is fully wired and correct end-to-end (DOM handle → `onDelta` → `setPanelSize` → CSS var → grid). BUT `.splitter { background: transparent }` and the handle is only **5px** wide, invisible until `:hover` (`styles.css:127-130`). Users cannot find/grab a 5px transparent strip on the pane seam → "nothing happens." | UX/discoverability |
| 2a | "What is the difference between save and export" | Save (`saveProject` → `.vae` ZIP of state+media, reopenable) vs Export (`doExport` → flattened MP4). Both work; the distinction is just **unlabeled** in the UI. | UX/clarity |
| 2b | "Export must also have file options" | Popover exposes **resolution + quality only**. No container/codec choice (mp4/mov/webm/gif). fps is plumbed in api.ts + backend but not surfaced. **Critically: the Quality (crf) selector is a NO-OP on this Mac** — export uses `h264_videotoolbox`, and `crf` is only honored on the libx264 fallback (`compositor.py:_video_encoder_args`). | Feature + real bug |
| 3 | "Record voiceover still doesn't work" | Info.plist entitlement (done) is necessary but **not sufficient**. Two more blockers: **(B1)** pywebview's Cocoa backend implements no WKWebView `requestMediaCapturePermissionForOrigin` delegate → WKWebView denies `getUserMedia` by default; **(B2)** app is served over `http://127.0.0.1:8765`, which WKWebView does not treat as a secure context → `navigator.mediaDevices` is likely `undefined`. | Platform |
| 4 | "Show tab and hide tab doesn't work" (user: "it's for all") | No control literally named that. The two real toggles (Stickers `▶` disclosure `StickerPanel.tsx:83-90`; Chat `×`/pill `ChatOverlay.tsx:109-119`) are **correctly wired in code** — failure is runtime/visual (likely CSS overflow-clipping the expanded content, or off-screen at the bottom of the left sidebar). Must reproduce live to pin down. | Runtime/CSS (needs live repro) |

---

## Task 1: Make resize handles discoverable + verify they actually resize

**Files:**
- Modify: `frontend/src/styles.css:127-130` (splitter visibility/width)
- Verify-only (already correct): `frontend/src/App.tsx:65-105`, `frontend/src/components/Splitter.tsx`, `frontend/src/store.ts:199-203`
- Test: Playwright drag against browser-dev mode

**Root cause:** The drag logic works; the handle is a 5px fully-transparent strip (invisible until hover). This is why it reads as "does not work whatsoever."

**Step 1: Reproduce the CURRENT (broken-feeling) state live — do this BEFORE editing**

Start browser-dev mode:
- Terminal A: `uv run uvicorn video_ai_editor.main:app --reload --reload-dir src --port 8000`
- Terminal B: `cd frontend && npm run dev` (serves :5173, proxies /api → :8000)

Then with Playwright: navigate to `http://localhost:5173`, take a snapshot, and try to locate a `div.splitter` element. Confirm it exists in the DOM (`browser_evaluate`: `document.querySelectorAll('.splitter').length` → expect 3). Confirm it's visually invisible (computed `background-color` is transparent). This proves the "can't find the handle" hypothesis.

**Step 2: Make the handle visible + widen the hit target**

In `styles.css:127-130`, replace:
```css
.splitter { background: transparent; z-index: 5; flex-shrink: 0; }
.splitter:hover, .splitter:active { background: var(--accent-2, #5b8dff); }
.splitter-vertical { width: 5px; height: 100%; cursor: col-resize; }
.splitter-horizontal { width: 100%; height: 5px; cursor: row-resize; }
```
with a design that has a persistent faint affordance, a wider *hit* area than *visual* width (via transparent padding / a wider track), and a clear hover state. Suggested:
```css
.splitter {
  /* faint always-visible seam so users can find it; brightens on hover */
  background: var(--line, #2a2a33);
  z-index: 5; flex-shrink: 0;
  position: relative;
}
.splitter::after {                 /* a subtle grip dots/line, centered */
  content: ''; position: absolute; inset: 0;
  background: transparent;
}
.splitter:hover, .splitter:active { background: var(--accent-2, #5b8dff); }
/* Visual width stays slim, but the grid tracks were 5px — widen to 6px and
   rely on cursor + color; if still hard to grab, bump the grid track + this to 8px. */
.splitter-vertical   { width: 6px; height: 100%; cursor: col-resize; }
.splitter-horizontal { width: 100%; height: 6px; cursor: row-resize; }
```
If 6px still feels thin in live testing, also bump the grid tracks in `.app` (`styles.css:42`, the two `5px` column tracks) and `.center` (`styles.css:116`, the `5px` row track) to match.

**Step 3: Verify the drag ACTUALLY resizes (the verification that was skipped last time)**

With Playwright against :5173:
1. `browser_evaluate` to read the left sidebar's width: `getComputedStyle(document.querySelector('.sidebar.left')).width` → record (expect ~220px).
2. Drag the left vertical splitter right by ~80px: use `browser_drag` from the splitter's center to a point +80px in x (or `browser_evaluate` dispatching mousedown/mousemove/mouseup on `.splitter-vertical` if drag helper is imprecise).
3. Re-read the sidebar width → assert it grew by ~80px (allowing for the 160-640 clamp).
4. Repeat for the right splitter (drag left → right sidebar grows) and the horizontal splitter (drag up → timeline grows).
5. Reload the page → assert the widths persisted (localStorage: `vai.leftW` etc.).

If any axis does NOT resize, THEN there's a real wiring bug (sign error, wrong CSS var, clamp) — debug per systematic-debugging. Per static analysis all three are wired, so the expected outcome is: after Step 2 they're findable and all three resize.

**Step 4: Commit**

```bash
git add frontend/src/styles.css
git commit -m "fix(layout): make resize handles visible/grabbable (were 5px transparent)"
```

---

## Task 2: Clarify Save vs Export + add real export file options (and fix the no-op quality knob)

Three sub-parts. **2a** (labeling) and **2c** (crf no-op) are small and high-value; **2b** (container/codec) is the larger feature.

### Task 2a: Label the Save vs Export distinction in the UI

**Files:** `frontend/src/components/TopBar.tsx` (Save button ~306, Export ~322), optionally `styles.css` for a tooltip.

**Root cause:** Both work; users don't know Save = editable `.vae` project, Export = final MP4.

**Step 1:** Add `title=` tooltips (and/or small sublabels) so intent is unmistakable:
- Save button: `title="Save an editable project file (.vae) you can reopen later"`.
- Export button: `title="Render the final flattened video (MP4) to share"`.
- The `↓ .vae` link already implies "download the saved project"; ensure its `title` says so.

**Step 2:** Verify live (Playwright): hover each, confirm tooltip text. Manual visual is fine — this is cosmetic.

**Step 3:** Commit: `git commit -m "docs(ui): tooltips clarifying Save (.vae project) vs Export (MP4)"`.

### Task 2c: Make the export Quality selector actually do something on macOS (real bug)

**Files:** `src/video_ai_editor/render/compositor.py` — `_hw_encoder_args` (~148) and `_video_encoder_args` (~128).

**Root cause:** On Mac, export uses `h264_videotoolbox` with a fixed `-q:v`; the popover's crf (High/Med/Small) is ignored because crf is only wired to libx264. So the Quality control is a silent no-op for the user.

**Step 1: Write a failing test** in `tests/` asserting that a lower crf maps to a higher-quality VideoToolbox `-q:v` (VideoToolbox `-q:v` is 0-100, higher=better; so map crf 18/23/28 → q:v ~65/50/35, i.e. an inverse mapping). Test `_hw_encoder_args("h264_videotoolbox", preview=False, crf=28)` yields a lower `-q:v` than `crf=18`.

**Step 2:** Thread `crf` into `_hw_encoder_args` and add a crf→quality mapping per HW encoder:
- videotoolbox `-q:v`: `q = round(map_crf_to_q(crf))` where e.g. `map_crf_to_q(crf) = clamp(0,100, 100 - (crf-14)*2.5)` (tune so 18→~90-ish high, 28→~65). Pick sensible endpoints and document them.
- nvenc/qsv/amf: map crf onto their `-cq`/`-global_quality`/`-qp` (roughly identity for cq/qp; document approximations). Keep it best-effort with a comment — a perfect cross-encoder mapping is out of scope, but "the knob visibly changes output" is the bar.
- `_video_encoder_args` must pass `crf` through to `_hw_encoder_args` (currently it only passes crf to the libx264 branch).

**Step 3:** Verify: run an actual export at crf 18 vs 28 on this Mac and confirm the output file sizes differ meaningfully (larger at 18). This requires driving `doExport` — do it via the API directly (`POST /api/sessions/{sid}/export?wait=1` with different `crf` in the body) against a short test timeline, comparing `os.path.getsize` of the two outputs. Assert size(crf=18) > size(crf=28) by a clear margin.

**Step 4:** Commit: `git commit -m "fix(export): honor quality/crf on hardware encoders (was libx264-only, no-op on Mac)"`.

### Task 2b: Add MOV as a second container option (scope confirmed with user — mp4 + mov only, no webm/gif this round)

**Files:**
- `src/video_ai_editor/render/compositor.py` — `render_export` (~806) + the hardcoded `.mp4` (compositor.py:667, 818).
- `src/video_ai_editor/main.py` — `ExportRequest` (~175) add a `container: Literal["mp4","mov"] = "mp4"` field; `render_export` call site.
- `frontend/src/components/TopBar.tsx` — add a Format `<select>` (MP4/MOV) to the popover; `frontend/src/store.ts:106` — widen `doExport` type to forward `container`.
- `frontend/src/api.ts` — widen the export types.
- Test: `tests/` asserting each container produces a valid, ffprobe-confirmed file.

**Root cause:** Pipeline is hardwired to `.mp4`. User confirmed scope: add MOV (same H.264/AAC codecs, different container/extension — cheap) and explicitly SKIP webm/gif this round (real encoder work, deferred). fps stays out of scope too (not requested).

**Step 1:** Thread a `container` param ("mp4"|"mov") through `render_export` → `_render`. Both containers use the identical H.264/AAC encoder args and `-movflags +faststart` (both are QuickTime-family containers — no codec branching needed, only the output extension changes). Replace the hardcoded `.mp4` suffix (compositor.py:667, 818) with `f"export_{h}.{container}"`.

**Step 2:** Add the Format `<select>` (MP4 / MOV) to the popover in `TopBar.tsx`; forward `container` from `confirmExport` → `doExport` (widen the store type at store.ts:106) → `api.exportAsync` → `ExportRequest.container`.

**Step 3: Verify both formats live** — export the same short timeline as mp4 and mov via the API (`wait=1`), assert each output exists, is non-empty, and `ffprobe` reports `mov,mp4,m4a,3gp,3g2,mj2` format either way (both are ISO-BMFF family) but the file extension and container `major_brand` differ appropriately. This is the real verification; a passing unit test that only checks the filename suffix is NOT sufficient.

**Step 4:** Commit: `git commit -m "feat(export): add MOV container option alongside MP4"`.

---

## Task 3: Voiceover recording in the packaged app — bypass getUserMedia via native capture

**Files:**
- `src/video_ai_editor/desktop.py` — extend the existing `_Api` js_api bridge (~83-141, added in `2ebf5cc`) with native mic capture.
- `frontend/src/components/VoRecorder.tsx` — when in pywebview (and getUserMedia unavailable), route Record through the bridge instead of showing the dead-end message.
- Reuse: `POST /api/sessions/{sid}/vo_record` (`main.py:305-355`) — already accepts an audio blob and drops a `vo`-track clip.
- Test: manual in the packaged `.app` (this is the ONLY environment that reproduces the bug — browser-dev mode's getUserMedia works fine and does NOT exercise the failure).

**Root cause (both must be solved for in-window getUserMedia; the chosen approach sidesteps both):**
- B1: pywebview's Cocoa `BrowserDelegate` implements no `webView:requestMediaCapturePermissionForOrigin:...` → WKWebView denies capture.
- B2: served over `http://127.0.0.1:8765` (not a WKWebView secure context) → `navigator.mediaDevices` likely `undefined`.

**Chosen approach — native capture via the js_api bridge (avoids WKWebView media entirely; most robust):**

**Step 1: Add a native mic-record method to `_Api` in `desktop.py`.**
Two implementation options — pick based on what's reliably available:
- **(preferred) ffmpeg avfoundation:** `ffmpeg -f avfoundation -i ":<audio_device_index>" -t <maxdur> -y <tmp.wav>`. Probe the default input device via `ffmpeg -f avfoundation -list_devices true -i ""`. Route the binary through `platformutil` (`FFMPEG`) and decode stderr with `encoding="utf-8", errors="replace"` (Windows footgun, even though this path is mac-first).
- **(alt) Python `sounddevice`:** record to a numpy buffer → write WAV. Adds a dep; only if avfoundation proves flaky.

The `_Api` methods should be: `vo_start()` (begin recording to a session temp WAV, non-blocking — spawn the ffmpeg process, store the handle), and `vo_stop()` (terminate ffmpeg gracefully, return the temp WAV path). Mirror the existing `save_export` error-handling style. Since this runs on the pywebview main thread via the bridge, ensure the ffmpeg process is spawned non-blocking (Popen) and stopped by sending `q`/SIGINT so the WAV finalizes cleanly.

**Step 2:** After `vo_stop()`, either (a) have Python POST the WAV to `vo_record` itself and return success, or (b) return the path/bytes to JS and let `VoRecorder.tsx` upload via the existing `api.voRecord`. Option (a) keeps it all native and avoids re-reading a file into JS; recommend (a) — `_Api.vo_stop()` reads the WAV, calls the same code path `vo_record` uses (import the handler or POST to localhost), and returns `{clip_id}`.

**Step 3:** In `VoRecorder.tsx`, detect pywebview + missing getUserMedia and switch modes:
```ts
const py = (window as any).pywebview?.api
if (py?.vo_start && py?.vo_stop) {
  // native path: Record button calls py.vo_start(); Stop calls py.vo_stop();
  // then refresh() so the new vo clip appears. No getUserMedia.
} else if (navigator.mediaDevices?.getUserMedia) {
  // existing browser-dev path (unchanged)
} else {
  // existing dead-end message (only if truly neither available)
}
```
Keep the current getUserMedia path for browser-dev mode (where it works).

**Step 4: Rebuild and verify in the packaged app (the only valid test):**
```bash
rm -rf "dist/Video AI Editor.app" && uv run bash build_app.sh
open "dist/Video AI Editor.app"
```
Then: click Record Voiceover, grant the macOS mic prompt (now that the entitlement + native capture path exist), speak, Stop → assert a clip appears on the `vo` track and plays back. **This must be done in the `.app`, not browser-dev mode.** If you cannot run the packaged app interactively in this environment, STOP and hand back to the user to test, clearly stating the code is in place but unverified in the packaged runtime.

**Step 5:** Commit: `git commit -m "feat(vo): native mic capture via js_api bridge (getUserMedia unusable in WKWebView)"`.

**Note on `build_app.sh`:** it only runs `npm run build` if `frontend/dist` is *missing* (`build_app.sh:17`). To guarantee the packaged app reflects current frontend, either delete `frontend/dist` before building or change the guard to always rebuild in dev. Add this as a hardening step so future rebuilds don't silently ship a stale frontend. (Not the cause of the current panel bug — the bundle was confirmed current — but a latent footgun.)

---

## Task 4: Audit and fix all show/hide/collapse toggles

**Files:**
- `frontend/src/components/StickerPanel.tsx:46,47-58,83-90` (Stickers `▶` disclosure)
- `frontend/src/components/ChatOverlay.tsx:26,107-119` (Chat `×`/pill)
- `frontend/src/styles.css` (overflow on `.sidebar.left` / `.media-bin`)
- Test: Playwright against browser-dev mode

**Root cause:** Both toggles are correctly wired at the click→state→render level. The user says "it's for all," so audit every toggle and fix whatever actually breaks at runtime. Prime suspect: expanded Stickers content is clipped/pushed off-screen at the bottom of the left sidebar (a CSS `overflow`/height issue), so clicking `▶` appears to do nothing.

**Step 1: Reproduce each toggle live (BEFORE editing).** With Playwright at :5173:
- Stickers: click the "▶ 😀 Stickers" row. `browser_evaluate` whether the emoji grid element appears in the DOM AND is visible (has non-zero height, is within the viewport, not clipped by an ancestor's `overflow:hidden`). If it's in the DOM but `getBoundingClientRect()` is off-screen or zero-height → it's the CSS-clipping bug.
- Chat: click `×` → assert the panel hides and the pill appears; click the pill → assert it reopens. Record whether either fails.

**Step 2: Fix whatever the repro reveals.** Likely fixes:
- If Stickers content is clipped: give `.sidebar.left`/`.media-bin` a scrollable overflow (`overflow-y:auto`) and ensure the expanded grid can push content or scroll into view. Possibly `scrollIntoView` on expand.
- If a toggle genuinely no-ops: debug the specific handler per systematic-debugging (the static read says they're wired, so a runtime cause like an event-swallowing parent, a pointer-events issue, or an outside-click handler at `StickerPanel.tsx:47-58` immediately re-closing it is the likely culprit — check whether the outside-click listener fires on the same click that opened it).

**Step 3: Verify each toggle live after the fix** — Stickers expands and its grid is fully visible/scrollable; Chat `×` hides and pill reopens. Playwright-assert both.

**Step 4:** Commit: `git commit -m "fix(ui): make Stickers disclosure + chat toggle reliably show/hide"`.

### Task 4b: Add a right-panel (Properties/History) hide/show toggle (scope confirmed with user — new control, not just fixing existing ones)

**Files:**
- `frontend/src/App.tsx` (right `<aside>` ~line 96-99)
- `frontend/src/store.ts` (new `rightPanelOpen` boolean + toggle action, persisted like `leftW`/`rightW`)
- `frontend/src/components/TopBar.tsx` or a small collapse tab affixed to the right sidebar's edge (CapCut-style: a thin always-visible tab on the sidebar's outer edge that toggles it)
- `frontend/src/styles.css` — collapsed-state width (0 or a thin rail) + transition
- Test: Playwright

**Design:** A CapCut-style collapse: when open, the right sidebar behaves as today (resizable via Task 1's splitter). When collapsed, it shrinks to ~0 (or a slim rail with just a re-expand affordance) and the center pane reclaims the space. Persist the open/closed state the same way panel widths are persisted (localStorage via the store).

**Step 1:** Add `rightPanelOpen: boolean` (default `true`) and `setRightPanelOpen(open: boolean)` to `store.ts`, persisted like `leftW`/`rightW` (`localStorage.setItem('vai.rightPanelOpen', ...)`).

**Step 2:** Add a small collapse/expand tab — a narrow button fixed to the outer edge of `.sidebar.right` (or integrated into the Properties header), toggling `rightPanelOpen`. When collapsed, set `--right-w` to a minimal rail width (e.g. `28px`, just enough for the re-expand tab) instead of hiding the grid column entirely (avoids reflow jumps); the Properties/OpsLog content inside is hidden via `display:none` or `visibility:hidden` while collapsed so it doesn't render uselessly.

**Step 3:** Make sure this doesn't fight Task 1's resize splitter — when collapsed, the splitter for the right panel should either hide or be a no-op (dragging a collapsed 28px rail shouldn't un-collapse it via drag; only the explicit toggle button should re-expand).

**Step 4: Verify live** — Playwright: click the collapse tab, assert the right sidebar's computed width drops to the rail width and Properties/OpsLog are not visible; click again, assert it returns to its prior width; reload, assert the collapsed/open state persisted.

**Step 5:** Commit: `git commit -m "feat(layout): add right-panel collapse/expand toggle"`.

---

## Cross-cutting: how to verify (do NOT repeat the Round-2 mistake)

Round 2 marked panels and VO "done" on `tsc`/`vite build`/`pytest` — none of which exercise a drag, a mic, or a WKWebView. For this round:

- **Interaction features (Tasks 1, 4, and the UI half of 2):** drive browser-dev mode (`:5173`) with Playwright — real pointer events, real DOM assertions on computed styles/positions. Use the `verify` skill.
- **Render/format features (Task 2b/2c):** drive the actual export via the API with `wait=1` and inspect the output files with `ffprobe`/`getsize`. A filename-suffix unit test is not proof.
- **VO (Task 3):** the packaged `.app` is the ONLY valid environment. Browser-dev mode's getUserMedia works and will give a false pass. If interactive `.app` testing isn't possible in-session, ship the code and explicitly hand verification to the user.

Then the usual gate (won't catch these bugs but must still pass): `cd frontend && npx tsc --noEmit && npx vite build && npm run lint`; `uv run pytest`; revert any `uv.lock` drift (`git checkout -- uv.lock`).

---

## Scope decisions (confirmed with user 2026-07-11)

1. **Task 2b:** MP4 + MOV only. WebM/GIF/fps explicitly deferred (real encoder work, lower value this round).
2. **Task 3:** Native ffmpeg capture via the js_api bridge (not the WKWebView delegate/TLS route).
3. **Task 4:** Fix the two existing toggles (Stickers, Chat) AND add a new right-panel collapse/expand toggle (Task 4b).

---

## Out of scope (explicitly deferred)

- WebM/GIF export containers and fps control (Task 2b — deferred to a future round).
- Real per-encoder CRF parity across all HW encoders (Task 2c does a best-effort mapping so the knob visibly works; a calibrated 1:1 is deferred).
- Rewriting pywebview's native delegate for in-window getUserMedia (Task 3 sidesteps it via native capture instead).
- Making `build_app.sh` a full incremental frontend build system (just fix the stale-frontend guard).
- A left-panel collapse toggle (only right-panel was requested).
