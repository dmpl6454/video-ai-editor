// An imported .cube LUT is stored as uploads/luts/<stem>_<uuid8>.cube
// (main._unique_upload_path); its chip and the "Apply look to all clips"
// tooltip read "Warm Teal 81e3e270" (final QA, editor-ux).
import { describe, expect, it } from 'vitest'
import { lutDisplayName } from './lutName'

describe('lutDisplayName', () => {
  it('drops the storage suffix of an imported LUT', () => {
    expect(lutDisplayName('/w/s_1/uploads/luts/warm_teal_81e3e270.cube')).toBe('Warm Teal')
    expect(lutDisplayName('C:\\w\\uploads\\luts\\warm-teal_1c978840.CUBE')).toBe('Warm Teal')
  })
  it('title-cases a bundled look by name', () => {
    expect(lutDisplayName('teal_orange')).toBe('Teal Orange')
    expect(lutDisplayName('film-kodak.cube')).toBe('Film Kodak')
  })
})
