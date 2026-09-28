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
    // A range the verifier sends as numbers (never "[11.5, 30.5]").
    expect(humanValue({ sessions: 3, duration: { min: 11.5, max: 30.5, unit: 's' } }))
      .toBe('sessions: 3, duration: 11.5–30.5 s')
    expect(humanValue({ min: 0, max: null, unit: 's' })).toBe('at least 0 s')
    expect(humanValue(undefined)).toBe('—')
  })

  it('headlines a run', () => {
    expect(checksHeadline({ passed: 3, total: 3, checks: CHECKS })).toBe('3 of 3 checks passed')
    expect(checksHeadline({ passed: 1, total: 2, checks: [{ ...CHECKS[0], pass: false }, CHECKS[1]] }))
      .toBe('1 of 2 checks passed · 1 failed')
  })
})

describe('the rendered run log', () => {
  const seed = async (status: 'done' | 'error' | 'running', after?: string) => {
    vi.resetModules()
    const { usePromptStore } = await import('../lib/promptStore')
    const init = usePromptStore.getInitialState()
    Object.assign(init, {
      status, prompt: 'make it 9:16', runId: 'r1', opSeen: true, logOpen: true,
      // the prompt's op is the newest state of the timeline (Final QA: Undo
      // is offered only then — lib/promptUndo)
      opRef: { seq: 3, hashAfter: 'h3' },
      plan: null, steps: [{ index: 0, tool: 'auto_reframe', status: 'ok', summary: 'reframed 1 clip' }],
      verify: { type: 'verify', plan_id: 'p', checks: CHECKS, passed: 3, total: 3, rendered: false },
    })
    const { useStore } = await import('../store')
    Object.assign(useStore.getInitialState(), { edlHash: after ?? 'h3' })
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

  it('withdraws Undo once a later edit is the newest state (it would undo that instead)', async () => {
    const html = await seed('done', 'h4')
    expect(html).toMatch(/>Details</)
    expect(html).not.toMatch(/>Undo</)
  })

  it('shows a failed run in full, with prose values and no JSON', async () => {
    const html = await seed('error')
    expect(html).toContain('prompt-checks')
    const vals = [...html.matchAll(/class="vals">([^<]*)</g)].map((m) => m[1])
    expect(vals).toHaveLength(4)
    for (const v of vals) expect(v).not.toMatch(/[{}[\]]|&quot;/)
    expect(html).not.toContain('class="val"')             // the old nowrap columns are gone
  })

  it('labels steps in editor language, the tool id only as a hover title (QA-101)', async () => {
    const html = await seed('error')
    const visible = html.replace(/title="[^"]*"/g, '')
    expect(visible).toContain('>Reframe<')
    expect(visible).not.toContain('auto_reframe')
    expect(html).not.toContain('<code')
  })
})

describe('the run log speaks editor language and says what happened (wave-B review)', () => {
  const seedWith = async (state: Record<string, unknown>) => {
    vi.resetModules()
    const { usePromptStore } = await import('../lib/promptStore')
    Object.assign(usePromptStore.getInitialState(), {
      prompt: 'p', runId: 'r2', opSeen: false, logOpen: true, verify: null, reply: '', ...state,
    })
    const { PromptRunLog } = await import('./PromptRunLog')
    return renderToStaticMarkup(createElement(PromptRunLog))
  }

  it('labels a step by its tool title, not the planner rationale; summaries lose ids and (s)', async () => {
    const html = await seedWith({
      status: 'error',
      plan: { steps: [{ tool: 'auto_reframe', args: {}, why: 'fill the frame — auto_reframe may skip a clip and Clip.fit defaults to contain' }] },
      steps: [{ index: 0, tool: 'auto_reframe', status: 'ok', summary: 'Fit c_3a7c50a2 → cover; reframed 1 clip(s)' }],
      reply: '· add_text: replaced BIG SALE. 1 step(s) done.',
    })
    const visible = html.replace(/title="[^"]*"/g, '')
    expect(visible).toContain('>Reframe<')
    expect(visible).not.toMatch(/auto_reframe|Clip\.fit|c_3a7c50a2|\(s\)|add_text/)
    expect(visible).toContain('Text: replaced BIG SALE')
  })

  it('a run that applied nothing folds to a neutral chip carrying the reason, not a green tick', async () => {
    const html = await seedWith({
      status: 'done', plan: { steps: [] }, steps: [],
      reply: '[recipes] There is no music on the timeline, so there is nothing to turn down. Add music first.',
    })
    expect(html).toContain('class="g is-info"')
    expect(html).not.toContain('is-pass')
    expect(html).toContain('There is no music on the timeline, so there is nothing to turn down.')
  })

  it('a replacement is said in the folded chip (QA-073)', async () => {
    const html = await seedWith({
      status: 'done', plan: { steps: [] },
      steps: [{ index: 0, tool: 'add_text', status: 'ok', summary: "Replaced text 'BIG SALE' with 'Grand Opening'" }],
      verify: { type: 'verify', plan_id: 'p', checks: [CHECKS[0]], passed: 1, total: 1, rendered: false },
    })
    expect(html).toContain('class="g is-info"')
    expect(html).toContain('Replaced text &#x27;BIG SALE&#x27; with &#x27;Grand Opening&#x27;')
  })

  it('a step interrupted by a cancel stops as cancelled, never a spinner (QA-064)', async () => {
    const { reduce } = await import('../lib/promptEvents')
    const s0 = { status: 'running', steps: [{ index: 0, total: 3, tool: 'transcribe', status: 'running' }] }
    const s1 = reduce(s0 as never, { type: 'error', message: 'Cancelled — timeline unchanged.' } as never)
    expect(s1.status).toBe('cancelled')
    expect(s1.steps[0].status).toBe('cancelled')
    const s2 = reduce(s1, { type: 'step', index: 0, total: 3, tool: 'transcribe', status: 'running' } as never)
    expect(s2.steps[0].status).toBe('cancelled')
  })
})

describe('review RD3: repeated checks', () => {
  it('lists an identical check once (Auto edit sent "the video got shorter" twice: a duplicate React key)', async () => {
    const { shownChecks } = await import('../lib/checkProse')
    const dup: VerifyCheck = { check: 'duration_shrank', human: 'the video got shorter', pass: true, headline: true }
    const info: VerifyCheck = { check: 'loudness', human: 'loudness measured', pass: null, headline: false }
    const out = shownChecks([info, dup, { ...dup }, { ...dup, pass: false }])
    expect(out.map((c) => [c.check, c.pass])).toEqual([['duration_shrank', true], ['duration_shrank', false], ['loudness', null]])
  })
})
