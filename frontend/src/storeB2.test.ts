// Lane B2 (session) — the REAL store against a mocked api:
//   QA-034 an export whose engine died must end (404 = interrupted) and
//          Cancel must always close the modal;
//   QA-047 a selection naming a clip that is gone (undo of a split) is pruned;
//   QA-056 copy holds clip CONTENT and paste lands at the playhead;
//   QA-105 edits carry the view's base hash, a stale view is refreshed, and
//          a window notices another window's edit;
//   QA-044/094/010 the import queue: placeholders in drop order, one at a
//          time, busy until the last, cancellable, import-only.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = {
  getSession: vi.fn(),
  getEDL: vi.fn(),
  sessionHead: vi.fn(),
  dispatch: vi.fn(),
  dispatchAsync: vi.fn(),
  getJob: vi.fn(),
  cancelJob: vi.fn(),
  exportAsync: vi.fn(),
  upload: vi.fn(),
  audioUpload: vi.fn(),
  renameSession: vi.fn(),
  preview: vi.fn(async () => ({ edl_hash: 'x' })),
}
vi.mock('./api', () => ({ api }))

const mem = new Map<string, string>()
vi.stubGlobal('localStorage', {
  getItem: (k: string) => mem.get(k) ?? null,
  setItem: (k: string, v: string) => { mem.set(k, v) },
  removeItem: (k: string) => { mem.delete(k) },
})

const { useStore, EXPORT_INTERRUPTED } = await import('./store')
const { useToasts } = await import('./toast')
const { importFiles } = await import('./lib/fileDrop')
const { STALE_VIEW_MESSAGE } = await import('./lib/staleView')

type Clip = { id: string; src?: string; start: number; in?: number; out?: number; end?: number; text?: string }
const edlWith = (v1: Clip[], text: Clip[] = []) => ({
  version: 2, duration: 10, canvas: { w: 1920, h: 1080, fps: 30 },
  tracks: [{ id: 'v1', type: 'video', clips: v1 }, { id: 'text', type: 'text', clips: text }],
})
const info = (hash: string) => ({ id: 's1', name: 'Reel', ops: [], redo_available: false, summary: { edl_hash: hash } })
const red: Clip = { id: 'red', src: '/u/red.mp4', start: 0, in: 0, out: 5 }
const blue: Clip = { id: 'blue', src: '/u/blue.mp4', start: 5, in: 0, out: 5 }
const flush = () => new Promise((r) => setTimeout(r, 0))
const toasts = () => useToasts.getState().toasts.map((t) => `${t.kind}:${t.message}`)

beforeEach(() => {
  for (const f of Object.values(api)) f.mockReset()
  api.preview.mockImplementation(async () => ({ edl_hash: 'x' }))
  useToasts.setState({ toasts: [] })
  useStore.setState({
    sessionId: 's1', edl: edlWith([red, blue]) as never, edlHash: 'h1', ops: [], pendingOps: 0,
    selection: null, multiSelection: [], framing: null, playhead: 0, clipboard: [],
    exporting: false, exportJobId: null, exportError: null, exportStatus: null,
    uploads: [], uploading: false, uploadBatch: { total: 0, done: 0 }, uploadError: null,
    importAddToTimeline: true,
  })
})
afterEach(() => { vi.useRealTimers() })

// --- QA-047 --------------------------------------------------------------------

describe('selection after the timeline changes underneath it (QA-047)', () => {
  it('drops ids that are no longer on the timeline when the EDL refreshes', async () => {
    // ⌘B selected the right half; ⌘Z removed it.
    useStore.setState({ selection: 'red_right', multiSelection: ['blue', 'gone'] })
    api.getSession.mockResolvedValue(info('h2'))
    api.getEDL.mockResolvedValue(edlWith([red, blue]))
    await useStore.getState().refresh()
    const s = useStore.getState()
    expect([s.selection, s.multiSelection]).toEqual(['blue', []])
  })

  it('so the next ⌘D / Backspace sends nothing stale after undoing a split', async () => {
    useStore.setState({ selection: 'red_right' })
    api.dispatch.mockResolvedValue({ result: { redo_available: true }, edl_hash: 'h0', op: null })
    api.getSession.mockResolvedValue(info('h0'))
    api.getEDL.mockResolvedValue(edlWith([red, blue]))
    await useStore.getState().dispatch('undo')
    api.dispatch.mockClear()
    await useStore.getState().duplicateSelection()
    await useStore.getState().rippleDeleteSelection()
    expect(api.dispatch).not.toHaveBeenCalled()
    expect(toasts().filter((t) => t.startsWith('error:'))).toEqual([])
  })

  it('keeps a selection an edit made while the fetch was in flight', async () => {
    let answer: (v: unknown) => void = () => {}
    api.getSession.mockResolvedValue(info('h1'))
    api.getEDL.mockImplementation(() => new Promise((r) => { answer = r }))
    const r = useStore.getState().refresh()           // an older refresh…
    api.dispatch.mockResolvedValue({ result: { halves: { red: 'red_r' } }, edl_hash: 'h2', op: null })
    await useStore.getState().splitTrackAt('v1', 2)     // …then a split selects the right half
    answer(edlWith([red, blue]))                        // …and the OLD EDL lands last
    await r
    expect(useStore.getState().selection).toBe('red_r')
  })
})

// --- QA-056 --------------------------------------------------------------------

describe('copy / paste (QA-056)', () => {
  it('copies the clip content and pastes it at the playhead in one call', async () => {
    useStore.setState({ selection: 'red', playhead: 7.5 })
    useStore.getState().copySelection()
    expect(useStore.getState().clipboard).toEqual([{ track: 'v1', clip: red }])
    api.dispatch.mockResolvedValue({ result: { clip_ids: ['c_new'] }, edl_hash: 'h2', op: null })
    await useStore.getState().pasteClipboard()
    expect(api.dispatch).toHaveBeenCalledTimes(1)
    const [, tool, args] = api.dispatch.mock.calls[0]
    expect(tool).toBe('paste_clips')
    expect(args).toEqual({ clips: [{ track: 'v1', clip: red }], at: 7.5 })
    expect(useStore.getState().selection).toBe('c_new')
  })

  it('still pastes after the source clip was deleted', async () => {
    useStore.setState({ selection: 'red', playhead: 1 })
    useStore.getState().copySelection()
    useStore.setState({ edl: edlWith([blue]) as never, selection: null })
    api.dispatch.mockResolvedValue({ result: { clip_ids: ['c_new'] }, edl_hash: 'h2', op: null })
    await useStore.getState().pasteClipboard()
    expect(api.dispatch.mock.calls[0][1]).toBe('paste_clips')
    expect((api.dispatch.mock.calls[0][2] as { clips: unknown[] }).clips).toEqual([{ track: 'v1', clip: red }])
  })

  it('copies a title from its own lane', () => {
    const t1: Clip = { id: 't1', text: 'HI', start: 1, end: 3 }
    useStore.setState({ edl: edlWith([red], [t1]) as never, selection: 't1' })
    useStore.getState().copySelection()
    expect(useStore.getState().clipboard).toEqual([{ track: 'text', clip: t1 }])
  })
})

// --- QA-105 --------------------------------------------------------------------

describe('two windows on one project (QA-105)', () => {
  it('sends the view\'s hash with an edit and adopts the answer\'s hash at once', async () => {
    api.dispatch.mockResolvedValue({ result: {}, edl_hash: 'h2', op: null })
    await useStore.getState().dispatch('ripple_delete', { clip_id: 'red' })
    expect(api.dispatch.mock.calls[0][3]).toBe('h1')
    expect(useStore.getState().edlHash).toBe('h2')
    await useStore.getState().dispatch('move_clip', { clip_id: 'blue', new_start: 0 })
    expect(api.dispatch.mock.calls[1][3]).toBe('h2')
  })

  it('a stale view\'s edit is refused, the timeline refreshed and the reason shown', async () => {
    api.dispatch.mockRejectedValue(new Error('409 Conflict: {"error":{"code":"CONFLICT","message":"request failed","details":{"code":"stale_edl","message":"x","edl_hash":"h9"}}}'))
    api.getSession.mockResolvedValue(info('h9'))
    api.getEDL.mockResolvedValue(edlWith([blue]))
    const res = await useStore.getState().dispatch('undo')
    expect(res).toBeNull()
    expect(useStore.getState().edlHash).toBe('h9')
    expect(toasts()).toEqual([`info:${STALE_VIEW_MESSAGE}`])
  })

  it('notices another window\'s edit and refreshes', async () => {
    api.sessionHead.mockResolvedValue({ edl_hash: 'h1' })
    await useStore.getState().syncWithServer()
    expect(api.getEDL).not.toHaveBeenCalled()
    api.sessionHead.mockResolvedValue({ edl_hash: 'h7' })
    api.getSession.mockResolvedValue(info('h7'))
    api.getEDL.mockResolvedValue(edlWith([blue]))
    await useStore.getState().syncWithServer()
    expect(useStore.getState().edlHash).toBe('h7')
    expect((useStore.getState().edl as unknown as ReturnType<typeof edlWith>).tracks[0].clips).toEqual([blue])
  })

  it('does not second-guess its own edit in flight', async () => {
    useStore.setState({ pendingOps: 1 })
    api.sessionHead.mockResolvedValue({ edl_hash: 'h7' })
    await useStore.getState().syncWithServer()
    expect(api.sessionHead).not.toHaveBeenCalled()
  })
})

// --- QA-034 --------------------------------------------------------------------

describe('export when the engine dies (QA-034)', () => {
  it('a 404 on the job ends the export with "interrupted" instead of polling for 30 minutes', async () => {
    vi.useFakeTimers()
    api.exportAsync.mockResolvedValue({ job_id: 'j1' })
    api.getJob.mockRejectedValue(new Error('404 Not Found: {"error":{"code":"NOT_FOUND","message":"job not found"}}'))
    const p = useStore.getState().doExport()
    await vi.advanceTimersByTimeAsync(600)
    await p
    const s = useStore.getState()
    expect([s.exporting, s.exportError]).toEqual([false, EXPORT_INTERRUPTED])
    expect(api.getJob).toHaveBeenCalledTimes(1)
  })

  it('Cancel closes the modal at once even when the cancel request fails, and the loop stops', async () => {
    vi.useFakeTimers()
    api.exportAsync.mockResolvedValue({ job_id: 'j1' })
    api.getJob.mockResolvedValue({ id: 'j1', status: 'running', progress: 0.3 })
    api.cancelJob.mockRejectedValue(new Error('404 Not Found'))
    const p = useStore.getState().doExport()
    await vi.advanceTimersByTimeAsync(1100)
    expect(useStore.getState().exporting).toBe(true)
    await useStore.getState().cancelExport()
    expect(useStore.getState().exporting).toBe(false)
    expect(api.cancelJob).toHaveBeenCalledWith('j1')
    const polls = api.getJob.mock.calls.length
    await vi.advanceTimersByTimeAsync(3000)
    await p
    expect(api.getJob.mock.calls.length).toBe(polls)
    expect(useStore.getState().exporting).toBe(false)
    expect(useStore.getState().exportError).toBeNull()
  })

  it('a new export after a cancelled one is not closed by the old loop', async () => {
    vi.useFakeTimers()
    api.exportAsync.mockResolvedValueOnce({ job_id: 'j1' }).mockResolvedValueOnce({ job_id: 'j2' })
    api.getJob.mockResolvedValue({ id: 'x', status: 'running', progress: 0.1 })
    api.cancelJob.mockResolvedValue({})
    const first = useStore.getState().doExport()
    await vi.advanceTimersByTimeAsync(600)
    await useStore.getState().cancelExport()
    const second = useStore.getState().doExport()
    await vi.advanceTimersByTimeAsync(600)
    await first
    expect(useStore.getState().exporting).toBe(true)
    expect(useStore.getState().exportJobId).toBe('j2')
    await useStore.getState().cancelExport()
    await vi.advanceTimersByTimeAsync(600)
    await second
  })
})

// --- QA-044 / QA-094 / QA-010 ----------------------------------------------------

describe('the import queue (QA-044/094, QA-010 import-only)', () => {
  const file = (name: string, type = 'video/mp4') => new File(['x'], name, { type })

  it('shows every dropped file at once, imports them one at a time in drop order, busy until the last', async () => {
    const gates: Array<() => void> = []
    const order: string[] = []
    api.upload.mockImplementation((_sid: string, f: File) => {
      order.push(f.name)
      return new Promise<void>((r) => gates.push(r))
    })
    api.getSession.mockResolvedValue(info('h2'))
    api.getEDL.mockResolvedValue(edlWith([red]))
    const s = useStore.getState()
    const all = importFiles([file('b.mp4'), file('a.mp4'), file('c.mp4')], { upload: s.upload, uploadAudio: s.uploadAudio })
    await flush()
    expect(useStore.getState().uploads.map((u) => [u.name, u.stage])).toEqual([
      ['b.mp4', 'uploading'], ['a.mp4', 'queued'], ['c.mp4', 'queued']])
    expect(order).toEqual(['b.mp4'])            // one at a time
    gates[0](); await flush(); await flush()
    expect(order).toEqual(['b.mp4', 'a.mp4'])
    expect(useStore.getState().uploading).toBe(true)          // 1 of 3 in: still busy
    expect(useStore.getState().uploadBatch).toEqual({ total: 3, done: 1 })
    gates[1](); await flush(); await flush()
    gates[2](); await all
    expect(order).toEqual(['b.mp4', 'a.mp4', 'c.mp4'])
    expect([useStore.getState().uploading, useStore.getState().uploads]).toEqual([false, []])
  })

  it('reports stage and progress, and Cancel during processing cancels the server job', async () => {
    let fail: (e: Error) => void = () => {}
    api.upload.mockImplementation((_sid: string, _f: File, _add: boolean, o: {
      onBytes: (p: { loaded: number; total: number }) => void; onJob: (id: string) => void; onProgress: (p: number) => void
    }) => {
      o.onBytes({ loaded: 50, total: 100 })
      return new Promise((_r, rej) => { fail = rej; setTimeout(() => { o.onBytes({ loaded: 100, total: 100 }); o.onJob('job9'); o.onProgress(0.25) }, 0) })
    })
    api.cancelJob.mockResolvedValue({})
    const p = useStore.getState().upload(file('long.mp4'))
    await flush()
    await flush()
    const item = useStore.getState().uploads[0]
    expect([item.stage, item.progress]).toEqual(['processing', 0.25])
    useStore.getState().cancelUpload(item.id)
    expect(api.cancelJob).toHaveBeenCalledWith('job9')
    expect(useStore.getState().uploads).toEqual([])
    expect(useStore.getState().uploading).toBe(false)       // idle at once, not on the next poll
    fail(new Error('long.mp4: import cancelled'))
    await p
    expect(useStore.getState().uploadError).toBeNull()
    expect(useStore.getState().uploading).toBe(false)
  })

  it('import-only sends add_to_timeline=false (video) and add_to_music=false (audio)', async () => {
    api.upload.mockResolvedValue({})
    api.audioUpload.mockResolvedValue({})
    api.getSession.mockResolvedValue(info('h1'))
    api.getEDL.mockResolvedValue(edlWith([red]))
    useStore.getState().setImportAddToTimeline(false)
    await useStore.getState().upload(file('b.mp4'))
    await useStore.getState().uploadAudio(file('s.mp3', 'audio/mpeg'))
    expect(api.upload.mock.calls[0][2]).toBe(false)
    expect(api.audioUpload.mock.calls[0][2]).toMatchObject({ addToMusic: false })
    expect(mem.get('vai.importAddToTimeline')).toBe('false')
    useStore.getState().setImportAddToTimeline(true)
    await useStore.getState().upload(file('c.mp4'))
    expect(api.upload.mock.calls[1][2]).toBe(true)
  })
})

// --- REV-B1B8-LANEDROP -----------------------------------------------------------

describe('a timeline lane drop goes through the import queue (REV-B1B8-LANEDROP)', () => {
  it('a photo dropped on a lane is a queued import placed as a 5 s clip; `uploading` stays true until it is placed', async () => {
    let finishUpload!: (v: unknown) => void
    api.upload.mockImplementation(() => new Promise((r) => { finishUpload = r }))
    api.dispatch.mockResolvedValue({ result: {}, edl_hash: 'h2', op: null })
    api.getSession.mockResolvedValue(info('h2'))
    api.getEDL.mockResolvedValue(edlWith([red, blue]))
    const cursor = { next: 3 }
    const done = useStore.getState().upload(new File(['x'], 'p.png'), {
      place: { track: 'v2', trackType: 'video', cursor, audioLane: 'music', fps: 30 },
    })
    await flush()
    const s = useStore.getState()
    expect(s.uploads.map((u) => u.name)).toEqual(['p.png'])       // a placeholder row
    expect(api.upload.mock.calls[0][2]).toBe(false)                // not the default placement
    finishUpload({ kind: 'image', normalized: '/u/p.still.mp4', duration: 300, edl_hash: 'h1' })
    await done
    const add = api.dispatch.mock.calls.find((c) => c[1] === 'add_clip')!
    expect(add[2]).toEqual({ track: 'v2', src: '/u/p.still.mp4', in: 0, out: 5, start: 3 })
    // base_hash was skipped: the import was still in the queue when it dispatched
    expect(add[3]).toBeFalsy()
    expect(useStore.getState().uploading).toBe(false)
    expect(cursor.next).toBe(8)
  })
})
