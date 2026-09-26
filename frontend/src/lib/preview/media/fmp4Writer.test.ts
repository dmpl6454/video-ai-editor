// fmp4Writer (INSTANT_PREVIEW_SPEC §3.1-3.2, §13 "fmp4Writer.test.ts"): parse
// the output with the mp4box.js the repo already ships and assert box
// structure, tfdt = k·T and sample durations at all nine standard rates; then
// hand real x264 all-intra samples through the writer and let ffprobe/ffmpeg
// say what a demuxer and decoder actually see.
import { execFileSync, spawnSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createFile } from 'mp4box'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import {
  Fmp4Writer,
  Fmp4WriterError,
  MSE_TIMESCALE,
  SYNC_SAMPLE_FLAGS,
  frameTicks,
  parseInitSegment,
  ticksPerFrame,
  writeFragment,
  writeInitSegment,
  type FrameEntry,
  type Rate,
  type Segment,
  type TrackFormat,
} from './fmp4Writer'

// The nine edl/timebase STANDARD_RATES and the T the spec (§3.1) lists.
const STANDARD: readonly (readonly [Rate, number])[] = [
  [{ num: 24000, den: 1001 }, 10010],
  [{ num: 24, den: 1 }, 10000],
  [{ num: 25, den: 1 }, 9600],
  [{ num: 30000, den: 1001 }, 8008],
  [{ num: 30, den: 1 }, 8000],
  [{ num: 48, den: 1 }, 5000],
  [{ num: 50, den: 1 }, 4800],
  [{ num: 60000, den: 1001 }, 4004],
  [{ num: 60, den: 1 }, 4000],
]

// ------------------------------------------------------------ mp4box access
// mp4box's typings describe its box classes loosely; this is the narrow view
// the assertions read.
interface ParsedTrun { sample_count: number; data_offset: number; flags: number; sample_duration: number[]; sample_size: number[]; sample_flags: number[] }
interface ParsedTraf { tfhd: { track_id: number; flags: number }; tfdt: { baseMediaDecodeTime: number; version: number }; truns: ParsedTrun[] }
interface ParsedMoof { start: number; mfhd: { sequence_number: number }; trafs: ParsedTraf[] }
interface ParsedEntry { type: string; width: number; height: number; avcC: { AVCProfileIndication: number; AVCLevelIndication: number } }
interface ParsedFile {
  boxes: { type: string }[]
  ftyp: { major_brand: string; compatible_brands: string[] }
  moov: {
    mvhd: { timescale: number; duration: number; next_track_id: number }
    traks: {
      tkhd: { track_id: number; width: number; height: number; flags: number }
      edts?: unknown
      mdia: { mdhd: { timescale: number }; hdlr: { handler: string }; minf: { stbl: { stsd: { entries: ParsedEntry[] } } } }
    }[]
    mvex: { trexs: { track_id: number; default_sample_description_index: number }[] }
  }
  moofs: ParsedMoof[]
  mdats: { start: number; size: number; hdr_size: number }[]
}

function parse(bytes: Uint8Array): ParsedFile {
  const file = createFile()
  let error = ''
  ;(file as unknown as { onError: (m: string, e: string) => void }).onError = (_m, e) => { error = e }
  const ab = bytes.slice().buffer as ArrayBuffer & { fileStart: number }
  ab.fileStart = 0
  file.appendBuffer(ab)
  file.flush()
  if (error) throw new Error(error)
  return file as unknown as ParsedFile
}

const join8 = (parts: readonly Uint8Array[]) => {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.byteLength, 0))
  let at = 0
  for (const p of parts) { out.set(p, at); at += p.byteLength }
  return out
}
const joinSegments = (segs: readonly Segment[]) => join8(segs.map((s) => s.bytes))

// ------------------------------------------------ a synthetic avc1 sample entry
function be32(n: number) { return [(n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255] }
function rawBox(type: string, payload: number[]): number[] {
  return [...be32(8 + payload.length), ...Array.from(type, (c) => c.charCodeAt(0)), ...payload]
}
/** A structurally valid avc1 entry (High, level 3.1) at w×h. The SPS/PPS
 *  bytes are placeholders: only real-decode tests need real ones. */
function syntheticFormat(w: number, h: number, level = 0x1f): TrackFormat {
  const sps = [0x67, 0x64, 0x00, level, 0xac, w & 255, h & 255]
  const pps = [0x68, 0xee, 0x3c, 0x80]
  const avcC = rawBox('avcC', [1, 0x64, 0x00, level, 0xff, 0xe1, 0, sps.length, ...sps, 1, 0, pps.length, ...pps])
  const fields = [0, 0, 0, 0, 0, 0, 0, 1, ...new Array(16).fill(0), (w >> 8) & 255, w & 255, (h >> 8) & 255, h & 255,
    0, 0x48, 0, 0, 0, 0x48, 0, 0, 0, 0, 0, 0, 0, 1, ...new Array(32).fill(0), 0, 0x18, 0xff, 0xff]
  const entry = Uint8Array.from(rawBox('avc1', [...fields, ...avcC]))
  const init = join8([
    Uint8Array.from(rawBox('ftyp', [...Array.from('isom', (c) => c.charCodeAt(0)), 0, 0, 2, 0])),
    // a minimal moov around the entry, so the real parser path is exercised
    Uint8Array.from(rawBox('moov', rawBox('trak', [
      ...rawBox('tkhd', [0, 0, 0, 3, ...new Array(72).fill(0), ...be32(w * 65536), ...be32(h * 65536)]),
      ...rawBox('mdia', [
        ...rawBox('hdlr', [0, 0, 0, 0, 0, 0, 0, 0, ...Array.from('vide', (c) => c.charCodeAt(0)), ...new Array(13).fill(0)]),
        ...rawBox('minf', rawBox('stbl', rawBox('stsd', [0, 0, 0, 0, 0, 0, 0, 1, ...entry]))),
      ]),
    ]))),
  ])
  return parseInitSegment(init)
}

const A = syntheticFormat(1280, 720)
const C = syntheticFormat(720, 1280)
const sample = (id: number, size = 5 + (id % 7)) => {
  const b = new Uint8Array(size)
  b[0] = 0; b[1] = 0; b[2] = 0; b[3] = size - 4; b[4] = 0x65
  for (let i = 5; i < size; i++) b[i] = (id * 31 + i) & 255
  return b
}
const at = (format: TrackFormat, id: number): FrameEntry => ({ format, bytes: sample(id) })

// ================================================================ unit tests

describe('ticksPerFrame (R1)', () => {
  it.each(STANDARD)('%o → %i ticks at 240000', (rate, T) => {
    expect(ticksPerFrame(rate)).toBe(T)
    expect(T * rate.num).toBe(MSE_TIMESCALE * rate.den)
  })

  it('refuses rates with no integer tick, and nonsense rates', () => {
    expect(ticksPerFrame({ num: 7, den: 1 })).toBeNull()
    expect(ticksPerFrame({ num: 30001, den: 1000 })).toBeNull()
    expect(ticksPerFrame({ num: 0, den: 1 })).toBeNull()
    expect(ticksPerFrame({ num: 30, den: -1 })).toBeNull()
    expect(ticksPerFrame({ num: 29.97, den: 1 })).toBeNull()
    expect(() => new Fmp4Writer({ rate: { num: 7, den: 1 } })).toThrow(Fmp4WriterError)
  })

  it('frameTicks is exact past 2^32 and rejects bad k', () => {
    const k = Math.ceil(2 ** 32 / 4000) + 5 // first k past the 32-bit tick range, at 60 fps
    expect(frameTicks(k, 4000)).toBe(4_294_968_000 + 20_000)
    expect(() => frameTicks(-1, 8000)).toThrow(Fmp4WriterError)
    expect(() => frameTicks(1.5, 8000)).toThrow(Fmp4WriterError)
  })
})

describe('parseInitSegment', () => {
  it('reads size, avcC, codec string and the size-class key', () => {
    expect(A.width).toBe(1280)
    expect(A.height).toBe(720)
    expect(A.codec).toBe('avc1.64001f')
    // ONE definition of the init class key on both sides (review RD1): the
    // lowercase hex of the avcC record, exactly ingest/proxy.py's init_key.
    expect(A.initKey).toBe(Array.from(A.avcC, (x) => x.toString(16).padStart(2, '0')).join(''))
    expect(A.initKey).not.toBe(C.initKey)
    expect(syntheticFormat(1280, 720).initKey).toBe(A.initKey)
    expect(fourccAt(A.sampleEntry, 4)).toBe('avc1')
  })

  it('rejects inits without a video avc1 track', () => {
    expect(() => parseInitSegment(Uint8Array.from(rawBox('ftyp', [0, 0, 0, 0])))).toThrow(/moov/)
    expect(() => parseInitSegment(Uint8Array.from([0, 0, 0, 99, 0x6d, 0x6f, 0x6f, 0x76]))).toThrow(/bad box size/)
  })
})
function fourccAt(b: Uint8Array, i: number) { return String.fromCharCode(b[i], b[i + 1], b[i + 2], b[i + 3]) }

describe('writeInitSegment', () => {
  const init = writeInitSegment(A)
  const file = parse(init)

  it('is ftyp + moov with timescale 240000 on movie and media, and no edit list', () => {
    expect(file.boxes.map((b) => b.type)).toEqual(['ftyp', 'moov'])
    expect(file.ftyp.major_brand).toBe('isom')
    expect(file.ftyp.compatible_brands).toContain('iso6')
    expect(file.moov.mvhd.timescale).toBe(MSE_TIMESCALE)
    expect(file.moov.mvhd.duration).toBe(0)
    expect(file.moov.mvhd.next_track_id).toBe(2)
    const trak = file.moov.traks[0]
    expect(trak.mdia.mdhd.timescale).toBe(MSE_TIMESCALE)
    expect(trak.mdia.hdlr.handler).toBe('vide')
    expect(trak.edts).toBeUndefined()
    expect(trak.tkhd.track_id).toBe(1)
    expect(trak.tkhd.flags & 3).toBe(3)
    expect(trak.tkhd.width / 65536).toBe(1280)
    expect(trak.tkhd.height / 65536).toBe(720)
  })

  it('carries the proxy sample entry byte for byte and declares fragments', () => {
    const entry = file.moov.traks[0].mdia.minf.stbl.stsd.entries[0]
    expect(entry.type).toBe('avc1')
    expect([entry.width, entry.height]).toEqual([1280, 720])
    expect(entry.avcC.AVCProfileIndication).toBe(0x64)
    expect(entry.avcC.AVCLevelIndication).toBe(0x1f)
    expect(indexOf(init, A.sampleEntry)).toBeGreaterThan(0)
    expect(file.moov.mvex.trexs[0]).toMatchObject({ track_id: 1, default_sample_description_index: 1 })
    expect(parseInitSegment(init).initKey).toBe(A.initKey) // our own init reads back as the same class
  })
})
function indexOf(hay: Uint8Array, needle: Uint8Array) {
  outer: for (let i = 0; i + needle.length <= hay.length; i++) {
    for (let j = 0; j < needle.length; j++) if (hay[i + j] !== needle[j]) continue outer
    return i
  }
  return -1
}

describe('writeFragment at every standard rate', () => {
  it.each(STANDARD)('%o: tfdt = k·T, every duration T, sizes and sync flags exact', (rate, T) => {
    const samples = [sample(1, 9), sample(2, 40), sample(3, 6)]
    for (const k of [0, 1, 29, 1001, 86_399]) {
      const frag = writeFragment({ sequence: 7, firstFrame: k, ticksPerFrame: T, samples })
      const f = parse(join8([writeInitSegment(A), frag]))
      expect(f.boxes.map((b) => b.type)).toEqual(['ftyp', 'moov', 'moof', 'mdat'])
      const moof = f.moofs[0]
      expect(moof.mfhd.sequence_number).toBe(7)
      const traf = moof.trafs[0]
      expect(traf.tfhd.track_id).toBe(1)
      expect(traf.tfhd.flags & 0x020000).toBe(0x020000) // default-base-is-moof
      expect(traf.tfdt.version).toBe(1)
      expect(traf.tfdt.baseMediaDecodeTime).toBe(k * T)
      expect(traf.tfdt.baseMediaDecodeTime * rate.num).toBe(k * MSE_TIMESCALE * rate.den) // exactly k/R seconds
      const trun = traf.truns[0]
      expect(trun.sample_count).toBe(3)
      expect(trun.sample_duration).toEqual([T, T, T])
      expect(trun.sample_size).toEqual(samples.map((s) => s.byteLength))
      expect(trun.sample_flags).toEqual([SYNC_SAMPLE_FLAGS, SYNC_SAMPLE_FLAGS, SYNC_SAMPLE_FLAGS])
      // data_offset (from the moof start) lands on the mdat payload, which is the samples in order
      const mdat = f.mdats[0]
      expect(moof.start + trun.data_offset).toBe(mdat.start + mdat.hdr_size)
      const payload = frag.subarray(trun.data_offset)
      expect(Array.from(payload)).toEqual(Array.from(join8(samples)))
    }
  })

  it('writes the high word of a 64-bit tfdt', () => {
    const k = Math.ceil(2 ** 32 / 4000) + 3
    const frag = writeFragment({ sequence: 1, firstFrame: k, ticksPerFrame: 4000, samples: [sample(1)] })
    const f = parse(join8([writeInitSegment(A), frag]))
    expect(f.moofs[0].trafs[0].tfdt.baseMediaDecodeTime).toBe(4_294_968_000 + 12_000)
    const dv = new DataView(frag.buffer, frag.byteOffset)
    expect(dv.getUint32(8 + 16 + 8 + 16 + 12)).toBe(1) // the high word itself
  })

  it('refuses empty fragments and bad ticks', () => {
    expect(() => writeFragment({ sequence: 1, firstFrame: 0, ticksPerFrame: 8000, samples: [] })).toThrow(Fmp4WriterError)
    expect(() => writeFragment({ sequence: 1, firstFrame: 0, ticksPerFrame: 0, samples: [sample(1)] })).toThrow(Fmp4WriterError)
    expect(() => writeFragment({ sequence: 1, firstFrame: 0, ticksPerFrame: 8008.5, samples: [sample(1)] })).toThrow(Fmp4WriterError)
  })
})

// ------------------------------------------------------------- Fmp4Writer

/** Every media sample in `segs`, flattened: [k, initKey, bytes]. Also checks
 *  that each media segment's init class is the one most recently appended. */
function flatten(segs: readonly Segment[], T: number, startKey: string | null = null) {
  const out: { k: number; key: string; bytes: Uint8Array }[] = []
  let loaded = startKey
  for (const s of segs) {
    if (s.kind === 'init') { loaded = s.initKey; continue }
    expect(s.initKey).toBe(loaded)
    const f = parse(join8([writeInitSegment(A), s.bytes]))
    const traf = f.moofs[0].trafs[0]
    const trun = traf.truns[0]
    expect(traf.tfdt.baseMediaDecodeTime).toBe(s.firstFrame * T)
    expect(trun.sample_count).toBe(s.frames)
    let off = trun.data_offset
    for (let i = 0; i < s.frames; i++) {
      out.push({ k: s.firstFrame + i, key: s.initKey, bytes: s.bytes.subarray(off, off + trun.sample_size[i]) })
      off += trun.sample_size[i]
    }
  }
  return out
}
const same = (a: Uint8Array, b: Uint8Array) => a.length === b.length && a.every((v, i) => v === b[i])

describe('Fmp4Writer timeline writes', () => {
  it.each(STANDARD)('%o: a 100-frame range is contiguous, one sample per k, fragments ≤ 30', (rate, T) => {
    const w = new Fmp4Writer({ rate })
    const frames = Array.from({ length: 100 }, (_, i) => at(A, i))
    const segs = w.write(250, frames)
    expect(segs[0].kind).toBe('init')
    const media = segs.filter((s) => s.kind === 'media')
    expect(media.map((s) => s.frames)).toEqual([30, 30, 30, 10])
    const flat = flatten(segs, T)
    expect(flat.map((f) => f.k)).toEqual(Array.from({ length: 100 }, (_, i) => 250 + i))
    flat.forEach((f, i) => expect(same(f.bytes, frames[i]!.bytes)).toBe(true))
  })

  it('writes an init before a size-class change and again when switching back', () => {
    const w = new Fmp4Writer({ rate: { num: 30, den: 1 } })
    const frames = [at(A, 0), at(A, 1), at(C, 2), at(C, 3), at(A, 4)]
    const segs = w.write(0, frames)
    expect(segs.map((s) => (s.kind === 'init' ? `init:${s.initKey === A.initKey ? 'A' : 'C'}` : `media:${s.firstFrame}+${s.frames}`)))
      .toEqual(['init:A', 'media:0+2', 'init:C', 'media:2+2', 'init:A', 'media:4+1'])
    expect(w.currentInitKey).toBe(A.initKey)
    // The init segments are this module's 240000-timescale inits, one per class.
    const initC = segs[2]
    expect(initC.kind === 'init' && parse(initC.bytes).moov.traks[0].tkhd.width / 65536).toBe(720)
    // A later write in the same class needs no init; a class change does.
    expect(w.write(5, [at(A, 5)]).map((s) => s.kind)).toEqual(['media'])
    expect(w.write(6, [at(C, 6)]).map((s) => s.kind)).toEqual(['init', 'media'])
  })

  it('fills a timeline gap with the previous appended sample, in the loaded class, with no init switch', () => {
    const w = new Fmp4Writer({ rate: { num: 25, den: 1 } })
    const segs = w.write(0, [at(A, 0), at(A, 1), null, null, null, at(A, 5)])
    expect(segs.map((s) => s.kind)).toEqual(['init', 'media'])
    const m = segs[1]
    expect(m.kind === 'media' && m.fillers).toBe(3)
    const flat = flatten(segs, 9600)
    expect(flat.map((f) => f.k)).toEqual([0, 1, 2, 3, 4, 5])
    for (const k of [2, 3, 4]) expect(same(flat[k].bytes, sample(1))).toBe(true)
    // A gap written LATER (e.g. after a delete) reuses the last appended sample,
    // even though it is from another call and another part of the timeline.
    const later = w.write(40, [null, null])
    expect(later.map((s) => s.kind)).toEqual(['media'])
    expect(flatten(later, 9600, A.initKey).every((f) => same(f.bytes, sample(5)))).toBe(true)
  })

  it('a gap after a class change fills in the NEW class (no extra init)', () => {
    const w = new Fmp4Writer({ rate: { num: 30, den: 1 } })
    const segs = w.write(0, [at(A, 0), at(C, 1), null, at(C, 3)])
    expect(segs.map((s) => s.kind)).toEqual(['init', 'media', 'init', 'media'])
    const flat = flatten(segs, 8000)
    expect(flat[2].key).toBe(C.initKey)
    expect(same(flat[2].bytes, sample(1))).toBe(true)
  })

  it('a timeline that STARTS with a gap is filled with the first clip frame of the range', () => {
    const w = new Fmp4Writer({ rate: { num: 30000, den: 1001 } })
    const segs = w.write(0, [null, null, at(C, 7), at(C, 8)])
    expect(segs[0]).toMatchObject({ kind: 'init', initKey: C.initKey })
    const flat = flatten(segs, 8008)
    expect(flat.map((f) => f.k)).toEqual([0, 1, 2, 3])
    expect(same(flat[0].bytes, sample(7)) && same(flat[1].bytes, sample(7))).toBe(true)
  })

  it('refuses a gap-only first write (nothing to fill with) and says why', () => {
    const w = new Fmp4Writer({ rate: { num: 30, den: 1 } })
    expect(() => w.write(0, [null, null])).toThrow(/no sample to fill the gap at frame 0/)
  })

  it('duplicates and reverse are the same bytes at new k (conform-up, slow-mo, reverse)', () => {
    const w = new Fmp4Writer({ rate: { num: 60, den: 1 } })
    const src = [at(A, 10), at(A, 11), at(A, 12)]
    const frames = [src[0], src[0], src[1], src[1], src[2], src[1], src[0]]
    const flat = flatten(w.write(12, frames), 4000)
    expect(flat.map((f) => f.k)).toEqual([12, 13, 14, 15, 16, 17, 18])
    flat.forEach((f, i) => expect(same(f.bytes, frames[i]!.bytes)).toBe(true))
  })

  it('mfhd sequence numbers strictly increase across writes; reset() re-sends the init', () => {
    const w = new Fmp4Writer({ rate: { num: 50, den: 1 }, maxFragmentFrames: 2 })
    const seqs: number[] = []
    for (const s of [...w.write(0, [at(A, 0), at(A, 1), at(A, 2)]), ...w.write(90, [at(A, 3)])]) {
      if (s.kind === 'media') seqs.push(parse(join8([writeInitSegment(A), s.bytes])).moofs[0].mfhd.sequence_number)
    }
    expect(seqs).toEqual([1, 2, 3])
    w.reset()
    expect(w.currentInitKey).toBeNull()
    expect(w.write(4, [at(A, 4)]).map((s) => s.kind)).toEqual(['init', 'media'])
  })

  it('validates maxFragmentFrames to 1..30 and caches one init per class', () => {
    expect(() => new Fmp4Writer({ rate: { num: 30, den: 1 }, maxFragmentFrames: 0 })).toThrow(Fmp4WriterError)
    expect(() => new Fmp4Writer({ rate: { num: 30, den: 1 }, maxFragmentFrames: 31 })).toThrow(Fmp4WriterError)
    const w = new Fmp4Writer({ rate: { num: 30, den: 1 } })
    expect(w.initFor(A)).toBe(w.initFor(A))
    expect(w.initFor(A)).not.toBe(w.initFor(C))
  })

  it('writes a 30-frame fragment in well under the 1 ms budget (informational)', () => {
    const big = Array.from({ length: 30 }, (_, i) => ({ format: A, bytes: sample(i, 40_000) }))
    const w = new Fmp4Writer({ rate: { num: 30, den: 1 } })
    w.write(0, big)
    const t0 = performance.now()
    for (let i = 0; i < 50; i++) w.write(30 * (i + 1), big)
    const perFragment = (performance.now() - t0) / 50
    expect(perFragment).toBeLessThan(20) // CI-safe ceiling; measured ≈ 0.3 ms on M-class
  })
})

// ============================================== real x264 samples via ffmpeg

const hasFfmpeg = spawnSync('ffmpeg', ['-version']).status === 0 && spawnSync('ffprobe', ['-version']).status === 0

interface RealSource { format: TrackFormat; samples: Uint8Array[]; md5: string[] }

/** Proxy-shaped source per the §5.1 recipe (all-intra, stitchable, 8-bit
 *  4:2:0, BT.709 tags, fragmented), demuxed with mp4box. */
function encodeProxy(dir: string, name: string, w: number, h: number, frames: number, pattern: string): RealSource {
  const path = join(dir, `${name}.mp4`)
  execFileSync('ffmpeg', ['-v', 'error', '-y', '-f', 'lavfi', '-i', `${pattern}=s=${w}x${h}:r=30:d=${frames / 30}`,
    '-frames:v', String(frames), '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23', '-profile:v', 'high', '-level:v', '4.1',
    '-x264-params', 'keyint=1:min-keyint=1:scenecut=0:bframes=0:stitchable=1', '-pix_fmt', 'yuv420p',
    '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709', '-color_range', 'tv',
    '-f', 'mp4', '-movflags', '+empty_moov+default_base_moof+frag_keyframe', path])
  const bytes = new Uint8Array(readFileSync(path))
  const samples: Uint8Array[] = []
  const file = createFile()
  file.onReady = (info) => {
    file.setExtractionOptions(info.tracks[0].id, null, { nbSamples: 100_000 })
    file.start()
  }
  file.onSamples = (_id, _user, got) => {
    for (const s of got) samples.push(new Uint8Array(s.data as unknown as ArrayBufferLike))
  }
  const ab = bytes.slice().buffer as ArrayBuffer & { fileStart: number }
  ab.fileStart = 0
  file.appendBuffer(ab)
  file.flush()
  return { format: parseInitSegment(bytes), samples, md5: frameMd5(path) }
}

function frameMd5(path: string): string[] {
  const out = execFileSync('ffmpeg', ['-v', 'error', '-i', path, '-map', '0:v:0', '-fps_mode', 'passthrough', '-f', 'framemd5', '-'], { encoding: 'utf-8' })
  return out.split('\n').filter((l) => l && !l.startsWith('#')).map((l) => l.split(',').pop()!.trim())
}

describe.skipIf(!hasFfmpeg)('real all-intra proxy samples through ffprobe and ffmpeg', () => {
  let dir = ''
  let A1: RealSource, A2: RealSource, P: RealSource
  beforeAll(() => {
    dir = mkdtempSync(join(tmpdir(), 'fmp4w-'))
    A1 = encodeProxy(dir, 'a1', 320, 180, 24, 'testsrc2')
    A2 = encodeProxy(dir, 'a2', 320, 180, 12, 'testsrc')
    P = encodeProxy(dir, 'p', 180, 320, 12, 'testsrc2')
  })
  afterAll(() => { if (dir) rmSync(dir, { recursive: true, force: true }) })

  it('same-size stitchable encodes share one avcC, so one init class', () => {
    expect(A1.samples).toHaveLength(24)
    expect(A1.format.initKey).toBe(A2.format.initKey)
    expect(A1.format.initKey).not.toBe(P.format.initKey)
    // High (0x64), level 4.1 (0x29) per the recipe. x264 sets constraint_set3
    // (0x10) for keyint=1, so the string is NOT the spec's example avc1.64001F:
    // laneA must use the parsed codec, never a hard-coded one.
    expect(A1.format.codec).toBe('avc1.641029')
  })

  // cut A1 → A2, reverse, freeze/duplicates, a gap, then a portrait source:
  // [src, frame] per output k, null = gap
  const plan = (): ([RealSource, number] | null)[] => [
    ...[0, 1, 2, 3, 4, 5].map((i) => [A1, i] as [RealSource, number]),
    ...[3, 4, 5].map((i) => [A2, i] as [RealSource, number]),
    ...[20, 19, 18, 17].map((i) => [A1, i] as [RealSource, number]),
    [A1, 9], [A1, 9], [A1, 9],
    null, null,
    ...[0, 1, 2].map((i) => [P, i] as [RealSource, number]),
    [A2, 11],
  ]

  it.each(STANDARD)('%o: ffprobe sees pts = k·T/240000, duration T, every packet a keyframe; ffmpeg decodes the planned frames', (rate, T) => {
    const w = new Fmp4Writer({ rate, maxFragmentFrames: 4 })
    const p = plan()
    const firstK = 3
    const segs = w.write(firstK, p.map((e) => (e ? { format: e[0].format, bytes: e[0].samples[e[1]] } : null)))
    const out = join(dir, `out_${rate.num}_${rate.den}.mp4`)
    // Mixed sizes can't live in one progressive file, so ffmpeg reads the
    // stream up to the size switch, and the portrait tail separately with its init.
    const switchAt = segs.findIndex((s, i) => i > 0 && s.kind === 'init')
    writeFileSync(out, joinSegments(segs.slice(0, switchAt)))
    const packets = execFileSync('ffprobe', ['-v', 'error', '-select_streams', 'v:0', '-show_entries',
      'packet=pts,dts,duration,flags', '-of', 'csv=p=0', out], { encoding: 'utf-8' }).trim().split('\n').map((l) => l.split(','))
    const before = p.findIndex((e) => e?.[0] === P)
    expect(packets).toHaveLength(before)
    packets.forEach(([pts, dts, duration, flags], i) => {
      expect(Number(pts)).toBe((firstK + i) * T)
      expect(Number(dts)).toBe((firstK + i) * T)
      expect(Number(duration)).toBe(T)
      expect(flags.startsWith('K')).toBe(true)
    })
    const tb = execFileSync('ffprobe', ['-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=time_base,start_time',
      '-of', 'csv=p=0', out], { encoding: 'utf-8' }).trim()
    expect(tb.startsWith('1/240000')).toBe(true)

    const md5 = frameMd5(out)
    expect(md5).toHaveLength(before)
    let lastReal = ''
    p.slice(0, before).forEach((e, i) => {
      const want = e ? e[0].md5[e[1]] : lastReal // a gap filler decodes as the previous frame
      expect(md5[i], `k=${firstK + i}`).toBe(want)
      if (e) lastReal = want
    })

    const tail = join(dir, `tail_${rate.num}_${rate.den}.mp4`)
    // The portrait run is its init plus its fragments, up to the switch back to A2's class.
    const back = segs.findIndex((s, i) => i > switchAt && s.kind === 'init')
    expect(back).toBeGreaterThan(switchAt)
    writeFileSync(tail, joinSegments(segs.slice(switchAt, back)))
    const portrait = (p.slice(before) as [RealSource, number][]).filter((e) => e[0] === P)
    expect(frameMd5(tail)).toEqual(portrait.map((e) => e[0].md5[e[1]]))
  })
})

describe('one R1 tick rule (review RD1)', () => {
  it('fmp4Writer, frameMap and timebase share one ticksPerFrame and one timescale', async () => {
    const tb = await import('../timeline/timebase')
    const fm = await import('../timeline/frameMap')
    const w = await import('./fmp4Writer')
    expect(w.MSE_TIMESCALE).toBe(tb.MSE_TIMESCALE)
    expect(fm.ticksPerFrame).toBe(tb.ticksPerFrame)
    expect(w.ticksPerFrame).toBe(tb.ticksPerFrameExact)
    expect(tb.ticksPerFrame(30000 / 1001)).toBe(8008)
    expect(tb.ticksPerFrameExact({ num: 30000, den: 1001 })).toBe(8008)
    expect(tb.ticksPerFrameExact({ num: 7, den: 1 })).toBeNull()
  })
})
