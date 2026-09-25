// Project switch must never show one project's EDL under another project's
// session id. Runs the REAL store against a mocked api whose getSession/getEDL
// answers are released by hand, so the arrival order is controlled exactly.
import { beforeEach, describe, expect, it, vi } from 'vitest'

interface Pending { sid: string; kind: 'info' | 'edl'; resolve: (v: unknown) => void }
const pending: Pending[] = []

const edlOf = (sid: string) => ({
  version: 2, duration: 5, canvas: { w: 1920, h: 1080, fps: 30 },
  tracks: [{ id: 'v1', type: 'video', clips: [{ id: `c_${sid}`, src: `/wd/${sid}/uploads/${sid}.mp4`, start: 0, in: 0, out: 5 }] }],
})
const infoOf = (sid: string) => ({ ops: [{ op: `op_${sid}` }], name: `name ${sid}`, redo_available: false, summary: { edl_hash: `h_${sid}` } })

vi.mock('./api', () => ({
  api: {
    getSession: vi.fn((sid: string) => new Promise((resolve) => pending.push({ sid, kind: 'info', resolve }))),
    getEDL: vi.fn((sid: string) => new Promise((resolve) => pending.push({ sid, kind: 'edl', resolve }))),
    preview: vi.fn(async () => ({ edl_hash: 'x' })),
  },
}))

const mem = new Map<string, string>()
vi.stubGlobal('localStorage', {
  getItem: (k: string) => mem.get(k) ?? null,
  setItem: (k: string, v: string) => { mem.set(k, v) },
  removeItem: (k: string) => { mem.delete(k) },
})

const { useStore } = await import('./store')
type Edl = ReturnType<typeof edlOf>

const flush = () => new Promise((r) => setTimeout(r, 0))
/** Answer every pending request for `sid`. */
async function release(sid: string) {
  for (const p of pending.filter((q) => q.sid === sid)) p.resolve(p.kind === 'edl' ? edlOf(sid) : infoOf(sid))
  pending.splice(0, pending.length, ...pending.filter((q) => q.sid !== sid))
  await flush()
}
const edlSid = () => (useStore.getState().edl as unknown as Edl | null)?.tracks[0].clips[0].id.slice(2) ?? null

beforeEach(() => {
  pending.length = 0
  useStore.setState({ sessionId: 's_A', edl: edlOf('s_A') as never, ops: [], edlHash: 'h_s_A', playhead: 3, selection: 'c_s_A' })
})

describe('project switch (openSession)', () => {
  it('never renders a state with the new session id and the old project\'s EDL', async () => {
    const seen: Array<[string | null, string | null]> = []
    const unsub = useStore.subscribe(() => seen.push([useStore.getState().sessionId, edlSid()]))
    const p = useStore.getState().openSession('s_B')
    await flush()
    // Still loading: the screen stays wholly on A.
    expect([useStore.getState().sessionId, edlSid()]).toEqual(['s_A', 's_A'])
    await release('s_B')
    await p
    unsub()
    expect(seen.filter(([sid, e]) => sid !== e)).toEqual([])
    const s = useStore.getState()
    expect([s.sessionId, edlSid(), s.edlHash, s.sessionName]).toEqual(['s_B', 's_B', 'h_s_B', 'name s_B'])
    // Per-project view state was reset with the swap.
    expect([s.playhead, s.selection]).toEqual([0, null])
  })

  it('a refresh of the old project that lands after the switch is dropped', async () => {
    const r = useStore.getState().refresh()             // e.g. refreshSoon() after an edit in A
    await flush()
    const o = useStore.getState().openSession('s_B')
    await flush()
    await release('s_B')
    await o
    await release('s_A')                                 // A's slow answer arrives last
    await r
    expect([useStore.getState().sessionId, edlSid(), useStore.getState().edlHash]).toEqual(['s_B', 's_B', 'h_s_B'])
  })

  it('A→B→C in quick succession lands on C even if B answers last', async () => {
    const b = useStore.getState().openSession('s_B')
    const c = useStore.getState().openSession('s_C')
    await flush()
    await release('s_C')
    await c
    await release('s_B')
    await b
    expect([useStore.getState().sessionId, edlSid()]).toEqual(['s_C', 's_C'])
  })
})
