// QA-063-SETTINGS / QA-106-CACHE-UI: every sentence the Settings dialog shows.
import { describe, expect, it } from 'vitest'
import { cacheLine, canSaveKey, freedMessage, keyInputProblem, keyStatusLine, modelConsentText,
         weightRows, type KeyStatus } from './settingsModel'

const status = (o: Partial<KeyStatus>): KeyStatus =>
  ({ configured: false, source: 'none', masked: null, keychain_available: true, can_edit: true, ...o })
const GOOD = 'sk-ant-api03-' + 'A1b2_C3d4-'.repeat(9)

describe('the key field', () => {
  it('stays quiet until something is typed, then explains what is wrong', () => {
    expect(keyInputProblem('')).toBeNull()
    expect(keyInputProblem('   ')).toBeNull()
    expect(keyInputProblem('sk-proj-abc')).toMatch(/starts with/)
    expect(keyInputProblem('sk-ant-abc def')).toMatch(/space/)
    expect(keyInputProblem('sk-ant-short')).toMatch(/incomplete/)
    expect(keyInputProblem(GOOD)).toBeNull()
    expect(keyInputProblem(`  ${GOOD}\n`)).toBeNull()      // a paste with a trailing newline is fine
  })
  it('only enables Save for a key-shaped value', () => {
    expect(canSaveKey('')).toBe(false)
    expect(canSaveKey('sk-ant-short')).toBe(false)
    expect(canSaveKey(GOOD)).toBe(true)
  })
})

describe('the key status line', () => {
  it('names where the key comes from, with the mask only', () => {
    const k = keyStatusLine(status({ configured: true, source: 'keychain', masked: 'sk-ant-…WxYz' }))
    expect(k).toEqual({ text: 'Saved in your Keychain (sk-ant-…WxYz).', tone: 'ok' })
    expect(keyStatusLine(status({ source: 'env', masked: 'sk-ant-…abcd', can_edit: false })).text)
      .toMatch(/started with \(sk-ant-…abcd\)/)
    expect(keyStatusLine(status({})).tone).toBe('muted')
    expect(keyStatusLine(status({ source: 'disabled' })).tone).toBe('warn')
    expect(keyStatusLine(status({ keychain_available: false })).text).toMatch(/no Keychain/)
  })
  it('never tells anyone to edit .env or restart', () => {
    for (const s of ['env', 'keychain', 'none'] as const) {
      expect(keyStatusLine(status({ source: s })).text).not.toMatch(/\.env|restart|uv sync/)
    }
  })
})

describe('render cache (QA-106)', () => {
  it('reads as used of budget', () => {
    expect(cacheLine({ bytes: 412e6, budget_bytes: 1073741824 })).toBe('412 MB of 1.1 GB')
    expect(cacheLine({ bytes: 0, budget_bytes: 1073741824 })).toBe('Empty · keeps up to 1.1 GB')
    expect(cacheLine(null)).toBe('Checking…')
  })
  it('says what Clear freed', () => {
    expect(freedMessage(380e6)).toBe('Freed 380 MB')
    expect(freedMessage(0)).toMatch(/Nothing to clear/)
  })
})

describe('models and voices', () => {
  it('lists every download with its size, what uses it, and whether it is here', () => {
    const rows = weightRows({
      'captions:large-v3': { what: 'the accurate caption model', bytes: 3_100_000_000, cached: true },
      tts: { what: 'the voiceover voice', bytes: 60_000_000, cached: false },
    })
    expect(rows).toEqual([
      { key: 'captions:large-v3', name: 'Accurate caption model', usedBy: 'Captions (Accurate)', size: '3.1 GB',
        cached: true, state: 'On this Mac' },
      { key: 'tts', name: 'Voiceover voice', usedBy: 'AI voiceover', size: '60 MB', cached: false,
        state: 'Downloads the first time you use it — you’ll be asked first' },
    ])
    expect(weightRows(null)).toEqual([])
  })
  it('asks before the local model download with its size and the free space', () => {
    expect(modelConsentText(4_300_000_000, 120e9)).toBe(
      'Download the local model (4.3 GB) from Hugging Face? It is fetched once and then runs on this Mac with no internet. 120.0 GB is free on this Mac.')
  })
})
