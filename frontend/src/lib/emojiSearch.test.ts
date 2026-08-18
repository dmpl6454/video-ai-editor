import { describe, it, expect } from 'vitest'
import { searchEmoji, stem, INDEX_SIZE } from './emojiSearch'

/** Position of an emoji in the results, or -1. */
const rank = (q: string, ch: string) =>
  searchEmoji(q).findIndex((e) => e.c === ch)

describe('emoji search', () => {
  it('indexes the whole catalogue', () => {
    expect(INDEX_SIZE).toBeGreaterThan(1500)
  })

  // Each of these is a query that returned the WRONG thing before ranking
  // existed. The numbers in the comments are what the old search did.
  describe('the obvious answer comes first', () => {
    it.each([
      ['fire', '🔥'],      // was 6th, behind "heart on fire" + 3 firefighters
      ['star', '⭐'],      // was 4th, behind 🤩 star-struck
      ['heart', '❤️'],     // was not in the top 8, then not in the top 5
      ['rocket', '🚀'],
      ['cat', '🐈'],       // CLDR: 🐈 IS "cat"; 🐱 is "cat face"
      ['check', '✔️'],     // ✔️ is "check mark"; ✅ is "check mark button"
    ])('%s -> %s', (q, ch) => {
      expect(rank(q, ch)).toBe(0)
    })

    it('keeps the plausible alternatives nearby', () => {
      // Not first, but reachable without scrolling. 🐱 is "cat FACE", so the
      // head-noun rule correctly ranks actual cats ("black cat", "weary cat")
      // above it — a user who wanted the face still sees it in the first row.
      expect(rank('check', '✅')).toBeLessThanOrEqual(2)
      expect(rank('cat', '🐱')).toBeGreaterThanOrEqual(0)
      expect(rank('cat', '🐱')).toBeLessThanOrEqual(8)
    })
  })

  it('matches whole words, not substrings inside them', () => {
    // "ok" used to return 💔 first, via "br-OK-en heart", and 🧑‍🍳 via "c-OK".
    const res = searchEmoji('ok')
    expect(res.length).toBeGreaterThan(0)
    expect(res[0].c).toBe('👌')
    expect(res.map((e) => e.c)).not.toContain('💔')
  })

  it('bridges the -ing / -s gap that CLDR names create', () => {
    // Every smiley is named "smilING", so a literal "smile" found exactly one
    // emoji: 😼 "cat with wry smile".
    const res = searchEmoji('smile')
    expect(res.length).toBeGreaterThan(5)
    expect(res.map((e) => e.c)).toContain('🙂')
    // And the reverse direction still works.
    expect(searchEmoji('hearts').map((e) => e.c)).toContain('❤️')
  })

  it('finds emoji whose name is nothing like what people type', () => {
    // 💯 is "hundred points" — "100" used to return zero results.
    expect(searchEmoji('100').map((e) => e.c)).toContain('💯')
    expect(searchEmoji('lol').map((e) => e.c)).toContain('😂')
    expect(searchEmoji('love').map((e) => e.c)).toContain('❤️')
  })

  it('ANDs multiple terms so a longer query narrows', () => {
    const one = searchEmoji('face')
    const two = searchEmoji('smiling face')
    expect(two.length).toBeLessThan(one.length)
    expect(searchEmoji('smiling eyes').map((e) => e.c)).toContain('😁')
  })

  it('returns nothing for a query that matches nothing', () => {
    expect(searchEmoji('zzzzqqq')).toEqual([])
    expect(searchEmoji('   ')).toEqual([])
    expect(searchEmoji('')).toEqual([])
  })

  it('caps results after sorting, never before', () => {
    // A cap applied during the scan would drop the best answer whenever it sat
    // late in catalogue order — which is exactly where 🔥 was for "fire".
    const capped = searchEmoji('face', 3)
    expect(capped).toHaveLength(3)
    expect(capped[0]).toEqual(searchEmoji('face', 96)[0])
  })

  it('stems predictably', () => {
    expect(stem('smiling')).toBe(stem('smile'))
    expect(stem('hearts')).toBe('heart')
    expect(stem('ok')).toBe('ok')      // too short to mangle
  })

  it('is fast enough to run on every keystroke', () => {
    const t0 = performance.now()
    for (const q of ['f', 'fi', 'fir', 'fire', 'fire e']) searchEmoji(q)
    // Generous — the point is to catch an accidental O(n^2), not to benchmark.
    expect(performance.now() - t0).toBeLessThan(250)
  })
})
