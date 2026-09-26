// laneA against a FAKE SourceBuffer (spec §13 "laneA.test.ts"): the fake
// parses every fragment the real Fmp4Writer produces (tfdt → k, trun sizes →
// samples) and keeps what each output frame holds, so the assertions are on
// what MSE would buffer: the contiguous window, fillers over gaps, init
// re-appends on a size-class change, the quota path, append order and the
// (k + 0.5)/R seek targets. The WK suite then proves the same code in WebKit.
import { describe, expect, it } from 'vitest'
import type { FrameSample, TrackFormat } from './fmp4Writer'
import {
  FILLER, GAP, LaneA, NONE, type LaneMedia, type LaneProgram, type MediaSourceLike, type SourceBufferLike,
  type TimeRangesLike, type VideoLike,
} from './laneA'
import { STANDARD_RATES, ticksPerFrameExact, type Rational } from '../timeline/timebase'

// ------------------------------------------------------------------ fakes

const enc = (s: string) => Uint8Array.from(s, (c) => c.charCodeAt(0))

function format(key: string, w = 1280, h = 720): TrackFormat {
  // The fake recognises an init by these marker bytes inside the sample entry.
  return { sampleEntry: enc(`FMT:${key};`), width: w, height: h, avcC: enc(key), codec: 'avc1.641029', initKey: key }
}

function sampleBytes(id: number, size = 16): Uint8Array {
  const b = new Uint8Array(size)
  new DataView(b.buffer).setUint32(0, 0x534d504c) // 'SMPL'
  new DataView(b.buffer).setUint32(4, id)
  return b
}

class Ranges implements TimeRangesLike {
  private readonly r: Array<[number, number]>
  constructor(r: Array<[number, number]>) { this.r = r }
  get length() { return this.r.length }
  start(i: number) { return this.r[i][0] }
  end(i: number) { return this.r[i][1] }
}

interface Held { id: number; initKey: string | null; bytes: number }

class FakeSourceBuffer extends EventTarget implements SourceBufferLike {
  mode = 'sequence'
  updating = false
  readonly frames = new Map<number, Held>()
  readonly log: string[] = []
  currentInit: string | null = null
  quotaBytes = Infinity
  bytes = 0
  maxRanges = 0
  failNext = 0
  private readonly T: number
  private readonly R: Rational
  constructor(T: number, R: Rational) { super(); this.T = T; this.R = R }

  private finish(fn: () => void): void {
    this.updating = true
    setTimeout(() => {
      if (this.failNext > 0) {
        this.failNext--
        this.updating = false
        this.dispatchEvent(new Event('error'))
        this.dispatchEvent(new Event('updateend'))
        return
      }
      fn()
      this.updating = false
      this.maxRanges = Math.max(this.maxRanges, this.buffered.length)
      this.dispatchEvent(new Event('updateend'))
    }, 0)
  }

  appendBuffer(data: Uint8Array): void {
    if (this.updating) throw new DOMException('busy', 'InvalidStateError')
    if (this.bytes + data.byteLength > this.quotaBytes) throw new DOMException('full', 'QuotaExceededError')
    const type = String.fromCharCode(...data.subarray(4, 8))
    this.finish(() => {
      if (type === 'ftyp') {
        const text = String.fromCharCode(...data)
        const m = /FMT:([^;]*);/.exec(text)
        this.currentInit = m ? m[1] : '?'
        this.log.push(`init:${this.currentInit}`)
        return
      }
      const dv = new DataView(data.buffer, data.byteOffset, data.byteLength)
      const base = dv.getUint32(60) * 2 ** 32 + dv.getUint32(64)
      const n = dv.getUint32(80)
      const k0 = base / this.T
      let at = dv.getUint32(0) + 8 // moof size + mdat header
      for (let i = 0; i < n; i++) {
        const size = dv.getUint32(88 + i * 12 + 4)
        const id = dv.getUint32(at + 4)
        const old = this.frames.get(k0 + i)
        if (old) this.bytes -= old.bytes
        this.frames.set(k0 + i, { id, initKey: this.currentInit, bytes: size })
        this.bytes += size
        at += size
      }
      this.log.push(`media:${k0}-${k0 + n}`)
    })
  }

  remove(start: number, end: number): void {
    if (this.updating) throw new DOMException('busy', 'InvalidStateError')
    this.finish(() => {
      for (const [k, h] of [...this.frames]) {
        const t = (k * this.R.den) / this.R.num
        if (t >= start && t < end) {
          this.frames.delete(k)
          this.bytes -= h.bytes
        }
      }
      this.log.push(`remove:${start.toFixed(3)}-${end.toFixed(3)}`)
    })
  }

  get buffered(): Ranges {
    const ks = [...this.frames.keys()].sort((a, b) => a - b)
    const out: Array<[number, number]> = []
    for (const k of ks) {
      const t0 = (k * this.R.den) / this.R.num
      const t1 = ((k + 1) * this.R.den) / this.R.num
      const last = out[out.length - 1]
      if (last && Math.abs(last[1] - t0) < 1e-9) last[1] = t1
      else out.push([t0, t1])
    }
    return new Ranges(out)
  }

  /** [kA, kB) of the single range (asserts contiguity). */
  get interval(): [number, number] {
    const ks = [...this.frames.keys()].sort((a, b) => a - b)
    if (!ks.length) return [0, 0]
    expect(this.buffered.length).toBe(1)
    return [ks[0], ks[ks.length - 1] + 1]
  }
}

/** WebKit's coded-frame-group behaviour (measured in WK, 1c): a media
 *  segment that jumps FORWARD past the previous one's end inside one coded
 *  frame group removes every frame in between; abort() (resetParserState)
 *  starts a new group, and media is dropped until the next init segment. */
class WebKitLikeSourceBuffer extends FakeSourceBuffer {
  groupEnd: number | null = null
  needInit = false
  aborts = 0
  abort(): void {
    if (this.updating) throw new DOMException('busy', 'InvalidStateError')
    this.groupEnd = null
    this.needInit = true
    this.aborts++
  }
  appendBuffer(data: Uint8Array): void {
    const type = String.fromCharCode(...data.subarray(4, 8))
    if (type === 'ftyp') {
      this.needInit = false
      super.appendBuffer(data)
      return
    }
    const dv = new DataView(data.buffer, data.byteOffset, data.byteLength)
    const k0 = (dv.getUint32(60) * 2 ** 32 + dv.getUint32(64)) / this.Tpub
    const n = dv.getUint32(80)
    if (this.needInit) {
      this.log.push(`dropped:${k0}-${k0 + n}`)
      super.appendBuffer(new Uint8Array(0))
      return
    }
    if (this.groupEnd !== null && k0 > this.groupEnd) {
      for (let k = this.groupEnd; k < k0; k++) {
        const old = this.frames.get(k)
        if (old) { this.frames.delete(k); this.bytes -= old.bytes }
      }
    }
    this.groupEnd = k0 + n
    super.appendBuffer(data)
  }
  get Tpub(): number { return (this as unknown as { T: number }).T }
}

class FakeMediaSource extends EventTarget implements MediaSourceLike {
  readyState = 'closed'
  private d = NaN
  sb: FakeSourceBuffer | null = null
  endOfStreamCalls = 0
  private readonly T: number
  private readonly R: Rational
  constructor(T: number, R: Rational) { super(); this.T = T; this.R = R }
  get duration() { return this.d }
  set duration(v: number) {
    if (this.sb?.updating) throw new DOMException('busy', 'InvalidStateError')
    const b = this.sb?.buffered
    if (b && b.length && v < b.end(b.length - 1) - 1e-9) throw new DOMException('below buffered', 'InvalidStateError')
    this.d = v
  }
  webkit = false
  addSourceBuffer(type: string): SourceBufferLike {
    expect(type).toMatch(/^video\/mp4; codecs="avc1\./)
    this.sb = this.webkit ? new WebKitLikeSourceBuffer(this.T, this.R) : new FakeSourceBuffer(this.T, this.R)
    return this.sb
  }
  endOfStream(): void { this.endOfStreamCalls++ }
}

class FakeVideo extends EventTarget implements VideoLike {
  src = ''
  currentTime = 0
  paused = true
  muted = true
  async play() { this.paused = false }
  pause() { this.paused = true }
  removeAttribute() { this.src = '' }
  load() {}
}

function fakeMedia(R: Rational, webkit = false): { media: LaneMedia; ms: () => FakeMediaSource; video: FakeVideo } {
  const T = ticksPerFrameExact(R)!
  const video = new FakeVideo()
  let ms: FakeMediaSource | null = null
  const media: LaneMedia = {
    video,
    createMediaSource: () => { ms = new FakeMediaSource(T, R); ms.webkit = webkit; return ms },
    createObjectURL: () => {
      setTimeout(() => { ms!.readyState = 'open'; ms!.dispatchEvent(new Event('sourceopen')) }, 0)
      return 'blob:fake'
    },
    revokeObjectURL: () => undefined,
  }
  return { media, ms: () => ms!, video }
}

// --------------------------------------------------------------- programs

const FMT: Record<string, TrackFormat> = { A: format('A'), C: format('C', 720, 1280) }
/** content id → (class, bytes): sources 1..4 are class A, 5..8 class C. */
const idOf = (src: number, frame: number) => src * 2 ** 16 + frame
const classOf = (id: number) => (Math.floor(id / 2 ** 16) >= 5 ? 'C' : 'A')

class TestProgram implements LaneProgram {
  readonly want: Float64Array
  readonly loaded = new Set<number>()
  readonly requests: Array<{ id: number; urgent: boolean }> = []
  loadAll = true
  constructor(want: number[]) { this.want = Float64Array.from(want) }
  get total() { return this.want.length }
  sample(id: number): FrameSample | null {
    if (!this.loadAll && !this.loaded.has(id)) return null
    return { format: FMT[classOf(id)], bytes: sampleBytes(id, 100) }
  }
  request(id: number, _priority: number, urgent: boolean): void { this.requests.push({ id, urgent }) }
}

/** `n` frames of source `src` from frame `f0` (a run of a clip). */
const clip = (src: number, f0: number, n: number) => Array.from({ length: n }, (_, i) => idOf(src, f0 + i))
const gap = (n: number) => new Array<number>(n).fill(GAP)

async function settle(lane: LaneA, rounds = 20000): Promise<void> {
  for (let i = 0; i < rounds; i++) {
    await new Promise((r) => setTimeout(r, 0))
    const sb = lane.sourceBuffer
    if (lane.plan() === null && !(sb && sb.updating)) {
      await new Promise((r) => setTimeout(r, 0))
      if (lane.plan() === null) return
    }
  }
  throw new Error('laneA did not settle')
}

async function openLane(R: Rational, program: TestProgram, playhead = 0, opts: Partial<ConstructorParameters<typeof LaneA>[0]> = {},
  webkit = false) {
  const f = fakeMedia(R, webkit)
  const events: string[] = []
  const lane = new LaneA({
    rate: R, media: f.media,
    events: {
      appended: (a, b) => events.push(`appended:${a}-${b}`),
      removed: (a, b) => events.push(`removed:${a}-${b}`),
      degraded: (r) => events.push(`degraded:${r}`),
      fatal: (r) => events.push(`fatal:${r}`),
    },
    ...opts,
  })
  lane.setProgram(program)
  lane.setPlayhead(playhead, false)
  await lane.open()
  await settle(lane)
  return { lane, f, events, sb: () => f.ms().sb! }
}

const R30: Rational = { num: 30, den: 1 }

// ------------------------------------------------------------------- tests

describe('contiguous window invariant', () => {
  it('buffers exactly playhead −10 s … +30 s as ONE range, every frame its wanted content', async () => {
    const program = new TestProgram(clip(1, 0, 3000))
    const { lane, sb } = await openLane(R30, program, 1500)
    expect(sb().interval).toEqual([1200, 2400])
    expect(lane.buffered).toEqual([1200, 2400])
    for (let k = 1200; k < 2400; k++) expect(sb().frames.get(k)!.id).toBe(program.want[k])
    expect(sb().maxRanges).toBe(1) // never a hole, at any updateend
  })

  it('slides with the playhead and resets on a far seek, staying contiguous', async () => {
    const program = new TestProgram(clip(1, 0, 3000))
    const { lane, sb } = await openLane(R30, program, 1500)
    lane.setPlayhead(1700, true)
    await settle(lane)
    const [a, b] = sb().interval
    expect(b).toBe(2600)
    expect(a).toBeGreaterThanOrEqual(1700 - 300 - 60)
    expect(a).toBeLessThanOrEqual(1400)
    lane.setPlayhead(100, false)
    await settle(lane)
    expect(sb().interval).toEqual([0, 1000])
    expect(lane.stats.resets).toBe(1)
    expect(sb().maxRanges).toBe(1)
  })

  it('clamps to the program and follows a shorter program with remove() + duration, never endOfStream', async () => {
    const program = new TestProgram(clip(1, 0, 600))
    const { lane, sb, f } = await openLane(R30, program, 100)
    expect(sb().interval).toEqual([0, 600])
    expect(f.ms().duration).toBeCloseTo(20, 9)
    lane.setProgram(new TestProgram(clip(1, 0, 300)))
    await settle(lane)
    expect(sb().interval).toEqual([0, 300])
    expect(f.ms().duration).toBeCloseTo(10, 9)
    expect(f.ms().endOfStreamCalls).toBe(0)
  })
})

describe('fillers over timeline gaps', () => {
  it('writes the previous sample over a gap: contiguous, ready, not a hole', async () => {
    const want = [...clip(1, 0, 60), ...gap(30), ...clip(2, 0, 60)]
    const { lane, sb } = await openLane(R30, new TestProgram(want), 0)
    expect(sb().interval).toEqual([0, 150])
    for (let k = 60; k < 90; k++) {
      expect(lane.isReady(k)).toBe(true)
      expect(lane.contentAt(k)).toBe(FILLER)
    }
    // the filler's bytes are the last sample written before the gap
    expect(sb().frames.get(60)!.id).toBe(idOf(1, 59))
    expect(lane.stats.fillerFrames).toBe(30)
  })

  it('a leading gap borrows the first clip frame', async () => {
    const want = [...gap(40), ...clip(3, 10, 50)]
    const { sb } = await openLane(R30, new TestProgram(want), 0)
    expect(sb().interval).toEqual([0, 90])
    expect(sb().frames.get(0)!.id).toBe(idOf(3, 10))
  })

  it('an all-gap window waits for any loaded frame instead of throwing', async () => {
    const program = new TestProgram([...gap(900), ...clip(1, 0, 30)])
    program.loadAll = false
    const { lane, sb } = await openLane(R30, program, 0)
    expect(sb()).toBeNull()
    program.loaded.add(idOf(1, 0))
    lane.poke()
    await settle(lane)
    expect(sb().interval[0]).toBe(0)
    expect(lane.isReady(0)).toBe(true)
  })
})

describe('init segments per size class', () => {
  it('appends an init before any fragment whose class differs, and every frame decodes with its own class', async () => {
    const want = [...clip(1, 0, 30), ...clip(5, 0, 30), ...clip(2, 0, 30), ...clip(6, 0, 30)]
    const { sb } = await openLane(R30, new TestProgram(want), 0)
    for (let k = 0; k < 120; k++) expect(sb().frames.get(k)!.initKey).toBe(classOf(want[k]))
    const inits = sb().log.filter((l) => l.startsWith('init:')).map((l) => l.slice(5))
    expect(inits).toEqual(['A', 'C', 'A', 'C'])
  })

  it('re-appends the init when an overwrite changes class mid-window', async () => {
    const program = new TestProgram(clip(1, 0, 300))
    const { lane, sb } = await openLane(R30, program, 100)
    const before = sb().log.length
    // class C at the playhead, and a different class-A source further on
    const edited = new TestProgram([...clip(1, 0, 100), ...clip(5, 0, 20), ...clip(1, 120, 10), ...clip(3, 0, 10), ...clip(1, 140, 160)])
    lane.setProgram(edited)
    await settle(lane)
    const log = sb().log.slice(before)
    expect(log[0]).toBe('init:C')
    expect(log[1]).toBe('media:100-105')
    // the class switch back to A is announced by an init before frame 130's fragment
    const at130 = log.indexOf('media:130-140')
    expect(at130).toBeGreaterThan(0)
    expect(log[at130 - 1]).toBe('init:A')
    for (let k = 0; k < 300; k++) expect(sb().frames.get(k)!.initKey).toBe(classOf(edited.want[k]))
  })
})

describe('append ordering', () => {
  it('paused: the frames at the playhead first (≤ 5), then forward, then backward', async () => {
    const program = new TestProgram(clip(1, 0, 3000))
    const { sb } = await openLane(R30, program, 1000)
    const media = sb().log.filter((l) => l.startsWith('media:')).map((l) => l.slice(6).split('-').map(Number))
    expect(media[0]).toEqual([1000, 1005])
    // everything forward of the playhead lands before anything behind it
    const firstBehind = media.findIndex(([a]) => a < 1000)
    const lastForward = media.map(([a]) => a >= 1000).lastIndexOf(true)
    expect(firstBehind).toBeGreaterThan(lastForward)
  })

  it('paused edit: the playhead frames are re-appended first, then outward by distance', async () => {
    const program = new TestProgram(clip(1, 0, 900))
    const { lane, sb } = await openLane(R30, program, 400)
    const before = sb().log.length
    lane.setProgram(new TestProgram([...clip(1, 0, 300), ...clip(2, 0, 200), ...clip(1, 500, 400)]))
    await settle(lane)
    const media = sb().log.slice(before).filter((l) => l.startsWith('media:')).map((l) => l.slice(6).split('-').map(Number))
    expect(media[0]).toEqual([400, 405])
    expect(media[1][0]).toBe(405)
    for (let k = 300; k < 500; k++) expect(sb().frames.get(k)!.id).toBe(idOf(2, k - 300))
  })

  it('playing edit: nothing nearer than presentedK + 150 ms is rewritten first', async () => {
    const program = new TestProgram(clip(1, 0, 900))
    const { lane, sb } = await openLane(R30, program, 400)
    lane.setPlayhead(400, true)
    await settle(lane)
    const before = sb().log.length
    lane.setProgram(new TestProgram([...clip(1, 0, 402), ...clip(2, 0, 100), ...clip(1, 502, 398)]))
    await settle(lane)
    const media = sb().log.slice(before).filter((l) => l.startsWith('media:')).map((l) => l.slice(6).split('-').map(Number))
    expect(media[0][0]).toBe(400 + 5) // ⌈0.15 · 30⌉ = 5
    // the frames it skipped are still stale (the compositor will hold, not show them)
    expect(lane.isReady(402)).toBe(false)
    expect(lane.isReady(405)).toBe(true)
  })
})

describe('coded frame groups (WebKit)', () => {
  it('a batch that does not continue the previous one starts a new group: no frame between them is lost', async () => {
    const base = new TestProgram(clip(1, 0, 900))
    const { lane, sb } = await openLane(R30, base, 400, {}, true)
    expect(sb().interval).toEqual([100, 900])
    // an edit that changes scattered frames around the playhead: the paused
    // order appends them as separate, non-adjacent batches
    const want = clip(1, 0, 900)
    for (const k of [398, 399, 404, 405, 420, 421, 377]) want[k] = idOf(2, k)
    lane.setProgram(new TestProgram(want))
    await settle(lane)
    const wk = sb() as unknown as WebKitLikeSourceBuffer
    expect(wk.aborts).toBeGreaterThan(0)
    expect(wk.log.some((l) => l.startsWith('dropped:'))).toBe(false)   // the init is re-sent after every abort
    expect(sb().interval).toEqual([100, 900])
    for (let k = 100; k < 900; k++) expect(sb().frames.get(k)?.id).toBe(want[k])
    expect(lane.stats.jumps).toBeGreaterThan(0)
  })
})

describe('page hidden (§3.5)', () => {
  it('suspend() stops appends until the page is back, then the window fills', async () => {
    const program = new TestProgram(clip(1, 0, 900))
    const { lane, sb } = await openLane(R30, program, 400)
    lane.suspend(true)
    const before = sb().log.length
    const edited = clip(1, 0, 900)
    for (let k = 380; k < 420; k++) edited[k] = idOf(3, k)
    lane.setProgram(new TestProgram(edited))
    await new Promise((r) => setTimeout(r, 30))
    expect(sb().log.length).toBe(before)                // nothing appended or removed while hidden
    lane.suspend(false)
    await settle(lane)
    for (let k = 380; k < 420; k++) expect(sb().frames.get(k)!.id).toBe(idOf(3, k))
  })
})

describe('stale frames without bytes', () => {
  it('are cut out of the interval (so playback stalls, never shows old content) and come back when loaded', async () => {
    const base = new TestProgram(clip(1, 0, 900))
    const { lane, sb } = await openLane(R30, base, 300)
    const edited = new TestProgram([...clip(1, 0, 310), ...clip(7, 0, 10), ...clip(1, 320, 580)])
    edited.loadAll = false
    for (let k = 0; k < 900; k++) if (k < 310 || k >= 320) edited.loaded.add(edited.want[k])
    lane.setProgram(edited)
    await settle(lane)
    expect(sb().interval[1]).toBe(310)
    expect(lane.stats.truncations).toBeGreaterThan(0)
    expect(edited.requests.some((r) => r.id === idOf(7, 0))).toBe(true)
    for (let i = 0; i < 10; i++) edited.loaded.add(idOf(7, i))
    lane.poke()
    await settle(lane)
    expect(sb().interval).toEqual([0, 900])
    for (let k = 310; k < 320; k++) expect(sb().frames.get(k)!.id).toBe(idOf(7, k - 310))
  })

  it('a paused playhead on a missing frame asks for it URGENTLY (Range read)', async () => {
    const program = new TestProgram(clip(1, 0, 300))
    program.loadAll = false
    const { lane } = await openLane(R30, program, 120)
    expect(program.requests.find((r) => r.id === idOf(1, 120))?.urgent).toBe(true)
    expect(lane.isReady(120)).toBe(false)
  })
})

describe('quota', () => {
  it('evicts behind the playhead to −2 s, halves the look-ahead, retries, then reports degraded', async () => {
    const program = new TestProgram(clip(1, 0, 6000))
    const f = fakeMedia(R30)
    const events: string[] = []
    const lane = new LaneA({ rate: R30, media: f.media, events: { degraded: (r) => events.push(r) } })
    lane.setProgram(program)
    lane.setPlayhead(3000, false)
    await lane.open()
    // let a first second land, then cap the buffer at 500 frames' worth
    for (let i = 0; i < 200 && !f.ms().sb; i++) await new Promise((r) => setTimeout(r, 0))
    const sbf = () => f.ms().sb!
    sbf().quotaBytes = 500 * 100
    await settle(lane)
    expect(lane.stats.quotaHits).toBeGreaterThan(0)
    expect(lane.lookAheadFrames).toBeLessThan(900)
    expect(lane.lookAheadFrames).toBeGreaterThanOrEqual(60)
    const [a] = sbf().interval
    expect(a).toBeGreaterThanOrEqual(3000 - 60)
    expect(sbf().maxRanges).toBe(1)
    expect(lane.isReady(3000)).toBe(true)
  })

  it('a second consecutive QuotaExceededError raises degraded instead of looping', async () => {
    const program = new TestProgram(clip(1, 0, 3000))
    const f = fakeMedia(R30)
    const events: string[] = []
    const lane = new LaneA({ rate: R30, media: f.media, events: { degraded: (r) => events.push(r) } })
    lane.setProgram(program)
    lane.setPlayhead(0, false)
    await lane.open()
    for (let i = 0; i < 200 && !f.ms().sb; i++) await new Promise((r) => setTimeout(r, 0))
    f.ms().sb!.quotaBytes = 0 // nothing more fits
    await new Promise((r) => setTimeout(r, 60))
    expect(events).toContain('quota')
    lane.destroy()
  })
})

describe('errors', () => {
  /** Runs a lane until its SourceBuffer exists, then fails the next `n`
   *  operations (each one 'error' event, as WebKit fires them). */
  async function failing(n: number) {
    const program = new TestProgram(clip(1, 0, 300))
    const f = fakeMedia(R30)
    const events: string[] = []
    const lane = new LaneA({ rate: R30, media: f.media, events: { fatal: (r) => events.push(r) } })
    lane.setProgram(program)
    await lane.open()
    for (let i = 0; i < 200 && !f.ms().sb; i++) await new Promise((r) => setTimeout(r, 0))
    const errorsBefore = lane.stats.errors
    f.ms().sb!.failNext = n
    await new Promise((r) => setTimeout(r, 300))
    lane.destroy()
    return { events, errors: lane.stats.errors - errorsBefore }
  }

  it('each SourceBuffer error event counts once', async () => {
    const { errors } = await failing(2)
    expect(errors).toBe(2)
  })

  it('two SourceBuffer errors within 60 s are NOT fatal', async () => {
    const { events } = await failing(2)
    expect(events).not.toContain('sourcebuffer-errors')
  })

  it('three SourceBuffer errors within 60 s are fatal (server-mode fallback)', async () => {
    const { events, errors } = await failing(3)
    expect(errors).toBe(3)
    expect(events).toContain('sourcebuffer-errors')
  })
})

describe('codec string', () => {
  it('is never guessed: with no codec from parseInitSegment the append waits', async () => {
    const noCodec: TrackFormat = { ...format('A'), codec: undefined as unknown as string }
    const program = new TestProgram(clip(1, 0, 60))
    program.sample = (id: number) => ({ format: noCodec, bytes: sampleBytes(id, 100) })
    const f = fakeMedia(R30)
    const types: string[] = []
    const lane = new LaneA({ rate: R30, media: f.media })
    lane.setProgram(program)
    lane.setPlayhead(0, false)
    await lane.open()
    const ms = f.ms()
    const orig = ms.addSourceBuffer.bind(ms)
    ms.addSourceBuffer = (type: string) => { types.push(type); return orig(type) }
    lane.poke()
    await new Promise((r) => setTimeout(r, 50))
    expect(types).toEqual([])
    expect(lane.stats.noCodec).toBeGreaterThan(0)
    lane.destroy()
  })
})

describe('seek targets', () => {
  it('are always mid-frame (k + 0.5)/R and map back to k, at every standard rate', () => {
    for (const R of STANDARD_RATES) {
      const lane = new LaneA({ rate: R, media: fakeMedia(R).media })
      for (const k of [0, 1, 29, 30, 1799, 21599, 107999]) {
        const t = lane.seekTime(k)
        expect(t).toBeCloseTo(((k + 0.5) * R.den) / R.num, 12)
        expect(lane.frameAt(t - (0.5 * R.den) / R.num)).toBe(k)
        // never an exact frame boundary
        expect(Math.abs((t * R.num) / R.den - Math.round((t * R.num) / R.den))).toBeGreaterThan(0.49)
      }
    }
  })
})

describe('bookkeeping', () => {
  it('NONE/FILLER/GAP are distinct sentinels below every content id', () => {
    expect(new Set([NONE, FILLER, GAP]).size).toBe(3)
    expect(Math.max(NONE, FILLER, GAP)).toBeLessThan(0)
  })
})
