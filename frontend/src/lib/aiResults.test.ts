// One fixture per handler shape (copied from the dispatch.py handlers' return
// dicts), plus the garbage inputs a result view must survive: the panel has
// no error boundary, so a throwing view would take every card down.
import { describe, expect, it } from 'vitest'
import { resultView } from './aiResults'

describe('resultView — per-tool shapes', () => {
  it('find_moments → range rows with the transcript and shot text', () => {
    const v = resultView('find_moments', {
      query: 'the reveal',
      matches: [
        { start: 3.2, end: 6.9, transcript: 'and here it is', shot_description: 'a hand opens a box', score: 0.91 },
        { start: 12, end: 14, description: 'a sunset', score: 0.4 },
      ],
    })
    expect(v.headline).toBe('2 moments for “the reveal”')
    expect(v.rows).toEqual([
      { kind: 'range', start: 3.2, end: 6.9, text: 'and here it is — a hand opens a box', score: 0.91 },
      { kind: 'range', start: 12, end: 14, text: 'a sunset', score: 0.4 },
    ])
    expect(resultView('find_moments', { matches: [], summary: 'no clips on v1' }).headline).toBe('no clips on v1')
  })

  it('make_shorts → range rows + a saved-sessions note', () => {
    const v = resultView('make_shorts', {
      summary: 'Made 2 short(s)',
      shorts: [{ start: 0, end: 30, score: 0.8, why: 'energy peak' }, { start: 40, end: 70, score: 0.6, why: 'balanced' }],
      new_sessions: ['abc', 'def'],
    })
    expect(v.headline).toBe('Made 2 short(s)')
    expect(v.rows).toHaveLength(2)
    expect(v.rows[0]).toEqual({ kind: 'range', start: 0, end: 30, text: 'energy peak', score: 0.8 })
    expect(v.note).toMatch(/Saved 2 sessions/)
    expect(resultView('make_shorts', { summary: 'x', shorts: [], new_sessions: [] }).note).toBeUndefined()
  })

  it('search_media → spoken ranges, visual point hits, and the unavailable note', () => {
    const v = resultView('search_media', {
      query: 'sunset', scope: 'both',
      visual: { status: 'unavailable', message: 'visual search needs the CLIP model' },
      spoken: { status: 'ok', results: [{ start: 1.5, end: 3, text: 'the sunset was wild' }] },
    })
    expect(v.headline).toBe('1 result for “sunset”')
    expect(v.rows).toEqual([{ kind: 'range', start: 1.5, end: 3, text: 'the sunset was wild' }])
    expect(v.note).toBe('visual search needs the CLIP model')

    const ok = resultView('search_media', {
      query: 'sunset', scope: 'visual',
      visual: { status: 'ok', results: [
        { clip_id: 'c1', score: 0.31, time: 4.5, src_name: 'beach.mp4' },
        { path: '/broll/sky.mp4' },
      ] },
    })
    expect(ok.rows).toEqual([
      { kind: 'range', start: 4.5, end: 4.5, text: 'beach.mp4 @ 4.5s', score: 0.31 },
      { kind: 'path', path: '/broll/sky.mp4', text: 'sky.mp4' },
    ])
    expect(ok.note).toBeUndefined()
  })

  it('generate_hook → text rows and the source note', () => {
    const v = resultView('generate_hook', { candidates: ['You won’t believe this', '', 'Stop scrolling'], source: 'heuristic (no API key)' })
    expect(v.rows).toEqual([{ kind: 'text', text: 'You won’t believe this' }, { kind: 'text', text: 'Stop scrolling' }])
    expect(v.headline).toBe('2 hook ideas')
    expect(v.note).toBe('Source: heuristic (no API key)')
  })

  it('find_broll → path rows only — never a "Use" text row', () => {
    const v = resultView('find_broll', {
      summary: '2 b-roll candidate(s) for \'city\' in /broll',
      candidates: [{ path: '/broll/city_night.mp4', score: 0.9, duration: 12.25 }, { path: '/broll/city.mov', score: 0.5 }],
      bin: '/broll',
    })
    expect(v.rows).toEqual([
      { kind: 'path', path: '/broll/city_night.mp4', text: 'city_night.mp4 · 12.3s' },
      { kind: 'path', path: '/broll/city.mov', text: 'city.mov' },
    ])
    expect(v.rows.some((r) => r.kind === 'text')).toBe(false)
  })

  it('diarize → speaker rows', () => {
    const v = resultView('diarize', {
      summary: 'Diarized main: 2 turns, 2 speaker(s)',
      turns: [{ speaker: 'SPEAKER_00', start: 0, end: 4.1 }, { speaker: 'SPEAKER_01', start: 4.1, end: 9 }],
      speakers: ['SPEAKER_00', 'SPEAKER_01'],
    })
    expect(v.headline).toBe('Diarized main: 2 turns, 2 speaker(s)')
    expect(v.rows).toEqual([
      { kind: 'speaker', speaker: 'SPEAKER_00', start: 0, end: 4.1 },
      { kind: 'speaker', speaker: 'SPEAKER_01', start: 4.1, end: 9 },
    ])
  })

  it('audit_aesthetic → score headline and levelled issue rows', () => {
    const v = resultView('audit_aesthetic', {
      score: 75, duration: 42, ok: false,
      issues: [
        { level: 'error', key: 'hook_missing', message: 'No hook in the first 3s', fix_tool: 'apply_hook_stack' },
        { level: 'warn', key: 'long_shot', message: 'Clip c1 runs 20s with no cut' },
        { level: 'nit', key: 'x', message: 'minor' },
      ],
      hook: { visual: false, text: false, audio: true, hook_score: 1, missing: ['visual', 'text'] },
    })
    expect(v.headline).toBe('Score 75/100')
    expect(v.rows).toEqual([
      { kind: 'issue', level: 'error', text: 'No hook in the first 3s' },
      { kind: 'issue', level: 'warn', text: 'Clip c1 runs 20s with no cut' },
      { kind: 'issue', level: 'info', text: 'minor' },
    ])
    expect(v.note).toBe('Hook stack 1/3')
  })

  it('match_style → raw fingerprint with a named headline', () => {
    const v = resultView('match_style', { reference: '/refs/viral.mp4', fingerprint: { cuts_per_min: 30 } })
    expect(v.headline).toBe('Style fingerprint of viral.mp4')
    expect(v.rows).toEqual([])
    expect(v.raw).toEqual({ reference: '/refs/viral.mp4', fingerprint: { cuts_per_min: 30 } })
  })

  it('everything else → summary or "<label> done", raw attached', () => {
    expect(resultView('remove_silences', { summary: 'Removed 4 silences', cuts: 4 }, 'Remove silences'))
      .toEqual({ headline: 'Removed 4 silences', rows: [], raw: { summary: 'Removed 4 silences', cuts: 4 } })
    expect(resultView('stabilize', { new_src: '/x.mp4' }, 'Stabilize').headline).toBe('Stabilize done')
  })
})

describe('resultView — never throws', () => {
  it('returns a raw view for null, arrays, strings and shape drift', () => {
    for (const bad of [null, undefined, [], 'str', 42, true]) {
      const v = resultView('find_moments', bad, 'Find moments')
      expect(v.headline).toBe('Find moments done')
      expect(v.rows).toEqual([])
      expect(v.raw).toBe(bad)
    }
    expect(resultView('find_moments', { matches: 'nope' }).rows).toEqual([])
    expect(resultView('make_shorts', { shorts: [{ start: 'a' }], new_sessions: null }).rows).toEqual([])
    expect(resultView('search_media', { visual: 'x', spoken: [] }).rows).toEqual([])
    expect(resultView('generate_hook', { candidates: [null, 3, { text: 'x' }] }).rows).toEqual([])
    expect(resultView('diarize', { turns: [{ speaker: 1 }] }).rows).toEqual([])
    expect(resultView('audit_aesthetic', { issues: null }).headline).toBe('Style audit')
    expect(resultView('find_broll', { candidates: [{}] }).rows).toEqual([])
  })
})
