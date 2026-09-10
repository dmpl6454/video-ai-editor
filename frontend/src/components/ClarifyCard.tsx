// One clarification, answered from the keyboard.
//
// The planner asks at most a handful of things (spec §2.7): a caption
// language, a brand handle, a music file, and the two gates — `downloads`
// (nothing in the prompt path fetches a model without a yes, §1.4) and `go`
// (a run over 90 s asks first). Every question arrives with the planner's
// own default where it has one, so the card opens with those selected and
// Enter runs it unchanged. Focus moves INTO the card when it appears and
// back to the input when it leaves (PromptBar owns that half).
//
// Presentational on purpose: `onSubmit` gets the answers as typed/picked and
// the caller coerces them (lib/clarifyDefaults.answersPayload). ChatOverlay
// renders the same card for a `clarify` that arrives on the chat stream, so
// the card must not reach into the prompt store.
//
// GATES ARRIVE ONE AT A TIME. When the first blocking question is a gate — a
// `confirm` (`downloads`, `go`) or a feature gate (`gate_<tool>`) — the card
// shows only it; the backend re-pauses with whatever is still unanswered
// (planner.apply_answers keeps the rest), so "download 3 GB?" and "auto
// captions is unavailable — continue without it?" never stack into one
// contradictory card. Option hints (the technical remedy) sit behind "Why?".

import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { coerceAnswer, defaultAnswers, isGate, kindOf, missingRequired, optionsFor, totalDownloadBytes,
         visibleQuestions, type Answers } from '../lib/clarifyDefaults'
import { humanBytes, humanDuration, type NeedsInput, type Plan } from '../lib/promptEvents'

interface Props {
  questions: NeedsInput[]
  plan?: Plan | null
  busy?: boolean
  onSubmit: (answers: Answers) => void
  onCancel: () => void
}

export function ClarifyCard({ questions: allQuestions, plan, busy = false, onSubmit, onCancel }: Props) {
  const questions = useMemo(() => visibleQuestions(allQuestions), [allQuestions])
  const [answers, setAnswers] = useState<Answers>(() => defaultAnswers(questions))
  const [touchedBad, setTouchedBad] = useState<string | null>(null)
  const firstRef = useRef<HTMLElement | null>(null)
  const rootRef = useRef<HTMLDivElement>(null)

  useEffect(() => { firstRef.current?.focus() }, [])

  const missing = useMemo(() => missingRequired(questions, answers), [questions, answers])
  // A gate card is one question — a `confirm` or a feature gate (`gate_<tool>`,
  // choice kind with two outcome options): two buttons and the facts.
  const gate = questions.length === 1 && isGate(questions[0]) && optionsFor(questions[0]).length === 2 ? questions[0] : null
  const [why, setWhy] = useState(false)
  const remaining = allQuestions.length - questions.length
  const downloadBytes = totalDownloadBytes(plan?.downloads_needed)

  const submit = () => {
    if (busy || missing.length) return
    // Refuse a typed value that does not coerce (e.g. "many" for a count)
    // here, with the field marked, rather than letting the store throw.
    for (const q of questions) {
      const v = answers[q.key]
      if (v === undefined || v === null || v === '') continue
      if (coerceAnswer(q, v) === null) { setTouchedBad(q.key); return }
    }
    onSubmit(answers)
  }

  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); onCancel(); return }
    if (e.key === 'Enter' && !e.shiftKey) {
      const tag = (e.target as HTMLElement).tagName
      if (tag === 'TEXTAREA') return
      e.preventDefault()
      submit()
    }
  }

  const setAnswer = (key: string, value: unknown) => {
    setTouchedBad(null)
    setAnswers((a) => ({ ...a, [key]: value }))
  }

  if (gate) {
    const opts = optionsFor(gate)
    const yes = opts[0]
    const no = opts[1]
    const facts: string[] = []
    if (gate.key === 'downloads' && downloadBytes > 0) facts.push(`${humanBytes(downloadBytes)} to download once`)
    if (typeof plan?.estimated_seconds === 'number') facts.push(humanDuration(plan.estimated_seconds))
    return (
      <div className="clarify" ref={rootRef} onKeyDown={onKey} role="group" aria-label="Before running">
        <div className="clarify-head">
          <span className="clarify-kicker">Before running</span>
          {facts.length > 0 && <span className="clarify-sub">{facts.join(' · ')}</span>}
        </div>
        <p className="clarify-question">{gate.question}</p>
        {(yes.hint || no.hint) && (
          <div className="clarify-why">
            <button type="button" className="ghost" aria-expanded={why} onClick={() => setWhy((v) => !v)}>Why?</button>
            {why && (
              <ul>
                {yes.hint && <li><b>{yes.label}</b> — {yes.hint}</li>}
                {no.hint && <li><b>{no.label}</b> — {no.hint}</li>}
              </ul>
            )}
          </div>
        )}
        {remaining > 0 && <span className="clarify-sub">{remaining} more question{remaining === 1 ? '' : 's'} after this</span>}
        {gate.key === 'downloads' && plan?.downloads_needed && plan.downloads_needed.length > 0 && (
          <ul className="clarify-dl">
            {plan.downloads_needed.map((d) => (
              <li key={d.what + d.tool}>{d.what} — {humanBytes(d.bytes)} <span>(for {d.tool})</span></li>
            ))}
          </ul>
        )}
        <div className="clarify-actions">
          <button
            type="button"
            className="primary"
            ref={(el) => { firstRef.current = el }}
            disabled={busy}
            onClick={() => { setAnswer(gate.key, yes.value); onSubmit({ ...answers, [gate.key]: yes.value }) }}
          >
            {yes.label}<kbd>↵</kbd>
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => { setAnswer(gate.key, no.value); onSubmit({ ...answers, [gate.key]: no.value }) }}
          >
            {no.label}
          </button>
          <button type="button" className="ghost" disabled={busy} onClick={onCancel}>
            Cancel<kbd>Esc</kbd>
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="clarify" ref={rootRef} onKeyDown={onKey} role="group" aria-label="One question before running">
      <div className="clarify-head">
        <span className="clarify-kicker">{questions.length === 1 ? 'One question' : `${questions.length} questions`}</span>
        <span className="clarify-sub">Defaults are pre-selected — Enter runs, Esc drops the question</span>
      </div>
      <div className="clarify-grid">
        {questions.map((q, i) => (
          <Question
            key={q.key}
            q={q}
            value={answers[q.key]}
            invalid={touchedBad === q.key}
            firstRef={i === 0 ? firstRef : undefined}
            onChange={(v) => setAnswer(q.key, v)}
          />
        ))}
      </div>
      <div className="clarify-actions">
        <button type="button" className="primary" disabled={busy || missing.length > 0} onClick={submit}>
          Run<kbd>↵</kbd>
        </button>
        <button type="button" disabled={busy} onClick={onCancel}>Cancel<kbd>Esc</kbd></button>
        {missing.length > 0 && <span className="err">Needed: {missing.join(', ')}</span>}
        {touchedBad && <span className="err">That is not a valid {kindOf(questions.find((q) => q.key === touchedBad)!)}</span>}
      </div>
    </div>
  )
}

interface QProps {
  q: NeedsInput
  value: unknown
  invalid: boolean
  firstRef?: React.MutableRefObject<HTMLElement | null>
  onChange: (v: unknown) => void
}

function Question({ q, value, invalid, firstRef, onChange }: QProps) {
  const id = useId()
  const kind = kindOf(q)
  const opts = optionsFor(q)

  if (kind === 'choice' || kind === 'confirm' || (kind === 'path' && opts.length > 0)) {
    return <Chips q={q} opts={opts} value={value} firstRef={firstRef} onChange={onChange} label={kind === 'path' ? 'Offered files' : undefined} />
  }
  if (kind === 'number' || kind === 'duration') {
    const range = [
      typeof q.min === 'number' ? `min ${q.min}` : null,
      typeof q.max === 'number' ? `max ${q.max}` : null,
    ].filter(Boolean).join(' · ')
    return (
      <div className="clarify-q">
        <label className="q" htmlFor={id}>{q.question}</label>
        <div className="clarify-field">
          <input
            id={id}
            ref={(el) => { if (firstRef) firstRef.current = el }}
            type={kind === 'number' ? 'number' : 'text'}
            inputMode="decimal"
            min={q.min}
            max={q.max}
            step={kind === 'number' ? 1 : undefined}
            value={value === undefined || value === null ? '' : String(value)}
            placeholder={kind === 'duration' ? '30s · 1m · 1:30' : ''}
            aria-invalid={invalid || undefined}
            aria-describedby={range ? `${id}-range` : undefined}
            onChange={(e) => onChange(e.target.value)}
          />
          {q.unit && <span className="unit">{q.unit}</span>}
          {range && <span className="range" id={`${id}-range`}>{range}</span>}
        </div>
      </div>
    )
  }
  return (
    <div className="clarify-q">
      <label className="q" htmlFor={id}>{q.question}</label>
      <input
        id={id}
        ref={(el) => { if (firstRef) firstRef.current = el }}
        className="clarify-text"
        type="text"
        value={value === undefined || value === null ? '' : String(value)}
        placeholder={typeof q.default === 'string' ? q.default : ''}
        aria-invalid={invalid || undefined}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  )
}

interface ChipsProps {
  q: NeedsInput
  opts: { value: unknown; label: string; hint?: string }[]
  value: unknown
  label?: string
  firstRef?: React.MutableRefObject<HTMLElement | null>
  onChange: (v: unknown) => void
}

/**
 * Segmented chips as a radiogroup with roving tabindex (the LeftPane tab
 * strip pattern): one tab stop, arrows move and select, Enter is left to the
 * card so it runs. The default option is marked and pre-selected.
 */
function Chips({ q, opts, value, label, firstRef, onChange }: ChipsProps) {
  const refs = useRef<(HTMLButtonElement | null)[]>([])
  const selected = opts.findIndex((o) => Object.is(o.value, value) || String(o.value) === String(value))
  const focusIdx = selected >= 0 ? selected : 0

  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const i = selected >= 0 ? selected : 0
    const next =
      e.key === 'ArrowRight' || e.key === 'ArrowDown' ? (i + 1) % opts.length
      : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? (i - 1 + opts.length) % opts.length
      : e.key === 'Home' ? 0
      : e.key === 'End' ? opts.length - 1
      : -1
    if (next < 0) return
    e.preventDefault()
    onChange(opts[next].value)
    refs.current[next]?.focus()
  }

  return (
    <fieldset className="clarify-q">
      <legend>{q.question}</legend>
      {label && <div className="clarify-sub clarify-sub-gap">{label}</div>}
      <div className="clarify-chips" role="radiogroup" aria-label={q.question} onKeyDown={onKey}>
        {opts.map((o, i) => {
          const isDefault = q.default !== undefined && q.default !== null
            && (Object.is(o.value, q.default) || String(o.value) === String(q.default))
          return (
            <button
              key={String(o.value)}
              type="button"
              role="radio"
              aria-checked={i === selected}
              tabIndex={i === focusIdx ? 0 : -1}
              className={`clarify-chip${isDefault ? ' is-default' : ''}`}
              title={o.hint}
              ref={(el) => {
                refs.current[i] = el
                if (firstRef && i === focusIdx) firstRef.current = el
              }}
              onClick={() => onChange(o.value)}
            >
              {o.label}
              {o.hint && <small>{o.hint}</small>}
            </button>
          )
        })}
      </div>
    </fieldset>
  )
}
