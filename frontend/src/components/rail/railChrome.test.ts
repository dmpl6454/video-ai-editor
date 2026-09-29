// The rail's pure helpers and its stylesheet's contrast: focus rescue's
// "lost" rule, the tooltip's placement, and the rail colours measured through
// the real :root tokens.
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { contrastRatio, resolveToken, rootTokens, ruleDeclarations } from '../../lib/contrast'
import { isFocusLost } from './focusRescue'
import { pointInRect, tipPosition } from './railTip'

describe('isFocusLost (LEFT_RAIL_SPEC §5.3)', () => {
  const body = { tag: 'body' }
  const el = (o: { connected?: boolean; hidden?: boolean; rects?: number } = {}) => ({
    isConnected: o.connected ?? true,
    closest: (sel: string) => (sel === '[hidden]' && o.hidden ? {} : null),
    getClientRects: () => ({ length: o.rects ?? 1 }),
  })
  it('counts nothing focused, or <body>, as lost (Chromium blurs to body)', () => {
    expect(isFocusLost(null, body)).toBe(true)
    expect(isFocusLost(body as never, body)).toBe(true)
  })
  it('counts an element inside [hidden], detached or unrendered as lost (WebKit keeps it active)', () => {
    expect(isFocusLost(el({ hidden: true }), body)).toBe(true)
    expect(isFocusLost(el({ connected: false }), body)).toBe(true)
    expect(isFocusLost(el({ rects: 0 }), body)).toBe(true)
  })
  it('leaves a visible, connected element alone', () => {
    expect(isFocusLost(el(), body)).toBe(false)
  })
})

describe('tipPosition (§2.6)', () => {
  const vp = { width: 1024, height: 768 }
  const tip = { width: 200, height: 24 }
  it('puts a rail tip to the right of the control, vertically centred', () => {
    const r = { left: 0, right: 48, top: 100, bottom: 144, width: 48, height: 44 }
    expect(tipPosition(r, tip, vp, 'right')).toEqual({ x: 56, y: 110 })
  })
  it('puts other tips below, centred, and clamps them inside the viewport', () => {
    const r = { left: 990, right: 1018, top: 60, bottom: 88, width: 28, height: 28 }
    expect(tipPosition(r, tip, vp, 'below')).toEqual({ x: 1024 - 200 - 8, y: 94 })
    const low = { left: 10, right: 40, top: 760, bottom: 790, width: 30, height: 30 }
    expect(tipPosition(low, tip, vp, 'below')).toEqual({ x: 8, y: 768 - 24 - 8 })
  })
})

describe('rail colours reach their contrast minimums (tokens only)', () => {
  const STYLES = readFileSync(new URL('../../styles.css', import.meta.url), 'utf8')
  const RAIL = readFileSync(new URL('./rail.css', import.meta.url), 'utf8')
  const TOKENS = rootTokens(STYLES)
  const tok = (v: string) => resolveToken(v, TOKENS)
  const decl = (sel: string) => ruleDeclarations(RAIL, sel)

  it('uses no raw colour: every colour in rail.css is a token', () => {
    const body = RAIL.replace(/\/\*[\s\S]*?\*\//g, '')
    const raw = [...body.matchAll(/(?:^|[\s:,(])(#[0-9a-f]{3,8}\b|rgb\([^)]*\)|hsl\([^)]*\))/gi)].map((m) => m[1])
    // The one allowed literal is the tooltip's drop shadow (black at 50 %).
    expect(raw.filter((c) => c !== 'rgba(0, 0, 0, 0.5)')).toEqual([])
  })
  it('idle label: --text-dim on the rail (--bg-0) >= 4.5:1', () => {
    expect(tok(decl('.rail-item').color)).toBe(tok('var(--text-dim)'))
    expect(contrastRatio(tok(decl('.rail-item').color), tok(decl('.rail').background))).toBeGreaterThanOrEqual(4.5)
  })
  it('selected label on its chip >= 4.5:1', () => {
    const fg = tok(decl('.rail-item[aria-selected="true"]').color)
    const bg = tok(decl('.rail-item[aria-selected="true"] .rail-chip').background)
    expect(contrastRatio(fg, bg)).toBeGreaterThanOrEqual(4.5)
  })
  it('the selection indicator is a >= 3:1 non-text mark on the rail', () => {
    expect(contrastRatio(tok(decl('.rail-indicator').background), tok(decl('.rail').background))).toBeGreaterThanOrEqual(3)
  })
  it('tooltip text and its key cap >= 4.5:1 on the tip', () => {
    const bg = tok(decl('.rail-tip').background)
    expect(contrastRatio(tok(decl('.rail-tip').color), bg)).toBeGreaterThanOrEqual(4.5)
    expect(contrastRatio(tok(decl('.rail-tip kbd').color), bg)).toBeGreaterThanOrEqual(4.5)
  })
  it('the panel title and chord hint >= 4.5:1 on the panel (--bg-1)', () => {
    const bg = tok(decl('.tool-panel').background)
    expect(contrastRatio(tok(decl('.tool-panel-head h2').color), bg)).toBeGreaterThanOrEqual(4.5)
    expect(contrastRatio(tok(decl('.tool-panel-kbd').color), bg)).toBeGreaterThanOrEqual(4.5)
  })
})

describe('the tip never takes a click (final QA)', () => {
  // It sits over the top row of the open tool panel (Upload PNG sticker…,
  // Add music…); with pointer-events it swallowed the first click there.
  const RAIL = readFileSync(new URL('./rail.css', import.meta.url), 'utf8')
  const TIP = readFileSync(new URL('./RailTooltip.tsx', import.meta.url), 'utf8')
  it('is pointer-events: none', () => {
    expect(ruleDeclarations(RAIL, '.rail-tip')['pointer-events']).toBe('none')
  })
  it('stays hoverable by geometry, not by pointer events on the tip', () => {
    expect(TIP).not.toMatch(/tip\.addEventListener\('pointer(enter|leave)'/)
    expect(TIP).toMatch(/pointInRect\(e\.clientX, e\.clientY, tip\.getBoundingClientRect\(\)\)/)
    expect(TIP).toMatch(/document\.addEventListener\('pointermove', onMove/)
  })
  it('pointInRect is half-open', () => {
    const r = { left: 71, top: 115, right: 312, bottom: 142 }
    expect(pointInRect(71, 115, r)).toBe(true)
    expect(pointInRect(200, 130, r)).toBe(true)
    expect(pointInRect(312, 130, r)).toBe(false)
    expect(pointInRect(200, 142, r)).toBe(false)
    expect(pointInRect(70, 130, r)).toBe(false)
  })
})
