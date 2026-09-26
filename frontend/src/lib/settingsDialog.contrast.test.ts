// Every text colour the Settings dialog draws, measured on the surface it sits
// on, through the REAL stylesheets' tokens (QA-103's contract, extended to the
// new dialog). The dialog is --bg-1; its rows are --bg-2.
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { contrastRatio, resolveToken, rootTokens, ruleDeclarations } from './contrast'

const STYLES = readFileSync(new URL('../styles.css', import.meta.url), 'utf8')
const SETTINGS = readFileSync(new URL('../components/settingsDialog.css', import.meta.url), 'utf8')
const TOKENS = rootTokens(STYLES)
const tok = (v: string) => resolveToken(v, TOKENS)
const DIALOG_BG = tok(ruleDeclarations(STYLES, '.dialog').background)

// [selector, the surface it is drawn on]
const TEXT: [string, string][] = [
  ['.settings-section h3', DIALOG_BG],
  ['.settings-subhead', DIALOG_BG],
  ['.settings-help', DIALOG_BG],
  ['.settings-problem', DIALOG_BG],
  ['.settings-status', DIALOG_BG],
  ['.settings-status[data-tone="ok"]', DIALOG_BG],
  ['.settings-status[data-tone="warn"]', DIALOG_BG],
  ['.settings-result', DIALOG_BG],
  ['.settings-result[data-tone="ok"]', DIALOG_BG],
  ['.settings-result[data-tone="warn"]', DIALOG_BG],
  ['.settings-cache-line', DIALOG_BG],
  ['.settings-row-name', 'row'],
  ['.settings-row-side', 'row'],
  ['.settings-row-meta', 'row'],
  ['.settings-key-row input', 'self'],
]

describe('Settings dialog text reaches AA', () => {
  const rowBg = tok(ruleDeclarations(SETTINGS, '.settings-row').background)
  for (const [selector, surface] of TEXT) {
    it(`${selector} >= 4.5:1`, () => {
      const d = ruleDeclarations(SETTINGS, selector)
      expect(d.color, `${selector} sets no color`).toBeTruthy()
      const bg = surface === 'row' ? rowBg : surface === 'self' ? tok(d.background) : surface
      expect(contrastRatio(tok(d.color), bg)).toBeGreaterThanOrEqual(4.5)
    })
  }
  it('uses tokens, never a literal colour', () => {
    const literals = SETTINGS.replace(/\/\*[\s\S]*?\*\//g, '').match(/#[0-9a-f]{3,8}\b|rgba?\(/gi)
    expect(literals).toBeNull()
  })
})
