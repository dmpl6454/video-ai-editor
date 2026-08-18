// Ranked search over the emoji catalogue.
//
// The picker's first search was `terms.every(t => haystack.includes(t))` with
// results left in catalogue order. At 113 curated emoji that was fine; at 1906
// it produced three distinct complaints, all reproducible:
//
//   • THE RIGHT EMOJI RANKS LOW. "fire" put 🔥 sixth, behind "heart on fire"
//     and three firefighters. "star" put ⭐ fourth. "heart" did not surface ❤️
//     in the first eight. Order came from the catalogue, which is CLDR keyboard
//     order — a fine way to BROWSE and a meaningless way to rank a query.
//   • SUBSTRINGS MATCH INSIDE WORDS. "ok" returned 💔 first, because "br-ok-en
//     heart" contains it; 🧑‍🍳 matched too, via "c-ok". Matching has to respect
//     word boundaries.
//   • NAMES ARE NOT WHAT PEOPLE TYPE. "smile" returned exactly one emoji (😼,
//     "cat with wry smile") because every other face is named "smil-ING".
//     "100" returned nothing at all: 💯 is "hundred points".
//
// So: word-boundary matching, light stemming, a small alias table for the cases
// no amount of stemming reaches, and a score so the obvious answer comes first.
//
// Pure and separate from the component so the ranking is unit-testable — the
// same reason frameWalk and cropLayout live out here. Ranking is exactly the
// kind of logic where a screenshot tells you nothing and a test tells you
// everything.

import { EMOJI_CATALOG, type EmojiEntry } from './emojiCatalog'

/** Words people type that no CLDR name contains.
 *
 *  Deliberately short. This is not a thesaurus — every entry is a term that
 *  returned the WRONG emoji or nothing at all when tried against the real
 *  catalogue. Adding speculative synonyms makes ranking worse, not better,
 *  because each one is another way for an unrelated emoji to match. */
const ALIASES: Record<string, string> = {
  '💯': '100 hundred perfect score',
  '😂': 'lol lmao laugh crying',
  '🤣': 'lol rofl laugh',
  '❤️': 'love',
  '😍': 'love adore',
  '👍': 'like yes approve thumbsup',
  '👎': 'dislike no thumbsdown',
  '🙏': 'thanks please pray namaste',
  '🔥': 'lit hot flame',
  '💀': 'dead dying',
  '😭': 'sob cry bawling',
  '✅': 'tick yes done correct',
  '❌': 'no wrong cross incorrect',
  '⭐': 'favourite favorite',
  '🎉': 'party celebrate congrats',
  '🚀': 'launch ship fast',
  '👀': 'look watch looking',
  '💪': 'strong gym muscle',
  '🥺': 'please puppy begging',
  '🤔': 'hmm think thinking',
  '😎': 'cool sunglasses',
  '🤯': 'mindblown shocked',
  '✨': 'sparkle shiny magic',
  '💸': 'money cash',
  '⚡': 'lightning fast power',
}

/** Crude suffix stemmer — enough to bridge "smile"/"smiling", "heart"/"hearts".
 *
 *  Not a real stemmer on purpose: emoji names are a small, plain-English,
 *  present-participle-heavy vocabulary, and a full Porter stemmer would add
 *  size and surprises for no measurable gain here. */
export function stem(w: string): string {
  if (w.length > 4 && w.endsWith('ing')) return w.slice(0, -3)
  if (w.length > 3 && w.endsWith('es')) return w.slice(0, -2)
  if (w.length > 3 && w.endsWith('s')) return w.slice(0, -1)
  if (w.length > 3 && w.endsWith('e')) return w.slice(0, -1)
  return w
}

const WORD = /[^a-z0-9]+/

interface Row {
  e: EmojiEntry
  name: string
  words: string[]
  stems: string[]
  /** Subgroup words and aliases — matched, but scored far below the name. */
  extra: string[]
  extraStems: string[]
}

/** Built once at module load, not per keystroke. 1906 entries of lowercasing
 *  and splitting on every input event is the kind of cost that only shows up on
 *  the machine you are not testing on. */
const INDEX: Row[] = (() => {
  const rows: Row[] = []
  for (const g of EMOJI_CATALOG) {
    for (const e of g.emojis) {
      const name = e.n.toLowerCase()
      const words = name.split(WORD).filter(Boolean)
      const extra = `${e.k} ${ALIASES[e.c] ?? ''}`.toLowerCase().split(WORD).filter(Boolean)
      rows.push({
        e, name, words, stems: words.map(stem),
        extra, extraStems: extra.map(stem),
      })
    }
  }
  return rows
})()

export const INDEX_SIZE = INDEX.length

/** Score one term against one row. 0 means "no match" and rejects the row. */
function scoreTerm(r: Row, term: string, termStem: string): number {
  // Declared without an initialiser: every branch below assigns it and the
  // final `else` returns, so a `= 0` seed is dead — and a seed that can never
  // survive is worse than none, since it reads as a real "no match" default.
  let s: number
  if (r.words.includes(term)) s = 40
  else if (r.stems.includes(termStem)) s = 34   // smiling ≈ smile: near-exact
  else if (r.words.some((w) => w.startsWith(term))) s = 25
  else if (r.stems.some((x) => x.startsWith(termStem))) s = 15
  else if (r.extra.includes(term)) s = 8
  else if (r.extra.some((w) => w.startsWith(term))) s = 6
  else if (r.extraStems.some((x) => x.startsWith(termStem))) s = 4
  else return 0

  // HEAD-NOUN BONUS. English compound nouns put the head last, and emoji names
  // are almost all compounds: "red heart" is a kind of heart, "heart suit" is a
  // kind of suit. So the term landing on the LAST word is a strong signal that
  // this emoji is the thing you asked for rather than something named after it.
  //
  // Without this, "heart" returned ♥️ heart suit, 🫶 heart hands and ❤️‍🔥 heart
  // on fire ahead of ❤️ — which was not even in the first five — because they
  // all begin with the query and ❤️ does not.
  const last = r.words[r.words.length - 1]
  if (last && (last === term || stem(last) === termStem)) s += 20
  return s
}

/**
 * Rank the catalogue against `query`.
 *
 * Every term must match somewhere (AND, not OR) — "smiling eyes" should narrow,
 * not widen. `limit` caps the RETURNED rows, applied after sorting so the cap
 * never costs you the best answer; it exists because the picker paints an
 * `<img>` per result and 1900 of them per keystroke is what made search feel
 * slow, not the scan itself.
 */
export function searchEmoji(query: string, limit = 96): EmojiEntry[] {
  const q = query.trim().toLowerCase()
  if (!q) return []
  const terms = q.split(WORD).filter(Boolean)
  if (!terms.length) return []
  const termStems = terms.map(stem)

  const hits: { r: Row; score: number }[] = []
  for (const r of INDEX) {
    let total = 0
    let ok = true
    for (let i = 0; i < terms.length; i++) {
      const s = scoreTerm(r, terms[i], termStems[i])
      if (s === 0) { ok = false; break }
      total += s
    }
    if (!ok) continue
    // Whole-name hits are what "the obvious answer" means: 🔥 IS "fire".
    if (r.name === q) total += 300
    // Deliberately small. A bigger prefix bonus reads as sensible and is not:
    // it ranks every "heart …" above "red heart" purely for starting with the
    // query. It should break ties, not decide them.
    else if (r.name.startsWith(q)) total += 12
    // CONCISENESS. A name is a description, so the fewer words it spends, the
    // more likely this emoji simply IS the thing asked for. Weighted per WORD
    // rather than per character because that is the unit of added qualification
    // — and it has to be heavy enough to matter, or the head-noun bonus above
    // overshoots: "man gesturing OK" ends in the query and so outranked 👌
    // "OK hand", which is plainly the answer. Three words of description should
    // cost more than ending on the right one earns.
    total -= r.words.length * 10
    total -= r.name.length * 0.05   // final tie-break only
    hits.push({ r, score: total })
  }

  hits.sort((a, b) => b.score - a.score)
  return hits.slice(0, limit).map((h) => h.r.e)
}
