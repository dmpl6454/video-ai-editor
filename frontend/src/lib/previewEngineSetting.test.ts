import { describe, expect, it } from 'vitest'
import { DEFAULT_PREVIEW_ENGINE, parsePreviewSettings, wantsClientEngine } from './previewEngineSetting'

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
