// QA-097 (remainder): the export ETA waits for enough of the run, follows the
// current rate (an EMA of Δp/Δt), counts down between polls and is rounded.
import { describe, expect, it } from 'vitest'
import { ETA_START, etaLeft, etaText, sampleEta, type EtaState } from './exportEta'

/** Poll a progress curve every `every` seconds, like the store's job poll. */
function run(curve: (t: number) => number, until: number, every = 0.5): { s: EtaState; at: (t: number) => number | null } {
  let s = ETA_START
  const states: { t: number; s: EtaState }[] = []
  for (let t = every; t <= until + 1e-9; t += every) {
    s = sampleEta(s, t, curve(t))
    states.push({ t, s })
  }
  return { s, at: (t) => { const hit = [...states].reverse().find((x) => x.t <= t + 1e-9); return hit ? etaLeft(hit.s, t) : null } }
}

// The 0.7.2 shape: a 30 s preparation at ~0 %, then 60 s of frames — a job
// that ends at 92 s. The old elapsed/progress read "~89 s" at 60 s.
const plateau = (t: number) => (t < 30 ? 0.002 * t / 30 : Math.min(1, 0.002 + (t - 30) / 62))

describe('export ETA (QA-097)', () => {
  it('stays silent before 10 % or 3 s', () => {
    const fast = run((t) => Math.min(1, t / 10), 2.5)
    expect(fast.at(2.5)).toBeNull()                 // 25 % but only 2.5 s in
    const slow = run((t) => t / 200, 15)
    expect(slow.at(15)).toBeNull()                  // 15 s in but 7.5 %
    expect(etaText(null)).toBe('')
  })

  it('follows the current rate after a slow start (the old formula was off by ~55 s)', () => {
    const r = run(plateau, 60)
    const truth = 92 - 60
    const left = r.at(60)!
    expect(Math.abs(left - truth)).toBeLessThan(2)
    const old = (60 / plateau(60)) * (1 - plateau(60))
    expect(Math.abs(old - truth)).toBeGreaterThan(20)
  })

  it('is steady under jittery polls — no swing bigger than the jitter can justify', () => {
    // Linear 100 s job; each poll's progress is ±1.5 % off.
    const jitter = (t: number) => Math.min(1, t / 100 + (Math.round(t * 2) % 2 ? 0.015 : -0.015))
    let s = ETA_START
    const lefts: number[] = []
    for (let t = 0.5; t <= 80; t += 0.5) {
      s = sampleEta(s, t, jitter(t))
      const l = etaLeft(s, t)
      if (l !== null) lefts.push(l)
    }
    const tail = lefts.slice(-60)
    for (let i = 1; i < tail.length; i++) expect(Math.abs(tail[i] - tail[i - 1])).toBeLessThan(6)
  })

  it('counts down between polls', () => {
    let s = ETA_START
    s = sampleEta(s, 4, 0.2)
    s = sampleEta(s, 8, 0.4)       // 0.05 / s → 12 s left at t = 8
    expect(etaLeft(s, 8)).toBeCloseTo(12, 5)
    expect(etaLeft(s, 10)).toBeCloseTo(10, 5)
    expect(etaLeft(s, 30)).toBe(0)
  })

  it('ignores a repeated or backwards sample', () => {
    const s = sampleEta(sampleEta(ETA_START, 4, 0.2), 8, 0.4)
    expect(sampleEta(s, 8, 0.4)).toBe(s)
    expect(sampleEta(s, 9, 0.39)).toBe(s)
  })

  it('says it in words, rounded to 5 s above 30 s', () => {
    expect(etaText(3)).toBe('a few seconds left')
    expect(etaText(17.4)).toBe('about 17 s left')
    expect(etaText(43)).toBe('about 45 s left')
    expect(etaText(58)).toBe('about 1 min left')
    expect(etaText(83)).toBe('about 1 min 25 s left')
    expect(etaText(3600)).toBe('about 60 min left')
  })
})
