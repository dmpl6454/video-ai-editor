// QA-059: the filmstrip waits for the preview to load (or a timeout).
import { describe, expect, it, vi } from 'vitest'
import { createPreviewGate, type GateDoc } from './previewGate'

function fakeDoc(videos: { readyState: number }[] = []) {
  const handlers = new Map<string, ((e: Event) => void)[]>()
  const doc: GateDoc & { fire(type: string, tag: string): void; count(): number } = {
    addEventListener: (t, fn) => { handlers.set(t, [...(handlers.get(t) ?? []), fn]) },
    removeEventListener: (t, fn) => { handlers.set(t, (handlers.get(t) ?? []).filter((f) => f !== fn)) },
    querySelectorAll: () => videos,
    fire: (type, tag) => { for (const f of handlers.get(type) ?? []) f({ target: { tagName: tag } } as unknown as Event) },
    count: () => [...handlers.values()].reduce((n, l) => n + l.length, 0),
  }
  return doc
}

describe('previewGate', () => {
  it('opens on the preview video loading data, not on an image', () => {
    const doc = fakeDoc()
    const onOpen = vi.fn()
    const g = createPreviewGate({ doc, onOpen, setTimer: () => 1, clearTimer: () => {} })
    doc.fire('loadeddata', 'IMG')
    expect(g.isOpen()).toBe(false)
    doc.fire('loadeddata', 'VIDEO')
    expect(g.isOpen()).toBe(true)
    expect(onOpen).toHaveBeenCalledTimes(1)
    expect(doc.count()).toBe(0)            // listeners removed
  })
  it('opens on a video error and on the timeout', () => {
    const d1 = fakeDoc()
    const g1 = createPreviewGate({ doc: d1, onOpen: () => {}, setTimer: () => 1, clearTimer: () => {} })
    d1.fire('error', 'VIDEO')
    expect(g1.isOpen()).toBe(true)
    let fire: () => void = () => {}
    const g2 = createPreviewGate({ doc: fakeDoc(), onOpen: () => {}, setTimer: (fn) => { fire = fn; return 1 }, clearTimer: () => {} })
    expect(g2.isOpen()).toBe(false)
    fire()
    expect(g2.isOpen()).toBe(true)
  })
  it('is open at once when a video already has data (a remount)', () => {
    const onOpen = vi.fn()
    const g = createPreviewGate({ doc: fakeDoc([{ readyState: 4 }]), onOpen })
    expect(g.isOpen()).toBe(true)
  })
})
