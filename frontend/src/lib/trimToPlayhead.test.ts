// QA-115: trim to the playhead and lift, decided from the real EDL shapes.
import { describe, expect, it } from 'vitest'
import type { EDL } from '../types'
import { planLift, planTrimToPlayhead } from './trimToPlayhead'

const SRC = '/w/s_1/uploads/a.mp4'

function edl(extra: Partial<EDL> = {}, v1Locked = false): EDL {
  return {
    version: 1, duration: 20, canvas: { w: 1920, h: 1080, fps: 30, bg: '#000' },
    tracks: [
      { id: 'v1', type: 'video', z: 0, clips: [
        { id: 'a', src: SRC, in: 0, out: 10, start: 0 },
        { id: 'b', src: SRC, in: 20, out: 30, start: 10 },
      ], ...(v1Locked ? { locked: true } : {}) } as EDL['tracks'][number],
      { id: 'v2', type: 'video', z: 5, clips: [
        { id: 'p', src: SRC, in: 2, out: 8, start: 4 },
      ] },
      { id: 'tx_hook', type: 'text', z: 20, clips: [
        { id: 't', text: 'Hook', start: 1, end: 6 },
      ] },
    ],
    ...extra,
  }
}

describe('planTrimToPlayhead', () => {
  it('Q with nothing selected trims the Main video clip under the playhead and ripples', () => {
    const plan = planTrimToPlayhead(edl(), [], 13, 'start')
    expect(plan).toEqual({ kind: 'dispatch', tool: 'trim_clip', args: { clip_id: 'b', in: 23 }, playheadAfter: 10 })
  })

  it('W trims the end of the selected clip at the playhead (source time via speed)', () => {
    const e = edl()
    ;(e.tracks[0].clips[0] as unknown as { speed: number }).speed = 2   // a: 0-10 src @2x = 0-5 on the timeline
    e.tracks[0].clips[1].start = 5
    const plan = planTrimToPlayhead(e, ['a'], 3, 'end')
    expect(plan).toEqual({ kind: 'dispatch', tool: 'trim_clip', args: { clip_id: 'a', out: 6 } })
  })

  it('on a speed CURVE the source time is the curve\'s integral, not the mean speed', () => {
    // a: Hero over 0-10 s of source fills 12.658 s (edl/speed_curve.py);
    // the server trims a curve clip to exactly the frame it showed there.
    const e = edl()
    const hero = [[0, 1], [0.3, 1], [0.42, 0.25], [0.58, 0.25], [0.7, 1], [1, 1]]
    ;(e.tracks[0].clips[0] as unknown as { speed: unknown }).speed = { curve: hero, name: 'hero' }
    e.tracks[0].clips[1].start = 12.658227848101266
    expect(planTrimToPlayhead(e, ['a'], 6, 'end'))
      .toEqual({ kind: 'dispatch', tool: 'trim_clip', args: { clip_id: 'a', out: 4.917721518987341 } })
    expect(planTrimToPlayhead(e, ['a'], 3, 'start'))
      .toMatchObject({ kind: 'dispatch', tool: 'trim_clip', args: { clip_id: 'a', in: 3 } })
  })

  it('a head trim off the main lane keeps the kept frames in place (move_start)', () => {
    const plan = planTrimToPlayhead(edl(), ['p'], 6, 'start')
    expect(plan).toEqual({ kind: 'dispatch', tool: 'trim_clip', args: { clip_id: 'p', in: 4, move_start: true } })
  })

  it('text and stickers retime their window', () => {
    expect(planTrimToPlayhead(edl(), ['t'], 2, 'start'))
      .toEqual({ kind: 'dispatch', tool: 'set_clip_timing', args: { clip_id: 't', start: 2 } })
    expect(planTrimToPlayhead(edl(), ['t'], 5, 'end'))
      .toEqual({ kind: 'dispatch', tool: 'set_clip_timing', args: { clip_id: 't', end: 5 } })
  })

  it('refuses, with the reason, instead of doing nothing silently', () => {
    expect(planTrimToPlayhead(edl(), ['p'], 15, 'start')).toMatchObject({ kind: 'refuse', message: expect.stringContaining("isn't inside the selected clip") })
    expect(planTrimToPlayhead(edl(), [], 25, 'end')).toMatchObject({ kind: 'refuse', message: expect.stringContaining('No clip under the playhead') })
    // On the edge: less than a frame to remove.
    expect(planTrimToPlayhead(edl(), ['b'], 10 + 1 / 60, 'start')).toMatchObject({ kind: 'refuse' })
    expect(planTrimToPlayhead(edl({}, true), [], 3, 'start')).toMatchObject({ kind: 'refuse', message: expect.stringContaining('locked') })
    expect(planTrimToPlayhead(null, [], 3, 'start')).toMatchObject({ kind: 'refuse' })
  })

  it('decodes the playhead through a transition like a split does', () => {
    // A 1 s dissolve at the a|b cut pulls b one second left: render 12 is layout 13.
    const e = edl({})
    ;(e.tracks[0] as unknown as { transitions: unknown[] }).transitions = [{ at: 10, type: 'fade', duration: 1 }]
    const plan = planTrimToPlayhead(e, ['b'], 12, 'start')
    expect(plan).toMatchObject({ kind: 'dispatch', tool: 'trim_clip', args: { clip_id: 'b', in: 23 } })
  })
})

describe('planLift', () => {
  it('deletes overlay-lane clips and nothing else moves (ripple ops only close v1)', () => {
    expect(planLift(edl(), ['p'], '⇧Del')).toEqual({ kind: 'dispatch', tool: 'ripple_delete', args: { clip_id: 'p' } })
    expect(planLift(edl(), ['p', 't', 'p'], '⇧Del')).toEqual({ kind: 'dispatch', tool: 'bulk_delete', args: { clip_ids: ['p', 't'] } })
  })

  it('refuses a Main video clip with the key that ripples instead', () => {
    const plan = planLift(edl(), ['p', 'a'], '⇧Del')
    expect(plan.kind).toBe('refuse')
    expect(plan).toMatchObject({ message: expect.stringContaining('magnetic') })
    expect(plan).toMatchObject({ message: expect.stringContaining('⇧Del') })
  })

  it('refuses an empty selection and a locked lane', () => {
    expect(planLift(edl(), [], 'x').kind).toBe('refuse')
    const e = edl()
    ;(e.tracks[1] as unknown as { locked: boolean }).locked = true
    expect(planLift(e, ['p'], 'x')).toMatchObject({ kind: 'refuse', message: expect.stringContaining('locked') })
  })
})
