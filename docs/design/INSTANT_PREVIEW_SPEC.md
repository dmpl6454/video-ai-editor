# Instant Preview Engine: specification (Wave D, design change #1)

Status: lead-architect synthesis of designs A (dual `<video>`), B (WebCodecs + canvas) and C (MSE + all-intra proxy segments), and of the three judgements on them. The repo was read only; nothing in it was changed while this was written. Wave C is still running in the same tree.

Target engine: **WKWebView on macOS 26 and 27** (the PyWebView cocoa backend: a default `WKWebViewConfiguration` with `defaultDataStore`). Chromium is a development convenience and is never the acceptance engine.

---

## 0. Decision

**Backbone: C.** The v1 picture is drawn by one muted `<video>` fed through **Media Source Extensions (MSE)**. The client writes its fMP4 fragments itself, on the project frame grid. The frames come from **all-intra, stitchable, 8-bit 4:2:0, BT.709-tagged proxies** of every source.

Why C wins:
- It has the strongest evidence gathered in WKWebView itself. Across 210 bar-coded frames covering cuts, reverse and 2x, it showed 0 mismatches and 0 missing frames. Mid-frame paused seeks were 42 of 42 exact, in 1 to 2 ms.
- It measured **15 ms from edit to correct pixel**.
- WebKit re-enqueues overwritten frames even 4 frames ahead of the playhead.
- Changing resolution through a new init segment was seamless.
- The MSE clock drifted 0.8 ppm against the AudioContext clock, with a residual σ of 0.42 ms.
- Decoding is native (VideoToolbox). Memory is bounded by the SourceBuffer window. The JavaScript only writes `moof`/`mdat` boxes.

How C overcomes the other two:
- **Against A:** A plays back exactly only while paused. Its seams depend on play-start jitter of 30 to 50 ms. It has no path for reverse or speed curves. Its audio goes through `MediaElementAudioSourceNode`, which has open WebKit bugs 221334 and 215314. It also needs about 10 media elements, against a WebKit element budget the repo has already hit (`pipDraw.ts:157`).
- **Against B:** B would mean writing a media player from scratch: demux, GOP walks, a frame cache, and A/V sync. WebKit applies no back-pressure to held `VideoFrame`s (298 held 1080p frames is about 925 MB). B also has to correct edit lists for both video (+2 frames) and AAC (1024 priming samples).

**What is grafted in:**

| From | Idea | Where it lands |
|---|---|---|
| B | WebCodecs `VideoDecoder` as a **second lane (laneB)** for the incoming side of transitions, and later for PiP. It reads the *same* all-intra proxy samples, so every decode is a single IDR with no GOP walk and no large cache. | §3, Phase 3 and 4 |
| B | A line-for-line port `timebase.ts` of `edl/timebase.py`. The output-frame to source-frame map is specified by a **golden table rendered by the real compositor**, not by prose. Parity follows the existing `textLayout.ts` ↔ `text_overlay.py` pattern. | §6, §13 |
| B | A mix graph that can be built on `OfflineAudioContext`, so the client mix is deterministic and can be diffed against the server's audio graph. | §3.6, §13 |
| B | The server render treated as *one more source* that goes through the same client path, so the player is never swapped. | §5.3 "bakes" |
| B | A worker `OffscreenCanvas` compositor, confirmed to work in WK. | Phase 4 |
| B, C | `/dispatch` returns the post-op EDL. | Phase 1 |
| Judgement 2 | The server returns a **Python reference program map** per `render_hash`. The client checks exact equality on every edit, and any mismatch automatically forces BAKED for that range. | §8 |
| Judgement 2 | Audio is **ffmpeg-decoded FLAC sidecars**, not WebKit's AAC decode: the same PCM the export decodes, with no priming or edit-list trap. | §5.2 |
| Judgement 2 | Ducking and loudness are marked APPROX until the matching server curve arrives. | §7, Phase 2 |
| A | A clock that publishes the **presented** frame time (rVFC `mediaTime`). Text, Sticker and PiP draw from it. | §3.5 |
| A | Transport calls (`play()`, `AudioContext.resume()`) made synchronously inside the gesture handler. | §3.5 |
| A | Trim-drag edge preview: while a handle is dragged, the frame at the handle is shown with no dispatch. | Phase 2 |
| A | Degraded tier: one paused `<video>` on the normalized master, seeked with the +1 ms bias, when a source cannot be proxied. | §7 |
| New | **On-demand span encoding.** All-intra spans are independent, so the server can encode the 2 s span under the playhead first. A new import is client-previewable in about 0.5 s instead of after the whole proxy finishes. | §5.1 |
| New | **Clip-keyed bake reuse.** A move of an effected clip reuses the frames already baked for it. | Phase 2 |

Rejected:
- `MediaElementAudioSourceNode` as an audio path.
- Long-GOP proxies. `appendWindowStart` mid-GOP drops frames up to the next keyframe (measured in WK).
- Client-side optimistic EDL editing. Python stays the only authority on ripple, snapping and the like. Drag-time provisional maps are display-only.
- Changing the GOP or pixel format of the export masters.

---

## 1. Goals and non-goals

### Goals
1. **G1.** Cuts, splits, trims, moves, deletes, ripple, gap insert/remove and undo/redo are visible and audible within the latency budgets of §11, paused or playing, with **no server render on the critical path**.
2. **G2.** The preview is frame-exact against export for everything classed EXACT. Output frame `k` shows the same source frame the export shows, per the `edl/timebase` rules (§6), paused **and** during playback, including constant speed, speed curves, reverse and freeze.
3. **G3.** Audio is sample-addressed on one AudioContext. Gain, gain envelope, mute/solo, fades and channel mode are exact; duck and loudness are exact once the server curve lands. A/V offset stays within 10 ms steady state.
4. **G4.** Anything the client cannot draw exactly degrades **per time range**: the last good frame stays on screen, a small corner spinner shows, and server frames are spliced into the same player once `previewHash == render_hash`. The rest of the timeline is never blocked.
5. **G5.** Memory is bounded and flat in the WebContent process: a 12-minute 1080p timeline looped for 30 minutes grows by less than 1 MB/min.
6. **G6.** Every phase can ship on its own behind the `preview.engine` setting (`auto | client | server`), and there is an automatic fallback to today's server preview.
7. **G7.** Every acceptance assertion runs in real WKWebView (the `wk.py` harness) on macOS 26 and 27.

### Non-goals
- **N1.** Replacing export. Export is always the server compositor and is ground truth. Nothing the client produces is ever exported.
- **N2.** Client-side edit semantics. The dispatch round trip (about 10 to 40 ms on loopback) stays on the path for committed edits.
- **N3.** Pixel identity with export. Proxies are 720p all-intra, so EXACT means *the same source frame, geometry and timing*, with pixels inside the §8 PSNR budgets.
- **N4.** Porting every effect. Custom ffmpeg-expression transitions, `post_filter`, AI effects, bgremove alpha (until Phase 4) and grain/VHS noise stay BAKED or APPROX.
- **N5.** Changing the export masters' GOP, pixel format or colour. The 6 of 82 workdir masters that are 10-bit or 4:4:4 stay as they are for export. The client only ever sees proxies.
- **N6.** iOS or Chromium product support. Headless Chromium is used for fast CI only.
- **N7.** Running the engine inside the phone-pairing remote view (`PhonePanel`). It keeps today's server preview.

---

## 2. Evidence this spec relies on (WKWebView, macOS 27, pywebview-style config)

| Fact | Source |
|---|---|
| MediaSource, ManagedMediaSource, `changeType`, rVFC all present | C probe `mse_wk.json` |
| Two same-size x264 all-intra `stitchable=1` encodes have an identical `avcC` | C probe |
| 42/42 mid-frame paused seeks `(k+0.5)/fps` exact at `seeked`, 1 to 2 ms | C probe |
| Exact-boundary seeks `k/fps`: 2 of 3 did **not** repaint | C probe; same family as QA-077 |
| Playback of 210 frames: 0 mismatches, 0 missing | C probe |
| Overwrite at the paused playhead: append 1 ms, re-seek 1 ms, **edit to pixel 15 ms** | C probe |
| Overwrite while playing, 4 frames ahead: 0 stale frames (Chromium: 3 stale) | C probe |
| A gap in the buffered ranges stalls playback | C probe |
| Resolution switch through a new init segment: seamless | C probe |
| `appendWindowStart` mid-GOP drops frames to the next keyframe | C probe |
| MSE clock vs AudioContext output clock: 0.8 ppm, σ 0.42 ms | C probe |
| SourceBuffer quota: QuotaExceeded at about 289 MB | C probe |
| `decodeAudioData` of 10 s FLAC: 33 ms | C probe |
| `VideoDecoder` avc1 High: supported. High10, 4:2:2, 4:4:4: rejected | B probe `wk4.json` |
| `texImage2D(video or VideoFrame)` ≤ 1 to 3 ms, frame-exact | A and B probes |
| 8 concurrent `VideoDecoder`s fine; held frames get no back-pressure | B probe |
| WebGL2: 3D textures (MAX 2048), RGBA16F and RGBA8 LUTs, `EXT_color_buffer_float` | B probe |
| `AudioContext` `outputLatency` 15.6 ms, `getOutputTimestamp` works, `setValueCurveAtTime` present | B probe |
| Worker: `VideoDecoder`, `OffscreenCanvas` webgl2, `VideoFrame` transfer, worker rAF | B probe `wk5.json` |
| Untagged H.264 decodes as BT.709 limited range, within ≤ 2 levels of ffmpeg `in_color_matrix=bt709` | B probe |
| `captureStream` and `SharedArrayBuffer` are **absent** | earlier probe |
| WebKit concurrent media-element budget is small | `frontend/src/lib/pipDraw.ts:157` comment |
| Normalized masters: x264 default keyint 250, B-frames, elst `media_time` of 2 frames, AAC priming 1024 | A and B probes, ffprobe |

Probe pages and harness: `/private/tmp/claude-501/-Users-sudhanshu-Dashboard/707ade2d-c314-48ce-bfc2-f30c1ec3135d/scratchpad/{probe,mse}/` (`wk.py`, `mse.html`, `mse2.html`, `probe4.html`, `gen.sh`, `prep.py`). Phase 1 promotes `wk.py` into the repo as a test harness (§13).

---

## 3. Architecture

```
                         ┌──────────────────────────── SERVER (FastAPI, loopback) ─────────────────────────────┐
 gesture ─► store.dispatch ─► POST /dispatch?wait=1&include=edl ─► _dispatch_sync ─► {result, edl, edl_hash,     │
   (in handler: engine.play/pause,  AudioContext.resume)                                 render_hash}            │
                         │                                                                                        │
 ingest/normalize ─┬────►│ ingest/proxy.py (NEW): all-intra stitchable 720p spans + FLAC 5 s chunks, niced,       │
 AI outputs cache/ ┘     │   eager in background + ON-DEMAND per span; cancel scope PROXIES                      │
                         │ render/frame_map.py (NEW): reference program map per render_hash (RLE)                │
                         │ render/bake.py (NEW): all-intra spans of preview.mp4 frames (BAKED ranges)            │
                         │ render/duck_curve.py (NEW, P2): duck gain curve per render_hash                       │
                         │ POST /preview: CHANGED client-mode cadence (1.5 s idle, niced) unless BAKED needed    │
                         └──────────────▲─────────────────────▲───────────────────────▲─────────────────────────┘
                                        │ edl (immediately)   │ spans / flac / maps   │ bakes, curves (later)
┌───────────────────────────────── frontend/src/lib/preview/ (NEW, no React) ─────────┴─────────────────────────┐
│ engine.ts  PreviewEngine.setTimeline(edl, renderHash) · play() · pause() · seek(k) · status events           │
│                                                                                                               │
│ timeline/  timebase.ts ─ framePlan.ts ─ frameMap.ts ─► programMap.ts (SoA, per-clip memo) ─► diff → dirty k  │
│            support.ts (EXACT | APPROX | BAKED | PENDING per range, per phase capability)                      │
│                                                                                                               │
│ media/     proxyIndex.ts + spanCache (LRU 128 MB) ─► fmp4Writer.ts (timescale 240000, tfdt = k·ticks)        │
│            laneA.ts: MediaSource + 1 SourceBuffer on hidden <video muted playsinline>  (window −10 s…+30 s)  │
│            laneB.ts (P3): VideoDecoder over the same all-intra samples, VideoFrame budget + leak counter     │
│                                                                                                               │
│ render/    compositor.ts (WebGL2, 1 canvas): per-frame params from programMap[k]                              │
│            geometry (fit/cover, transform+keyframes, crop-zoom, flip, opacity) → colour (P2: eq, colorbalance,│
│            lut3d) → transition (P3: xfade ports, laneB texture) → effects (P4) ; gap = black                   │
│                                                                                                               │
│ audio/     audioChunks.ts (FLAC → AudioBuffer, LRU 96 MB) → mixGraph.ts (clip gain/env/fades/channel mode    │
│            → lane bus mute/solo → music duck → master loudness gain → limiter) on AudioContext OR Offline    │
│                                                                                                               │
│ clock/     presentedClock.ts: rVFC(laneA) → presented k; audio anchor via getOutputTimestamp; drift monitor  │
│ verify/    divergence.ts: frame_map equality per edit; idle pixel/audio sampling vs bakes; telemetry          │
└───────────────────────────────────────────────┬──────────────────────────────────────────────────────────────┘
                                                ▼
            Preview.tsx: <canvas> (engine) under TextLayer / StickerLayer / PiP (unchanged drawing,
            clock = presentedClock) · corner spinner per fidelity state · server-mode path kept intact
```

### 3.1 The core trick (from C)

The server never renders a timeline for structural edits. It provides **source frames only**:
- every frame is an IDR;
- every same-size proxy shares one SPS/PPS (x264 `stitchable=1`, with a fixed profile, level and colour tags);
- each proxy has a per-frame index.

For each output frame `k` the client writes one MSE sample:
- `tfdt = k · T` in timescale **240000**, where `T = 240000 · den / num` is an integer for every `STANDARD_RATES` entry. Checked: 23.976 → 10010, 24 → 10000, 25 → 9600, 29.97 → 8008, 30 → 8000, 48 → 5000, 50 → 4800, 59.94 → 4004, 60 → 4000.
- The sample's bytes are the proxy frame that `programMap[k]` names.

So:
- **Duplicates** (fps conform-up, slow motion, freeze) are the same bytes appended again.
- **Reverse** is descending indices.
- **Speed** is index stepping.
- **Cuts, trims and moves** only change which bytes sit at which `k`.

Because every frame is independently decodable, MSE never has a dependency to break.

### 3.2 laneA (MSE) invariants
- One `MediaSource`, one `SourceBuffer('video/mp4; codecs="avc1.64001F"')` (level from the recipe), `mode = 'segments'`, `timestampOffset = 0`.
- `laneA.currentTime` **is render-clock seconds**, so `k = round(mediaTime · R)` with `R = rate_of(canvas.fps)`.
- **The buffered range is always contiguous** from `playhead − 10 s` to `playhead + 30 s`, clamped to `[0, total)`. A gap stalls WebKit, so gaps in the *timeline* get **filler samples**: the previous appended sample's bytes (the first clip's frame when the timeline starts with a gap). The compositor draws black for them because `programMap[k].kind == gap`.
- **Init switching.** The writer tracks `lastInitKey` and appends an init segment before any fragment whose proxy size class differs. The size class is (W×H, recipe), and one class shares one `avcC`.
- **Fragments** are 1 to 30 frames. Appends are queued strictly one at a time (`updateend`). Order: frames at the playhead first (1 to 5 while paused; from `playhead + 150 ms` while playing), then outward in 1 s fragments.
- **Shrinking.** `remove(a, b)` is issued outside the window and whenever the timeline got shorter. `mediaSource.duration` is set to `total/R` after any required `remove()`. `endOfStream()` is never called, so the source stays editable.
- **Quota.** On `QuotaExceededError`: remove behind the playhead down to `playhead − 2 s`, halve the look-ahead, retry once, then raise `status: degraded` with telemetry.
- **Paused seeks are always `(k + 0.5)/R`.** Exact-boundary seeks fail to repaint in WebKit.
- **Media elements.** laneA, the degraded-tier `<video>` (when active), and the existing PiP and source-drag pools. The engine itself adds **at most 2**, and in client mode laneA replaces today's `preview.mp4` `<video>`. So the element count never exceeds today's. Phase 4 moves PiP to laneB, which brings the total to **4 or fewer**.

### 3.3 laneB (Phase 3; from B)
- One or two `VideoDecoder`s configured from the proxy `avcC` (`optimizeForLatency: true`), and reconfigured when the size class changes.
- For every `k` in `[playhead, playhead + 1 s]` where `programMap[k].b` is set (the incoming side of a transition; PiP from Phase 4), it decodes the single IDR sample. Frames are held in a ring keyed by `k`.
- **Hard budget: 24 held `VideoFrame`s** (about 33 MB at 720p NV12). The owner calls `close()` on eviction, and a leak counter (`opened − closed`) is asserted at zero after tests.
- The compositor, on laneA's rVFC for frame `k`, takes the laneB frame for `k`.
  - Missing while paused: hold the last good frame and show the spinner after 80 ms.
  - Missing while playing: draw side A only for that frame, increment `laneB.drops`, and mark the range APPROX for telemetry.

### 3.4 Compositor (WebGL2, one canvas)
- Canvas size is the display box × DPR, capped at a 1080 short edge. `premultipliedAlpha: false`, `preserveDrawingBuffer: false`.
- Sources:
  - laneA via `texImage2D(video)` on each rVFC. Frame dimensions come from rVFC `metadata.width/height`, never from the element.
  - laneB via `texImage2D(VideoFrame)`.
  - LUTs as 3D textures.
- Pass order mirrors `_build_clip_video_chain` exactly:
  1. fit (contain: scale-down plus black pad; cover: scale-up plus crop)
  2. rotate in place with black corners
  3. crop-zoom and pan
  4. transform and keyframes (evaluated with `lib/overlay.ts`)
  5. colour (P2)
  6. effects (P4)
  7. opacity
  8. video fades (multiply toward black)
  9. transition blend (P3)
- Colour maths run in **BT.709 limited-range YUV** (§6 R12).
- **"Last good frame".** The engine draws only when every texture for `k` is ready; otherwise it leaves the canvas as it is. On pause, a snapshot is copied to a 2D backing canvas (≤ 2 ms) so `webglcontextlost` never shows black. On `webglcontextrestored` the programs and textures are rebuilt. If restore takes longer than 2 s, the engine falls back to server mode (§7).

### 3.5 Clock and transport (A's presented-time idea, C's measured clock)
- **Timeline authority while playing: the presented frame.** In the laneA rVFC callback, `presentedK = round(metadata.mediaTime · R)`. `presentedClock.now()` returns `presentedK / R`, interpolated by `(performance.now() − expectedDisplayTime)` only for the rAF-driven overlay layers between video frames.
- **While paused:** `presentedK = frame_of(store.playhead)`.
- **Overlays.** TextLayer, StickerLayer and pipDraw take `clock.now()` in place of `videoEl.currentTime`, so overlays are frame-locked to the picture actually on screen. The prop defaults to `videoEl.currentTime` in server mode.
- **Store playhead** is written from the clock by the existing rAF loop. `TRUST_TOL` logic is bypassed in client mode.
- **Transport:**
  - Space, click and the L key call `engine.play()` **synchronously** inside the handler. That calls `ac.resume()` and `laneA.play()` in the same task.
  - Pause calls `laneA.pause()`, ramps the master to 0 over 5 ms, then `ac.suspend()` after 20 ms.
  - Seek is paused-seek semantics plus an audio reschedule.
- **Audio anchoring (one hardware clock, measured drift 0.8 ppm):**
  - At play, audio is pre-scheduled to start at the AudioContext time mapped from `performance.now() + L`. `L` is the rolling median of the last 8 measured laneA play-start latencies (seeded at 40 ms).
  - At the first rVFC after play: `ts = ac.getOutputTimestamp()`, and `ctxAt(E) = ts.contextTime + (E − ts.performanceTime)/1000`. The error is `e = ctxAt(expectedDisplayTime) − ctxTimeScheduledFor(mediaTime)`.
  - If `|e| > 4 ms`, the running sources are re-scheduled once with a 5 ms equal-power crossfade.
  - The drift monitor re-checks `e` every 500 ms. Above 20 ms it re-anchors at the next chunk or clip boundary (at most 1 per 2 s). Given 0.8 ppm this should essentially never fire, and it is counted in telemetry.
  - When `getOutputTimestamp()` returns zeros (device change), the engine uses `ac.currentTime + outputLatency + baseLatency` until it recovers.
- **Buffering.** If laneA's `waiting` fires while playing (span not yet fetched or encoded), the engine pauses audio at the same `k`, shows the buffering spinner, and resumes both together from a new anchor.
- **Visibility.** On `visibilitychange` hidden or window occluded (PyWebView `on_minimized`), the engine pauses decode, appends and proxy prefetch.

### 3.6 Audio graph
```
per clip:  AudioBufferSourceNode(s) over FLAC chunks (sample-exact start(when, offset, duration))
           → ChannelSplitter/Merger (channel mode) → clipGain (gain_db × gain_env × fades × mute; automation)
per lane:  → laneBus (track mute/solo)                  [v1 clip audio, audio-on-video lane, music, VO, sfx]
music:     → duckGain (P1: APPROX trapezoid; P2: server curve via setValueCurveAtTime)
master:    → loudnessGain (preview_loudness gain) → limiter (P1: DynamicsCompressorNode ≈; P2: AudioWorklet
             port of PREVIEW_LIMITER "alimiter=limit=0.891251:level=0:latency=1") → destination
```
- `mixGraph.build(ctx, programAudioMap, range)` accepts an `AudioContext` **or** an `OfflineAudioContext`. The live graph and the verification render are one code path.
- Scheduling window: `playhead` to `+4 s`, refilled every 1 s. Each clip's chunks are chained at integer-sample times, `when = anchorCtx + (s − anchorSample)/48000`.
- A structural edit while playing stops the affected sources with a 5 ms ramp and reschedules them from `presentedK + 6` frames (≈ 200 ms).
- A parameter-only edit (gain, fade, mute) rewrites automation with `cancelAndHoldAtTime` plus new ramps. It is audible at the next render quantum, well under 100 ms.
- Speed ≠ 1: `playbackRate = speed` (varispeed), marked APPROX where export uses pitch-preserving `atempo`. Phase 4 adds server tempo sidecars.

---

## 4. Data flow

### 4.1 Committed edit (pointer-up, key, menu, AI op, undo/redo)
1. `store.dispatch(op)` → `POST /api/sessions/{sid}/dispatch?wait=1&include=edl`.
2. `_dispatch_sync` (`main.py:1480`) serialises the post-op EDL under the lock it already holds. It returns `{result, edl, edl_hash, render_hash}`. Cost: one loopback round trip, about 10 to 40 ms.
3. The store sets `edl` and `edlHash` from the response **immediately**. `refreshSoon()` (the 120 ms debounce, `store.ts:783`) still runs, but only for the session fields (undo depth, jobs).
4. `engine.setTimeline(edl, render_hash)`:
   1. `programMap.build`. It is memoized per clip on a timing hash (`start`, `in`, `out`, `speed`, `reverse`, `src`, `freeze`, the seam entries that touch the clip) and a params hash (geometry and colour). It returns `{map, dirtyFrames: [k0, k1) ∪ …, dirtyParams: Set<clipId>}`.
   2. Split: the frames are identical and only the clip ids differ, so `dirtyFrames = ∅` and only the right half's params are rebound.
   3. Trim, delete, ripple, move, insert: dirty from the first changed `k` to the old or new end, whichever is later, intersected with the window.
   4. `support.classify` sets the mode of every range: EXACT, APPROX, BAKED or PENDING.
5. `laneA.schedule(dirtyFrames)`. While paused: append the 1 to 5 frames around `presentedK`, then re-seek `(k+0.5)/R` (measured 15 ms edit to pixel), then the rest of the window. While playing: start from `presentedK + ⌈0.15·R⌉`.
6. The compositor rebinds uniforms for `dirtyParams` on the next rVFC or rAF.
7. The audio engine reschedules the lanes whose clips changed (§3.6).
8. Background:
   - `divergence.checkStructure(render_hash)` fetches `GET /frame_map?h=` (§8). It is async and never blocks.
   - If any range is BAKED or PENDING-BAKE, `renderPreview()` runs at today's 250 ms debounce. Otherwise it runs after 1.5 s idle, with the render process niced (`os.nice(10)`).
   - When `previewHash == render_hash`, BAKED ranges fetch bake spans (§5.3) and append them over the RAW frames.

### 4.2 Drag in flight (Phase 2)
`dragResolve` and `dragVisuals` already compute provisional clip overrides. `programMap.buildProvisional(edl, overrides)` produces a display-only map. For a trim handle, laneA appends the single frame at the handle's in/out and paused-seeks there (A's edge preview). For a move, the moved clip's frames are appended at the ghost position. Nothing is dispatched until pointer-up, and the server EDL then replaces the provisional map.

### 4.3 Playback
laneA plays natively. Each rVFC:
1. compute `k`;
2. upload the texture(s);
3. draw with `programMap[k]` params;
4. publish `presentedK`.

The overlay layers draw on their own rAF from `clock.now()`. The window manager tops up appends every 250 ms. The audio scheduler tops up every 1 s.

### 4.4 New import or AI output
1. Ingest enqueues `proxy.ensure(src, priority=normal)`.
2. When the source enters the timeline, the client asks for the spans at the playhead. The server encodes those spans on demand at high priority (§5.1). The first frame is ready in about 0.3 to 0.6 s.
3. Meanwhile the range is PENDING: last good frame, spinner, and audio from its FLAC chunks, which are encoded first (≥ 100× realtime).

---

## 5. Server contracts

### 5.1 Proxies (`ingest/proxy.py`, NEW)

**Key.** `sha256(realpath, size, mtime_ns, RECIPE_VERSION)[:24]`. The files live at `workdir/proxies/<key>/`:
- `index.json`: recipe, `src_rate {num,den}`, `frames`, `w`, `h`, `span_frames` (= round(2·src_rate)), span states, master pts table digest.
- `init.mp4`: `ftyp` + `moov` with `avcC`.
- `v/NNNN.bin`: a span sample pack. Header `u32 first, u32 count, u32 sizes[count]`, then AVCC length-prefixed samples.
- `a/NNNN.flac`: 48 kHz stereo, exactly 240000 samples per chunk (the last may be short).

**Video recipe (normative):**
```
ffmpeg -nostdin -threads 2 [-ss <keyframe ≤ span start> for on-demand] -i <master> -map 0:v:0
  -vf "<exact frame select for the span>,scale=<short edge 720, even, bicubic>,format=yuv420p"
  -fps_mode passthrough
  -c:v libx264 -preset veryfast -crf 23 -profile:v high -level:v 4.1
  -x264-params keyint=1:min-keyint=1:scenecut=0:bframes=0:stitchable=1
  -colorspace bt709 -color_primaries bt709 -color_trc bt709 -color_range tv
  -f mp4 -movflags +empty_moov+default_base_moof+frag_keyframe  <tmp>  →  split into init.mp4 + span packs
```
- **Frame identity.** Proxy frame `i` is master frame `i` in presentation order **after** the edit list: ffmpeg applies elst on decode, so the 2-frame elst and AAC priming traps cannot arise.
- **Asserted.** The proxy frame count equals the master's decoded frame count, which is recorded once at ingest. On-demand spans select by the master pts table: `trim=start_pts:end_pts` in the stream timebase, then `setpts` rebased, then asserting that `count` and first pts match. A mismatch sets proxy state `failed`, and the source falls to the degraded tier (§7).
- **Short edge.** 720 by default. Phase 4 adds a 1080 variant on demand for clips with `transform.scale > 1.5`, crop-zoom > 1.5, or a canvas display short edge > 900 device px.
- **Scheduling.**
  - Eager background encode after normalize and after every AI output lands in `cache/`, with priority by timeline proximity.
  - Niced (`nice 10`), at most 2 concurrent encodes, paused while an export runs.
  - Cancellable through a new `cancel.PROXIES` scope (latest-wins per source).
  - On-demand span requests jump the queue.
- **Audio sidecar.** Decode the master's audio once (`aresample=48000, aformat=fltp:stereo`) to f32. Python slices it into exact 240000-sample chunks and encodes FLAC. Silent-track masters produce a `silent: true` flag and no chunks.
- **Disk.** 720p all-intra at crf 23 comes to about 4 to 5 Mb/s, roughly 35 MB per source-minute. Proxies are registered with `render/cache_budget.py` in their own LRU class. Evicted spans are rebuilt on demand.

### 5.2 Routes (`api/preview_routes.py`, NEW; mounted from `main.py`)

| Route | Returns | Notes |
|---|---|---|
| `POST /api/sessions/{sid}/dispatch?include=edl` | adds `edl`, `edl_hash`, `render_hash` | CHANGED. Default response unchanged for old clients |
| `GET /api/sessions/{sid}/media` | rows gain `fps {num,den}`, `frames`, `pix_fmt`, `has_audio`, `proxy {key, state, w, h}` | CHANGED |
| `GET /api/proxies/{key}/index.json` · `init.mp4` · `v/{n}.bin` · `a/{n}.flac` | proxy data | `v/` and `a/` encode on demand, blocking ≤ 2 s, else `202` + `Retry-After: 0.2`. Keys must be referenced by a live session (containment check as in `/files`). Exempt from the per-path 60 rps bucket, with a global cap of 400 rps |
| `GET /api/sessions/{sid}/frame_map?h=<render_hash>` | reference program map, RLE (§8) | cached per hash |
| `GET /api/sessions/{sid}/bake/{render_hash}/init.mp4` · `v/{n}.bin` | bake spans (§5.3) | 404 until `preview.mp4` for that hash exists |
| `GET /api/sessions/{sid}/duck_curve?h=` (P2) | `{lanes: {trackId: {rate_hz: 100, db: float32 base64}}}` | from the `duck_probe` stem |
| `GET /api/sessions/{sid}/preview_loudness?h=` (P2) | `{gain_db}` | |
| `GET /api/sessions/{sid}/luts/{name}.cube` (P2) | LUT file | contained to the LUT dirs |

### 5.3 Bakes (`render/bake.py`, NEW)
- **Phase 1.** A bake of `render_hash h` is simply a proxy of `previews/h.mp4`, using the same recipe at the preview's own size. Bake frame `k` is output frame `k`, because `render_preview` renders the render clock from 0 at project fps. The preview **already excludes text, stickers and PiP** (see `Preview.tsx:456-481`, pixel-ownership rule), so the client overlays never double-draw. The client maps BAKED ranges to `{src: 'bake:'+h, srcFrame: k}`.
- **Phase 2: clip-keyed reuse.** A bake frame is also indexed as `(clipChainHash, localFrame)`, where `clipChainHash` = the hash of every field in `_build_clip_video_chain` plus `src`, `in` and `speed`. After a move, the client asks `bake/{oldHash}` for the frames of an unchanged clip at their old `k`, so no new render is needed. Transition overlaps are excluded from reuse.

---

## 6. Frame-accuracy and A/V-sync rules (normative, tied to `edl/timebase.py`)

- **R1. Output grid.** `R = rate_of(canvas.fps)` as a rational. Output frame `k` spans `[k, k+1)/R`. MSE ticks: `tick(k) = k · T`, `T = 240000·R.den/R.num` (an integer for all `STANDARD_RATES`). If `rate_of` yields a non-standard rate and `T` is not an integer, the engine refuses and uses server mode.
- **R2. One timebase in two languages.** `timeline/timebase.ts` ports `rate_of`, `fps_float`, `frame_duration`, `frame_of` (including the `−1e-6` tolerance), `time_of`, `quantize`, `frames_between`, `source_rate`, `floor_to_frame`, `ceil_to_frame`, `samples_for_frames` and `seek_preroll`, rational-exact with integer `{num, den}` and no float accumulation. `frameStep.ts` re-exports from it, `overlayGate.ts` uses it, and the frontend keeps exactly one copy. Golden: `tests/test_timebase_golden.py` writes `frontend/src/lib/__fixtures__/timebase_cases.json` over `t ∈` a grid including exact multiples ± 1e-7, times all `STANDARD_RATES` plus off-standard inputs. Vitest asserts 100%.
- **R3. V1 frame plan.** `framePlan.ts` ports `_v1_frame_plan` (`compositor.py:478`): a clip starts at `frame_of(start)`, occupies `clip_frames(c) = max(1, frame_of(effective_duration))`, gaps are whole frames, overlaps pack with `max(cursor, start)`, and the tail gap runs to `frame_of(total_duration)`. Seams come from a port of `seam_table_for(clips, transitions, fps)` (`edl/schema.py:909`) **with the whole-frame rounding**. `timelineLayout.seamTable` gains an `fps` argument so the timeline UI and the engine share it. Golden: `frame_plan_cases.json`, generated from the fuzz corpus of `test_b8_fuzz_sweep.py` plus hand cases (overlaps, sub-frame gaps, transitions longer than a side).
- **R4. Output frame to source frame.** For clip-local output frame `j ∈ [0, clip_frames)`, the source frame is what the export chain picks:
  - `clip_input_args` seeks to `in − seek_preroll(in)`;
  - then `setpts=PTS-STARTPTS`, `setpts=PTS/speed` (or the speed-curve integral);
  - then `fps=R` (round=near);
  - then `tpad=stop_mode=clone`, `trim=end_frame=clip_frames`;
  - with reverse and freeze per `render/reverse.py` and the freeze rules.

  The **normative definition is the golden table**, not this prose. `tests/test_frame_map_golden.py` renders bar-coded counter sources through the real compositor (`render_preview` on one-clip EDLs) across:
  - source rates 23.976/25/29.97/30/50/59.94/60, each in 24/25/29.97/30/60 projects;
  - speeds 0.25/0.5/1/1.5/2/4, two speed curves, reverse, freeze;
  - in-points on-grid, ±0.3 frame and ±0.5 frame.

  It decodes the bar per output frame and writes `frame_map_cases.json`. `render/frame_map.py` (the Python reference) and `frameMap.ts` must both reproduce it 100%.
- **R5. Source index space.** Index `i` means the master's i-th frame in presentation order after the edit list, which is the proxy's i-th sample. Asserted by equal counts (§5.1).
- **R6. Non-v1 lanes.** Text, stickers, PiP and captions keep their placement through `renderSpanOf`, `renderWindow`, `layoutTime` and `enable_window`, evaluated at `clock.now() = presentedK/R`.
- **R7. Seeks.** laneA paused seeks go to `(k + 0.5)/R`. The degraded `<video>` tier seeks to `pts(src frame) + PAUSED_SEEK_BIAS_S` (1 ms) and confirms with rVFC `mediaTime`, re-seeking once on mismatch. Never seek to an exact boundary.
- **R8. Presented frame.** `presentedK = round(mediaTime·R)`, taken from rVFC only. `currentTime` is never used to pick a frame in client mode.
- **R9. Audio placement.** Output frame `k` begins at output sample `S(k) = samples_for_frames(k, R)` (48 kHz). A clip whose plan starts at `k0` and spans `n` frames occupies output samples `[S(k0), S(k0+n))`. That matches the server's `atrim=end_sample=M`. The source sample offset for the clip start mirrors the export's audio seek (`clip_input_args` preroll, `audio_mix.input_seek`, then `atrim`), pinned by `audio_map_cases.json`, rendered from click-track sources through `_audio_only_graph`.
- **R10. Seams in audio.** `acrossfade` overlaps by the seam table's exact seconds, per the `seam_table_for` docstring. Curve shapes (`afade` tri/qsin/esin and so on, and the `acrossfade` default) are evaluated by `audio/curves.ts` and pinned by `audio_curve_cases.json`, sampled from ffmpeg at 1 ms.
- **R11. A/V sync.** The sample `S(k)` is heard when frame `k` is displayed. Steady state `|offset| ≤ 10 ms` p95 and ≤ 20 ms max. At play start the offset must be ≤ 1 frame within 100 ms. Measured with the WK tap test (§13).
- **R12. Colour.** Proxies are tagged BT.709 limited. WebKit decodes them within ≤ 2 levels of ffmpeg `in_color_matrix=bt709:in_range=tv`. Shaders convert RGB to BT.709 limited YUV for `eq` and `colorbalance`, apply ffmpeg's formulas in the order of `_build_clip_video_chain`, and convert back. `lut3d` is applied in the RGB domain of the chain with trilinear sampling (ffmpeg's default `interp=tetrahedral` is ported in the shader in Phase 2). Phase 2 also makes the **server** force `in_color_matrix=bt709:in_range=tv` wherever swscale converts untagged sources. That change is guarded by an export colour golden test, because it changes export pixels on untagged sources.
- **R13. Bake frames.** Bake frame `k` of hash `h` is `previews/h.mp4` frame `k` after the edit list, asserted by bar-code in `test_bake.py`.
- **R14. Structural agreement.** On every committed edit, `frameMap.ts` output equals `render/frame_map.py` for the same `render_hash` (§8). A mismatch demotes the range to BAKED.

---

## 7. Fidelity classes and fallback ladder

Every output range carries one mode. `support.ts` decides it from a per-phase capability table: what the client can do exactly, approximately, or not at all.

| Mode | Meaning | UI |
|---|---|---|
| EXACT | Client frames and audio match export within the §8 budgets | none |
| APPROX | Client draws a documented approximation (duck trapezoid, varispeed audio, noise effects, a laneB drop) | small corner spinner while a better answer is pending; "≈" badge in the Properties panel |
| BAKED | Server frames (bake spans) are appended into laneA over the RAW frames; client effects are skipped there | spinner until the bake lands; RAW client frames shown meanwhile |
| PENDING | Proxy spans not ready | last good frame held (paused) or buffering (playing) + spinner after 80 ms |

**Per-range ladder:** EXACT → APPROX → BAKED → PENDING (last good frame). "Last good frame" is literal: the canvas is simply not redrawn.

**Degraded source tier (A).** Used when a source's proxy is `failed`, or the degraded path is forced. That source's ranges are drawn from one paused `<video>` on the normalized master:
- seeked with the +1 ms bias and confirmed by rVFC, for paused frames;
- its texture goes into the same compositor;
- while playing, the range is BAKED.

At most 1 such element exists. 10-bit or 4:4:4 masters are fine here because `<video>` plays them, though with dropped frames when played.

**Engine-level fallback to server mode** (today's `preview.mp4` `<video>` path, kept intact) triggers on any of:
- `!('MediaSource' in window || 'ManagedMediaSource' in window)`, or no WebGL2;
- the non-standard-rate refusal (R1);
- ≥ 3 SourceBuffer `error` events or decode errors within 60 s;
- `webglcontextlost` not restored within 2 s;
- a frame-map mismatch rate above 5% of edits in a session;
- user setting `preview.engine = server`.

The switch is logged, and a one-line toast appears in debug builds only.

---

## 8. Preview-vs-export reconciliation

1. **Authority.** The Python compositor defines behaviour. `render/frame_map.py` is the executable description of its frame selection and is pinned to real ffmpeg output by `test_frame_map_golden.py`. The TS engine mirrors both and is pinned to the same fixtures. Changing a compositor rule without regenerating the fixtures fails CI on both sides.
2. **Structural check (every edit).**
   - `GET /frame_map?h=` returns RLE runs `{k0, n, kind, clip_id, src_key, src_frame0, step_num, step_den, pattern_id?, b?: {...}, progress0?, dprogress?}`. Non-linear runs (fps=near with mismatched rates) carry a delta-encoded `src_frame` array, base64 varint.
   - The client compares with its own map, O(runs).
   - A mismatch marks those `k` BAKED, sends telemetry `{render_hash, first_k, client, server}`, and adds a `console.warn` in dev.
   - Cost is under 5 ms on both sides for 300 clips. It never blocks the display.
3. **Pixel check (idle).**
   - When a bake for the current hash exists and the app has been idle for 3 s, `divergence.ts` samples 8 frames per EXACT or APPROX range.
   - It renders the client composite, with overlays excluded, and the bake frame into an offscreen framebuffer, and computes Y-PSNR at 360p.
   - Thresholds: EXACT ≥ 36 dB; APPROX ≥ 28 dB.
   - A failure demotes that feature class (for example `eq`) to BAKED for the session and sends telemetry.
4. **Audio check (tests and opt-in idle).** The `OfflineAudioContext` render of `mixGraph` over a range is compared with the server's audio-only render of the same EDL range. Criteria: per-50 ms RMS within 0.25 dB for EXACT features and 1 dB for APPROX; cross-correlation lag 0 ± 1 sample.
5. **Accepted, documented differences:**
   - 720p proxy softness, and all-intra crf 23 artefacts;
   - chroma-edge colour differences (≤ 35 levels at single edges, B probe);
   - the varispeed resampler;
   - APPROX effects;
   - AudioContext output-device resampling (the device rate may be 44.1 kHz).

   None of these reach export.
6. **Export never consults the client.** No client state is uploaded.

---

## 9. Module list

### 9.1 New: frontend (`frontend/src/lib/preview/`)

| File | Responsibility | Phase |
|---|---|---|
| `engine.ts` | `PreviewEngine` facade: `setTimeline`, `play`, `pause`, `seek`, `setProvisional`, `dispose`, status events (`mode per range`, `buffering`, `degraded`). No React | 1 |
| `timeline/timebase.ts` | Rational port of `edl/timebase.py` (R2) | 1 |
| `timeline/framePlan.ts` | Port of `_v1_frame_plan`, `clip_frames` and `seam_table_for` with fps rounding (R3) | 1 |
| `timeline/frameMap.ts` | Clip-local output frame → source frame (R4): speed, curves, reverse, freeze | 1 |
| `timeline/programMap.ts` | Struct-of-arrays map (`Int32Array` src key, src frame, clip idx, b-src, b-frame; `Float32Array` progress; `Uint8Array` mode). Per-clip memo, diff → dirty ranges, provisional builds | 1 (provisional: 2) |
| `timeline/support.ts` | Capability table per phase → EXACT, APPROX, BAKED or PENDING per range | 1 |
| `media/proxyIndex.ts` | Fetch and parse `index.json`, span pack parser, span LRU (128 MB of ArrayBuffers), on-demand retry on 202 | 1 |
| `media/fmp4Writer.ts` | Init pass-through, `moof`/`traf`/`tfhd`/`tfdt`/`trun` (+`mdat`) writer, timescale 240000, all samples sync | 1 |
| `media/laneA.ts` | MediaSource and SourceBuffer manager: window, append queue, init switching, filler, quota handling, mid-frame seeks, `waiting` handling | 1 |
| `media/laneB.ts` | WebCodecs lane: decoder pool (≤ 2), `VideoFrame` ring (≤ 24), `close()` ownership, leak counter | 3 |
| `media/degradedSource.ts` | The single paused `<video>` tier (+1 ms bias, rVFC-confirmed) | 1 |
| `render/compositor.ts` | WebGL2 program management, per-frame draw, context loss and restore, 2D snapshot | 1 |
| `render/shaders/geometry.ts` | fit/cover, rotate, crop-zoom, transform, flip, opacity, fades | 1 |
| `render/shaders/colour.ts` | BT.709 conversions, `eq`, `colorbalance`, `lut3d` tetrahedral | 2 |
| `render/lut.ts` | `.cube` parse → RGBA16F 3D texture, cached by name | 2 |
| `render/shaders/xfade.ts` | GLSL ports of the 58 native `xfade` transitions (formulas from `vf_xfade.c`), with a per-transition tolerance table | 3 |
| `render/shaders/effects.ts` | blur, sharpen, vignette, glow, rgb_split, chroma key, mask texture; grain, vintage and VHS as ≈ | 4 |
| `audio/audioChunks.ts` | FLAC fetch + `decodeAudioData`, LRU capped at 96 MB of decoded f32 | 1 |
| `audio/mixGraph.ts` | The graph of §3.6 on `AudioContext` or `OfflineAudioContext`; scheduling window; automation rewrite | 1 |
| `audio/curves.ts` | `afade` and `acrossfade` curve shapes, `gain_env` evaluation (R10) | 1 |
| `audio/limiter.worklet.ts` | AudioWorklet port of `PREVIEW_LIMITER` (limit −1 dBFS, 1 ms latency compensated) | 2 |
| `clock/presentedClock.ts` | rVFC-based presented time, play-start latency estimator, audio anchor, drift monitor | 1 |
| `verify/divergence.ts` | Frame-map equality, idle pixel sampler, telemetry | 1 (pixel: 2) |
| `worker/compositorWorker.ts` | OffscreenCanvas host for `render/` and `media/laneB` | 4 |
| `*.test.ts` alongside each | vitest units and fixture parity | per phase |

### 9.2 Changed: frontend

| File | Change | Phase |
|---|---|---|
| `store.ts` | `dispatch` adds `include=edl` and applies `edl`/`edlHash`/`renderHash` at once. Undo and redo likewise. `renderPreview` cadence in client mode (1.5 s idle, or 250 ms when BAKED is needed). The engine instance lives outside React (module singleton, like `LIVE_FRAMING`) | 1 |
| `components/Preview.tsx` | In client mode, mounts the engine `<canvas>` and the hidden laneA `<video>` in place of the `preview.mp4` `<video>`. Per-range spinner. Passes `clock` to the layers. Keeps the server-mode branch unchanged | 1 |
| `components/TextLayer.tsx`, `components/StickerLayer.tsx`, `lib/pipDraw.ts` | Accept a `clock: {now(): number}` prop (default `videoEl.currentTime`) | 1 |
| `lib/pipDraw.ts` | PiP sourced from laneB, releasing its `<video>` pool | 4 |
| `lib/timelineLayout.ts` | `seamTable(…, fps?)` whole-frame rounding matching `seam_table_for` | 1 |
| `lib/frameStep.ts`, `lib/overlayGate.ts` | Re-export from and use `timebase.ts` | 1 |
| `api.ts`, `types.ts` | New routes and media fields | 1 |
| `lib/settingsModel.ts`, `components/SettingsDialog.tsx` | `preview.engine: auto/client/server` | 1 |
| `lib/dragResolve.ts`, `lib/dragVisuals.ts` | Emit provisional overrides to `engine.setProvisional` | 2 |
| `components/Properties.tsx`, `components/EffectsPanel.tsx` | Slider drags push params to the engine per rAF (no dispatch until commit); "≈" badges | 2 |
| `keymap/*` | J/K/L shuttle incl. reverse (paused frame-steps at 15 fps, or native reverse via descending appends) | 4 |
| `components/FrameScrubber.tsx` | Optional: scrub thumbnails from proxy IDRs (single-sample decode) | 4 |

### 9.3 New: server

| File | Responsibility | Phase |
|---|---|---|
| `src/video_ai_editor/ingest/proxy.py` | Proxy and FLAC sidecar build (eager and on-demand), recipe, frame-count assertion, pts table, span packer | 1 |
| `src/video_ai_editor/api/preview_routes.py` | Proxy, frame_map, bake, duck_curve, loudness and LUT routes, with containment and the rate-cap exemption | 1 (duck, LUT: 2) |
| `src/video_ai_editor/render/frame_map.py` | Reference program map (R4, R14) built from `_v1_frame_plan`, `clip_frames` and `seam_table_for` plus the golden-pinned selection rule; RLE encoder | 1 |
| `src/video_ai_editor/render/bake.py` | Bake spans from `previews/<hash>.mp4`; clip-keyed index | 1 (reuse: 2) |
| `src/video_ai_editor/render/duck_curve.py` | Extract the duck gain curve per render_hash from the `duck_probe` stem path in `audio_mix.py` | 2 |
| `src/video_ai_editor/render/tempo_sidecar.py` | Pitch-preserving `atempo` audio chunks keyed by (src, in, out, speed) | 4 |

### 9.4 Changed: server

| File | Change | Phase |
|---|---|---|
| `main.py` | `_dispatch_sync` + `/dispatch`: `include=edl` returns `edl`, `edl_hash`, `render_hash`. `/media` rows gain fields. Mount `preview_routes` | 1 |
| `ingest/pipeline.py` | Enqueue `proxy.ensure` after normalize. Record the master decoded frame count and pts table | 1 |
| AI output writers (`ai/*`, results into `cache/`) | Enqueue `proxy.ensure` on output | 1 |
| `render/cancel.py` | `PROXIES` and `BAKES` scopes (latest-wins per key) | 1 |
| `render/cache_budget.py` | A proxies/bakes LRU class with its own byte cap | 1 |
| `main.py` `/preview` | Honour `priority=low`: nice the render subprocesses in client mode | 1 |
| `render/audio_mix.py` | Expose the duck curve and loudness gain for routes | 2 |
| `render/compositor.py` | `in_color_matrix=bt709:in_range=tv` on untagged swscale conversions (R12), behind an export golden | 2 |
| `ingest/normalize.py` | **No change.** Masters keep their GOP and pix_fmt (N5) | none |

### 9.5 New: tests

| File | Purpose |
|---|---|
| `tests/test_timebase_golden.py` | Generates `timebase_cases.json` |
| `tests/test_frame_plan_golden.py` | Generates `frame_plan_cases.json` (fuzz and hand cases) |
| `tests/test_frame_map_golden.py` | Renders bar-coded counters through the compositor → `frame_map_cases.json`; asserts `render/frame_map.py` = ffmpeg |
| `tests/test_audio_map_golden.py` | Click-track sources through `_audio_only_graph` → `audio_map_cases.json`, `audio_curve_cases.json` |
| `tests/test_proxy.py` | Frame count, `avcC` identity across sources, bar identity, on-demand span = eager span (Y-PSNR ≥ 50 dB), FLAC chunk sample-exactness, cancellation |
| `tests/test_dispatch_include_edl.py` | Response EDL is the stored EDL, hashes equal, undo and redo, concurrent dispatch ordering |
| `tests/test_bake.py` | Bake frame `k` bar = preview frame `k`; clip-keyed reuse (P2) |
| `tests/wk/harness.py` | Promoted `wk.py`: a WKWebView configured like pywebview 6.2.1, run on-screen at 4 px (hidden pages throttle timers), results posted to file |
| `tests/wk/pages/*.html` + `frontend/src/lib/preview/testkit/` | Engine test pages built from the frontend package (bar reader, audio tap worklet) |
| `tests/wk/test_wk_phase{1..5}.py` | The acceptance suites of §13 (marked `wk`, nightly and pre-release; skipped off macOS) |
| `frontend/e2e/preview/*.spec.ts` | The same pages under Playwright headless Chromium for fast CI (non-WK-specific assertions only) |

---

## 10. WKWebView compatibility (macOS 26 and 27)

| Capability | WK status | Used for | Fallback |
|---|---|---|---|
| `MediaSource` + `SourceBuffer` (`segments`) | present (probed) | laneA | `ManagedMediaSource` with `disableRemotePlayback = true`; else server mode |
| Overwrite re-enqueue while playing | yes, 0 stale frames 4 ahead | edits while playing | append from `+150 ms` regardless (Chromium-safe) |
| Exact-boundary seek repaint | **broken** (2 of 3) | none | always `(k+0.5)/R` |
| Buffered gap | stalls | none | filler samples; contiguous window invariant |
| Init-segment size switch | seamless | mixed-resolution sources | none needed; count re-inits in telemetry |
| SourceBuffer quota | ≈ 289 MB | window ≤ 60 MB | evict-behind and shrink look-ahead on `QuotaExceededError` |
| `requestVideoFrameCallback` | present | presented clock | rAF + `currentTime` (APPROX, telemetry) |
| `VideoDecoder` H.264 8-bit 4:2:0 | yes; High10, 4:2:2, 4:4:4 rejected | laneB (P3+) | proxies are always 8-bit 4:2:0; else BAKED |
| `AudioDecoder` | yes (AAC priming not trimmed) | **not used** | FLAC sidecars |
| `decodeAudioData` FLAC | yes (33 ms / 10 s) | audio chunks | WAV chunks (`?fmt=wav`) |
| `AudioContext` `getOutputTimestamp`, `outputLatency` | yes (15.6 ms) | A/V anchoring | `currentTime + outputLatency + baseLatency` |
| `setValueCurveAtTime`, `cancelAndHoldAtTime` | present | duck curve, automation edits | `cancelScheduledValues` + `setValueAtTime` |
| AudioWorklet | present (Safari ≥ 14.1) | limiter (P2), test tap | `DynamicsCompressorNode` (APPROX) |
| `MediaElementAudioSourceNode` | present, bugs 221334 and 215314 | **not used** | none |
| `OfflineAudioContext` | present | verification, tests | none |
| WebGL2, 3D textures, float linear | yes | compositor, LUT | server mode if no WebGL2 |
| `webglcontextlost` / restored | WebKit drops contexts under GPU pressure | none | 2D snapshot; rebuild; server mode after 2 s |
| `texImage2D(video / VideoFrame)` | ≤ 1 to 3 ms | textures | none |
| Worker `OffscreenCanvas` webgl2 + `VideoDecoder` | yes | P4 off-main-thread compositor | main thread (P1 to P3 default) |
| `captureStream` | **absent** | none | none |
| `SharedArrayBuffer` | **absent** (no COOP/COEP) | none | transferables only |
| Muted autoplay | allowed | laneA | none |
| AudioContext start | resumes without a gesture in WK (Chromium does not) | none | always `resume()` inside the handler |
| Concurrent media-element budget | small (repo evidence) | none | engine adds ≤ 2; ≤ 4 total after P4 |
| Hidden-window timer throttling | yes | none | pause engine on hidden or minimized |
| `fastSeek` | present | not needed (all-intra) | none |

---

## 11. Performance budgets

### 11.1 Edit-to-visible latency
Measured in the WK harness from the commit event (pointer-up or keydown) to the first presented canvas frame whose bar reads correctly. The p95 is over 50 edits on a 5-minute, 40-clip, 1080p-source timeline. Server times assume warm proxies unless noted.

| Edit | Paused p95 | Playing | Audio audible (playing) |
|---|---|---|---|
| Split | ≤ 60 ms | no visible change (correct by construction) | none (continuity) |
| Trim in/out, ripple, delete, move, insert/close gap, undo/redo of these | ≤ 80 ms | correct from `presentedK + 5` (≈ 150 ms) | ≤ 250 ms |
| Same, spans cold (not in span LRU; on disk) | ≤ 120 ms | correct ≤ 300 ms | ≤ 300 ms |
| Same, source spans not yet encoded (on-demand) | ≤ 700 ms; last good frame + spinner from 80 ms | buffering ≤ 700 ms | together with video |
| Clip gain, gain_env, mute/solo, channel mode, fades | n/a | n/a | ≤ 100 ms |
| Ducking (P1 APPROX → P2 exact) | n/a | trapezoid ≤ 100 ms; exact when the curve lands ≤ 1.5 s | as left |
| Opacity, transform, fit/cover, flip (P1) | ≤ 60 ms (next rAF after dispatch) | next frame | none |
| Slider drag of those, or of colour (P2; no dispatch) | ≤ 1 frame per rAF | ≤ 1 frame | none |
| eq/colorbalance (P2) | ≤ 60 ms | next frame | none |
| LUT change (P2) | ≤ 100 ms (33³ parse ≤ 50 ms, cached after) | ≤ 100 ms | none |
| Transition add/change/remove (P3) | ≤ 100 ms | correct from `+150 ms` | ≤ 250 ms (acrossfade) |
| Trim-drag edge frame (P2) | ≤ 50 ms per pointer move (coalesced to rAF) | n/a | none |
| BAKED feature change | RAW frame + spinner ≤ 80 ms; baked frame = server render time + ≤ 500 ms bake | same | as preview.mp4 today |

Engine overhead budget inside those numbers: dispatch round trip ≤ 40 ms, `setTimeline` ≤ 6 ms at 300 clips, first append plus seek ≤ 20 ms.

### 11.2 Per-frame (main thread, WK, M1-class Mac)
- rVFC handler (texture upload(s) + uniforms + draw): **≤ 4 ms p99** at 1080p canvas.
- `programMap.build`: ≤ 4 ms full rebuild at 21,600 frames (12 min at 30 fps); ≤ 1 ms incremental per edit.
- `fmp4Writer`: ≤ 1 ms per 30-frame fragment. Appends are async.
- Overlay layers unchanged.
- Dropped frames during steady 1080p30 playback with overlays: ≤ 0.1%.

### 11.3 Memory (WebContent process)

| Pool | Cap |
|---|---|
| SourceBuffer window (−10 s…+30 s, 720p all-intra ≈ 5 Mb/s) | ≈ 25 MB typical, 60 MB hard (soft target far below the 289 MB quota) |
| Span LRU (encoded ArrayBuffers) | 128 MB |
| Decoded audio LRU (f32, 5 s chunks ≈ 1.9 MB each) | 96 MB |
| laneB `VideoFrame`s | 24 frames (≈ 33 MB at 720p) |
| LUT textures | 16 MB |
| programMap + audio map | ≤ 2 MB at 12 min |
| **Engine total** | **≤ 350 MB**; process total ≤ 1 GB |

Soak: flat memory, slope < 1 MB/min over 30 min with an edit every 10 s. `laneB` leak counter = 0.

### 11.4 Server, disk and CPU
- Proxy encode: ≥ 15× realtime per source at 2 threads, niced; ≤ 50% of cores in aggregate; suspended during export.
- On-demand span (60 frames, 720p): ≤ 400 ms p95 including the master GOP walk.
- Disk: ≤ 40 MB per source-minute, in an LRU class in `cache_budget.py`.
- Loopback serving of spans: ≤ 5 ms p95 for a 1.3 MB span.

---

## 12. Phased delivery plan

Every phase ships behind `preview.engine = auto`. `auto` means client when capabilities pass and the timeline has no rate refusal; otherwise server. Each phase has a kill switch: set the default to `server`.

### Phase 1: instant structure (cuts, splits, trims, moves, deletes, ripple, gaps, undo)
Scope:
- dispatch with EDL;
- proxies and FLAC sidecars (eager and on-demand);
- `frame_map.py` and the golden tables (timebase, frame plan, frame map, audio map and curves);
- laneA MSE with fmp4Writer;
- WebGL2 compositor with geometry passes (fit/cover, rotate, crop-zoom, transform + keyframes, flip, opacity, video fades);
- constant speed, speed curves, reverse and freeze on picture, all EXACT;
- mixGraph with gain, gain_env, fades, mute/solo, channel mode (EXACT); varispeed audio, duck trapezoid, last-known loudness gain + DynamicsCompressor (APPROX);
- presented clock and overlays on it;
- fidelity classes; BAKED via bake spans of `preview.mp4` for colour, LUTs, effects, transitions, chroma, masks and custom expressions;
- PENDING and degraded tiers; server-mode fallback;
- preview render demoted to 1.5 s idle, niced, unless BAKED is needed;
- structural divergence check;
- WK harness in the repo.

Internal milestones (not separately shipped): 1a server contracts and goldens → 1b laneA + geometry on a static timeline → 1c edits + audio → 1d fallbacks, soak, flip default.

Ships when the Phase 1 acceptance suite passes on macOS 26 and 27. Result: every structural edit is instant; effect-bearing ranges behave exactly as today, but without blocking the rest of the timeline.

### Phase 2: instant sound and colour
Scope:
- `/duck_curve` per render_hash with `setValueCurveAtTime` (EXACT after it lands);
- `/preview_loudness` + AudioWorklet limiter;
- eq, colorbalance and lut3d shaders (EXACT within budget);
- the server BT.709 swscale fix (R12) behind the export colour golden;
- provisional maps for trim-drag edge preview and move ghosts;
- slider drags live without dispatch;
- clip-keyed bake reuse, so moving an effected clip needs no re-render;
- idle pixel sampler.

### Phase 3: instant transitions
Scope:
- laneB (WebCodecs over all-intra samples, 24-frame budget);
- xfade GLSL ports of all 58 native transitions with a tolerance table, each EXACT or APPROX by measured PSNR;
- `acrossfade` curves on seams;
- seam frame indices from R3;
- custom-expression transitions stay BAKED.

### Phase 4: effects, PiP, shuttle, off-main-thread
Scope:
- shader effects (blur, sharpen, vignette, glow, rgb_split EXACT or APPROX; grain, vintage and VHS APPROX); chroma key; masks as textures from `render_mask_png`;
- PiP on laneB: frame-exact, releasing the PiP `<video>` pool, so ≤ 4 media elements total;
- bgremove through an alpha-packed proxy variant;
- J/K/L shuttle including reverse;
- tempo sidecars for pitch-preserving speed audio;
- 1080p proxy variant rule;
- compositor and laneB moved to a worker `OffscreenCanvas` when the main-thread p99 exceeds 4 ms on the test machine.

### Phase 5: server preview becomes verifier only
Scope:
- `POST /preview` is not issued for timelines with no BAKED ranges, except the idle verification render (≥ 5 s idle, niced, cancelled on any edit);
- telemetry-driven auto-demotion of feature classes;
- the legacy CSS stand-ins for live transform and filter in `Preview.tsx` are removed from client mode;
- the server-mode path stays as the fallback, and is still tested.

---

## 13. Automated test plan

Principles:
- **Every acceptance assertion runs in the WK harness** (`tests/wk/`, macOS 26 and 27 runners, marker `wk`, nightly and pre-release).
- Playwright headless Chromium runs the same pages on every PR for logic regressions. Assertions known to differ in WebKit (overwrite staleness, autoplay, `outputLatency`, ManagedMediaSource) are tagged `wk-only` and skipped there.
- Frame identity is read from an **11-bit frame-number bar plus a 4-bit source-id bar** burned into test sources (C's `gen.sh`), by `readPixels` on the engine canvas.
- Audio identity comes from click-track sources: a unique-frequency click at each source frame boundary, plus a 1 kHz carrier for gain.
- Per the house rules, tests are written first for each module (RED → GREEN). Coverage of `lib/preview/` must be ≥ 80%.

### Phase 1
Units (vitest):
- `timebase.test.ts`, `framePlan.test.ts`, `frameMap.test.ts`: 100% of the golden fixtures.
- `programMap.test.ts`:
  - split → `dirtyFrames = ∅`;
  - trim/ripple/delete → the exact dirty range;
  - 300-clip rebuild ≤ 4 ms (bench, informational in CI);
  - equality with the `frame_map.py` RLE on the fuzz corpus (500 random op sequences from the `test_b8_fuzz_sweep` generator).
- `fmp4Writer.test.ts`: parse the output with mp4box; `tfdt` = `k·T` exactly for all 9 standard rates; sample flags all sync; `trun` sizes match.
- `laneA.test.ts` with a fake SourceBuffer:
  - contiguous window invariant;
  - filler over gaps;
  - init re-append on size-class change;
  - quota path;
  - append ordering (playhead first);
  - seek targets always `(k+0.5)/R`.
- `curves.test.ts`: afade/acrossfade shapes vs `audio_curve_cases.json` within 1e-5.
- `support.test.ts`: the classification table per phase.

Python (pytest): `test_timebase_golden`, `test_frame_plan_golden`, `test_frame_map_golden`, `test_audio_map_golden`, `test_proxy`, `test_dispatch_include_edl`, `test_bake`. Plus route containment tests (a key not referenced by the session → 404; path traversal).

Browser (Chromium PR + WK nightly):
- **P1-F1 paused exactness.** 200 random `k` over a fixture timeline covering:
  - 12 cuts across 3 sources of different sizes;
  - a 25 fps source in a 30 fps project;
  - 0.5×, 2× and curve speeds;
  - a reverse span, a freeze and a gap.

  After each `seek`, the canvas bar equals golden `(src, srcFrame)`, **200/200**.
- **P1-F2 playback exactness** (WK normative). A 20 s play, reading the bar on every rVFC: **0 mismatches**, missing frames ≤ 0.1%.
- **P1-F3 edit latency** (WK normative). A test backend session (a real FastAPI in a thread, fixture sources) runs 50 each of split, trim, move, delete, ripple, undo while paused. Time from dispatch start to correct bar: p95 within §11.1.
- **P1-F4 edit while playing.** An edit at `presentedK + 30`. No frame at or after `presentedK_at_edit + 5` shows old content (WK); no `waiting` stall.
- **P1-F5 last good frame.** The span route is delayed by 1 s. Canvas pixel hash is unchanged until the correct frame; the spinner is visible from 80 ms; recovery after the delay.
- **P1-A1 sample placement** (Chromium + WK, OfflineAudioContext). Offline render of the fixture timeline: each click at output sample `S(k)` ± 0; clip boundaries exactly at `samples_for_frames`.
- **P1-A2 mix parity.** Offline client render vs the server `_audio_only_graph` render of the same EDL: per-50 ms RMS within 0.25 dB (EXACT features), xcorr lag 0 ± 1 sample.
- **P1-A3 live A/V sync** (WK only). An AudioWorklet tap records the master output with `currentFrame`. White flash frames coincide with clicks at 20 cuts. Offset = `ctxAt(expectedDisplayTime(flash)) − ctxTime(click)`: **p95 ≤ 10 ms, max ≤ 20 ms**, and ≤ 1 frame within 100 ms of play start.
- **P1-S1 structural agreement.** For each of 200 edits, the client map equals `/frame_map` (0 mismatches).
- **P1-M1 soak** (WK, nightly). A 12-minute 1080p-source timeline looped for 30 min, an edit every 10 s. WebContent footprint (via `task_info` from the harness) slope < 1 MB/min; `buffered` span ≤ 45 s; audio LRU ≤ cap; no unhandled `QuotaExceededError`.
- **P1-E1 element budget.** Over a 100-edit script, the count of `HTMLMediaElement`s created by the engine is ≤ 2 at all times.
- **P1-R1 fallbacks.**
  - With `MediaSource` deleted, the engine is in server mode and `preview.mp4` plays.
  - Injecting 3 decode errors switches to server mode.
  - Forcing `webglcontextlost` shows the snapshot, then restores.
  - Proxy `failed` activates the degraded `<video>` tier: paused frames exact to the bar via the +1 ms bias.
- **P1-B1 bake splice.** A clip with `eq` is BAKED. After the render lands, the bars in that range equal the bake's (post-render), the rest stay client frames, and no `<video>` src swap happens.

### Phase 2
- Colour parity: eq, colorbalance and LUT (3 `.cube` files) on SMPTE bars and a skin-tone chart, compared with the ffmpeg filter output of the same chain on the same proxy frames: mean |Δ| ≤ 2 levels, max ≤ 6 outside chroma edges.
- Server R12 export golden: exports of untagged sources before and after the fix are compared, and the new result matches WK decode within ≤ 2 levels.
- Duck: after the curve lands, the client music gain vs the `STEM_DUCK_PROBE` stem (1 kHz carrier) is within 0.5 dB per 20 ms block. Before the curve, the mode is APPROX and the spinner shows.
- Limiter: the worklet vs `alimiter` on a loudness torture clip: peak ≤ −1 dBFS + 0.1, RMS within 0.5 dB.
- Provisional drag: trim-handle move → edge frame bar correct within 50 ms p95; nothing dispatched until pointer-up.
- Bake reuse: moving an eq'd clip issues no `/preview` render request before its frames are correct; bars equal the pre-move bake.
- Idle pixel sampler: an injected wrong `eq` formula demotes the class to BAKED within one idle cycle.

### Phase 3
- For each of the 58 transitions, at progress 0.1/0.5/0.9: Y-PSNR vs ffmpeg `xfade` of the same proxy frames meets the per-transition tolerance (≥ 35 dB EXACT, else APPROX).
- Seam timing: the first and last blended output frame indices equal `frame_plan_cases` (0 off-by-one).
- laneB leak counter = 0 after 1,000 seams in the soak; held frames never exceed 24.
- Playback through 30 seams (WK): laneB drops ≤ 1%, 0 bar mismatches on side A.
- acrossfade offline parity: RMS within 0.25 dB, lag 0.

### Phase 4
- Effect shader parity vs ffmpeg per effect, with the tolerance table.
- PiP frame-exact bar test (reusing the `test_b5_pip_frame_exact.py` scenarios, run in the browser).
- Media elements ≤ 4 in total.
- Reverse shuttle: 15 fps paused steps, correct bar per step.
- Tempo sidecar parity vs the export atempo within 0.25 dB RMS.
- Worker compositor: main-thread rVFC handler ≤ 1 ms p99 when enabled.

### Phase 5
- No `/preview` request is issued within 5 s of edits on BAKED-free timelines.
- Verification render is cancelled on the next edit.
- The server-mode fallback suite (today's `storePreview.test.ts` and friends) still passes.

---

## 14. Risks and mitigations

| # | Risk | Likelihood / impact | Mitigation |
|---|---|---|---|
| 1 | WebKit MSE behaviour changes between Safari/macOS releases (overwrite re-enqueue, init switching, boundary-seek bug) | M / H | WK acceptance suite on macOS 26, 27 and each new beta. Behaviour-independent design choices: append from `+150 ms`, mid-frame seeks, filler. Automatic server-mode fallback on error rates |
| 2 | TS frame mapping drifts from the compositor (fps=near tie rules, speed curves, reverse) | M / H | Golden tables rendered by real ffmpeg. `frame_map.py` reference. Per-edit structural equality with auto-BAKED on mismatch. Fixture regeneration required in any compositor PR (CI check) |
| 3 | A new import or AI output is not previewable until its proxy exists | H / M | On-demand span encoding (≤ 400 ms per 2 s span), eager background encode, FLAC first, PENDING tier with last good frame, degraded `<video>` tier on failure |
| 4 | Proxy disk and CPU cost (≈ 35 MB/min; encode contention with renders and export) | M / M | Niced 2-thread encodes, suspended during export. LRU class in `cache_budget.py`. On-demand rebuild of evicted spans. crf tunable per recipe version |
| 5 | 720p proxies look soft on zoomed or cropped clips vs export | M / L | 1080p variant rule (P4). Pixel check thresholds account for it. Export unaffected |
| 6 | A/V drift or play-start offset between MSE video and WebAudio | L / H | Same hardware clock (0.8 ppm measured). `getOutputTimestamp` anchor corrected at first rVFC. Drift monitor with re-anchor. WK tap test with hard thresholds |
| 7 | Memory blow-up in WebContent (SourceBuffer, audio, `VideoFrame` leaks) kills the process | M / H | Hard caps per pool (§11.3). Owned `close()` with a leak counter. 30-minute soak test. Quota handling. Pause on hidden |
| 8 | Ducking and loudness are signal-dependent and go stale after every v1/VO edit | H / M | APPROX trapezoid + spinner immediately. Server curve per render_hash (P2). Parity tests vs the `STEM_DUCK_PROBE` stem |
| 9 | Colour mismatch (BT.601 vs BT.709 on untagged sources; eq in YUV) | M / M | Proxies explicitly tagged BT.709. Shaders operate in BT.709 limited YUV. Server swscale fix behind an export golden (P2). Idle pixel sampler demotes failing classes |
| 10 | WebGL context loss under GPU pressure | M / M | 2D snapshot on pause. Rebuild on restore. Server mode after 2 s |
| 11 | Media-element budget exhaustion in WebKit | L / H | The engine adds ≤ 2 elements and replaces today's preview `<video>`. PiP moves to laneB in P4 (≤ 4 total). Element-count test |
| 12 | Dispatch-with-EDL payload size on huge projects (JSON EDL per edit) | L / M | EDL is already serialised under the lock. Gzip on loopback is unnecessary below 1 MB. If > 1 MB, fall back to `edl_hash` + GET. Measured in `test_dispatch_include_edl` |
| 13 | Wave C (still running) changes timebase, segments or the text model under this spec | M / M | Phase 1 starts after Wave C lands. The goldens are regenerated from the post-Wave-C tree, and the spec's module list is re-checked against it |
| 14 | Chromium CI gives false confidence | H / M | Acceptance only in WK. `wk-only` tags. WK runs block release |
| 15 | Main-thread jank from React re-renders on playhead writes | M / M | Engine and clock are outside React. Playhead writes stay rAF-throttled as today. Worker compositor (P4) if p99 exceeds 4 ms |
| 16 | Security of new file-serving routes | L / H | Hash keys only, containment to `workdir/proxies` and `previews`, session-reference check, rate cap. Reviewed with the `/files` hardening tests |

---

## 15. Open decisions (to settle during Phase 1a)
1. Proxy crf (23 vs 24) and 720p bitrate, once measured on the real workdir corpus (target ≤ 40 MB per source-minute).
2. Whether `render_preview` should render at 720 short edge in client mode so bakes match proxy sharpness (costs about 1.8× pixels per bake).
3. Whether ducking is ported to JS with a golden parity test (a deterministic dynaudnorm key is hard) or stays server-curve only. Default: server curve.
4. Telemetry sink: local log file under the app cache (no network), surfaced in Settings > Diagnostics.

---

## Appendix A: program map entry (logical)
```ts
type Mode = 0 /*EXACT*/ | 1 /*APPROX*/ | 2 /*BAKED*/ | 3 /*PENDING*/
interface ProgramMap {
  R: { num: number; den: number }; T: number            // ticks per frame at 240000
  total: number                                          // frames = frame_of(total_duration)
  kind: Uint8Array        // 0 clip, 1 gap
  clipIdx: Int32Array     // index into clips[] (−1 for gap)
  srcKey: Int32Array      // index into sources[] (proxy key or 'bake:<hash>')
  srcFrame: Int32Array    // R4 source frame
  bSrcKey: Int32Array; bSrcFrame: Int32Array; progress: Float32Array   // transitions (P3), PiP (P4)
  mode: Uint8Array
  clips: ClipParams[]     // geometry/colour/effects uniforms, params hash
  sources: SourceRef[]    // {key, w, h, initKey, spanFrames}
}
```

## Appendix B: dispatch response (client mode)
```json
{ "result": { "...": "unchanged" },
  "edl_hash": "…", "render_hash": "…",
  "edl": { "...": "full EDL, same JSON as GET /edl" } }
```
