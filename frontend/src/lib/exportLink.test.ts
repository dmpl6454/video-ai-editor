import { describe, expect, it } from 'vitest'
import { exportFor, exportKind, exportLink, isExportStale, visibleExport, withExport, withoutExport } from './exportLink'

describe('exportLink', () => {
  it('takes the hash from the payload', () => {
    const l = exportLink('s1', { url: '/api/sessions/s1/files/exports/export_0123456789abcdef.mp4',
                                 filename: 'export_0123456789abcdef.mp4', edl_hash: 'fedcba9876543210' })
    expect(l).toEqual({ sid: 's1', url: '/api/sessions/s1/files/exports/export_0123456789abcdef.mp4',
                        filename: 'export_0123456789abcdef.mp4', edlHash: 'fedcba9876543210' })
  })

  it('falls back to the hash render_export put in the filename', () => {
    const l = exportLink('s1', { url: '/x/export_0123456789abcdef.mov', filename: 'export_0123456789abcdef.mov' })
    expect(l.edlHash).toBe('0123456789abcdef')
    expect(exportKind(l)).toBe('MOV')
  })

  it('is visible only in its own session', () => {
    const l = exportLink('s1', { url: '/u', filename: 'export_0123456789abcdef.mp4' })
    expect(visibleExport(l, 's1')).toBe(l)
    expect(visibleExport(l, 's2')).toBeNull()
    expect(visibleExport(l, null)).toBeNull()
    expect(visibleExport(null, 's1')).toBeNull()
  })

  it('is stale exactly when the current hash differs', () => {
    const l = exportLink('s1', { url: '/u', filename: 'f.mp4', edl_hash: 'h1' })
    expect(isExportStale(l, 'h1')).toBe(false)
    expect(isExportStale(l, 'h0')).toBe(true)
    expect(isExportStale(l, null)).toBe(false)
  })
})

describe('per-session links', () => {
  it('adds, replaces and removes one session without touching another', () => {
    const a = exportLink('A', { url: '/a', filename: 'a.mp4', edl_hash: 'ha' })
    const b = exportLink('B', { url: '/b', filename: 'b.mp4', edl_hash: 'hb' })
    const both = withExport(withExport({}, a), b)
    expect(exportFor(both, 'A')).toBe(a)
    expect(exportFor(both, 'B')).toBe(b)
    expect(exportFor(both, 'C')).toBeNull()
    expect(exportFor(both, null)).toBeNull()
    const noB = withoutExport(both, 'B')
    expect(exportFor(noB, 'B')).toBeNull()
    expect(exportFor(both, 'B')).toBe(b)          // the original map is untouched
  })
})
