// Final QA: the Transform x/y fields were 56 px number inputs, so WebKit's
// spin buttons covered the last digit ("96" for 960) and a 1920-wide canvas's
// 4-digit positions never fit. They must hold "-1920" beside the spinner.
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const src = readFileSync(new URL('./Properties.tsx', import.meta.url), 'utf8')

describe('Transform position fields', () => {
  it('are wide enough for a 4-digit position plus WebKit spin buttons', () => {
    const widths = [...src.matchAll(/<NumberField value=\{[xy]Val\}[^>]*?width=\{(\w+)\}/g)].map((m) => m[1])
    expect(widths).toHaveLength(2)
    const consts = Object.fromEntries([...src.matchAll(/const (\w+) = (\d+)/g)].map((m) => [m[1], Number(m[2])]))
    for (const w of widths) expect(Number.isFinite(Number(w)) ? Number(w) : consts[w]).toBeGreaterThanOrEqual(72)
  })
})
