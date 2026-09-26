import { describe, expect, it } from 'vitest'
import {
  DEFAULT_PREVIEW_ENGINE, INSTANT_PREVIEW_HELP, PREVIEW_ENGINE_CHOICES, parsePreviewSettings, previewEngineNote,
  probePreviewCapabilities, resolvePreviewMode, wantsClientEngine,
} from './previewEngineSetting'

describe('parsePreviewSettings', () => {
  it('reads the route answer', () => {
    expect(parsePreviewSettings({ engine: 'auto', source: 'settings', eager_proxies: true }))
      .toEqual({ engine: 'auto', source: 'settings', eagerProxies: true })
    expect(parsePreviewSettings({ engine: 'client', source: 'env' }))
      .toEqual({ engine: 'client', source: 'env', eagerProxies: false })
  })

  it('defaults to the server preview in this milestone', () => {
    expect(DEFAULT_PREVIEW_ENGINE).toBe('server')
    expect(parsePreviewSettings({ engine: 'server', source: 'default' }).engine).toBe('server')
  })

  it('falls back to server for anything it does not understand', () => {
    for (const raw of [null, undefined, 42, 'client', {}, { engine: 'warp' }, { engine: 1 }]) {
      expect(parsePreviewSettings(raw)).toEqual({ engine: 'server', source: 'default', eagerProxies: false })
    }
    expect(parsePreviewSettings({ engine: 'auto', source: 'elsewhere' }).source).toBe('default')
  })
})

describe('wantsClientEngine', () => {
  const s = (engine: 'auto' | 'client' | 'server') => ({ engine, source: 'default' as const, eagerProxies: false })
  it('server never, client always, auto by capability', () => {
    expect(wantsClientEngine(s('server'), true)).toBe(false)
    expect(wantsClientEngine(s('client'), false)).toBe(true)
    expect(wantsClientEngine(s('auto'), true)).toBe(true)
    expect(wantsClientEngine(s('auto'), false)).toBe(false)
  })
})

describe('capabilities and resolution (spec §7 engine-level fallback)', () => {
  const s = (engine: 'auto' | 'client' | 'server', source: 'env' | 'settings' | 'default' = 'settings') =>
    ({ engine, source, eagerProxies: false })
  const all = { mse: true, webgl2: true, audio: true }

  it('probes MediaSource (or ManagedMediaSource), WebGL2 and AudioContext', () => {
    const win = (o: Record<string, unknown>, gl: boolean) => ({
      ...o, document: { createElement: () => ({ getContext: (k: string) => (k === 'webgl2' && gl ? {} : null) }) },
    }) as unknown as Window & typeof globalThis
    expect(probePreviewCapabilities(win({ MediaSource: 1, AudioContext: 1 }, true))).toEqual(all)
    expect(probePreviewCapabilities(win({ ManagedMediaSource: 1, webkitAudioContext: 1 }, true))).toEqual(all)
    expect(probePreviewCapabilities(win({ AudioContext: 1 }, true)).mse).toBe(false)
    expect(probePreviewCapabilities(win({ MediaSource: 1, AudioContext: 1 }, false)).webgl2).toBe(false)
    expect(probePreviewCapabilities(undefined)).toEqual({ mse: false, webgl2: false, audio: false })
  })

  it('Off is the server; Auto and Always need every capability and a 240 kHz-exact rate', () => {
    expect(resolvePreviewMode(s('server'), all, true)).toEqual({ mode: 'server', reason: 'setting' })
    expect(resolvePreviewMode(s('auto'), all, true)).toEqual({ mode: 'client', reason: null })
    expect(resolvePreviewMode(s('client'), all, true)).toEqual({ mode: 'client', reason: null })
    expect(resolvePreviewMode(s('auto'), { ...all, mse: false }, true)).toEqual({ mode: 'server', reason: 'no-mse' })
    expect(resolvePreviewMode(s('client'), { ...all, webgl2: false }, true)).toEqual({ mode: 'server', reason: 'no-webgl2' })
    expect(resolvePreviewMode(s('auto'), { ...all, audio: false }, true)).toEqual({ mode: 'server', reason: 'no-audio' })
    expect(resolvePreviewMode(s('auto'), all, false)).toEqual({ mode: 'server', reason: 'rate' })
  })

  it('the dialog offers Auto / Always / Off, says one plain line, and explains a server fallback', () => {
    expect(PREVIEW_ENGINE_CHOICES.map((c) => [c.engine, c.label])).toEqual([['auto', 'Auto'], ['client', 'Always'], ['server', 'Off']])
    expect(INSTANT_PREVIEW_HELP.split(/[.!?](\s|$)/).filter((x) => x && x.trim()).length).toBe(1)
    expect(previewEngineNote(s('server'), { mode: 'server', reason: 'setting' })).toBeNull()
    expect(previewEngineNote(s('auto'), { mode: 'client', reason: null })).toMatch(/instantly/)
    expect(previewEngineNote(s('auto'), { mode: 'server', reason: 'rate' })).toMatch(/frame rate/)
    expect(previewEngineNote(s('client'), { mode: 'server', reason: 'no-mse' })).toMatch(/cannot run/)
    expect(previewEngineNote(s('client'), { mode: 'server', reason: 'webgl-lost' })).toMatch(/problem/)
    expect(previewEngineNote(s('auto', 'env'), null)).toMatch(/VAI_PREVIEW_ENGINE/)
  })
})
