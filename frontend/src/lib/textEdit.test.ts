// Double-clicking a title in the preview or on the timeline starts editing
// its words, as in CapCut: the clip is selected and the Inspector's Text box
// takes focus with its contents selected — the ⌥T path (keymap/commands
// focusNewText) shares the same routine. Before, a double-click only selected
// the title and focus stayed on the page / timeline canvas, so typing did
// nothing (final QA, editor-ux).
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { focusTextField, textClipId, type TextFieldEnv } from './textEdit'
import type { EDL } from '../types'

const here = dirname(fileURLToPath(import.meta.url))

const edl = {
  version: 1, duration: 10, canvas: { w: 1920, h: 1080, fps: 30 },
  tracks: [
    { id: 'v1', type: 'video', z: 0, clips: [{ id: 'c1', src: 'a.mp4', in: 0, out: 5, start: 0 }] },
    { id: 'text', type: 'text', z: 5, clips: [{ id: 't1', text: 'Hello title', start: 0, end: 3 }] },
    { id: 'captions', type: 'captions', z: 9, clips: [{ id: 'cue', text: 'hi', start: 0, end: 1 }] },
  ],
} as unknown as EDL

function fakeEnv(over: Partial<TextFieldEnv> & { sel?: string | null; openAfter?: number } = {}) {
  let frames = 0
  let focused = false
  let selected = false
  const field = { focus: () => { focused = true }, select: () => { selected = true } }
  const env: TextFieldEnv = {
    frame: async () => { frames++ },
    selection: () => (over.sel === undefined ? 't1' : over.sel),
    inspectorOpen: () => frames >= (over.openAfter ?? 0),
    openInspector: () => {},
    field: () => field,
    timeoutMs: 50,
    ...over,
  }
  return { env, state: () => ({ focused, selected, frames }) }
}

describe('textClipId', () => {
  it('answers a text or caption clip and nothing else', () => {
    expect(textClipId(edl, 't1')).toBe('t1')
    expect(textClipId(edl, 'cue')).toBe('cue')
    expect(textClipId(edl, 'c1')).toBeNull()
    expect(textClipId(edl, 'nope')).toBeNull()
    expect(textClipId(null, 't1')).toBeNull()
  })
})

describe('focusTextField', () => {
  it('focuses and selects the Text box of the selected title', async () => {
    const f = fakeEnv()
    expect(await focusTextField('t1', f.env)).toBe(true)
    expect(f.state()).toMatchObject({ focused: true, selected: true })
  })
  it('opens the Inspector first and waits for it', async () => {
    let opened = 0
    const f = fakeEnv({ openAfter: 3, openInspector: () => { opened++ } })
    expect(await focusTextField('t1', f.env)).toBe(true)
    expect(opened).toBeGreaterThan(0)
  })
  it('gives up when the user selected something else meanwhile', async () => {
    const f = fakeEnv({ sel: 'c1' })
    expect(await focusTextField('t1', f.env)).toBe(false)
    expect(f.state().focused).toBe(false)
  })
})

describe('double-click wiring', () => {
  it('the preview and the timeline both start text editing on a double-click', () => {
    const layer = readFileSync(join(here, '../components/StickerLayer.tsx'), 'utf8')
    expect(layer).toMatch(/addEventListener\('dblclick'/)
    expect(layer).toMatch(/editTextClip\(/)
    const tl = readFileSync(join(here, '../components/Timeline.tsx'), 'utf8')
    expect(tl).toMatch(/onDoubleClick=\{onDoubleClick\}/)
    expect(tl).toMatch(/editTextClip\(/)
  })
})
