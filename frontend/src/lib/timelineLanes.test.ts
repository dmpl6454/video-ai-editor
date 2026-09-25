// QA-014: every EDL lane drew whether or not it held anything — eleven rows in
// a 247 px pane, new text on a 12th row below the fold, "captions" the only
// lowercase lane, mute boxes on silent lanes, and sticker clips unlabelled.
import { describe, expect, it } from 'vitest'
import type { AnyClip, Track } from '../types'
import {
  clipLabel, ghostTarget, GHOST_LANE_ID, laneHasSound, laneName, laneRows,
} from './timelineLanes'

const clip = (id: string, start = 0): AnyClip => ({ id, src: `/m/${id}.mp4`, in: 0, out: 5, start })
const text = (id: string): AnyClip => ({ id, text: 'Hi', start: 1, end: 2 })
function tr(id: string, type: string, z: number, clips: AnyClip[] = [], label?: string): Track {
  return { id, type, z, clips, ...(label ? { label } : {}) }
}

// empty_edl()'s lanes, plus the `text` lane add_text appends at the END.
const EDL_LANES = (): Track[] => [
  tr('v1', 'video', 0, [clip('a')], 'Main video'),
  tr('v2', 'video', 1, [], 'PIP / overlay video'),
  tr('a1', 'audio', 0, [], 'Main audio'),
  tr('music', 'music', 0, [clip('m')], 'Music'),
  tr('vo', 'vo', 0, [], 'Voiceover'),
  tr('tx_hook', 'text', 10, [], 'Hook'),
  tr('tx_super', 'text', 11, [], 'Super text'),
  tr('tx_lt', 'text', 12, [], 'Lower thirds'),
  tr('stickers', 'sticker', 12, [{ id: 'st', src: '/s/star.png', start: 4, end: 7 } as unknown as AnyClip], 'Stickers'),
  tr('captions', 'captions', 13, []),
  tr('text', 'text', 10, [text('t')], 'Text'),
]

describe('laneRows', () => {
  it('draws only lanes in use (plus Main video), overlays above it, audio below', () => {
    const rows = laneRows(EDL_LANES()).map((t) => t.id)
    expect(rows).toEqual(['stickers', 'text', 'v1', 'music'])
  })

  it('an empty project still has its Main video lane', () => {
    const lanes = EDL_LANES().map((t) => ({ ...t, clips: [] }))
    expect(laneRows(lanes).map((t) => t.id)).toEqual(['v1'])
  })

  it('adds the new-track row last, only while dragging, so no row moves', () => {
    const idle = laneRows(EDL_LANES())
    const dragging = laneRows(EDL_LANES(), true)
    expect(dragging.slice(0, idle.length).map((t) => t.id)).toEqual(idle.map((t) => t.id))
    expect(dragging[dragging.length - 1].id).toBe(GHOST_LANE_ID)
  })
})

describe('ghostTarget', () => {
  it('resolves the new-track row to the first EMPTY lane of the family', () => {
    const lanes = EDL_LANES()
    expect(ghostTarget(lanes, 'video')?.id).toBe('v2')
    expect(ghostTarget(lanes, 'audio')?.id).toBe('a1')      // music is in use
    expect(ghostTarget(lanes, 'text')?.id).toBe('tx_hook')
    expect(ghostTarget(lanes, 'audio', 'a1')?.id).toBe('vo') // never the lane it came from
  })

  it('says so when there is none', () => {
    const lanes = EDL_LANES().filter((t) => t.id !== 'v2')
    expect(ghostTarget(lanes, 'video')).toBeNull()
  })
})

describe('lane names and clip labels', () => {
  it('capitalises an unlabelled lane and keeps real labels', () => {
    expect(laneName(tr('captions', 'captions', 13))).toBe('Captions')
    expect(laneName(tr('v2', 'video', 1, [], 'PIP / overlay video'))).toBe('PIP / overlay video')
  })

  it('only sound lanes get a mute toggle', () => {
    expect(EDL_LANES().filter(laneHasSound).map((t) => t.id)).toEqual(['v1', 'v2', 'a1', 'music', 'vo'])
  })

  it('labels sticker clips (emoji, else file name)', () => {
    expect(clipLabel({ id: 's', src: '/x/star.png', start: 0, end: 1 } as unknown as AnyClip)).toBe('star.png')
    expect(clipLabel({ id: 's', src: '/x/1f600.png', start: 0, end: 1, label: '😀' } as unknown as AnyClip)).toBe('😀')
    expect(clipLabel(text('t'))).toBe('Hi')
  })
})
