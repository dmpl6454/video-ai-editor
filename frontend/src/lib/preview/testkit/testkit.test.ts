// The WK testkit's pure parts: the bar decoder (mirrors
// tests/wk/proxy_fixture.py) and the plan → expected-bar model the WK
// assertions compare against. A wrong expectation model would make the WK
// suite pass or fail for the wrong reason, so it is pinned here.
import { describe, expect, it } from 'vitest'
import type { FrameSample, TrackFormat } from '../media/fmp4Writer'
import { BAR_CELL, BAR_WIDTH, NO_PICTURE, barCode, barFrame, barSrc, decodeBarRow } from './barcode'
import {
  SEEK_TIMEOUT_MS, SeekTimeout, each, expectedCodes, frameAt, gap, midFrame, secondsOf, seekTo, seg,
} from './mseKit'
import type { ProxySource } from './proxySource'

function row(code: number): Uint8Array {
  const px = new Uint8Array(BAR_WIDTH * 4)
  for (let bit = 0; bit < 15; bit++) {
    const v = (code >> bit) & 1 ? 235 : 16
    for (let x = bit * BAR_CELL; x < (bit + 1) * BAR_CELL; x++) px.set([v, v, v, 255], x * 4)
  }
  return px
}

describe('bar decoding', () => {
  it('reads source id and frame back from a rendered row', () => {
    for (const [src, frame] of [[1, 0], [3, 2047], [15, 1234], [9, 77]]) {
      const code = barCode(src, frame)
      expect(decodeBarRow(row(code))).toBe(code)
      expect([barSrc(code), barFrame(code)]).toEqual([src, frame])
    }
  })

  it('an all-dark row (no picture) is NO_PICTURE, never frame 0', () => {
    expect(decodeBarRow(new Uint8Array(BAR_WIDTH * 4))).toBe(NO_PICTURE)
    expect(decodeBarRow(row(barCode(0, 5)))).toBe(NO_PICTURE) // src id 0 is never a real frame
  })

  it('refuses a row narrower than the bar', () => {
    expect(() => decodeBarRow(new Uint8Array(10))).toThrow()
  })
})

const fmt = { initKey: 'k' } as TrackFormat
const src = (srcId: number): ProxySource => ({
  name: `s${srcId}`, srcId, format: fmt, index: {} as ProxySource['index'],
  sample: (): FrameSample => ({ format: fmt, bytes: new Uint8Array() }),
})

describe('plans and the expected bar per k', () => {
  const A = src(1)
  const B = src(2)

  it('builds forward, reverse, stepped and duplicated runs', () => {
    expect(seg(A, 3, 6).map((e) => e![1])).toEqual([3, 4, 5])
    expect(seg(A, 6, 3, -1).map((e) => e![1])).toEqual([6, 5, 4])
    expect(seg(A, 0, 7, 2).map((e) => e![1])).toEqual([0, 2, 4, 6])
    expect(each(seg(B, 0, 2), 2).map((e) => e![1])).toEqual([0, 0, 1, 1])
    expect(gap(2)).toEqual([null, null])
  })

  it('a gap shows the previous frame (the filler); a leading gap the first frame', () => {
    const plan = [...gap(2), ...seg(A, 5, 7), ...gap(2), ...seg(B, 0, 1)]
    expect(expectedCodes(plan)).toEqual([
      barCode(1, 5), barCode(1, 5), barCode(1, 5), barCode(1, 6), barCode(1, 6), barCode(1, 6), barCode(2, 0),
    ])
  })

  it('seek targets are mid-frame on the rational grid, and round back to k', () => {
    const ntsc = { num: 30000, den: 1001 }
    for (const k of [0, 1, 29, 30, 1799, 107_892]) {
      expect(frameAt(midFrame(k, ntsc) - 0.49 * 1001 / 30000, ntsc)).toBe(k)
      expect(midFrame(k, ntsc)).toBeCloseTo((k + 0.5) * 1001 / 30000, 12)
      expect(frameAt(secondsOf(k, ntsc), ntsc)).toBe(k)
    }
  })
})

describe('seekTo fails fast (review RD1)', () => {
  /** A stand-in <video>: fires `seeked` after a currentTime write, or never
   *  (a seek into a buffered hole). */
  function fakeVideo(fires: boolean) {
    const target = new EventTarget()
    let t = 0
    const video = Object.assign(target, {
      buffered: { length: 2, start: (i: number) => [0, 2][i], end: (i: number) => [1, 3][i] },
      readyState: 1,
    }) as unknown as HTMLVideoElement
    Object.defineProperty(video, 'currentTime', {
      get: () => t,
      set: (v: number) => { t = v; if (fires) setTimeout(() => target.dispatchEvent(new Event('seeked')), 1) },
    })
    return video
  }

  it('resolves with the seek time when `seeked` fires', async () => {
    const ms = await seekTo(fakeVideo(true), 0.5, 200)
    expect(ms).toBeGreaterThanOrEqual(0)
    expect(ms).toBeLessThan(200)
  })

  it('rejects with the target time and buffered ranges instead of hanging', async () => {
    const err = await seekTo(fakeVideo(false), 1.5, 20).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(SeekTimeout)
    const st = err as SeekTimeout
    expect(st.t).toBe(1.5)
    expect(st.buffered).toEqual([[0, 1], [2, 3]])
    expect(st.message).toMatch(/1\.5.*\[\[0,1\],\[2,3\]\]/)
  })

  it('defaults to a bound well under the harness deadline', () => {
    expect(SEEK_TIMEOUT_MS).toBeGreaterThanOrEqual(1000)
    expect(SEEK_TIMEOUT_MS).toBeLessThanOrEqual(5000)
  })
})
