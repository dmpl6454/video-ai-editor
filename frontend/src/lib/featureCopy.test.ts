// QA-101 sweep (wave-B remainder 4): an unavailable feature is said in editor
// language; the developer fix only appears where it can be run.
import { describe, expect, it } from 'vitest'
import { featureCopy } from './featureCopy'
import { gateFor, type CatalogEntry } from './aiCatalog'
import type { FeatureReport } from '../api'

// Verbatim entries from GET /api/features (ai/features.py strings).
const REMBG = { key: 'cutout', feature: 'Background removal (rembg)', available: false,
  fix: '`uv sync --all-extras --group dev` (installs rembg)' }
const report = (packaged: boolean, extra: Record<string, unknown> = {}): FeatureReport => ({
  packaged_app: packaged, python: '3.13', anthropic_key_set: false, available: [], summary: '',
  unavailable: [{ ...REMBG, ...extra }],
} as unknown as FeatureReport)
const entry = { tool: 'remove_background', label: 'Remove background', description: '', gate: 'cutout' } as unknown as CatalogEntry

const DEV_TEXT = /uv |pip|--group|`|sync|extra/

describe('feature copy (QA-101)', () => {
  it('packaged app: no command, no copy button — just what it means', () => {
    const g = gateFor(entry, report(true))
    expect(g.ok).toBe(false)
    if (g.ok) return
    const c = featureCopy(g)
    expect(c.fix).toBeNull()
    expect(`${c.badge} ${c.line} ${c.reason}`).not.toMatch(DEV_TEXT)
    expect(c.line).toBe('Background removal (rembg) isn’t set up on this Mac.')
  })

  it('left out of the packaged build: says so', () => {
    const g = gateFor(entry, report(true, { packaged_app_excluded: true }))
    if (g.ok) throw new Error('should be unavailable')
    expect(featureCopy(g)).toMatchObject({ badge: 'Not included', fix: null,
      line: 'Background removal (rembg) is not included in this version of the app.' })
  })

  it('a source checkout keeps the command, without markdown backticks', () => {
    const g = gateFor(entry, report(false))
    if (g.ok) throw new Error('should be unavailable')
    const c = featureCopy(g)
    expect(c.badge).toBe('Not installed')
    expect(c.fix).toBe('uv sync --all-extras --group dev (installs rembg)')
    expect(`${c.line} ${c.reason}`).not.toMatch(DEV_TEXT)
  })
})
