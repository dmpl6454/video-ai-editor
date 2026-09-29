// Final QA: Inspector > Video fade in/out were bare <input type=number> wired
// to onBlur only, so typing 0.4 and pressing Enter left the EDL at 0 with no
// History entry — while the Audio fade fields beside them (NumberField)
// commit on Enter. Both must go through NumberField (Enter → blur → one
// commit; same-value guard against the 2-dp display) and dispatch
// set_video_fade.
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const src = readFileSync(new URL('./Properties.tsx', import.meta.url), 'utf8')

describe('Inspector video fade fields', () => {
  for (const [label, arg] of [['Video fade in (s)', 'in_s'], ['Video fade out (s)', 'out_s']] as const) {
    it(`${label} commits on Enter like the audio fade fields`, () => {
      const re = new RegExp(`<NumberField ariaLabel="${label.replace(/[()]/g, '\\$&')}"[^>]*?`
        + `onCommit=\\{\\(n\\) => (?:void )?dispatch\\('set_video_fade', \\{ clip_id: c\\.id, ${arg}: n \\}\\)\\}`)
      expect(src).toMatch(re)
      // No second, blur-only copy of the control left behind.
      expect(src).not.toMatch(new RegExp(`<input[^>]*aria-label="${label.replace(/[()]/g, '\\$&')}"`))
    })
  }
  it('NumberField commits on Enter', () => {
    const nf = src.slice(src.indexOf('function NumberField('), src.indexOf('function NumberField(') + 3000)
    expect(nf).toMatch(/onKeyDown=\{\(e\) => \{ if \(e\.key === 'Enter'\) e\.currentTarget\.blur\(\) \}\}/)
    expect(nf).toMatch(/onBlur=\{commit\}/)
  })
})
