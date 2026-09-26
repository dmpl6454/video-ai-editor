// fMP4 writer for the instant-preview engine's laneA (INSTANT_PREVIEW_SPEC
// §3.1-3.2, §9.1). The client writes its own fragments ON THE PROJECT FRAME
// GRID: output frame k is one sample with
//
//     tfdt = k · T   in timescale 240000,   T = 240000 · R.den / R.num
//
// which is an integer for every STANDARD_RATES entry (10010, 10000, 9600,
// 8008, 8000, 5000, 4800, 4004, 4000). Each sample's bytes are one all-intra
// proxy frame, so every sample is a sync sample and MSE never has a decode
// dependency to break: duplicates, reverse and speed are just which bytes sit
// at which k.
//
// Pure byte work, no DOM: laneA feeds the output to a SourceBuffer, vitest
// parses it with mp4box, and the WK harness plays it in real WebKit.

import { MSE_TIMESCALE, ticksPerFrameExact } from '../timeline/timebase'

/** The media timescale of every init and fragment this module writes (the
 *  one definition lives in timeline/timebase). */
export { MSE_TIMESCALE }

/** A frame rate as an exact rational (the `edl/timebase.rate_of` shape). */
export interface Rate {
  readonly num: number
  readonly den: number
}

/** Ticks per frame at `rate` in {@link MSE_TIMESCALE}, or null when it is not
 *  an integer — the R1 refusal: such a timeline stays in server mode. */
export const ticksPerFrame: (rate: Rate) => number | null = ticksPerFrameExact

/** `tfdt` of output frame k: exact while k·T < 2^53 (≈ 70 000 years at 60 fps). */
export function frameTicks(k: number, ticks: number): number {
  if (!Number.isSafeInteger(k) || k < 0) throw new Fmp4WriterError(`bad frame index ${k}`)
  return k * ticks
}

export class Fmp4WriterError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'Fmp4WriterError'
  }
}

// ---------------------------------------------------------------- box reading

interface BoxRef {
  readonly type: string
  readonly start: number // offset of the size field
  readonly body: number // offset of the first payload byte
  readonly end: number
}

function fourcc(buf: Uint8Array, at: number): string {
  return String.fromCharCode(buf[at], buf[at + 1], buf[at + 2], buf[at + 3])
}

function* children(buf: Uint8Array, from: number, to: number): Generator<BoxRef> {
  const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength)
  let at = from
  while (at + 8 <= to) {
    let size = dv.getUint32(at)
    let header = 8
    if (size === 1) {
      if (at + 16 > to) throw new Fmp4WriterError('truncated largesize box')
      size = Number(dv.getBigUint64(at + 8))
      header = 16
    } else if (size === 0) {
      size = to - at
    }
    if (size < header || at + size > to) throw new Fmp4WriterError(`bad box size at ${at}`)
    yield { type: fourcc(buf, at + 4), start: at, body: at + header, end: at + size }
    at += size
  }
}

function child(buf: Uint8Array, parent: { body: number; end: number }, type: string, skip = 0): BoxRef | null {
  for (const b of children(buf, parent.body + skip, parent.end)) if (b.type === type) return b
  return null
}

function need<T>(v: T | null, what: string): T {
  if (v === null) throw new Fmp4WriterError(`init segment has no ${what}`)
  return v
}

/** What the writer needs from a proxy's `init.mp4`: its video sample entry
 *  (the `avc1` box, carried VERBATIM so avcC, colr and pasp survive), the
 *  display size, and the size-class key laneA switches inits on. */
export interface TrackFormat {
  readonly sampleEntry: Uint8Array
  readonly width: number
  readonly height: number
  readonly avcC: Uint8Array
  /** `avc1.PPCCLL`, for `SourceBuffer` / `isTypeSupported`. */
  readonly codec: string
  /** One size class shares one avcC (§3.2): (W×H, avcC). */
  readonly initKey: string
}

const hex = (bytes: Uint8Array) => Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')

/** Parses the `ftyp`+`moov` init segment ffmpeg writes for a proxy
 *  (`-movflags +empty_moov+default_base_moof+frag_keyframe`). */
export function parseInitSegment(init: Uint8Array): TrackFormat {
  const all = { body: 0, end: init.byteLength }
  const moov = need(child(init, all, 'moov'), 'moov')
  const trak = need(child(init, moov, 'trak'), 'trak')
  const tkhd = need(child(init, trak, 'tkhd'), 'tkhd')
  const mdia = need(child(init, trak, 'mdia'), 'mdia')
  const hdlr = need(child(init, mdia, 'hdlr'), 'hdlr')
  if (fourcc(init, hdlr.body + 8) !== 'vide') throw new Fmp4WriterError('first track is not video')
  const minf = need(child(init, mdia, 'minf'), 'minf')
  const stbl = need(child(init, minf, 'stbl'), 'stbl')
  const stsd = need(child(init, stbl, 'stsd'), 'stsd')
  const entry = need(child(init, stsd, 'avc1', 8), 'avc1 sample entry')
  // VisualSampleEntry: 6 reserved + 2 dref idx + 16 pre/reserved, then w, h (u16) … 78 bytes in all.
  const avcC = need(child(init, entry, 'avcC', 78), 'avcC')
  const dv = new DataView(init.buffer, init.byteOffset, init.byteLength)
  const v1 = init[tkhd.body] === 1
  const whAt = tkhd.body + (v1 ? 88 : 76)
  let width = dv.getUint32(whAt) >>> 16
  let height = dv.getUint32(whAt + 4) >>> 16
  if (!width || !height) {
    width = dv.getUint16(entry.body + 24)
    height = dv.getUint16(entry.body + 26)
  }
  const avcCBytes = init.slice(avcC.body, avcC.end)
  if (avcCBytes.length < 4 || avcCBytes[0] !== 1) throw new Fmp4WriterError('avcC is not version 1')
  const codec = 'avc1.' + hex(avcCBytes.subarray(1, 4))
  return {
    sampleEntry: init.slice(entry.start, entry.end),
    width,
    height,
    avcC: avcCBytes,
    codec,
    // The init CLASS key: the avcC bytes as lowercase hex — the same
    // definition as ingest/proxy.py's index.json init_key (the avcC holds the
    // SPS, so it pins the size too). Asserted equal on real proxies.
    initKey: hex(avcCBytes),
  }
}

// ---------------------------------------------------------------- box writing

const enc = (s: string) => Uint8Array.from(s, (c) => c.charCodeAt(0))

function concat(parts: readonly Uint8Array[]): Uint8Array {
  let n = 0
  for (const p of parts) n += p.byteLength
  const out = new Uint8Array(n)
  let at = 0
  for (const p of parts) {
    out.set(p, at)
    at += p.byteLength
  }
  return out
}

function u32s(...values: number[]): Uint8Array {
  const out = new Uint8Array(values.length * 4)
  const dv = new DataView(out.buffer)
  values.forEach((v, i) => dv.setUint32(i * 4, v >>> 0))
  return out
}

function box(type: string, ...payload: Uint8Array[]): Uint8Array {
  const body = concat(payload)
  return concat([u32s(8 + body.byteLength), enc(type), body])
}

function fullBox(type: string, version: number, flags: number, ...payload: Uint8Array[]): Uint8Array {
  return box(type, u32s(((version & 0xff) << 24) | (flags & 0xffffff)), ...payload)
}

// Unity matrix, 16.16 / 2.30 fixed point.
const MATRIX = u32s(0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)

/** An init segment (`ftyp` + `moov`) for one size class, at timescale
 *  240000, with no edit list (presentation time = decode time = k·T) and an
 *  `mvex` so every media segment is a fragment. The sample entry is the
 *  proxy's own, byte for byte. */
export function writeInitSegment(format: TrackFormat, trackId = 1): Uint8Array {
  const ftyp = box('ftyp', enc('isom'), u32s(0x200), enc('isom'), enc('iso6'), enc('avc1'), enc('mp41'))
  const mvhd = fullBox('mvhd', 0, 0,
    u32s(0, 0, MSE_TIMESCALE, 0, 0x00010000), new Uint8Array([0x01, 0x00, 0, 0]), u32s(0, 0),
    MATRIX, u32s(0, 0, 0, 0, 0, 0), u32s(trackId + 1))
  const tkhd = fullBox('tkhd', 0, 0x000003,
    u32s(0, 0, trackId, 0, 0, 0, 0), u32s(0, 0), MATRIX, u32s(format.width * 0x10000, format.height * 0x10000))
  const mdhd = fullBox('mdhd', 0, 0, u32s(0, 0, MSE_TIMESCALE, 0), new Uint8Array([0x55, 0xc4, 0, 0])) // 'und'
  const hdlr = fullBox('hdlr', 0, 0, u32s(0), enc('vide'), u32s(0, 0, 0), enc('VideoHandler\0'))
  const vmhd = fullBox('vmhd', 0, 1, new Uint8Array(8))
  const dinf = box('dinf', fullBox('dref', 0, 0, u32s(1), fullBox('url ', 0, 1)))
  const stbl = box('stbl',
    fullBox('stsd', 0, 0, u32s(1), format.sampleEntry),
    fullBox('stts', 0, 0, u32s(0)),
    fullBox('stsc', 0, 0, u32s(0)),
    fullBox('stsz', 0, 0, u32s(0, 0)),
    fullBox('stco', 0, 0, u32s(0)))
  const trak = box('trak', tkhd, box('mdia', mdhd, hdlr, box('minf', vmhd, dinf, stbl)))
  const mvex = box('mvex', fullBox('trex', 0, 0, u32s(trackId, 1, 0, 0, 0)))
  return concat([ftyp, box('moov', mvhd, trak, mvex)])
}

/** ISO/IEC 14496-12 sample_flags of an IDR: sample_depends_on = 2 (depends
 *  on no other sample), is_non_sync_sample = 0. */
export const SYNC_SAMPLE_FLAGS = 0x02000000

const TFHD_DEFAULT_BASE_IS_MOOF = 0x020000
const TRUN_FLAGS = 0x000001 | 0x000100 | 0x000200 | 0x000400 // data offset, duration, size, flags

export interface FragmentInput {
  /** mfhd sequence number (strictly increasing per SourceBuffer). */
  readonly sequence: number
  readonly trackId?: number
  /** Output frame index of the first sample. */
  readonly firstFrame: number
  readonly ticksPerFrame: number
  /** AVCC (length-prefixed) access units, one per output frame. */
  readonly samples: readonly Uint8Array[]
}

/** One media segment (`moof` + `mdat`) holding `samples.length` consecutive
 *  output frames starting at `firstFrame`, each lasting exactly T ticks. */
export function writeFragment(input: FragmentInput): Uint8Array {
  const { samples, ticksPerFrame: T } = input
  const n = samples.length
  if (n === 0) throw new Fmp4WriterError('a fragment needs at least one sample')
  if (!Number.isInteger(T) || T <= 0) throw new Fmp4WriterError(`bad ticks per frame ${T}`)
  const base = frameTicks(input.firstFrame, T)
  const trunSize = 20 + n * 12
  const trafSize = 8 + 16 + 20 + trunSize
  const moofSize = 8 + 16 + trafSize
  let dataSize = 0
  for (const s of samples) dataSize += s.byteLength
  const out = new Uint8Array(moofSize + 8 + dataSize)
  const dv = new DataView(out.buffer)
  let o = 0
  const u32 = (v: number) => {
    dv.setUint32(o, v >>> 0)
    o += 4
  }
  const head = (size: number, type: string) => {
    u32(size)
    for (let i = 0; i < 4; i++) out[o + i] = type.charCodeAt(i)
    o += 4
  }
  head(moofSize, 'moof')
  head(16, 'mfhd'); u32(0); u32(input.sequence)
  head(trafSize, 'traf')
  head(16, 'tfhd'); u32(TFHD_DEFAULT_BASE_IS_MOOF); u32(input.trackId ?? 1)
  head(20, 'tfdt'); u32(0x01000000); u32(Math.floor(base / 2 ** 32)); u32(base % 2 ** 32)
  head(trunSize, 'trun'); u32(TRUN_FLAGS); u32(n); u32(moofSize + 8)
  for (const s of samples) {
    u32(T)
    u32(s.byteLength)
    u32(SYNC_SAMPLE_FLAGS)
  }
  head(8 + dataSize, 'mdat')
  for (const s of samples) {
    out.set(s, o)
    o += s.byteLength
  }
  return out
}

// ------------------------------------------------------ timeline → segments

/** A picture sample for one output frame: the proxy frame's bytes and the
 *  format (size class) of the proxy it came from. */
export interface FrameSample {
  readonly format: TrackFormat
  readonly bytes: Uint8Array
}

/** One output frame to write: a sample, or a timeline gap (null). */
export type FrameEntry = FrameSample | null

export type Segment =
  | { readonly kind: 'init'; readonly initKey: string; readonly bytes: Uint8Array }
  | {
      readonly kind: 'media'
      readonly initKey: string
      readonly firstFrame: number
      readonly frames: number
      /** How many of those frames are gap fillers. */
      readonly fillers: number
      readonly bytes: Uint8Array
    }

export interface WriterOptions {
  readonly rate: Rate
  readonly trackId?: number
  /** Frames per fragment, 1..30 (§3.2). */
  readonly maxFragmentFrames?: number
}

/**
 * Stateful writer for ONE SourceBuffer. It remembers the init class last
 * appended (`lastInitKey`) and the last sample it wrote, so:
 *
 *  - an init segment precedes any fragment whose size class differs from the
 *    one the SourceBuffer currently holds (a mixed-size timeline, §3.2);
 *  - a timeline GAP is written as FILLER samples — the previous appended
 *    sample's bytes, or, when nothing was appended yet, the first real frame
 *    of the range — because a hole in the buffered ranges stalls WebKit
 *    playback. The compositor draws black there from programMap, not from the
 *    filler's pixels. Reusing the last sample keeps the loaded size class, so
 *    a gap never costs an init switch.
 *
 * The writer does not pick which frames to append or when; laneA does.
 */
export class Fmp4Writer {
  readonly ticksPerFrame: number
  readonly trackId: number
  readonly maxFragmentFrames: number
  private sequence = 1
  private lastInitKey: string | null = null
  private last: FrameSample | null = null
  private readonly inits = new Map<string, Uint8Array>()

  constructor(options: WriterOptions) {
    const T = ticksPerFrame(options.rate)
    if (T === null) {
      throw new Fmp4WriterError(`rate ${options.rate.num}/${options.rate.den} has no integer tick at ${MSE_TIMESCALE}`)
    }
    const max = options.maxFragmentFrames ?? 30
    if (!Number.isInteger(max) || max < 1 || max > 30) throw new Fmp4WriterError(`maxFragmentFrames ${max} not in 1..30`)
    this.ticksPerFrame = T
    this.trackId = options.trackId ?? 1
    this.maxFragmentFrames = max
  }

  /** The init class the SourceBuffer holds after everything written so far. */
  get currentInitKey(): string | null {
    return this.lastInitKey
  }

  /** Forget the SourceBuffer's state (a new MediaSource, or after an error):
   *  the next write starts with an init segment. */
  reset(): void {
    this.lastInitKey = null
    this.last = null
  }

  initFor(format: TrackFormat): Uint8Array {
    let init = this.inits.get(format.initKey)
    if (!init) {
      init = writeInitSegment(format, this.trackId)
      this.inits.set(format.initKey, init)
    }
    return init
  }

  /**
   * Segments, in append order, that place `frames[i]` at output frame
   * `firstFrame + i`. Appending them in order to a `segments`-mode
   * SourceBuffer with timestampOffset 0 overwrites exactly those frames.
   */
  write(firstFrame: number, frames: readonly FrameEntry[]): Segment[] {
    frameTicks(firstFrame, this.ticksPerFrame)
    const out: Segment[] = []
    let filler = this.last ?? frames.find((f): f is FrameSample => f !== null) ?? null
    let run: Uint8Array[] = []
    let runFillers = 0
    let runStart = firstFrame
    let runKey: string | null = null

    const flush = () => {
      if (run.length === 0 || runKey === null) return
      out.push({
        kind: 'media',
        initKey: runKey,
        firstFrame: runStart,
        frames: run.length,
        fillers: runFillers,
        bytes: writeFragment({
          sequence: this.sequence++,
          trackId: this.trackId,
          firstFrame: runStart,
          ticksPerFrame: this.ticksPerFrame,
          samples: run,
        }),
      })
      run = []
      runFillers = 0
    }

    frames.forEach((entry, i) => {
      const k = firstFrame + i
      const sample = entry ?? filler
      if (sample === null) {
        throw new Fmp4WriterError(`no sample to fill the gap at frame ${k}: nothing appended yet and no clip in range`)
      }
      const key = sample.format.initKey
      if (key !== this.lastInitKey || run.length >= this.maxFragmentFrames) flush()
      if (key !== this.lastInitKey) {
        out.push({ kind: 'init', initKey: key, bytes: this.initFor(sample.format) })
        this.lastInitKey = key
      }
      if (run.length === 0) {
        runStart = k
        runKey = key
      }
      run.push(sample.bytes)
      if (entry === null) runFillers++
      else filler = entry
      this.last = sample
    })
    flush()
    return out
  }
}
