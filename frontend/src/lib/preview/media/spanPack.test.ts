import { describe, expect, it } from 'vitest'
import { SpanPackError, parseSpanPack, writeSpanPack } from './spanPack'

const s = (...b: number[]) => Uint8Array.from(b)

describe('span packs (§5.1 v/NNNN.bin)', () => {
  it('round-trips first, count and every sample, big-endian', () => {
    const samples = [s(0, 0, 0, 1, 0x65), s(), s(0, 0, 0, 2, 0x65, 9)]
    const pack = writeSpanPack(120, samples)
    expect(Array.from(pack.subarray(0, 8))).toEqual([0, 0, 0, 120, 0, 0, 0, 3])
    expect(Array.from(pack.subarray(8, 20))).toEqual([0, 0, 0, 5, 0, 0, 0, 0, 0, 0, 0, 6])
    const back = parseSpanPack(pack)
    expect(back.first).toBe(120)
    expect(back.samples.map((x) => Array.from(x))).toEqual(samples.map((x) => Array.from(x)))
  })

  it('returns views, not copies, and honours a byteOffset', () => {
    const pack = writeSpanPack(0, [s(1, 2, 3)])
    const shifted = new Uint8Array(pack.byteLength + 4)
    shifted.set(pack, 4)
    const back = parseSpanPack(shifted.subarray(4))
    expect(back.samples[0].buffer).toBe(shifted.buffer)
    expect(Array.from(back.samples[0])).toEqual([1, 2, 3])
  })

  it('rejects short headers, overruns and trailing bytes', () => {
    expect(() => parseSpanPack(s(0, 0, 0))).toThrow(SpanPackError)
    expect(() => parseSpanPack(s(0, 0, 0, 0, 0, 0, 0, 9))).toThrow(/claims 9 samples/)
    expect(() => parseSpanPack(s(0, 0, 0, 7, 0, 0, 0, 1, 0, 0, 0, 5, 1, 2))).toThrow(/sample 7 runs past/)
    expect(() => parseSpanPack(s(0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 1, 1, 2))).toThrow(/1 trailing bytes/)
  })
})
