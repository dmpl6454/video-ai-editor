// Answers built from the recorded `clarify` event's own defaults, coerced per
// kind, and refused when a required answer is missing.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import {
  answersPayload, coerceAnswer, defaultAnswers, isGate, missingRequired, optionsFor, parseDuration,
  totalDownloadBytes, visibleQuestions,
} from './clarifyDefaults'
import { parseSseText, type ClarifyEvent, type NeedsInput } from './promptEvents'

const FIXTURE = readFileSync(fileURLToPath(new URL('./__fixtures__/prompt_stream.txt', import.meta.url)), 'utf-8')
const clarify = parseSseText(FIXTURE).find((e) => e.type === 'clarify') as unknown as ClarifyEvent

const LANG: NeedsInput = {
  key: 'target_lang', question: 'Which language for the captions?', kind: 'choice', required: true,
  options: [
    { value: 'hi', label: 'Hindi', synonyms: ['hindi', 'devanagari'] },
    { value: 'en', label: 'English' },
    { value: 'hinglish', label: 'Hinglish' },
    { value: 'es', label: 'Spanish', synonyms: ['español'] },
  ],
}
const COUNT: NeedsInput = { key: 'count', question: 'How many shorts?', kind: 'number', required: false, default: 3, min: 1, max: 10 }
const MAXDUR: NeedsInput = { key: 'max_dur', question: 'Longest short?', kind: 'duration', required: false, default: 60, min: 5, max: 180, unit: 's' }
const HANDLE: NeedsInput = { key: 'handle', question: 'Your handle?', kind: 'text', required: true }
const SRC: NeedsInput = { key: 'src', question: 'Which track?', kind: 'path', required: true,
  options: [{ value: 'bench_bed_100bpm.wav', label: 'bench_bed_100bpm.wav' }, { value: 'upbeat_120bpm.wav', label: 'upbeat_120bpm.wav (bundled)' }],
  default: 'bench_bed_100bpm.wav' }

describe('the recorded confirm question', () => {
  it('is required with no default, so it blocks until answered', () => {
    const q = clarify.questions[0]
    expect(q.kind).toBe('confirm')
    expect(defaultAnswers(clarify.questions)).toEqual({})
    expect(missingRequired(clarify.questions, {})).toEqual(['downloads'])
    expect(() => answersPayload(clarify.questions, {})).toThrow(/unanswered: downloads/)
  })
  it('accepts the offered values and the phone-style words', () => {
    expect(optionsFor(clarify.questions[0]).map((o) => o.label)).toEqual(['Download', 'Skip'])
    expect(answersPayload(clarify.questions, { downloads: 'yes' })).toEqual({ downloads: 'yes' })
    expect(answersPayload(clarify.questions, { downloads: 'Skip' })).toEqual({ downloads: 'no' })
    // A confirm question with no options on the wire still gets Yes/No.
    const bare: NeedsInput = { key: 'go', question: 'Start?', kind: 'confirm', required: true }
    expect(optionsFor(bare).map((o) => o.value)).toEqual(['yes', 'no'])
    expect(coerceAnswer(bare, 'go')).toBe('yes')
    expect(coerceAnswer(bare, 'nahi')).toBe('no')
    expect(coerceAnswer(bare, 'maybe')).toBeNull()
  })
  it('sums the download sizes for the card headline', () => {
    expect(totalDownloadBytes([{ bytes: 3_000_000_000 }, { bytes: 60_000_000 }])).toBe(3_060_000_000)
    expect(totalDownloadBytes(undefined)).toBe(0)
  })
})

describe('defaults and coercion per kind', () => {
  it('pre-fills every question that has a default and nothing else', () => {
    expect(defaultAnswers([LANG, COUNT, MAXDUR, HANDLE, SRC])).toEqual({ count: 3, max_dur: 60, src: 'bench_bed_100bpm.wav' })
  })
  it('choice: value, label or synonym; a stray string is not an answer', () => {
    expect(coerceAnswer(LANG, 'hi')).toBe('hi')
    expect(coerceAnswer(LANG, 'Hindi')).toBe('hi')
    expect(coerceAnswer(LANG, 'devanagari')).toBe('hi')
    expect(coerceAnswer(LANG, 'ESPAÑOL')).toBe('es')
    expect(coerceAnswer(LANG, 'fr')).toBeNull()
  })
  it('number and duration parse and clamp to the question bounds', () => {
    expect(coerceAnswer(COUNT, '4')).toBe(4)
    expect(coerceAnswer(COUNT, 40)).toBe(10)
    expect(coerceAnswer(COUNT, 'many')).toBeNull()
    expect(coerceAnswer(MAXDUR, '45s')).toBe(45)
    expect(coerceAnswer(MAXDUR, '1.5m')).toBe(90)
    expect(coerceAnswer(MAXDUR, '1:30')).toBe(90)
    expect(coerceAnswer(MAXDUR, '2 minutes')).toBe(120)
    expect(coerceAnswer(MAXDUR, '1000')).toBe(180)
    expect(coerceAnswer(MAXDUR, 'forever')).toBeNull()
    expect(parseDuration('')).toBeNull()
    expect(parseDuration('30')).toBe(30)
  })
  it('text and path trim and refuse empty; path must be an offered file', () => {
    expect(coerceAnswer(HANDLE, '  @priya.codes ')).toBe('@priya.codes')
    expect(coerceAnswer(HANDLE, '   ')).toBeNull()
    expect(coerceAnswer(SRC, 'upbeat_120bpm.wav')).toBe('upbeat_120bpm.wav')
  })
  it('the payload carries only asked-for keys, coerced; a blanked answer falls back to the default', () => {
    const qs = [LANG, COUNT, MAXDUR, HANDLE, SRC]
    const answers = { ...defaultAnswers(qs), target_lang: 'Hinglish', handle: '@q', max_dur: '', stray: 'x' }
    expect(answersPayload(qs, answers)).toEqual({
      target_lang: 'hinglish', count: 3, max_dur: 60, handle: '@q', src: 'bench_bed_100bpm.wav',
    })
    // `src` is required but has a default, so only `handle` blocks (§1.1).
    expect(missingRequired(qs, { target_lang: 'hi' })).toEqual(['handle'])
    // An optional question with no default and no answer is simply omitted.
    const NOTE: NeedsInput = { key: 'note', question: 'Anything else?', kind: 'text', required: false }
    expect(answersPayload([NOTE], {})).toEqual({})
  })
})

describe('gates come one at a time', () => {
  const dl = { key: 'downloads', question: 'First use downloads MADLAD (3 GB). Download or skip?', kind: 'confirm' as const, required: true }
  const gate = { key: 'gate_auto_caption', question: 'auto captions is not available here. Continue without it, or stop?',
                 kind: 'choice' as const, required: true,
                 options: [{ value: 'skip', label: 'Continue without auto captions', hint: 'The rest of the edit still runs' },
                           { value: 'abort', label: 'Stop', hint: 'Nothing changes' }] }
  const lang = { key: 'target_lang', question: 'Which language?', kind: 'choice' as const, required: true,
                 options: [{ value: 'hi', label: 'Hindi' }, { value: 'en', label: 'English' }] }
  it('shows only the first gate when several questions arrive, then the rest', () => {
    expect(isGate(dl) && isGate(gate) && !isGate(lang)).toBe(true)
    expect(visibleQuestions([dl, gate, lang])).toEqual([dl])
    expect(visibleQuestions([gate, lang])).toEqual([gate])
    expect(visibleQuestions([lang, dl])).toEqual([lang, dl])   // a real question first: the whole grid
    expect(visibleQuestions([dl])).toEqual([dl])
    expect(visibleQuestions([])).toEqual([])
  })
})
