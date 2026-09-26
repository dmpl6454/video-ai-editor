import * as lucide from 'lucide-react'
import { describe, expect, it } from 'vitest'
import { ICON_SIZE, ICONS, iconNode } from './icons'

// THE icon set (QA-125) is lucide-react and nothing else: every name maps to a
// component the pinned library exports, and the canvas path (the timeline's
// lane padlock) draws that same component's geometry.
describe('app icon set', () => {
  const exported = new Set(Object.values(lucide))

  it('maps every name to a lucide-react component', () => {
    for (const [name, C] of Object.entries(ICONS)) {
      expect(exported.has(C as never), `${name} is not a lucide-react export`).toBe(true)
    }
  })

  it('draws controls at 16 px', () => {
    expect(ICON_SIZE).toBe(16)
  })

  it('hands the canvas the lucide Lock geometry, not a hand-drawn padlock', () => {
    const node = iconNode('lock')
    expect(node.map(([tag]) => tag)).toEqual(['rect', 'path'])
    expect(node[0][1]).toMatchObject({ x: '3', y: '11', width: '18', height: '11' })
    expect(node[1][1].d).toBe('M7 11V7a5 5 0 0 1 10 0v4')
  })

  it('has geometry for every icon (the canvas helper never draws an empty glyph)', () => {
    for (const name of Object.keys(ICONS) as (keyof typeof ICONS)[]) {
      expect(iconNode(name).length, name).toBeGreaterThan(0)
    }
  })
})
