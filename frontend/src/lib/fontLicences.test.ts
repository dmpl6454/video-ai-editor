import { readdirSync, readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { parseFontLicences } from './fontLicences'

// The REAL licence file Help fetches (public/fonts/OFL.txt), against the REAL
// font folder next to it: every bundled font is listed with a copyright line,
// and the OFL-1.1 text is complete.
const DIR = new URL('../../public/fonts/', import.meta.url)
const TEXT = readFileSync(new URL('OFL.txt', DIR), 'utf8')
const FONTS = readdirSync(DIR).filter((f) => /\.(ttf|otf)$/i.test(f))

describe('bundled font licences (public/fonts/OFL.txt)', () => {
  const parsed = parseFontLicences(TEXT)

  it('lists every font file shipped to the browser, each with a copyright notice', () => {
    const listed = new Map(parsed.entries.flatMap((e) => e.files.map((f) => [f, e] as const)))
    expect(FONTS.length).toBeGreaterThan(0)
    for (const f of FONTS) {
      expect(listed.has(f), `${f} is not in OFL.txt`).toBe(true)
      expect(listed.get(f)!.copyright).toMatch(/copyright|\(c\)/i)
    }
  })

  it('carries the whole OFL-1.1 text, from its title to its disclaimer', () => {
    expect(parsed.licence.startsWith('-----------------------------------------------------------\nSIL OPEN FONT LICENSE Version 1.1 - 26 February 2007')).toBe(true)
    for (const section of ['PREAMBLE', 'DEFINITIONS', 'PERMISSION & CONDITIONS', 'TERMINATION', 'DISCLAIMER']) {
      expect(parsed.licence).toContain(`\n${section}\n`)
    }
    expect(parsed.licence.endsWith('OTHER DEALINGS IN THE FONT SOFTWARE.')).toBe(true)
  })

  it('parses families with several files and notices that are not "Copyright …"', () => {
    const p = parseFontLicences('Head\n\nInter (Inter-Black.ttf, Inter-Bold.ttf)\nCopyright 2016 The Inter Project Authors\n\n'
      + 'Noto Sans SC (NotoSansSC-VF.ttf)\n(c) 2014-2021 Adobe, with Reserved Font Name \'Source\'.\n\n'
      + 'This Font Software is licensed under the SIL Open Font License, Version 1.1.\n\n-----\nBODY')
    expect(p.entries).toEqual([
      { family: 'Inter', files: ['Inter-Black.ttf', 'Inter-Bold.ttf'], copyright: 'Copyright 2016 The Inter Project Authors' },
      { family: 'Noto Sans SC', files: ['NotoSansSC-VF.ttf'], copyright: "(c) 2014-2021 Adobe, with Reserved Font Name 'Source'." },
    ])
    expect(p.licence).toBe('-----\nBODY')
  })
})
