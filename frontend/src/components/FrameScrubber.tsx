/**
 * Frame-accurate seek via WebCodecs + mp4box.js.
 *
 * The HTML <video> element's `currentTime = t` is allowed to land on any keyframe
 * the browser feels like, which is why CapCut/Premiere-style scrubbing in plain
 * <video> always feels mushy. WebCodecs lets us:
 *   1. Demux the mp4 with mp4box → get sample tables (decode order, keyframe
 *      flags, exact PTS in microseconds).
 *   2. Find the latest keyframe ≤ target time, push every sample from that
 *      keyframe through the target into a `VideoDecoder`.
 *   3. Render the frame whose `timestamp` matches the target to a canvas.
 *
 * The component exposes an imperative handle: caller calls `seek(seconds)` to
 * paint the exact frame at that time. The rest of the time the canvas is
 * hidden — the regular <video> handles playback.
 */
import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react'
// mp4box.js publishes named exports; there's no `default`. We pull the pieces
// we need: `createFile` (the demuxer factory), `DataStream` (used to serialise
// codec description boxes for VideoDecoder) and the `Endianness` enum.
// mp4box 2.3 DOES ship types (`dist/mp4box.all.d.ts`), so this import needs no
// suppression — a `@ts-expect-error` here is itself an error under `tsc -b`.
import { createFile, DataStream, Endianness } from 'mp4box'
import { frameChainFor } from '../lib/frameWalk'

export interface FrameScrubberHandle {
  seek: (timeSeconds: number) => Promise<void>
  isReady: () => boolean
  /**
   * Copy the <video>'s CURRENTLY displayed frame onto the canvas, synchronously.
   *
   * The canvas is revealed the instant a scrub starts, but its own exact frame
   * only lands once the async WebCodecs walk finishes (~150ms). Without this,
   * the first scrub of a session fades up a never-painted (transparent) canvas
   * over the video — a visible flash — and later scrubs briefly show the
   * previous scrub's frame. Priming from the element that is already on screen
   * makes the reveal a genuine no-op: identical pixels, then one clean swap to
   * the exact frame.
   */
  prime: (video: HTMLVideoElement | null) => void
}

interface Props {
  src: string
  width: number
  height: number
  visible: boolean
}

interface Sample {
  cts: number   // composition time, microseconds
  dts: number   // decode time, microseconds
  duration: number
  is_sync: boolean
  data: ArrayBuffer
}

interface MP4BoxFile {
  onReady: (info: { videoTracks: { id: number; codec: string; nb_samples: number;
                                   timescale: number; movie_duration: number;
                                   movie_timescale: number; track_width: number;
                                   track_height: number }[] }) => void
  // mp4box's real signature is (module, message) — declaring one param made the
  // handler log the MODULE name where it meant to log the message.
  onError: (module: string, message: string) => void
  onSamples: (id: number, _user: unknown, samples: Sample[]) => void
  setExtractionOptions: (id: number, user: unknown,
                         opts: { nbSamples: number }) => void
  appendBuffer: (buf: ArrayBuffer & { fileStart: number }) => void
  start: () => void
  flush: () => void
  getTrackById: (id: number) => {
    samples: Sample[]
    mdia: { mdhd: { timescale: number } }
  }
}

export const FrameScrubber = forwardRef<FrameScrubberHandle, Props>(
  function FrameScrubber({ src, width, height, visible }, ref) {
    const canvasRef = useRef<HTMLCanvasElement>(null)
    // Hidden <video> used only when mp4box/WebCodecs can't demux the file
    // (malformed/torn mp4, edit lists, an unsupported codec). It still scrubs —
    // just by keyframe-snapped `currentTime` seeks instead of frame-exact.
    const fallbackVideoRef = useRef<HTMLVideoElement>(null)
    const [ready, setReady] = useState(false)
    const stateRef = useRef<{
      decoder: VideoDecoder | null
      samples: Sample[]            // all samples in decode order
      keyIdx: number[]             // indices of keyframe samples
      timescale: number            // ticks per second
      lastDecodedCts: number       // last cts decoded, microseconds
      targetUs: number             // current seek target, microseconds
      paintUs: number              // composition timestamp of the ONE frame
                                   // this seek should paint (see seek())
      painted: boolean             // has THIS seek painted yet? Only used to
                                   // arm the non-conforming-decoder safety net
                                   // in the output gate; false on a conforming
                                   // one only until the exact match lands.
      pendingTarget: number | null // seconds — newest seek queued behind an
                                   // in-flight walk (drag coalescing)
      inFlight: boolean            // a decode walk + flush is running
      lastSeekKey: number          // the keyframe-sample index used last seek
      decoderConfig: VideoDecoderConfig | null
      useFallback: boolean         // mp4box failed → drive the hidden <video>
    }>({
      decoder: null, samples: [], keyIdx: [], timescale: 1,
      lastDecodedCts: -1, targetUs: 0, paintUs: -1, painted: false, pendingTarget: null, inFlight: false,
      lastSeekKey: -1, decoderConfig: null, useFallback: false,
    })

    // Load + demux the mp4 whenever `src` changes
    useEffect(() => {
      let cancelled = false
      let sawSamples = false
      // Guard: calling close() on an already-closed VideoDecoder throws
      // ("Cannot call 'close' on a closed codec"). The cleanup may have
      // already closed it before this re-run, so check state first.
      const prev = stateRef.current.decoder
      if (prev && prev.state !== 'closed') {
        try { prev.close() } catch {}
      }
      stateRef.current = {
        decoder: null, samples: [], keyIdx: [], timescale: 1,
        lastDecodedCts: -1, targetUs: 0, paintUs: -1, painted: false, pendingTarget: null, inFlight: false,
        lastSeekKey: -1, decoderConfig: null, useFallback: false,
      }
      // Detach any prior fallback <video> source so a stale clip can't paint.
      const fv0 = fallbackVideoRef.current
      if (fv0) { try { fv0.removeAttribute('src'); fv0.load() } catch {} }
      setReady(false)
      if (!src) return

      // Switch to the hidden-<video> fallback. Idempotent, and a no-op once the
      // WebCodecs path is already producing frames (so a late, benign mp4box
      // onError can't tear down a working scrubber).
      function enableFallback(reason: string): void {
        const st = stateRef.current
        if (cancelled || st.useFallback) return
        if (st.decoder && st.samples.length > 0) return  // WebCodecs already works
        console.warn('[FrameScrubber] mp4box failed, falling back to <video> seek:', reason)
        st.useFallback = true
        const d = st.decoder
        if (d && d.state !== 'closed') { try { d.close() } catch {} }
        st.decoder = null
        const v = fallbackVideoRef.current
        if (!v) return
        v.src = src
        try { v.load() } catch {}
        const onLoaded = () => { if (!cancelled) setReady(true) }
        v.addEventListener('loadeddata', onLoaded, { once: true })
        // Some browsers fire 'canplay' but not 'loadeddata' for short clips.
        v.addEventListener('canplay', onLoaded, { once: true })
      }

      // `MP4BoxFile` is a deliberately NARROW view of mp4box's `ISOFile` —
      // only the members this scrubber touches. It is not assignable to/from
      // `ISOFile` directly (ISOFile.onReady takes the full `Movie`, ours takes
      // the subset we read), so the hop through `unknown` is required. The
      // previous direct `as MP4BoxFile` cast was a `tsc -b` error AND hid the
      // onError arity bug fixed below.
      const mp4 = createFile() as unknown as MP4BoxFile

      mp4.onError = (module: string, message: string) => {
        // mp4box.js emits these on abort + on real parse errors. A real parse
        // error means it can't demux this file — switch to the <video> fallback
        // so scrubbing still works instead of silently disabling it.
        console.warn('[FrameScrubber] mp4box error:', module, message)
        enableFallback('onError: ' + message)
      }

      let trackId: number | null = null
      mp4.onReady = (info) => {
        // EVERYTHING in here runs inside mp4box's stack frame. A synchronous
        // throw here propagates to whatever called appendBuffer/flush — which
        // is our async loop, where the unhandled rejection bubbles up to React
        // and the editor goes blank. Catch + log instead.
        try {
          const vt = info.videoTracks[0]
          if (!vt) return
          trackId = vt.id
          stateRef.current.timescale = vt.timescale
          let description: Uint8Array | undefined
          try {
            description = extractDescription(mp4, vt.id)
          } catch (e) {
            enableFallback('codec description: ' + e)
            return
          }
          const config: VideoDecoderConfig = {
            codec: vt.codec,
            codedWidth: vt.track_width,
            codedHeight: vt.track_height,
            description,
          }
          stateRef.current.decoderConfig = config

          const decoder = new VideoDecoder({
            output: (frame) => {
              // Paint EXACTLY the one frame seek() picked, by its composition
              // timestamp. Everything else the walk feeds is decode-only:
              // the GOP intermediates before the target (painting those made a
              // drag visibly rewind to the keyframe and replay forward), and —
              // just as important — the reference frames AFTER it that a
              // B-frame stream forces us to feed (see seek()). The old gate
              // was "covers-or-follows the target", which paints every frame
              // from the covering one onward; since the decoder emits in
              // PRESENTATION order, the last one to land won, so the canvas
              // ended up showing a frame past the target and the picture
              // stepped BACK when the <video> was revealed underneath.
              try {
                // ±2µs: cts/duration can be fractional in the track timescale
                // while VideoFrame.timestamp is integer microseconds.
                if (Math.abs(frame.timestamp - stateRef.current.paintUs) <= 2) {
                  stateRef.current.painted = true
                  renderFrame(frame)
                } else if (!stateRef.current.painted
                           && frame.timestamp > stateRef.current.paintUs) {
                  // Safety net for a decoder that does not echo the chunk
                  // timestamp back on the decoded frame. WebCodecs REQUIRES it
                  // (the output VideoFrame's timestamp is set from the
                  // EncodedVideoChunk's), and Chromium does — but this is the
                  // one claim in the scrubber that cannot be measured on the
                  // engine it matters most for, WKWebView, and the cost of
                  // being wrong is asymmetric: an unmatched gate paints
                  // NOTHING, leaving a blank canvas over an otherwise correct
                  // <video>.
                  //
                  // Frames are emitted in PRESENTATION order, so the first one
                  // past the target proves the exact match is never coming.
                  // Painting it degrades to the old "covers-or-follows"
                  // behaviour — at most one frame late, which is what shipped
                  // before this branch — instead of degrading to blank. On a
                  // conforming decoder the exact branch always fires first and
                  // this is unreachable, so it changes nothing on Chromium.
                  stateRef.current.painted = true
                  renderFrame(frame)
                }
              } finally { frame.close() }
            },
            error: (e) => console.warn('[FrameScrubber] decoder error:', e),
          })
          try {
            decoder.configure(config)
          } catch (e) {
            try { decoder.close() } catch {}
            enableFallback(`configure rejected codec ${vt.codec}: ${e}`)
            return
          }
          stateRef.current.decoder = decoder
          mp4.setExtractionOptions(vt.id, null, { nbSamples: vt.nb_samples })
          mp4.start()
        } catch (e) {
          enableFallback('onReady: ' + e)
        }
      }

      mp4.onSamples = (id, _u, samples) => {
        if (id !== trackId) return
        const st = stateRef.current
        const offset = st.samples.length
        for (let i = 0; i < samples.length; i++) {
          const s = samples[i]
          // Convert sample times (in track timescale) to microseconds.
          const cts = (s.cts * 1_000_000) / st.timescale
          const dts = (s.dts * 1_000_000) / st.timescale
          const dur = (s.duration * 1_000_000) / st.timescale
          st.samples.push({ ...s, cts, dts, duration: dur })
          if (s.is_sync) st.keyIdx.push(offset + i)
        }
        // The first batch arriving lights up scrubbing; we don't have to wait
        // for every sample to be parsed before allowing seek.
        // `sawSamples` is a per-effect-run local, NOT the `ready` state. Using
        // `!ready` here read the value captured when this effect run was
        // created — which is `true` on every re-run after the first successful
        // load (a preview re-render swaps `src`). The re-run's own
        // `setReady(false)` above never reached that stale closure, so the
        // guard short-circuited forever and the scrubber went permanently
        // dead after the very first edit of a session: `isReady()` false →
        // Preview never shows the canvas → every paused seek exposed the raw
        // <video> GOP-walk (keyframe, then visibly decoding forward frame by
        // frame), which is the "stutters when I pause and move the playhead"
        // report. It looked intermittently fixed because a freshly-loaded
        // session, before any edit, still had its first (working) src.
        if (!cancelled && !sawSamples) { sawSamples = true; setReady(true) }
      }

      function renderFrame(frame: VideoFrame) {
        const c = canvasRef.current
        if (!c) return
        const ctx = c.getContext('2d')
        if (!ctx) return
        if (c.width !== frame.codedWidth) c.width = frame.codedWidth
        if (c.height !== frame.codedHeight) c.height = frame.codedHeight
        ctx.drawImage(frame, 0, 0)
        stateRef.current.lastDecodedCts = frame.timestamp
      }

      // Stream the file in chunks — mp4box wants ArrayBuffers tagged with the
      // byte offset they came from in the source file.
      ;(async () => {
        try {
          const resp = await fetch(src)
          if (!resp.ok) throw new Error(`fetch ${src} → ${resp.status}`)
          const reader = resp.body?.getReader()
          if (!reader) throw new Error('no body reader')
          let offset = 0
          while (!cancelled) {
            const { done, value } = await reader.read()
            if (done) break
            const buf = value.buffer.slice(
              value.byteOffset, value.byteOffset + value.byteLength
            ) as ArrayBuffer & { fileStart: number }
            buf.fileStart = offset
            try { mp4.appendBuffer(buf) }
            catch (e) {
              // mp4box throws on truncated / malformed boxes (e.g. an invalid
              // box in a torn preview). Stop feeding it and switch the scrubber
              // to the <video> fallback for this file.
              enableFallback('appendBuffer: ' + e)
              return
            }
            offset += value.byteLength
          }
          try { mp4.flush() } catch (e) {
            enableFallback('flush: ' + e)
          }
        } catch (e) {
          if (!cancelled) console.warn('[FrameScrubber] fetch failed:', e)
        }
      })()

      return () => {
        cancelled = true
        const d = stateRef.current.decoder
        if (d && d.state !== 'closed') {
          try { d.close() } catch {}
        }
        const v = fallbackVideoRef.current
        if (v) { try { v.removeAttribute('src'); v.load() } catch {} }
      }
    }, [src])

    useImperativeHandle(ref, () => ({
      isReady: () => ready,
      prime(video: HTMLVideoElement | null) {
        if (video && video.readyState >= 2) paintVideoFrame(video, canvasRef.current)
      },
      async seek(timeSeconds: number) {
        const st = stateRef.current

        // Fallback path: drive the hidden <video> and paint the seeked frame.
        if (st.useFallback) {
          const v = fallbackVideoRef.current
          if (!v) return
          if (v.readyState < 2) {
            await new Promise<void>((res) => {
              v.addEventListener('loadeddata', () => res(), { once: true })
              v.addEventListener('canplay', () => res(), { once: true })
            })
          }
          await seekVideoElement(v, timeSeconds)
          paintVideoFrame(v, canvasRef.current)
          return
        }

        if (!st.decoder || !st.samples.length) return

        // Coalesce concurrent seeks: a paused drag fires one unawaited seek
        // per mousemove. If a walk+flush is already in flight, just remember
        // the newest target — the in-flight call runs one more walk for it
        // after its flush, collapsing N mousemoves into at most 2 walks.
        if (st.inFlight) {
          st.pendingTarget = timeSeconds
          return
        }
        st.inFlight = true
        try {
          let target = timeSeconds
          for (;;) {
            const targetUs = target * 1_000_000
            // Arm the paint gate (see the decoder output callback) before
            // any chunk is fed.
            st.targetUs = targetUs

            // Which frames to feed, and which single one to paint. The rule
            // lives in lib/frameWalk (pure + unit-tested) because it turns on
            // decode-order vs composition-order, which is easy to get wrong in
            // review and instantly visible on screen: the canvas painted a
            // frame ~2 ahead of the <video> underneath, so the picture snapped
            // BACK the moment the canvas handed over. Measured on a
            // frame-numbered fixture: canvas 165, video 163, on every paused
            // seek and every single-frame step.
            const { startIdx, lastIdx, paintUs, maxEndUs } =
              frameChainFor(st.samples, st.keyIdx, targetUs)
            if (lastIdx < 0) break
            for (let i = startIdx; i <= lastIdx; i++) {
              const s = st.samples[i]
              const chunk = new EncodedVideoChunk({
                type: i === startIdx || s.is_sync ? 'key' : 'delta',
                timestamp: s.cts,
                duration: s.duration,
                data: s.data,
              })
              st.decoder.decode(chunk)
            }
            // The frame the canvas must show — and the exact value the output
            // gate compares against. `painted` re-arms the gate's safety net
            // for THIS seek; without the reset a previous seek's success would
            // leave it disarmed for the rest of the session.
            st.paintUs = paintUs
            st.painted = false
            // A target past the last fed sample would otherwise fail the
            // gate and paint nothing — clamp it to the walk's real end.
            // (-2µs absorbs float cts/duration truncating to VideoFrame's
            // integer-microsecond timestamps.)
            st.targetUs = Math.min(st.targetUs, maxEndUs - 2)
            // Force the decoder to emit any queued frames so the canvas paints.
            await st.decoder.flush().catch(() => {})
            st.lastSeekKey = startIdx

            // src changed mid-walk: the effect wholesale-replaced stateRef
            // (and closed this decoder) — this state object is dead, stop.
            if (stateRef.current !== st) break
            const next = st.pendingTarget
            st.pendingTarget = null
            if (next === null || next === target) break
            target = next
          }
        } finally {
          st.inFlight = false
        }
      },
    }), [ready])

    return (
      <>
        <canvas
          ref={canvasRef}
          width={width}
          height={height}
          style={{
            position: 'absolute', inset: 0, width: '100%', height: '100%',
            objectFit: 'fill', pointerEvents: 'none',
            opacity: visible && ready ? 1 : 0,
            transition: 'opacity 60ms linear',
          }}
        />
        {/* Hidden decode surface for the fallback path. `src` is set only when
            mp4box fails, so we never double-fetch the file when WebCodecs works.
            muted + playsInline so a frame can be decoded without user gesture. */}
        <video
          ref={fallbackVideoRef}
          muted
          playsInline
          preload="auto"
          crossOrigin="anonymous"
          style={{ display: 'none' }}
        />
      </>
    )
  }
)


// Seek a <video> to `t` and resolve once the frame is ready. A no-op seek (we're
// already there) won't emit 'seeked', so short-circuit that case.
function seekVideoElement(video: HTMLVideoElement, t: number): Promise<void> {
  return new Promise((resolve) => {
    if (video.readyState >= 2 && Math.abs(video.currentTime - t) < 1e-3) {
      resolve(); return
    }
    video.addEventListener('seeked', () => resolve(), { once: true })
    try { video.currentTime = Math.max(0, t) }
    catch { resolve() }
  })
}

// Paint the current <video> frame onto the scrubber canvas, scaled to fit.
function paintVideoFrame(video: HTMLVideoElement, canvas: HTMLCanvasElement | null): void {
  if (!canvas) return
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  const w = video.videoWidth, h = video.videoHeight
  if (w && h) {
    if (canvas.width !== w) canvas.width = w
    if (canvas.height !== h) canvas.height = h
  }
  try { ctx.drawImage(video, 0, 0, canvas.width, canvas.height) } catch {}
}


// mp4box stores codec descriptions as `entries[0].avcC` / `hvcC` etc.
// VideoDecoder needs them as a Uint8Array. This grovels them out.
function extractDescription(mp4: MP4BoxFile, trackId: number): Uint8Array | undefined {
  const track = mp4.getTrackById(trackId) as unknown as {
    mdia: { minf: { stbl: { stsd: { entries: Array<{
      avcC?: { write: (s: { adjustUint32: (n: number, v: number) => void;
                            position: number; getEndPosition: () => number }) => void }
      hvcC?: { write: (s: { adjustUint32: (n: number, v: number) => void;
                            position: number; getEndPosition: () => number }) => void }
      av1C?: { write: (s: { adjustUint32: (n: number, v: number) => void;
                            position: number; getEndPosition: () => number }) => void }
    }> } } } }
  }
  const entry = track.mdia.minf.stbl.stsd.entries[0]
  const box = entry.avcC || entry.hvcC || entry.av1C
  if (!box) return undefined
  // mp4box doesn't expose the raw bytes directly; build a tiny stream.
  // Reference: https://github.com/gpac/mp4box.js/issues/243#issuecomment-1003305708
  // BIG_ENDIAN lives on the `Endianness` enum, NOT as a DataStream class-static:
  // `DataStream.BIG_ENDIAN` is `undefined` (verified at runtime), which fell
  // through to the constructor's documented BIG_ENDIAN default — accidentally
  // correct. Note `DataStream.ENDIANNESS` is 2 (LITTLE_ENDIAN), so do NOT
  // "fix" this by reaching for that static.
  const stream = new DataStream(undefined, 0, Endianness.BIG_ENDIAN) as unknown as {
    adjustUint32: (n: number, v: number) => void; position: number;
    getEndPosition: () => number; buffer: ArrayBuffer
  }
  // Write box → stream then trim the leading 8-byte size+type box header.
  // (`description` for AVC/HEVC must be the avcC/hvcC payload, no header.)
  ;(box as { write: (s: typeof stream) => void }).write(stream)
  return new Uint8Array(stream.buffer, 8)
}
