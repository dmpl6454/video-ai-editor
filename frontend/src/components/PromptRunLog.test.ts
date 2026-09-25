// QA-017: the run log's verified checks must read as prose — no raw JSON — and
// a finished run must fold to one row instead of holding the preview's height.
//
// The checks below are VERBATIM from a real "make it 9:16" run against the
// backend (prompt_run.json → verify). The old formatter printed the reframe
// check as {"src_changed":true,…} and the safe-zone target as
// expected {"y_min":0.1,…}; in the grid those nowrap strings crushed the label
// column to one character per line.
//
// The log is rendered through react-dom/server. zustand's server snapshot is a
// store's INITIAL state, so the prompt store's initial state is seeded here
// before the component module loads — the real component, the real store.
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import type { VerifyCheck } from '../lib/promptEvents'

vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })

const CHECKS: VerifyCheck[] = [
  { check: 'canvas_aspect', human: 'the canvas has the requested aspect', pass: true, measured: '9:16', expected: '9:16', headline: true },
  { check: 'reframe_effective', human: 'the reframe changed the picture', pass: true,
    measured: { src_changed: true, skipped: false, all_cover: true }, expected: 'reframed or fit=cover', headline: true },
  { check: 'no_letterbox', human: 'no black bars', pass: true, measured: 0, expected: 0, headline: true, unit: 'letterboxed clips' },
  { check: 'overlays_inside_safe_zone', human: 'text stays clear of the platform UI', pass: null, measured: null,
    expected: { y_min: 0.1, y_max: 0.78, x_max: 0.85, caption_y: 0.76, lower_third_y: 0.74 }, headline: true,
    detail: 'no overlays to place' },
]

const { humanValue, checkValues, checksHeadline } = await import('../lib/checkProse')

describe('check values as prose', () => {
  it('never prints JSON for any real check', () => {
    for (const c of CHECKS) {
      const line = checkValues(c) ?? ''
      expect(line, c.check).not.toMatch(/[{}"[\]]/)
    }
  })

  it('spells object measurements out as key: value words', () => {
    expect(checkValues(CHECKS[1])).toBe('measured src changed: yes, skipped: no, all cover: yes · expected reframed or fit=cover')
    expect(checkValues(CHECKS[3])).toBe(
      'not measured · target y min: 0.10, y max: 0.78, x max: 0.85, caption y: 0.76, lower third y: 0.74')
  })

  it('says "as expected" instead of printing the same value twice', () => {
    expect(checkValues(CHECKS[0])).toBe('9:16 · as expected')
    expect(checkValues(CHECKS[2])).toBe('0 letterboxed clips · as expected')
  })

  it('formats the other shapes a check can carry', () => {
    expect(humanValue([1.5, 2], 's')).toBe('1.50, 2 s')
    expect(humanValue({ lufs: { integrated: -14.2 } })).toBe('lufs (integrated: -14.2)')
    expect(humanValue([])).toBe('none')
    expect(humanValue(undefined)).toBe('—')
  })

  it('headlines a run', () => {
    expect(checksHeadline({ passed: 3, total: 3, checks: CHECKS })).toBe('3 of 3 checks passed')
    expect(checksHeadline({ passed: 1, total: 2, checks: [{ ...CHECKS[0], pass: false }, CHECKS[1]] }))
      .toBe('1 of 2 checks passed · 1 failed')
  })
})

describe('the rendered run log', () => {
  const seed = async (status: 'done' | 'error' | 'running') => {
    vi.resetModules()
    const { usePromptStore } = await import('../lib/promptStore')
    const init = usePromptStore.getInitialState()
    Object.assign(init, {
      status, prompt: 'make it 9:16', runId: 'r1', opSeen: true, logOpen: true,
      plan: null, steps: [{ index: 0, tool: 'auto_reframe', status: 'ok', summary: 'reframed 1 clip' }],
      verify: { type: 'verify', plan_id: 'p', checks: CHECKS, passed: 3, total: 3, rendered: false },
    })
    const { PromptRunLog } = await import('./PromptRunLog')
    return renderToStaticMarkup(createElement(PromptRunLog))
  }

  it('folds a finished run to one summary row with Details / Undo / Clear', async () => {
    const html = await seed('done')
    expect(html).toContain('prompt-log-summary')
    expect(html).toContain('3 of 3 checks passed')
    expect(html).toMatch(/>Details</)
    expect(html).toMatch(/>Undo</)
    expect(html).not.toContain('prompt-checks')          // the table is folded away
  })

  it('shows a failed run in full, with prose values and no JSON', async () => {
    const html = await seed('error')
    expect(html).toContain('prompt-checks')
    const vals = [...html.matchAll(/class="vals">([^<]*)</g)].map((m) => m[1])
    expect(vals).toHaveLength(4)
    for (const v of vals) expect(v).not.toMatch(/[{}[\]]|&quot;/)
    expect(html).not.toContain('class="val"')             // the old nowrap columns are gone
  })
})
