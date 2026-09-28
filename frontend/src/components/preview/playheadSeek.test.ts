// Final QA (engine): Play after a paused seek issued a second, no-op PLAYING
// seek to the same frame 2–4 ms after play started — the engine stopped and
// restarted its sound, which opened the suspend race in audioEngine. The
// paused seek now records the time it sought to, so play from that frame is
// not a jump.
import { describe, expect, it } from 'vitest'
import { playheadSeek } from './playheadSeek'

describe('playheadSeek', () => {
  it('a paused seek seeks, and remembers where', () => {
    expect(playheadSeek(false, 6, 1.0)).toEqual({ seek: true, lastWritten: 6 })
  })
  it('play starting on the frame the paused seek went to is not a jump', () => {
    const paused = playheadSeek(false, 6, 1.0)
    expect(playheadSeek(true, 6, paused.lastWritten)).toEqual({ seek: false, lastWritten: 6 })
  })
  it('a jump while playing still seeks', () => {
    expect(playheadSeek(true, 9, 6.5)).toEqual({ seek: true, lastWritten: 9 })
  })
  it('the playing loop’s own writes are not seeks', () => {
    expect(playheadSeek(true, 6.5, 6.5)).toEqual({ seek: false, lastWritten: 6.5 })
  })
})
