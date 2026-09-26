// QA-101 sweep: a refused edit's toast is in editor words — never a tool id
// or a clip id. The bodies are the engine's real 400 answers.
import { describe, expect, it, vi } from 'vitest'

const toasts: string[] = []
const bodies: Record<string, string> = {
  set_clip_timing: '{"error":{"code":"BAD_REQUEST","message":"set_clip_timing is for overlays (sticker/text); use trim_clip / move_clip for media clips","request_id":"4e6bd2e28679"}}',
  set_clip_z: '{"error":{"code":"BAD_REQUEST","message":"set_clip_z targets a sticker (per-clip z exists only on stickers; other clip kinds layer by track z)","request_id":"c1ee6b599042"}}',
  ripple_delete: '{"error":{"code":"BAD_REQUEST","message":"clip c_deadbeef0 not found","request_id":"68de160aa114"}}',
}
vi.mock('./api', () => ({
  api: {
    dispatch: vi.fn(async (_sid: string, tool: string) => { throw new Error(`400 Bad Request: ${bodies[tool]}`) }),
  },
}))
vi.mock('./toast', () => ({
  toast: { info: (m: string) => toasts.push(m), error: (m: string) => toasts.push(m),
    action: (m: string) => toasts.push(m), success: (m: string) => toasts.push(m) },
}))
vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
const { useStore } = await import('./store')

describe('a refused edit is said in editor words (QA-101)', () => {
  it.each(Object.keys(bodies))('%s', async (tool) => {
    toasts.length = 0
    useStore.setState({ sessionId: 's_x' })
    expect(await useStore.getState().dispatch(tool, { clip_id: 'c_deadbeef0' })).toBeNull()
    expect(toasts).toHaveLength(1)
    expect(toasts[0]).not.toMatch(/\b[a-z]+_[a-z_]+\b/)            // no tool id
    expect(toasts[0]).not.toMatch(/\b[a-z]{1,3}_[0-9a-f]{6,}\b/)   // no clip id
  })
})
