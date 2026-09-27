import { describe, expect, it } from 'vitest'
import { canFreeze, planFreeze } from './freezeFrame'
import { clipDuration, clipSpeedFactor, type EDL } from '../types'

const clip = (id: string, start: number, extra: Record<string, unknown> = {}) =>
  ({ id, src: '/m/a.mp4', in: 0, out: 4, start, ...extra })

function edl(clips: object[], opts: { locked?: boolean; transitions?: object[] } = {}): EDL {
  return {
    version: 3, duration: 8, canvas: { w: 1080, h: 1920, fps: 30, bg: '#000' },
    tracks: [
      { id: 'v1', type: 'video', z: 0, clips, locked: opts.locked, transitions: opts.transitions ?? [] },
      { id: 'tx_super', type: 'text', z: 3, clips: [{ id: 't1', text: 'x', start: 0, end: 8 }] },
    ],
  } as unknown as EDL
}

describe('planFreeze', () => {
  it('freezes the Main video clip under the playhead, in layout time', () => {
    expect(planFreeze(edl([clip('a', 0), clip('b', 4)]), null, 5)).toEqual({ kind: 'freeze', args: { time: 5 } })
  })

  it('names a selected Main video clip, and refuses one the playhead is not over', () => {
    const e = edl([clip('a', 0), clip('b', 4)])
    expect(planFreeze(e, 'b', 5)).toEqual({ kind: 'freeze', args: { time: 5, clip_id: 'b' } })
    expect(planFreeze(e, 'a', 5)).toMatchObject({ kind: 'refuse', message: expect.stringContaining('Move the playhead over the clip') })
  })

  it('ignores a selection on another lane', () => {
    expect(planFreeze(edl([clip('a', 0)]), 't1', 1)).toEqual({ kind: 'freeze', args: { time: 1 } })
  })

  it('refuses past the end, on an empty or locked Main lane', () => {
    expect(planFreeze(edl([clip('a', 0)]), null, 6).kind).toBe('refuse')
    expect(planFreeze(edl([]), null, 0).kind).toBe('refuse')
    expect(planFreeze(edl([clip('a', 0)], { locked: true }), null, 1)).toMatchObject({ message: expect.stringContaining('locked') })
    expect(canFreeze(edl([clip('a', 0)]), null, 1)).toBe(true)
    expect(canFreeze(null, null, 1)).toBe(false)
  })

  it('decodes the render-time playhead through v1 transitions', () => {
    // A 1 s dissolve at 4: clip b PLAYS from 3; render 3.5 is layout 4.5 in b.
    const e = edl([clip('a', 0), clip('b', 4)], { transitions: [{ at: 4, type: 'fade', duration: 1 }] })
    const p = planFreeze(e, null, 3.5)
    expect(p.kind).toBe('freeze')
    if (p.kind === 'freeze') expect(p.args.time).toBeCloseTo(4.5, 9)
  })

  it('uses the retimed footprint (a curve fills its integral, a freeze its hold)', () => {
    const curve = clip('a', 0, { speed: { curve: [[0, 1], [1, 3]], name: 'custom' } }) // mean 2 → 2 s
    expect(clipDuration(curve as never)).toBeCloseTo(2, 12)
    expect(clipSpeedFactor(curve as never)).toBeCloseTo(2, 12)
    expect(planFreeze(edl([curve, clip('b', 2)]), null, 2.5)).toEqual({ kind: 'freeze', args: { time: 2.5 } })
    const still = clip('s', 0, { out: 1 / 30, freeze: 3 })
    expect(clipDuration(still as never)).toBe(3)
    expect(clipSpeedFactor(still as never)).toBeCloseTo(1 / 90, 12)
  })
})

describe('planFreeze on an overlay (PIP) clip — wave D3, E2', () => {
  function withPip(pips: object[], opts: { locked?: boolean; transitions?: object[] } = {}): EDL {
    const e = edl([clip('a', 0), clip('b', 4)], { transitions: opts.transitions })
    ;(e.tracks as unknown as object[]).push({ id: 'v2', type: 'video', z: 1, label: 'Overlay', clips: pips, locked: opts.locked })
    return e
  }

  it('freezes the SELECTED overlay clip on its own lane', () => {
    const e = withPip([clip('p', 2, { out: 2, speed: 0.5 })])        // footprint 2-6
    expect(planFreeze(e, 'p', 5)).toEqual({ kind: 'freeze', args: { time: 5, clip_id: 'p', track: 'v2' } })
    // the right-clicked clip wins over the selection
    expect(planFreeze(e, 'a', 5, 'p')).toEqual({ kind: 'freeze', args: { time: 5, clip_id: 'p', track: 'v2' } })
  })

  it('refuses when the playhead is off the overlay clip or its lane is locked', () => {
    expect(planFreeze(withPip([clip('p', 2, { out: 2 })]), 'p', 7)).toMatchObject({
      kind: 'refuse', message: expect.stringContaining('Move the playhead over the clip') })
    expect(planFreeze(withPip([clip('p', 2)], { locked: true }), 'p', 3)).toMatchObject({
      kind: 'refuse', message: expect.stringContaining('locked') })
  })

  it('an overlay is placed in LAYOUT time: render time maps through v1 seams', () => {
    // a 1 s dissolve at 4 pulls everything after it 1 s earlier on the render clock
    const e = withPip([clip('p', 5, { out: 2 })], { transitions: [{ at: 4, type: 'fade', duration: 1 }] })
    const p = planFreeze(e, 'p', 4.5)
    expect(p).toMatchObject({ kind: 'freeze', args: { clip_id: 'p', track: 'v2' } })
    if (p.kind === 'freeze') expect(p.args.time).toBeCloseTo(5.5, 9)
  })
})

describe('freezeAtPlayhead', () => {
  it('dispatches ONE freeze_frame and selects the new still', async () => {
    const { freezeAtPlayhead } = await import('./freezeFrame')
    const calls: [string, Record<string, unknown>][] = []
    let selected: string | null = 'b'
    const host = {
      edl: edl([clip('a', 0), clip('b', 4)]), selection: 'b', playhead: 5,
      dispatch: async (tool: string, args: Record<string, unknown>) => {
        calls.push([tool, args]); return { result: { clip_id: 'c_still' }, edl_hash: 'h', op: null }
      },
      setSelection: (id: string | null) => { selected = id },
    }
    const notes: string[] = []
    await freezeAtPlayhead(host, (m) => notes.push(m))
    expect(calls).toEqual([['freeze_frame', { time: 5, clip_id: 'b' }]])
    expect(selected).toBe('c_still')
    expect(notes).toEqual([])
  })

  it('says why and dispatches nothing when it cannot freeze', async () => {
    const { freezeAtPlayhead } = await import('./freezeFrame')
    const calls: unknown[] = []
    const notes: string[] = []
    await freezeAtPlayhead({ edl: edl([clip('a', 0)]), selection: null, playhead: 9,
      dispatch: async (...a: unknown[]) => { calls.push(a); return null }, setSelection: () => {} },
      (m) => notes.push(m))
    expect(calls).toEqual([])
    expect(notes[0]).toMatch(/Move the playhead over a clip/)
  })
})
