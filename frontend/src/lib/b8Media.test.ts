import { describe, expect, it } from 'vitest'
import type { MediaItem } from '../types'
import { displayNameFor, itemsFor, namesBySrc, offlineSrcs, prettyDiskName } from './mediaNames'
import { importFollowUp, shortDuration } from './importFollowUp'
import { etaText } from './aiEta'
import { clipLabel } from './timelineLanes'
import { binMeta, binRows } from './mediaLibrary'

const item = (over: Partial<MediaItem>): MediaItem => ({
  id: 'a1b2c3d4e5f6', src: '/wd/s_1/uploads/upload_6f3d3a_1a2b3c4d/upload_6f3d3a.normalized.mp4',
  name: '"पहला वीडियो".mp4', kind: 'video', origin: 'upload', duration: 4, width: 1920, height: 1080,
  added: 1, uses: 1, clip_ids: ['c1'], ...over,
})

describe('media display names (QA-045)', () => {
  it('names a clip by the library, never by its disk file', () => {
    const it0 = item({})
    const names = namesBySrc([it0])
    expect(displayNameFor(it0.src, names)).toBe('"पहला वीडियो".mp4')
    const clip = { id: 'c1', src: it0.src, in: 0, out: 4, start: 0 } as never
    expect(clipLabel(clip, names)).toBe('"पहला वीडियो".mp4')
    expect(clipLabel(clip)).not.toContain('/')
  })

  it('falls back to a cleaned file name, not a path, before the library loads', () => {
    expect(displayNameFor('/a/b/take_1a2b3c4d/take.normalized.mp4', new Map())).toBe('take.mp4')
    expect(prettyDiskName('song_0a1b2c3d.mp3')).toBe('song.mp3')
  })

  it('only uses the library of the project on screen', () => {
    expect(itemsFor({ sid: 's_a', items: [item({})] }, 's_b')).toEqual([])
    expect(itemsFor({ sid: 's_a', items: [item({})] }, 's_a')).toHaveLength(1)
  })
})

describe('offline media (QA-095)', () => {
  it('flags missing items and says so in the bin', () => {
    const gone = item({ missing: true, duration: null })
    expect(offlineSrcs([gone, item({ src: '/x.mp4' })])).toEqual(new Set([gone.src]))
    const [row] = binRows([gone], null)
    expect(row.missing).toBe(true)
    expect(binMeta(row)).toMatch(/^Offline/)
  })

  it('labels a photo as a photo (QA-090)', () => {
    const [row] = binRows([item({ still: true, duration: 5 })], null)
    expect(binMeta(row)).toContain('photo')
  })
})

describe('import follow-ups (QA-083 / QA-092)', () => {
  it('offers Trim to video when an audio file runs past the picture', () => {
    const f = importFollowUp({ clip_id: 'm1', start: 6, past_video_s: 27, video_end: 20,
                               display_name: 'narration.wav' }, 'x.wav')
    expect(f?.message).toBe('narration.wav runs 27 s past the end of the video.')
    expect(f?.action).toEqual({ label: 'Trim to video', tool: 'trim_clip', args: { clip_id: 'm1', out: 14 } })
  })

  it('trims to the server\'s render-clock answer when it gives one (transitions)', () => {
    // Final QA (round 3): with transitions the picture ends before the v1
    // layout end, and the bed plays from render_time(start); the server
    // measures both and sends the source `out` that ends it on the picture.
    // A bed at layout 4.0 after a 0.5 s fade at 3.0 plays from render 3.5;
    // the picture ends at 5.5, so 2.0 s of it (not 5.5 − 4.0 = 1.5) stays.
    const f = importFollowUp({ clip_id: 'm1', start: 4, past_video_s: 26, video_end: 5.5,
                               trim_out: 2, display_name: 'n.wav' }, 'n.wav')
    expect(f?.action?.args).toEqual({ clip_id: 'm1', out: 2 })
  })

  it('says where an audio-only video file went', () => {
    const f = importFollowUp({ kind: 'audio', routed_to: 'music', display_name: 'podcast.mp4',
                               past_video_s: 0 }, 'podcast.mp4')
    expect(f).toEqual({ message: 'podcast.mp4 has no picture, so it went on the Music lane.' })
  })

  it('stays quiet for an ordinary import', () => {
    expect(importFollowUp({ kind: 'video' }, 'a.mp4')).toBeNull()
    expect(importFollowUp({ clip_id: 'm1', past_video_s: 0, video_end: 20 }, 'a.wav')).toBeNull()
    expect(shortDuration(65.2)).toBe('1:05')
  })
})

describe('AI tool time left (QA-066)', () => {
  it('extrapolates this run, and says nothing too early', () => {
    expect(etaText(1, 0.5)).toBe('')
    expect(etaText(30, 0.01)).toBe('')
    expect(etaText(60, 0.25)).toBe('about 3 min left')
    expect(etaText(20, 0.5)).toBe('about 20 s left')
    expect(etaText(10, 0.95)).toBe('a few seconds left')
    expect(etaText(10, 1)).toBe('')
  })
})

describe('media names break between words, never inside the extension (wave-B review)', () => {
  it('keeps the extension on the last piece', async () => {
    const { nameBreaks } = await import('./mediaNames')
    expect(nameBreaks('scene_16x9.mp4')).toEqual(['scene_', '16x9.mp4'])
    expect(nameBreaks('narration_en.wav')).toEqual(['narration_', 'en.wav'])
    expect(nameBreaks('chill_90bpm.wav')).toEqual(['chill_', '90bpm.wav'])
    expect(nameBreaks('My trip.final.mov')).toEqual(['My ', 'trip.', 'final.mov'])
    expect(nameBreaks('noext')).toEqual(['noext'])
    expect(nameBreaks('दिल्ली वीडियो.mp4').join('')).toBe('दिल्ली वीडियो.mp4')
  })
})
