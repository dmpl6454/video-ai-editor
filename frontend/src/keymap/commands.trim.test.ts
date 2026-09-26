// QA-115: Q/W and ⌥[ ⌥] trim to the playhead, lift is a real command, and
// Premiere's Delete lifts while Shift+Delete ripples — through the real
// registry, presets and the engine's chord lookup.
import { describe, expect, it, vi } from 'vitest'
import type { Store } from './commands'
import type { EDL } from '../types'
import type { PresetId } from './presets'

vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
const { COMMAND_BY_ID } = await import('./commands')
const { useKeymapStore } = await import('./engine')
const { useToasts } = await import('../toast')

const SRC = '/w/s_1/uploads/a.mp4'
const EDL_: EDL = {
  version: 1, duration: 20, canvas: { w: 1920, h: 1080, fps: 30, bg: '#000' },
  tracks: [
    { id: 'v1', type: 'video', z: 0, clips: [
      { id: 'a', src: SRC, in: 0, out: 10, start: 0 },
      { id: 'b', src: SRC, in: 0, out: 10, start: 10 },
    ] },
    { id: 'v2', type: 'video', z: 5, clips: [{ id: 'p', src: SRC, in: 0, out: 6, start: 2 }] },
  ],
}

function fake(selection: string | null, playhead: number) {
  const calls: { tool: string; args: unknown }[] = []
  const s = {
    edl: EDL_, selection, multiSelection: [] as string[], playhead,
    dispatch: async (tool: string, args: unknown) => { calls.push({ tool, args }); return { edl_hash: 'h' } },
    setPlayhead: (t: number) => { s.playhead = t },
    clearSelection: vi.fn(),
  }
  return { s, calls, store: s as unknown as Store }
}

const chordsFor = (preset: PresetId) => {
  useKeymapStore.getState().setPreset(preset)
  return useKeymapStore.getState().chordToCommand()
}

describe('keyboard trim to playhead (QA-115)', () => {
  it('Q, W, ⌥[ and ⌥] are bound (CapCut) and Premiere/Final Cut bind theirs', () => {
    const cap = chordsFor('capcut')
    expect([cap.KeyQ, cap.KeyW, cap['Alt+BracketLeft'], cap['Alt+BracketRight']])
      .toEqual(['trimStartToPlayhead', 'trimEndToPlayhead', 'trimStartToPlayhead', 'trimEndToPlayhead'])
    const pr = chordsFor('premiere')
    expect([pr.KeyQ, pr.KeyW]).toEqual(['trimStartToPlayhead', 'trimEndToPlayhead'])
    const fc = chordsFor('finalcut')
    expect([fc['Alt+BracketLeft'], fc['Alt+BracketRight']]).toEqual(['trimStartToPlayhead', 'trimEndToPlayhead'])
  })

  it('Q ripples the main-lane head off and parks the playhead on the kept frame', async () => {
    const f = fake(null, 13)
    await COMMAND_BY_ID.trimStartToPlayhead.run(f.store)
    expect(f.calls).toEqual([{ tool: 'trim_clip', args: { clip_id: 'b', in: 3 } }])
    expect(f.s.playhead).toBe(10)
  })

  it('W trims the selected overlay clip and leaves the playhead', async () => {
    const f = fake('p', 5)
    await COMMAND_BY_ID.trimEndToPlayhead.run(f.store)
    expect(f.calls).toEqual([{ tool: 'trim_clip', args: { clip_id: 'p', out: 3 } }])
    expect(f.s.playhead).toBe(5)
  })

  it('a refused trim dispatches nothing and says why', async () => {
    const f = fake('p', 15)
    const before = useToasts.getState().toasts.length
    await COMMAND_BY_ID.trimStartToPlayhead.run(f.store)
    expect(f.calls).toEqual([])
    expect(useToasts.getState().toasts.length).toBe(before + 1)
  })
})

describe('lift vs ripple delete (QA-115)', () => {
  it('Premiere: Delete lifts, Shift+Delete ripples', () => {
    const pr = chordsFor('premiere')
    expect([pr.Delete, pr.Backspace]).toEqual(['lift', 'lift'])
    expect([pr['Shift+Delete'], pr['Shift+Backspace']]).toEqual(['rippleDelete', 'rippleDelete'])
  })

  it('Final Cut and CapCut: Delete ripples, Shift+Delete lifts', () => {
    for (const p of ['finalcut', 'capcut'] as const) {
      const m = chordsFor(p)
      expect([m.Delete, m['Shift+Delete']]).toEqual(['rippleDelete', 'lift'])
    }
  })

  it('lift deletes an overlay clip and clears the selection', async () => {
    const f = fake('p', 0)
    await COMMAND_BY_ID.lift.run(f.store)
    expect(f.calls).toEqual([{ tool: 'ripple_delete', args: { clip_id: 'p' } }])
    expect(f.s.clearSelection).toHaveBeenCalled()
  })

  it('lift on Main video is refused and names the active ripple key', async () => {
    chordsFor('premiere')
    const f = fake('a', 0)
    await COMMAND_BY_ID.lift.run(f.store)
    expect(f.calls).toEqual([])
    const last = useToasts.getState().toasts.at(-1)!
    expect(last.message).toMatch(/magnetic/)
    expect(last.message).toMatch(/Del/)
  })
})
