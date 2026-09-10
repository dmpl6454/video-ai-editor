// The record of a run: what was planned, what each step did, what the
// verifier measured, and the honest summary — in that order, because that is
// the order a user reads it to decide whether to keep the edit or undo it.
//
// Nothing here is inferred. Step rows are `step` events (with the tool's own
// summary), `effect:"none"` is shown as "no effect" so a remove_fillers that
// found nothing reads as such before the verifier says so, and the verify
// table prints measured vs expected exactly as the backend reported them.
//
// A step row's primary label is the plan's own `why` ("cut the silent pauses
// on v1") — the sentence the planner wrote for a reader deciding whether to
// keep or undo. The dispatch tool id (`remove_silences`) is the secondary
// tag: a CapCut user should not need the tool vocabulary to read the log.

import { useState } from 'react'
import { useStore } from '../store'
import { toast } from '../toast'
import { usePromptStore, isBusy } from '../lib/promptStore'
import { brainLabel, humanBytes, humanDuration, type Plan, type StepRow, type VerifyCheck } from '../lib/promptEvents'

const pad2 = (n: number) => String(n + 1).padStart(2, '0')

function glyph(s: StepRow): string {
  switch (s.status) {
    case 'ok': return '✓'
    case 'failed': return '✗'
    case 'skipped': return '–'
    default: return '…'
  }
}

// Human names for the few tool ids that can appear without a plan step
// (the verify render, a consented download, a shorts child).
const TOOL_TITLES: Record<string, string> = {
  verify_render: 'Verify render', download: 'Download (you said yes)', finish_short: 'Finish short',
  transcribe: 'Transcribe', auto_caption: 'Auto captions', add_caption_track: 'Captions',
  remove_silences: 'Remove silences', remove_fillers: 'Remove fillers', apply_hook_stack: 'Hook',
  apply_export_preset: 'Export preset', auto_reframe: 'Reframe', set_clip_fit: 'Fill the frame',
  add_music: 'Music', set_duck: 'Duck music', noise_reduce: 'Noise removal',
  set_loudness_target: 'Loudness', add_transition: 'Transitions', apply_lut: 'Colour look',
  audit_aesthetic: 'Aesthetic audit', cut_range: 'Trim', make_shorts: 'Shorts', apply_brand_kit: 'Brand kit',
  tts_voiceover: 'Voiceover', add_lower_third: 'Lower third', add_text: 'Text', apply_text_template: 'End card',
}

function stepLabel(s: StepRow, plan: Plan | null): string {
  const why = plan?.steps?.[s.index]?.why
  if (why && plan?.steps?.[s.index]?.tool === s.tool) return why
  return TOOL_TITLES[s.tool] ?? s.tool.replace(/_/g, ' ')
}

function fmt(v: unknown, unit?: string): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'number') {
    const s = Number.isInteger(v) ? String(v) : v.toFixed(Math.abs(v) < 10 ? 2 : 1)
    return unit ? `${s} ${unit}` : s
  }
  if (typeof v === 'boolean') return v ? 'yes' : 'no'
  if (typeof v === 'string') return v
  if (Array.isArray(v)) return v.map((x) => fmt(x)).join(', ')
  return JSON.stringify(v)
}

export function PromptRunLog() {
  const status = usePromptStore((s) => s.status)
  const plan = usePromptStore((s) => s.plan)
  const prompt = usePromptStore((s) => s.prompt)
  const brain = usePromptStore((s) => s.brain)
  const steps = usePromptStore((s) => s.steps)
  const verify = usePromptStore((s) => s.verify)
  const reply = usePromptStore((s) => s.reply)
  const lastError = usePromptStore((s) => s.lastError)
  const opSeen = usePromptStore((s) => s.opSeen)
  const connectionDropped = usePromptStore((s) => s.connectionDropped)
  const reconnecting = usePromptStore((s) => s.reconnecting)
  const dismiss = usePromptStore((s) => s.dismiss)
  const dispatch = useStore((s) => s.dispatch)
  const [copied, setCopied] = useState(false)

  const busy = isBusy(status)
  const title = plan?.title || plan?.intent || prompt || 'Prompt'
  const via = brain ? (brain.label || brainLabel(brain.brain)) : plan ? brainLabel(plan.brain) : null
  const contentBy = plan?.content_brain && plan.content_brain !== plan.brain ? brainLabel(plan.content_brain) : null
  const downloads = plan?.downloads_needed ?? []
  const eta = typeof plan?.estimated_seconds === 'number' ? plan.estimated_seconds : null
  const headline = verify ? verify.checks.filter((c) => c.headline !== false) : []
  const info = verify ? verify.checks.filter((c) => c.headline === false) : []

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
                <span className="g" aria-hidden="true">{glyph(s)}</span>
                <span className="tool" title={s.tool}>
                  {stepLabel(s, plan)}<code className="prompt-step-id">{s.tool}</code>
                </span>
                <span className="sum">
                  {s.status === 'failed' ? (s.error || 'failed') : s.status === 'skipped' ? `skipped${s.error ? ` — ${s.error}` : ''}` : (s.summary || (s.status === 'running' ? 'running' : ''))}
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
            {[...headline, ...info].map((c) => <CheckRow key={c.check + c.human} c={c} />)}
          </div>
        </div>
      )}

      {reply && <p className="prompt-reply">{reply}</p>}
      {lastError && <div className="prompt-error" role="alert">{lastError}</div>}
      {connectionDropped && (
        <div className="prompt-notice">
          The connection dropped — the run continues on the Mac. {reconnecting ? 'Reconnecting…' : 'Reload to see how it ended.'}
        </div>
      )}

      <div className="prompt-log-actions">
        {opSeen && !busy && (
          <button type="button" onClick={() => void dispatch('undo')} title="Undo the whole prompt (one history step)">Undo</button>
        )}
        {plan && (
          <button type="button" onClick={() => void copyPlan()}>{copied ? 'Copied' : 'Copy plan'}</button>
        )}
        <span className="spacer" />
        <button type="button" className="ghost" onClick={dismiss}>{busy ? 'Hide' : 'Clear'}</button>
      </div>
    </section>
  )
}

function CheckRow({ c }: { c: VerifyCheck }) {
  const state = c.pass === true ? 'pass' : c.pass === false ? 'fail' : 'skip'
  const g = c.pass === true ? '✓' : c.pass === false ? '✗' : '—'
  const label = c.pass === true ? 'passed' : c.pass === false ? 'failed' : 'not measured'
  return (
    <div className={`prompt-check is-${state}`} role="row">
      <span className="g" aria-hidden="true">{g}</span>
      <span className="human" title={c.check}>
        {c.human || c.check.replace(/_/g, ' ')}
        {c.headline === false && <small>info</small>}
        <span className="prompt-sr-only"> {label}</span>
      </span>
      <span className="val" title="measured">{fmt(c.measured, c.unit)}</span>
      <span className="val" title="expected">{c.expected === undefined || c.expected === null ? '' : `expected ${fmt(c.expected, c.unit)}`}</span>
      {c.detail && <div className="prompt-check-detail">{c.detail}</div>}
    </div>
  )
}
