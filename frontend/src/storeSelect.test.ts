// QA-116: ⌘A selects everything editable — text included — and the timeline's
// box selection replaces or adds to the selection through one store action.
import { describe, expect, it, vi, beforeEach } from 'vitest'
import type { EDL } from './types'

vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
const { useStore } = await import('./store')

const SRC = '/w/s1/uploads/a.mp4'
const EDL_: EDL = {
  version: 1, duration: 10, canvas: { w: 1080, h: 1920, fps: 30, bg: '#000' },
  tracks: [
    { id: 'v1', type: 'video', z: 0, clips: [{ id: 'm1', src: SRC, in: 0, out: 10, start: 0 }] },
    { id: 'tx_hook', type: 'text', z: 20, clips: [{ id: 't1', text: 'Hook', start: 0, end: 3 }] },
    { id: 'stickers', type: 'sticker', z: 30,
      clips: [{ id: 's1', src: '/w/s1/stickers/x.png', start: 1, end: 2 } as unknown as EDL['tracks'][number]['clips'][number]] },
    { id: 'captions', type: 'captions', z: 40, clips: [{ id: 'c1', text: 'hi', start: 0, end: 1 }],
      locked: true } as unknown as EDL['tracks'][number],
  ],
}

beforeEach(() => useStore.setState({ edl: EDL_, selection: null, multiSelection: [] }))

const selected = () => {
  const s = useStore.getState()
  return [s.selection, ...s.multiSelection].filter(Boolean)
}

describe('Select All (QA-116)', () => {
  it('selects media, text and stickers — but not a locked lane', () => {
    useStore.getState().selectAll()
    expect(selected()).toEqual(['m1', 't1', 's1'])
  })
})

describe('selectClips — box selection (QA-116)', () => {
  it('replaces the selection', () => {
    useStore.setState({ selection: 'm1', multiSelection: [] })
    useStore.getState().selectClips(['t1', 's1'])
    expect(selected()).toEqual(['t1', 's1'])
  })

  it('adds to it when additive, without duplicates', () => {
    useStore.setState({ selection: 'm1', multiSelection: ['t1'] })
    useStore.getState().selectClips(['t1', 's1'], true)
    expect(selected()).toEqual(['m1', 't1', 's1'])
  })

  it('an empty box clears a non-additive selection', () => {
    useStore.setState({ selection: 'm1', multiSelection: [] })
    useStore.getState().selectClips([])
    expect(selected()).toEqual([])
  })
})
