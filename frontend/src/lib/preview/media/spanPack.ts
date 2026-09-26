// Proxy span packs (INSTANT_PREVIEW_SPEC §5.1): `v/NNNN.bin` holds one span of
// consecutive all-intra proxy frames,
//
//     u32 first, u32 count, u32 sizes[count], then the samples back to back
//
// every integer BIG-ENDIAN (the ISO BMFF byte order the samples themselves
// use), each sample one AVCC (length-prefixed) access unit — exactly the bytes
// fmp4Writer puts in an `mdat`. proxyIndex.ts owns fetching and caching spans;
// this module is only the format, shared by it, the writer tests and the WK
// harness pages.

export class SpanPackError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'SpanPackError'
  }
}

export interface SpanPack {
  /** Proxy frame index of `samples[0]`. */
  readonly first: number
  readonly samples: readonly Uint8Array[]
}

/** Parses a span pack. Samples are views into `buf` (no copy). */
export function parseSpanPack(buf: Uint8Array): SpanPack {
  if (buf.byteLength < 8) throw new SpanPackError('span pack shorter than its header')
  const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength)
  const first = dv.getUint32(0)
  const count = dv.getUint32(4)
  const headerEnd = 8 + count * 4
  if (headerEnd > buf.byteLength) throw new SpanPackError(`span pack header claims ${count} samples`)
  const samples: Uint8Array[] = []
  let at = headerEnd
  for (let i = 0; i < count; i++) {
    const size = dv.getUint32(8 + i * 4)
    if (at + size > buf.byteLength) throw new SpanPackError(`sample ${first + i} runs past the end of the pack`)
    samples.push(buf.subarray(at, at + size))
    at += size
  }
  if (at !== buf.byteLength) throw new SpanPackError(`${buf.byteLength - at} trailing bytes after the last sample`)
  return { first, samples }
}

/** The inverse of {@link parseSpanPack} (tests and fixtures). */
export function writeSpanPack(first: number, samples: readonly Uint8Array[]): Uint8Array {
  let data = 0
  for (const s of samples) data += s.byteLength
  const out = new Uint8Array(8 + samples.length * 4 + data)
  const dv = new DataView(out.buffer)
  dv.setUint32(0, first)
  dv.setUint32(4, samples.length)
  let at = 8 + samples.length * 4
  samples.forEach((s, i) => {
    dv.setUint32(8 + i * 4, s.byteLength)
    out.set(s, at)
    at += s.byteLength
  })
  return out
}
