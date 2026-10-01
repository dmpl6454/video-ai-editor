// Editor Brain (EB1-F, FX-E wave): the run reducer's brain additions — the
// footage analysis progress (UX-07), the advisory audit that is a note and not
// a miss in the headline (UX-13), and step summaries with no ids.
import { describe, expect, it } from 'vitest'
import {
  analysisLine, EMPTY_RUN, isBrainPlan, isPromptCancelMessage, normalizeAnalysis, ranBrainTools, reduce, startRun,
  terminalAnnouncement,
  tidySummary, type Plan, type VerifyEvent,
} from './promptEvents'
import { checksHeadline } from './checkProse'

const brainPlan = (): Plan => ({
  version: 1, intent: 'edit', steps: [{ tool: 'cut_source_ranges', args: { plan_ref: 'd_0a1b2c3d' }, why: 'cuts' }],
  needs_input: [], postconditions: [], confidence: 1, brain: 'recipes',
})
const plainPlan = (): Plan => ({
  version: 1, intent: 'audit', steps: [{ tool: 'audit_aesthetic', args: {}, why: 'audit' }],
  needs_input: [], postconditions: [], confidence: 1, brain: 'recipes',
})
const verify = (): VerifyEvent => ({
  type: 'verify', plan_id: 'p_1', passed: 2, total: 3, rendered: false, checks: [
    { check: 'captions_nonempty', human: 'captions were laid', pass: true, headline: true, blocking: false },
    { check: 'canvas_aspect', human: 'the canvas has the requested aspect', pass: true, headline: true, blocking: true },
    { check: 'audit_ok', human: 'the aesthetic audit passes', pass: false, headline: true, blocking: false,
      measured: { score: 85, errors: [], hook_score: 2 }, expected: { errors: 0, hook_score: 3 } },
  ],
})

describe('analysis progress (UX-07)', () => {
  it('reads the frame the backend sends today: layer, pct, eta_s', () => {
    const a = normalizeAnalysis({ type: 'analysis', layer: 'speech', pct: 41.6, eta_s: 20 })
    expect(a).toEqual({ layer: 'speech', pct: 41.6, etaS: 20, jobId: null, layers: [] })
    expect(analysisLine(a)).toBe('Reading the footage — speech · 42% · about 20 s left')
  })
  it('tolerates every optional field missing, malformed or odd', () => {
    expect(normalizeAnalysis({})).toEqual({ layer: '', pct: 0, etaS: null, jobId: null, layers: [] })
    expect(normalizeAnalysis(null).pct).toBe(0)
    expect(normalizeAnalysis({ pct: 250 }).pct).toBe(100)
    expect(normalizeAnalysis({ pct: -3 }).pct).toBe(0)
    expect(normalizeAnalysis({ layer: 7, pct: 'x', eta_s: 'soon', layers: 'no' })).toEqual(
      { layer: '', pct: 0, etaS: null, jobId: null, layers: [] })
  })
  it('uses the fields a later backend adds: fractions, a job id, a layer list', () => {
    const a = normalizeAnalysis({
      layer: 'semantic', fraction: 0.5, job_id: 'job_abc',
      layers: [{ name: 'speech', fraction: 1 }, { layer: 'semantic', pct: 30 }, 'scenes', { name: '' }, 3],
    })
    expect(a.pct).toBe(50)
    expect(a.jobId).toBe('job_abc')
    expect(a.layers).toEqual([
      { name: 'speech', pct: 100, state: 'done' }, { name: 'semantic', pct: 30, state: 'running' },
      { name: 'scenes', pct: null, state: 'waiting' }])
  })
  it('says a long wait in minutes, a finished read as done, and names no layer it was not told', () => {
    expect(analysisLine(normalizeAnalysis({ pct: 10, eta_s: 240 }))).toBe('Reading the footage · 10% · about 4 min left')
    expect(analysisLine(normalizeAnalysis({ pct: 100, eta_s: 0 }))).toBe('Reading the footage · done')
    expect(analysisLine(normalizeAnalysis({ layer: 'unheard_voice', pct: 0 }))).toBe('Reading the footage — unheard voice · 0%')
  })
  it('lives in the run state until the plan, a card, an end or an error replaces it', () => {
    let s = reduce(startRun(), { type: 'analysis', layer: 'speech', pct: 12, eta_s: 30 })
    expect(s.analysis?.pct).toBe(12)
    s = reduce(s, { type: 'analysis', layer: 'speech', pct: 55, eta_s: 10 })
    expect(s.analysis?.pct).toBe(55)
    expect(s.unknownEvents).toBe(0)
    expect(reduce(s, { type: 'plan', plan: brainPlan() }).analysis).toBeNull()
    expect(reduce(s, { type: 'error', message: 'x' }).analysis).toBeNull()
    expect(reduce(s, { type: 'done' }).analysis).toBeNull()
    expect(EMPTY_RUN.analysis).toBeNull()
  })
  it('a cancelled read is a cancel, not an error', () => {
    expect(isPromptCancelMessage('Cancelled — timeline unchanged.')).toBe(true)
    expect(isPromptCancelMessage('via Recipes — The footage could not be read (cancelled). Nothing was changed.')).toBe(true)
    expect(isPromptCancelMessage('via Recipes — The footage could not be read (disk full). Nothing was changed.')).toBe(false)
    const s = reduce(reduce(startRun(), { type: 'analysis', layer: 'speech', pct: 5 }),
      { type: 'error', message: 'via Recipes — The footage could not be read (cancelled). Nothing was changed.' })
    expect(s.status).toBe('cancelled')
  })
})

describe('the advisory audit is a note (UX-13)', () => {
  it('a brain run reads clean: 2 of 2, nothing failed, the audit listed as info', () => {
    const s = reduce(reduce(startRun(), { type: 'plan', plan: brainPlan() }), verify())
    expect(s.verify?.passed).toBe(2)
    expect(s.verify?.total).toBe(2)
    expect(checksHeadline(s.verify)).toBe('2 of 2 checks passed')
    const audit = s.verify!.checks.find((c) => c.check === 'audit_ok')!
    expect(audit.pass).toBe(false)                    // still said, honestly, as an info check
    expect(audit.headline).toBe(false)
    expect(terminalAnnouncement({ ...s, status: 'done' })).toBe('Done — 2 of 2 checks passed')
  })
  it('a real failure of a brain run still counts and still fails', () => {
    const e = verify()
    e.checks[1] = { ...e.checks[1], pass: false }
    const s = reduce(reduce(startRun(), { type: 'plan', plan: brainPlan() }), e)
    expect(checksHeadline(s.verify)).toBe('1 of 2 checks passed · 1 failed')
  })
  it('an ordinary audit run keeps the 0.8.0 headline', () => {
    const s = reduce(reduce(startRun(), { type: 'plan', plan: plainPlan() }), verify())
    expect(s.verify?.total).toBe(3)
    expect(checksHeadline(s.verify)).toBe('2 of 3 checks passed · 1 failed')
    expect(isBrainPlan(plainPlan())).toBe(false)
    expect(isBrainPlan(brainPlan())).toBe(true)
  })
})

describe('step summaries a person can read (UX-13)', () => {
  it('drops decision and clip ids and the colon they leave dangling', () => {
    expect(tidySummary('Reorder v1: c_e1941d65_acf27e_71c059, c_e1941d65, c_e1941d65_0cae84')).toBe('Reorder v1')
    expect(tidySummary('Reorder V1:,,,,,,,,,')).toBe('Reorder V1')
    expect(tidySummary('8 cuts dropped (k_0003, k_0004): already removed earlier in this plan'))
      .toBe('8 cuts dropped: already removed earlier in this plan')
    expect(tidySummary('9 resolved')).toBe('9 resolved')
    expect(tidySummary(undefined)).toBeUndefined()
  })
  it('is applied to the steps of a brain run only', () => {
    const evt = { type: 'step', index: 0, total: 1, tool: 'reorder_clips', status: 'ok', summary: 'Reorder V1:,,,' }
    const brain = reduce(reduce(startRun(), { type: 'plan', plan: brainPlan() }), evt)
    expect(brain.steps[0].summary).toBe('Reorder V1')
    const plain = reduce(reduce(startRun(), { type: 'plan', plan: plainPlan() }), evt)
    expect(plain.steps[0].summary).toBe('Reorder V1:,,,')
  })
})

describe('a stored brain run', () => {
  it('is recognised by the tools only the brain plans carry', () => {
    expect(ranBrainTools(['cut_source_ranges', 'add_caption_track'])).toBe(true)
    expect(ranBrainTools(['sync_dialogue_lane'])).toBe(true)
    expect(ranBrainTools(['remove_fillers', 'audit_aesthetic'])).toBe(false)
  })
})
