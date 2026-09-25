import { describe, expect, it } from 'vitest'
import { readRightTab, writeRightTab, RIGHT_TAB_KEY } from './rightTab'

const mem = () => {
  const m = new Map<string, string>()
  return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => { m.set(k, v) }, m }
}

describe('docked chat tab (QA-061)', () => {
  it('is closed (Inspector) on a first load', () => {
    expect(readRightTab(mem())).toBe('inspect')
    expect(readRightTab(null)).toBe('inspect')
  })
  it('remembers opening and closing across loads', () => {
    const s = mem()
    writeRightTab(s, 'chat')
    expect(readRightTab(s)).toBe('chat')
    writeRightTab(s, 'inspect')
    expect(readRightTab(s)).toBe('inspect')
    expect(s.m.get(RIGHT_TAB_KEY)).toBe('inspect')
  })
  it('survives a storage that throws or holds garbage', () => {
    const bad = { getItem: () => { throw new Error('blocked') }, setItem: () => { throw new Error('blocked') } }
    expect(readRightTab(bad)).toBe('inspect')
    expect(() => writeRightTab(bad, 'chat')).not.toThrow()
    const s = mem(); s.setItem(RIGHT_TAB_KEY, 'true')
    expect(readRightTab(s)).toBe('inspect')
  })
})
