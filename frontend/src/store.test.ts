// downloadExport must keep going through the SAME bridge detection the .vae
// link uses (lib/nativeSave) — with the export's own filename, and without
// ever reaching the anchor path when the bridge answered. Node has no
// `document`, so a stray anchor fallback would throw here rather than pass.
import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'

vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
const { useStore } = await import('./store')
const { useToasts } = await import('./toast')

const arm = (save_export: (sid: string, filename: string) => Promise<string | null>) =>
  vi.stubGlobal('pywebview', { api: { save_export } })

beforeEach(() => {
  useStore.setState({ sessionId: 's1', exportUrl: '/api/sessions/s1/files/exports/s1.mp4', exportFilename: 's1.mp4' })
  useToasts.setState({ toasts: [] })
})
afterEach(() => vi.unstubAllGlobals())

describe('downloadExport in the packaged app', () => {
  it('hands the export filename to the bridge and toasts the chosen path', async () => {
    const save = vi.fn(async () => '/Users/me/Movies/s1.mp4')
    arm(save)
    await useStore.getState().downloadExport()
    expect(save).toHaveBeenCalledWith('s1', 's1.mp4')
    expect(useToasts.getState().toasts.map((t) => t.message)).toEqual(['Saved to /Users/me/Movies/s1.mp4'])
  })

  it('stays silent when the user cancels the native dialog', async () => {
    arm(async () => null)
    await useStore.getState().downloadExport()
    expect(useToasts.getState().toasts).toEqual([])
  })

  it('does nothing without an export', async () => {
    const save = vi.fn(async () => '/p')
    arm(save)
    useStore.setState({ exportUrl: null })
    await useStore.getState().downloadExport()
    expect(save).not.toHaveBeenCalled()
  })
})

// The string the user actually reads. api.upload / api.audioUpload throw the
// api.ts contract shape ("<status> <statusText>: <raw envelope>"), and
// MediaBin renders `uploadError` verbatim — so reading `e.message` here pasted
// the whole {"error":{…,"request_id":…}} JSON into the panel. errorMessage()
// is the only thing that knows the envelope; this pins the DISPLAYED string,
// which a test of errorMessage() alone cannot see.
describe('what an upload failure puts on screen', () => {
  const REASON = 'that file is 1.2 GB, over the 512 MB import limit'
  const envelope = (status: number, statusText: string) => new Response(
    JSON.stringify({ error: { code: 'HTTP_' + status, message: REASON, request_id: 'r-9' } }),
    { status, statusText, headers: { 'content-type': 'application/json' } },
  )

  it('shows the backend sentence, not the JSON envelope', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => envelope(413, 'Request Entity Too Large')))
    await useStore.getState().upload(new File(['x'], 'clip.mp4'))
    const shown = useStore.getState().uploadError
    expect(shown).toBe('clip.mp4: ' + REASON)
    expect(shown).not.toContain('{')
    expect(shown).not.toContain('request_id')
  })

  it('does the same for an audio import', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => envelope(415, 'Unsupported Media Type')))
    await useStore.getState().uploadAudio(new File(['x'], 'track.wav'))
    expect(useStore.getState().uploadError).toBe('track.wav: ' + REASON)
  })

  it('falls back to the raw text when the body is not an envelope', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 500, statusText: 'Internal Server Error' })))
    await useStore.getState().upload(new File(['x'], 'clip.mp4'))
    expect(useStore.getState().uploadError).toBe('clip.mp4: 500 Internal Server Error')
  })
})
