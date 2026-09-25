// QA-007: a video import is a background job on the server; api.upload must
// submit it with wait=0, poll the job, report its progress, and resolve with
// the same body the synchronous route returns.
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
const { api } = await import('./api')

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

afterEach(() => vi.unstubAllGlobals())

describe('api.upload runs the import as a polled job', () => {
  it('posts wait=0, reports progress and resolves with the job result', async () => {
    const result = { src: '/u/clip.mp4', normalized: '/u/clip_ab/clip.normalized.mp4',
                     duration: 4, probe: { duration: 4 }, edl_hash: 'h1' }
    const calls: string[] = []
    const replies = [
      json({ job_id: 'j1', status: 'queued' }, 202),
      json({ id: 'j1', status: 'running', progress: 0.5 }),
      json({ id: 'j1', status: 'completed', progress: 1, result }),
    ]
    vi.stubGlobal('fetch', vi.fn(async (url: string) => { calls.push(String(url)); return replies.shift()! }))
    const seen: number[] = []
    const out = await api.upload('s1', new File(['x'], 'clip.mp4'), true, { onProgress: (p) => seen.push(p) })
    expect(calls[0]).toContain('/sessions/s1/upload?wait=0')
    expect(calls.slice(1).every((u) => u.includes('/jobs/j1'))).toBe(true)
    expect(seen).toEqual([0.5, 1])
    expect(out).toEqual(result)
  })

  it('throws the job error when the import fails', async () => {
    const replies = [
      json({ job_id: 'j2', status: 'queued' }, 202),
      json({ id: 'j2', status: 'failed', progress: 0, error: "Couldn't import this file" }),
    ]
    vi.stubGlobal('fetch', vi.fn(async () => replies.shift()!))
    await expect(api.upload('s1', new File(['x'], 'bad.mp4'))).rejects.toThrow("Couldn't import this file")
  })
})
