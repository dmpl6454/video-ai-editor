// QA-063: the brain popover never shows a shell command, an env var or a
// camelCase state. Rows are the backend's real availability dicts.
import { describe, expect, it } from 'vitest'
import { brainCopy } from './brainCopy'
import type { BrainRow } from './promptEvents'

const row = (o: Partial<BrainRow>): BrainRow => ({ id: 'x', label: 'x', available: false, detail: '', fix: null, action: 'none', model: null, ...o })
const DEV = /uv sync|pip|\.env|ANTHROPIC_API_KEY|swift build|cd tools|helperMissing|unsupportedOS|fm-planner|[a-z][A-Z][a-z]+:/

describe('brain popover copy (QA-063)', () => {
  const REAL: BrainRow[] = [
    row({ id: 'local_model', detail: 'not installed (pip)', fix: 'uv sync --extra local-llm', action: 'install' }),
    row({ id: 'local_model', detail: 'excluded from the packaged app', fix: 'Run from source: uv sync --extra local-llm' }),
    row({ id: 'claude', detail: 'no ANTHROPIC_API_KEY', fix: 'Add ANTHROPIC_API_KEY to .env and restart to enable Claude', action: 'add_key' }),
    row({ id: 'apple_intelligence', detail: 'helperMissing: fm-planner binary not found', fix: 'Build the helper: cd tools/fm-planner && swift build -c release --arch arm64' }),
    row({ id: 'apple_intelligence', detail: 'unsupportedOS: needs macOS 26, this is 15.4', fix: 'Update to macOS 26 or later to use Apple Intelligence' }),
    row({ id: 'apple_intelligence', detail: 'appleIntelligenceNotEnabled', fix: 'x', action: 'enable_in_settings' }),
  ]
  it('never shows developer commands or internal states', () => {
    for (const r of REAL) {
      const c = brainCopy(r)
      expect(`${c.detail} ${c.fix ?? ''}`, r.detail).not.toMatch(DEV)
      expect(c.detail.length).toBeGreaterThan(0)
    }
  })
  it('says what a user can do when there is something to do', () => {
    expect(brainCopy(REAL[5]).fix).toMatch(/System Settings/)
    expect(brainCopy(REAL[2]).fix).toMatch(/API key/)
  })
})
