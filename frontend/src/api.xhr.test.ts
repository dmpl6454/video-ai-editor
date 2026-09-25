// QA-044: an import's BYTES must report progress and be cancellable. fetch()
// cannot report request-body progress, so api.upload sends through
// XMLHttpRequest when there is one. This drives the real api.upload against
// a scripted XHR and a stubbed fetch for the job poll.
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
const { api } = await import('./api')
const conn = await import('./lib/connection')

type Script = (x: FakeXHR) => void
let script: Script = () => {}
const sent: FakeXHR[] = []

class FakeXHR {
  status = 0
  statusText = ''
  responseText = ''
  url = ''
  upload: { onprogress: ((e: { lengthComputable: boolean; loaded: number; total: number }) => void) | null } = { onprogress: null }
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  onabort: (() => void) | null = null
  open(_m: string, url: string) { this.url = url }
  getResponseHeader() { return 'application/json' }
  send() { sent.push(this); script(this) }
  abort() { this.onabort?.() }
  respond(status: number, body: unknown) {
    this.status = status
    this.statusText = status === 202 ? 'Accepted' : 'OK'
    this.responseText = JSON.stringify(body)
    this.onload?.()
  }
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

afterEach(() => { vi.unstubAllGlobals(); vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} }); sent.length = 0; conn._resetConnectionForTests() })

describe('api.upload over XHR (QA-044)', () => {
  it('reports bytes sent, hands over the job id, then the job progress', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXHR)
    script = (x) => {
      x.upload.onprogress?.({ lengthComputable: true, loaded: 25, total: 100 })
      x.upload.onprogress?.({ lengthComputable: true, loaded: 100, total: 100 })
      setTimeout(() => x.respond(202, { job_id: 'j1', status: 'queued' }), 0)
    }
    const replies = [json({ id: 'j1', status: 'running', progress: 0.5 }),
                     json({ id: 'j1', status: 'completed', progress: 1, result: { src: '/u/a.mp4' } })]
    vi.stubGlobal('fetch', vi.fn(async () => replies.shift()!))
    const bytes: number[] = []
    const jobs: string[] = []
    const progress: number[] = []
    const out = await api.upload('s1', new File(['x'], 'a.mp4'), false, {
      onBytes: (p) => bytes.push(p.loaded / p.total), onJob: (j) => jobs.push(j), onProgress: (p) => progress.push(p),
    })
    expect(sent[0].url).toBe('/api/sessions/s1/upload?wait=0')
    expect(bytes).toEqual([0.25, 1])
    expect(jobs).toEqual(['j1'])
    expect(progress).toEqual([0.5, 1])
    expect(out).toEqual({ src: '/u/a.mp4' })
  })

  it('aborting the signal cancels the upload itself', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXHR)
    script = () => { /* never answers */ }
    const ac = new AbortController()
    const p = api.upload('s1', new File(['x'], 'a.mp4'), true, { signal: ac.signal })
    ac.abort()
    await expect(p).rejects.toMatchObject({ name: 'AbortError' })
  })

  it('a network failure is the engine being unreachable, not a raw error', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXHR)
    script = (x) => { x.onerror?.() }
    await expect(api.upload('s1', new File(['x'], 'a.mp4'))).rejects.toSatisfy((e: unknown) => conn.isEngineOffline(e))
    expect(conn.engineState()).toBe('offline')
  })

  it('audio imports report bytes too', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXHR)
    script = (x) => {
      x.upload.onprogress?.({ lengthComputable: true, loaded: 10, total: 40 })
      x.respond(200, { src: '/u/s.mp3', duration: 3, edl_hash: 'h' })
    }
    const bytes: number[] = []
    const out = await api.audioUpload('s1', new File(['x'], 's.mp3'), { addToMusic: false, onBytes: (p) => bytes.push(p.loaded) })
    expect(bytes).toEqual([10])
    expect(out.src).toBe('/u/s.mp3')
  })
})
