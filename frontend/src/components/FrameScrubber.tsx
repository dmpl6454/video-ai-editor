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
// mp4box.js publishes named exports; there's no `default`. We pull the two
// pieces we need: `createFile` (the demuxer factory) and `DataStream`
// (used to serialise codec description boxes for VideoDecoder).
// @ts-expect-error — mp4box.js has no bundled types
import { createFile, DataStream } from 'mp4box'

export interface FrameScrubberHandle {
  seek: (timeSeconds: number) => Promise<void>
  isReady: () => boolean
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
  onError: (e: string) => void
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
      pendingTarget: number | null // seconds
      lastSeekKey: number          // the keyframe-sample index used last seek
      decoderConfig: VideoDecoderConfig | null
      useFallback: boolean         // mp4box failed → drive the hidden <video>
    }>({
      decoder: null, samples: [], keyIdx: [], timescale: 1,
      lastDecodedCts: -1, pendingTarget: null, lastSeekKey: -1,
      decoderConfig: null, useFallback: false,
    })

    // Load + demux the mp4 whenever `src` changes
    useEffect(() => {
      let cancelled = false
      // Guard: calling close() on an already-closed VideoDecoder throws
      // ("Cannot call 'close' on a closed codec"). The cleanup may have
      // already closed it before this re-run, so check state first.
      const prev = stateRef.current.decoder
      if (prev && prev.state !== 'closed') {
        try { prev.close() } catch {}
      }
      stateRef.current = {
        decoder: null, samples: [], keyIdx: [], timescale: 1,
        lastDecodedCts: -1, pendingTarget: null, lastSeekKey: -1,
        decoderConfig: null, useFallback: false,
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

      const mp4 = createFile() as MP4BoxFile

      mp4.onError = (e: string) => {
        // mp4box.js emits these on abort + on real parse errors. A real parse
        // error means it can't demux this file — switch to the <video> fallback
        // so scrubbing still works instead of silently disabling it.
        console.warn('[FrameScrubber] mp4box error:', e)
        enableFallback('onError: ' + e)
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
              try { renderFrame(frame) } finally { frame.close() }
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
        if (!cancelled && !ready) setReady(true)
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
        const targetUs = timeSeconds * 1_000_000

        // Bisect to find latest keyframe index whose cts ≤ target
        let lo = 0, hi = st.keyIdx.length - 1, best = 0
        while (lo <= hi) {
          const mid = (lo + hi) >> 1
          const sIdx = st.keyIdx[mid]
          const cts = st.samples[sIdx].cts
          if (cts <= targetUs) { best = mid; lo = mid + 1 }
          else hi = mid - 1
        }
        const startIdx = st.keyIdx[best]

        // Walk forward from the keyframe through every sample whose cts ≤ target.
        // We feed everything to the decoder so output frames before the target
        // are decoded but discarded by `renderFrame` (the latest one wins).
        for (let i = startIdx; i < st.samples.length; i++) {
          const s = st.samples[i]
          if (s.cts > targetUs && i > startIdx) {
            // Include the first sample whose cts > targetUs so a target
            // that lies between two frames still gets the next frame
            // displayed (closest match).
            const chunk = new EncodedVideoChunk({
              type: s.is_sync ? 'key' : 'delta',
              timestamp: s.cts,
              duration: s.duration,
              data: s.data,
            })
            st.decoder.decode(chunk)
            break
          }
          const chunk = new EncodedVideoChunk({
            type: i === startIdx || s.is_sync ? 'key' : 'delta',
            timestamp: s.cts,
            duration: s.duration,
            data: s.data,
          })
          st.decoder.decode(chunk)
        }
        // Force the decoder to emit any queued frames so the canvas paints.
        await st.decoder.flush().catch(() => {})
        st.lastSeekKey = startIdx
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
  // mp4box's DataStream constants are class-statics; BIG_ENDIAN === 1.
  // @ts-expect-error — DataStream is mp4box's writer
  const stream = new DataStream(undefined, 0, DataStream.BIG_ENDIAN) as {
    adjustUint32: (n: number, v: number) => void; position: number;
    getEndPosition: () => number; buffer: ArrayBuffer
  }
  // Write box → stream then trim the leading 8-byte size+type box header.
  // (`description` for AVC/HEVC must be the avcC/hvcC payload, no header.)
  ;(box as { write: (s: typeof stream) => void }).write(stream)
  return new Uint8Array(stream.buffer, 8)
}
