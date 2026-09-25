// QA-093: an OS file dropped on a lane at a time was appended to the end of
// V1, because only the window importer ever saw `dataTransfer.files`.
// Wave-B review: the drop now goes through the store's ONE import queue and is
// placed from the server's ANSWER (photo → 5 s, audio-only .mp4 → audio lane).
import { describe, expect, it } from 'vitest'
import { dropFilesOnLane, laneFor, placementFor, type LanePlacement, type PlacedAnswer, type Placement } from './laneDrop'
import { claimFileDrop, installWindowFileDrop, isClaimedFileDrop } from './fileDrop'

const LANES = [
  { id: 'v1', type: 'video' }, { id: 'v2', type: 'video' }, { id: 'a1', type: 'audio' },
  { id: 'music', type: 'music' }, { id: 'vo', type: 'vo' }, { id: 'stickers', type: 'sticker' },
]
const f = (name: string, type = '') => ({ name, type })
const place = (lane: string | null, start: number, mainEmpty = false): Placement => ({
  lane: lane ? LANES.find((l) => l.id === lane)! : null, start, lanes: LANES, mainEmpty,
})

// A stand-in for the store's import queue: answers like the server, then
// places the file from the ANSWER (placementFor) — what runImport does.
function deps(answers: Record<string, PlacedAnswer> = {}) {
  const calls: unknown[] = []
  return {
    calls,
    d: {
      enqueue: async (file: { name: string }, as: 'video' | 'audio', p: LanePlacement | null) => {
        if (!p) { calls.push(['default', file.name]); return }
        calls.push([as, file.name])
        const answer = answers[file.name] ?? (as === 'audio'
          ? { kind: 'audio', src: `/a/${file.name}`, duration: 4.02 }
          : { kind: 'video', normalized: `/n/${file.name}`, duration: 10 })
        const r = placementFor(answer, as, p)
        if (r.notice) calls.push(['notice', r.notice])
        if (r.args) calls.push(['add', r.args])
      },
      notice: (m: string) => { calls.push(['notice', m]) },
    },
  }
}

describe('laneFor', () => {
  it('a video dropped on PIP goes on PIP; audio on a video lane goes to Music', () => {
    expect(laneFor(f('a.mp4'), place('v2', 8))?.id).toBe('v2')
    expect(laneFor(f('s.wav'), place('v1', 8))?.id).toBe('music')
    expect(laneFor(f('s.wav'), place('vo', 8))?.id).toBe('vo')
  })

  it('the first video of an empty project takes the default import (canvas + rate)', () => {
    expect(laneFor(f('a.mp4'), place('v1', 3, true))).toBeNull()
  })

  it('a non-media lane falls back', () => {
    expect(laneFor(f('a.mp4'), place('stickers', 3))).toBeNull()
  })
})

describe('dropFilesOnLane', () => {
  it('queues each file and places it end to end at the drop time', async () => {
    const { calls, d } = deps()
    const n = await dropFilesOnLane([f('a.mp4'), f('b.mp4')], place('v2', 8), d, 30)
    expect(n).toBe(2)
    expect(calls).toEqual([
      ['video', 'a.mp4'], ['add', { track: 'v2', src: '/n/a.mp4', in: 0, out: 10, start: 8 }],
      ['video', 'b.mp4'], ['add', { track: 'v2', src: '/n/b.mp4', in: 0, out: 10, start: 18 }],
    ])
  })

  it('the next file starts on the frame grid', async () => {
    const { calls, d } = deps()
    await dropFilesOnLane([f('x.wav'), f('y.wav')], place('vo', 1), d, 30)
    const adds = calls.filter((c) => (c as unknown[])[0] === 'add') as [string, { start: number }][]
    expect(adds[1][1].start * 30).toBeCloseTo(Math.round((1 + 4.02) * 30), 9)
  })

  it('falls back to the default import where no lane fits', async () => {
    const { calls, d } = deps()
    await dropFilesOnLane([f('a.mp4')], place('stickers', 3), d, 30)
    expect(calls).toEqual([['default', 'a.mp4']])
  })

  it('a photo is a 5 s clip, not its 300 s still source (REV-B1B8-LANEDROP)', async () => {
    const { calls, d } = deps({ 'p.png': { kind: 'image', normalized: '/n/p.still.mp4', duration: 300 } })
    await dropFilesOnLane([f('p.png'), f('b.mp4')], place('v2', 2), d, 30)
    expect(calls).toEqual([
      ['video', 'p.png'], ['add', { track: 'v2', src: '/n/p.still.mp4', in: 0, out: 5, start: 2 }],
      ['video', 'b.mp4'], ['add', { track: 'v2', src: '/n/b.mp4', in: 0, out: 10, start: 7 }],
    ])
  })

  it('an audio-only .mp4 is placed from the answer\'s src on an audio lane, never src=undefined', async () => {
    const { calls, d } = deps({ 'tone.mp4': { kind: 'audio', src: '/w/uploads/audio/tone.mp4', duration: 3 } })
    await dropFilesOnLane([f('tone.mp4')], place('v2', 4), d, 30)
    const add = calls.find((c) => (c as unknown[])[0] === 'add') as [string, { track: string; src: string }]
    expect(add[1]).toEqual({ track: 'music', src: '/w/uploads/audio/tone.mp4', in: 0, out: 3, start: 4 })
    expect(calls.some((c) => (c as unknown[])[0] === 'notice')).toBe(true)
  })
})

describe('the window importer stands down for a drop the timeline claimed', () => {
  it('clears the overlay but does not import again', () => {
    const target = new EventTarget()
    const imported: unknown[] = []
    let active = true
    installWindowFileDrop(target, { setActive: (a) => { active = a }, importFiles: (fl) => imported.push(fl) })
    const mk = () => {
      const e = new Event('drop', { cancelable: true }) as Event & { dataTransfer: unknown }
      e.dataTransfer = { types: ['Files'], files: [f('a.mp4')] }
      return e
    }
    const claimed = mk()
    claimFileDrop(claimed)
    expect(isClaimedFileDrop(claimed)).toBe(true)
    target.dispatchEvent(claimed)
    expect(imported).toEqual([])
    expect(active).toBe(false)
    target.dispatchEvent(mk())
    expect(imported.length).toBe(1)
  })
})
