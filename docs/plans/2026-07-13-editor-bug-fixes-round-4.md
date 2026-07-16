# Editor Bug-Fixes Round 4 — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. **Every task here is interaction- or platform-gated. `tsc`/`vite build`/`pytest` passing does NOT prove any of these work — that exact mistake has now shipped "fixed" features that were still broken three times.** Verification MUST drive the real running app: Playwright pointer events against the `:5173` dev server for UI, `ffprobe` on real renders for export, and the packaged `.app` for VO (VO's whole point is the packaged app — browser-dev mode gives a false pass).

**Goal:** Fix the real bugs from the 2026-07-13 testing session — timeline vertical scroll, timeline click-to-seek, cross-track drag visual merge, double subtitle (still open), Windows VO fallback — and make **voiceover recording actually work in the packaged macOS app, by any means necessary** (this has failed repeatedly; this plan treats it as the headline item with a multi-strategy approach, not one fragile path). Browser and packaged app must have identical, correct behavior.

**Architecture:** Frontend React 19 / Zustand / canvas Timeline; backend FastAPI + ffmpeg; desktop is pywebview (WKWebView on mac, WebView2 on Windows) with a Python `js_api` bridge in `desktop.py`. VO in the packaged app cannot use `getUserMedia` (WKWebView has no media-capture delegate + non-secure-context origin), so it goes through a native capture bridge; the reliability problem is macOS TCC granting a **subprocess** mic access under an ad-hoc-signed, non-hardened-runtime bundle.

**Tech Stack:** React/TS/Zustand/canvas, FastAPI/Pydantic, ffmpeg (avfoundation capture on mac), PyInstaller + pywebview 6.2.1 (pyobjc Cocoa backend), Playwright for UI verification.

**Baseline:** Line numbers verified against the working tree on 2026-07-13 (after Round-3 commits through `a7f3f4a`). Confirmed live in the current session's EDL that the double-subtitle bug is real and open (`tx_super` has `"RIPPLE TEST"` and `"RIPPLE TEST 2"`, both `start=16.0 end=18.0`).

---

## Issue → Root cause → Task map

| # | Report | Root cause (verified) | Task | Nature |
|---|---|---|---|---|
| 1 | VO "must work in the app, any means necessary" | Packaged app uses native ffmpeg-avfoundation bridge (`desktop.py` `_Api.vo_start/vo_stop`); it's well-built but its reliability hinges on macOS TCC granting a **subprocess** mic access under an ad-hoc-signed, no-hardened-runtime, no-entitlements bundle — which is the classic "works in code, denied silently in the real app" trap. | Task 1 | Platform (headline) |
| 2 | VO broken on Windows | `vo_start()` correctly returns error on non-mac, but `VoRecorder.tsx` routes to the bridge on *method presence* and never falls back to getUserMedia — so Windows packaged builds always error instead of using WebView2's working getUserMedia. | Task 2 | Bug (Windows) |
| 3 | "Permission dismissed" in browser | Not our code — Chrome's own native permission-prompt UI when a mic prompt is dismissed (not blocked). No string like it exists in the repo. | Task 3 | Non-bug (doc/UX) |
| 4 | Timeline vertical scroll "not at all times" | Wheel handler hijacks ALL deltaY-dominant wheel events into horizontal `scrollLeft` with `preventDefault()` (`Timeline.tsx` onWheel). Vertical scroll only reachable via scrollbar-drag or an accidentally-horizontal trackpad gesture. Also `contentH = Math.max(size.h, …)` can transiently equal a stale viewport height right after a panel resize. Identical browser/app. | Task 4 | Bug |
| 5 | Click timeline rows doesn't seek | `setPlayhead` only called when click y < 24px ruler, or within 5px of the playhead. Track-row clicks select/deselect, never seek. Identical browser/app. | Task 5 | Bug |
| 6 | Cross-track drag "merges" clips | No time-overlap check in frontend drop or backend `move_clip` (only clip-type). Both clips survive in EDL (no data loss); canvas draws them identical-fill with no z-order/outline, so they look fused. | Task 6 | Bug (visual) |
| 7 | Double subtitle persists | Prior fix (88fc48b) only dedupes EXACT matches (same text AND same start/end). Two clips with different text ("RIPPLE TEST" vs "RIPPLE TEST 2") at identical time on the same track both pass and stack. `TextLayer.tsx` has zero collision handling. | Task 7 | Bug (still open) |
| 8 | Export popover "grows exponentially" | Could NOT reproduce — every dimension is literal px, no inherited units, popover renders 198×23px normally when driven live. User's screenshot shows a `--no-sandbox` Chrome (special/automation launch). Not a code bug; would not occur in the packaged app. | Task 8 | Non-bug (verify + doc) |
| — | "Export must truly work + save" | Verified end-to-end: real ffprobe-valid MP4 produced; browser `<a download>` fires a real download event; packaged-app native save dialog wired (`store.ts` → `pywebview.api.save_export`). Working. | (covered by Task 1's rebuild + a verify step) | Already works |

**Recommended order:** Task 1 (VO, headline, hardest) → Tasks 4/5/6 (timeline, shared file, sequential) → Task 7 (double subtitle) → Task 2 (Windows VO) → Tasks 3/8 (non-bugs: doc + confirm). Each is independently shippable.

---

## Task 1: Make VO recording actually work in the packaged macOS app (any means necessary)

**Files:**
- `src/video_ai_editor/desktop.py` — `_Api.vo_start`/`vo_stop`, `_avfoundation_default_audio_index`, window creation
- `build_app.sh` — signing/entitlements/hardened-runtime
- Possibly add: `entitlements.plist` (repo root)
- `frontend/src/components/VoRecorder.tsx` — surface actionable errors + a "reveal mic settings" affordance
- Test: **the packaged `.app`, interactively** (the only valid environment)

**Root cause of the recurring failure:** The native avfoundation capture code is correct, but macOS **TCC (privacy) grants microphone access per "responsible process."** The app is **ad-hoc signed** (`codesign -s -`), with **no hardened runtime and no entitlements file** (`.spec` has `entitlements_file=None`; only `NSMicrophoneUsageDescription` is added to Info.plist). Under these conditions, when the app spawns `ffmpeg` as a subprocess to hit the mic, TCC's attribution of that request is unreliable — it can be silently denied with no prompt, producing exactly the "recording produced no audio" / "still doesn't work" symptom. **This is why the entitlement alone never fixed it.**

**Strategy — belt-and-suspenders, three layers so at least one path works:**

### Task 1a: Trigger the TCC prompt from the APP process, not (only) the ffmpeg subprocess

**Step 1: Pre-authorize the mic via AVFoundation from the Python/app process before spawning ffmpeg.**
On `vo_start` (mac branch), before launching ffmpeg, request mic authorization through pyobjc so the prompt is attributed to the app bundle itself:
```python
def _ensure_mic_authorized_mac() -> tuple[bool, str]:
    """Request macOS mic authorization from the APP process (via AVFoundation)
    so TCC attributes the prompt to this bundle, then the ffmpeg subprocess
    inherits the granted permission. Returns (authorized, detail)."""
    try:
        import AVFoundation  # pyobjc-framework-avfoundation
        from Foundation import NSObject
    except Exception as e:
        return True, f"AVFoundation unavailable ({e}); relying on subprocess prompt"
    AVMediaTypeAudio = "soun"
    status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio)
    # 0 notDetermined, 1 restricted, 2 denied, 3 authorized
    if status == 3:
        return True, "already authorized"
    if status in (1, 2):
        return False, "microphone access denied — enable it in System Settings › Privacy & Security › Microphone"
    # notDetermined → request synchronously (block on a semaphore for the async callback)
    import threading
    result = {"granted": False}
    done = threading.Event()
    def _cb(granted):
        result["granted"] = bool(granted); done.set()
    AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(AVMediaTypeAudio, _cb)
    done.wait(timeout=30)
    return (result["granted"],
            "granted" if result["granted"] else "user dismissed or denied the mic prompt")
```
Add `pyobjc-framework-avfoundation` to `pyproject.toml` deps if not already present (pyobjc-core/cocoa are; confirm avfoundation is pulled in — add it explicitly). Call `_ensure_mic_authorized_mac()` at the top of `vo_start`'s mac branch; if it returns `False`, return `{"ok": False, "error": <detail>}` so the UI shows an actionable message instead of a silent empty WAV.

> Why this matters: requesting via `AVCaptureDevice` from the app process makes TCC show the prompt attributed to "Video AI Editor" and persist the grant for the bundle; the child ffmpeg then inherits it. This is the single highest-leverage fix for the recurring failure.

### Task 1b: Sign with hardened runtime + the mic entitlement (makes TCC attribution deterministic)

**Step 2: Add an entitlements file and re-sign the bundle with hardened runtime.**
Create `entitlements.plist` at repo root:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>com.apple.security.device.audio-input</key>
  <true/>
  <!-- PyInstaller apps need these under hardened runtime to run at all -->
  <key>com.apple.security.cs.allow-jit</key>
  <true/>
  <key>com.apple.security.cs.allow-unsigned-executable-memory</key>
  <true/>
  <key>com.apple.security.cs.disable-library-validation</key>
  <true/>
</dict>
</plist>
```
In `build_app.sh`, after the PlistBuddy step, re-sign the bundle with hardened runtime + entitlements (ad-hoc identity is fine for local use — the entitlements + runtime flags are what matter for TCC, not a paid cert):
```bash
codesign --force --deep --options runtime \
  --entitlements entitlements.plist \
  --sign - "dist/Video AI Editor.app" \
  || echo "[build] WARNING: codesign failed — VO mic access may be denied by TCC"
```
Note the existing PyInstaller BUNDLE-stage codesign warning ("resource fork … not allowed") — the explicit re-sign here supersedes it. If `--deep` trips on nested resource-fork detritus, add a `xattr -cr "dist/Video AI Editor.app"` step before signing to strip extended attributes (documented macOS codesign fix).

### Task 1c: Make the raw-avfoundation path robust + fail loud

**Step 3:** Harden `_avfoundation_default_audio_index()` — it currently returns `"0"` on any parse failure. Confirm it actually parses the "audio devices" section (the current sed showed the function body may be truncated mid-implementation — read it fully and ensure it returns the first *audio* device index, not a video one). Prefer the device literally named like "Microphone" / "Built-in" if present, else index 0.

**Step 4:** In `vo_stop`, the "recording produced no audio" branch currently returns a generic error. Capture ffmpeg's stderr (already piped) and include a tail in the error when the WAV is empty, so a TCC denial vs a device-index mismatch vs a genuine no-audio are distinguishable. Surface this to `VoRecorder.tsx`'s error display.

### Task 1d: Verify in the real packaged app (the only valid test)

**Step 5: Rebuild and test interactively.**
```bash
rm -rf "dist/Video AI Editor.app" && uv run bash build_app.sh
open "dist/Video AI Editor.app"
```
Then: click Record Voiceover → **a macOS mic-permission prompt attributed to "Video AI Editor" must appear** (first run) → grant it → speak → Stop → a real audio clip must land on the `vo` track and play back. Confirm the recorded clip via the session's `uploads/vo/` dir + `ffprobe`.

**This step requires a human at the keyboard** (clicking through a native OS permission dialog and speaking into a mic cannot be automated from this harness). If executing in a non-interactive context: complete Tasks 1a-1c, rebuild, confirm the entitlement + signing landed (`codesign -d --entitlements - "dist/Video AI Editor.app"` shows `com.apple.security.device.audio-input`), confirm the AVFoundation authorization code is in the bundle, and **explicitly hand the final interactive mic test to the user** — do not claim VO works without the click-through.

**Step 6:** Commit. `git commit -m "fix(vo): app-process TCC mic authorization + hardened-runtime entitlement so packaged-app recording actually works"`.

**Fallback if 1a-1c still don't grant access on the user's machine:** offer a "record via the browser-dev mode and import" escape hatch in the error message, and/or a file-picker "import an audio file as voiceover" affordance (the `/vo_record` endpoint already accepts an uploaded blob — a plain file input wired to `api.voRecord` would let the user attach any recording). Document this as the guaranteed-working path if native capture is refused by TCC.

---

## Task 2: Fix Windows VO fallback (route to getUserMedia when native bridge can't serve)

**Files:** `frontend/src/components/VoRecorder.tsx` (bridge-detection branch ~line 95-118)

**Root cause:** `if (py?.vo_start && py?.vo_stop)` routes to the native bridge based on method *existence*. On Windows the bridge exists but `vo_start` returns `{ok: false, error: "native mic capture is only implemented on macOS"}` — and the code shows that error instead of falling back to getUserMedia (which works in WebView2).

**Step 1:** When the native `vo_start` returns `ok: false` with the macOS-only error (or any "not implemented" signal), fall through to the getUserMedia path instead of surfacing the error. Cleanest: have `vo_start` return a distinguishable code (e.g. `{ok: false, unsupported: true}`) on the non-mac branch, and in `VoRecorder.tsx`:
```ts
if (py?.vo_start && py?.vo_stop) {
  const res = await py.vo_start(sid)
  if (res?.ok) { nativeRecordingRef.current = true; /* ...native path... */; return }
  if (!res?.unsupported) { setError(res?.error || 'Could not start native mic recording.'); return }
  // unsupported on this platform (e.g. Windows) → fall through to getUserMedia below
}
// ...existing getUserMedia path (works in WebView2 on Windows)...
```
Add `unsupported?: boolean` to the bridge type and set it in `desktop.py`'s non-mac branch.

**Step 2: Verify.** Can't test Windows here — write it Windows-safe by construction, confirm tsc/build clean, and confirm the macOS path still works (native bridge, `ok: true`, no regression). Note in the report that the Windows getUserMedia fallback is code-reviewed, not Windows-executed.

**Step 3:** Commit. `git commit -m "fix(vo): fall back to getUserMedia on Windows instead of erroring on the mac-only native bridge"`.

---

## Task 3: "Permission dismissed" — documentation/UX, not a code bug

**Files:** `frontend/src/components/VoRecorder.tsx` (optional UX nicety)

**Root cause:** Chrome's own native UI when a mic prompt is dismissed (not blocked). Nothing in our code produces it.

**Step 1 (optional, low priority):** In browser-dev mode, when getUserMedia rejects with `NotAllowedError`, show a more actionable message: "Microphone blocked or dismissed for this site. Click the 🔒/tune icon in the address bar → Site settings → Microphone → Allow, then reload." This turns a confusing browser-chrome moment into a self-serve fix.

**Step 2:** No verification needed beyond confirming the message renders on a `NotAllowedError`. Commit if done: `git commit -m "fix(vo): clearer message when the browser blocks/dismisses the mic prompt"`.

---

## Task 4: Timeline vertical scroll (stop hijacking the vertical wheel)

**Files:** `frontend/src/components/Timeline.tsx` (onWheel ~708-728, contentH ~111)

**Root cause:** Plain vertical wheel is converted to horizontal `scrollLeft` with `preventDefault()`; vertical scroll only works via scrollbar or a horizontal-dominant trackpad gesture.

**Step 1: Reproduce live first.** Playwright at :5173: with more track rows than fit, dispatch a `wheel` event with `deltaY: 120` over the timeline canvas and confirm `.timeline-canvas-wrap.scrollTop` does NOT change (proving the hijack), while `scrollLeft` does.

**Step 2: Redesign the wheel mapping.** The intent (plain wheel → horizontal pan, like NLEs) conflicts with needing vertical scroll when there are more rows than fit. Resolve by making vertical scroll win when there's vertical overflow:
```ts
function onWheel(e: React.WheelEvent) {
  if (e.ctrlKey || e.metaKey) { e.preventDefault(); setZoomStore(zoom * (e.deltaY < 0 ? 1.15 : 1/1.15)); return }
  const wrap = wrapRef.current
  if (!wrap) return
  const canScrollV = wrap.scrollHeight > wrap.clientHeight
  // Shift+wheel → always horizontal (explicit pan gesture)
  if (e.shiftKey) { e.preventDefault(); wrap.scrollLeft += e.deltaY || e.deltaX; return }
  // If content overflows vertically and the gesture is vertical-dominant, let the
  // native vertical scroll happen (do NOT preventDefault) so lower rows are reachable.
  if (canScrollV && Math.abs(e.deltaY) >= Math.abs(e.deltaX)) { return /* native vertical scroll */ }
  // Otherwise map vertical wheel → horizontal pan (timeline convention when no V overflow).
  if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) { e.preventDefault(); wrap.scrollLeft += e.deltaY }
}
```
(Adjust to taste: some NLEs use plain wheel = vertical, shift = horizontal, ⌘ = zoom — that's arguably more standard and even simpler. Decide with the user, but the invariant is: **when rows overflow, a plain vertical wheel must scroll vertically.**)

**Step 3: Fix the transient `contentH`.** `contentH = Math.max(size.h, headerHeight + tracks.length*(trackHeight+4)+4)` can equal a stale large `size.h` right after a panel resize. Since the wrapper's own `overflow:auto` handles the viewport, `contentH` should be the *content* height only: `headerHeight + tracks.length*(trackHeight+4)+4` (drop the `Math.max(size.h, …)`), letting the wrapper scroll whenever content exceeds its box. Verify this doesn't shrink the canvas below the viewport (set the canvas CSS height to the content height and let the wrap be `flex:1; overflow:auto`).

**Step 4: Verify live.** Playwright: dispatch a vertical wheel over the timeline with overflow present → assert `scrollTop` increases and the lowest track row (e.g. "captions") becomes visible in the viewport. Confirm ⌘+wheel still zooms and shift+wheel still pans horizontally.

**Step 5:** Commit. `git commit -m "fix(timeline): plain vertical wheel scrolls rows when they overflow (was always hijacked to horizontal)"`.

---

## Task 5: Timeline click-to-seek in track rows

**Files:** `frontend/src/components/Timeline.tsx` (onMouseDown ~414-472)

**Root cause:** `setPlayhead` only fires for clicks in the 24px ruler or within 5px of the playhead. Track-row clicks select/deselect and never seek.

**Design decision (recommend confirming with user):** Match CapCut/Premiere behavior — clicking empty timeline space (below the ruler, not on a clip) should seek the playhead there; clicking directly on a clip selects it (and optionally also seeks). Minimal, least-surprising version: **seek on any track-row click that lands on empty space; keep clip-click as select-only.** Optionally also seek-on-clip-click (many NLEs do). 

**Step 1: Reproduce live.** Playwright: click at a known x in an empty track row (below y=24, on a lane with no clip at that x) → assert `playhead` (read via the "0.00 / 51.00s" readout or store) did NOT change → confirms the bug.

**Step 2: Add seek to the empty-row click path.** In `onMouseDown`, the `else { setSelection(null) }` branch (empty-space click below the ruler) should also seek:
```ts
const hit = hits.find(...)
if (hit) { /* select + start move/trim drag (unchanged) */ }
else {
  setSelection(null)
  // Empty timeline area click also seeks the playhead to the clicked position,
  // matching CapCut/Premiere. Only below the ruler and right of the label column.
  if (x > labelWidth) {
    const raw = Math.max(0, (x - labelWidth) / zoom)
    const dur = edl?.duration ?? raw
    setPlayhead(Math.min(raw, dur))
  }
}
```
If the user also wants seek-on-clip-click, add the same `setPlayhead` computation to the `if (hit)` branch (after selecting), but that can conflict with starting a clip-drag — safer to seek on mouseUP if the click didn't turn into a drag. Keep it to empty-space seek unless the user asks for more.

**Step 3: Verify live.** Playwright: click empty track-row space at a known x → assert the playhead moved to ~that timecode. Click on a clip → assert it selects (and, per the chosen design, either seeks or doesn't — match what was implemented). Confirm ruler-click and playhead-drag still work.

**Step 4:** Commit. `git commit -m "fix(timeline): clicking empty track-row space seeks the playhead (was ruler-only)"`.

---

## Task 6: Cross-track drag — prevent silent overlap / show it clearly

**Files:** `frontend/src/components/Timeline.tsx` (onMouseUp drop ~546-590), `src/video_ai_editor/agent/dispatch.py` (`move_clip` ~420-443)

**Root cause:** No time-overlap check on drop (frontend or backend); overlapping clips draw with identical fill and no distinction, so they look merged. No data loss — both clips exist.

**Design decision (recommend confirming):** Two viable fixes — (a) **prevent** the overlap (reject or auto-shift to nearest gap), or (b) **allow but clearly render** overlap (stagger/outline/hatch). For a CapCut-parity single-clip-per-audio-track feel, (a) is cleaner. Recommend: **on drop, if the destination track already has a clip overlapping the drop time range, auto-shift the dropped clip's start to the nearest free gap on that track (snapping to the end of the overlapping clip); if no gap fits, reject with a toast.** Also add the backend guard so Claude/MCP callers can't create the overlap either.

**Step 1: Backend guard in `move_clip`.** After computing the destination track + new_start, check for overlap against existing clips on that track (excluding the clip being moved). If overlapping, either snap `new_start` to the first free gap ≥ requested position, or raise `ValueError` (→ 400) with a clear message. Add a helper `_first_free_gap(track, duration, preferred_start)`.

**Step 2: Frontend drop mirror.** In `onMouseUp`, before dispatching `move_clip`, compute the same overlap check client-side for instant feedback (toast if rejected, or show the snapped position). The backend remains the real enforcement.

**Step 3: Visual distinction (defense-in-depth even if overlap becomes impossible).** In the canvas per-clip draw loop, if two clips on the same track still overlap in x (e.g. legacy data), draw the later one with a distinct outline (e.g. a 2px warning-colored border) so it's never invisibly merged. Cheap insurance.

**Step 4: Write failing tests + verify.** Backend: a test that `move_clip`-ing a clip onto an occupied overlapping range either snaps to the gap or raises (assert the resulting clips don't overlap). Live: Playwright drag "Main audio" clip onto the "Voiceover" row where a clip exists → assert (per chosen design) it either lands in a free gap or is rejected with a toast, and that both clips remain distinct in the EDL (`get_timeline`).

**Step 5:** Commit. `git commit -m "fix(timeline): prevent silent clip overlap on cross-track drop (snap-to-gap + backend guard)"`.

---

## Task 7: Double subtitle — general temporal-overlap dedupe, not just exact-match

**Files:** `src/video_ai_editor/agent/dispatch.py` (`add_super_text` ~557-595, `add_text` ~2480-2505), possibly `frontend/src/components/TextLayer.tsx`
- Test: `tests/test_tools_dispatch.py`

**Root cause:** Prior fix (88fc48b) only skips EXACT duplicates (same text AND same start/end). Two clips with different text at the same time on the same track both pass — confirmed live: `tx_super` has `"RIPPLE TEST"` and `"RIPPLE TEST 2"` both at `16.0–18.0`. `TextLayer.tsx` renders all active text clips with no collision handling → they stack.

**Design decision (recommend confirming):** The core question is what SHOULD happen when two text clips want the same time window on the same track. Options: (a) **replace** — a new text clip on the same track+overlapping-window replaces the old one (last-write-wins, most intuitive for "change the caption"); (b) **reject** the second with a clear error; (c) **auto-offset** the second in time; (d) **allow but never visually stack** (client renders them side-by-side or vertically stacked with clear separation). Recommend **(a) replace on the same track when time windows overlap and it's the same role** — this matches user intent ("I re-added the hook, it should update, not double") and directly kills the reported bug. Keep the existing `replace` arg but make same-track+overlapping-window replacement the DEFAULT for `add_super_text`/`add_text` (with an opt-out if truly two are wanted).

**Step 1: Write failing tests.**
```python
def test_add_super_text_replaces_overlapping_same_role_on_same_track(edl_store):
    from video_ai_editor.agent.dispatch import dispatch
    dispatch(edl_store, "add_super_text", {"text": "RIPPLE TEST",   "role": "super", "start": 16.0, "end": 18.0})
    dispatch(edl_store, "add_super_text", {"text": "RIPPLE TEST 2", "role": "super", "start": 16.0, "end": 18.0})
    supers = [c for t in edl_store.edl.tracks if t.id == "tx_super" for c in t.clips]
    # Overlapping same-role text on the same track should NOT stack — the second replaces the first.
    assert len(supers) == 1
    assert supers[0].text == "RIPPLE TEST 2"

def test_add_super_text_keeps_non_overlapping_text(edl_store):
    from video_ai_editor.agent.dispatch import dispatch
    dispatch(edl_store, "add_super_text", {"text": "A", "role": "super", "start": 0.0,  "end": 3.0})
    dispatch(edl_store, "add_super_text", {"text": "B", "role": "super", "start": 10.0, "end": 13.0})
    supers = [c for t in edl_store.edl.tracks if t.id == "tx_super" for c in t.clips]
    assert len(supers) == 2  # different time windows — both legitimate
```

**Step 2:** Run tests → confirm the first FAILS (2 clips) against current code.

**Step 3: Implement overlap-replace.** In `add_super_text`/`add_text`, before appending, remove any existing clip on the SAME track with the SAME role whose `[start,end)` overlaps the incoming clip's window:
```python
track.clips = [
    c for c in track.clips
    if not (isinstance(c, TextClip) and getattr(c, "role", None) == clip.role
            and c.start < clip.end and c.end > clip.start)
]
track.clips.append(clip)
```
This replaces the narrow exact-match `_same` guard with a temporal-overlap guard (superset — still catches exact duplicates). Keep an opt-out arg (`allow_stack: bool = False`) if the user ever wants two.

**Step 4: Also handle the already-corrupted session.** Existing sessions already have both clips (the reported one does). The replace logic only prevents NEW stacking. Add a note that the user can delete the extra clip, OR (nicer) add a one-time cleanup: consider whether `auto_caption`/render should de-dup exact-overlap same-role text at read time. At minimum, deleting one of the two existing clips via the timeline/`bulk_delete` resolves the live case. Don't silently mutate existing EDLs on load without the user's action — surface it.

**Step 5: Run tests → both pass.** Then full `uv run pytest`.

**Step 6: Verify live.** In the current session (which has the two RIPPLE TEST clips), delete one via the UI and confirm the preview shows a single caption. Then re-run an `add_super_text` with overlapping time and confirm it replaces rather than stacks.

**Step 7:** Commit. `git commit -m "fix(text): replace overlapping same-role text on a track instead of stacking (double-subtitle)"`.

---

## Task 8: Export popover size — confirm not-a-bug + guard against the reported symptom

**Files:** investigation only; possibly `frontend/src/components/TopBar.tsx` / `styles.css` if a real cause surfaces

**Root cause:** Not reproducible — popover renders 198×23px normally; every dimension is literal px. User's screenshot was a `--no-sandbox` Chrome (special launch), so most likely a browser zoom / accessibility text-size / extension artifact, not code.

**Step 1:** Re-drive live at 100% zoom via Playwright and DevTools computed styles on the `<select>`/label nodes — confirm normal sizing (already done once; re-confirm on the executor's machine). 

**Step 2:** If it genuinely reproduces in a *normal* Chrome window (not `--no-sandbox`/automation), inspect which declaration wins via computed styles and fix that specific rule. If it only reproduces under the special-flag Chrome, document it as an environment artifact (browser zoom/extension), not a code bug — nothing to change.

**Step 3:** No commit unless a real cause is found. Report the determination to the user.

---

## Cross-cutting: verification discipline (do NOT repeat the shipped-broken mistake)

- **UI interaction (Tasks 4, 5, 6):** drive `:5173` with Playwright — real pointer/wheel events, assert computed styles/positions/scrollTop/playhead. `verify` skill.
- **Text/EDL (Task 7):** unit tests + confirm live in the browser preview that captions no longer stack.
- **VO (Tasks 1, 2):** the packaged `.app` is the ONLY valid environment for Task 1's mic test — browser-dev mode gives a false pass. Task 1's interactive mic click-through requires a human; ship the code + entitlement + AVFoundation-authorization and hand the final test to the user, explicitly, if not interactively testable in-session.
- Then the standard gate (won't catch these bugs but must pass): `cd frontend && npx tsc --noEmit && npx vite build && npm run lint`; `uv run pytest`; revert `uv.lock` drift (`git checkout -- uv.lock`); remove any `.playwright-mcp/` scratch dir before committing.
- **Local commits only, no push** (per standing user instruction this project).

---

## Scope decisions to confirm with the user before executing

1. **Task 1 VO fallback:** if native TCC grant still fails on their machine after 1a-1c, is a "import an audio file as voiceover" file-picker (guaranteed-working, uses the existing `/vo_record` endpoint) an acceptable guaranteed path? (Recommend yes — it's the "any means necessary" safety net.)
2. **Task 4 wheel convention:** plain wheel = vertical-scroll-when-overflow + ⌘ zoom + shift horizontal (recommended), vs the current plain-wheel-always-horizontal. Which feels right?
3. **Task 5 seek scope:** seek on empty-space click only (recommended, safe), or also seek-on-clip-click (more CapCut-like but trickier with drag)?
4. **Task 6 overlap policy:** auto-snap-to-gap (recommended) vs reject-with-toast vs allow-but-render-distinctly.
5. **Task 7 text policy:** replace overlapping same-role text (recommended) vs reject vs allow-with-clear-separation.

---

## Out of scope (deferred)

- A paid Apple Developer ID signature / notarization (Task 1b uses ad-hoc signing + entitlements + hardened runtime, which is sufficient for local TCC mic access; distribution-grade signing is a separate concern).
- WebM/GIF export, fps control (deferred from Round 3).
- The pre-existing `tsc -b` failures (FrameScrubber.tsx, Properties.tsx) — unrelated; `build_app.sh` already routes around them via `tsc --noEmit`.
