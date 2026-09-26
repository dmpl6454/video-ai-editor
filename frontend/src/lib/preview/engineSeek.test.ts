// ElementSeeker and PlayingSeekGate (engineSeek.ts) against a fake element
// that behaves like WebKit measured in review RD2: an assignment of the SAME
// time while that seek is still pending fires no second 'seeking'.
import { describe, expect, it } from 'vitest'
import { ElementSeeker, PlayingSeekGate } from './engineSeek'
import type { LaneA } from './media/laneA'

class FakeVideo {
  seeking = false
  private t = 0
  seekingEvents = 0
  assignments = 0
  private pendingTime = Number.NaN
  get currentTime(): number { return this.t }
  set currentTime(v: number) {
    this.assignments++
    // WebKit: a second assignment of the pending time fires no 'seeking'
    if (!(this.seeking && v === this.pendingTime)) this.seekingEvents++
    this.seeking = true
    this.pendingTime = v
    this.t = v
  }
}

function fakeLane(): { lane: LaneA; idle: Array<() => void>; holds: boolean[]; gone: Set<number> } {
  const idle: Array<() => void> = []
  const holds: boolean[] = []
  const gone = new Set<number>()
  const lane = {
    hold: (on: boolean) => { holds.push(on) },
    idle: () => new Promise<void>((r) => idle.push(r)),
    seekTime: (k: number) => (k + 0.5) / 30,
    isReady: (k: number) => !gone.has(k),
  } as unknown as LaneA
  return { lane, idle, holds, gone }
}

const flush = () => new Promise((r) => setTimeout(r, 0))

describe('ElementSeeker', () => {
  it('seek(k) → seek(k + 1) → seek(k) while laneA is busy assigns ONCE: counters stay in step', async () => {
    const video = new FakeVideo()
    const { lane, idle } = fakeLane()
    const s = new ElementSeeker(() => undefined, undefined, 60_000)
    const v = video as unknown as HTMLVideoElement
    for (const k of [10, 11, 10]) s.start(k, v, lane, () => true)
    for (const r of idle) r()
    await flush()
    expect(video.assignments).toBe(1)
    expect(s.issued).toBe(video.seekingEvents)
    for (let i = 0; i < video.seekingEvents; i++) s.onSeeking()
    video.seeking = false
    expect(s.isLatest(v)).toBe(true)
    s.finish()
  })

  it('a same-time re-assignment while that seek is pending is not repeated', () => {
    const video = new FakeVideo()
    const v = video as unknown as HTMLVideoElement
    const s = new ElementSeeker(() => undefined, undefined, 60_000)
    s.assign(v, 1.25)
    s.assign(v, 1.25)
    expect(video.assignments).toBe(1)
    expect(s.issued).toBe(video.seekingEvents)
    video.seeking = false
    s.assign(v, 1.25) // not pending any more: a real seek again
    expect(video.assignments).toBe(2)
  })

  it('the seek timeout re-syncs counters a swallowed "seeking" left apart, and reports the seek', async () => {
    const video = new FakeVideo()
    const v = video as unknown as HTMLVideoElement
    const { lane, idle } = fakeLane()
    const timedOut: number[] = []
    const s = new ElementSeeker((k) => timedOut.push(k), undefined, 5)
    s.start(7, v, lane, () => true)
    idle.forEach((r) => r())
    await flush()
    s.issued += 1 // a 'seeking' WebKit never fired
    video.seeking = false
    expect(s.isLatest(v)).toBe(false)
    await new Promise((r) => setTimeout(r, 20))
    expect(timedOut).toEqual([7])
    expect(s.inFlight).toBe(-1)
    expect(s.seen).toBe(s.issued)
  })

  it('never seeks into a frame laneA trimmed while it went idle (a hole WebKit never completes)', async () => {
    const video = new FakeVideo()
    const { lane, idle, gone } = fakeLane()
    const notReady: number[] = []
    const s = new ElementSeeker(() => undefined, (k) => notReady.push(k), 60_000)
    s.start(137, video as unknown as HTMLVideoElement, lane, () => true)
    gone.add(137) // the 'behind' trim in flight completes
    idle.forEach((r) => r())
    await flush()
    expect(video.assignments).toBe(0)
    expect(notReady).toEqual([137])
    expect(s.inFlight).toBe(-1)
  })

  it('finish() cancels the pending assignment and the timeout', async () => {
    const video = new FakeVideo()
    const { lane, idle } = fakeLane()
    const timedOut: number[] = []
    const s = new ElementSeeker((k) => timedOut.push(k), undefined, 5)
    s.start(3, video as unknown as HTMLVideoElement, lane, () => true)
    s.finish()
    idle.forEach((r) => r())
    await new Promise((r) => setTimeout(r, 20))
    expect(video.assignments).toBe(0)
    expect(timedOut).toEqual([])
  })
})

describe('PlayingSeekGate', () => {
  it('holds stale frames of the old position and opens on the first frame of the new one', () => {
    const g = new PlayingSeekGate()
    g.seeked(30, 1000)
    expect(g.admits(351, 30, 1003)).toBe(false) // WebKit still presenting the old position
    expect(g.admits(352, 30, 1036)).toBe(false)
    expect(g.admits(30, 30, 1060)).toBe(true)
    expect(g.admits(900, 30, 1061)).toBe(true) // open for good
  })

  it('never waits longer than WAIT_MS', () => {
    const g = new PlayingSeekGate()
    g.seeked(30, 0)
    expect(g.admits(400, 30, PlayingSeekGate.WAIT_MS + 1)).toBe(true)
  })

  it('admits everything when no seek is pending', () => {
    expect(new PlayingSeekGate().admits(5, 30)).toBe(true)
  })
})
