import { describe, expect, it } from 'vitest'
import { PipBlendLayers } from './pipBlendLayers'

// A canvas stand-in with what the layer manager touches (vitest runs in node).
function fakeCanvas() {
  const attrs: Record<string, string> = {}
  return {
    width: 0, height: 0, style: {} as Record<string, string>, dataset: {} as Record<string, string>,
    removed: false,
    setAttribute(k: string, v: string) { attrs[k] = v },
    getAttribute(k: string) { return attrs[k] ?? null },
    getContext() { return { setTransform() {}, clearRect() {} } },
    remove() { this.removed = true },
  }
}

function host() {
  const kids: ReturnType<typeof fakeCanvas>[] = []
  return { kids, appendChild(n: never) { kids.push(n); return n } }
}

describe('PiP blend layers', () => {
  it('gives each PiP its own layer, in z order, composited with its CSS blend', () => {
    const h = host()
    const layers = new PipBlendLayers(h as never, () => fakeCanvas() as never)
    layers.begin(200, 100, 2)
    layers.next('normal')
    layers.next('screen')
    layers.next('plus-lighter')
    layers.end()
    expect(h.kids.map((c) => c.style.mixBlendMode)).toEqual(['normal', 'screen', 'plus-lighter'])
    expect(h.kids.map((c) => c.style.display)).toEqual(['block', 'block', 'block'])
    expect(h.kids[1].width).toBe(400)
    expect(h.kids[1].style.width).toBe('200px')
    expect(h.kids[0].getAttribute('aria-hidden')).toBe('true')
    expect(h.kids[0].style.cssText).toContain('pointer-events:none')
    expect(layers.active).toBe(3)
  })

  it('hides the layers a frame does not use, reuses them, and removes them on destroy', () => {
    const h = host()
    const layers = new PipBlendLayers(h as never, () => fakeCanvas() as never)
    layers.begin(10, 10, 1)
    layers.next('multiply')
    layers.next('multiply')
    layers.end()
    layers.begin(10, 10, 1)
    layers.end()
    expect(h.kids).toHaveLength(2)
    expect(h.kids.every((c) => c.style.display === 'none')).toBe(true)
    layers.begin(10, 10, 1)
    layers.next('difference')
    layers.end()
    expect(h.kids).toHaveLength(2)
    expect(h.kids[0].style.mixBlendMode).toBe('difference')
    expect(h.kids[1].style.display).toBe('none')
    layers.destroy()
    expect(h.kids.every((c) => c.removed)).toBe(true)
  })
})
