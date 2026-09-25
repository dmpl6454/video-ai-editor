import { afterEach, describe, expect, it, vi } from 'vitest'

afterEach(() => { vi.unstubAllGlobals(); vi.resetModules() })

describe('canvas tokens (wave-B review)', () => {
  it('resolves getComputedStyle once per frame, not once per call', async () => {
    let calls = 0
    const rafs: (() => void)[] = []
    vi.stubGlobal('document', { documentElement: {} })
    vi.stubGlobal('requestAnimationFrame', (cb: () => void) => { rafs.push(cb); return rafs.length })
    vi.stubGlobal('getComputedStyle', () => {
      calls++
      return { getPropertyValue: (n: string) => ({ '--bg-2': '#1d1d22', '--font-ui': 'Inter, sans-serif' } as Record<string, string>)[n] ?? '' }
    })
    const { cssToken, uiFont } = await import('./themeTokens')
    for (let i = 0; i < 50; i++) cssToken('--bg-2', '#000')
    expect(cssToken('--bg-2', '#000')).toBe('#1d1d22')
    expect(calls).toBe(1)
    rafs.splice(0).forEach((cb) => cb())      // next frame
    cssToken('--bg-2', '#000')
    expect(calls).toBe(2)
    // a canvas font never carries var(): ctx.font would reject it
    expect(uiFont(10)).toBe('10px Inter, sans-serif')
    expect(uiFont(9, 'bold')).toBe('bold 9px Inter, sans-serif')
    expect(uiFont(9)).not.toContain('var(')
  })
})
