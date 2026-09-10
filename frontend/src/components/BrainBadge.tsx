// Which brain answered, and why a better one did not.
//
// The pill reads "Recipes", "Apple Intelligence", "Local model · Qwen 7B" or
// "Claude" with a dot: filled = answered this run, hollow = available,
// struck = unavailable. When the content came from a different brain than
// the plan ("Recipes · text by Apple Intelligence", spec §3.7) both are
// named. The popover is `/api/prompt/brains` verbatim — every unavailable row
// carries its `fix` — plus this run's attempt ladder.
//
// The one action here that reaches the network is "Download": it is offered
// only when the report says `action:"download"` AND the page is served from
// a loopback origin (the route itself is loopback-only and answers 403
// otherwise — a paired phone must not be able to start a 4 GB fetch on the
// Mac). Progress comes from /api/jobs/{id}, the same poll Export uses.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { api, type PromptModelRow } from '../api'
import { errorMessage } from '../store'
import { usePromptStore } from '../lib/promptStore'
import { brainLabel, humanBytes, isLoopbackOrigin, shortModel,
         type BrainAttempt, type BrainRow } from '../lib/promptEvents'

const JOB_POLL_MS = 700

type DlState =
  | { status: 'idle' }
  | { status: 'running'; jobId: string; progress: number }
  | { status: 'done' }
  | { status: 'error'; message: string }

export function BrainBadge() {
  const brain = usePromptStore((s) => s.brain)
  const attempts = usePromptStore((s) => s.attempts)
  const plan = usePromptStore((s) => s.plan)
  const status = usePromptStore((s) => s.status)
  const brains = usePromptStore((s) => s.brains)
  const brainsError = usePromptStore((s) => s.brainsError)
  const brainsLoading = usePromptStore((s) => s.brainsLoading)
  const loadBrains = usePromptStore((s) => s.loadBrains)

  const [open, setOpen] = useState(false)
  const [models, setModels] = useState<PromptModelRow[] | null>(null)
  const [dl, setDl] = useState<DlState>({ status: 'idle' })
  // The popover is portaled to document.body and positioned from the pill's
  // rect — .center clips overflow on both axes (styles.css), so a child
  // positioned under the pill was cut off at the column edge at the 900px
  // floor, the same clip TopBar's pickers and the CC menu escape this way.
  const [popPos, setPopPos] = useState<{ left: number; top: number; width: number } | null>(null)
  const btnRef = useRef<HTMLButtonElement>(null)
  const popRef = useRef<HTMLDivElement>(null)
  const recheckRef = useRef<HTMLButtonElement>(null)
  const loopback = isLoopbackOrigin()

  // Once, on mount. NOT keyed on `brains`/`brainsLoading`: while the prompt
  // routes are absent (or the report 5xxs) the report stays null and the
  // loading flag flips true→false on every failure — an effect keyed on
  // those re-fired on each flip and hammered the backend into 429s.
  useEffect(() => { void loadBrains() }, [loadBrains])

  // What the pill names when no run has answered yet: the first available
  // brain in ladder order (recipes is always there), never a guess.
  const resting = useMemo(() => brains?.brains.find((b) => b.available) ?? null, [brains])
  const trying = status === 'planning' ? attempts.filter((a) => a.status === 'trying').at(-1) ?? null : null
  const shown: { id: string; label: string; model?: string | null } | null =
    trying ? { id: trying.brain, label: trying.label || brainLabel(trying.brain), model: trying.model }
    : brain ? { id: brain.brain, label: brain.label || brainLabel(brain.brain), model: brain.model }
    : resting ? { id: resting.id, label: resting.label, model: resting.model } : null

  const dotClass = trying ? 'is-trying' : brain ? 'is-answered' : resting ? '' : 'is-struck'
  const contentBy = plan?.content_brain && plan.content_brain !== plan.brain ? brainLabel(plan.content_brain) : null
  const modelTag = shown?.id === 'local_model' ? shortModel(shown.model) : ''
  // No report yet: say "loading" while it loads and "unavailable" when it
  // failed — never a brain name the backend has not confirmed.
  const pillText = shown
    ? `${shown.label}${contentBy ? ` · text by ${contentBy}` : ''}`
    : brainsError ? 'Brains unavailable' : 'Brains…'
  const title = shown ? (brains?.brains.find((b) => b.id === shown.id)?.detail || '')
    : brainsError ? `The brain report could not be read: ${brainsError}` : 'Loading the brain report'

  const close = useCallback(() => { setOpen(false); setPopPos(null); btnRef.current?.focus() }, [])

  // Place from the pill's rect, clamped to the viewport. Called when the
  // popover opens (from the click) and on resize (from the listener) — never
  // from an effect body, so opening is one render, not a cascade.
  const place = useCallback(() => {
    const r = btnRef.current?.getBoundingClientRect()
    if (!r) return
    const width = Math.min(380, window.innerWidth - 16)
    const left = Math.max(8, Math.min(r.right - width, window.innerWidth - width - 8))
    setPopPos({ left, top: r.bottom + 6, width })
  }, [])

  // Popover: Esc / outside click close; focus lands on Recheck and returns to
  // the pill. Keyed on `key`, not `code`: the keymap engine wants the physical
  // key, but a dismiss gesture should follow whatever the OS calls Escape.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.stopPropagation(); close() } }
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node
      if (popRef.current?.contains(t) || btnRef.current?.contains(t)) return
      close()
    }
    window.addEventListener('keydown', onKey, true)
    window.addEventListener('mousedown', onDown)
    return () => { window.removeEventListener('keydown', onKey, true); window.removeEventListener('mousedown', onDown) }
  }, [open, close])

  // While open: focus lands on Recheck and the popover follows a resize.
  useLayoutEffect(() => {
    if (!open) return
    recheckRef.current?.focus()
    window.addEventListener('resize', place)
    return () => window.removeEventListener('resize', place)
  }, [open, place])

  const toggle = () => {
    const next = !open
    if (next) place(); else setPopPos(null)
    setOpen(next)
    if (next) {
      void loadBrains()
      // The free-space line and expected size for the Download row.
      if (loopback && models === null) {
        api.promptModels().then((r) => setModels(r.models ?? [])).catch(() => setModels([]))
      }
    }
  }

  const startDownload = async (row: BrainRow) => {
    const id = row.model ?? models?.find((m) => !m.installed)?.id
    if (!id) return
    setDl({ status: 'running', jobId: '', progress: 0 })
    try {
      const { job_id } = await api.downloadModel(id)
      setDl({ status: 'running', jobId: job_id, progress: 0 })
      for (;;) {
        await new Promise((r) => setTimeout(r, JOB_POLL_MS))
        const job = await api.getJob(job_id)
        if (job.status === 'completed') { setDl({ status: 'done' }); await loadBrains(true); return }
        if (job.status === 'failed') { setDl({ status: 'error', message: job.error || 'download failed' }); return }
        if (job.status === 'cancelled') { setDl({ status: 'idle' }); return }
        setDl({ status: 'running', jobId: job_id, progress: job.progress ?? 0 })
      }
    } catch (e) {
      setDl({ status: 'error', message: errorMessage(e) })
    }
  }

  const cancelDownload = async () => {
    if (dl.status !== 'running' || !dl.jobId) return
    try { await api.cancelJob(dl.jobId) } catch (e) { setDl({ status: 'error', message: errorMessage(e) }) }
  }

  const modelRow = (row: BrainRow) => models?.find((m) => m.id === row.model) ?? models?.find((m) => !m.installed) ?? null

  return (
    <div className="brain-badge">
      <button
        ref={btnRef}
        type="button"
        className="brain-pill"
        title={title}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={`Brain: ${pillText}${modelTag ? ` (${modelTag})` : ''}. Show the brain report`}
        onClick={toggle}
      >
        <span className={`brain-dot ${dotClass}`} aria-hidden="true" />
        <span className="name">{pillText}{modelTag && <span className="model"> · {modelTag}</span>}</span>
      </button>
      {open && popPos && createPortal(
        <div className="brain-pop" ref={popRef} role="dialog" aria-label="Brains"
             style={{ left: popPos.left, top: popPos.top, width: popPos.width }}>
          <div className="brain-pop-head">
            <h3>Brains — which one answers, and why</h3>
            <button type="button" ref={recheckRef} disabled={brainsLoading} onClick={() => void loadBrains(true)}>
              {brainsLoading ? 'Checking…' : 'Recheck'}
            </button>
          </div>
          {brainsError && <div className="prompt-error">Couldn't read the brain report: {brainsError}</div>}
          {brains && (
            <ul className="brain-rows">
              {brains.brains.map((row) => {
                const answered = brain?.brain === row.id
                const canDownload = loopback && row.action === 'download' && !row.available
                const m = canDownload ? modelRow(row) : null
                const size = m?.expected_bytes ?? row.bytes ?? null
                return (
                  <li className="brain-row" key={row.id}>
                    <span className={`brain-dot ${answered ? 'is-answered' : row.available ? '' : 'is-struck'}`} aria-hidden="true" />
                    <div className="brain-row-name">
                      {row.label}
                      {row.id === 'local_model' && row.model && <span className="tag">{shortModel(row.model)}</span>}
                      {answered && <span className="tag answered">answered</span>}
                      {!answered && <span className="tag">{row.available ? 'available' : 'unavailable'}</span>}
                    </div>
                    {row.detail && <div className="brain-row-detail">{row.detail}</div>}
                    {!row.available && row.fix && <pre className="brain-row-fix">{row.fix}</pre>}
                    {canDownload && (
                      <div className="brain-row-actions">
                        {dl.status === 'running' ? (
                          <>
                            <button type="button" onClick={() => void cancelDownload()}>Cancel</button>
                            <span className="note">{Math.round(dl.progress * 100)}%{size ? ` of ${humanBytes(size)}` : ''}</span>
                          </>
                        ) : (
                          <button type="button" className="primary" onClick={() => void startDownload(row)}>
                            Download{size ? ` ${humanBytes(size)}` : ''}
                          </button>
                        )}
                        {typeof m?.free_bytes === 'number' && <span className="note">{humanBytes(m.free_bytes)} free</span>}
                        {dl.status === 'error' && <span className="note bad">{dl.message}</span>}
                        {dl.status === 'done' && <span className="note good">Downloaded — rechecked</span>}
                      </div>
                    )}
                    {canDownload && dl.status === 'running' && (
                      <div className="brain-dl-bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(dl.progress * 100)}>
                        <span style={{ transform: `scaleX(${Math.max(0.02, dl.progress)})` }} />
                      </div>
                    )}
                    {!loopback && row.action === 'download' && !row.available && (
                      <div className="brain-row-detail">Downloads can only be started from the Mac itself.</div>
                    )}
                  </li>
                )
              })}
            </ul>
          )}
          {attempts.length > 0 && <Ladder attempts={attempts} />}
        </div>,
        document.body,
      )}
    </div>
  )
}

function Ladder({ attempts }: { attempts: BrainAttempt[] }) {
  return (
    <div className="brain-ladder">
      <h4>This run</h4>
      <ol>
        {attempts.map((a, i) => (
          <li key={i}>
            <b>{a.label || brainLabel(a.brain)}</b> {a.status}
            {typeof a.latency_ms === 'number' && <span className="ms"> · {a.latency_ms} ms</span>}
            {a.detail && <> — {a.detail}</>}
          </li>
        ))}
      </ol>
    </div>
  )
}
