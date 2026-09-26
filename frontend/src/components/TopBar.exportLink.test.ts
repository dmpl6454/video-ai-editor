// QA-026: the "↓ MP4" link leaked across projects and did not go stale on Undo.
//
// This drives the REAL store (doExport's poll loop, refresh(), resetTransient,
// downloadExport) against a stubbed fetch that answers like the backend, and
// reads the link through `exportLinkView` — the exact function TopBar renders.
// (A static render of TopBar itself cannot show it: zustand's server snapshot
// is the store's INITIAL state, so SSR never sees a finished export.)
// Before the fix: after switching to project B the markup still showed A's
// "↓ MP4" and downloadExport handed the bridge A's file; after "export → Undo"
// the link read "↓ MP4" with no "(outdated)", because the check was
// `ops.length > exportGen` and Undo lowers ops.length.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
const { useStore } = await import('../store')
const { exportLinkView } = await import('../lib/exportLink')

// What GET /sessions/:id reports as the current timeline, per session.
const current: Record<string, string> = {}
const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })

function backend(url: string, init?: RequestInit): Response {
  const u = new URL(url, 'http://x')
  let m = /^\/api\/sessions\/([^/]+)\/export$/.exec(u.pathname)
  if (m && init?.method === 'POST') return json({ job_id: `job-${m[1]}`, status: 'queued' })
  m = /^\/api\/jobs\/job-(.+)$/.exec(u.pathname)
  if (m) {
    const sid = m[1]
    return json({ id: `job-${sid}`, status: 'completed', progress: 1, error: null, result: {
      url: `/api/sessions/${sid}/files/exports/export_${current[sid]}.mp4`,
      filename: `export_${current[sid]}.mp4`, edl_hash: current[sid] } })
  }
  m = /^\/api\/sessions\/([^/]+)$/.exec(u.pathname)
  if (m) return json({ id: m[1], name: m[1], ops: [], redo_available: false,
                       summary: { duration: 4, canvas: { w: 1080, h: 1350, fps: 30 }, tracks: [], edl_hash: current[m[1]], ops: 0 } })
  m = /^\/api\/sessions\/([^/]+)\/edl$/.exec(u.pathname)
  if (m) return json({ canvas: { w: 1080, h: 1350, fps: 30 }, duration: 4, tracks: [] })
  if (u.pathname === '/api/version') return json({ version: '0.7.2', build: 'test' })
  throw new Error(`unexpected ${init?.method ?? 'GET'} ${u.pathname}`)
}

const saved: [string, string][] = []
beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => backend(url, init)))
  // The packaged app's native bridge: records which session/file it was asked for.
  vi.stubGlobal('pywebview', { api: { save_export: async (sid: string, filename: string) => { saved.push([sid, filename]); return `/Movies/${filename}` } } })
  saved.length = 0
  current.A = 'aaaaaaaaaaaaaaa1'
  current.B = 'bbbbbbbbbbbbbbb1'
  useStore.setState({ sessionId: 'A', exportLinks: {}, edlHash: null, exporting: false })
})
afterEach(() => vi.unstubAllGlobals())

/** The toolbar's link text for the project on screen, or null when none is shown. */
const shown = () => {
  const s = useStore.getState()
  return exportLinkView(s.exportLinks, s.sessionId, s.edlHash)?.label ?? null
}

async function switchTo(sid: string) {
  // TopBar.switchSession, verbatim in effect.
  useStore.getState().resetTransient()
  useStore.setState({ sessionId: sid, sessionName: sid })
  await useStore.getState().refresh()
}

describe('the export link belongs to its project', () => {
  it('is not shown, and cannot be downloaded, in another project', async () => {
    await useStore.getState().refresh()
    await useStore.getState().doExport()
    saved.length = 0                       // doExport's own auto-save
    expect(shown()).toBe('MP4')

    await switchTo('B')
    expect(shown()).toBeNull()
    await useStore.getState().downloadExport()
    expect(saved).toEqual([])              // A's file is never handed out in B

    await switchTo('A')                    // …and it comes back with its project
    expect(shown()).toBe('MP4')
    await useStore.getState().downloadExport()
    expect(saved).toEqual([['A', 'export_aaaaaaaaaaaaaaa1.mp4']])
  })

  it('each project keeps its own link: exporting B does not take A\'s away', async () => {
    await useStore.getState().refresh()
    await useStore.getState().doExport()          // A
    await switchTo('B')
    await useStore.getState().doExport()          // B
    expect(shown()).toBe('MP4')
    await switchTo('A')
    expect(shown()).toBe('MP4')
    saved.length = 0
    await useStore.getState().downloadExport()
    expect(saved).toEqual([['A', 'export_aaaaaaaaaaaaaaa1.mp4']])
  })

  it('a brand-new project shows no export link', async () => {
    await useStore.getState().refresh()
    await useStore.getState().doExport()
    current.N = 'nnnnnnnnnnnnnnn1'
    await switchTo('N')
    expect(shown()).toBeNull()
  })

  it('an export failure stays with its project: switching away clears the error chip', async () => {
    useStore.setState({ exportError: 'RuntimeError: ffmpeg failed in A' })
    await switchTo('B')
    expect(useStore.getState().exportError).toBeNull()
  })

  it('switching projects forgets the previous preview hash', async () => {
    useStore.setState({ previewHash: current.A })
    await switchTo('B')
    expect(useStore.getState().previewHash).toBeNull()
  })
})

describe('the export link knows which timeline it rendered', () => {
  it('goes outdated on Undo past the export and current again on Redo', async () => {
    current.A = 'aaaaaaaaaaaaaaa2'           // edited (e.g. set 1:1) — then exported
    await useStore.getState().refresh()
    await useStore.getState().doExport()
    expect(shown()).toBe('MP4')

    current.A = 'aaaaaaaaaaaaaaa1'           // Undo: history got SHORTER
    await useStore.getState().refresh()
    expect(shown()).toBe('MP4 (outdated)')

    current.A = 'aaaaaaaaaaaaaaa2'           // Redo: back to exactly what was rendered
    await useStore.getState().refresh()
    expect(shown()).toBe('MP4')
  })

  it('goes outdated after an ordinary edit', async () => {
    await useStore.getState().refresh()
    await useStore.getState().doExport()
    current.A = 'aaaaaaaaaaaaaaa3'
    await useStore.getState().refresh()
    expect(shown()).toBe('MP4 (outdated)')
  })
})
