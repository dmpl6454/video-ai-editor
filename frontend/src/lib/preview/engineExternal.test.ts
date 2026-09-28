// ExternalPauses (engineExternal.ts): pauses the engine did not issue stop
// picture and sound together and resume both only per the user's intent.
import { describe, expect, it } from 'vitest'
import { ExternalPauses, type ExternalHost } from './engineExternal'
import { DelayedFlag } from './engineDraw'
import { NullAudioSink } from './engineOptions'
import type { ExternalPauseEvent } from './engine'

function host(over: Partial<ExternalHost> = {}) {
  const log: string[] = []
  const pauses: ExternalPauseEvent[] = []
  const st = { playing: true, intent: 'play' as 'play' | 'pause', canDraw: true }
  const h: ExternalHost = {
    opts: {},
    isPlaying: () => st.playing,
    isDestroyed: () => false,
    presentedK: () => 42,
    intent: () => st.intent,
    setIntent: (i) => { st.intent = i },
    stopPlayback: (why) => { log.push(`stop:${why}`); st.playing = false },
    play: () => { log.push('play'); st.playing = true },
    sink: () => new NullAudioSink(),
    canDraw: () => st.canDraw,
    emitPause: (e) => pauses.push(e),
    emitStatus: () => undefined,
    onHidden: () => undefined,
    onShown: () => undefined,
    elementMoved: () => log.push('moved'),
    ...over,
  }
  return { h, log, pauses, st }
}

describe('ExternalPauses', () => {
  it('a context loss while playing stops both at presentedK and keeps the intent to play', () => {
    const { h, log, pauses, st } = host()
    const x = new ExternalPauses(h)
    x.pause('context')
    expect(log).toEqual(['stop:external:context'])
    expect(pauses).toEqual([{ k: 42, cause: 'context', willResume: true }])
    expect(st.intent).toBe('play')
    expect(x.onContextRestored()).toBe(true)
    expect(log).toEqual(['stop:external:context', 'play'])
  })

  it('a restore after the user paused does not resume', () => {
    const { h, log } = host()
    const x = new ExternalPauses(h)
    x.pause('context')
    h.setIntent('pause')
    x.reset()
    expect(x.onContextRestored()).toBe(false)
    expect(log).toEqual(['stop:external:context'])
  })

  it('an element pause gives up the intent to play (no resume)', () => {
    const { h, pauses, st } = host()
    new ExternalPauses(h).pause('element')
    expect(pauses[0].willResume).toBe(false)
    expect(st.intent).toBe('pause')
  })
})

describe('the element playing by itself (WebKit, an occluded window flickering visible)', () => {
  // measured in WKWebView (tests/wk/test_wk_integration external pause):
  // the parked element fired 'play', was paused again at once, and still
  // presented frames 178 → 223 while reporting paused. play() trusted its
  // stale position and resumed picture and sound 1.4 s past the stop frame.
  function fakeVideo() {
    const on: Record<string, () => void> = {}
    const v = { paused: true, pauses: 0, addEventListener: (t: string, f: () => void) => { on[t] = f }, pause() { v.pauses++; v.paused = true } }
    return { v, on, video: v as unknown as HTMLVideoElement }
  }

  it('is parked, and where it stands is forgotten (the next play or paused show seeks it)', () => {
    const { h, log, st } = host()
    st.playing = false
    const x = new ExternalPauses(h)
    const f = fakeVideo()
    x.listen(f.video)
    f.v.paused = false
    f.on.play()
    expect(f.v.pauses).toBe(1)
    expect(log).toEqual(['moved'])
  })

  it("the engine's own play is not a move", () => {
    const { h, log } = host()
    const x = new ExternalPauses(h)
    const f = fakeVideo()
    x.listen(f.video)
    f.on.play()
    expect(f.v.pauses).toBe(0)
    expect(log).toEqual([])
  })
})

describe('DelayedFlag', () => {
  it('rises only if still wanted after the delay; falls at once', async () => {
    let changes = 0
    const f = new DelayedFlag(5, () => { changes++ })
    f.set(true, () => false)
    await new Promise((r) => setTimeout(r, 15))
    expect(f.on).toBe(false)
    f.set(true)
    await new Promise((r) => setTimeout(r, 15))
    expect(f.on).toBe(true)
    f.set(false)
    expect(f.on).toBe(false)
    expect(changes).toBe(2)
  })
})
