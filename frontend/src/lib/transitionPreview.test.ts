// Every preview kind the catalog can hand a tile yields a complete variable
// set for the keyframes in transitionsPanel.css — a clip kind without both
// endpoints, or a move kind without a translate, would be a tile that never
// animates and never says why.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it, vi } from 'vitest'

vi.mock('../api', () => ({ api: { dispatch: vi.fn() } }))
const { normalizeCatalog } = await import('./transitionCatalog')
const { previewClass, previewStyle } = await import('./transitionPreview')

const RAW = JSON.parse(readFileSync(fileURLToPath(new URL('./__fixtures__/list_transitions.json', import.meta.url)), 'utf-8'))
const CSS = readFileSync(fileURLToPath(new URL('../components/transitionsPanel.css', import.meta.url)), 'utf-8')

const CLIP_KINDS = ['wipe', 'diag', 'iris', 'iris-close', 'crop', 'box', 'diamond', 'doors', 'doors-close', 'curtain', 'curtain-close', 'wave']
const MOVE_KINDS = ['slide', 'cover', 'reveal', 'whip', 'wind', 'slice']

describe('previewStyle over the recorded catalog', () => {
  const cat = normalizeCatalog(RAW)

  it('gives every look a duration and the variables its kind needs', () => {
    for (const e of cat.entries) {
      const v = previewStyle(e.preview)
      expect(v['--tp-dur']).toMatch(/^\d(\.\d+)?s$/)
      if (CLIP_KINDS.includes(e.preview.kind)) {
        expect(v['--cp-from'], e.name).toBeTruthy()
        expect(v['--cp-to'], e.name).toBeTruthy()
        // Interpolable pairs only: the same shape function on both ends.
        expect(v['--cp-from']!.split('(')[0]).toBe(v['--cp-to']!.split('(')[0])
      }
      if (MOVE_KINDS.includes(e.preview.kind)) {
        expect(v['--tx-from'], e.name).toMatch(/^translate[XY]\(-?100%\)$/)
        expect(v['--tx-out'], e.name).toMatch(/^translate[XY]\(-?100%\)$/)
      }
    }
  })

  it('has a keyframe rule in the stylesheet for every kind class it emits', () => {
    const kinds = new Set(cat.entries.map((e) => e.preview.kind))
    for (const k of kinds) expect(CSS, k).toContain(`.kind-${k}`)
  })

  it('directions drive the endpoints, not just the class', () => {
    expect(previewStyle({ kind: 'wipe', dir: 'left' })['--cp-from']).toBe('inset(0 0 0 100%)')
    expect(previewStyle({ kind: 'wipe', dir: 'down' })['--cp-from']).toBe('inset(0 0 100% 0)')
    expect(previewStyle({ kind: 'slide', dir: 'right' })['--tx-from']).toBe('translateX(-100%)')
    expect(previewStyle({ kind: 'slide', dir: 'up' })['--tx-out']).toBe('translateY(-100%)')
    expect(previewStyle({ kind: 'squeeze', dir: 'up' })['--sq']).toBe('scaleY(0)')
    expect(previewStyle({ kind: 'blinds', dir: null })['--mask-angle']).toBe('90deg')
    expect(previewStyle({ kind: 'slice', dir: 'up' })['--mask-angle']).toBe('0deg')
  })

  it('puts the outgoing layer on top only for the kinds that animate it away', () => {
    expect(previewClass({ kind: 'iris-close', dir: null })).toBe('tp-prev kind-iris-close a-top')
    expect(previewClass({ kind: 'reveal', dir: 'left' })).toBe('tp-prev kind-reveal dir-left a-top')
    expect(previewClass({ kind: 'slide', dir: 'left' })).toBe('tp-prev kind-slide dir-left')
    expect(previewClass({ kind: 'fade', dir: null })).toBe('tp-prev kind-fade')
  })
})
