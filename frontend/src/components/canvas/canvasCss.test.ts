// Final sweep 2: Canvas > Colour's Custom row wrapped as "Cust/om" and
// "#000/000". `.props input:not([type=checkbox]):not([type=radio])` (0,3,1)
// set width:100% over `.canvas-custom input[type=color]` (0,2,1), so the well
// stretched to 181 px (measured in headless Chromium and Playwright WebKit)
// and `.props { overflow-wrap:anywhere }` broke both labels mid-word.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const read = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf-8')
const CANVAS = read('./canvas.css').replace(/\/\*[\s\S]*?\*\//g, '')
const STYLES = read('../../styles.css').replace(/\/\*[\s\S]*?\*\//g, '')

/** [ids, classes/attributes/pseudo-classes, elements] — :not() counts its argument. */
function specificity(sel: string): [number, number, number] {
  let s = sel.replace(/:not\(([^)]*)\)/g, ' $1')
  const ids = (s.match(/#[\w-]+/g) ?? []).length
  s = s.replace(/#[\w-]+/g, ' ')
  const cls = (s.match(/\.[\w-]+|\[[^\]]*\]|:(?!:)[\w-]+/g) ?? []).length
  s = s.replace(/\.[\w-]+|\[[^\]]*\]|::?[\w-]+/g, ' ')
  const els = (s.match(/[a-zA-Z][\w-]*/g) ?? []).length
  return [ids, cls, els]
}
const beats = (a: number[], b: number[]) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2]

function rules(css: string): Array<{ selectors: string[]; body: string }> {
  return [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((m) => ({
    selectors: m[1].split(',').map((x) => x.trim()).filter(Boolean), body: m[2],
  }))
}

describe('Canvas > Colour > Custom row', () => {
  it('the colour well keeps 32 px against the Inspector\'s full-width input rule', () => {
    const inspector = rules(STYLES).flatMap((r) => r.selectors)
      .filter((s) => /^\.props input:not/.test(s) && !/\[type="color"\]/.test(s))
    expect(inspector.length).toBeGreaterThan(0)
    const worst = inspector.map(specificity).sort((a, b) => beats(b, a))[0]
    const well = rules(CANVAS).filter((r) => /width:\s*32px/.test(r.body) && /flex:\s*0 0 32px/.test(r.body))
      .flatMap((r) => r.selectors).filter((s) => /canvas-custom/.test(s) && /type="color"/.test(s))
    expect(well.some((s) => beats(specificity(s), worst) > 0)).toBe(true)
  })
  it('its labels never break mid-word', () => {
    expect(rules(CANVAS).some((r) => r.selectors.includes('.canvas-custom > span')
      && /white-space:\s*nowrap/.test(r.body))).toBe(true)
  })
})
