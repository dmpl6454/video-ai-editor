import { useEffect, useMemo, useRef, useState } from 'react'
import { api, type ToolSchema } from '../api'
import { ASYNC_DISPATCH_TOOLS, useStore } from '../store'
import { toast } from '../toast'
import { clipRequirement, gateFor, motionTrackSeed, type CatalogEntry } from '../lib/aiCatalog'
import { useAiRuns, type RunState } from '../lib/aiRuns'
import { buildArgs, fieldsFor, initialValues, reseedContextValues, type FormContext } from '../lib/schemaForm'
import { AiToolForm } from './AiToolForm'
import { AiResult } from './AiResult'

interface Props {
  entry: CatalogEntry
  schema: ToolSchema
  onRun: (entry: CatalogEntry, args: Record<string, unknown>) => Promise<void>
}

type Running = Extract<RunState, { status: 'running' }>
const IDLE: RunState = { status: 'idle' }

// The CC button's remembered choices (CaptionsButton.tsx) so the Auto captions
// card agrees with it. 'as-spoken' / 'quality' mean "send nothing".
function readCaptionPrefs(): { captionTargetPref: string | null; captionSpeedPref: string | null } {
  try {
    const target = localStorage.getItem('vai.captionTarget')
    const speed = localStorage.getItem('vai.captionSpeed')
    return { captionTargetPref: target && target !== 'as-spoken' ? target : null,
             captionSpeedPref: speed === 'fast' ? 'large-v3-turbo' : null }
  } catch { return { captionTargetPref: null, captionSpeedPref: null } }
}

// What a form is seeded from, read fresh from the store at the moment of use.
function formContext(): FormContext {
  const s = useStore.getState()
  return { playhead: s.playhead, inMark: s.inMark, outMark: s.outMark, ...readCaptionPrefs() }
}

// The packaged WKWebView is a non-secure http://127.0.0.1 origin where
// navigator.clipboard is undefined — and packaged users are exactly the ones
// who must copy a `fix`. Fall back to selecting the <pre> and the legacy
// command; if even that refuses, the text stays selected for a manual ⌘C.
async function copyText(text: string, el: HTMLElement | null): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(text); return true }
  } catch { /* permission denied / insecure context — try the selection path */ }
  if (!el) return false
  try {
    const range = document.createRange()
    range.selectNodeContents(el)
    const sel = window.getSelection()
    sel?.removeAllRanges()
    sel?.addRange(range)
    return document.execCommand('copy')
  } catch { return false }
}

// One-second clock for the elapsed / "N s ago" readouts: ticks while a run is
// in flight and for a minute after it lands, then stops (the "ago" text
// freezes at "1 min ago" rather than a 40-card list ticking forever).
function useNow(running: boolean, doneAt: number | null): number {
  const [now, setNow] = useState(() => Date.now())
  const active = running || (doneAt !== null && now - doneAt < 60_000)
  useEffect(() => {
    if (!active) return
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [active])
  return now
}

const secs = (ms: number) => Math.max(0, Math.floor(ms / 1000))
const ago = (ms: number) => { const s = secs(ms); return s < 60 ? `${s}s ago` : `${Math.floor(s / 60)} min ago` }

// What the screen reader hears. It changes only on transitions — never on
// the one-second clock — so a three-minute caption run is a handful of
// announcements, not ~180 "Working… 12s / 13s / …". Progress is announced
// in quarters; the exact figure is on the progressbar's aria-valuenow. Errors
// are left to the visible role="alert" line so they are not read twice.
function liveAnnouncement(run: RunState, label: string): string {
  switch (run.status) {
    case 'running': {
      if (run.cancelling) return `${label}: stopping after the current chunk`
      const quarter = run.reportsProgress ? Math.floor(run.progress * 4) * 25 : 0
      return quarter > 0 && quarter < 100 ? `${label}: working, ${quarter}% done` : `${label}: working`
    }
    case 'done': return `${label}: done`
    default: return ''
  }
}

function RunningState({ run, now, onCancel }: { run: Running; now: number; onCancel: () => void }) {
  const elapsed = secs(now - run.startedAt)
  const pct = Math.round(run.progress * 100)
  const determinate = run.reportsProgress && pct > 0
  const text = run.cancelling ? `Stopping… finishing the current chunk · ${elapsed}s`
    : run.reportsProgress ? `Working… ${pct > 0 ? `${pct}% · ` : ''}${elapsed}s`
    : `Working… ${elapsed}s — this tool can’t be interrupted; the result will still land`
  return (
    <div className="ai-state ai-state-running">
      {run.reportsProgress ? (
        <div className={`ai-bar${determinate ? '' : ' indeterminate'}`} role="progressbar" aria-label="Progress"
             aria-valuemin={0} aria-valuemax={100} aria-valuenow={determinate ? pct : undefined}>
          <div className="ai-bar-fill" style={determinate ? { transform: `scaleX(${Math.max(0.02, run.progress)})` } : undefined} />
        </div>
      ) : (
        <div className="ai-bar indeterminate" aria-hidden="true"><div className="ai-bar-fill" /></div>
      )}
      {/* Ticks every second — kept out of the accessibility tree; the card's
          live region (AiToolCard) carries the announcements. */}
      <div className="ai-state-line" aria-hidden="true">{text}</div>
      {run.cancellable && !run.cancelling && (
        <button type="button" disabled={!run.jobId} onClick={onCancel} title="Stop after the current chunk">Cancel</button>
      )}
    </div>
  )
}

export function AiToolCard({ entry, schema, onRun }: Props) {
  const tool = entry.tool
  const run = useAiRuns((s) => s.runs[tool]) ?? IDLE
  const features = useAiRuns((s) => s.features)
  const featuresError = useAiRuns((s) => s.featuresError)
  const setRun = useAiRuns((s) => s.setRun)
  const patchRun = useAiRuns((s) => s.patchRun)
  const edl = useStore((s) => s.edl)
  const selection = useStore((s) => s.selection)
  const playhead = useStore((s) => s.playhead)
  const inMark = useStore((s) => s.inMark)
  const outMark = useStore((s) => s.outMark)
  const [open, setOpen] = useState(false)
  const [values, setValues] = useState<Record<string, unknown> | null>(null)
  const [errors, setErrors] = useState<Record<string, string>>({})
  // Fields the user has typed into; the reseed effect below leaves them alone.
  const touched = useRef(new Set<string>())
  const fixRef = useRef<HTMLPreElement>(null)

  const fields = useMemo(() => fieldsFor(schema, entry), [schema, entry])
  const gate = gateFor(entry, features, values ?? undefined)
  const clipReq = clipRequirement(entry, edl, selection)
  const running = run.status === 'running'
  const now = useNow(running, run.status === 'done' ? run.at : null)
  const isJob = ASYNC_DISPATCH_TOOLS.has(tool) || !!entry.runAsJob
  const noKey = !!entry.keyHint && !!features && !features.anthropic_key_set
  const bodyId = `ai-body-${tool}`

  // Values are seeded on first expand, not on mount, so time defaults reflect
  // the playhead / marks at the moment the user opens the form.
  const seed = () => {
    const s = useStore.getState()
    const base = initialValues(fields, formContext())
    return tool === 'motion_track' ? { ...base, ...motionTrackSeed(s.edl, s.selection) } : base
  }
  const toggle = () => {
    if (!open && values === null) setValues(seed())
    setOpen((o) => !o)
  }
  const change = (name: string, value: unknown) => {
    touched.current.add(name)
    setValues((v) => ({ ...(v ?? {}), [name]: value }))
    setErrors((e) => (name in e ? Object.fromEntries(Object.entries(e).filter(([k]) => k !== name)) : e))
  }

  // The marks / playhead a form was seeded from keep moving while it sits
  // open. Untouched time fields follow them — so "Set In/Out marks first"
  // clears itself once the user presses I and O, instead of sticking to the
  // blank read at first expand. reseedContextValues returns the same object
  // when nothing moved, so this is a no-op render most of the time.
  useEffect(() => {
    if (!open) return
    setValues((v) => v && reseedContextValues(fields, v, touched.current, formContext()))
  }, [open, fields, playhead, inMark, outMark])

  const disabledReason = !gate.ok ? `${gate.feature} isn’t installed` : !clipReq.ok ? clipReq.reason : null
  const canRun = !disabledReason && !running

  const submit = async () => {
    if (!canRun || !values) return
    const built = buildArgs(fields, values)
    setErrors(built.errors)
    if (Object.keys(built.errors).length) return
    const args = clipReq.ok && clipReq.clipId ? { ...built.args, clip_id: clipReq.clipId } : built.args
    await onRun(entry, args)
  }

  const cancel = async () => {
    if (run.status !== 'running' || !run.jobId) return
    patchRun(tool, { cancelling: true })
    try {
      await api.cancelJob(run.jobId)
    } catch {
      patchRun(tool, { cancelling: false })
      toast.error('Could not cancel — it may have already finished')
    }
  }

  const copyFix = async () => {
    if (gate.ok) return
    if (await copyText(gate.fix, fixRef.current)) toast.success('Copied')
    else toast.info('Select and copy')
  }

  const stateClass = run.status === 'error' ? (run.cancelled ? 'is-cancelled' : 'is-error')
    : run.status === 'running' ? 'is-running' : run.status === 'done' ? 'is-done' : ''
  const cls = ['ai-card', open ? 'is-open' : '', stateClass, gate.ok ? '' : 'is-unavailable'].filter(Boolean).join(' ')

  return (
    <article className={cls} aria-busy={running || undefined}>
      {/* One persistent live region per card, always rendered (a run keeps
          going while the card is collapsed, and "done" is worth hearing). */}
      <div className="ai-sr-only" role="status" aria-live="polite">{liveAnnouncement(run, entry.label)}</div>
      <button type="button" className="ai-card-head" aria-expanded={open} aria-controls={bodyId} onClick={toggle}>
        <span className="ai-card-title">{entry.label}</span>
        <span className="ai-chevron" aria-hidden="true">›</span>
      </button>
      <div className="ai-card-meta">
        {isJob && <span className="ai-badge" title="Runs in the background — the rest of the editor stays usable">long</span>}
        {entry.readOnly && <span className="ai-badge" title="Doesn’t change the timeline">read-only</span>}
        {entry.advanced && <span className="ai-badge" title="Takes file paths typed by hand — meant for a source install">advanced · source install</span>}
        {!gate.ok && <span className="ai-badge warn">Not installed</span>}
        {gate.ok && gate.checking && !featuresError && <span className="ai-badge dim">checking…</span>}
        {noKey && <span className="ai-badge dim" title={entry.keyHint}>no API key</span>}
        {!gate.ok && <button type="button" className="ai-copy" onClick={() => { void copyFix() }}>Copy fix</button>}
      </div>
      <p className="ai-card-desc">{entry.description}</p>
      {!gate.ok && (
        <div className="ai-unavail">
          <b>{gate.feature}</b>{gate.packagedExcluded ? ' is not included in the packaged app.' : ' is not installed.'}
          {gate.fix && <pre ref={fixRef} className="ai-fix" tabIndex={0}>{gate.fix}</pre>}
        </div>
      )}
      {open && values && (
        <div id={bodyId} className="ai-card-body">
          {noKey && <p className="ai-hint">{entry.keyHint}</p>}
          <AiToolForm tool={tool} label={entry.label} fields={fields} values={values} errors={errors}
                      disabled={running} edl={edl} playhead={playhead} onChange={change}
                      onSubmit={() => { void submit() }} />
          {run.status === 'idle' && (
            <>
              {disabledReason && <p className="ai-hint">{disabledReason}</p>}
              <button type="button" className="primary ai-run" disabled={!canRun} onClick={() => { void submit() }}>Run</button>
            </>
          )}
          {run.status === 'running' && <RunningState run={run} now={now} onCancel={() => { void cancel() }} />}
          {run.status === 'done' && (
            <div className="ai-state ai-state-done">
              {/* "N s ago" ticks; the live region above already said "done". */}
              <div className="ai-state-line" aria-hidden="true">Done · {ago(now - run.at)}</div>
              {entry.readOnly && <AiResult tool={tool} label={entry.label} result={run.result} />}
              <button type="button" onClick={() => setRun(tool, IDLE)}>Run again</button>
            </div>
          )}
          {run.status === 'error' && (
            <div className={`ai-state ${run.cancelled ? 'ai-state-cancelled' : 'ai-state-error'}`}>
              <div className="ai-state-line" role="alert">{run.cancelled ? 'Cancelled' : run.message}</div>
              <button type="button" onClick={() => setRun(tool, IDLE)}>Dismiss</button>
            </div>
          )}
        </div>
      )}
    </article>
  )
}
