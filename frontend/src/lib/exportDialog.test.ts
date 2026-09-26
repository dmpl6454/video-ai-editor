// QA-100 / QA-009: the Export dialog's choices → POST /export body, and the
// facts it shows (frame rates, size estimate, file name).
import { describe, expect, it } from 'vitest'
import {
  EXPORT_FORMATS, FRAME_RATES, PLATFORM_SPECS, WAV_KBPS, audioDescription, estimateAudioOnlyBytes, estimateBytes,
  estimateVideoKbps, exportBody, exportFileName, frameRateOptions, isAudioOnly, sameRate,
} from './exportOptions'

const C30 = { w: 1920, h: 1080, fps: 30 }
const C2997 = { w: 1080, h: 1920, fps: 30000 / 1001 }

describe('frame rate (QA-009 remainder)', () => {
  it('sends fps only when it differs from the project rate', () => {
    expect(exportBody(C30, 0, 18, 30)).not.toHaveProperty('fps')
    expect(exportBody(C30, 0, 18, null)).not.toHaveProperty('fps')
    expect(exportBody(C30, 0, 18, 25)).toMatchObject({ fps: 25, crf: 18 })
    expect(exportBody(C2997, 1080, 18, 24000 / 1001)).toMatchObject({ height: 1080, fps: 24000 / 1001 })
    expect(exportBody(C2997, 0, 18, 29.97)).not.toHaveProperty('fps')   // 29.97 IS 30000/1001
  })
  it('offers the delivery rates, the project rate first', () => {
    const o = frameRateOptions(C2997)
    expect(o[0]).toEqual({ value: '', label: 'Project (29.97 fps)' })
    expect(o.map((x) => x.label)).toEqual(['Project (29.97 fps)', '23.976 fps', '24 fps', '25 fps', '30 fps', '50 fps', '59.94 fps', '60 fps'])
    expect(FRAME_RATES.some((r) => sameRate(r.fps, 59.94))).toBe(true)
  })
})

describe('size estimate and file name (QA-100)', () => {
  it('a platform target is exact and scales with pixels like the backend', () => {
    const yt = { ...C30, bitrate_kbps: 12000 }
    expect(estimateVideoKbps(yt, 0, 'platform')).toBe(12000)
    expect(estimateVideoKbps(yt, 720, 'platform')).toBe(Math.round(12000 * (1280 * 720) / (1920 * 1080)))
    expect(estimateBytes(12000, 60)).toBe((12000 + 192) * 1000 / 8 * 60)
  })
  it('quality mode grows with size and frame rate, shrinks with Small file', () => {
    const hi = estimateVideoKbps(C30, 1080, 18)
    expect(estimateVideoKbps(C30, 720, 18)).toBeLessThan(hi)
    expect(estimateVideoKbps(C30, 1080, 18, 60)).toBeGreaterThan(hi)
    expect(estimateVideoKbps(C30, 1080, 28)).toBeLessThan(estimateVideoKbps(C30, 1080, 23))
  })
  it('the name becomes a safe leaf with the container extension', () => {
    expect(exportFileName('My reel: take 2/final', 'mp4')).toBe('My reel take 2 final.mp4')
    expect(exportFileName('clip.mov', 'mov')).toBe('clip.mov')
    expect(exportFileName('  ', 'mp4')).toBe('export.mp4')
  })
  it('YouTube 16:9 is a platform the UI can reach', () => {
    expect(PLATFORM_SPECS.find((p) => p.preset === 'youtube_16x9')).toMatchObject({ w: 1920, h: 1080, lufs: -14 })
  })
})

describe('audio-only export (QA-100 remainder)', () => {
  it('offers the sound alone next to the video formats', () => {
    expect(EXPORT_FORMATS.map((f) => f.value)).toEqual(['mp4', 'mov', 'm4a', 'wav'])
    expect(EXPORT_FORMATS.filter((f) => isAudioOnly(f.value)).map((f) => f.label)).toEqual(['Audio M4A', 'Audio WAV'])
  })
  it('estimates the audio stream alone — 192 kbps AAC, or 24-bit 48 kHz stereo PCM', () => {
    expect(estimateAudioOnlyBytes('m4a', 60)).toBe(192 * 1000 / 8 * 60)
    expect(WAV_KBPS).toBe(2304)
    expect(estimateAudioOnlyBytes('wav', 10)).toBe(2304 * 1000 / 8 * 10)
    expect(audioDescription('wav')).toMatch(/24-bit/)
  })
  it('names the file with the audio extension', () => {
    expect(exportFileName('Episode 1.mp4', 'm4a')).toBe('Episode 1.m4a')
    expect(exportFileName('Episode 1.wav', 'wav')).toBe('Episode 1.wav')
  })
})
