// QA-101: the AI panel's cards speak editor language. Engine names, track
// ids and pipeline jargon may be searched for (keywords) but never shown.
import { describe, expect, it } from 'vitest'
import { AI_CATALOG, filterCatalog } from './aiCatalog'

const JARGON = /ripple|demucs|diariz|rembg|esrgan|vidstab|\brife\b|\blama\b|piper|whisper|madlad|\bclip model|<session>|blank path|\bv1\b|\bvo track|fps|stem|fingerprint|inpaint/i

describe('AI card copy (QA-101)', () => {
  it('no card description carries engine names or pipeline jargon', () => {
    for (const e of AI_CATALOG) expect(e.description, e.tool).not.toMatch(JARGON)
  })
  it('the engine names still find their tools', () => {
    expect(filterCatalog(AI_CATALOG, 'demucs').map((e) => e.tool).sort()).toEqual(['instrumental_isolate', 'vocal_isolate'])
    expect(filterCatalog(AI_CATALOG, 'whisper').map((e) => e.tool)).toContain('auto_caption')
  })
})
