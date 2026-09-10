// Run state for the Prompt bar, as a module-level Zustand store (the
// aiRuns.ts / toast.ts pattern) so the bar, the run log, the clarify card,
// the brain badge and ChatOverlay all read one truth and none of them owns
// it. Events are folded by the pure reducer in promptEvents.ts; this file
// adds the side effects: the fetch, the `op` → refresh + preview, history,
// reconnect, cancel.
//
// Three rules from the spec that shape this file:
//
//   * A run OUTLIVES its stream (§4.2). Closing the connection only
//     unsubscribes; the Mac keeps editing and the only way to stop it is
//     `POST …/prompt/cancel`. So an AbortController here exists for unmounts
//     and session switches, and a dropped stream leads to a reconnect
//     (`GET …/prompt/run`, then `POST …/prompt {resume_run}`), never to a
//     "failed" verdict the timeline would contradict.
//   * One session lock (§4.2). While a run holds it, `/dispatch` answers
//     409 `prompt_running`; while chat streams, the bar waits. `chatBusy`
//     is how ChatOverlay tells the bar, and `isBusy()` is how the bar tells
//     ChatOverlay — the same lock, seen from both sides.
//   * The UI always says which brain answered and why a better one did not
//     (§3.1 honesty rules). `brains` is `/api/prompt/brains` verbatim after
//     normalisation; nothing here invents availability.

import { create } from 'zustand'
import { api, type PromptBody, type PromptPending, type PromptRunEnvelope, type PromptRunRecord } from '../api'
import { useStore, errorMessage } from '../store'
import { toast } from '../toast'
import { answersPayload, type Answers } from './clarifyDefaults'
import {
  EMPTY_RUN, PROMPT_RUNNING_MESSAGE, normalizeBrainsReport, onPromptRunning, promptRunningFromError,
  readSseStream, reduce, startRun, type BrainsReport, type PromptEvent, type PromptRunState,
  type ClarifyState, type PromptStatus, type StepRow, type VerifyEvent,
} from './promptEvents'

export const HISTORY_KEY = 'vai.promptHistory'
export const HISTORY_MAX = 20

export const isBusy = (status: PromptStatus) =>
  status === 'planning' || status === 'running' || status === 'verifying'

function readHistory(): string[] {
  try {
    const raw = typeof localStorage === 'undefined' ? null : localStorage.getItem(HISTORY_KEY)
    const parsed = raw ? JSON.parse(raw) : []
    return Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === 'string').slice(0, HISTORY_MAX) : []
  } catch { return [] }
}
function writeHistory(items: string[]) {
  try { if (typeof localStorage !== 'undefined') localStorage.setItem(HISTORY_KEY, JSON.stringify(items)) } catch { /* private mode */ }
}

/** Newest first, de-duplicated, capped — the ↑/↓ recall list. */
export function pushHistory(items: string[], prompt: string): string[] {
  const p = prompt.trim()
  if (!p) return items
  return [p, ...items.filter((x) => x !== p)].slice(0, HISTORY_MAX)
}

interface PromptStoreState extends PromptRunState {
  sid: string | null          // the session the current run/log belongs to
  runId: string | null
  prompt: string              // the sentence the current/last run was made from
  history: string[]
  brains: BrainsReport | null
  brainsError: string | null
  brainsLoading: boolean
  chatBusy: boolean
  focusNonce: number
  logOpen: boolean
  // The stream ended before `done`. The run may still be going on the Mac.
  connectionDropped: boolean
  reconnecting: boolean

  run(prompt: string): Promise<void>
  answer(answers: Answers): Promise<void>
  cancel(): Promise<void>
  dropClarify(): Promise<void>
  reconnect(sid: string): Promise<void>
  hydrate(sid: string, record: PromptRunRecord, live?: boolean): void
  restorePending(sid: string): Promise<boolean>
  loadBrains(refresh?: boolean): Promise<void>
  focus(): void
  dismiss(): void
  setLogOpen(open: boolean): void
  setChatBusy(busy: boolean): void
  applyEvent(evt: { type: string }): void
}

// One live reader at a time. Kept outside the store so a re-render never
// clones it and so `dismiss()` can drop a stale subscription.
let _abort: AbortController | null = null

function uiState() {
  const { sessionId, selection, multiSelection, playhead } = useStore.getState()
  return { sessionId, selection: selection ?? null, multi_selection: multiSelection ?? [], playhead }
}

export const usePromptStore = create<PromptStoreState>((set, get) => {
  /** Read one SSE response to the end, folding every frame into the store. */
  async function consume(res: Response, signal: AbortSignal): Promise<void> {
    const body = res.body
    if (!body) return
    const onAbort = () => { void body.cancel().catch(() => undefined) }
    signal.addEventListener('abort', onAbort, { once: true })
    try {
      await readSseStream(body, (evt) => { if (!signal.aborted) get().applyEvent(evt) })
    } finally {
      signal.removeEventListener('abort', onAbort)
    }
  }

  /** Start a fresh subscription, cancelling whatever reader came before. */
  function freshSignal(): AbortSignal {
    _abort?.abort()
    _abort = new AbortController()
    return _abort.signal
  }

  /**
   * After a stream closes: if the turn never reached `done`, the connection
   * dropped — say so and try to pick the run back up rather than declaring
   * an outcome the Mac has not reported.
   */
  async function settle(sid: string, signal: AbortSignal): Promise<void> {
    if (signal.aborted) return
    const s = get()
    if (s.sid !== sid || !isBusy(s.status)) return
    set({ connectionDropped: true })
    await get().reconnect(sid)
  }

  async function startTurn(sid: string, prompt: string, open: () => Promise<Response>): Promise<void> {
    const signal = freshSignal()
    set({ ...startRun(), sid, runId: null, prompt, logOpen: true, connectionDropped: false, reconnecting: false })
    let res: Response
    try {
      res = await open()
    } catch (e) {
      if (signal.aborted) return
      const running = promptRunningFromError(e)
      if (running) {
        // Another run (a phone, a second tab) holds the lock: attach to it
        // instead of failing — the user asked for an edit and there is one
        // in flight to watch.
        set({ status: 'idle' })
        toast.info('A prompt is already running — showing it')
        await get().reconnect(sid)
        return
      }
      set({ status: 'error', lastError: errorMessage(e) })
      return
    }
    try {
      await consume(res, signal)
    } catch (e) {
      // A rejected reader.read() is a network drop mid-run, not a failed run.
      if (!signal.aborted) console.warn('[prompt] stream dropped:', errorMessage(e))
    }
    await settle(sid, signal)
  }

  return {
    ...EMPTY_RUN,
    sid: null,
    runId: null,
    prompt: '',
    history: readHistory(),
    brains: null,
    brainsError: null,
    brainsLoading: false,
    chatBusy: false,
    focusNonce: 0,
    logOpen: false,
    connectionDropped: false,
    reconnecting: false,

    applyEvent: (evt) => {
      const before = get()
      const next = reduce(before, evt as PromptEvent)
      const patch: Partial<PromptStoreState> = { ...next }
      // The run id is the plan id on the wire (`run p_xxxx` in the provisional
      // reply, `plan.id` on the plan event, `plan_id` on verify/clarify).
      if (evt.type === 'plan' && next.plan?.id) patch.runId = next.plan.id
      set(patch)
      if (evt.type === 'op') {
        // EDL changed — same as ChatOverlay: refresh the store, then re-render
        // the preview. Errors surface as toasts via refreshSoon's pattern.
        const st = useStore.getState()
        st.refresh().then(() => st.renderPreview()).catch((e) => {
          console.warn('[prompt] refresh after op failed:', e)
          toast.error(`Couldn't refresh the timeline: ${errorMessage(e)}`)
        })
      }
    },

    run: async (prompt) => {
      const text = prompt.trim()
      const { sessionId, selection, multi_selection, playhead } = uiState()
      if (!text || !sessionId) return
      const s = get()
      if (isBusy(s.status) || s.chatBusy) return
      const history = pushHistory(s.history, text)
      writeHistory(history)
      set({ history })
      const body: PromptBody = { message: text, selection, multi_selection, playhead }
      await startTurn(sessionId, text, () => api.promptStream(sessionId, body))
    },

    answer: async (answers) => {
      const s = get()
      if (!s.clarify || !s.sid) return
      let payload: Answers
      try {
        payload = answersPayload(s.clarify.questions, answers)
      } catch (e) {
        set({ lastError: errorMessage(e) })
        return
      }
      const { token } = s.clarify
      const sid = s.sid
      // The resumed turn re-sends `plan` and runs; the reducer clears
      // `clarify` on that plan event. Keep the prompt and the brain ladder —
      // it is the same turn, answered.
      const signal = freshSignal()
      set({ status: 'planning', clarify: null, reply: '', steps: [], verify: null, lastError: null,
            opSeen: false, connectionDropped: false, logOpen: true })
      let res: Response
      try {
        res = await api.promptAnswer(sid, token, payload)
      } catch (e) {
        if (signal.aborted) return
        set({ status: 'error', lastError: errorMessage(e) })
        return
      }
      try {
        await consume(res, signal)
      } catch (e) {
        if (!signal.aborted) console.warn('[prompt] stream dropped:', errorMessage(e))
      }
      await settle(sid, signal)
    },

    cancel: async () => {
      const s = get()
      // A run started elsewhere (phone, second tab) has no `sid` here yet —
      // the 409 toast's Cancel still has to reach it.
      const sid = s.sid ?? useStore.getState().sessionId
      if (!sid) return
      try {
        await api.promptCancel(sid)
        // The server answers on the stream: `error{"Cancelled — timeline
        // unchanged."}` then `done`. If no stream is attached (reconnect
        // pending) say it here so the bar does not sit on "running".
        if (!_abort || _abort.signal.aborted) {
          set({ status: 'error', lastError: 'Cancelled — timeline unchanged.' })
        }
      } catch (e) {
        toast.error(`Couldn't cancel: ${errorMessage(e)}`)
      }
    },

    dropClarify: async () => {
      const s = get()
      if (!s.clarify || !s.sid) return
      const { token } = s.clarify
      set({ status: 'idle', clarify: null, plan: null, reply: '', logOpen: false })
      try {
        await api.promptCancel(s.sid, token)
      } catch (e) {
        // The pending file expires on its own in 10 minutes; a failed drop is
        // worth a line, not a red toast.
        console.warn('[prompt] dropping the question failed:', errorMessage(e))
      }
    },

    reconnect: async (sid) => {
      if (get().reconnecting) return
      set({ reconnecting: true })
      let env: PromptRunEnvelope | null
      try {
        env = await api.promptRun(sid)
      } catch (e) {
        // 404 = the session has never run a prompt; anything else is worth a
        // console line and nothing more — this is a background probe.
        if (!/^404 /.test(errorMessage(e))) console.warn('[prompt] run lookup failed:', errorMessage(e))
        set({ reconnecting: false })
        return
      }
      // The route answers `{run, live, replayable}`; a stub or an older
      // backend may answer something else — treat anything without a record
      // as "never ran" rather than crash the bar.
      const record = env && typeof env === 'object' && env.run && typeof env.run === 'object' ? env.run : null
      const live = !!env && typeof env === 'object' && env.live === true
      if (!record) {
        set({ reconnecting: false })
        await get().restorePending(sid)
        return
      }
      const status = String(record.status ?? '')
      const busyStatus = status === 'running' || status === 'planning' || status === 'verifying'
      if (!busyStatus || !live) {
        // A finished run — or one the backend no longer holds — is shown
        // only if nothing newer is on screen.
        const s = get()
        if (s.status === 'idle' || (s.sid === sid && s.connectionDropped) || (s.sid === sid && s.runId === record.run_id)) {
          get().hydrate(sid, record, live)
        }
        set({ reconnecting: false, connectionDropped: false })
        await get().restorePending(sid)
        return
      }
      const runId = typeof record.run_id === 'string' ? record.run_id : null
      get().hydrate(sid, record, live)
      set({ reconnecting: false })
      if (!runId) return
      // Re-attach: the route replays the bus, so the reducer rebuilds the run
      // from its first event (fresh state, keep the hydrated header).
      const signal = freshSignal()
      set({ ...startRun(), sid, runId, prompt: get().prompt, logOpen: true, connectionDropped: false })
      let res: Response
      try {
        res = await api.promptStream(sid, { message: '', resume_run: runId })
      } catch (e) {
        if (signal.aborted) return
        set({ connectionDropped: true, lastError: errorMessage(e) })
        return
      }
      try {
        await consume(res, signal)
      } catch (e) {
        if (!signal.aborted) console.warn('[prompt] replay dropped:', errorMessage(e))
      }
      await settle(sid, signal)
    },

    restorePending: async (sid) => {
      // A clarification outlives the page (pending.py keeps it 10 minutes):
      // after a reload the card comes back from `GET …/prompt/pending`. Never
      // over a run that is busy here, and never for another session.
      const s = get()
      if (s.sid !== null && s.sid !== sid) return false
      if (isBusy(s.status)) return false
      // Already showing a card (a live turn paused here): nothing to restore.
      // A record hydrated as `clarify` has no questions yet — fetch them.
      if (s.status === 'clarify' && s.clarify) return true
      let pending: PromptPending | null
      try {
        const res = await api.promptPending(sid)
        pending = res && typeof res === 'object' && res.pending && typeof res.pending === 'object' ? res.pending : null
      } catch (e) {
        if (!/^404 /.test(errorMessage(e))) console.warn('[prompt] pending lookup failed:', errorMessage(e))
        return false
      }
      if (!pending || typeof pending.token !== 'string' || !Array.isArray(pending.questions) || !pending.questions.length) return false
      const brainId = typeof pending.brain === 'string' ? pending.brain : null
      set({
        ...EMPTY_RUN,
        status: 'clarify',
        sid,
        runId: typeof pending.plan_id === 'string' ? pending.plan_id : null,
        prompt: typeof pending.prompt === 'string' ? pending.prompt : get().prompt,
        brain: brainId ? { status: 'answered', brain: brainId, label: '' } : null,
        clarify: {
          token: pending.token,
          planId: typeof pending.plan_id === 'string' ? pending.plan_id : '',
          questions: pending.questions as ClarifyState['questions'],
          expiresInS: typeof pending.expires_in_s === 'number' ? pending.expires_in_s : 0,
        },
        logOpen: true,
      })
      return true
    },

    hydrate: (sid, record, live = false) => {
      // A run record → run state, tolerant of partial fields. Steps are the
      // last `step` event per index; verify is the verify event; the reply is
      // the accumulated text.
      const steps: StepRow[] = []
      for (const raw of Array.isArray(record.steps) ? record.steps : []) {
        const r = raw as Partial<StepRow> | null
        if (!r || typeof r.index !== 'number' || typeof r.tool !== 'string') continue
        const row: StepRow = { index: r.index, total: typeof r.total === 'number' ? r.total : 0,
                               tool: r.tool, status: (r.status ?? 'ok') as StepRow['status'],
                               progress: r.progress, summary: r.summary, effect: r.effect, error: r.error }
        const i = steps.findIndex((s) => s.index === row.index)
        if (i === -1) steps.push(row); else steps[i] = row
      }
      steps.sort((a, b) => a.index - b.index)
      const verify = record.verify && typeof record.verify === 'object' && Array.isArray((record.verify as VerifyEvent).checks)
        ? ({ type: 'verify', ...(record.verify as object) } as VerifyEvent) : null
      const status = String(record.status ?? '')
      const busyStatus = status === 'running' || status === 'planning' || status === 'verifying'
      const mapped: PromptStatus =
        status === 'done' || status === 'ok' || status === 'completed' ? 'done'
        : status === 'error' || status === 'failed' || status === 'cancelled' ? 'error'
        : status === 'clarify' || status === 'pending' ? 'clarify'
        : busyStatus ? (live ? (status as PromptStatus) : 'error')
        : 'done'
      const brainId = typeof record.brain === 'string' ? record.brain : null
      const recordError = typeof record.error === 'string' && record.error ? record.error
        : typeof record.reply === 'string' && record.reply ? record.reply : null
      const lastError = mapped !== 'error' ? null
        : busyStatus && !live
          // Honest about what is known: the file says "running", the process
          // has no such run — a restart or crash mid-run. The timeline shows
          // whatever landed; nothing here guesses.
          ? 'The run was interrupted — the server no longer has it. The timeline shows what landed; check History.'
          : recordError ?? 'The run did not finish.'
      set({
        ...EMPTY_RUN,
        status: mapped,
        sid,
        runId: typeof record.run_id === 'string' ? record.run_id : null,
        prompt: typeof record.prompt === 'string' ? record.prompt : get().prompt,
        brain: brainId ? { status: 'answered', brain: brainId, label: '' } : null,
        steps,
        verify,
        reply: typeof record.reply === 'string' ? record.reply : '',
        opSeen: !!record.op,
        lastError,
        logOpen: steps.length > 0 || !!verify || !!record.reply || mapped === 'error',
      })
    },

    loadBrains: async (refresh = false) => {
      const s = get()
      if (s.brainsLoading) return
      // A report already in hand is served until someone asks to Recheck;
      // the backend memoises 60 s anyway and the badge opens often.
      if (!refresh && s.brains) return
      set({ brainsLoading: true, brainsError: null })
      try {
        const raw = await api.promptBrains(refresh)
        set({ brains: normalizeBrainsReport(raw), brainsLoading: false })
      } catch (e) {
        set({ brainsError: errorMessage(e), brainsLoading: false })
      }
    },

    focus: () => set({ focusNonce: get().focusNonce + 1 }),

    dismiss: () => {
      // Hide the log and forget the last run's rows. Never cancels: a run
      // that is still going keeps going (and the bar keeps showing it).
      if (isBusy(get().status)) { set({ logOpen: false }); return }
      _abort?.abort()
      _abort = null
      set({ ...EMPTY_RUN, runId: null, logOpen: false, connectionDropped: false })
    },

    setLogOpen: (open) => set({ logOpen: open }),
    setChatBusy: (busy) => set({ chatBusy: busy }),
  }
})

/**
 * `/dispatch` answered 409 `prompt_running` (spec §5.1): say so once, offer
 * Cancel, and attach the bar to the run that holds the lock. Registered with
 * store.ts through promptEvents.onPromptRunning at module load.
 */
export function notifyPromptRunning(sid: string): void {
  toast.action(PROMPT_RUNNING_MESSAGE,
    { label: 'Cancel', onClick: () => { void usePromptStore.getState().cancel() } },
    { kind: 'info', ttlMs: 6000 })
  const s = usePromptStore.getState()
  if (!isBusy(s.status)) void s.reconnect(sid)
}

/** For tests and hot reload: drop the live subscription without cancelling the run. */
export function _detachPromptStream(): void {
  _abort?.abort()
  _abort = null
}

onPromptRunning(notifyPromptRunning)
