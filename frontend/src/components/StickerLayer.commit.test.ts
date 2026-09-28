// Dragging, resizing or rotating an overlay in the preview must not delete its
// animation. StickerLayer's gesture commits go through `transformCommitArgs`,
// which adds the playhead's clip-local `time`; the backend
// (dispatch.set_clip_transform) then writes a key AT that time on a keyed
// property instead of flattening it to a constant — pinned on the backend by
// tests/test_keyframe_editing_flow.py.
//
// The defect: a PiP keyed x {[[4,400],[10,1500]]} dragged at 7 s committed
// `set_clip_transform {clip_id, x:475, y:790}` with no time, and every key
// was gone. vitest runs without a DOM, so the canvas gesture itself is not
// driven here; the source guard below pins that every set_clip_transform the
// layer sends is built by the helper.
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { keyTimeAt, transformCommitArgs } from '../lib/overlayCommit'
import type { EDL } from '../types'

const here = dirname(fileURLToPath(import.meta.url))

function pipEdl(transitions: { at: number; type: string; duration: number }[] = []): EDL {
  const v1 = {
    id: 'v1', type: 'video', z: 0,
    clips: [
      { id: 'a', track: 'v1', src: 'a.mp4', in: 0, out: 5, start: 0 },
      { id: 'b', track: 'v1', src: 'b.mp4', in: 0, out: 15, start: 5 },
    ],
    transitions,
  }
  const v2 = {
    id: 'v2', type: 'video', z: 1,
    clips: [{
      id: 'pip', track: 'v2', src: 'r.mp4', in: 0, out: 20, start: 0,
      transform: {
        x: { keyframes: [[4, 400], [10, 1500]], interp: 'linear' },
        y: { keyframes: [[4, 300], [10, 800]], interp: 'linear' },
        scale: 0.4, rotation: 0, opacity: 1,
      },
    }],
  }
  return {
    version: 1, duration: 20, canvas: { w: 1920, h: 1080, fps: 30 },
    tracks: [v1, v2],
  } as unknown as EDL
}

describe('transformCommitArgs (preview drag of a keyframed overlay)', () => {
  it('carries the playhead as clip-local time, so the backend keys instead of flattening', () => {
    const args = transformCommitArgs(pipEdl(), 'pip', { x: 475, y: 790 }, 7)
    expect(args).toEqual({ clip_id: 'pip', x: 475, y: 790, time: 7 })
  })

  it('measures time on the render clock, as the Inspector does', () => {
    // A 0.5 s dissolve at the v1 cut (5.0) pulls every overlay lane after it
    // left by 0.5 s — but the PiP starts at 0, before the seam, so its render
    // start is still 0 and render 7.0 is 7.0 into it. A sticker starting after
    // the seam is measured from its pulled start.
    const edl = pipEdl([{ at: 5, type: 'dissolve', duration: 0.5 }])
    expect(keyTimeAt(edl, 'pip', 7)).toBeCloseTo(7, 9)
    const withLate = {
      ...edl,
      tracks: [...edl.tracks, {
        id: 'stickers', type: 'sticker', z: 5,
        clips: [{ id: 'st', track: 'stickers', start: 8, end: 12, emoji: '⭐' }],
      }],
    } as unknown as EDL
    // Layout 8.0 plays at render 7.5; the playhead at render 9.0 is 1.5 s in.
    expect(keyTimeAt(withLate, 'st', 9)).toBeCloseTo(1.5, 9)
  })

  it('works for every gesture field (rotate, resize) and passes extra flags through', () => {
    expect(transformCommitArgs(pipEdl(), 'pip', { rotation: 30 }, 2).time).toBe(2)
    expect(transformCommitArgs(pipEdl(), 'pip', { scale: 0.6 }, 11)).toEqual(
      { clip_id: 'pip', scale: 0.6, time: 11 })
    expect(transformCommitArgs(pipEdl(), 'pip', { x: 1, raise_to_front: true }, 1).raise_to_front).toBe(true)
  })

  it('omits time for a clip it cannot find (the commit still goes through)', () => {
    expect(transformCommitArgs(pipEdl(), 'gone', { x: 5 }, 3)).toEqual({ clip_id: 'gone', x: 5 })
  })
})

describe('StickerLayer gesture commits', () => {
  it('builds every set_clip_transform through transformCommitArgs', () => {
    const src = readFileSync(join(here, 'StickerLayer.tsx'), 'utf8')
    const calls = [...src.matchAll(/dispatch\(\s*'set_clip_transform'\s*,\s*([^\n]*)/g)]
    // Move (overlay), move (v1 video), rotate and resize.
    expect(calls.length).toBeGreaterThanOrEqual(4)
    for (const m of calls) expect(m[1].trimStart().startsWith('commitArgs(')).toBe(true)
  })
})
