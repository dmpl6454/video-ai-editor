// QA-046: the REAL store keeps `undoDepth` in step with the server and toasts
// a refused undo, against a mocked api.
import { beforeEach, describe, expect, it, vi } from 'vitest'

const state = { depth: 2, ops: [{ seq: 1 }, { seq: 2 }, { seq: 3 }] }
const toasts: string[] = []

vi.mock('./api', () => ({
  api: {
    getSession: vi.fn(async () => ({ ops: state.ops, name: 'n', redo_available: false,
      undo_depth: state.depth, summary: { edl_hash: 'h' } })),
    getEDL: vi.fn(async () => ({ version: 2, duration: 0, canvas: { w: 1920, h: 1080, fps: 30 }, tracks: [] })),
    dispatch: vi.fn(async (_sid: string, tool: string) => {
      if (tool === 'undo') {
        const ok = state.depth > 0
        if (ok) state.depth -= 1
        return { result: { ok, redo_available: ok, undo_depth: state.depth }, edl_hash: 'h', op: null }
      }
      state.depth += 1
      return { result: {}, edl_hash: 'h', op: { seq: 9 }, undo_depth: state.depth }
    }),
  },
}))
vi.mock('./toast', () => ({
  toast: { info: (m: string) => toasts.push(m), error: (m: string) => toasts.push(m),
    action: (m: string) => toasts.push(m), success: (m: string) => toasts.push(m) },
}))
vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })

const { useStore } = await import('./store')

beforeEach(() => {
  state.depth = 2
  toasts.length = 0
  useStore.setState({ sessionId: 's_x', ops: state.ops as never, undoDepth: 0 })
})

describe('store undo depth', () => {
  it('reads undo_depth from the session and from every dispatch answer', async () => {
    await useStore.getState().refresh()
    expect(useStore.getState().undoDepth).toBe(2)
    await useStore.getState().dispatch('add_marker', { time: 1 })
    expect(useStore.getState().undoDepth).toBe(3)
    await useStore.getState().dispatch('undo')
    expect(useStore.getState().undoDepth).toBe(2)
  })

  it('toasts when the server refuses an undo past the horizon', async () => {
    state.depth = 0
    await useStore.getState().dispatch('undo')
    expect(useStore.getState().undoDepth).toBe(0)
    expect(toasts.some((t) => /past the undo limit/.test(t))).toBe(true)
  })
})
