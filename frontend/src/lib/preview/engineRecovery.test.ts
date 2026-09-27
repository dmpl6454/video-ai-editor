// EngineRecovery (spec §7 fallback ladder, §13 P1-R1): decode errors rebuild
// laneA below 3 in 60 s and fall back to the server preview at the 3rd; a
// lost WebGL context not restored in 2 s falls back too.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { CONTEXT_RESTORE_MS, DECODE_ERROR_WINDOW_MS, EngineRecovery, type RecoveryHost } from './engineRecovery'

function host(over: Partial<RecoveryHost> = {}) {
  const log: string[] = []
  const st = { playing: false, lost: false, t: 0, snapshotK: 7, presented: 7 }
  const h: RecoveryHost = {
    isPlaying: () => st.playing,
    presentedK: () => st.presented,
    live: () => true,
    compositorLost: () => st.lost,
    snapshotK: () => st.snapshotK,
    clearSnapshot: () => log.push('clearSnapshot'),
    spinner: (on) => log.push(`spinner:${on}`),
    pauseExternal: (c) => { log.push(`pause:${c}`); st.playing = false },
    resumeAfterRestore: () => false,
    forgetElementFrame: () => log.push('forget'),
    showSnapshot: (on) => log.push(`snapshot:${on}`),
    showPaused: () => log.push('showPaused'),
    rebuildLane: () => log.push('rebuild'),
    stopPlayback: (why) => { log.push(`stop:${why}`); st.playing = false },
    play: () => { log.push('play'); st.playing = true },
    fallback: (r) => log.push(`fallback:${r}`),
    emitStatus: () => undefined,
    now: () => st.t,
    ...over,
  }
  return { h, log, st }
}

beforeEach(() => { vi.useFakeTimers(); vi.spyOn(console, 'warn').mockImplementation(() => undefined) })
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

describe('decode errors', () => {
  it('the 1st and 2nd rebuild the lane and re-show; the 3rd within 60 s falls back', () => {
    const { h, log, st } = host()
    const r = new EngineRecovery(h)
    r.onMediaError(3)
    expect(log).toEqual(['rebuild', 'showPaused'])
    st.t = 10_000
    r.onMediaError(3)
    expect(log.filter((x) => x === 'rebuild')).toHaveLength(2)
    expect(log.some((x) => x.startsWith('fallback'))).toBe(false)
    st.t = 20_000
    r.onMediaError(3)
    expect(log.at(-1)).toBe('fallback:decode-errors')
    expect(log.filter((x) => x === 'rebuild')).toHaveLength(2)
    expect(r.stats.decodeErrors).toBe(3)
  })

  it('errors spread wider than 60 s never fall back', () => {
    const { h, log, st } = host()
    const r = new EngineRecovery(h)
    for (let i = 0; i < 6; i++) {
      st.t = i * (DECODE_ERROR_WINDOW_MS / 2 + 1)
      r.onMediaError(3)
    }
    expect(log.some((x) => x.startsWith('fallback'))).toBe(false)
    expect(r.stats.laneRebuilds).toBe(6)
  })

  it('while playing: stops at presentedK, rebuilds, and plays on', () => {
    const { h, log, st } = host()
    st.playing = true
    new EngineRecovery(h).onMediaError(3)
    expect(log).toEqual(['stop:decode-error', 'rebuild', 'play'])
  })

  it('an engine that is not live ignores errors (teardown, server mode)', () => {
    const { h, log } = host({ live: () => false })
    const r = new EngineRecovery(h)
    for (let i = 0; i < 5; i++) r.onMediaError(3)
    expect(log).toEqual([])
    expect(r.recentErrors).toBe(0)
  })
})

describe('context loss', () => {
  it('not restored within 2 s: the server preview', () => {
    const { h, log, st } = host()
    const r = new EngineRecovery(h)
    st.lost = true
    r.onContextLost()
    vi.advanceTimersByTime(CONTEXT_RESTORE_MS - 1)
    expect(log.some((x) => x.startsWith('fallback'))).toBe(false)
    vi.advanceTimersByTime(2)
    expect(log.at(-1)).toBe('fallback:webgl-lost')
  })

  it('restored in time: no fallback; the paused frame is shown again from a fresh texture', () => {
    const { h, log, st } = host()
    const r = new EngineRecovery(h)
    st.lost = true
    r.onContextLost()
    vi.advanceTimersByTime(500)
    st.lost = false
    r.onContextRestored()
    vi.advanceTimersByTime(5000)
    expect(log).toEqual(['snapshot:false', 'forget', 'showPaused'])
  })

  it('while playing: a context pause; a snapshot of another frame is blacked out', () => {
    const { h, log, st } = host()
    st.playing = true
    st.presented = 110
    st.snapshotK = 60
    new EngineRecovery(h).onContextLost()
    expect(log).toEqual(['pause:context', 'clearSnapshot', 'spinner:true'])
  })
})
