// The failure contract of api.ts's fetch-based helpers, asserted end to end
// through the production shapes: a stubbed `fetch` answers with the body the
// backend really sends, the helper throws, and store.errorMessage() — the
// function every toast in the app calls — must yield the backend's sentence.
//
// The defect this guards: POST /api/load_project answers a filename it does
// not like with 415 and the reason under `{error:{message}}` (api/hardening.py
// wraps every HTTPException that way). loadProject and its multipart siblings
// each carried a private parse that read `body.detail` — a key that is never
// on the wire — so the toast showed "415 Unsupported Media Type" and nothing
// else. Every helper now throws through one apiError() with the raw body
// appended, which is the shape errorMessage() already understood.
import { describe, expect, it, vi, afterEach } from 'vitest'

// store.ts reads persisted panel sizes at module scope; node's `localStorage`
// stub has no getItem, so the import needs a minimal one (see TopBar.test.ts).
vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
const { api } = await import('./api')
const { errorMessage } = await import('./store')

const REASON = 'That file is not a Video AI Editor project — expected a .vae saved by this app.'

const envelope415 = () => new Response(
  JSON.stringify({ error: { code: 'HTTP_415', message: REASON, request_id: 'r-1' } }),
  { status: 415, statusText: 'Unsupported Media Type', headers: { 'content-type': 'application/json' } },
)

const answer = (res: Response) => vi.stubGlobal('fetch', vi.fn(async () => res))
const projectFile = () => new File(['not really a zip'], 'p.vae')

const thrown = async (run: () => Promise<unknown>): Promise<unknown> => {
  try { await run() } catch (e) { return e }
  throw new Error('expected the call to throw')
}

afterEach(() => vi.unstubAllGlobals())

describe('loadProject on a 415 error envelope', () => {
  it('throws an Error whose message errorMessage() turns into the backend sentence', async () => {
    answer(envelope415())
    const e = await thrown(() => api.loadProject(projectFile()))
    expect(e).toBeInstanceOf(Error)
    expect(errorMessage(e)).toBe(REASON)
  })

  it('so the Open toast reads the reason, not the bare status line', async () => {
    answer(envelope415())
    const e = await thrown(() => api.loadProject(projectFile()))
    // Exactly the template TopBar.onLoadProject uses.
    expect(`Couldn't open that .vae project: ${errorMessage(e)}`)
      .toBe(`Couldn't open that .vae project: ${REASON}`)
    expect(errorMessage(e)).not.toContain('415')
  })

  it('keeps the status line in the raw message for callers that inspect it', async () => {
    answer(envelope415())
    const e = await thrown(() => api.loadProject(projectFile())) as Error
    expect(e.message.startsWith('415 Unsupported Media Type: ')).toBe(true)
  })
})

describe('the shared failure contract', () => {
  it('still understands a legacy FastAPI {detail} body', async () => {
    answer(new Response(JSON.stringify({ detail: 'expected a .vae project file' }),
      { status: 415, statusText: 'Unsupported Media Type' }))
    const e = await thrown(() => api.loadProject(projectFile()))
    expect(errorMessage(e)).toBe('expected a .vae project file')
  })

  it('falls back to the bare status line — no dangling colon — when the body is empty', async () => {
    answer(new Response(null, { status: 415, statusText: 'Unsupported Media Type' }))
    const e = await thrown(() => api.loadProject(projectFile()))
    expect(errorMessage(e)).toBe('415 Unsupported Media Type')
  })

  it('is the same one the multipart siblings throw through', async () => {
    answer(new Response(JSON.stringify({ error: { code: 'HTTP_422', message: 'expected a video file' } }),
      { status: 422, statusText: 'Unprocessable Entity' }))
    const e = await thrown(() => api.upload('s1', new File(['x'], 'x.txt')))
    expect(errorMessage(e)).toBe('expected a video file')
  })

  it('and the one http() throws through', async () => {
    answer(new Response(JSON.stringify({ error: { code: 'HTTP_404', message: 'no such session' } }),
      { status: 404, statusText: 'Not Found' }))
    const e = await thrown(() => api.getSession('nope'))
    expect(errorMessage(e)).toBe('no such session')
  })
})
