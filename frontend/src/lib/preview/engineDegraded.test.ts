// DegradedTier (engineDegraded.ts, spec §7): the engine's side of the
// degraded <video> tier — which frames it owns, texture reuse, the upload
// only while the frame is still wanted, one element for the engine's life.
import { describe, expect, it, vi } from 'vitest'
import { DegradedTier, type DegradedHost } from './engineDegraded'
import type { ProgramFeed } from './engineFeed'
import type { Compositor } from './render/compositor'
import type { DegradedRequest } from './media/degradedSource'

const INFO = { rate: { num: 30, den: 1 }, tb: { num: 1, den: 15360 }, frames: 300, startTicks: 0, w: 1280, h: 720 }
const req = (frame: number): DegradedRequest => ({ url: '/m/P.mp4', info: INFO, frame, contentId: 7000 + frame })

function rig(textureContent = -1) {
  const log: string[] = []
  const comp = { lost: false, textureContent, upload: vi.fn(() => true) } as unknown as Compositor
  const feed = { degradedAt: (k: number) => (k >= 10 && k < 20 ? req(k + 100) : null) } as unknown as ProgramFeed
  let wanted = true
  const host: DegradedHost = {
    root: () => null,
    compositor: () => comp,
    wanted: () => wanted,
    draw: (k) => log.push(`draw:${k}`),
    created: () => log.push('created'),
    failed: (k, why) => log.push(`failed:${k}:${why}`),
  }
  return { tier: new DegradedTier(host, feed), log, comp, setWanted: (w: boolean) => { wanted = w } }
}

describe('DegradedTier', () => {
  it('owns exactly the frames the feed names (failed proxy, master known)', () => {
    const { tier } = rig()
    expect(tier.request(9)).toBeNull()
    expect(tier.request(10)?.frame).toBe(110)
    expect(tier.request(20)).toBeNull()
  })

  it('a texture already holding the frame is drawn at once: no element, no seek', () => {
    const { tier, log } = rig(7000 + 112)
    tier.show(12, req(112))
    expect(log).toEqual(['draw:12'])
    expect(tier.element).toBeNull()
  })

  it('otherwise creates ONE element (counted) and waits for the master frame', () => {
    const el = Object.assign(new EventTarget(), {
      src: '', currentTime: 0, seeking: false, readyState: 0, videoWidth: 1280, videoHeight: 720, muted: false, preload: '',
      style: {}, playsInline: false, disableRemotePlayback: false,
      pause() {}, load() {}, removeAttribute() {}, setAttribute() {},
    })
    vi.stubGlobal('document', { createElement: () => el })
    try {
      const { tier, log } = rig()
      tier.show(12, req(112))
      tier.show(13, req(113))
      expect(log).toEqual(['created'])
      expect(tier.pending).toBe(13)
      expect(el.src).toBe('/m/P.mp4')
    } finally {
      vi.unstubAllGlobals()
    }
  })
})
