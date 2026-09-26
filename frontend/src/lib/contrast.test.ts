import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { aaMinimum, contrastRatio, resolveToken, rootTokens, ruleDeclarations } from './contrast'

// The REAL stylesheets, resolved through their own :root tokens — a token edit
// that drops any pair the UI draws below AA fails here (QA-103).
const STYLES = readFileSync(new URL('../styles.css', import.meta.url), 'utf8')
const TRANSITION_POPOVER = readFileSync(new URL('../components/TransitionPopover.css', import.meta.url), 'utf8')
const TOKENS = rootTokens(STYLES)
const tok = (v: string) => resolveToken(v, TOKENS)

const SURFACES = ['--bg-0', '--bg-1', '--bg-2', '--bg-3'] as const
// Every token the UI uses as a TEXT colour on the dark surfaces.
const TEXT_ON_SURFACES = ['--text', '--text-dim', '--text-faint', '--text-disabled', '--accent', '--accent-2', '--good', '--warn'] as const
// Filled controls: [background token, text token].
const FILLS = [
  ['--accent-fill', '--on-accent'],
  ['--accent-fill-hover', '--on-accent'],
  ['--accent-2-fill', '--on-accent'],
  ['--accent-2-fill-hover', '--on-accent'],
  // Wave C: a panel error well, and THE disabled state.
  ['--error-bg', '--error-text'],
  ['--bg-disabled', '--text-disabled'],
  // A soloed lane's Solo button (components/Timeline .lane-monitor, wave C review).
  ['--warn', '--bg-0'],
] as const

describe('contrast math', () => {
  it('matches the WCAG reference values', () => {
    expect(contrastRatio('#000000', '#ffffff')).toBeCloseTo(21, 5)
    expect(contrastRatio('#ffffff', '#ffffff')).toBeCloseTo(1, 5)
    // The QA-103 measurements, reproduced.
    expect(contrastRatio('#ffffff', '#ff4d6d')).toBeCloseTo(3.21, 2)
    expect(contrastRatio('#5a5a64', '#1d1d22')).toBeCloseTo(2.46, 2)
  })
  it('composites a translucent foreground before measuring', () => {
    // --text-dim at the version badge's old opacity 0.7 over --bg-1: 3.84:1.
    expect(contrastRatio('rgba(155, 155, 165, 0.7)', '#16161a')).toBeCloseTo(3.84, 2)
  })
  it('uses 3:1 only for large text', () => {
    expect(aaMinimum(13)).toBe(4.5)
    expect(aaMinimum(24)).toBe(3)
    expect(aaMinimum(19, true)).toBe(3)
    expect(aaMinimum(19, false)).toBe(4.5)
  })
})

describe('design tokens reach AA (styles.css :root)', () => {
  for (const fg of TEXT_ON_SURFACES) {
    for (const bg of SURFACES) {
      it(`${fg} on ${bg} >= 4.5:1`, () => {
        expect(contrastRatio(tok(`var(${fg})`), tok(`var(${bg})`))).toBeGreaterThanOrEqual(4.5)
      })
    }
  }
  for (const [bg, fg] of FILLS) {
    it(`${fg} on ${bg} >= 4.5:1`, () => {
      expect(contrastRatio(tok(`var(${fg})`), tok(`var(${bg})`))).toBeGreaterThanOrEqual(4.5)
    })
  }
})

describe('filled controls in the stylesheets use a passing pair', () => {
  const cases: [string, string, string][] = [
    // [label, css, selector]
    ['primary button', STYLES, 'button.primary'],
    ['transition Apply', TRANSITION_POPOVER, '.tp-apply'],
  ]
  for (const [label, css, selector] of cases) {
    it(`${label} (${selector}) text >= 4.5:1 on its fill`, () => {
      const d = ruleDeclarations(css, selector)
      expect(d.background, `${selector} has no background`).toBeTruthy()
      expect(d.color, `${selector} has no color`).toBeTruthy()
      expect(contrastRatio(tok(d.color), tok(d.background))).toBeGreaterThanOrEqual(4.5)
    })
  }
  it('primary hover keeps the primary text readable', () => {
    const base = ruleDeclarations(STYLES, 'button.primary')
    const hover = ruleDeclarations(STYLES, 'button.primary:hover:not(:disabled)')
    expect(contrastRatio(tok(base.color), tok(hover.background))).toBeGreaterThanOrEqual(4.5)
  })
  it('a disabled primary is neutral and not faded to illegibility', () => {
    const d = ruleDeclarations(STYLES, 'button.primary:disabled')
    // Not the accent at reduced opacity (1.54:1 in QA-103): its own opaque grey.
    expect(d.opacity).toBe('1')
    expect(tok(d.background)).not.toBe(tok('var(--accent)'))
    expect(tok(d.background)).not.toBe(tok('var(--accent-fill)'))
    expect(contrastRatio(tok(d.color), tok(d.background))).toBeGreaterThanOrEqual(4.5)
  })
})

describe('the one disabled state (wave C)', () => {
  it('is neutral and legible: --text-disabled on --bg-disabled, never a faded accent', () => {
    const d = ruleDeclarations(STYLES, 'button.primary:disabled')
    expect(tok(d.color)).toBe(tok('var(--text-disabled)'))
    expect(tok(d.background)).toBe(tok('var(--bg-disabled)'))
  })
  it('reads as OFF next to live text: dimmer than --text and --text-dim', () => {
    const lum = (c: string) => contrastRatio(tok(c), '#000000')
    expect(lum('var(--text-disabled)')).toBeLessThan(lum('var(--text-dim)'))
    expect(lum('var(--text-disabled)')).toBeLessThan(lum('var(--text)'))
  })
})

describe('an icon-only disabled toolbar button (review RD2)', () => {
  // Split, Freeze frame, Delete and Duplicate in an empty project measured
  // rgb(142,142,152) disabled against rgb(155,155,165) enabled: a 1.17
  // luminance ratio, so they looked usable. An icon carries no text, and
  // WCAG exempts disabled controls, so the icon dims to --icon-disabled.
  it('is clearly dimmer than an enabled icon (luminance ratio >= 2)', () => {
    const d = ruleDeclarations(STYLES, '.timeline-toolbar .tb-icon:disabled')
    const on = ruleDeclarations(STYLES, '.timeline-toolbar .tb-icon')
    const lum = (c: string) => contrastRatio(tok(c), '#000000')
    expect(tok(d.color.replace(/\s*!important/, ''))).toBe(tok('var(--icon-disabled)'))
    expect(lum(on.color) / lum('var(--icon-disabled)')).toBeGreaterThanOrEqual(2)
  })
})

