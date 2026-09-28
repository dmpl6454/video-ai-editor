// Review RE (wave E fixer): support.ts classes the re-test found wrong.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { EdlLike } from './framePlan'
import type { SourceInfoJson } from './frameMap'
import { buildProgramMap, lookupFromJson } from './programMap'
import { ANIM_BLUR_MODE, classify, MODE_BAKED, MODE_EXACT, MODE_PENDING, PHASE_CAPS } from './support'

interface Case { name: string; edl: EdlLike; sources: Record<string, SourceInfoJson> }
const cases: Case[] = JSON.parse(readFileSync(fileURLToPath(
  new URL('../../../../../tests/goldens/frame_map/structure.json', import.meta.url)), 'utf8')).cases
const base = cases.find((c) => c.name === 'gaps_p30') ?? cases[0]

function build(edit: (clips: Array<Record<string, unknown>>) => void) {
  const edl = structuredClone(base.edl)
  edit(edl.tracks!.find((t) => t.id === 'v1')!.clips as Array<Record<string, unknown>>)
  return { edl, pm: buildProgramMap(edl, lookupFromJson(base.sources)) }
}

describe('Blur In / Out stays BAKED at every phase', () => {
  it('is not the shader-effects capability (APPROX from phase 4): the engine has no blur-mix pass', () => {
    expect(PHASE_CAPS[4].shaderEffects).not.toBe(MODE_BAKED)          // why this mattered
    expect(ANIM_BLUR_MODE).toBe(MODE_BAKED)
    const { edl, pm } = build((cl) => { cl[0].anim_in = 'blur_in' })
    const k = pm.clipStart[0] + 1
    for (const phase of [1, 2, 3, 4, 5] as const) {
      const s = classify(pm, edl, { phase })
      expect(s.mode[k], `phase ${phase}`).toBe(MODE_BAKED)
      expect(s.ranges.find((r) => k >= r.k0 && k < r.k1)!.reasons).toContain('anim:blur')
    }
  })
})

describe('an image canvas background whose picture is not ready', () => {
  it('makes its clip PENDING (the spinner), never EXACT black bars', () => {
    const { edl, pm } = build((cl) => { cl[0].canvas_bg = { type: 'image', image: '/p.png' } })
    const k = pm.clipStart[0] + 1
    const other = pm.clipStart[1] + 1
    const pending = classify(pm, edl, { phase: 1, canvasImagePending: (c) => (c as { id?: string }).id === cl0(edl) })
    expect(pending.mode[k]).toBe(MODE_PENDING)
    expect(pending.ranges.find((r) => k >= r.k0 && k < r.k1)!.reasons).toContain('canvas:image:pending')
    expect(pending.mode[other]).toBe(MODE_EXACT)
    expect(classify(pm, edl, { phase: 1, canvasImagePending: () => false }).mode[k]).toBe(MODE_EXACT)
  })
})

function cl0(edl: EdlLike): string {
  return String((edl.tracks!.find((t) => t.id === 'v1')!.clips[0] as { id?: string }).id)
}

describe('canvas background pictures are retried after a failure', () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })

  it('refetches a failed picture after its backoff, and at once on reconnect', async () => {
    const made: Array<{ src: string; onload?: () => void; onerror?: () => void }> = []
    class FakeImage { decoding = ''; onload?: () => void; onerror?: () => void; private s = ''
      set src(v: string) { this.s = v; made.push(this) } get src() { return this.s } }
    vi.stubGlobal('Image', FakeImage)
    let t = 1000
    vi.stubGlobal('performance', { now: () => t })
    const M = await import('../render/canvasBgImages')
    M.resetCanvasBgImages()
    expect(M.canvasBgImage('/a.png')).toBeNull()
    expect(made).toHaveLength(1)
    made[0].onerror!()
    expect(M.canvasBgImageState('/a.png')).toBe('error')
    expect(M.canvasBgImage('/a.png')).toBeNull()          // within the backoff: no new request
    expect(made).toHaveLength(1)
    t += 1500
    const ready = vi.fn()
    expect(M.canvasBgImage('/a.png', ready)).toBeNull()   // past it: refetched
    expect(made).toHaveLength(2)
    made[1].onload!()
    expect(ready).toHaveBeenCalledOnce()
    expect(M.canvasBgImageState('/a.png')).toBe('ready')
    // a second picture fails; the connection coming back retries it at once
    M.canvasBgImage('/b.png')
    made[2].onerror!()
    M.retryFailedCanvasBgImages()
    expect(made).toHaveLength(4)
  })
})
