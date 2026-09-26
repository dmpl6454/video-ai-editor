// QA-048: the clock, the playhead chip, the ruler and the Timing fields all
// read decimal seconds ("2.0s" for both frame 60 and frame 61 at 30 fps) and
// nothing could format or parse HH:MM:SS:FF.
import { describe, expect, it } from 'vitest'
import { frameOf as tbFrameOf, timeOf as tbTimeOf } from './preview/timeline/timebase'
import { toFrameGrid } from './frameStep'
import {
  formatTimecode, frameIndex, framesToTimecode, parseTimecode, rulerStepFrames, rulerTicks,
} from './timecode'

const NTSC = 30000 / 1001
const NTSC60 = 60000 / 1001
const FILM = 24000 / 1001

describe('formatTimecode', () => {
  it('names every frame distinctly (frames 60 and 61 at 30 fps)', () => {
    expect(formatTimecode(60 / 30, 30)).toBe('00:00:02:00')
    expect(formatTimecode(61 / 30, 30)).toBe('00:00:02:01')
    expect(formatTimecode(0, 30)).toBe('00:00:00:00')
    expect(formatTimecode(85.03333, 30)).toBe('00:01:25:01')
    expect(formatTimecode(3600, 25)).toBe('01:00:00:00')
  })

  it('writes 29.97 and 59.94 as drop-frame, so an hour reads 01:00:00;00', () => {
    expect(framesToTimecode(1799, NTSC)).toBe('00:00:59;29')
    expect(framesToTimecode(1800, NTSC)).toBe('00:01:00;02')
    expect(framesToTimecode(17982, NTSC)).toBe('00:10:00;00')
    expect(formatTimecode(3600, NTSC)).toBe('01:00:00;00')
    expect(framesToTimecode(3600, NTSC60)).toBe('00:01:00;04')
  })

  it('counts 23.976 at a nominal 24 without dropping', () => {
    expect(framesToTimecode(24, FILM)).toBe('00:00:01:00')
    expect(formatTimecode(1.001, FILM)).toBe('00:00:01:00')
  })
})

describe('parseTimecode', () => {
  it('round-trips every frame of the first eleven minutes at 29.97 DF', () => {
    for (let n = 0; n < 20000; n += 7) {
      const t = n / NTSC
      const back = parseTimecode(formatTimecode(t, NTSC), NTSC)
      expect(back, `frame ${n}`).not.toBeNull()
      expect(Math.round(back! * NTSC)).toBe(n)
    }
  })

  it('accepts right-aligned SMPTE, seconds, clock time and frame counts on the grid', () => {
    expect(parseTimecode('00:00:02:01', 30)).toBeCloseTo(61 / 30, 9)
    expect(parseTimecode('2:01', 30)).toBeCloseTo(61 / 30, 9)          // SS:FF
    expect(parseTimecode('1:02:03', 30)).toBeCloseTo(62 + 3 / 30, 9)   // MM:SS:FF
    expect(parseTimecode('12.5', 30)).toBeCloseTo(12.5, 9)
    expect(parseTimecode('12.51s', 30)).toBeCloseTo(375 / 30, 9)       // snapped to a frame
    expect(parseTimecode('1:05.5', 30)).toBeCloseTo(65.5, 9)
    expect(parseTimecode('90f', 30)).toBeCloseTo(3, 9)
    expect(parseTimecode('00:01:00;00', NTSC)).toBeCloseTo(1800 / NTSC, 9)  // skipped label → next real one
  })

  it('refuses what is not a time', () => {
    for (const bad of ['', 'abc', '1:2:3:4:5', '00:00:00:30', '00:61:00:00', '-1', '1::2']) {
      expect(parseTimecode(bad, 30), bad).toBeNull()
    }
  })
})

describe('ruler ticks', () => {
  it('labels frames when zoomed in and timecode seconds when zoomed out', () => {
    expect(rulerStepFrames(600, 30)).toBe(5)       // 20 px/frame
    expect(rulerStepFrames(80, 30)).toBe(30)       // one label a second
    expect(rulerStepFrames(0.5, 30)).toBeGreaterThanOrEqual(160 * 30)
  })

  it('draws a minor tick on every frame once frames are wide, all on the grid', () => {
    const ticks = rulerTicks(80, 80 + 600, 80, 600, 30, 100)
    expect(ticks.length).toBe(31)
    for (const tk of ticks) expect(Math.abs(tk.t * 30 - Math.round(tk.t * 30))).toBeLessThan(1e-9)
    expect(ticks.filter((tk) => tk.major).map((tk) => Math.round(tk.t * 30))).toEqual([0, 5, 10, 15, 20, 25, 30])
    // Majors carry distinct labels.
    const labels = ticks.filter((tk) => tk.major).map((tk) => formatTimecode(tk.t, 30))
    expect(new Set(labels).size).toBe(labels.length)
  })
})

describe('ruler labels under the playhead chip (wave-B review)', () => {
  it('finds the label the chip covers at 0 and none far from it', async () => {
    const { rulerLabelsUnder, rulerTicks } = await import('./timecode')
    const ticks = rulerTicks(0, 800, 120, 50, 30, 60)
    const measure = (s: string) => s.length * 6
    // chip at the playhead head (t = 0): x = 120 + 8 .. 120 + 8 + 66
    const hit = rulerLabelsUnder(ticks, 120, 50, 30, measure, 125, 200)
    expect(hit.length).toBe(1)
    expect(hit[0].x).toBe(123)
    expect(rulerLabelsUnder(ticks, 120, 50, 30, measure, 2000, 2100)).toEqual([])
  })
})

describe('one frame rule (review RD1: spec R2 keeps exactly one frame_of)', () => {
  it('a half-frame time names the frame a step or snap lands on (ties to even)', () => {
    // 0.75 s at 30 fps is exactly frame 22.5: the server's frame_of says 22.
    expect(frameIndex(0.75, 30)).toBe(tbFrameOf(0.75, 30))
    expect(frameIndex(0.75, 30)).toBe(22)
    expect(formatTimecode(0.75, 30)).toBe('00:00:00:22')
    expect(formatTimecode(0.75, 30)).toBe(formatTimecode(toFrameGrid(0.75, 30), 30))
    // Every displaySeekTime-style (n + 0.5)/fps agrees with the step grid.
    for (const fps of [30, 25, 24, 30000 / 1001, 60]) {
      for (let n = 0; n < 300; n++) {
        const t = (n + 0.5) / fps
        expect(frameIndex(t, fps)).toBe(tbFrameOf(t, fps))
      }
    }
  })

  it('typed seconds and frame counts land on the timebase grid', () => {
    expect(parseTimecode('0.75', 30)).toBe(toFrameGrid(0.75, 30))
    expect(parseTimecode('90f', 30000 / 1001)).toBe(tbTimeOf(90, 30000 / 1001))
    expect(parseTimecode('00:00:03:00', 30000 / 1001)).toBe(tbTimeOf(90, 30000 / 1001))
  })
})
