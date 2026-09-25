import { describe, expect, it } from 'vitest'
import { consentText, downloadBadge, downloadKeyFor, pendingDownload, sizeLabel, type DownloadReport } from './modelDownloads'

// Shape of GET /api/downloads (ai/weights.py) with nothing cached but large-v3.
const REPORT: DownloadReport = {
  'captions:large-v3': { what: 'the accurate caption model', bytes: 3_100_000_000, cached: true },
  'captions:large-v3-turbo': { what: 'the fast caption model', bytes: 1_600_000_000, cached: false },
  translate: { what: 'the translation model', bytes: 3_000_000_000, cached: false },
  bg_remove: { what: 'the background-removal model', bytes: 176_000_000, cached: false },
}

describe('first-run downloads (QA-065)', () => {
  it('maps a run to the weights it needs', () => {
    expect(downloadKeyFor('auto_caption', { model: 'large-v3-turbo' })).toBe('captions:large-v3-turbo')
    expect(downloadKeyFor('auto_caption', {})).toBe('captions:large-v3')
    expect(downloadKeyFor('translate_captions', { target_lang: 'hi' })).toBe('translate')
    expect(downloadKeyFor('translate_captions', { target_lang: 'hinglish' })).toBeNull()
    expect(downloadKeyFor('remove_background')).toBe('bg_remove')
    expect(downloadKeyFor('split_at')).toBeNull()
  })
  it('asks only when the weights are not on disk', () => {
    expect(pendingDownload(REPORT, 'captions:large-v3')).toBeNull()
    expect(pendingDownload(REPORT, 'captions:large-v3-turbo')?.bytes).toBe(1_600_000_000)
    expect(pendingDownload(null, 'translate')).toBeNull()
  })
  it('discloses the size in the badge and the question', () => {
    expect(downloadBadge(pendingDownload(REPORT, 'captions:large-v3-turbo'))).toBe('Downloads 1.6 GB first')
    expect(downloadBadge(pendingDownload(REPORT, 'captions:large-v3'))).toBeNull()
    expect(consentText(REPORT.bg_remove)).toContain('176 MB')
    expect(sizeLabel(3_000_000_000)).toBe('3.0 GB')
  })
})
