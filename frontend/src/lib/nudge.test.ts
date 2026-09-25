// QA-022: Alt+→ on a main-track clip must move it one frame or refuse with a
// message — never teleport it past the last clip. Drives the REAL store action
// (nudgeSelection) against a stubbed backend and records what it dispatches.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { EDL } from '../types'
import { planNudge } from './nudge'

vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
const { useStore } = await import('../store')
const { useToasts } = await import('../toast')

const clip = (id: string, start: number, dur: number) =>
  ({ id, src: `/x/${id}.mp4`, in: 0, out: dur, start, transform: {}, effects: [] })

function edl(v1: ReturnType<typeof clip>[], fps = 30, v1Locked = false): EDL {
  return {
    version: 2, duration: 40, canvas: { w: 1080, h: 1920, fps, bg: '#000' },
    tracks: [{ id: 'v1', type: 'video', z: 0, clips: v1, ...(v1Locked ? { locked: true } : {}) }],
  } as unknown as EDL
}

const RBF = [clip('red', 0, 10), clip('blue', 10.005, 10.005), clip('fc30', 20.01, 20.01)]

describe('planNudge', () => {
  it('refuses a one-frame right nudge into the next main-track clip', () => {
    const p = planNudge(edl(RBF), 'blue', 1 / 30)
    expect(p.kind).toBe('refuse')
  })
  it('refuses a one-frame left nudge into the previous clip instead of a silent no-op', () => {
    expect(planNudge(edl(RBF), 'blue', -1 / 30).kind).toBe('refuse')
  })
  it('moves exactly one PROJECT frame when there is room', () => {
    const p = planNudge(edl([clip('a', 0, 2), clip('b', 5, 2)], 25), 'b', 1 / 30)
    expect(p).toEqual({ kind: 'move', clipId: 'b', newStart: 5 + 1 / 25 })
  })
  it('refuses on a locked lane', () => {
    const p = planNudge(edl([clip('a', 0, 2), clip('b', 5, 2)], 30, true), 'b', 1 / 30)
    expect(p.kind).toBe('refuse')
    expect(p.kind === 'refuse' && p.message).toMatch(/locked/)
  })
  it('does nothing at t=0 moving left', () => {
    expect(planNudge(edl([clip('a', 0, 2)]), 'a', -1 / 30).kind).toBe('none')
  })
})

describe('nudgeSelection (real store)', () => {
  const sent: { tool: string; args: Record<string, unknown> }[] = []
  beforeEach(() => {
    sent.length = 0
    useToasts.setState({ toasts: [] })
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      const u = new URL(url, 'http://x')
      if (u.pathname.endsWith('/dispatch') && init?.method === 'POST') {
        sent.push(JSON.parse(String(init.body)))
        return new Response(JSON.stringify({ result: {}, edl_hash: 'h', op: null }), { status: 200 })
      }
      return new Response('{}', { status: 200 })
    }))
  })
  afterEach(() => vi.unstubAllGlobals())

  it('does not dispatch a teleporting move and tells the user why', async () => {
    useStore.setState({ sessionId: 's_n', edl: edl(RBF), selection: 'blue' })
    await useStore.getState().nudgeSelection(1 / 30)
    expect(sent).toEqual([])
    expect(useToasts.getState().toasts.map((t) => t.message).join(' ')).toMatch(/No room to nudge/)
  })

  it('dispatches a one-frame move when the lane has room', async () => {
    useStore.setState({ sessionId: 's_n', edl: edl([clip('a', 0, 2), clip('b', 5, 2)]), selection: 'b' })
    await useStore.getState().nudgeSelection(-1 / 30)
    expect(sent).toHaveLength(1)
    expect(sent[0].tool).toBe('move_clip')
    expect(sent[0].args).toEqual({ clip_id: 'b', new_start: 5 - 1 / 30 })
  })
})
