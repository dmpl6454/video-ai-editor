import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { approxLabel, approxSummary } from './fidelityLabels'
import { liveApproxReasons, publishLiveApprox } from './liveApprox'

// Review RE: the engine classed Spin, Zoom Out In, a rotated canvas and the
// pitch / Hall voice effects APPROX, but nothing said so on screen.
describe('the ≈ chip words', () => {
  it('names each APPROX reason in words', () => {
    expect(approxLabel('anim:in:spin')).toBe('Spin In animation')
    expect(approxLabel('anim:out:spin')).toBe('Spin Out animation')
    expect(approxLabel('anim:in:zoom_out')).toBe('Zoom Out In animation')
    expect(approxLabel('canvas:blur:rotated')).toBe('Canvas behind a rotated clip')
    expect(approxLabel('audio:voice:deep')).toBe('Voice effect: Deep')
    expect(approxLabel('audio:voice:reverb')).toBe('Voice effect: Hall')
    expect(approxLabel('blend:soft_light')).toBe('Blend: Soft Light')
    expect(approxLabel('audio:limiting')).toBe('Limiter on loud sound')
    expect(approxLabel('something:new')).toBe('something:new')
    expect(approxSummary(['audio:voice:deep', 'anim:in:spin', 'audio:voice:deep']))
      .toBe('Approximate preview: Voice effect: Deep, Spin In animation. The export is exact.')
  })
  it('publishes live overlay reasons once per change', () => {
    publishLiveApprox(['blend:soft_light', 'blend:soft_light'])
    const a = liveApproxReasons()
    publishLiveApprox(['blend:soft_light'])
    expect(liveApproxReasons()).toBe(a)          // unchanged: the same array, no re-render
    publishLiveApprox([])
    expect(liveApproxReasons()).toEqual([])
  })
})

// Final QA r3: a moved or scaled clip over a Canvas background showed
// "Approximate preview: canvas:blur:moved" — the raw code. Every reason code
// support.ts can emit (read from its source, so a new one cannot slip by)
// must come back as words.
describe('every support.ts reason code has words', () => {
  const src = readFileSync(new URL('./timeline/support.ts', import.meta.url), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')
  const codes = new Set<string>()
  // any literal shaped like a code with a colon: 'audio:duck', `canvas:${t}:moved`
  for (const m of src.matchAll(/(['`])([a-z][a-z-]*:[^'`\s]*)\1/g)) codes.add(m[2])
  // and the colon-less ones: the reason slot of a (mode, reason) pair
  for (const m of src.matchAll(/\[(?:MODE_\w+|caps\.\w+|[A-Z_]+_MODE), (['`])([a-z][a-z-]*)\1\]/g)) codes.add(m[2])
  const sample = [...codes].map((c) => c.replace(/\$\{[^}]*\}/g, 'blur'))

  it('finds the codes', () => {
    for (const c of ['canvas:blur:moved', 'canvas:blur:rotated', 'audio:varispeed', 'chromakey', 'motion-track', 'audio:loudness']) {
      expect(sample).toContain(c)
    }
  })
  it('turns each into words, never the code', () => {
    for (const c of sample) {
      const w = approxLabel(c)
      expect(w, c).not.toBe(c)
      expect(w, c).not.toMatch(/[a-z]:[a-z]/)
    }
  })
  it('names the moved canvas and Keep-pitch-off sound as the chip says them', () => {
    expect(approxLabel('canvas:blur:moved')).toBe('Canvas behind a moved or resized clip')
    expect(approxLabel('canvas:color:moved')).toBe('Canvas behind a moved or resized clip')
    expect(approxLabel('audio:varispeed')).toBe('Speed change without Keep pitch (sound)')
    expect(approxSummary(['canvas:blur:moved'])).toBe(
      'Approximate preview: Canvas behind a moved or resized clip. The export is exact.')
  })
})

// K2 (0.8.0 QA): with "Loudness" on every frame the chip read as if that
// were all; each approximation keeps its own words next to the others.
describe('the chip names every reason of the frame', () => {
  it('keeps "Voice effect: Deep" beside "Loudness"', () => {
    const s = approxSummary(['audio:loudness', 'audio:voice:deep'])
    expect(s).toContain('Voice effect: Deep')
    expect(s).toContain('Loudness')
    expect(approxSummary(['audio:voice:deep', 'anim:in:spin'])).toBe(
      'Approximate preview: Voice effect: Deep, Spin In animation. The export is exact.')
  })
})
