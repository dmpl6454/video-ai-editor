// Editor Brain (EB1-F): the Plan tab's read-only model — decisions grouped by
// kind in an editor's order, seek labels on the ruler's SMPTE grid, the
// summary and "Not done this time" lines, and a tolerant wire parser.
import { describe, expect, it } from 'vitest'
import {
  cleanText, deferredLines, groupDecisions, KIND_LABELS, normalizeBrainCard, seekLabel, summaryLine, tallyLine,
  type BrainCardInfo, type BrainDecision,
} from './brainDecisions'

const d = (id: string, kind: string, code: string, text: string, over: Partial<BrainDecision> = {}): BrainDecision =>
  ({ id, kind, code, text, optional: false, by: 'recipes', applied: true, ref: null, timelineT: null, ...over })

const decisions: BrainDecision[] = [
  d('k_0005', 'captions', 'caption_mode', 'Captions: Dynamic (talking head, energy 5)'),
  d('k_0001', 'cut_range', 'silence', 'silence of 0.8 s at 00:00:03:00', { ref: { src: 'talk.mp4', t0: 3, t1: 3.8 } }),
  d('k_0006', 'music', 'music_mood', 'chill bed: talking head, arousal 0.3, subtle', { optional: true }),
  d('k_0002', 'cut_range', 'filler', 'filler “um” at 00:00:05:00'),
  d('k_0004', 'keep_pause', 'pause_kept:emotion', 'kept 0.6 s of the pause at 00:00:07:00: after an emotional line',
    { applied: null }),
  d('k_0003', 'cut_range', 'false_start', 'false start “so the—” before “so the cut” at 00:00:09:00'),
  d('k_0007', 'punch_in', 'hook_emphasis', 'punch in on the hook statement', { applied: false, by: 'apple_intelligence' }),
  d('k_0008', 'switch_angle', 'speaker_turn', 'Guest starts speaking'),
  d('k_0009', 'dialogue', 'dialogue_lane', 'dialogue from recorder.wav on lane a1; camera microphones muted; 12 seams faded'),
]

const info: BrainCardInfo = {
  decisionsId: 'd_0a1b2c3d', tabDefault: 'plan',
  rungs: { brain: 'recipes', contentBrain: 'apple_intelligence', line: 'via Recipes · moments by Apple Intelligence' },
  summary: {
    projectType: 'talking_head', target: 'Reel', durationS: 45,
    hook: { quote: 'Ninety percent of first cuts are thrown away.', src: 'talk.mp4', t0: 2, t1: 4.5, timelineT: 2.0 },
    camera: { angles: 1, switches: 0, atCut: 0 }, pausesKept: 1,
    music: { bed: 'chill', shape: 'bed' }, captions: { mode: 'dynamic', style: 'ig_chunky', position: 'bottom' },
    dialogue: null, resultS: 44.7,
  },
  decisions, deferred: [{ asked: 'per-word caption highlight', why: 'next wave' }, { asked: 'music intro and outro', why: 'next wave' }],
  whys: [], grouped: [], unexplained: [],
}

describe('groupDecisions', () => {
  it('groups by kind in the order an editor reads the plan, keeping plan order inside a group', () => {
    const groups = groupDecisions(decisions)
    expect(groups.map((g) => g.kind)).toEqual(['cut_range', 'keep_pause', 'switch_angle', 'punch_in', 'captions', 'music', 'dialogue'])
    expect(groups[0].items.map((x) => x.id)).toEqual(['k_0001', 'k_0002', 'k_0003'])
    expect(groups[0].label).toBe('Cuts')
    expect(KIND_LABELS.keep_pause).toBe('Pauses kept')
    expect(KIND_LABELS.switch_angle).toBe('Camera')
    expect(KIND_LABELS.punch_in).toBe('Punch-ins')
  })
  it('labels a kind it does not know from the id, never drops it', () => {
    const groups = groupDecisions([d('k_1', 'lower_third', 'lower_third_intro', "Priya's first appearance")])
    expect(groups).toHaveLength(1)
    expect(groups[0].label).toBe('Lower third')
  })
  it('counts what was not applied and what was kept', () => {
    const groups = groupDecisions(decisions)
    const punch = groups.find((g) => g.kind === 'punch_in')!
    expect(punch.dropped).toBe(1)
    const pauses = groups.find((g) => g.kind === 'keep_pause')!
    expect(pauses.dropped).toBe(0)
  })
})

describe('seek labels', () => {
  it('reads like the ruler: SMPTE on the project grid, drop-frame at 29.97', () => {
    expect(seekLabel(12.1, 30)).toBe('Seek to 00:00:12:03')
    expect(seekLabel(3600, 29.97)).toBe('Seek to 01:00:00;00')
    expect(seekLabel(0, 25)).toBe('Seek to 00:00:00:00')
  })
})

describe('summary and deferred lines', () => {
  it('names the type, the target and the counts', () => {
    expect(summaryLine(info)).toBe('Talking head → Reel, 44.7 s: 3 cuts, 1 pause kept, 1 angle change, captions, chill bed')
  })
  it('lists what this wave does not do, with the why', () => {
    expect(deferredLines(info)).toEqual(['per-word caption highlight — next wave', 'music intro and outro — next wave'])
  })
  it('tallies reason codes the way the grouped card line does', () => {
    expect(tallyLine(decisions.filter((x) => x.kind === 'cut_range'))).toBe('1 silence, 1 filler, 1 false start')
    expect(tallyLine([d('a', 'cut_range', 'silence', ''), d('b', 'cut_range', 'silence', ''),
      d('c', 'cut_range', 'filler_acoustic', '')])).toBe('2 silences, 1 filler')
  })
})

describe('what the card never shows', () => {
  it('states the length the run leaves, and only when the backend said it', () => {
    expect(summaryLine({ ...info, summary: { ...info.summary, resultS: null } }))
      .toBe('Talking head → Reel: 3 cuts, 1 pause kept, 1 angle change, captions, chill bed')
  })
  it('hides graph keys, ids and the .normalized copy wherever the text came from', () => {
    expect(cleanText('dialogue from src_55e24f95eb89a2019f59d349 on lane a1')).toBe('dialogue from the footage on lane a1')
    expect(cleanText('dialogue from th_16x9.normalized.mp4 on lane a1; camera microphones muted'))
      .toBe('dialogue from th_16x9.mp4 on lane a1; camera microphones muted')
    expect(cleanText('8 cuts dropped (k_0003, k_0004): already removed')).toBe('8 cuts dropped: already removed')
    expect(cleanText('clip c_e210aa4d_ada34a moved')).toBe('clip moved')
    expect(cleanText('silence of 1.5 s at 00:00:14:11')).toBe('silence of 1.5 s at 00:00:14:11')
    expect(cleanText('th_16x9.mp4 and p2_recorder.wav')).toBe('th_16x9.mp4 and p2_recorder.wav')
  })
  it('never prints a graph key or a file for the dialogue source', () => {
    for (const src of ['src_55e24f95eb89a2019f59d349', 'recorder.wav', 'th_16x9.normalized.mp4']) {
      const line = summaryLine({ ...info, summary: { ...info.summary, dialogue: { src, lane: 'a1', seams: 3 } } })
      expect(line).toContain('dialogue on its own audio lane')
      expect(line).not.toMatch(/src_|\.wav|\.mp4|dialogue from/)
    }
  })
  it('names an unresolved dialogue source as its own lane, never as a key', () => {
    const line = summaryLine({ ...info, summary: { ...info.summary, dialogue: { src: '', lane: 'a1', seams: 3 } } })
    expect(line).toContain('dialogue on its own audio lane')
  })
})

describe('normalizeBrainCard', () => {
  it('parses the wire payload and keeps the whys aligned with the lines', () => {
    const raw = {
      decisions_id: 'd_0a1b2c3d', tab_default: 'plan',
      rungs: { brain: 'recipes', content_brain: null, line: 'via Recipes' },
      summary: { project_type: 'podcast', target: 'Premium Podcast', duration_s: 601.5,
        hook: null, camera: { angles: 2, switches: 14, at_cut: 3 }, pauses_kept: 2, music: null,
        captions: { mode: 'podcast', style: 'default', position: 'bottom' },
        dialogue: { src: 'recorder.wav', lane: 'a1', offsets: { 'a.mp4': 0.35 }, seams: 38 } },
      decisions: [{ id: 'k_0001', kind: 'cut_range', code: 'silence', text: 's', optional: false, by: 'recipes',
        applied: true, ref: { src: 'a.mp4', t0: 1, t1: 2 }, timeline_t: 1.25 }, { id: 'bad' }],
      deferred: [{ asked: 'lower thirds', why: 'next wave' }, 'junk'],
      whys: ['because', null, 7], grouped: [0], unexplained: [3],
    }
    const out = normalizeBrainCard(raw)!
    expect(out.decisionsId).toBe('d_0a1b2c3d')
    expect(out.summary.hook).toBeNull()
    expect(out.summary.dialogue?.seams).toBe(38)
    expect(out.decisions).toHaveLength(1)
    expect(out.decisions[0].timelineT).toBe(1.25)
    expect(out.summary.resultS).toBeNull()
    expect(out.deferred).toEqual([{ asked: 'lower thirds', why: 'next wave' }])
    expect(out.whys).toEqual(['because', null, null])
    expect(out.grouped).toEqual([0])
    expect(out.unexplained).toEqual([3])
    expect(summaryLine(out)).toBe('Podcast → Premium Podcast: 1 cut, 2 pauses kept, 14 angle changes, captions, dialogue on its own audio lane')
  })
  it('rejects anything that is not a card payload', () => {
    expect(normalizeBrainCard(null)).toBeNull()
    expect(normalizeBrainCard({ decisions: [] })).toBeNull()
    expect(normalizeBrainCard({ decisions_id: 'nope', decisions: [] })).toBeNull()
  })
})
