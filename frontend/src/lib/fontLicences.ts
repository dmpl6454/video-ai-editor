// The bundled fonts' licence, as Help shows it. The source of truth is
// public/fonts/OFL.txt (written by scripts/font_licences.py from each font's
// own name table, shipped next to the fonts in the .app) — Help fetches and
// parses it rather than restating a list that could drift from the files.

export interface FontLicenceEntry {
  family: string
  files: string[]
  copyright: string
}

export interface FontLicences {
  entries: FontLicenceEntry[]
  /** The OFL-1.1 text itself, from its first rule line. */
  licence: string
}

export const FONT_LICENCE_URL = '/fonts/OFL.txt'

const ENTRY = /^(.+?) \(([^()]+\.(?:ttf|otf)(?:, [^()]+\.(?:ttf|otf))*)\)\n(.+)$/gim

export function parseFontLicences(text: string): FontLicences {
  const t = text.replace(/\r\n/g, '\n')
  const introAt = t.indexOf('This Font Software is licensed')
  const list = introAt >= 0 ? t.slice(0, introAt) : t
  const entries: FontLicenceEntry[] = []
  for (const m of list.matchAll(ENTRY)) {
    entries.push({ family: m[1].trim(), files: m[2].split(', ').map((f) => f.trim()), copyright: m[3].trim() })
  }
  const ruleAt = t.indexOf('-----')
  return { entries, licence: ruleAt >= 0 ? t.slice(ruleAt).trim() : '' }
}
