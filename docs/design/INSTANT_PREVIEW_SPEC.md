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
| Two same-size x264 all-intra `stitchable=1` encodes have an identical `avcC` **at the same source rate** (x264 writes the rate into the SPS VUI: a 25 fps and a 30 fps proxy of one size differ; measured in 1a) | C probe; milestone 1 |
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
- One `MediaSource`, one `SourceBuffer('video/mp4; codecs="<codec>"')`, `mode = 'segments'`, `timestampOffset = 0`. The codec string is **read from the init segment** (`parseInitSegment` in `media/fmp4Writer.ts`; the real proxies measure `avc1.641029`), never hard-coded; with no codec known yet, laneA skips the append rather than guess.
- `laneA.currentTime` **is render-clock seconds**, so `k = round(mediaTime · R)` with `R = rate_of(canvas.fps)`.
- **The buffered range is always contiguous** from `playhead − 10 s` to `playhead + 30 s`, clamped to `[0, total)`. A gap stalls WebKit, so gaps in the *timeline* get **filler samples**: the previous appended sample's bytes (the first clip's frame when the timeline starts with a gap). The compositor draws black for them because `programMap[k].kind == gap`.
- **Init switching.** The writer tracks `lastInitKey` and appends an init segment before any fragment whose init class differs. The class is the **full `avcC`**, not (W×H, recipe): one size can carry two classes (the source rate is in the SPS VUI). **Its key is defined once, on both sides: the `avcC` bytes as lowercase hex** (`index.json` `init_key` from `ingest/proxy.py`, `parseInitSegment().initKey` in `media/fmp4Writer.ts`; the avcC's SPS pins the size, and x264 writes the source rate into the VUI, so a 25 fps and a 30 fps proxy of one size are two classes — measured in 1a). The WK real-proxy suite asserts the two are equal (review RD1).
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
  - laneA via `texImage2D(video)` on each rVFC. Frame dimensions come from the program map's source (the proxy `index.json` w×h), never from the element; rVFC `metadata.width/height` is only a cross-check (it lagged one frame after an init switch in 1/17 runs, milestone 1). A texture is uploaded only at a completed paused `'seeked'` or in rVFC while playing — never between an append that overwrote the paused frame and its re-seek (WebKit returns black there).
  - laneB via `texImage2D(VideoFrame)`.
  - LUTs as 3D textures.
- Pass order mirrors `_build_clip_video_chain` exactly:
  1. fit (contain: scale-down plus black pad; cover: scale-up plus crop)
  2. rotate in place with black corners
  3. crop-zoom and pan
  4. transform and keyframes (evaluated with `lib/overlay.ts` at the clip-local TIMELINE seconds of the OUTPUT frame, `(k − clipStart)/R` = `playhead − clip.start`: a keyframed clip's export chain runs its geometry after the retime and the grid `fps`; keyed scale/pan = the static rule per frame, +x right, zoom about the centre; as built, Wave D3 lane E1a, superseding RD2's retime of the source frame's time)
  5. colour (P2)
  6. effects (P4)
  7. opacity
  8. video fades (multiply toward black)
  9. transition blend (P3)
- Colour maths run in **BT.709 limited-range YUV** (§6 R12).
- **"Last good frame".** The engine draws only when every texture for `k` is ready; otherwise it leaves the canvas as it is. On pause, a snapshot is copied to a 2D backing canvas (≤ 2 ms) so `webglcontextlost` never shows black. On `webglcontextrestored` the programs and textures are rebuilt. If restore takes longer than 2 s, the engine falls back to server mode (§7). As built (review RD2): the snapshot is shown only while it holds `presentedK`. A loss **while playing** is an external pause (`cause: 'context'`): picture and sound stop together at `presentedK`, the snapshot (the last PAUSE's frame) is replaced by black with the spinner, and on restore both resume from a fresh anchor if the user's intent was play.

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
- **As built (1c, D2-integration; measured in WKWebView, macOS 27):**
  - With a MediaSource-fed `<video>`, rVFC `expectedDisplayTime`/`presentationTime` are on another clock (≈ −70.3e6 ms against a callback `now` of ~700 ms); a file-backed `<video>` reports them sanely. The engine moves them onto `performance.now()` by the smallest `now − expectedDisplayTime` seen since the last play (`clock/presentedClock.DisplayTimeBase`; the two clocks differ by a constant, and a callback never runs before its frame).
  - `getOutputTimestamp()` returns the RENDERED time (`contextTime = currentTime − 1 quantum`, `performanceTime = now`), without `outputLatency` (15.6 ms). `AudioEngine.ctxTimeAt` subtracts it once it has seen such a timestamp (sticky per context).
  - The anchor is checked at the first presented frame and again at the 8th (display-time estimate settled), both at 4 ms; the drift monitor re-anchors above **8 ms** (not 20: under load the output clock lost up to 14 ms over 15 s).
  - A pause WebKit makes (page hidden, window occluded; it pauses the muted `<video>` itself) or an interrupted AudioContext is an external pause: picture and sound stop together at the presented k. On return, if the user's last intent was play, both resume from a fresh anchor after the page has stayed visible 250 ms (an occluded window flips hidden/visible every ~10 ms for a while) **and** the context clock is seen advancing (`AudioSink.whenRunning`: after a hidden page WebKit reports `running` up to ~1.8 s before it renders); otherwise it stays paused.

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
- A structural edit while playing stops the affected sources with a 5 ms ramp and reschedules them from `presentedK` plus a lead of 150 ms rounded up to whole frames (`clock/editLead.ts`; as built, wave D3 E4: six frames were 250 ms at 24 fps, the whole §11.1 budget before the round trip, and 100 ms at 60 fps).
- A parameter-only edit (gain, fade, mute) rewrites automation with `cancelAndHoldAtTime` plus new ramps. It is audible at the next render quantum, well under 100 ms.
- Speed ≠ 1: `playbackRate = speed` (varispeed), marked APPROX where export uses pitch-preserving `atempo`. Phase 4 adds server tempo sidecars. A speed CURVE's sound is APPROX on the client too: the export reads a cached numpy intermediate (`render/speed_audio.py`, varispeed or WSOLA; lane S1), and a FREEZE is digital silence.

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
  *As built (final sweep 3):* `index.json` and the `/proxy` summary carry `has_video`. An audio-only source (a music bed, a voice-over file) has `frames` 0 for good; `PreviewController` waited for `frames > 0` and never gave it a proxy key, so its sound read as silence and the range stayed EXACT (the bed was inaudible in Instant preview, and the loudness gain measured with it was played on a mix without it). It now takes `has_video: false` as a probed source. A non-v1 media clip whose source has no proxy key yet is APPROX `audio:pending` ("Sound loading") over its window, not EXACT silence. The same run found a live-scheduling fault the bed made audible: a block's gain curve written after the context had passed its start was moved by Chromium onto the next block's first event, which threw after the block's source had started and before the clip recorded it, so each refill scheduled the block again (a faded-in bed up to +6.7 dB for 1-2 s). `MixGraph` records the block first and drops a late curve's played samples (`setLiveCurve`; `mixGraphLateCurve.test.ts`, `tests/test_instant_preview_audio_only_ui.py`, Playwright Chromium and WebKit: bed and v1 within 0.5 dB of the server preview). *As built (final sweep 3, run 3):* "the context has passed" means the RENDER position, which Chromium runs a callback buffer past `currentTime` (measured 256 frames): a curve trimmed to `currentTime` + one quantum still started in the past, was moved there with its duration kept, and the next block's automation was refused, freezing a fade's gain for a whole 1 s block (−5.9 dB). The margin is now `max(quantum, baseLatency)` + a quantum, and a refused block appended after everything its clip holds truncates the overrun (`cancelAndHoldAtTime` at its start) and is written once more. Play pressed while the sound under the start was not in memory (a fresh import's FLAC chunks still loading) used to run picture and playhead over up to 1 s of silence that read EXACT: `play()` now asks `AudioSink.soundHold` and, while the chunks of the start's block (and the one 0.3 s on) load, shows buffering with neither picture nor sound running, at most 700 ms (§11.1, `engineSoundHold.ts`); past that both run, and every stretch the sink could not play, or cannot yet, is APPROX `audio:pending` (`AudioSink.soundLoadingFrames`, `MixGraph.soundGaps`) until the sound joins. A seek while playing restarts the sound without the hold (labelled the same way). Real WKWebView with the chunks held back 400 ms: buffering 0.4 s, then within 0.01 dB of the server preview; held 1.5 s: buffering 0.7 s, then "≈ Sound loading" over the silent 0.8 s.
- `init.mp4`: `ftyp` + `moov` with `avcC`.
- `v/NNNN.bin`: a span sample pack. Header `u32 first, u32 count, u32 sizes[count]`, then AVCC length-prefixed samples.
- `a/NNNN.flac`: 48 kHz stereo, exactly 240000 samples per chunk (the last may be short).

**Video recipe (normative):**
```
ffmpeg -nostdin -threads 2 [-noaccurate_seek -seek_timestamp 1 -ss <keyframe pts ≤ span start> for on-demand]
  -copyts -i <master> -map 0:v:0
  -vf "<exact frame select for the span>,scale=<short edge 720 of the DISPLAYED size, even, bicubic>
       [:in_range=<pc|tv>:out_range=tv:in_color_matrix=<bt601|bt2020|…>:out_color_matrix=bt709
        — only for a source tagged full range or a non-709 matrix],setsar=1,format=yuv420p,
       setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv"
  -fps_mode passthrough
  -c:v libx264 -preset veryfast -crf 24 -profile:v high -level:v 4.1
  -x264-params keyint=1:min-keyint=1:scenecut=0:bframes=0:stitchable=1
  -colorspace bt709 -color_primaries bt709 -color_trc bt709 -color_range tv
  -f mp4 -movflags +empty_moov+default_base_moof+frag_keyframe  <tmp>  →  split into init.mp4 + span packs
```
- **Seeks are absolute** (`-seek_timestamp 1`): the master pts table is absolute, and without it ffmpeg adds the container's `start_time` to `-ss`, so every on-demand span of a source that does not start at 0 (a camera MTS, `output_ts_offset`) came out empty and failed the proxy (review RD1, measured).
- **Colour is converted, not relabelled** (R12): a full-range (`yuvj*`/`pc`) or BT.601/BT.2020-tagged master keeps its own levels through export, so its proxy converts to BT.709 limited in the scale step (0/255 → 16/235; BT.601 red Y81 U90 V240 → Y62 U102 V238, BT.709 red within 2 levels). Untagged and BT.709-limited sources get the plain scale, pixel for pixel. Anamorphic sources are sized to their displayed width (the probe reads ffprobe's `4:3` SAR; `render/sar.display_width`, the nearest even width) and every proxy is square-pixel (`setsar=1`, one SAR for every same-size SPS); the export fits the same displayed shape (as built, Wave D3 lane E1a). `RECIPE_VERSION` 2.
- **Frame identity.** Proxy frame `i` is master frame `i` in presentation order **after** the edit list: ffmpeg applies elst on decode, so the 2-frame elst and AAC priming traps cannot arise.
- **Asserted.** The proxy frame count equals the master's decoded frame count, which is recorded once at ingest. On-demand spans select by the master pts table: `trim=start_pts:end_pts` in the stream timebase, then `setpts` rebased, then asserting that `count` and first pts match. A mismatch sets proxy state `failed`, and the source falls to the degraded tier (§7).
- **Short edge.** 720 by default. Phase 4 adds a 1080 variant on demand for clips with `transform.scale > 1.5`, crop-zoom > 1.5, or a canvas display short edge > 900 device px.
- **Scheduling.**
  - Eager background encode after normalize and after every AI output lands in `cache/`, with priority by timeline proximity.
  - Niced (`nice 10`), at most 2 concurrent encodes, paused while an export runs.
  - Cancellable through a new `cancel.PROXIES` scope (latest-wins per source).
  - On-demand span requests jump the queue.
- **Audio sidecar.** Decode the master's audio once (`aresample=48000, aformat=fltp:stereo`) to f32. Python slices it into exact 240000-sample chunks and encodes FLAC. Silent-track masters produce a `silent: true` flag and no chunks. **Headroom:** 24-bit FLAC would hard-clip samples above full scale, which the export's float mix keeps; a chunk whose decoded peak exceeds 1.0 is stored divided by the smallest power of two that brings it under (exact in float), index.json `audio.chunk_gain` maps `"n"` → that linear factor (sparse; absent = 1), `audio.gain_db` is the largest in dB, and the chunk route repeats it as `X-Audio-Gain`. The client multiplies the decoded chunk back by it (review RD1).
- **Disk.** 720p all-intra at crf 24 comes to about 4 to 5 Mb/s: 35.9 MB of video per source-minute measured over the 82 workdir masters (crf 23 measured 40.0, exactly on the target; see §15.1). Proxies are registered with `render/cache_budget.py` in their own LRU class. Evicted spans are rebuilt on demand.

### 5.2 Routes (`api/preview_routes.py`, NEW; mounted from `main.py`)

| Route | Returns | Notes |
|---|---|---|
| `POST /api/sessions/{sid}/dispatch?include=edl` | adds `edl`, `edl_hash`, `render_hash` | CHANGED. Default response unchanged for old clients |
| `GET /api/sessions/{sid}/media` | rows gain `fps {num,den}`, `frames`, `pix_fmt`, `has_audio`, `proxy {key, state, w, h}` | CHANGED |
| `GET /api/proxies/{key}/index.json` · `init.mp4` · `v/{n}.bin` · `a/{n}.flac` | proxy data | `v/` and `a/` encode on demand, blocking ≤ 2 s, else `202` + `Retry-After: 0.2`; `a/{n}` answers as soon as chunk `n` itself is on disk (not the whole source's audio) and carries `X-Audio-Gain`. Keys must be referenced by a live session (containment check as in `/files`). GET/HEAD of `init.mp4`, `v/*.bin` and `a/*.flac` are exempt from the per-path 60 rps bucket, with a global cap of 400 rps; `index.json` and every other method keep the per-path bucket. No preview route serves a session's `cache/` (§14 risk 16) |
| `GET /api/sessions/{sid}/frame_map?h=<render_hash>` | reference program map, RLE (§8), plus `sources` {src: SourceInfo} it was built from | cached per (hash, source file identities). As built (D2): `409 stale_render_hash` + the current hash when `h` is not current; `202` + Retry-After while an edit holds the session or a source is probing; `422 source_unavailable` when a source cannot be probed |
| `GET /api/sessions/{sid}/bake/{render_hash}/init.mp4` · `v/{n}.bin` · `index.json[?ranges=k0-k1,…]` | bake spans (§5.3) | 404 until `preview.mp4` for that hash exists. As built (D2): `index.json` names the bake's `init_key` class and queues only the spans the BAKED `ranges` touch; media GETs share the preview-media bucket |
| `PUT /api/settings/preview` | `{engine: auto\|client\|server}` → the GET body | As built (D2): JSON only, loopback and same origin; stored in settings.json under the settings lock; `VAI_PREVIEW_ENGINE` still wins |
| `GET /api/sessions/{sid}/duck_curve?h=` (P2) | `{lanes: {trackId: {rate_hz: 100, db: float32 base64}}}` | from the `duck_probe` stem |
| `GET /api/sessions/{sid}/preview_loudness?h=` (P2) | `{gain_db}` | As built (Final QA r3): `{gain_db, current, target_lufs}` for the CURRENT hash (409 `stale_render_hash` otherwise, 202 while an edit holds the session). `gain_db` is the master gain the server preview of this SOUND was rendered with (`render/preview_loudness.audio_key`: canvas + sound-bearing tracks, overlay clips reduced as `lib/previewFingerprint.ts` reduces them, so a title keeps it current), `current` true; else the session's last-known gain (null before any measurement), `current` false. No target: `gain_db` null, `current` true. `PreviewController` asks with every hashed timeline and when a preview of the current hash lands; the AudioEngine plays it as the master loudness gain (the route did not exist: the Instant preview played the raw mix, ~11 dB under the export, marked EXACT). K2 (0.8.0 QA): a gain not measured yet for this sound is NOT a chip ("≈ Loudness" sat on every fresh project with the beta on, and hid nothing a person could act on): it is telemetry (`ControllerView.loudness` `'pending'`/`'measured'`, `PreviewController.loudnessLog` with the wait and the played-vs-measured difference), and it makes the background server render that measures it urgent (`renderUrgent()`: asked 250 ms after the edit, not at the 1.5 s idle cadence). Frames are APPROX `audio:loudness` only when the gain the sound plays is more than 1 dB (`LOUDNESS_AUDIBLE_DB`) off the MEASURED one (`AudioSink.loudnessOffDb`). Measured live (`tests/test_instant_preview_loudness_ui.py`, Playwright Chromium and WebKit): within 0.06 dB of the server preview. |
| `GET /api/sessions/{sid}/luts/{name}.cube` (P2) | LUT file | contained to the LUT dirs |

### 5.3 Bakes (`render/bake.py`, NEW)
- **Phase 1.** A bake of `render_hash h` is simply a proxy of `previews/h.mp4`, using the same recipe at the preview's own size. As built (D2): its key is `sha256(realpath, size, recipe)` — not mtime, because the render cache touches a reused preview — it lives in `WORKDIR/proxies/` in the proxies LRU class (orphaned when the preview is evicted), and a session's newer hash cancels the older bake's span jobs. Bake frame `k` is output frame `k`, because `render_preview` renders the render clock from 0 at project fps. The preview **already excludes text, stickers and PiP** (see `Preview.tsx:456-481`, pixel-ownership rule), so the client overlays never double-draw. The client maps BAKED ranges to `{src: 'bake:'+h, srcFrame: k}`.
- **Phase 2: clip-keyed reuse.** A bake frame is also indexed as `(clipChainHash, localFrame)`, where `clipChainHash` = the hash of every field in `_build_clip_video_chain` plus `src`, `in` and `speed`. After a move, the client asks `bake/{oldHash}` for the frames of an unchanged clip at their old `k`, so no new render is needed. Transition overlaps are excluded from reuse.

---

## 6. Frame-accuracy and A/V-sync rules (normative, tied to `edl/timebase.py`)

- **R1. Output grid.** `R = rate_of(canvas.fps)` as a rational. Output frame `k` spans `[k, k+1)/R`. MSE ticks: `tick(k) = k · T`, `T = 240000·R.den/R.num` (an integer for all `STANDARD_RATES`). If `rate_of` yields a non-standard rate and `T` is not an integer, the engine refuses and uses server mode.
- **R2. One timebase in two languages.** `timeline/timebase.ts` ports `rate_of`, `fps_float`, `frame_duration`, `frame_of` (exact: round half to even on the exact rational `t·R`; the TS port takes a float fast path only when the product is more than 1e-6 from a half-frame tie), `time_of`, `quantize`, `frames_between`, `source_rate`, `floor_to_frame`, `ceil_to_frame`, `samples_for_frames` and `seek_preroll`, rational-exact with integer `{num, den}` and no float accumulation. `frameStep.ts` re-exports from it, `overlayGate.ts` uses it, and the frontend keeps exactly one copy. Golden: `tests/test_timebase_golden.py` writes `tests/goldens/timebase_cases.json` over `t ∈` a grid including exact multiples ± 1e-7, exact half-frame ties and their ±4-ulp neighbours at the NTSC rates (review RD2), times all `STANDARD_RATES` plus off-standard inputs. Vitest asserts 100%.
- **R3. V1 frame plan.** `framePlan.ts` ports `_v1_frame_plan` (`compositor.py:478`): a clip starts at `frame_of(start)`, occupies `clip_frames(c) = max(1, frame_of(effective_duration))`, gaps are whole frames, overlaps pack with `max(cursor, start)`, and the tail gap runs to `frame_of(total_duration)`. Seams come from a port of `seam_table_for(clips, transitions, fps)` (`edl/schema.py:909`) **with the whole-frame rounding**. `timelineLayout.seamTable` gains an `fps` argument so the timeline UI and the engine share it. Golden: `frame_plan_cases.json`, generated from the fuzz corpus of `test_b8_fuzz_sweep.py` plus hand cases (overlaps, sub-frame gaps, transitions longer than a side).
- **R4. Output frame to source frame.** For clip-local output frame `j ∈ [0, clip_frames)`, the source frame is what the export chain picks:
  - `clip_input_args` seeks to `in − seek_preroll(in)`;
  - then `setpts=PTS-STARTPTS`, `setpts=PTS/speed` (or the speed-curve integral);
  - then `fps=R` (round=near);
  - then `tpad=stop_mode=clone`, `trim=end_frame=clip_frames`;
  - with reverse and freeze per `render/reverse.py` and the freeze rules.

  As built (Wave D lane S1): a speed curve `{"curve": [[x, r], …]}` has `x` over the clip's OUTPUT (0..1) and piecewise-linear speed `r` (0.1–10x); its footprint is `S / mean(r)` and its `setpts` is the closed-form root `t_i + 2q/(r_i + sqrt(r_i² + k_i q))` with every constant printed in `repr`, so `edl/speed_curve.py`, `frame_map.py` and `timeline/speedCurve.ts` reproduce ffmpeg's ticks bit for bit (sqrt is correctly rounded in C, Python and JS). A freeze (`Clip.freeze` = hold seconds) holds the first frame a 1x chain opened at `in` shows, cloned on the grid; its sound is silence. Curve sound is a cached numpy intermediate (`render/speed_audio.py`: varispeed or WSOLA) read with `amovie`: APPROX on the client. Goldens: `tests/goldens/frame_map/speed.json`.

  The **normative definition is the golden table**, not this prose. `tests/test_frame_map_golden.py` renders bar-coded counter sources through the real compositor (`render_preview` on one-clip EDLs) across:
  - source rates 23.976/25/29.97/30/50/59.94/60, each in 24/25/29.97/30/60 projects;
  - speeds 0.25/0.5/1/1.5/2/4, two speed curves, reverse, freeze;
  - in-points on-grid, ±0.3 frame and ±0.5 frame.

  It decodes the bar per output frame and writes `frame_map_cases.json`. `render/frame_map.py` (the Python reference) and `frameMap.ts` must both reproduce it 100%.
- **R5. Source index space.** Index `i` means the master's i-th frame in presentation order after the edit list, which is the proxy's i-th sample. Asserted by equal counts (§5.1).
- **R6. Non-v1 lanes.** Text, stickers, PiP and captions keep their placement through `renderSpanOf`, `renderWindow`, `layoutTime` and `enable_window`, evaluated at `clock.now() = presentedK/R`.
- **R7. Seeks.** laneA paused seeks go to `(k + 0.5)/R`. The degraded `<video>` tier seeks to `pts(src frame) + PAUSED_SEEK_BIAS_S` (1 ms) and confirms with rVFC `mediaTime`, re-seeking once on mismatch. Never seek to an exact boundary.
- **R8. Presented frame.** `presentedK = round(mediaTime·R)`, taken from rVFC only. `currentTime` is never used to pick a frame in client mode.
- **R9. Audio placement.** Output frame `k` begins at output sample `S(k) = samples_for_frames(k, R)` (48 kHz). A clip whose plan starts at `k0` and spans `n` frames occupies output samples `[S(k0), S(k0+n))`: its length is the running-sum difference `S(k0+n) − S(k0)`, **not** `samples_for_frames(n)` (they differ by a sample at NTSC rates; milestone 1). That matches the server's `atrim=end_sample=M`. The source sample offset for the clip start mirrors the export's audio seek (`clip_input_args` preroll, `audio_mix.input_seek`, then `atrim`), pinned by `audio_map_cases.json`, rendered from click-track sources through `_audio_only_graph`.
  *As built (wave E gate, X1): ONE start rule.* A clip's sound starts on source sample `S(in) = timebase.edit_sample(in)` (TS `editSample`): the **nearest** sample to `in` as ffmpeg reads it (`%.6f` µs, then rounded to 1/48000). It is the output side's own rule (frame `k` starts on the nearest sample, `samples_for_frames(k)`), so a source frame's first sample (`samples_for_frames(f, Rs)`, where a click-per-frame source puts its click) is the clip's first sample when `in` is on the source grid. Every chain cuts to it **in samples** — the input opens on `S(seek)` and `atrim=start_sample=S(in) − S(seek)` follows (`compositor.clip_head_samples` for v1 and PiP, the lanes' `input_seek(in)` and their primed cut `S(in) − S(in − prime)`, `reverse._render_segment`, `speed_audio._decode`) — and `frame_map._clip_sample0`, `programMap.clipSample0` and the lanes' `inputSeekSample` model it. It replaced a **two-roundings** rule (`S(in − pre) + round(pre · 48 kHz)` for a half-frame pre-roll cut by `atrim=start=` in seconds): one sample late in 10% of cuts at 29.97 and 20% at 59.94 (up to 0.91 sample from `in`, against ≤ 0.512 for the nearest), dropping a source-grid cut's first-frame click. Review RE's in-anchored seek had moved the v1 chain alone to `S(in)`, which is the gate's P1-A1 off-by-one (client 152633, server 152632). Measured by `tests/test_audio_start_rule.py`: click-per-frame and sample-counter sources at 30 and 29.97, cuts on and off the grid, all nine rates, through `_audio_only_graph`, the PiP fold, the four lanes, the curve intermediate and real export / single-pass / chunked preview renders (ALAC for AAC). *Open (length):* the assembly and both models give each segment `samples_for_frames(its frames)`, so `out0` is the per-segment sum, not `S(k0)` — they agree with each other sample for sample, but at NTSC rates `out0` drifts from `S(k0)` by up to half a sample per preceding cut (a 24-frame clip at 59.94 is 19219 samples for 19219.2).
  *As built (wave E gate, RX): the proxy's sound is on the FILE clock.* `proxy.AUDIO_FILTER` decodes a master's sound through `aresample=async=1:first_pts=0` at its own rate before the 48 kHz stereo conversion — the head of every render chain — so an audio stream that starts after the file (audio `start_time` > format `start_time`: camera and screen-recorder MOV/MP4) is led by silence and proxy sample `S(t)` is file time `t`, which the client indexes by `edit_sample(in)`. It was plain `aresample=48000` (index 0 = the stream's first sample): a source with audio 0.1 s late played 4800 samples off the server on every sample of every clip (`RECIPE_VERSION` 4; `tests/test_proxy_audio_clock.py`, P1-A1 `placement_offset` in `tests/wk/test_wk_audio.py`, WK/Chromium/PW-WebKit). The export's picture cap is a `trim=end_frame` inside the graph, not `-frames:v`, which closed the file before the AAC flush and cut ~10 ms of sound off NTSC renders (`tests/test_render_audio_tail.py`).
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

**The master limiter (wave E gate RX, as built).** The client's limiter is a `DynamicsCompressorNode`, not the export's `alimiter`: both are transparent below the ceiling (the offline mix matches the server within 1e-4), above it their attack and release differ (|Δ| up to 0.25, 0.64 dB per 50 ms block over twice the ceiling). `audio/limiting.ts` bounds each stretch's PRE-limiter peak — master gain × Σ bus gain × clip gain × envelope maximum × the source chunks' recorded peak (`index.json` `audio.chunk_peak`, ffmpeg's float decode rounded up; unknown until the layout lands — a layout read while the proxy's sound was still being built is re-read until it is, K2; a voice-effect clip is bounded by `voiceFx.voicePeakBound` of its source peak, priming included: echo taps, each biquad's impulse-response L1 norm, the drive's tanh ceiling, the ring's depth, the Hall IR's L1 norm — K2, 0.8.0 QA: it was unbounded, and every voice effect on a project with a loudness target read "≈ Limiter on loud sound", an EXACT echo included) — and `AudioPlan.limiting` names the output ranges whose bound tops the ceiling, widened by 100 ms (the limiters' look-ahead and release); `support.ts` classes them APPROX `audio:limiting` ("Limiter on loud sound"), the engine reclassifying when a late layout narrows them. The bound is the triangle inequality, so it flags more than the limiter actually touches (a talker over a bed whose peaks never coincide still reads ≈); it never flags less (`tests/wk/test_wk_audio.py` `mix_hot`: every sample where client and server part lies inside a range). A sample-exact clip without a voice effect is bounded one second of source at a time (`clipPieces`, `PEAK_GRAIN`, and the Σ is a sweep over the pieces; final sweep 2: bounded once by its whole source span, one hot 5 s chunk put a 200 s single-track clip under the flag end to end); a resample (`rate`/`curve`) or a voice effect (its tails carry a hot chunk forward) keeps the whole-clip bound. A port of alimiter's envelope would make the range EXACT and retire the flag.

**The master limiter, P2 (0.8.0 final QA, P2 limiter tail, as built).** The port exists: `audio/alimiter.ts` (`ALimiterCore`) is `af_alimiter.c` (FFmpeg 8.1, `asc=0`) line for line in doubles — bit-exact to ffmpeg on `tests/goldens/alimiter_cases.json` (`tests/gen_alimiter_goldens.py`, both preview limiters, a hot step, click bursts, a one-sided skew; `alimiter.test.ts`) — and `audio/limiterWorklet.ts` runs it as an AudioWorklet built from the class's own source text (a Blob URL module, one per context; the limit is a k-rate param snapped back to the server's 6 decimals), its 239-frame ring padded to `LIMITER_LATENCY` (288) so scheduling is unchanged. `MixGraph` builds its limiter stages on it whenever `limiterWorkletReady(ctx)` (`AudioEngine` registers it when it makes its context and moves a graph built before that onto it, `upgradeLimiter`; `renderOffline` registers it per offline context), with unity output gain and no warm-up. A plan made for it (`AudioPlanOptions.exactLimiter`) has no `limiting` ranges, so no "Limiter on loud sound". Measured against the server's render in Chromium, Playwright WebKit and WKWebView (`test_wk_audio.py`: `mix_hot`, `mix_hot_loud`, `mix_hot_release`, offline and live): max |Δ| 2.4e-7, except one lone sample where a release ENDS (the gain's last linear step crosses 1 one sample apart, ≤ 1/2400 of full scale; 2.0e-4 measured). Where AudioWorklet is missing or refuses the module the DynamicsCompressorNode stays, and so do the ranges above; their 100 ms spread was measured to cover its release (≤ 90.6 ms to agree within 1e-4 after the last sample over the ceiling, 0.5-30 dB over, Chromium and WebKit), and the parity test now judges EXACT blocks by the plan's ranges alone (it had its own "server near full scale" rule, which called the fallback's release at `mix_hot` 3.20 s EXACT: 0.76 dB).

**Voice effects (wave E, F3, as built).** CapCut's voice changer (`AudioProps.voice_effect` + `voice_intensity`, the ONE table `edl/voice_effects.py`) is classed per preset by `support.ts` `VOICE_FX_PARITY`, from measurement (`tests/wk/test_wk_voice_fx.py`: the export's sound against the client's offline mix of the same timeline, a synthesized voice on v1, an overlay, the voice-over and the music lane, Playwright Chromium and WebKit identical; WK pending while the owner's screen is locked):
- **EXACT** — Robot, Echo, Telephone, Megaphone, Radio, Underwater, Vibrato. The client (`lib/voice/voiceFx.ts`, run on each scheduled block by `mixGraph`) repeats the export's own arithmetic: af_biquads' RBJ filters with their `m=` blend, aecho's feed-forward taps, the `tanh` drive and the ring modulator of `aeval`, af_vibrato's 5 ms line and wave table — each block read with its look-back (the echo's 750 ms, 4096 samples of filter warm-up), so a seek mid-echo is exact too. Measured: max |Δ| 1.6e-5 (ffmpeg's float32 biquads against double), per-50 ms level ≤ 0.0002 dB, lag 0; the golden `tests/goldens/voice_fx_cases.json` holds every stage to 2e-5.
- **APPROX** — Hall: the export's own impulse response (a closed form both `aevalsrc` and `reverbIr` evaluate) in a `ConvolverNode`, exact from a cold start (1.9e-7) but restarting its 2 s tail at a seek or a structural edit. Chipmunk, Deep, Monster: the export pitch-shifts with `asetrate` + `aresample` + `atempo` (no rubberband in the render binary: pitch within 1 cent, transients within ±9 ms measured, the keep-pitch bound ±20 ms); the client uses a period-aligned granular shifter (2048-sample Hann grains, each read offset by up to half a local period so overlaps stay in phase). Measured against the export: spectral centroid 6.1-8.0 % mean error (21 % worst block), level 1.3-1.7 dB mean (3.7 dB worst), 0.7-1.3 ms late.
The effect sits after a clip's retime and channel mode and before its gain, automation and fades, on both sides; an effect or intensity edit reschedules the lane (it is part of the clip's `timing`).

**Clip animations (wave E, F1, as built).** CapCut's In / Out / Combo on a v1 clip, an overlay (PiP) clip or a sticker (`anim_in` / `anim_out` / `anim_combo` / `anim_dur` / `anim_out_dur`, the ONE table `edl/clip_animations.py`, served by `GET /api/animations/presets` and dumped to `lib/anim/clipAnimTable.json`). An animation composes ON TOP of the keyed pose on the keyframe clock (clip-local timeline seconds at the output frame's own time k/R): scale ×, x/y + (a share of the canvas), rotation +, the opacity ramps as `fade`, and Blur as a gaussian copy mixed in. On v1 an animated clip is a `kf_clip` (geometry after the grid); `geometry.ts` runs the same keyed-transform branch with the animation's terms (`kfPanFrame` widened by its peak zoom and travel). `support.ts` classes it from measurement (`tests/wk/test_clip_anim_parity.py`: engine canvas vs the decoded export, every preset at 3 frames of its window, real proxies; Playwright Chromium, Playwright WebKit and the real WKWebView agree to 0.1 dB):
- **EXACT** (≥ 35 dB, lowest 35.67) — Fade, Zoom In (In), Zoom In / Zoom Out (Out), the four Slides, Rotate, Bounce and every Combo, also on top of keys, static poses and cover fit.
- **APPROX** (`ANIM_APPROX`, over the animation's window only) — Zoom Out In 34.8-34.9 dB (its ×1.5 start: bicubic up-scale in the export, bilinear on the GPU), Spin In 34.0-34.2 and Spin Out 30.3 dB (the export turns the picture, then re-scales it: two resamplings against the engine's one).
- **BAKED** — Blur In / Blur Out, over the blur's own window (26.3 dB drawn without the blur).
Overlays and stickers are drawn by `pipDraw` / `StickerLayer` in both modes (`lib/anim/animDraw.ts`: travel, turn, zoom, alpha, and the blur mixed linearly in premultiplied space — canvas `filter: blur()` where the engine has it, a down-and-up resample otherwise), at the output frame's own time; `tests/test_clip_anim_ui_e2e.py` measures a sticker's Slide and an overlay's Zoom In on the preview canvas against the plan in Chromium and WebKit.

**Mirror / Flip and the D3 leftovers (wave E, F4a, as built).** `Transform.flip_h` / `flip_v` (CapCut Mirror / Flip; omitted from the JSON while off) mirror a clip's PICTURE before its rotation, scale and position: on v1 the fitted canvas-sized frame gets `hflip`/`vflip` just before the rotate (a cover-pan window is taken at +x so the mirrored picture still moves right), on an overlay the framed element before its key/shape/rotate (`pip.py`), on a sticker its PNG before the rotation (`text_overlay.py`); no filter at all when off, so every other chain is byte-identical. **EXACT** (`support.ts` `TRANSFORM_FLIP_MODE`): `geometry.ts` `flipStage` mirrors F1 about the frame centre, and the nine `tflip_*` geometry goldens (real renders: rotate, cover pan, scale + pan, keyframes, anamorphic, with the hflip effect) hold to 1 px while each would miss by > 20 px unmirrored (the cover-pan case, whose only visible marker sits on the axis, by 60 px with the pan's sign unflipped; `transformFlip.test.ts`); pipDraw/StickerLayer mirror inside the element's turn (`flipScale`), `tests/test_transform_flip_render.py` measures the export. A reversed SPEED-CURVE clip's intermediate is built on the source's absolute grid `[floor(in), ceil(out))` and opened at a fractional `in` (`reverse.intermediate_span` / `view_range`, `framePlan.intermediateSpan` / `reversedViewRange`), so splits, cuts, freezes and trims of it are frame-exact like a forward curve's (Hero at 30 fps: 25 of 76 split points were one source frame off). Volume automation (`gain_env`) is applied PER SAMPLE (`aeval`, the picture's 1 µs key rule), so the client's continuous `curves.ts` envelope is the export's (the per-packet `volume` staircase was up to 0.48 dB off inside a ramp and a step up to a packet late). Every render is held to its plan (`frame_map.planned_frames`, the renderer's own seam arithmetic; a `trim=end_frame=<plan>` on the mapped picture stops an overlay input that outlives the timeline — it was the output option `-frames:v`, which closed the file before the AAC encoder's last packet and cut ~10 ms of sound off NTSC renders, gate RX, `tests/test_render_audio_tail.py`) and a mismatch fails the render. The proxy probe reads the display rotation (`render/sar.display_size`): `RECIPE_VERSION` 3.

**Canvas backgrounds and overlay blend modes (wave E, F2, as built).** CapCut's Canvas (`Clip.canvas_bg`: `color` #RRGGBB, `blur` strength 1-4, `image` a picture; omitted from the JSON while unset) fills a CONTAIN-fit main-track clip's letterbox; `Clip.blend` (14 modes; omitted while `normal`) composites an overlay clip. The ONE table is `edl/canvas_blend.py` (`GET /api/canvas-blend/presets`, dumped to `lib/canvasBlend/blendModes.json` and pinned equal).
- **Export** (`render/canvas_bg.py`, spliced in place of the contain branch's `scale … pad`): colour = `pad` in BT.709 (an untagged link is labelled BT.709 for the pad alone and its tags put back — `pad` converts with the link's matrix, and #E53935 read back 243,72,49 under 601); blur = the clip `split`, scaled to COVER a 1/4-size frame, `gblur` (steps 4, ≥ 43 dB from a true Gaussian) in RGB, bilinear back up, the fitted picture `overlay`ed at the pad's own offset; image = the picture cover-fitted ONCE in Pillow (`image_file`, cached by content; also served to the engine by `GET /api/sessions/{sid}/canvas-bg/{clip}.png`) read with `movie=` and held under the picture. Every branch that goes through RGB restores the source's own colour tags (`tags_filter`): ffmpeg 8 negotiates colorspace per link and otherwise converted the picture (24 dB). A clip without a background, or with `fit='cover'`, emits the old filter text byte for byte. Blend (`pip.blend_overlay_parts`): the element on a transparent canvas-sized RGBA frame, `lut2` with the W3C formula per RGB plane against the base (BT.709), then overlaid on the UNTOUCHED base with its alpha — `(1 − α)·Cb + α·B(Cb, Cs)`; Add and Linear Burn are CSS's Porter-Duff plus-lighter / plus-darker on the element premultiplied over black. Measured: every mode 36.4-46.2 dB against the W3C oracle on decoded renders (the nearest wrong formula ≤ 27.1), the base outside the overlay untouched.
- **Engine** (`render/canvasBg.ts` → the geometry shader's letterbox, `canvasBlur.ts`, `canvasBgImages.ts`): the background is a STILL layer at the canvas point (as built, review RE: `u_toBg` maps canvas pixels, the blur mirrored with the Transform flip), and the picture is laid over it by its matte, dimmed by `u_alpha` (opacity) and `u_fade` (fades) exactly as the export's `canvas_bg.composite_block` does — `split` → a white matte (`lutyuv`) → `alphamerge` → `overlay` on the background, opacity as an alpha stage, animation fades as `fade=…:alpha=1`. Before RE the background moved and dimmed WITH the picture; CapCut and our export hold it still. `support.ts` `CANVAS_BG_MODE`, measured (`tests/wk/test_canvas_bg_parity.py`, 16:9 and 9:16 canvases, Playwright Chromium and WebKit identical ±0.1 dB): colour **EXACT** (letterbox 51-73 dB), image **EXACT** (the export's own file, 49-50), blur **EXACT** (the same 1/4 cover + Gaussian + bilinear on the GPU, 42.7-50.2 letterbox, 35.6-42.6 frame); any kind on a ROTATED clip **APPROX** (`CANVAS_BG_ROTATED_MODE`: the rotated frame's black corners meet the background and the two rotate resamplings differ there — white at 6°: 33.0 dB). Re-measured on the still-layer composite (review RE, Chromium and WebKit identical): unmoved kinds 35.7-45.1 dB frame / 43.7-55.3 letterbox, a static pan 38.7-42.0 / 40.7-47.8 — EXACT; a picture scaled below 1, or a keyed scale/x/y, or an animation that moves the picture (`CANVAS_BG_MOVED_MODE`, reason `canvas:<kind>:moved`) — APPROX (Blur 3 at scale 0.7: 34.1-36.0 frame, the matte's soft edge against the GPU's hard one); rotated white 34.0 / 34.7 dB, still APPROX. A background picture still loading is PENDING (`canvas:image:pending`); a failed load retries with backoff (1 s → 30 s) and again when the engine comes back online. WK pending (screen locked): `VAI_WK=1 .venv/bin/python -m pytest -q tests/wk/test_canvas_bg_parity.py -k wkwebview`.
- **Live blend** (both modes): overlays are drawn by `StickerLayer`, so a blended one gets its own canvas UNDER StickerLayer's, composited by the browser with CSS `mix-blend-mode` (`lib/pipBlendLayers.ts`; a composite operation on the transparent overlay canvas cannot reach the video element beneath it). Measured against the export on the export's own decoded inputs (`tests/test_f2_blend_browser_parity.py`): every mode 36.4-45.1 dB in Chromium and Playwright WebKit, except — flagged in the Inspector (`lib/canvasBlend` `LIVE_BLEND_FLAGS`, `blendLiveNote`), never hidden — WebKit's Soft Light (its own curve, 32.0 dB) and WebKit's Color Dodge / Burn below full opacity (not clamped before the opacity mix: 16-17 dB where they saturate; exact at 100 %), and Chromium's Linear Burn (no `plus-darker`: previews Normal). Real-WKWebView blend pixels are pending a harness snapshot.

**Review RE fixes (wave E, as built).**
- **Anchored file clock for every v1 speed.** Constant-speed and 1x chains now run on the source file clock anchored at `in` (`speed_curve.anchored_const_setpts_expr`, `curve_seek` on the 1/5 s grid, `settb` + `fps=R:start_time=0`), as speed curves already did, so a split of a clip whose source rate differs from the canvas is exact on both sides (8 of 10 mixed-rate split cases were a frame off before; 20/20 probe cases now 0 frames differ). `frame_map.select_frames` / `frameMap.ts` model the same rule (`anchoredConstTicks`); the frame_map goldens were re-decoded from real renders (138 cases, model = render). A ONE-frame clip holds its frame (`tpad=stop_mode=clone` for 1/R before `fps`) instead of rendering black.
- **Voice-effect priming.** Pitch and vibrato stages have start-up latency; both sides now read `VOICE_PRIME_S` = 50 ms (2400 samples at 48 k) of source before the clip and cut it after the effect (`audio_mix.voice_prime_cut`, `voiceFx.VoicePlan.prime`, `mixGraph.primeOf`), so a seam no longer drops a hole (5 ms windows, zero-run check). Keep-pitch retimes are deliberately NOT primed (it moved a 2x clip's sound 0.49 s); the keep-pitch seam hole stays open.
- **Picture overlays and BT.709.** A still-picture overlay (PNG/JPEG sticker, a picture PiP) over an untagged base is converted with BT.709 (`canvas_bg.picture_overlay_tags`, spec R12), not the 601 default (untagged: (106,96,222) → (90,106,222)).
- **WebKit PiP colour.** WebKit's colour-managed `drawImage(<video>)` applies a ~1.96 gamma; overlays drawn by `pipDraw` in WebKit copy the frame through WebGL2 (`lib/videoColour.ts` `drawVideoExact`): old path [64,142,211], export [56,130,205], now matched.
- **PiP and sticker animations** run over the element's render window (`overlayAnimPose`), so an Out ends on the seam.
- **Blur In / Out** is always BAKED (`ANIM_BLUR_MODE`), whatever the engine's shader caps.
- **The "≈" chip.** An APPROX range at the playhead, or a live approximation (a WebKit blend flag, `lib/preview/liveApprox.ts`), shows a "≈" chip on the preview (`data-fidelity="approx"`, its reasons in the label, `fidelityLabels.ts`); nothing shows while a wait spinner is up.
- **Video fades on the anchored clock (gate X2, as built).** A clip without keys fades BEFORE the grid, on the retimed source frame's time T = (pts − `in`) / speed (through the curve for a speed curve) — the anchored clock above, read as a time; a keyed or animated clip fades on the grid (k/R), a freeze over its still on the grid. The export's pre-roll frames reach `fade` with NEGATIVE pts, and vf_fade holds its start and length as uint64, so every clip cut at `in` > 0 came out unfaded (in) or black (out) — 13.86 dB in the geometry gate; the fades now run on that clock lifted by whole seconds (`compositor._fade_clock_shift`, `st` moved in decimal: the same factor per frame). The engine's clock is `frameMap.anchoredFrameSeconds` (it rebased at the first SHOWN frame, the pre-RE chain: a source frame off whenever `in` falls between source frames). Goldens `fades_in_offset*` (11 cases: off-grid `in`, 2x, 0.5x, 25 fps in 30, a curve, a freeze, reversed 2x, keyed opacity, an animation, a Canvas background); the WK/Playwright geometry parity `fades` 40.3 dB (was 13.86), `fades_offgrid` 40.4, `fades_half_speed` 37.9, Chromium, WebKit and WKWebView alike.
- **Looped overlay inputs last their window.** A keyed-opacity sticker, an animated text and a keyed-transform text are looped PNGs `-t` exactly `re − rs` at the project rate (`text_overlay._looped_input_seconds`): the old window + 0.5 s at 30 fps outlived v1 when the item ended at the timeline's end and drove `overlay` on by itself (255 frames for a 240-frame plan; 193 for 192 at 24 fps with one frame of slack); the plan cap had hidden it. `tests/test_overlay_input_length.py` lifts the plan cap and holds the graph itself to the plan at 24/25/30/60 fps.
- **Renders read a snapshot.** Preview and export build from a deep copy of the EDL taken under the session lock (`main._edl_snapshot`), so an edit during a render cannot tear it. A chunk whose frame count misses its plan is not cached and the render falls back to one pass.

**Degraded source tier (A).** Used when a source's proxy is `failed`, or the degraded path is forced. As built (review RD2): `failed` is only a 410 or `index.json` `failed: true`. A transient open failure (5xx, 429, a network error, an init still pending) is retried five times (250 ms doubling, ≈ 7.75 s) before the engine hears of it; the source is then degraded until a later open of that key succeeds (the engine retries after 10 s and at every edit), which makes it readable again. One 500 used to degrade a source for the engine's whole life. That source's ranges are drawn from one paused `<video>` on the normalized master:
- seeked with the +1 ms bias, for paused frames, and handed over on the presented frame's own stamp at `'seeked'` (as built, wave D3 E4: rVFC fired for 1 of 255 paused seeks on a file-backed `<video>` in WKWebView, so waiting for it cost 250 ms a frame; the rVFC wait and one corrected re-seek remain for a stamp that names another frame). A proxy that fails AFTER its paused frame was asked for is shown from this tier at once, without another seek (review RD3: the frame stayed stale under a spinner until the user sought);
- its texture goes into the same compositor;
- while playing, the range is BAKED.

At most 1 such element exists. 10-bit or 4:4:4 masters are fine here because `<video>` plays them, though with dropped frames when played.

**Engine-level fallback to server mode** (today's `preview.mp4` `<video>` path, kept intact) triggers on any of:
- `!('MediaSource' in window || 'ManagedMediaSource' in window)`, or no WebGL2;
- the non-standard-rate refusal (R1);
- ≥ 3 SourceBuffer `error` events or decode errors within 60 s (each event counted once — as built, review RD2: the awaiting append also counted it, so 2 events were fatal);
- `webglcontextlost` not restored within 2 s;
- a frame-map mismatch rate above 5% of edits in a session. As built (`verify/divergence.ts`): only once ≥ 20 edits were checked **and** ≥ 2 mismatched (`fallbackMinChecks`, `fallbackMinMismatches`) — a small sample says little about a rate, and every mismatched range is already BAKED (R14), so correctness holds meanwhile; e.g. 5 mismatches in 10 edits do not fall back;
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
   - 720p proxy softness, and all-intra crf 24 artefacts (§15.1);
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
| `audio/alimiter.ts` + `audio/limiterWorklet.ts` | AudioWorklet port of ffmpeg's `alimiter` (both preview limiters: the mix's 0.97 auto-level and `PREVIEW_LIMITER`'s −1 dBFS), bit-exact to ffmpeg 8.1, padded to the compressor's 288-sample look-ahead (as built, 0.8.0 final QA) | 2 |
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


#### Phase 1 as built (milestones 1 and 2)

Where this subsection and older text above disagree, this subsection wins (the lines that were simply wrong have been corrected in place). Every item was measured; the WK numbers are real WKWebView (macOS 27) unless marked.

**Milestone 1 (1a/1b foundation, commit f3cceea), normative:**
1. **Codec string** from `parseInitSegment` (the real proxies: `avc1.641029`), never hard-coded (§3.2). Since review RD2 laneA skips an append whose codec is unknown instead of falling back to a literal.
2. **Init class = the full `avcC`** (`index.json` `init_key`): a 25 fps and a 30 fps proxy of one size are two classes (x264 writes the rate into the VUI). Not (W×H, recipe).
3. **No laneA texture upload between an overwrite at the paused playhead and its re-seek**: WebKit returns black there. Uploads happen only after `'seeked'` (paused) or in rVFC (playing).
4. **Frame size from the program map's source** (the proxy index); rVFC `width/height` only as a cross-check (it lagged one frame after an init switch in 1/17 runs).
5. The span-pack parser lives in `media/spanPack.ts`.
6. **Proxies:** crf 24 (§15.1 measurements), never upscaled below 720, colour tags via a `setparams` filter.
7. **Audio segment lengths** are `samples_for_frames` running sums per segment (R9), not `samples_for_frames(n)`.
8. **Nested blends are BAKED.**
9. **Eager proxy builds** run only when `preview.engine != server`.
10. **External pauses** (product requirement found in milestone 1): WebKit pauses a muted `<video>` by itself when the page is hidden or the window occluded (visibility hidden, `paused` true, no event from the engine). The engine treats any pause it did not issue as a transport pause: sound stops in lockstep at the same `k` (5 ms ramp), `presentedK` is kept, the state shows paused, and on return picture and sound resume together from a fresh anchor, or stay paused, per the user's last intent (§3.5 as built).

**Milestone 2, by lane:**
- **D2-video (engine picture).** Appends are HELD from the paused re-seek until the frame is uploaded at `'seeked'` (without the hold 6/200 paused seeks uploaded an older or a black picture; an rVFC confirmation was tried first and rVFC did not fire for about half of paused seeks). Rotation samples bilinearly over the F1 (canvas-size) grid with an explicit mip level, matching `vf_rotate`'s second resample (rotate 34.74 → 36.44 dB, keyframed rotation 34.61 → 36.63 dB Y-PSNR against the export). Keyframe time: see RD2 below (it first mirrored the export's `t − start` bug).
- **D2-audio.** P1-A1 is ± 1 sample at NTSC rates (§13 corrected); the P1-A2 reference for AAC sources is the server's render over lossless PCM twins of the masters (ffmpeg's decode of the whole file), because an AAC decode differs at chunk seams.
- **D2-server.** The bake key is `sha256(realpath of previews/<h>.mp4, size, recipe)`, without the mtime the proxy key has: the render cache touches a preview on every reuse. There is no separate `BAKES` cancel scope: a bake is a proxy of the preview file and rides the `PROXIES` scope, with latest-wins by cancelling the previous hash's jobs. `POST /preview?priority=low` renders niced.
- **D2-integration.** rVFC display times moved onto `performance.now()` by `DisplayTimeBase`; `getOutputTimestamp()` is the RENDERED time (output latency subtracted); drift threshold 8 ms; second anchor check at the 8th frame; external-pause resume after 250 ms of visibility and `AudioSink.whenRunning` (§3.5 as built).
- **S1 (speed render).** A speed curve is `{"curve": [[x, r], …]}`, `x` over the clip's OUTPUT, piecewise-linear `r` in 0.1–10x, footprint `S / mean(r)`, closed-form `setpts` (R4 as built). A freeze holds the first frame a 1x chain opened at `in` shows, silent. Curve sound is a cached numpy intermediate (`render/speed_audio.py`: band-limited varispeed, or WSOLA with keep-pitch, ≤ 7 ms transient error from 0.2x to 2x) read with `amovie`, because ffmpeg has no time-varying stretcher (constant-speed pieces click at every join; `asendcmd`-driven `atempo` drifts along a ramp). `FRAME_MAP_VERSION` stays 1.
- **S2 (speed edits and UI).** The presets are S1's `CURVE_PRESETS` shapes plus a label table (`edl/speed_presets.py`), served at `GET /api/speed/presets`; the browser keeps no copy (since RD2 the curve editor's range and point budget come from there too). The Inspector's menu is CapCut's six plus None and Custom; `ramp_up`/`ramp_down` are agent and Prompt-bar names. A curve clip's source cut is rounded to 1 µs, not snapped to the frame grid (snapping made a split 25 fps Montage clip 145 frames for 144).
- **Divergence fallback gates** (§7 as built): ≥ 20 edits checked and ≥ 2 mismatched before the 5% rate counts.

**Review RD2 fixes (milestone 2 fixer), with the measurement each rests on:**
- **Paused seeks can no longer wedge.** Two `currentTime` assignments of the same time in one task (seek(K) → seek(K+1) → seek(K) while laneA is busy, or a seek then `setTimeline` in the same task) fire ONE `'seeking'`; the stale-`'seeked'` guard's counters then stayed one apart and no paused seek showed again until the Preview remounted. Now only the latest paused seek assigns, an identical pending assignment is not repeated, and the seek timeout re-syncs the counters (`engineSeek.ts`). WK: 160/160 and 60/60 exact with the counters in step (before: every row failed, lag 5 and 9).
- **A paused seek re-checks that its frame is still buffered when it assigns.** A `'behind'` trim of the window already in flight could remove the frame between the readiness check and laneA going idle; the seek then went into a hole WebKit never completes, with appends held — a 2 s stall until the timeout (measured twice in 160 flip-flop seeks; now 0, 0 seek timeouts).
- **Sound restarts at the new position after a seek while playing.** WebKit presents frames of the OLD position after `currentTime` is set; the restart anchored on one (7 of 24 seeks restarted 100–400 frames away). The restart now waits for a frame within (frames elapsed since the seek + 2) of the target, at most 500 ms (`PlayingSeekGate`): 0 of 24.
- **Context loss while playing** stops and resumes both (§3.4 as built). Before: sound ran on for up to 2 s over a frozen picture, and the snapshot showed the last pause's frame (k = 60 while k = 110 was on screen).
- **Proxy open failures** (§7 as built): one 500 on `index.json` no longer degrades a source for the engine's life (WK: frame 0 exact after one retry).
- **SourceBuffer `error` events** count once (2 events were fatal).
- **Keyframe time.** The export evaluated a v1 clip's keyframed transform and opacity at `t − start` on the chain's SOURCE-local `t`: a clip not at 0 animated late or not at all, and a retimed clip at the source's pace. Both sides now evaluate at clip-local TIMELINE seconds (the chain `t` through the clip's retime: `t/speed`, a curve's `out_seconds`, or `t`), which is what the Properties panel authors keys at. Goldens `kf_start_offset` and `kf_speed2` regenerated from real renders (the other 39 geometry cases unchanged); `RENDER_BEHAVIOR_VERSION` 17 → 18.
- **Fixed in wave D3 (E1b): a split on a CURVE clip is frame-identical to the unsplit clip** (the in-anchored chain, below). What milestone 2 measured: Reference model (it matches the export frame for frame): Hero on 20 s at 30 fps split at 10 s shifts 103 of 759 frames by +1 source frame, at 12 s 236 frames by −1, at 5 s and 7.3 s none. No choice of the right half's `in` removes it (searched ±1.33 source frames in 1/40-frame steps: at best still 103), because the chain rebases at the first kept source frame, not at `in`. An exact split needs the render chain to rebase at `in` (or carry a curve offset); tracked as a follow-up.
- **`TIE_MARGIN = 0` in `timebase.frameOf` is an equivalent mutant**, measured: for every half-frame tie with k < 3,000,000 at 24000/30000/60000/120000 over 1001, and ±4 ulp around it, the float product never lands strictly on the wrong side of k + 0.5; the products that are exactly k + 0.5 in float take the exact path for 0 and 1e-6 alike. The near-tie cases are in the golden anyway.
- `textureLocked` removed (it never changed an upload: uploads only happen at a completed `'seeked'` or in rVFC). `engineCore.ts` split into `engineSeek`, `engineExternal`, `engineBake`, `engineDraw`, `engineLoop` (795 lines; in wave D3 also `engineSources`, `engineDegraded`, `engineRecovery` and `engineBase`, 775 lines).

**Acceptance numbers, fixer's full WK run** (75 WK tests green; the machine was not quiet: load 0.52–0.64 per core on 14 cores, from background system processes and the suite's own backends — a quiet re-run belongs to the gate): P1-F1 200/200, seek p95 8 ms; P1-F2 599 frames, 0 mismatches, 0 missing; paused edits 40/40, p95 2 ms; P1-F3 p95 split 10, trim 16, move 14, delete 16, ripple 14, undo 16 ms (budget 60/80); P1-F4 no stale frame, no stall; P1-F5 spinner at 81 ms, frame at 994 ms under a 1 s span delay; P1-A3 |offset| p95 4.33 ms, max 4.33 ms, 35/35 clicks paired; P1-S1 200/200; P1-B1 render 505 ms, splice 238 ms; P1-E1 1 element; rVFC handler at 1080p p50 0, p95 1, p99 3, max 7 ms (budget p99 4; the review measured 2 and 5 ms on two loaded runs).

#### Phase 1d as built (wave D3: milestone 3 and its review, RD3)

Where this and older text disagree, this wins. WK numbers are real WKWebView (macOS 27).

**The engine (lanes E4, E5; review RD3):**
- **Degraded tier** (§7): `engineDegraded.ts` + `media/degradedSource.ts`, one paused `<video>` on the master for a failed proxy's paused frames, BAKED while playing. WK: 295/295 paused frames exact to the bar, p50 16 ms, 2 media elements in all. RD3: a proxy failing after the paused frame was asked for (spans or index turning 410, or 5 transient open failures) now shows that frame from the tier at once (`EngineSources.failed` → `showPaused`): 410 case 42 ms, 500×5 case 3.8 s, where it had stayed stale under a spinner (the 410 case until the user sought).
- **Hidden page** (§3.5): laneA appends, span and bake fetches and proxy reopens are suspended while the page is hidden and resume when it is shown.
  - *As built (0.8.0 QA, C1).* **WebKit flips `document.visibilityState` a task BEFORE it dispatches `visibilitychange`.** A seek (or a landed span) in that gap found laneA not yet suspended and removed its whole window while hidden (`reset`, [0,45] → [0,0]; the WK hidden test failed 3/18, 1/30 measured; it was timing, not wave E: the page sets no canvas background). laneA's pump and both proxy stores now ask `EngineSources.hiddenNow()` before they start work: a page seen hidden suspends everything at once, only the 'visible' event resumes (so reopen/prefetch/re-show stay in one place), and `attach()` resumes a suspension that predates its listener. 30/30 after, with seeks landing in the gap.
  - **The parked element is not still.** On an occluded window's visible flips WebKit fires `play` on the parked `<video>`; the engine pauses it at once, yet WebKit went on presenting frames (178 → 223 with `paused` true, measured). `play()` trusted its stale `elementFrame` and the return resumed picture and sound 1.4-1.7 s past the stop frame (2 of 45 runs); one drifted frame was then drawn at the resume. An external `play` now forgets where the element stands (`ExternalHost.elementMoved`), and the run-start gate is armed by where the ELEMENT stood, not the canvas. The ~500 ms `afterMax` of `test_webkit_pauses_on_hide_and_occlusion…` was never a late resume: it measured those undrawn element frames (no sound by design; the canvas never showed them), in a window that ran on into the next phase's stop. The test now judges only flashes the engine drew, per phase, and asserts the return starts at the stop frame (0 skips in 90 runs after).
- **P1-R1 fallbacks**, each with a WK test: MediaSource deleted → server mode, and the server's `preview.mp4` then plays (`tests/wk/test_wk_fallback_preview.py`, added by RD3: the half the robustness suite pointed at did not exist); 3 decode errors in 60 s → server (1–2 rebuild laneA and show the exact frame); `webglcontextlost` not restored in 2 s → server; proxy failed → the degraded tier.
- **Proxy span I/O** (`media/proxyIndex.ts`, RD3): a transient span failure (5xx, a network error, a response that has not finished in `spanTimeoutMs`, 15 s) is retried by the store itself, 250 ms doubling to 1 s — a paused engine has nothing else that would ask again (8 failures used to stop every request for good; now shown at 6.8 s). A span answering 202 (an on-demand encode) goes back into the queue with its Retry-After instead of sleeping in its fetch slot: with spans 22-29 pending, a paused seek to a ready span showed in 8 ms (it waited behind all three slots for 30 s). `MAX_PENDING_MS` still bounds a 202 run. *Final QA r2:* a key reported degraded for a span streak (`SPAN_DEGRADE_AFTER`) is reopened as soon as one of its spans lands (`ProxyStore.onRecovered` → `EngineSources.open()`), not at the 10 s `PROXY_REOPEN_MS` reopen: with 5 failures per span, Play waited 7.6 s on a source that had been serving again since 2.8 s; now 33 ms (`tests/wk/test_wk_robustness.py` [8] shown again). A span or audio job that fails for a full disk (ENOSPC) holds its key for `DISK_FULL_HOLD_S` (10 s, `ingest/proxy_queue.py`): the routes answer `507 disk_full` without queueing another encode (each 202 poll used to start a fresh encode that failed on write — 102 in two minutes — and the engine never left the spinner), `index.json` carries `span_error: "disk_full"`, and the 5xx counts toward the degraded tier.
- **Stuck play** (RD3): a watchdog (`engineLoop.StallWatch`) restarts a run whose presented frame has not moved for 1.5 s while the element is not waiting for data — or is "waiting" but its own clock moves (the flag is cleared only by a presented frame). At most 3 restarts without progress; each logs the run's state. The intermittent freeze RD3 saw once (isPlaying, playhead on k=1 for 2.7 s, no error) was not reproduced; the WK test induces the silent form (a lost rVFC chain) and the run resumes, every frame exact.
- **Audible edit latency while playing** ≤ 250 ms (§11.1): the lead above; measured p95 below the budget in `tests/wk/test_wk_audible_edit.py`.
- **P1-M1 soak** (E5, §13 as built): the footprint that survives a forced collection, slope within budget; the 5-minute gate form checks gross leaks only.
- `engineCore.ts` 775 lines (the observable state moved to `engineBase.ts`).

**The model and the export it mirrors (E1a, E1b, E2; RD3):**
- Keyframes at `playhead − clip.start` on every clock, SAR by display shape (see §3.4 pass 4 and §5 R12 as built).
- A speed-CURVE clip's chain runs on the source file's clock anchored at `in` (`edl/speed_curve.py`): splits, cuts and trims of a curve clip export the whole clip's frames. `frame_map` ≈ 11× faster (`frame_map_vec.py`), output byte-identical.
- PiP (v2+) clips take speed, curves, freeze and reverse, on v1's per-clip frame rule (E2).
- **A reversed view keeps its clip's footprint** (RD3): `reverse.view_out` = the clip's own `out − in`, the float `effective_duration` divides (it was the intermediate's `time_of(M)`: equal in exact arithmetic, but a reversed 2x/4x clip with an odd frame count sat on a .5-frame tie the two rounded apart — 653-693 of 3000 grid-aligned reversed 2x clips were a frame off, and every later v1 clip slid; six 63-frame reversed 2x clips exported 192 frames for the timeline's 189). The goldens that hold reversed clips were re-rendered: `rates` (24 cases) and `fuzz_03_p50`, `fuzz_18_p29.97`; `frame_plan_cases` 222/229/388 re-derived. The TS port (`framePlan.reversedViewOut`, `pipTime`) follows.
- **A seam is judged on the frame grid** (RD3, R3): with the project rate, two v1 clips are adjacent unless a whole frame separates the first's last frame and the next's first (what `_v1_frame_plan` inserts filler for); seconds-adjacent pairs stay seams as before. A retimed clip's exact end is rarely on the grid (1.5x on 91 frames ends 11.1 ms before its rippled neighbour), and the 1 ms rule had dropped the transition after it: stored, reported, never applied. Golden `xfade_retimed_p30/p25` added; `fuzz_11_p60` and `frame_plan_cases` 42/450/476 now charge their seams.
- Edit ops on REVERSED clips cut from `out` down (RD3, `speed_edit.cut_point`/`piece_range`): exact at 1x and 2x on v1 and overlays at every frame; a reversed CURVE split is within one source frame, not exact (its right piece's intermediate is built on a grid a fraction of a frame off the whole's; follow-up).
- Transform keyframes are partitioned by split, cut_range and head trims (RD3): each piece keeps the part of the animation it plays, re-based.
- `RENDER_BEHAVIOR_VERSION` 22.

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
- **P1-A1 sample placement** (Chromium + WK, OfflineAudioContext). Offline render of the fixture timeline: each click at output sample `S(k)` ± 0 at an integer samples-per-frame rate (30 fps: exact), ± 1 at NTSC rates, where the source's frame starts and the program's frame starts round independently (as built, D2-audio); the client equals the server's render sample for sample, and clip boundaries sit exactly on the `samples_for_frames` running sums.
- **P1-A2 mix parity.** Offline client render vs the server `_audio_only_graph` render of the same EDL: per-50 ms RMS within 0.25 dB (EXACT features), xcorr lag 0 ± 1 sample.
- **P1-A3 live A/V sync** (WK only). An AudioWorklet tap records the master output with `currentFrame`. White flash frames coincide with clicks at 20 cuts. Offset = `ctxAt(expectedDisplayTime(flash)) − ctxTime(click)`: **p95 ≤ 10 ms, max ≤ 20 ms**, and ≤ 1 frame within 100 ms of play start.
- **P1-S1 structural agreement.** For each of 200 edits, the client map equals `/frame_map` (0 mismatches).
- **P1-M1 soak** (WK, nightly). A 12-minute 1080p-source timeline looped for 30 min, an edit every 10 s. WebContent footprint (via `task_info` from the harness) slope < 1 MB/min; `buffered` span ≤ 45 s; audio LRU ≤ cap; no unhandled `QuotaExceededError`.
  - *As built (wave D3, E5; `tests/wk/test_wk_soak.py`, `wk` + `slow`, opt-in with `-m slow` or `VAI_SOAK=short|full`).* The footprint is `phys_footprint` of the view's own WebContent pid, read with `proc_pid_rusage` (task_info's number without the task port); the child reports the pid through WKWebView SPI and the sampler records the executable. **The slope is judged on the footprint that survives a forced full JS collection** (`WKProcessPool _garbageCollectJavaScriptObjectsForTesting` every 30 s, the lowest sample after each), not the raw one: measured, 128 MB of dropped fetch buffers stayed in the raw footprint (149 MB) until a forced collection took it to 21 MB, so the raw level moves 100-200 MB with the collector's heuristics (one 30-minute run's raw floor stepped from ~100 to ~290 MB a minute after the loop and stayed there: 9.9 MB/min raw, flat after the step). Two full runs (load 0.56-0.71/core, not quiet): retained slope **0.47 and 0.72 ± 0.26 MB/min**, retained 249-340 MB, raw peak 485 MB, GPU process flat (≈ 390 MB, slope 0.0); `buffered` peak 42 s; span LRU peak 128.0 MB of 128; audio LRU 95.2 MB of 96; 0 `QuotaExceededError`; 173-178 edits (split/trim/move/delete/undo in rotation, p95 25-28 ms); 29/29 paused bar checks; 52,000+ playing frames judged, 0 wrong in one run and **1 wrong in the other (k = 2 drawn with source frame 1, 0.9 s into the first play of the page; not reproduced in 40 dedicated first-play page loads, nor in the opt-in repro `test_play_start_first_frames_are_the_right_ones` (40 runs from a paused frame, 1,439 frames judged, 0 wrong): open; no engine change was made for it)**; 1 media element. The 5-minute gate form starts 60 s before the end (the loop and its refill in the warm-up), runs 64/24 MB caps and fails a gross leak only (the lowest retained point of the last third ≤ the first third's + budget × distance + 32 MB; the lowest, because what rides on the retained set is one-sided: memory not yet collected or returned): 2.5 minutes resolve the slope only to ±5-12 MB/min. Short-form runs after the final statistic (load 0.44-0.48/core): thirds growth −25.4 and +13.3 MB over 1.5-1.8 min (bound 33.5-33.8), retained 60-165 MB, span LRU 64.0 of 64 MB, audio 23.8 of 24 MB, 29 edits (p95 30-35 ms), 9/9 paused checks, 8,820+ frames judged, 0 wrong, 0 divergence. **Found and fixed by the soak:** play at the end of a program whose start had left the span LRU stopped again 8 ms later on the last frame, 3,249 times in one run (WebKit keeps presenting old-position frames and fires `waiting` before the new start is buffered); `RunStartGate` (`engineSeek.ts`) ignores those until a frame of the new position is presented.
- **P1-E1 element budget.** Over a 100-edit script, the count of `HTMLMediaElement`s created by the engine is ≤ 2 at all times.
  - *As built (E5).* Through the app path (`/dispatch?include=edl` → `PreviewController.applyTimeline`), 100 edits (split/trim/move/delete/undo, 50 of them while playing, seeks between): 1 element created, at most 1 live, in WK and in Playwright Chromium and WebKit; the 30-minute soak also stays at 1.
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
   **Settled in 1a: crf 24** (`ingest/proxy.py` `DEFAULT_CRF`; `VAI_PROXY_CRF` overrides; the crf is part of the proxy key). Measured 2026-09-26 on all 82 workdir masters (42.3 source-minutes; 2 encode threads, nice 10, on a machine with a load average of 8 to 15 from concurrent work):
   - crf 23: video 40.0 MB per source-minute (66/82 sources ≤ 40, max 90.0), 11.5× realtime aggregate (median 13.2×).
   - crf 24: video 35.9 MB per source-minute (67/82 ≤ 40, max 79.9), 12.3× realtime aggregate (median 14.1×).
   - The misses are dense portrait 1080×1920 and 4K sources (720×1280 proxies average 50.7 MB/min at crf 24); the 4K masters also set the slowest build (4.1×, decode-bound). 1280×720 proxies average 30.8 MB/min at 16.5×.
   - FLAC audio (24-bit stereo) adds about 8 MB per source-minute and decodes at about 160× realtime.
   - On-demand span (2 s, one encode of two spans): p50 254 ms, p95 596 ms, max 1.8 s (4K sources) over 3 random spans per source; 1080p sources p95 about 300 ms.
   - The §11.4 targets of ≥ 15× per source and ≤ 400 ms p95 per span are met for ≤ 1080p landscape sources and missed for 4K and dense portrait sources on this loaded machine.
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
