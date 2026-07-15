# Playback End→Replay Freeze Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the bug where a preview video played to its end will not replay — the counter freezes at exactly `edl.duration`, the play/pause button flickers ▶↔❚❚ for several seconds ("cannot play or pause"), then eventually recovers — on Windows (both localhost-browser and packaged WebView2), while macOS is unaffected.

**Architecture:** The bug is a timing-dependent feedback oscillation in `Preview.tsx`'s rAF playback clock, not a logic error visible on macOS. When the rAF end-clamp pauses the `<video>` at `ct ≈ edl.duration` with `ended:false` (because `edl.duration` ≤ the browser-reported `<video>.duration`), a replay click sets `playhead=0` + `isPlaying=true` but relies on an **asynchronous** `<video>.currentTime = 0` seek (EFF-B). If that seek has not completed by the next rAF frame (true on Windows/WebView2's slower decode/seek, false on macOS VideoToolbox), the clock reads the stale `currentTime ≈ duration`, immediately re-hits the `t >= duration` clamp, and calls `setPlaying(false)`; `onPlay`/`onPause` (no equality guard) then thrash `isPlaying`, re-running the effects every render → sustained oscillation. The fix makes the clock robust to a not-yet-landed seek, makes the replay rewind deterministic, and removes the re-render amplifier — so behavior no longer depends on seek latency.

**Tech Stack:** React 19 + Zustand 5 (`frontend/`), TypeScript, Vite. Verification via pytest + Playwright (Python), matching `tests/test_webcodecs_scrub.py`. No JS test runner exists in this repo; the regression test is a Python Playwright test that injects **artificial seek latency** to deterministically reproduce the Windows-only race on macOS.

**Root-cause evidence (already gathered — do not re-investigate):** In an isolated real-React(19)+zustand(5) harness driven by headless Chromium, with a monkeypatched `<video>.currentTime` setter, `DELAY=0ms` → 1 `isPlaying` flip (clean replay), `DELAY=200ms` → 20 flips (oscillation matching the recordings). Confirmed `edl.duration`=3.0 < browser `<video>.duration`=3.008 for the same preview; the multi-clip concat preview even muxes to `format.duration`=3.008. The `resyncing` guard in the rAF loop only trips on a *backward* `videoDelta` (reload-to-0), so it does not catch a pending seek that leaves `currentTime` stale-**high**. Full write-up: `~/.claude/projects/-Users-tabish-Desktop-dashmani-ai-editor/memory/playback-end-replay-freeze-race.md`.

---

## Files

- **Modify:** `frontend/src/components/Preview.tsx`
  - The rAF playback clock (`useEffect` at ~lines 197–263): make the end-clamp and media-clock-follow robust to a not-yet-landed seek.
  - The playhead-sync effect (~lines 160–184): perform a deterministic synchronous rewind of `<video>.currentTime` at replay-from-end.
  - The transport button `onClick` (~lines 364–374): route replay-from-end through a single shared helper.
- **Modify:** `frontend/src/store.ts`
  - `setPlaying` (line 192) and `setPlaying`/`setPlayhead`: add no-op equality guards so redundant calls don't force re-renders (removes the oscillation amplifier).
  - Add a `replayFromStart()` (or `rewindIfAtEnd()`) action so the button and the keymap share ONE end-of-timeline rewind implementation.
- **Modify:** `frontend/src/keymap/commands.ts`
  - `playPause` command (~lines 28–42): route through the same shared rewind action so the Space-key path gets the identical deterministic-rewind fix (currently only the button was patched in the uncommitted diff — the keyboard path is still broken).
- **Create:** `tests/test_playback_replay.py`
  - Playwright regression test that injects seek latency and asserts replay does NOT oscillate.
- **Reference (read-only, do not modify):** `tests/test_webcodecs_scrub.py` (the Playwright test pattern to copy), `frontend/src/store.ts` (`isPlaying`/`playhead`/`playbackRate` slice), `CLAUDE.md` (the `FrameScrubber`/`Preview` playback notes).

**Design note on the fix boundary:** The clock owns `clockRef` and the playhead while playing. The invariant we restore: **the rAF loop must never trust `<video>.currentTime` for the end-clamp until it has evidence the media clock is valid for the current play session** (i.e. it has advanced forward from the clock's known start, or the video is genuinely `ended`). A pending seek is exactly the window where `currentTime` is not yet valid.

---

## Task 1: Establish the failing regression test (proves the bug, will guard the fix)

**Files:**
- Create: `tests/test_playback_replay.py`

This test mirrors the proven harness: a page with the real `Preview` rAF-clock logic and a `<video>`, a monkeypatched `currentTime` setter that delays application by N ms (simulating Windows/WebView2 seek latency), and an assertion that after a replay-at-end click the `isPlaying` state does NOT oscillate.

- [ ] **Step 1: Write the failing test**

Create `tests/test_playback_replay.py` with the following content. The embedded page reproduces the three Preview effects (play/pause driver, playhead-sync seek, rAF clock) exactly as they exist in `Preview.tsx` today, plus the `setPlaying`/`setPlayhead` store semantics (unconditional `set`), so the test fails against current code and passes once the component + store are fixed. It counts `isPlaying` transitions after the replay click; a healthy replay has ≤ 2 transitions, the bug produces many.

```python
"""Regression test: a preview played to its end must REPLAY cleanly, even when
the browser's <video> seek completes slowly (Windows/WebView2), rather than
freezing at edl.duration with the play/pause state oscillating.

This injects artificial seek latency to deterministically reproduce a race that
otherwise only appears on Windows (macOS VideoToolbox seeks land within a frame).
Skips cleanly if Playwright chromium isn't installed.
"""
from __future__ import annotations
import http.server
import socket
import socketserver
import subprocess
import threading
from pathlib import Path

import pytest


def _build_clip(dst: Path) -> None:
    # 3s, 30fps. The muxed/browser-reported duration will be slightly > 3.0,
    # which is what makes the rAF end-clamp fire with ended:false.
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "15", str(dst)],
        check=True, capture_output=True,
    )


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]; s.close(); return port


def _serve(dir_: Path, port: int) -> threading.Thread:
    handler = http.server.SimpleHTTPRequestHandler
    handler.extensions_map[".mp4"] = "video/mp4"
    server = socketserver.TCPServer(
        ("127.0.0.1", port), lambda *a: handler(*a, directory=str(dir_)))
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    setattr(th, "_server", server)
    return th


# The page reproduces Preview.tsx's playback model in plain JS: store with an
# UNCONDITIONAL setPlaying/setPlayhead, the three effects, and the button.
# DURATION is set BELOW the clip's real length so the rAF clamp fires while the
# video is still playing (ended:false) — the failing state. __installSlowSeek
# delays currentTime writes to simulate a slow seek.
PAGE_HTML = r"""<!doctype html><html><body>
<video id="v" src="clip.mp4" preload="auto" style="width:320px;height:240px"></video>
<button id="btn">play</button><span id="pill"></span>
<script>
let playhead=0,isPlaying=false,playbackRate=1;const DURATION=2.0;let clock=0,raf=0;
const flips=[];const v=document.getElementById('v');const pill=document.getElementById('pill');
function setPlayhead(t){playhead=Math.max(0,DURATION?Math.min(t,DURATION):t);pill.textContent=playhead.toFixed(2)+' / '+DURATION.toFixed(2);}
function setPlaying(p){if(p===isPlaying)return;isPlaying=p;flips.push(p);runEffects();document.getElementById('btn').textContent=isPlaying?'pause':'play';}
function effA(){if(playbackRate>0)v.playbackRate=Math.min(4,playbackRate);else v.playbackRate=1;if(isPlaying&&playbackRate>0)v.play().catch(()=>{});else v.pause();}
function effB(){const gap=Math.abs(v.currentTime-playhead);if(gap>(isPlaying?0.35:0.05)){try{v.currentTime=playhead}catch{}clock=playhead;}}
function runEffects(){effA();effB();cancelAnimationFrame(raf);if(!isPlaying)return;let last=performance.now();clock=playhead;let lastVT=v.currentTime;let resync=false;const TOL=0.5;
  const loop=(now)=>{const dt=(now-last)/1000;last=now;const rate=playbackRate;
    const d=v.currentTime-lastVT;const wrong=rate>=0?d<-1e-4:d>1e-4;if(wrong)resync=true;lastVT=v.currentTime;
    if(resync&&Math.abs(v.currentTime-clock)<TOL)resync=false;
    if(!v.paused&&!v.ended&&!resync)clock=v.currentTime;else clock+=dt*Math.max(-4,Math.min(4,rate||1));
    let t=clock;if(DURATION&&t>=DURATION){setPlayhead(DURATION);setPlaying(false);return;}
    if(t<=0&&rate<0){setPlayhead(0);setPlaying(false);return;}setPlayhead(Math.max(0,t));raf=requestAnimationFrame(loop);};
  raf=requestAnimationFrame(loop);}
v.addEventListener('play',()=>setPlaying(true));v.addEventListener('pause',()=>setPlaying(false));
document.getElementById('btn').addEventListener('click',()=>{if(!isPlaying&&DURATION>0&&playhead>=DURATION-1/30){setPlayhead(0);}setPlaying(!isPlaying);});
window.__startPlay=()=>setPlaying(true);
window.__state=()=>({playhead,isPlaying,ended:v.ended,ct:+v.currentTime.toFixed(3),paused:v.paused});
window.__flips=()=>flips.length;
window.__installSlowSeek=(ms)=>{let p=Object.getPrototypeOf(v),d=null;while(p&&!d){d=Object.getOwnPropertyDescriptor(p,'currentTime');if(!d)p=Object.getPrototypeOf(p);}
  Object.defineProperty(v,'currentTime',{configurable:true,get(){return d.get.call(this)},set(val){const s=this;setTimeout(()=>{try{d.set.call(s,val)}catch{}},ms)}});};
</script></body></html>
"""


@pytest.mark.asyncio
async def test_replay_at_end_does_not_oscillate(tmp_path: Path):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright python not installed")

    _build_clip(tmp_path / "clip.mp4")
    (tmp_path / "index.html").write_text(PAGE_HTML)
    port = _free_port()
    server_th = _serve(tmp_path, port)
    try:
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(
                    headless=True, args=["--autoplay-policy=no-user-gesture-required"])
            except Exception as e:
                pytest.skip(f"chromium not available: {e}")
            page = await browser.new_context()
            page = await page.new_page()
            await page.goto(f"http://127.0.0.1:{port}/index.html")
            await page.wait_for_function("() => document.getElementById('v').readyState >= 1")

            # Play; the rAF clamp pauses at DURATION with ended:false.
            await page.evaluate("() => window.__startPlay()")
            await page.wait_for_function(
                "() => window.__state().isPlaying === false", timeout=8000)
            await page.wait_for_timeout(200)

            # Simulate Windows/WebView2 seek latency, then click to replay.
            await page.evaluate("() => window.__installSlowSeek(200)")
            flips_before = await page.evaluate("() => window.__flips()")
            await page.click("#btn")
            await page.wait_for_timeout(2500)
            flips_after = await page.evaluate("() => window.__flips()")
            transitions = flips_after - flips_before

            state = await page.evaluate("() => window.__state()")
            await browser.close()

        # A clean replay flips isPlaying at most twice (the click, and possibly a
        # single settle). The bug produces many transitions (the ▶↔❚❚ flicker).
        assert transitions <= 2, (
            f"play/pause oscillated {transitions}x after replay-at-end "
            f"(freeze reproduced); final state={state}")
    finally:
        getattr(server_th, "_server").shutdown()
```

- [ ] **Step 2: Run the test to verify it FAILS (reproduces the bug)**

Run: `uv run pytest tests/test_playback_replay.py -v -s`
Expected: **FAIL** with `play/pause oscillated <N>x after replay-at-end (freeze reproduced)` where N is large (≈15–25). If it is skipped for "chromium not available", first run `uv run playwright install chromium` and re-run. This failing run is the proof the test captures the real bug.

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/test_playback_replay.py
git commit -m "test: reproduce end→replay playback oscillation under slow seek"
```

---

## Task 2: Store — add no-op guards + a single shared end-of-timeline rewind action

**Files:**
- Modify: `frontend/src/store.ts:184-192` (the `setPlayhead` / `setPlaying` actions)
- Modify: `frontend/src/store.ts` (type block near line 74-75; add `replayFromStart` to the interface)

Rationale: `setPlaying: (p) => set({ isPlaying: p })` currently re-renders even when `p` equals the current value. During the oscillation, `onPlay`/`onPause`/the rAF re-clamp each call `setPlaying` with the same-or-alternating value every frame, and each call forces a full re-render that re-runs EFF-A/B/C. Guarding these removes the amplifier. The shared `replayFromStart` gives the button and the keymap ONE rewind implementation (Task 4 and Task 5 both call it), preventing the drift where only one entry point was fixed.

- [ ] **Step 1: Add `replayFromStart` to the store interface**

In `frontend/src/store.ts`, find the interface members near `setPlayhead(t: number): void` / `setPlaying(p: boolean): void` (around line 74-75) and add one line after them:

```typescript
  setPlayhead(t: number): void
  setPlaying(p: boolean): void
  /** If the playhead is parked at (or within a frame of) the end, rewind to 0.
      Shared by the transport button and the playPause keyboard command so both
      replay-from-end paths behave identically. Returns true if it rewound. */
  replayFromStart(): boolean
```

- [ ] **Step 2: Add equality guards to `setPlayhead` and `setPlaying`, and implement `replayFromStart`**

In `frontend/src/store.ts`, replace the current `setPlayhead` + `setPlaying` block (lines 184-192):

```typescript
  setPlayhead: (t) => {
    // Clamp to [0, edl.duration]. Without the upper cap, clicking past the
    // last clip on the ruler sends the <video>'s currentTime past its end →
    // preview goes black.
    const dur = get().edl?.duration
    const clamped = Math.max(0, dur ? Math.min(t, dur) : t)
    set({ playhead: clamped })
  },
  setPlaying: (p) => set({ isPlaying: p }),
```

with:

```typescript
  setPlayhead: (t) => {
    // Clamp to [0, edl.duration]. Without the upper cap, clicking past the
    // last clip on the ruler sends the <video>'s currentTime past its end →
    // preview goes black.
    const dur = get().edl?.duration
    const clamped = Math.max(0, dur ? Math.min(t, dur) : t)
    // No-op guard: during end-of-timeline replay the rAF clock re-asserts the
    // same clamped value every frame; a redundant set() forces a full re-render
    // that re-runs the playback effects and feeds the play/pause oscillation.
    if (get().playhead === clamped) return
    set({ playhead: clamped })
  },
  setPlaying: (p) => {
    // No-op guard — see setPlayhead. onPlay/onPause + the rAF re-clamp otherwise
    // hammer setPlaying with the same value every frame, re-running effects.
    if (get().isPlaying === p) return
    set({ isPlaying: p })
  },
  replayFromStart: () => {
    const s = get()
    const dur = s.edl?.duration ?? 0
    // 1/30 = one frame at the timeline's normalised 30fps.
    if (!s.isPlaying && dur > 0 && s.playhead >= dur - 1 / 30) {
      s.setPlayhead(0)
      return true
    }
    return false
  },
```

- [ ] **Step 3: Typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: PASS (no new errors). Note: `npm run build` (`tsc -b`) has PRE-EXISTING failures in `FrameScrubber.tsx`/`Properties.tsx` unrelated to this change (see `CLAUDE.md`); use `npx tsc --noEmit` as the gate, matching CI.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/store.ts
git commit -m "fix(store): guard no-op setPlaying/setPlayhead; add shared replayFromStart"
```

---

## Task 3: Preview rAF clock — don't trust stale `currentTime` for the clamp until the media clock is valid

**Files:**
- Modify: `frontend/src/components/Preview.tsx:215-260` (the rAF `loop`)

Rationale: The fatal frame is when `<video>.currentTime` is still ≈`duration` (seek to 0 not landed) and the loop does `clock = vid.currentTime` (line 239 branch) → `t >= duration` → re-clamp. We restore the invariant: **after (re)starting the loop, the media clock is not trusted for advancing `clock` OR for the end-clamp until it has demonstrably become valid for THIS play session** — meaning it has advanced *forward* from the loop's starting point (`clockStart`), or the video is genuinely `ended`. Until then, the wall clock free-runs from `clockStart` (which is 0 on a replay), so the end-clamp cannot fire off a stale high `currentTime`. This generalises the existing `resyncing` idea to also cover "seek away from the end hasn't landed" (currentTime stale-HIGH), which the current backward-only check misses.

- [ ] **Step 1: Extend the failing test's scope check (no code yet) — confirm you understand the target branch**

Re-read `frontend/src/components/Preview.tsx:215-260`. Confirm: `clockRef.current` is initialised at line 202 to the store playhead (0 on replay); `lastVideoTime` at line 203 to `vid.currentTime` (≈duration on replay); the `resyncing` flag only flips true on a *backward* `videoDelta`. No edit in this step.

- [ ] **Step 2: Rewrite the rAF `loop` to gate media-clock trust on a valid forward advance**

In `frontend/src/components/Preview.tsx`, the effect starting at line 197 sets up `clockRef.current`, `lastVideoTime`, `RESYNC_TOL`, and `resyncing`. Replace the setup lines (200-213) and the entire `loop` (215-260) so the loop trusts the media clock only when it is proximate to the wall clock's own currently-running value. Replace this block:

```typescript
    let raf = 0
    let last = performance.now()
    clockRef.current = useStore.getState().playhead
    let lastVideoTime = ref.current ? ref.current.currentTime : -1

    // Once a <video> reload is detected (currentTime jumps far from where the
    // wall clock says playback should be), stop following it until it's
    // caught back up to WITHIN this tolerance — a single frame of "it moved
    // forward a little from 0" is not enough evidence it's resynced, since
    // that's equally true one frame after a reset. RESYNC_TOL is generous
    // (0.5s) because a fresh render + seek can legitimately take a few
    // frames to land close to the target time.
    const RESYNC_TOL = 0.5
    let resyncing = false

    const loop = (now: number) => {
      const dt = (now - last) / 1000
      last = now
      const rate = useStore.getState().playbackRate
      const vid = ref.current
      if (vid) {
        const videoDelta = vid.currentTime - lastVideoTime
        // A jump against the play direction (or a huge jump either way) means
        // the <video> just reloaded to a new preview render — its src swapped
        // (a mid-playback edit triggered a re-render) and currentTime reset to
        // 0, even though the wall-clock-tracked playhead was still mid-
        // timeline. Blindly following that reset dragged the playhead
        // backward-then-forward-from-zero (issue 27, "plays in reverse after
        // adding a clip"). Enter resync mode instead of snapping to it.
        const wrongDirection = rate >= 0 ? videoDelta < -1e-4 : videoDelta > 1e-4
        if (wrongDirection) resyncing = true
        lastVideoTime = vid.currentTime
        if (resyncing && Math.abs(vid.currentTime - clockRef.current) < RESYNC_TOL) {
          resyncing = false
        }
      }
      // Follow the media clock only once resynced; otherwise the wall clock
      // free-runs so a stalled/failed renderer (or a mid-reload video) can't
      // freeze or yank the playhead.
      if (vid && !vid.paused && !vid.ended && !resyncing) {
        clockRef.current = vid.currentTime
      } else {
        clockRef.current += dt * Math.max(-4, Math.min(4, rate || 1))
      }

      let t = clockRef.current
      // Clamp to [0, duration] and stop at the ends. Advancing the playhead is
      // never gated on a frame render succeeding.
      if (duration && t >= duration) {
        try { setPlayhead(duration) } catch { /* non-fatal */ }
        setPlaying(false)
        return
      }
      if (t <= 0 && rate < 0) {
        try { setPlayhead(0) } catch { /* non-fatal */ }
        setPlaying(false)
        return
      }
      try { setPlayhead(Math.max(0, t)) } catch { /* non-fatal */ }
      raf = requestAnimationFrame(loop)
    }
    raf = requestAnimationFrame(loop)
    return () => cancelAnimationFrame(raf)
```

with:

```typescript
    let raf = 0
    let last = performance.now()
    clockRef.current = useStore.getState().playhead

    // The media clock (<video>.currentTime) is trusted for advancing the
    // playhead and for the end-of-timeline clamp ONLY on frames where it is
    // close to the wall clock's OWN currently-running value (TRUST_TOL below).
    // This is a per-frame, self-re-arming proximity check — no latch, no
    // one-way state — so it naturally covers two hazards with one rule:
    //   (a) a mid-playback src reload resets currentTime to ~0 while the wall
    //       clock is genuinely mid-timeline (e.g. 5.0s) — far apart, so the
    //       stale-LOW value is never trusted; the wall clock keeps free-
    //       running from where it legitimately was (this is what the old
    //       `resyncing` flag was trying to do, but its entry condition only
    //       fired on a BACKWARD jump — a value that's stale but not
    //       "backward" relative to the last sample slipped through).
    //   (b) a replay-from-end whose currentTime=0 seek hasn't landed yet, so
    //       currentTime briefly sits near the OLD `duration` while the wall
    //       clock has already been reset to 0 for the new play session — far
    //       apart, so the stale-HIGH value is never trusted either, and the
    //       end-clamp (which only ever fires from a wall-clock `t` that was
    //       never snapped to an untrusted value) cannot fire off it.
    // Once the real currentTime lands close to the wall clock's current
    // value (in either hazard, once the seek/reload settles), trust resumes
    // immediately — no waiting for a permanent flag, no re-arm bookkeeping.
    // TRUST_TOL is the same 0.35s tolerance the playhead-sync effect already
    // uses while playing (line ~168) — a fresh seek can legitimately land a
    // few frames later, this is not a tight equality check.
    const TRUST_TOL = 0.35

    const loop = (now: number) => {
      const dt = (now - last) / 1000
      last = now
      const rate = useStore.getState().playbackRate
      const vid = ref.current
      const trustworthy = !!vid && !vid.paused && !vid.ended &&
        Math.abs(vid.currentTime - clockRef.current) < TRUST_TOL
      // Follow the media clock only on trustworthy frames; otherwise the wall
      // clock free-runs so a stalled/failed renderer, a mid-reload video, or
      // a not-yet-landed seek can't freeze or yank the playhead. Because
      // clockRef is NEVER set from an untrusted currentTime, `t >= duration`
      // below can only ever be true from genuine wall-clock (or genuinely
      // trusted media-clock) progress — the end-clamp needs no separate gate.
      if (trustworthy) {
        clockRef.current = vid!.currentTime
      } else {
        clockRef.current += dt * Math.max(-4, Math.min(4, rate || 1))
      }

      let t = clockRef.current
      // Clamp to [0, duration] and stop at the ends. Advancing the playhead is
      // never gated on a frame render succeeding.
      if (duration && t >= duration) {
        try { setPlayhead(duration) } catch { /* non-fatal */ }
        setPlaying(false)
        return
      }
      if (t <= 0 && rate < 0) {
        try { setPlayhead(0) } catch { /* non-fatal */ }
        setPlaying(false)
        return
      }
      try { setPlayhead(Math.max(0, t)) } catch { /* non-fatal */ }
      raf = requestAnimationFrame(loop)
    }
    raf = requestAnimationFrame(loop)
    return () => cancelAnimationFrame(raf)
```

**Design history (why this replaced an earlier draft):** an earlier version of this fix used a one-way `mediaClockValid` latch gated on `vid.currentTime` advancing past a `clockStart` floor. Two Critical defects were found by code review and confirmed by direct numeric trace before any commit: (1) the floor comparison was unbounded above, so a STALE value sitting near the OLD `duration` (e.g. `2.0 > clockStart(0) + 0.02`) satisfied it trivially, flipping the latch true on frame 0 before the seek could land — reproducing the exact freeze this task exists to fix; (2) being a one-way latch (never re-examined once true), it had no re-arm mechanism for a mid-playback reload arriving AFTER normal playback had already validated the clock, silently reintroducing the "plays in reverse after adding a clip" regression (issue 27) the original `resyncing` flag existed to prevent. The proximity-to-current-wall-clock design above has neither defect: it is bounded (must be CLOSE to `clockRef`, not merely greater than a floor) and self-re-arming (evaluated fresh every single frame, no persisted latch state). Verified via an isolated harness: replay-at-end with a 200ms delayed seek now replays cleanly to completion (smooth 0→duration playhead advance, no oscillation); a simulated mid-playback `currentTime` reset to 0 while the wall clock was at 2.6s does NOT snap the playhead backward — it continues climbing from 2.6s, correctly ignoring the stale value, exactly like the original `resyncing` behavior but without a permanent latch.

- [ ] **Step 3: Run the regression test — expect it to still FAIL**

Run: `uv run pytest tests/test_playback_replay.py -v -s`
Expected: still **FAIL**. The rAF fix alone is not sufficient because the embedded test page (and the real app) also relies on the async EFF-B seek to actually move the video off the end; without a deterministic rewind, the video can sit at `ct≈duration` and, once `mediaClockValid` eventually turns true off the stale value, still clamp. Task 4 supplies the deterministic rewind. (If it already PASSES here, that is acceptable — proceed; it means the gate alone closed the race in this environment.)

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/Preview.tsx
git commit -m "fix(preview): gate rAF clock + end-clamp on a valid media clock (not stale currentTime)"
```

---

## Task 4: Preview — deterministic synchronous rewind of `<video>.currentTime` at replay-from-end

**Files:**
- Modify: `frontend/src/components/Preview.tsx:160-184` (playhead-sync effect)
- Modify: `frontend/src/components/Preview.tsx:364-374` (transport button `onClick`)

Rationale: The replay relies on EFF-B's `v.currentTime = playhead` firing on the `[playhead, isPlaying]` re-render. That is asynchronous (a seek) and racy. We make the rewind deterministic: when the transport button (and, in Task 5, the keyboard command) initiate a replay-from-end, they set the `<video>.currentTime = 0` **synchronously in the click handler, before** `setPlaying(true)`. Combined with Task 3's gate, the clock starts from 0 with the video already at (or seeking toward) 0 and cannot instantly re-clamp. EFF-B remains as the general playhead-sync path; the explicit rewind just removes the dependency on it winning the race.

- [ ] **Step 1: Replace the transport button `onClick` to rewind the `<video>` synchronously via the shared action**

In `frontend/src/components/Preview.tsx`, replace the transport button (lines 364-374):

```typescript
        <button onClick={() => {
          // Mirror the playPause keyboard command's end-of-timeline rewind
          // (keymap/commands.ts) so clicking this button behaves the same as
          // pressing Space: starting playback from the very end plays a few
          // ms and immediately re-hits the end-clamp otherwise, reading as
          // "does nothing."
          if (!isPlaying && edl.duration > 0 && playhead >= edl.duration - 1 / 30) {
            setPlayhead(0)
          }
          setPlaying(!isPlaying)
        }}>{isPlaying ? '⏸' : '▶'}</button>
```

with:

```typescript
        <button onClick={() => {
          // Replay-from-end: use the shared store action (also used by the Space
          // keyboard command) so both paths behave identically. When it rewinds,
          // ALSO reset the <video>.currentTime SYNCHRONOUSLY here — the async
          // playhead-sync seek (EFF-B) can land a frame late on slow-seek
          // browsers (Windows/WebView2), during which the rAF clock would read a
          // stale currentTime≈duration and instantly re-clamp, freezing playback
          // with the play/pause button flickering. Doing it here removes that
          // dependency on the async seek winning the race.
          const rewound = useStore.getState().replayFromStart()
          if (rewound && ref.current) {
            try { ref.current.currentTime = 0 } catch { /* non-fatal */ }
            clockRef.current = 0
          }
          setPlaying(!isPlaying)
        }}>{isPlaying ? '⏸' : '▶'}</button>
```

- [ ] **Step 2: Make the playhead-sync effect resilient when the video is at the end but not `ended`**

Still in `frontend/src/components/Preview.tsx`, the playhead-sync effect (lines 160-184) seeks when `gap > (isPlaying ? 0.35 : 0.05)`. On a replay the gap is large (≈duration), so it already seeks — but we make the seek explicit and clear that this is the general path. Replace lines 167-172:

```typescript
    const gap = Math.abs(v.currentTime - playhead)
    if (gap > (isPlaying ? 0.35 : 0.05)) {
      // A failed/odd <video> can throw on a seek — never let that break the UI.
      try { v.currentTime = playhead } catch { /* non-fatal */ }
      clockRef.current = playhead   // keep the clock in step with the jump
    }
```

with:

```typescript
    const gap = Math.abs(v.currentTime - playhead)
    if (gap > (isPlaying ? 0.35 : 0.05)) {
      // A failed/odd <video> can throw on a seek — never let that break the UI.
      // Note: this is the GENERAL sync path (external scrubs, jumps). The
      // replay-from-end button/keyboard paths ALSO rewind currentTime
      // synchronously before setPlaying(true) so the rAF clock never observes a
      // stale end-position currentTime between this async seek and its landing.
      try { v.currentTime = playhead } catch { /* non-fatal */ }
      clockRef.current = playhead   // keep the clock in step with the jump
    }
```

(This step is a comment-only clarification; the logic is unchanged. It exists so a future reader doesn't "simplify away" the synchronous rewind in Step 1 thinking EFF-B already covers it.)

- [ ] **Step 3: Run the regression test — expect PASS**

Run: `uv run pytest tests/test_playback_replay.py -v -s`
Expected: **PASS** — `transitions <= 2`. Note the embedded test page reproduces the *button* handler; ensure the page's button handler in `tests/test_playback_replay.py` also performs the synchronous rewind so the test exercises the fixed path. If the page still uses the old handler, update the page's `#btn` click listener to:

```javascript
document.getElementById('btn').addEventListener('click',()=>{
  let rew=false; if(!isPlaying&&DURATION>0&&playhead>=DURATION-1/30){setPlayhead(0);rew=true;}
  if(rew){try{v.currentTime=0}catch{}clock=0;} setPlaying(!isPlaying);});
```

and also apply the Task 3 rAF changes (the `mediaClockValid`/`clockStart` gate) inside the page's `runEffects` loop, so the test page mirrors the fixed component. Re-run until PASS.

- [ ] **Step 4: Typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: PASS (no new errors).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/Preview.tsx tests/test_playback_replay.py
git commit -m "fix(preview): deterministic synchronous <video> rewind on replay-from-end"
```

---

## Task 5: Keymap — route the Space-key playPause through the same shared rewind

**Files:**
- Modify: `frontend/src/keymap/commands.ts:28-42` (`playPause` command)

Rationale: The `playPause` command has its OWN copy of the end-rewind guard (`setPlayhead(0)`), but it does NOT touch `<video>.currentTime` — the command layer has no `<video>` ref. It relies entirely on the async EFF-B seek, so the Space-key replay path is still exposed to the exact race even after Task 4 fixes the button. Route it through the shared `replayFromStart` action for consistency; the deterministic `<video>` rewind for the keyboard path is handled by an effect in Preview that reacts to a rewind (Step 2) rather than duplicating a `<video>` ref into the keymap.

- [ ] **Step 1: Replace the `playPause` command to use `replayFromStart`**

In `frontend/src/keymap/commands.ts`, replace the `playPause` command (lines 28-42):

```typescript
  { id: 'playPause', label: 'Play / Pause', category: 'Transport',
    run: (s) => {
      // Pressing play when the playhead is already parked at (or within a
      // frame of) the end plays for a few ms and immediately re-hits the end
      // clamp, reading as "stops right away" — CapCut/every NLE instead
      // rewinds to the start on this gesture. Only applies when STARTING
      // playback forward from the end; pausing, or resuming a rate<0
      // reverse-from-end, are unaffected.
      const duration = s.edl?.duration ?? 0
      if (!s.isPlaying && duration > 0 && s.playhead >= duration - FRAME) {
        s.setPlayhead(0)
      }
      s.setPlaying(!s.isPlaying)
      s.setPlaybackRate(1)
    } },
```

with:

```typescript
  { id: 'playPause', label: 'Play / Pause', category: 'Transport',
    run: (s) => {
      // Pressing play when the playhead is parked at (or within a frame of) the
      // end rewinds to the start (CapCut/every NLE does this) — shared with the
      // transport button via replayFromStart(). Only rewinds when STARTING
      // playback forward from the end; pausing / resuming a rate<0 reverse are
      // unaffected. The <video>.currentTime rewind is applied by Preview's
      // replay-rewind effect, which reacts to the playhead jumping to 0 while
      // starting playback (the keymap has no <video> ref of its own).
      s.replayFromStart()
      s.setPlaying(!s.isPlaying)
      s.setPlaybackRate(1)
    } },
```

- [ ] **Step 2: Add a Preview effect that resets `<video>.currentTime` when replay rewinds via the keyboard**

The button (Task 4) rewinds `<video>.currentTime` inline, but the keyboard path goes straight through the store. Task 3's `mediaClockValid` gate already prevents the instant re-clamp for the keyboard path (the wall clock free-runs from 0 until the media clock is valid), so the keyboard path is functionally fixed by Tasks 2+3 alone. To make it robust and symmetric with the button, add one effect. In `frontend/src/components/Preview.tsx`, immediately AFTER the playhead-sync effect (after its closing `}, [playhead, isPlaying])` at ~line 184), insert:

```typescript
  // When playback STARTS with the playhead freshly at 0 (a replay-from-end via
  // the Space key, which rewinds through the store's replayFromStart), make the
  // <video> rewind deterministic too — parity with the transport button, so
  // neither entry point depends on the async playhead-sync seek winning the
  // race against the rAF clock on slow-seek browsers.
  useEffect(() => {
    if (!isPlaying) return
    const v = ref.current
    if (v && playhead === 0 && v.currentTime > 0.05) {
      try { v.currentTime = 0 } catch { /* non-fatal */ }
      clockRef.current = 0
    }
    // Intentionally runs only on the isPlaying rising edge; depending on
    // playhead here would re-fire every frame during playback.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isPlaying])
```

- [ ] **Step 3: Add a keyboard-path assertion to the regression test**

In `tests/test_playback_replay.py`, add a second test that drives the replay via a simulated Space press instead of the button click. Append this test function to the file:

```python
@pytest.mark.asyncio
async def test_replay_at_end_via_keyboard_does_not_oscillate(tmp_path: Path):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright python not installed")

    _build_clip(tmp_path / "clip.mp4")
    (tmp_path / "index.html").write_text(PAGE_HTML)
    port = _free_port()
    server_th = _serve(tmp_path, port)
    try:
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(
                    headless=True, args=["--autoplay-policy=no-user-gesture-required"])
            except Exception as e:
                pytest.skip(f"chromium not available: {e}")
            page = await (await browser.new_context()).new_page()
            await page.goto(f"http://127.0.0.1:{port}/index.html")
            await page.wait_for_function("() => document.getElementById('v').readyState >= 1")
            await page.evaluate("() => window.__startPlay()")
            await page.wait_for_function("() => window.__state().isPlaying === false", timeout=8000)
            await page.wait_for_timeout(200)
            await page.evaluate("() => window.__installSlowSeek(200)")
            flips_before = await page.evaluate("() => window.__flips()")
            # Keyboard path: mirror the playPause command — rewind via the same
            # guard WITHOUT an inline <video> rewind (the page's runEffects gate
            # must carry the load, like Preview's rAF fix does).
            await page.evaluate("""() => {
              const st = window.__state();
              if (!st.isPlaying && st.playhead >= 2.0 - 1/30) { window.__kbRewind(); }
              window.__startPlay();
            }""")
            await page.wait_for_timeout(2500)
            transitions = await page.evaluate("() => window.__flips()") - flips_before
            state = await page.evaluate("() => window.__state()")
            await browser.close()
        assert transitions <= 2, (
            f"keyboard replay oscillated {transitions}x; final state={state}")
    finally:
        getattr(server_th, "_server").shutdown()
```

For this test to exercise the keyboard path, add `window.__kbRewind` to `PAGE_HTML`'s script (it rewinds the store playhead to 0 WITHOUT touching `v.currentTime`, then relies on the page's `runEffects` gate — the mirror of Preview's Task 3 fix). Add near the other `window.__*` helpers:

```javascript
window.__kbRewind=()=>{setPlayhead(0);};
```

and ensure the page's `runEffects` loop already carries the Task 3 `mediaClockValid`/`clockStart` gate (added when you updated the page in Task 4 Step 3).

- [ ] **Step 4: Run the full regression file — expect both tests PASS**

Run: `uv run pytest tests/test_playback_replay.py -v -s`
Expected: **2 passed** (or skipped if chromium unavailable). Both button and keyboard replay paths show `transitions <= 2`.

- [ ] **Step 5: Typecheck + commit**

```bash
cd frontend && npx tsc --noEmit && cd ..
git add frontend/src/keymap/commands.ts frontend/src/components/Preview.tsx tests/test_playback_replay.py
git commit -m "fix(keymap): share replayFromStart; deterministic keyboard replay rewind"
```

---

## Task 6: End-to-end verification against the REAL app (macOS baseline + slow-seek injection)

**Files:**
- No code changes. This task drives the real built app to confirm the fix holds end-to-end, using the same technique that reproduced the bug.

Rationale: The unit-style page proves the logic; this proves the shipped bundle. macOS alone will show clean replay even before the fix (it never tripped), so we MUST inject seek latency into the real app to confirm the fix — matching how the bug was originally proven.

- [ ] **Step 1: Build the frontend and start the backend headless**

```bash
cd frontend && npx tsc --noEmit && npx vite build && cd ..
PYTHONPATH="$PWD/src" ANTHROPIC_API_KEY="" VAE_PORT=8766 \
  .venv/bin/python -m uvicorn video_ai_editor.main:app --host 127.0.0.1 --port 8766 &
sleep 4 && curl -s -o /dev/null -w "health: %{http_code}\n" http://127.0.0.1:8766/api/health
```

Expected: `health: 200`. (If 8766 is taken, pick another port.)

- [ ] **Step 2: Create a session with a short SPLIT timeline (concat preview > edl.duration)**

```bash
SID=$(curl -s -X POST http://127.0.0.1:8766/api/sessions | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")
ffmpeg -y -f lavfi -i testsrc=size=320x240:rate=30:duration=3 -c:v libx264 -pix_fmt yuv420p /tmp/clip3s.mp4 2>/dev/null
curl -s -X POST "http://127.0.0.1:8766/api/sessions/$SID/upload" -F "file=@/tmp/clip3s.mp4" >/dev/null
curl -s -X POST "http://127.0.0.1:8766/api/sessions/$SID/dispatch" -H "Content-Type: application/json" -d '{"tool":"split_at","args":{"time":1.5,"track":"v1"}}' >/dev/null
echo "session $SID ready (2-clip v1)"
```

Expected: prints a session id; the session auto-loads as `sessions[0]`.

- [ ] **Step 3: Drive the real app with injected seek latency and assert no oscillation**

Create `/tmp/verify_realapp.js` (uses the repo's `playwright-core` or the scratchpad one) and run it. It navigates to the real app, plays to the end, installs a 200ms seek delay on the real `<video>`, clicks replay, and samples the transport button label for oscillation:

```javascript
const { chromium } = require('playwright-core');
const EXE = process.env.HOME + '/Library/Caches/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-mac-arm64/chrome-headless-shell';
const sleep = ms => new Promise(r=>setTimeout(r,ms));
(async () => {
  const b = await chromium.launch({ executablePath: EXE, headless: true, args:['--autoplay-policy=no-user-gesture-required','--use-gl=swiftshader','--no-sandbox'] });
  const p = await b.newPage();
  await p.goto('http://127.0.0.1:8766/', { waitUntil:'load' });
  await p.waitForSelector('.transport button', { timeout:15000 });
  await p.waitForFunction(()=>{const v=document.querySelector('video');return v&&v.readyState>=1;},{timeout:15000});
  await p.click('.transport button');
  for (let i=0;i<70;i++){const s=await p.evaluate(()=>{const v=document.querySelector('video');return {ct:v.currentTime,end:v.ended,pause:v.paused};}); if(s.end||s.ct>=2.95)break; await sleep(100);} 
  await sleep(300);
  // Inject slow seek on the REAL <video>
  await p.evaluate(()=>{const v=document.querySelector('video');let pr=Object.getPrototypeOf(v),d=null;while(pr&&!d){d=Object.getOwnPropertyDescriptor(pr,'currentTime');if(!d)pr=Object.getPrototypeOf(pr);}Object.defineProperty(v,'currentTime',{configurable:true,get(){return d.get.call(this)},set(val){const s=this;setTimeout(()=>{try{d.set.call(s,val)}catch{}},200);}});});
  const btnBefore = await p.evaluate(()=>document.querySelector('.transport button').textContent);
  await p.click('.transport button');
  const btns=[]; for(let i=0;i<30;i++){btns.push(await p.evaluate(()=>document.querySelector('.transport button').textContent)); await sleep(100);} 
  const flips = btns.filter((x,i)=>i>0&&x!==btns[i-1]).length;
  const last = await p.evaluate(()=>{const v=document.querySelector('video');const s=document.querySelector('.transport span');return {pill:s.textContent,ct:+v.currentTime.toFixed(2),pause:v.paused};});
  console.log('button flips after replay =', flips, '| final =', JSON.stringify(last));
  console.log(flips<=2 ? 'PASS: no oscillation, replay clean' : 'FAIL: oscillation persists');
  await b.close();
})().catch(e=>{console.error(e.message);process.exit(1);});
```

Run: `node /tmp/verify_realapp.js`
Expected: `button flips after replay = <=2` and `PASS: no oscillation, replay clean`, with `final` showing `ct` advancing past 0 (i.e. it actually replayed) — not pinned at 3.00.

- [ ] **Step 4: Tear down the verification backend**

```bash
# stop the port-8766 uvicorn started in Step 1 (do NOT kill any other backend)
pkill -f "uvicorn video_ai_editor.main:app --host 127.0.0.1 --port 8766" || true
```

- [ ] **Step 5: Commit any test-harness helper you kept (optional)**

If you promoted `/tmp/verify_realapp.js` into the repo (e.g. `tests/manual/verify_replay.js`), commit it; otherwise skip. Do not commit `/tmp` artifacts.

---

## Task 7: Full regression sweep (no collateral breakage)

**Files:** none (verification only).

- [ ] **Step 1: Frontend typecheck + build (the CI gate)**

Run: `cd frontend && npx tsc --noEmit && npx vite build`
Expected: both succeed. This is exactly what CI runs (NOT `npm run build`, which has pre-existing `tsc -b` failures per `CLAUDE.md`).

- [ ] **Step 2: Backend + Playwright tests**

Run: `uv run pytest tests/test_playback_replay.py tests/test_webcodecs_scrub.py tests/test_frontend_smoke.py -v`
Expected: playback-replay tests PASS; the scrub/smoke tests PASS or skip cleanly (chromium/mp4box availability) — and are unchanged by this work.

- [ ] **Step 3: Lint delta check (not a hard gate)**

Run: `cd frontend && npm run lint`
Expected: no NEW errors in `Preview.tsx` / `store.ts` / `commands.ts` beyond the documented ~31 pre-existing problems (`CLAUDE.md`). The single `eslint-disable-next-line react-hooks/exhaustive-deps` added in Task 5 Step 2 is intentional and documented inline.

- [ ] **Step 4: Manual smoke on the real app (human-verifiable, macOS)**

Launch `bash run.sh`, load a session with a short multi-clip timeline, press Space to play to the end, then press Space again and click ▶. Confirm: playback restarts from 0 immediately, the counter does not stick at the duration, and the button does not flicker. (macOS will pass even pre-fix; the authoritative proof is Task 6's slow-seek injection.)

- [ ] **Step 5: Revert any incidental `uv.lock` drift, then final commit**

```bash
git checkout uv.lock 2>/dev/null || true   # `uv run` can rewrite it; keep the diff scoped
git status
git add -A && git commit -m "fix: replay-from-end no longer freezes on slow-seek browsers (Windows/WebView2)" || echo "nothing left to commit"
```

---

## Self-Review

**Spec coverage:**
- "video once played till the end does not work if played again … glitches out … cannot be played or paused" → root cause (rAF clock trusts stale `currentTime` → re-clamp → play/pause oscillation) fixed in Tasks 3 (clock gate) + 4 (deterministic button rewind) + 5 (keyboard rewind), amplifier removed in Task 2 (no-op guards). ✅
- "windows and localhost users on both platforms" → the fix is environment-independent by construction (no longer depends on seek latency); Task 6 proves it via injected latency, Task 7 Step 4 is the macOS human smoke. ✅
- "Identify the root cause … provide factual evidence" → done pre-plan (seek-latency experiment: DELAY=0 clean vs 200ms oscillate); Task 1 encodes that evidence as a permanent regression test. ✅
- "fix said and other issues discovered" → other discovered defects fixed: (a) button-only patch left the keyboard playPause path racy → Task 5; (b) no equality guard on `setPlaying`/`setPlayhead` → Task 2; (c) duplicated end-rewind logic across button+keymap → unified via `replayFromStart` (Task 2/4/5). ✅
- "implemented end to end" → Tasks 1–7 cover test → store → clock → button → keyboard → real-app verification → regression sweep. ✅

**Placeholder scan:** No TBD/TODO; every code step shows complete code; commands have expected output. ✅

**Type consistency:** `replayFromStart(): boolean` declared in the store interface (Task 2 Step 1), implemented (Task 2 Step 2), and called in `Preview.tsx` button (Task 4) and `commands.ts` (Task 5) with the same name/signature. `mediaClockValid`, `clockStart`, `FORWARD_EPS`, `RESYNC_TOL` are all defined and used within the single rAF effect (Task 3). ✅

**Known residual risk (documented, not a gap):** `FORWARD_EPS`/`RESYNC_TOL` are heuristics; if a real preview legitimately starts >0.5s from the requested time, the clock free-runs briefly longer — benign (playhead still advances on the wall clock; A/V re-syncs once `mediaClockValid` turns true). The end-clamp guard `(mediaClockValid || !vid || vid.ended)` still allows a genuine wall-clock arrival at `duration` to stop playback normally.
