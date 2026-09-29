// A PORT OF FFMPEG'S `alimiter` (libavfilter/af_alimiter.c, FFmpeg 8.1,
// `asc=0`) for the preview's master limiter (P2 limiter tail, 0.8.0 final
// QA). The server's preview limits the mix with `alimiter=limit=0.97:
// latency=1` and a loudness stage's `alimiter=limit=0.891251:level=0:
// latency=1` (audio_mix.py); the browser used a DynamicsCompressorNode, whose
// attack and release are not alimiter's — every stretch over the ceiling
// and ~90 ms after it differed (up to 0.75 dB per 50 ms block) and was APPROX
// ("Limiter on loud sound"). This is the same gain computer sample for
// sample, in doubles like the C (a float input is exact in a double; the
// output is rounded to float once, as ffmpeg's dbl→flt conversion does), so
// over the ceiling the preview is the render's sound.
//
// The algorithm, as the C runs it per stereo frame: the frame goes into a
// ring of `attack` (5 ms = 240 frames); the frame leaving the ring is played
// times the gain `att`, which moves by `delta` per frame. A frame whose peak
// tops `limit` sets a descending `delta` that reaches `limit/peak` exactly
// when that frame leaves the ring (later peaks queue in `nextpos` /
// `nextdelta`); on reaching it the gain restarts from `limit/peak` and rises
// back to 1 linearly over `release` (50 ms). Then the output is clipped to
// ±limit and multiplied by `level` (1/limit when auto-level is on).
//
// NO outer references and NO class fields: the worklet (limiterWorklet.ts)
// ships this class as its own source text (`ALimiterCore.toString()`).

/** Frames alimiter delays its input by (the ring of `attack`, less one):
 *  239 at 48 kHz and 5 ms. `latency=1` trims exactly these. */
export function alimiterRingLatency(sampleRate: number, attackMs: number): number {
  let size = Math.trunc(sampleRate * (attackMs / 1000) * 2)
  size -= size % 2
  return size / 2 - 1
}

/** The ring's latency plus `pad` extra frames: the port's output frame k is
 *  input frame k − (ringFrames − 1) − pad. */
export class ALimiterCore {
  limit: number
  level: number
  private release: number
  private rate: number
  private bufferSize: number
  private buffer: Float64Array
  private nextpos: Int32Array
  private nextdelta: Float64Array
  private pos: number
  private att: number
  private delta: number
  private nextiter: number
  private nextlen: number
  private pad: Float64Array
  private padPos: number
  /** Frames from an input frame to its output frame. */
  latency: number

  /** `limit` linear, `autoLevel` = alimiter's `level` option, `attackMs` /
   *  `releaseMs` its options, `padFrames` extra delay after the ring. */
  constructor(limit: number, autoLevel: boolean, attackMs: number, releaseMs: number, sampleRate: number, padFrames: number) {
    const channels = 2
    this.limit = limit
    this.level = autoLevel ? 1 / limit : 1
    this.release = releaseMs / 1000
    this.rate = sampleRate
    // config_input: int truncation of a double, as the C assigns it
    let size = Math.trunc(sampleRate * (attackMs / 1000) * channels)
    size -= size % channels
    if (size <= 0) throw new Error('alimiter: attack is too small')
    this.bufferSize = size
    const obuf = Math.trunc(sampleRate * channels * 100 / 1000 + channels)
    this.buffer = new Float64Array(obuf)
    this.nextpos = new Int32Array(obuf).fill(-1)
    this.nextdelta = new Float64Array(obuf)
    this.pos = 0
    this.att = 1
    this.delta = 0
    this.nextiter = 0
    this.nextlen = 0
    const p = Math.max(0, Math.trunc(padFrames))
    this.pad = new Float64Array(2 * p)
    this.padPos = 0
    this.latency = size / channels - 1 + p
  }

  /** Set the ceiling (and, with auto-level, the level) from now on. */
  setLimit(limit: number, autoLevel: boolean): void {
    this.limit = limit
    this.level = autoLevel ? 1 / limit : 1
  }

  /** Run `n` stereo frames: inL/inR → outL/outR (may alias). */
  process(inL: ArrayLike<number>, inR: ArrayLike<number>, outL: { [i: number]: number }, outR: { [i: number]: number }, n: number): void {
    const channels = 2
    const size = this.bufferSize
    const buffer = this.buffer
    const nextpos = this.nextpos
    const nextdelta = this.nextdelta
    const limit = this.limit
    const release = this.release
    const rate = this.rate
    const level = this.level
    const pad = this.pad
    const padLen = pad.length
    for (let k = 0; k < n; k++) {
      const xl = +inL[k], xr = +inR[k]
      const pos = this.pos
      buffer[pos] = xl
      buffer[pos + 1] = xr
      let peak = Math.max(0, Math.abs(xl))
      peak = Math.max(peak, Math.abs(xr))
      if (peak > limit) {
        const patt = Math.min(limit / peak, 1)
        const rdelta = (1.0 - patt) / (rate * release)
        const delta = (limit / peak - this.att) / size * channels
        let found = false
        if (delta < this.delta) {
          this.delta = delta
          nextpos[0] = pos
          nextpos[1] = -1
          nextdelta[0] = rdelta
          this.nextlen = 1
          this.nextiter = 0
        } else {
          let i = this.nextiter
          for (; i < this.nextiter + this.nextlen; i++) {
            const j = i % size
            let ppeak = 0
            if (nextpos[j] >= 0) {
              ppeak = Math.max(ppeak, Math.abs(buffer[nextpos[j]]))
              ppeak = Math.max(ppeak, Math.abs(buffer[nextpos[j] + 1]))
            }
            // C: int % int, then int / int (truncating), then a double divide
            const frames = Math.trunc(((size - nextpos[j] + pos) % size) / channels)
            const pdelta = (limit / peak - limit / ppeak) / frames
            if (pdelta < nextdelta[j]) {
              nextdelta[j] = pdelta
              found = true
              break
            }
          }
          if (found) {
            this.nextlen = i - this.nextiter + 1
            nextpos[(this.nextiter + this.nextlen) % size] = pos
            nextdelta[(this.nextiter + this.nextlen) % size] = rdelta
            nextpos[(this.nextiter + this.nextlen + 1) % size] = -1
            this.nextlen++
          }
        }
      }

      const b = (pos + channels) % size
      const bl = buffer[b], br = buffer[b + 1]
      peak = Math.max(0, Math.abs(bl))
      peak = Math.max(peak, Math.abs(br))
      this.att += this.delta
      let yl = bl * this.att
      let yr = br * this.att
      if (b === nextpos[this.nextiter]) {
        this.delta = nextdelta[this.nextiter]
        this.att = limit / peak
        this.nextlen -= 1
        nextpos[this.nextiter] = -1
        this.nextiter = (this.nextiter + 1) % size
      }
      if (this.att > 1.0) {
        this.att = 1.0
        this.delta = 0.0
        this.nextiter = 0
        this.nextlen = 0
        nextpos[0] = -1
      }
      if (this.att <= 0.0) {
        this.att = 0.0000000000001
        this.delta = (1.0 - this.att) / (rate * release)
      }
      if (this.att !== 1.0 && (1.0 - this.att) < 0.0000000000001) this.att = 1.0
      if (this.delta !== 0.0 && Math.abs(this.delta) < 0.00000000000001) this.delta = 0.0
      // av_clipd, then level (level_out = 1)
      yl = (yl > limit ? limit : yl < -limit ? -limit : yl) * level
      yr = (yr > limit ? limit : yr < -limit ? -limit : yr) * level
      this.pos = (pos + channels) % size
      if (padLen) {
        const q = this.padPos
        const ol = pad[q], or = pad[q + 1]
        pad[q] = yl
        pad[q + 1] = yr
        this.padPos = (q + 2) % padLen
        yl = ol
        yr = or
      }
      outL[k] = yl
      outR[k] = yr
    }
  }
}
