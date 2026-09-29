// The record of a run: what was planned, what each step did, what the
// verifier measured, and the honest summary — in that order, because that is
// the order a user reads it to decide whether to keep the edit or undo it.
//
// Nothing here is inferred. Step rows are `step` events (with the tool's own
// summary), `effect:"none"` is shown as "no effect" so a remove_fillers that
// found nothing reads as such before the verifier says so, and the verify
// table prints measured vs expected exactly as the backend reported them.
//
// A step row's label is the tool's EDITOR name ("Remove silences") and its
// summary goes through the same cleanSummary rule as History (QA-101). The
// planner's `why` read as developer text in practice ("auto_reframe may skip
// a clip and Clip.fit defaults to contain") and the tool id is internal: both
// are only the row's hover title.

import { promptUndoAvailable } from '../lib/promptUndo'
import { useState } from 'react'
import { useStore } from '../store'
import { toast } from '../toast'
import { usePromptStore, isBusy } from '../lib/promptStore'
import { brainLabel, createdProjects, humanBytes, humanDuration, replySentence, RUN_WITHOUT_RESULT, type ChildRun, type StepRow, type VerifyCheck } from '../lib/promptEvents'
import { errorMessage } from '../store'
import { checksHeadline, checkValues, shownChecks } from '../lib/checkProse'
import { editorProse, toolTitle } from '../lib/opLabels'
import { Icon, type IconName } from './Icon'

const pad2 = (n: number) => String(n + 1).padStart(2, '0')

function glyph(s: StepRow): IconName {
  switch (s.status) {
    case 'ok': return 'check'
    case 'failed': return 'close'
    case 'skipped': return 'minus'
    case 'cancelled': return 'minus'
    default: return 'more'
  }
}

/** A step row's label: the tool's EDITOR name (QA-101). The planner's `why`
 *  is its internal rationale ("auto_reframe may skip a clip and Clip.fit
 *  defaults to contain") — developer text, kept only as the hover title. */
export function stepLabel(s: StepRow): string {
  return toolTitle(s.tool)
}

/** A step row's second column, in editor language (opLabels.editorProse —
 *  History's cleanSummary rule, plus tool ids named): no clip ids, "clip(s)",
 *  Python reprs or tool ids. */
export function stepSummary(s: StepRow): string {
  if (s.status === 'failed') return editorProse(s.error || 'failed')
  if (s.status === 'skipped') return `skipped${s.error ? ` — ${editorProse(s.error)}` : ''}`
  if (s.status === 'cancelled') return 'cancelled'
  return editorProse(s.summary || '') || (s.status === 'running' ? 'running' : '')
}

/** The folded run's glyph and line (QA-073 / QA-018): a run that applied
 *  nothing is NOT a green tick, and a replacement is said out loud. */
export function collapsedSummary(p: {
  steps: readonly StepRow[]; verify: { checks: VerifyCheck[] } | null; reply: string; headline: string | null
  nothing?: boolean
}): { tone: 'pass' | 'fail' | 'info'; text: string } {
  const failed = !!p.verify && p.verify.checks.some((c) => c.pass === false && c.headline !== false)
  if (failed) return { tone: 'fail', text: p.headline ?? 'A check failed' }
  // A preview that would change nothing: its dry-run steps are not "done"
  // (final sweep 3 r2 — it read "✓ 1 step done").
  if (p.nothing) return { tone: 'info', text: `Nothing to change — ${editorProse(firstSentence(replySentence(p.reply)))}` }
  const real = p.steps.filter((s) => s.tool !== 'verify_render')
  const applied = real.filter((s) => s.status === 'ok' && s.effect !== 'none')
  const notice = real.map((s) => s.summary ?? '').find((t) => /^replaced\b/i.test(t.trim()))
  if (notice) return { tone: 'info', text: editorProse(notice) }
  if (applied.length === 0) {
    // No reply at all: the run ended without reporting (a full disk) — not
    // "Nothing to change" (final sweep 3 r2).
    const why = editorProse(firstSentence(p.reply)) || (p.reply.trim() ? 'Nothing to change' : RUN_WITHOUT_RESULT)
    return { tone: 'info', text: why }
  }
  return { tone: 'pass', text: p.headline ?? `${applied.length} step${applied.length === 1 ? '' : 's'} done` }
}

function firstSentence(text: string): string {
  const t = (text ?? '').replace(/^\s*\[[^\]]*\]\s*/, '').trim()   // drop a "[brain]" prefix
  const m = /^(.+?[.!?])(\s|$)/.exec(t)
  return (m ? m[1] : t).slice(0, 140)
}

export function PromptRunLog() {
  const status = usePromptStore((s) => s.status)
  const plan = usePromptStore((s) => s.plan)
  const prompt = usePromptStore((s) => s.prompt)
  const brain = usePromptStore((s) => s.brain)
  const steps = usePromptStore((s) => s.steps)
  const verify = usePromptStore((s) => s.verify)
  const reply = usePromptStore((s) => s.reply)
  const nothingToApply = usePromptStore((s) => !!s.nothingToApply)
  const lastError = usePromptStore((s) => s.lastError)
  const opSeen = usePromptStore((s) => s.opSeen)
  const opRef = usePromptStore((s) => s.opRef)
  // Only while the prompt's op is still the newest state: a plain `undo`
  // after a later edit undid THAT edit (lib/promptUndo, Final QA).
  const canUndo = useStore((s) => promptUndoAvailable(opSeen, opRef, { edlHash: s.edlHash, ops: s.ops }))
  const connectionDropped = usePromptStore((s) => s.connectionDropped)
  const reconnecting = usePromptStore((s) => s.reconnecting)
  const cancelling = usePromptStore((s) => s.cancelling)
  const dismiss = usePromptStore((s) => s.dismiss)
  const runId = usePromptStore((s) => s.runId)
  const children = usePromptStore((s) => s.children)
  const dispatch = useStore((s) => s.dispatch)
  const [copied, setCopied] = useState(false)
  // A FINISHED run folds to one summary row so the log stops taking the
  // preview's height (QA-017: the open log squeezed a 9:16 viewer to 128×228).
  // "Details" opens it again; a new run, a failure or a live run shows in full.
  const runKey = runId ?? prompt ?? ''
  const [openFor, setOpenFor] = useState<string | null>(null)

  const busy = isBusy(status)
  // Projects the run created (a shorts run): each one gets an Open button.
  const projects = createdProjects({ steps, children })
  const title = plan?.title || plan?.intent || prompt || 'Prompt'
  const via = brain ? (brain.label || brainLabel(brain.brain)) : plan ? brainLabel(plan.brain) : null
  const contentBy = plan?.content_brain && plan.content_brain !== plan.brain ? brainLabel(plan.content_brain) : null
  const downloads = plan?.downloads_needed ?? []
  const eta = typeof plan?.estimated_seconds === 'number' ? plan.estimated_seconds : null

  const copyPlan = async () => {
    if (!plan) return
    try {
      await navigator.clipboard.writeText(JSON.stringify(plan, null, 2))
      setCopied(true)
      setTimeout(() => setCopied(false), 1400)
    } catch {
      toast.error('Clipboard is not available here')
    }
  }

  const collapsed = status === 'done' && openFor !== runKey
  if (collapsed) {
    const chip = collapsedSummary({ steps, verify, reply, headline: checksHeadline(verify), nothing: nothingToApply })
    const g: IconName = chip.tone === 'fail' ? 'close' : chip.tone === 'info' ? 'info' : 'check'
    return (
      <section className="prompt-log is-collapsed" aria-label="Prompt run">
        <div className="prompt-log-summary">
          <span className={`g is-${chip.tone}`} aria-hidden="true"><Icon name={g} /></span>
          <span className="prompt-log-title">{title}</span>
          <span className="sum">{chip.text}</span>
          <span className="spacer" />
          <button type="button" className="prompt-log-expand" aria-expanded={false}
                  onClick={() => setOpenFor(runKey)} title="Show the steps and what the verifier measured">Details</button>
          {canUndo && (
            <button type="button" onClick={() => void dispatch('undo')} title="Undo the whole prompt (one history step)">Undo</button>
          )}
          <button type="button" className="ghost" onClick={dismiss}>Clear</button>
        </div>
        <CreatedProjects items={projects} />
      </section>
    )
  }

  return (
    <section className="prompt-log" aria-label="Prompt run">
      <div className="prompt-log-head">
        <span className="prompt-log-title">{title}</span>
        {via && (
          <span className="prompt-log-via">via <b>{via}</b>{contentBy && <> · text by <b>{contentBy}</b></>}</span>
        )}
      </div>
      {plan && (downloads.length > 0 || eta !== null || plan.confidence < 1) && (
        <div className="prompt-log-meta">
          {eta !== null && <span><b>{humanDuration(eta)}</b> estimated</span>}
          {downloads.length > 0 && (
            <span className="dl">
              downloads: {downloads.map((d) => `${d.what} (${humanBytes(d.bytes)})`).join(', ')}
            </span>
          )}
          {plan.confidence < 1 && <span>confidence <b>{plan.confidence.toFixed(2)}</b></span>}
        </div>
      )}

      {steps.length > 0 && (
        <ol className="prompt-steps" aria-label="Steps">
          {steps.map((s) => {
            const verifyRow = s.tool === 'verify_render'
            const cls = `prompt-step is-${s.status}${s.effect === 'none' ? ' is-none' : ''}${verifyRow ? ' is-verify' : ''}`
            return (
              <li key={`${s.index}-${s.tool}`} className={cls}>
                <span className="n">{verifyRow ? '··' : pad2(s.index)}</span>
                <span className="g" aria-hidden="true"><Icon name={glyph(s)} /></span>
                {/* The tool id and the planner's rationale are internal
                    (QA-101): hover only, never the visible label. */}
                <span className="tool" title={[plan?.steps?.[s.index]?.why, s.tool].filter(Boolean).join(' · ')}>{stepLabel(s)}</span>
                <span className="sum">
                  {stepSummary(s)}
                  <span className="prompt-sr-only"> {s.status}</span>
                </span>
                {s.status === 'running' && (
                  <div className={`prompt-step-bar${typeof s.progress === 'number' ? '' : ' indeterminate'}`}
                       role="progressbar" aria-valuemin={0} aria-valuemax={100}
                       aria-valuenow={typeof s.progress === 'number' ? Math.round(s.progress * 100) : undefined}>
                    <span style={{ transform: typeof s.progress === 'number' ? `scaleX(${Math.max(0.02, Math.min(1, s.progress))})` : undefined }} />
                  </div>
                )}
              </li>
            )
          })}
        </ol>
      )}

      {steps.length === 0 && busy && (
        <div className="prompt-step is-running is-placeholder">
          <span className="g" aria-hidden="true">…</span>
          <span className="sum">{status === 'planning' ? 'planning' : 'starting'}</span>
        </div>
      )}

      {verify && (
        <div className="prompt-verify">
          <div className="prompt-verify-head">
            <span>Verified</span>
            <b>{verify.passed} of {verify.total}</b>
            <span className="rendered">{verify.rendered ? 'measured on a 360p render' : 'measured on the timeline'}</span>
          </div>
          <div className="prompt-checks">
            {shownChecks(verify.checks).map((c, i) => <CheckRow key={`${i}-${c.check}`} c={c} />)}
          </div>
        </div>
      )}

      {reply && <p className="prompt-reply">{editorProse(reply)}</p>}
      {!busy && <CreatedProjects items={projects} />}
      {/* QA-064: a cancel is the user's own choice — a neutral line, never a
          red "failed". While the current step finishes, say it is stopping. */}
      {cancelling && busy && (
        <div className="prompt-notice" role="status">Stopping after the current step — nothing it did will be kept.</div>
      )}
      {lastError && status === 'cancelled' && <div className="prompt-notice" role="status">{lastError}</div>}
      {lastError && status !== 'cancelled' && <div className="prompt-error" role="alert">{lastError}</div>}
      {connectionDropped && (
        <div className="prompt-notice">
          The connection dropped — the run continues on the Mac. {reconnecting ? 'Reconnecting…' : 'Reload to see how it ended.'}
        </div>
      )}

      <div className="prompt-log-actions">
        {canUndo && !busy && (
          <button type="button" onClick={() => void dispatch('undo')} title="Undo the whole prompt (one history step)">Undo</button>
        )}
        {plan && (
          <button type="button" onClick={() => void copyPlan()}>{copied ? 'Copied' : 'Copy plan'}</button>
        )}
        <span className="spacer" />
        {status === 'done' && (
          <button type="button" className="ghost" aria-expanded={true} onClick={() => setOpenFor(null)}>Fold</button>
        )}
        <button type="button" className="ghost" onClick={dismiss}>{busy ? 'Hide' : 'Clear'}</button>
      </div>
    </section>
  )
}

/** One Open button per project the run created (QA-068) — the reply used to
 *  list raw `s_…` ids with no way to get to them from here. */
function CreatedProjects({ items }: { items: readonly ChildRun[] }) {
  const openSession = useStore((s) => s.openSession)
  const current = useStore((s) => s.sessionId)
  if (!items.length) return null
  const open = (id: string, name: string) => {
    openSession(id).catch((e) => toast.error(`Couldn't open “${name}”: ${errorMessage(e)}`))
  }
  return (
    <ul className="prompt-projects" aria-label="New projects">
      {items.map((c, i) => {
        const name = c.name ?? `Short ${i + 1}`
        const here = c.session === current
        return (
          <li key={c.session}>
            <span className="name">{name}</span>
            {c.status === 'failed' && <span className="note">not finished</span>}
            <button type="button" disabled={here} aria-label={here ? `${name} is open` : `Open ${name}`}
                    onClick={() => open(c.session, name)}>{here ? 'Open now' : 'Open'}</button>
          </li>
        )
      })}
    </ul>
  )
}

function CheckRow({ c }: { c: VerifyCheck }) {
  const state = c.pass === true ? 'pass' : c.pass === false ? 'fail' : 'skip'
  const g: IconName = c.pass === true ? 'check' : c.pass === false ? 'close' : 'minus'
  const label = c.pass === true ? 'passed' : c.pass === false ? 'failed' : 'not measured'
  // Prose, never JSON (lib/checkProse): one wrapping line under the label.
  const values = checkValues(c)
  return (
    <div className={`prompt-check is-${state}`} role="row">
      <span className="g" aria-hidden="true"><Icon name={g} /></span>
      <span className="human" title={c.check}>
        {c.human || c.check.replace(/_/g, ' ')}
        {c.headline === false && <small>info</small>}
        {c.pass === false && c.blocking === false && <small>advisory · the edit was kept</small>}
        <span className="prompt-sr-only"> {label}</span>
      </span>
      {values && <span className="vals">{values}</span>}
      {c.detail && <div className="prompt-check-detail">{c.detail}</div>}
    </div>
  )
}
