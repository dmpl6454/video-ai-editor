// syncPipVideo's playback behaviour.
//
// Reported as "the pip video keeps flickering when the video is played and
// sometimes it gets vanished". The hidden element backing a PIP was SEEKED
// every frame: the playhead advances ~1/fps per frame while the tolerance was
// max(1/fps, 0.03), so every single frame re-assigned `currentTime`. Each
// assignment starts an asynchronous seek, `readyState` dips below
// HAVE_CURRENT_DATA while it runs, and the draw loop painted its no-frame
// placeholder instead — which is the flicker, and the "vanish" when the seeks
// never settle. Playing a decoder is what it is built for; seeking it 30 times
// a second is not.

import { describe, expect, it, vi } from 'vitest'
import { syncPipVideo, pausePipVideosExcept } from './pipDraw'

/** Minimal stand-in for the bits of HTMLVideoElement syncPipVideo touches. */
function fakeVideo(over: Partial<HTMLVideoElement> = {}) {
  const seeks: number[] = []
  let _ct = 0
  const v = {
    duration: 10,
    paused: true,
    playbackRate: 1,
    playCalls: 0,
    pauseCalls: 0,
    get currentTime() { return _ct },
    set currentTime(t: number) { _ct = t; seeks.push(t) },
    play() { this.playCalls++; this.paused = false; return Promise.resolve() },
    pause() { this.pauseCalls++; this.paused = true },
    ...over,
  }
  return { v: v as unknown as HTMLVideoElement & { playCalls: number; pauseCalls: number }, seeks }
}

describe('syncPipVideo while PLAYING', () => {
  it('starts the element playing instead of seeking it', () => {
    const { v, seeks } = fakeVideo()
    syncPipVideo(v, 0, 0, 0, 30, { playing: true, rate: 1 })
    expect(v.playCalls).toBe(1)
    expect(v.paused).toBe(false)
    expect(seeks).toEqual([])          // already on time — no seek needed
  })

  it('does NOT seek once per frame — the actual defect', () => {
    const { v, seeks } = fakeVideo()
    syncPipVideo(v, 0, 0, 0, 30, { playing: true, rate: 1 })
    // Walk a second of playback the way the rAF loop does, with the element
    // keeping time as a playing decoder would.
    for (let i = 1; i <= 30; i++) {
      ;(v as unknown as { currentTime: number }).currentTime = i / 30
      seeks.length = 0                 // ignore our own bookkeeping write
      syncPipVideo(v, i / 30, 0, 0, 30, { playing: true, rate: 1 })
      expect(seeks).toEqual([])
    }
  })

  it('corrects REAL drift, so a stall still recovers', () => {
    const { v, seeks } = fakeVideo({ paused: false })
    syncPipVideo(v, 5, 0, 0, 30, { playing: true, rate: 1 })   // element at 0, wants 5
    expect(seeks).toEqual([5])
  })

  it('ignores drift below the resync tolerance', () => {
    const { v, seeks } = fakeVideo({ paused: false })
    syncPipVideo(v, 0.1, 0, 0, 30, { playing: true, rate: 1 })
    expect(seeks).toEqual([])
  })

  it('follows the shuttle rate', () => {
    const { v } = fakeVideo()
    syncPipVideo(v, 0, 0, 0, 30, { playing: true, rate: 2 })
    expect(v.playbackRate).toBe(2)
  })

  it('treats rate 0 as paused rather than playing at a standstill', () => {
    const { v } = fakeVideo()
    syncPipVideo(v, 0, 0, 0, 30, { playing: true, rate: 0 })
    expect(v.playCalls).toBe(0)
  })

  it('honours the clip trim when starting', () => {
    // in_=2 means timeline 0 shows source second 2.
    const { v, seeks } = fakeVideo()
    syncPipVideo(v, 0, 0, 2, 30, { playing: true, rate: 1 })
    expect(seeks).toEqual([2])
    expect(v.playCalls).toBe(1)
  })
})

describe('syncPipVideo while PAUSED', () => {
  it('still seeks, which is what scrubbing needs', () => {
    const { v, seeks } = fakeVideo()
    syncPipVideo(v, 3, 0, 0, 30)
    expect(seeks).toEqual([3])
  })

  it('pauses an element left running by playback', () => {
    const { v } = fakeVideo({ paused: false })
    syncPipVideo(v, 0, 0, 0, 30, { playing: false })
    expect(v.pauseCalls).toBe(1)
  })

  it('does not seek for a sub-frame difference', () => {
    const { v, seeks } = fakeVideo({ paused: true })
    ;(v as unknown as { currentTime: number }).currentTime = 1
    seeks.length = 0
    syncPipVideo(v, 1.01, 0, 0, 30)
    expect(seeks).toEqual([])
  })

  it('clamps to the source, never past its end', () => {
    const { v, seeks } = fakeVideo()
    syncPipVideo(v, 999, 0, 0, 30)
    expect(seeks[0]).toBeLessThan(10)
    expect(seeks[0]).toBeGreaterThan(9.9)
  })

  it('does nothing at all before duration is known', () => {
    const { v, seeks } = fakeVideo({ duration: NaN })
    syncPipVideo(v, 3, 0, 0, 30, { playing: true })
    expect(seeks).toEqual([])
    expect(v.playCalls).toBe(0)
  })
})

describe('pausePipVideosExcept', () => {
  it('is safe with nothing registered', () => {
    expect(() => pausePipVideosExcept(new Set())).not.toThrow()
  })
})

// --- the elements must be IN the document ----------------------------------
//
// A detached <video> can be seeked and drawn from, which is all this file did
// originally. Once the flicker fix switched it to PLAYING them, being attached
// stopped being optional: Blink runs a detached element, WebKit makes no such
// promise, and WKWebView is what the packaged macOS app renders in. So the
// failure would be invisible on the machine this was written on and would
// appear on a Mac as the PIP frozen on one frame during playback.

describe('pipVideo host attachment', () => {
  it('appends the element to an offscreen host that is NOT display:none', async () => {
    // Minimal stand-in: this suite runs in `environment: 'node'`, so there is
    // no document at all unless we provide one.
    const made: Record<string, unknown>[] = []
    // A real `document.body.appendChild` sets `isConnected` on the child; the
    // host-reuse check reads it, so the stub has to as well or the test
    // measures its own shortcut instead of the code.
    const body = {
      children: [] as unknown[],
      appendChild(c: Record<string, unknown>) { this.children.push(c); c.isConnected = true },
    }
    const mk = (tag: string) => {
      const el: Record<string, unknown> = {
        tagName: tag.toUpperCase(), style: { cssText: '' }, dataset: {},
        isConnected: false, children: [] as unknown[],
        setAttribute() {},
        appendChild(c: Record<string, unknown>) {
          ;(this.children as unknown[]).push(c); c.isConnected = true
        },
      }
      made.push(el)
      return el
    }
    ;(globalThis as Record<string, unknown>).document = { createElement: mk, body }
    try {
      // A fresh module instance, so the cached VIDEO_HOST/VIDEOS from another
      // test can't satisfy this one. `import('./x?query')` also works in vitest
      // but does NOT type-resolve, and `tsc -b` is a CI gate.
      vi.resetModules()
      const mod = await import('./pipDraw')
      const v = mod.pipVideo('s1', 'blob:x') as unknown as Record<string, unknown>
      expect(v.isConnected).toBe(true)
      const host = made.find((e) => e.tagName === 'DIV')!
      const css = (host.style as { cssText: string }).cssText
      // display:none would let an engine stop feeding the decoder — the exact
      // thing this host exists to avoid. Off-screen and transparent instead.
      expect(css).not.toContain('display:none')
      expect(css).toContain('opacity:0')
      expect(css).toContain('pointer-events:none')
    } finally {
      delete (globalThis as Record<string, unknown>).document
    }
  })

  it('gives the source preview its OWN element for the same file', async () => {
    // THE BUG. `pipVideo` keys on `src` alone, so the same file on v1 and on a
    // PIP lane handed StickerLayer's loop and Preview's source-preview loop one
    // element. Each called syncPipVideo on it every frame with a different clip
    // trim (measured live: v1 at in=0, a circle PIP of the same file at
    // in=4.0125), the seeks never settled, readyState never reached
    // HAVE_CURRENT_DATA, and the source canvas painted only its black fill —
    // the main video went black for the whole rotation drag while the PIP kept
    // drawing off its LAST_FRAME cache.
    //
    // Note it only bites when the two clips point at DIFFERENT source times: at
    // the same in-point both loops want the same instant and syncPipVideo's
    // tolerance suppresses the seek. An earlier repro used in=0 for both and
    // proved nothing.
    const made: Record<string, unknown>[] = []
    const body = {
      children: [] as unknown[],
      appendChild(c: Record<string, unknown>) { this.children.push(c); c.isConnected = true },
    }
    const mk = (tag: string) => {
      const el: Record<string, unknown> = {
        tagName: tag.toUpperCase(), style: { cssText: '' }, dataset: {},
        isConnected: false, children: [] as unknown[],
        setAttribute() {},
        appendChild(c: Record<string, unknown>) {
          ;(this.children as unknown[]).push(c); c.isConnected = true
        },
      }
      made.push(el)
      return el
    }
    ;(globalThis as Record<string, unknown>).document = { createElement: mk, body }
    try {
      vi.resetModules()
      const mod = await import('./pipDraw')
      const same = '/uploads/car.mp4'
      const pip = mod.pipVideo(same, 'blob:car')
      const srcp = mod.sourcePreviewVideo(same, 'blob:car')
      expect(srcp).not.toBe(pip)          // two owners of one clock is the bug
      // ...and each pool is still internally stable.
      expect(mod.pipVideo(same, 'blob:car')).toBe(pip)
      expect(mod.sourcePreviewVideo(same, 'blob:car')).toBe(srcp)
    } finally {
      delete (globalThis as Record<string, unknown>).document
    }
  })

  it('does not let pausePipVideosExcept touch the source-preview element', async () => {
    // That helper pauses every element IT owns that is not a currently-active
    // PIP. A v1 source is not a PIP at all, so a shared pool would have it
    // pausing the very element the source preview is driving.
    const body = {
      children: [] as unknown[],
      appendChild(c: Record<string, unknown>) { this.children.push(c); c.isConnected = true },
    }
    const mk = (tag: string) => ({
      tagName: tag.toUpperCase(), style: { cssText: '' }, dataset: {},
      isConnected: false, children: [] as unknown[],
      setAttribute() {},
      appendChild(c: Record<string, unknown>) {
        ;(this.children as unknown[]).push(c); c.isConnected = true
      },
      pause() { (this as Record<string, unknown>).paused = true },
      paused: false,
    })
    ;(globalThis as Record<string, unknown>).document = { createElement: mk, body }
    try {
      vi.resetModules()
      const mod = await import('./pipDraw')
      const v = mod.sourcePreviewVideo('/uploads/car.mp4', 'blob:car') as unknown as
        { paused: boolean }
      v.paused = false
      mod.pausePipVideosExcept(new Set())     // "no PIP is active"
      expect(v.paused).toBe(false)
    } finally {
      delete (globalThis as Record<string, unknown>).document
    }
  })

  it('reuses one host across sources rather than one per video', async () => {
    const made: Record<string, unknown>[] = []
    // A real `document.body.appendChild` sets `isConnected` on the child; the
    // host-reuse check reads it, so the stub has to as well or the test
    // measures its own shortcut instead of the code.
    const body = {
      children: [] as unknown[],
      appendChild(c: Record<string, unknown>) { this.children.push(c); c.isConnected = true },
    }
    const mk = (tag: string) => {
      const el: Record<string, unknown> = {
        tagName: tag.toUpperCase(), style: { cssText: '' }, dataset: {},
        isConnected: false, children: [] as unknown[],
        setAttribute() {},
        appendChild(c: Record<string, unknown>) {
          ;(this.children as unknown[]).push(c); c.isConnected = true
        },
      }
      made.push(el)
      return el
    }
    ;(globalThis as Record<string, unknown>).document = { createElement: mk, body }
    try {
      vi.resetModules()
      const mod = await import('./pipDraw')
      mod.pipVideo('a', 'blob:a')
      mod.pipVideo('b', 'blob:b')
      expect(made.filter((e) => e.tagName === 'DIV')).toHaveLength(1)
      expect(body.children).toHaveLength(1)
    } finally {
      delete (globalThis as Record<string, unknown>).document
    }
  })
})

// --- rotating the picture INSIDE the shape ---------------------------------
//
// Mirrors render/pip.py's cover -> rotate -> crop. The growth is the whole
// point: rotating a box-sized cover in place drags its transparent corners
// into the shape, so the cover has to be enlarged first.

import { pipInnerPlan } from './pipDraw'

describe('pipInnerPlan', () => {
  it('is a no-op at 0 degrees', () => {
    const p = pipInnerPlan(400, 300, { rotation: 0 })
    expect(p.coverScale).toBeCloseTo(1)
    expect(p.destW).toBeCloseTo(400)
    expect(p.destH).toBeCloseTo(300)
    expect(p.rotRad).toBe(0)
  })

  it('grows the cover enough that no corner reaches the box', () => {
    for (const deg of [15, 30, 45, 90, -30, 175]) {
      const p = pipInnerPlan(400, 300, { rotation: deg })
      const r = (deg * Math.PI) / 180
      const needW = 400 * Math.abs(Math.cos(r)) + 300 * Math.abs(Math.sin(r))
      const needH = 400 * Math.abs(Math.sin(r)) + 300 * Math.abs(Math.cos(r))
      expect(p.destW + 1e-6).toBeGreaterThanOrEqual(needW)
      expect(p.destH + 1e-6).toBeGreaterThanOrEqual(needH)
    }
  })

  it('needs the most growth at 45 degrees on a square box', () => {
    const a = pipInnerPlan(300, 300, { rotation: 45 }).coverScale
    const b = pipInnerPlan(300, 300, { rotation: 10 }).coverScale
    expect(a).toBeGreaterThan(b)
    expect(a).toBeCloseTo(Math.SQRT2, 3)
  })

  it('composes with zoom rather than replacing it', () => {
    const z = pipInnerPlan(400, 300, { zoom: 2 })
    const zr = pipInnerPlan(400, 300, { zoom: 2, rotation: 45 })
    expect(z.destW).toBeCloseTo(800)
    expect(zr.destW).toBeGreaterThan(z.destW)
  })

  it('clamps zoom below 1, matching the renderer', () => {
    // Sub-1 leaves less source than box and bakes black — pip.py clamps too.
    expect(pipInnerPlan(400, 300, { zoom: 0.2 }).destW).toBeCloseTo(400)
  })

  it('pans AFTER the rotation, in the box axes, with the renderer sign', () => {
    // crop x = (in-out)/2 + f*(in-out)/2 — the window moves right, so the
    // picture moves LEFT. +x must therefore translate negative.
    const p = pipInnerPlan(400, 300, { zoom: 2, x: 1 })
    expect(p.offX).toBeCloseTo(-(800 - 400) / 2)
    expect(pipInnerPlan(400, 300, { zoom: 2, x: 0 }).offX).toBe(0)
  })

  it('has no pan headroom when there is no margin', () => {
    // zoom 1, no rotation: the cover IS the box, so a pan cannot move anything
    // — the same no-op the renderer's clamp produces.
    expect(pipInnerPlan(400, 300, { zoom: 1, x: 1, y: -1 }).offX).toBe(0)
    expect(pipInnerPlan(400, 300, { zoom: 1, x: 1, y: -1 }).offY).toBe(0)
  })

  it('clamps the normalised pan to -1..1', () => {
    const a = pipInnerPlan(400, 300, { zoom: 2, x: 5 })
    const b = pipInnerPlan(400, 300, { zoom: 2, x: 1 })
    expect(a.offX).toBeCloseTo(b.offX)
  })

  it('survives a degenerate box', () => {
    const p = pipInnerPlan(0, 0, { rotation: 45 })
    expect(Number.isFinite(p.destW)).toBe(true)
    expect(p.coverScale).toBe(1)
  })
})

describe('rotateHandleLocal', () => {
  it('sits on a stem above the top edge', async () => {
    const dv = await import('./dragVisuals')
    const at = dv.rotateHandleLocal(50)
    expect(at.lx).toBe(0)
    expect(at.ly).toBe(-50 - dv.ROT_GAP)
  })

  it('flips below when there is no room above', async () => {
    // Chrome drawn outside the overlay canvas is invisible AND unclickable —
    // the same failure the delete handle's corner search exists to avoid.
    const dv = await import('./dragVisuals')
    const at = dv.rotateHandleLocal(50, true)
    expect(at.ly).toBe(50 + dv.ROT_GAP)
  })
})
