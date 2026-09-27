// DegradedSource (spec §7 degraded tier, §6 R7): one paused <video> on the
// master; seek to pts + 1 ms, confirm by rVFC mediaTime, re-seek once on a
// mismatch, hand the frame over only when confirmed. A fake element models
// what the tier relies on: metadata before seeking, 'seeked' after an
// assignment, and an rVFC reporting the frame it presents.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DegradedSource, PAUSED_SEEK_BIAS_S, frameAtTime, frameTime, type DegradedRequest, type DegradedVideo } from './degradedSource'
import type { SourceInfo } from '../timeline/frameMap'

const INFO30: SourceInfo = { rate: { num: 30, den: 1 }, tb: { num: 1, den: 15360 }, frames: 300, startTicks: 0, w: 1280, h: 720 }
const INFO25: SourceInfo = { rate: { num: 25, den: 1 }, tb: { num: 1, den: 12800 }, frames: 250, startTicks: 0, w: 960, h: 540 }
const NTSC: SourceInfo = { rate: { num: 30000, den: 1001 }, tb: { num: 1, den: 30000 }, frames: 900, startTicks: 0, w: 1920, h: 1080 }

/** A master element: `shift` frames of error in what it presents after a seek. */
class FakeVideo extends EventTarget implements DegradedVideo {
  src = ''
  private t = 0
  get currentTime() { return this.t }
  set currentTime(t: number) { this.t = t; this.seekTo(t) }
  seeking = false
  readyState = 0
  videoWidth = 1280
  videoHeight = 720
  muted = false
  preload = ''
  paused = true
  loads = 0
  assigned: number[] = []
  rvfcOn = true
  /** frames the presented frame is off by, per seek (consumed in order) */
  shifts: number[] = []
  info: SourceInfo = INFO30
  private cbs = new Map<number, (now: number, meta: { mediaTime: number }) => void>()
  private next = 1
  pause() { this.paused = true }
  load() {
    this.loads++
    this.readyState = 0
    setTimeout(() => { this.readyState = 1; this.dispatchEvent(new Event('loadedmetadata')) }, 1)
  }
  removeAttribute() { this.src = '' }
  seekTo(t: number) {
    this.assigned.push(t)
    this.seeking = true
    setTimeout(() => {
      this.seeking = false
      this.dispatchEvent(new Event('seeked'))
      const shift = this.shifts.shift() ?? 0
      const f = frameAtTime(this.info, t) + shift
      if (this.rvfcOn) {
        setTimeout(() => {
          for (const [h, cb] of [...this.cbs]) { this.cbs.delete(h); cb(0, { mediaTime: frameTime(this.info, f) }) }
        }, 1)
      }
    }, 2)
  }
  requestVideoFrameCallback(cb: (now: number, meta: { mediaTime: number }) => void) { const h = this.next++; this.cbs.set(h, cb); return h }
  cancelVideoFrameCallback(h: number) { this.cbs.delete(h) }
}

function rig(el: FakeVideo = new FakeVideo(), presented: ((el: DegradedVideo) => number | null) | null = () => null) {
  const ready: Array<[number, number]> = []
  const failed: Array<[number, string]> = []
  let created = 0
  const src = new DegradedSource({
    createVideo: () => { created++; return el },
    onReady: (k, id) => ready.push([k, id]),
    onFailed: (k, why) => failed.push([k, why]),
    presented: presented ?? undefined,
  })
  return { el, src, ready, failed, created: () => created }
}

const req = (frame: number, info = INFO30, url = '/m/P.mp4'): DegradedRequest => ({ url, info, frame, contentId: 1000 + frame })

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers() })

describe('frame times', () => {
  it('frameTime is the pts; frameAtTime inverts it at every standard and NTSC rate', () => {
    for (const info of [INFO30, INFO25, NTSC]) {
      for (let i = 0; i < info.frames; i++) {
        expect(frameAtTime(info, frameTime(info, i))).toBe(i)
        expect(frameAtTime(info, frameTime(info, i) + PAUSED_SEEK_BIAS_S)).toBe(i)
        // just before the next frame is still this one
        expect(frameAtTime(info, frameTime(info, i + 1) - 1e-4)).toBe(i)
      }
    }
  })
  it('honours the start offset (startTicks)', () => {
    const info = { ...INFO30, startTicks: 1024 }
    expect(frameTime(info, 0)).toBeCloseTo(1024 / 15360, 12)
    expect(frameAtTime(info, frameTime(info, 7))).toBe(7)
  })
})

describe('DegradedSource', () => {
  it('loads the master once, seeks to pts + 1 ms, and hands the frame over when rVFC confirms it', async () => {
    const r = rig()
    r.src.show(12, req(40))
    await vi.advanceTimersByTimeAsync(20)
    expect(r.el.loads).toBe(1)
    expect(r.el.assigned).toEqual([40 / 30 + PAUSED_SEEK_BIAS_S])
    expect(r.ready).toEqual([[12, 1040]])
    expect(r.src.stats.confirmed).toBe(1)
    expect(r.created()).toBe(1)
  })

  it('re-seeks ONCE, corrected, when the presented frame is not the one asked for', async () => {
    const r = rig()
    r.el.shifts = [-1, -1]
    r.src.show(3, req(100))
    await vi.advanceTimersByTimeAsync(30)
    expect(r.el.assigned.length).toBe(2)
    expect(r.el.assigned[1]).toBeCloseTo(100 / 30 + PAUSED_SEEK_BIAS_S + 1 / 30, 9)
    expect(r.ready).toEqual([[3, 1100]])
    expect(r.src.stats.reseeks).toBe(1)
    expect(r.src.stats.mismatches).toBe(1)
  })

  it('twice wrong: never hands over another frame (the last good frame stays)', async () => {
    const r = rig()
    r.el.shifts = [2, 5]
    r.src.show(3, req(100))
    await vi.advanceTimersByTimeAsync(40)
    expect(r.ready).toEqual([])
    expect(r.failed.length).toBe(1)
    expect(r.failed[0][1]).toMatch(/mismatch/)
  })

  it('latest wins: an older show never reports', async () => {
    const r = rig()
    r.src.show(1, req(10))
    r.src.show(2, req(20))
    await vi.advanceTimersByTimeAsync(40)
    expect(r.ready).toEqual([[2, 1020]])
  })

  it('a second source is a new src on the same ONE element', async () => {
    const r = rig()
    r.src.show(1, req(10))
    await vi.advanceTimersByTimeAsync(20)
    r.src.show(2, req(5, INFO25, '/m/Q.mp4'))
    r.el.info = INFO25
    await vi.advanceTimersByTimeAsync(20)
    expect(r.created()).toBe(1)
    expect(r.el.loads).toBe(2)
    expect(r.el.src).toBe('/m/Q.mp4')
    expect(r.ready).toEqual([[1, 1010], [2, 1005]])
  })

  it('the frame already on the element is handed over without a seek', async () => {
    const r = rig()
    r.src.show(1, req(10))
    await vi.advanceTimersByTimeAsync(20)
    r.src.show(9, req(10))
    await vi.advanceTimersByTimeAsync(5)
    expect(r.el.assigned.length).toBe(1)
    expect(r.ready).toEqual([[1, 1010], [9, 1010]])
    expect(r.src.stats.reused).toBe(1)
  })

  it('no rVFC after the seek: the seek target is trusted after the confirm wait (counted)', async () => {
    const r = rig()
    r.el.rvfcOn = false
    r.src.show(4, req(33))
    await vi.advanceTimersByTimeAsync(400)
    expect(r.ready).toEqual([[4, 1033]])
    expect(r.src.stats.unconfirmed).toBe(1)
  })

  it('a media error fails the pending show; cancel() drops it', async () => {
    const r = rig()
    r.src.show(4, req(33))
    r.el.dispatchEvent(new Event('error'))
    expect(r.failed).toEqual([[4, 'media-error']])
    r.src.show(5, req(34))
    r.src.cancel()
    await vi.advanceTimersByTimeAsync(40)
    expect(r.ready).toEqual([])
    expect(r.src.pending).toBe(-1)
  })

  it('a presented-frame stamp naming the frame hands it over at once, without waiting for rVFC', async () => {
    const el = new FakeVideo()
    el.rvfcOn = false
    const r = rig(el, (v) => v.currentTime)
    r.src.show(4, req(33))
    await vi.advanceTimersByTimeAsync(10)
    expect(r.ready).toEqual([[4, 1033]])
    expect(r.src.stats.stamped).toBe(1)
    expect(r.src.stats.unconfirmed).toBe(0)
  })

  it('a stamp naming ANOTHER frame (the previous one still presented) waits for rVFC', async () => {
    const el = new FakeVideo()
    const r = rig(el, () => frameTime(INFO30, 32))
    r.src.show(4, req(33))
    await vi.advanceTimersByTimeAsync(3)
    expect(r.ready).toEqual([])
    await vi.advanceTimersByTimeAsync(10)
    expect(r.ready).toEqual([[4, 1033]])
    expect(r.src.stats.confirmed).toBe(1)
    expect(r.src.stats.stamped).toBe(0)
  })
})
