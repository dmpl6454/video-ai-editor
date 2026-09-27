import { beforeEach, describe, expect, it } from 'vitest'
import {
  BADGE_H, formatSpeed, layoutSpeedBadge, rectsOverlap, rememberPresetLabels, resetPresetLabelsForTests,
  speedBadgeText, type BadgeGeometry,
} from './speedBadge'
import { labelPlate } from './timelineLabels'
import type { AnyClip } from '../types'

const media = (extra: Record<string, unknown>): AnyClip =>
  ({ id: 'c_1', src: '/a.mp4', in: 0, out: 4, start: 0, ...extra }) as unknown as AnyClip

// 5.5 px a glyph at 9 px (the canvas measures; the tests only need monotone)
const measure = (s: string) => s.length * 5.5

beforeEach(() => resetPresetLabelsForTests())

describe('speedBadgeText', () => {
  it('reads a constant speed CapCut-style', () => {
    expect(speedBadgeText(media({ speed: 2 }))).toEqual({ text: '2.0x', kind: 'constant' })
    expect(speedBadgeText(media({ speed: 0.5 }))?.text).toBe('0.5x')
    expect(speedBadgeText(media({ speed: 1.5 }))?.text).toBe('1.5x')
    expect(speedBadgeText(media({ speed: 0.25 }))?.text).toBe('0.25x')
    expect(speedBadgeText(media({ speed: 12 }))?.text).toBe('12x')
  })

  it('has no badge at normal speed, on a still, on text', () => {
    expect(speedBadgeText(media({}))).toBeNull()
    expect(speedBadgeText(media({ speed: 1 }))).toBeNull()
    expect(speedBadgeText(media({ speed: 0 }))).toBeNull()
    expect(speedBadgeText({ id: 't', text: 'Hi', start: 0, end: 2 } as unknown as AnyClip)).toBeNull()
  })

  it('names a curve by its preset, a hand-drawn one "Curve", a hold "Freeze"', () => {
    expect(speedBadgeText(media({ speed: { curve: [[0, 1], [1, 2]], name: 'hero' } }))).toEqual({ text: 'Hero', kind: 'curve' })
    expect(speedBadgeText(media({ speed: { curve: [[0, 1], [1, 2]], name: 'jump_cut' } }))?.text).toBe('Jump Cut')
    expect(speedBadgeText(media({ speed: { curve: [[0, 1], [1, 2]], name: 'custom' } }))?.text).toBe('Curve')
    expect(speedBadgeText(media({ speed: { curve: [[0, 1], [1, 2]] } }))?.text).toBe('Curve')
    expect(speedBadgeText(media({ freeze: 3, out: 4.033 }))).toEqual({ text: 'Freeze', kind: 'freeze' })
  })

  it('uses the server catalog label once it has loaded', () => {
    rememberPresetLabels([{ id: 'flash_in', label: 'Flash In ⚡' }])
    expect(speedBadgeText(media({ speed: { curve: [[0, 5], [1, 1]], name: 'flash_in' } }))?.text).toBe('Flash In ⚡')
  })

  it('formats', () => {
    expect([2, 0.5, 0.25, 3, 1.25, 0.1].map(formatSpeed)).toEqual(['2.0x', '0.5x', '0.25x', '3.0x', '1.25x', '0.1x'])
  })
})

describe('layoutSpeedBadge', () => {
  const row = 100
  const geo = (clipX: number, clipW: number, extra: Partial<BadgeGeometry> = {}): BadgeGeometry =>
    ({ clipX, clipW, viewLeft: 80, viewRight: 1000, rectTop: row + 4, ...extra })

  it('sits in the top-right corner of a wide clip with its icon and text', () => {
    const b = layoutSpeedBadge('2.0x', geo(100, 400), measure)!
    expect(b.icon).toBe(true)
    expect(b.text).toBe('2.0x')
    expect(b.x + b.w).toBe(100 + 400 - 3)
    expect(b.y).toBe(row + 7)
    expect(b.h).toBe(BADGE_H)
  })

  it('degrades with the zoom: icon + text, then text, then the icon, then nothing', () => {
    // "2.0x" is 22 px here: icon + text 38 px, text 28 px, icon 14 px; the room is w - 6
    const tiers = [300, 44, 36, 25, 12].map((w) => layoutSpeedBadge('2.0x', geo(100, w), measure))
    expect(tiers.map((t) => t && [t.icon, t.text])).toEqual([
      [true, '2.0x'], [true, '2.0x'], [false, '2.0x'], [true, ''], null,
    ])
    // whatever is drawn fits inside the clip
    for (const [i, w] of [300, 44, 36, 25].entries()) {
      const t = tiers[i]!
      expect(t.x).toBeGreaterThanOrEqual(100)
      expect(t.x + t.w).toBeLessThanOrEqual(100 + w)
    }
  })

  it('never overlaps the clip name, which ends before it', () => {
    for (const w of [60, 90, 140, 400]) {
      const b = layoutSpeedBadge('Jump Cut', geo(100, w), measure)
      if (!b) continue
      const nameX = 100 + 6
      const nameW = Math.max(0, Math.min(100 + w - nameX - 8, b.nameMaxRight - nameX))
      if (nameW <= 0) continue                   // the name is dropped on a narrow clip
      expect(rectsOverlap(labelPlate(nameX, nameW, row, 36), b)).toBe(false)
      expect(nameX + nameW).toBeLessThanOrEqual(b.x - 4)
    }
  })

  it('is sticky to the visible right edge of a clip scrolled past the view', () => {
    const b = layoutSpeedBadge('Hero', geo(100, 5000), measure)!
    expect(b.x + b.w).toBe(1000 - 3)
  })

  it('keeps clear of a transition bowtie on the clip tail', () => {
    const plain = layoutSpeedBadge('2.0x', geo(100, 300), measure)!
    const bowtie = layoutSpeedBadge('2.0x', geo(100, 300, { tailReserve: 10 }), measure)!
    expect(plain.x - bowtie.x).toBe(10)
  })

  it('reads at every zoom: a drawn text is never clipped', () => {
    for (let w = 4; w < 200; w += 1) {
      const b = layoutSpeedBadge('0.25x', geo(100, w), measure)
      if (b?.text) expect(b.w).toBeGreaterThanOrEqual(3 + measure(b.text) + 3)
    }
  })
})
