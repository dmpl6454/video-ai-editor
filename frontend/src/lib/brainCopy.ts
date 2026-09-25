// What the brain popover SAYS about each brain (QA-063). The backend's
// availability report is written for developers and MCP clients — "not
// installed (pip) · uv sync --extra local-llm", "Add ANTHROPIC_API_KEY to .env
// and restart", "helperMissing: fm-planner binary not found" — and the badge
// printed it verbatim, in a <pre>, inside a packaged app where none of it can
// be acted on. This maps a row to editor language; the raw report stays in
// the row's hover title for bug reports.

import type { BrainRow } from './promptEvents'

export interface BrainCopy { detail: string; fix: string | null }

const CAMEL_STATE: Record<string, string> = {
  deviceNotEligible: 'This Mac can’t run Apple Intelligence.',
  appleIntelligenceNotEnabled: 'Apple Intelligence is turned off.',
  modelNotReady: 'Apple Intelligence is still getting ready. Try again in a few minutes.',
}

export function brainCopy(row: BrainRow): BrainCopy {
  const d = row.detail ?? ''
  if (row.available) {
    if (row.id === 'local_model') return { detail: 'Ready on this Mac.', fix: null }
    if (row.id === 'apple_intelligence') return { detail: 'Ready on this Mac.', fix: null }
    if (row.id === 'claude') return { detail: 'Ready. Uses the internet.', fix: null }
    return { detail: 'Ready.', fix: null }
  }
  switch (row.id) {
    case 'claude':
      if (row.action === 'add_key') return { detail: 'Not set up.', fix: 'Claude needs an Anthropic API key, which isn’t set up on this Mac.' }
      return { detail: 'Turned off on this Mac.', fix: null }
    case 'local_model':
      if (row.action === 'download') return { detail: 'The model isn’t downloaded yet.', fix: null }
      if (/packaged app/.test(d)) return { detail: 'Not included in this version of the app.', fix: null }
      if (row.action === 'install') return { detail: 'Not installed on this Mac.', fix: null }
      if (/RAM/.test(d)) return { detail: d.replace(/^needs/, 'Needs'), fix: null }
      if (/Apple silicon/.test(d)) return { detail: 'Needs a Mac with Apple silicon.', fix: null }
      return { detail: 'Not available on this Mac.', fix: null }
    case 'apple_intelligence': {
      if (/^unsupportedOS/.test(d)) return { detail: 'Needs macOS 26 or later.', fix: null }
      if (/^helper/.test(d)) return { detail: 'Not included in this version of the app.', fix: null }
      if (/^assetsUnavailable/.test(d)) return { detail: 'Apple Intelligence is still downloading its model.', fix: 'Try again in a few minutes.' }
      const known = CAMEL_STATE[d.split(':')[0]]
      if (row.action === 'enable_in_settings') return { detail: known ?? 'Apple Intelligence is turned off.', fix: 'Turn it on in System Settings › Apple Intelligence & Siri.' }
      return { detail: known ?? 'Not available on this Mac.', fix: null }
    }
    default:
      return { detail: 'Not available.', fix: null }
  }
}
