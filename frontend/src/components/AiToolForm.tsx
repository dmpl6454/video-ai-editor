import { useEffect, type KeyboardEvent } from 'react'
import { isMediaClip, isTextClip, type EDL } from '../types'
import { baseName } from '../lib/paths'
import { useGuideRects } from '../lib/guideRects'
import { useAiRuns } from '../lib/aiRuns'
import type { Field } from '../lib/schemaForm'

// Generated form for one tool: one control per lib/schemaForm Field. Values
// are kept as the user typed them (strings, a File, a 4-string bbox) and only
// buildArgs coerces — so a half-typed number never snaps to NaN under the caret.
//
// Keyboard: the keymap engine normally lets Space win over a focused
// checkbox/button (play/pause beats a "click"), which made every checkbox and
// button here unreachable by keyboard. AiPanel marks its root
// `data-keymap-ignore` (keymap/engine.ts honours it), so inside the panel a
// focused control keeps its own keys — Space toggles the box, presses Run.

interface Props {
  tool: string; label: string; fields: Field[]
  values: Record<string, unknown>; errors: Record<string, string>
  disabled: boolean; edl: EDL | null; playhead: number
  onChange: (name: string, value: unknown) => void
  onSubmit: () => void
}

const submitOnEnter = (onSubmit: () => void) => (e: KeyboardEvent<HTMLInputElement>) => {
  if (e.key === 'Enter') { e.preventDefault(); onSubmit() }
}

// A blank required time almost always means no marks are set yet — say so
// instead of a bare "Required".
function errorText(f: Field, err: string | undefined): string | undefined {
  if (err === 'Required' && (f.defaultFrom === 'inMark' || f.defaultFrom === 'outMark')) {
    return 'Set In/Out marks (I / O) first'
  }
  return err
}

function clipOptions(edl: EDL | null, filter: 'overlay' | 'video'): { id: string; label: string }[] {
  if (!edl) return []
  const out: { id: string; label: string }[] = []
  for (const t of edl.tracks) {
    if (filter === 'overlay') {
      // Not the captions track: its cues are not addressable clips and would
      // 400 inside motion_track.
      if (t.type !== 'text' && t.type !== 'sticker') continue
      for (const c of t.clips) {
        out.push({ id: c.id, label: isTextClip(c) ? c.text : ((c as { label?: string | null }).label ?? c.id) })
      }
    } else if (t.type === 'video') {
      for (const c of t.clips) {
        if (isMediaClip(c)) out.push({ id: c.id, label: `${t.id} · ${baseName(c.src)} @ ${c.start.toFixed(1)}s` })
      }
    }
  }
  return out
}

// Four inputs plus a live rectangle on the preview (lib/guideRects.ts, drawn
// by SafeZones.tsx): typed 0..1 fractions are unusable blind.
function BboxField({ id, tool, label, value, disabled, errId, onChange }: {
  id: string; tool: string; label: string; value: unknown; disabled: boolean
  errId?: string; onChange: (v: string[]) => void
}) {
  const arr = Array.isArray(value) ? value.map((x) => String(x ?? '')) : ['', '', '', '']
  const key = arr.join(',')
  // LeftPane HIDES the AI tab rather than unmounting it, so the unmount
  // cleanup below never ran on a switch to Media and the rectangle stayed
  // drawn over the footage with no form anywhere to explain it — the exact
  // state that is worse than no box at all. The panel's visibility is part
  // of the effect: hidden → cleared, shown again → republished as-is.
  const visible = useAiRuns((s) => s.panelVisible)
  useEffect(() => {
    const parts = key.split(',')
    const nums = parts.map((s) => Number(s.trim()))
    const guideId = `ai:${tool}`
    const complete = parts.length === 4 && parts.every((s) => s.trim() !== '') && nums.every(Number.isFinite)
    if (visible && complete) {
      useGuideRects.getState().set(guideId, { rect: { x: nums[0], y: nums[1], w: nums[2], h: nums[3] }, label })
    } else {
      useGuideRects.getState().clear(guideId)
    }
  }, [key, tool, label, visible])
  // Collapse / unmount takes the box off the picture too.
  useEffect(() => () => useGuideRects.getState().clear(`ai:${tool}`), [tool])
  return (
    <div className="ai-bbox" role="group" aria-labelledby={`${id}-label`}>
      {(['x', 'y', 'w', 'h'] as const).map((k, i) => (
        <label key={k} className="ai-bbox-cell">
          <span>{k}</span>
          <input id={i === 0 ? id : undefined} type="number" step="0.01" min={0} max={1} value={arr[i]}
                 disabled={disabled} aria-describedby={errId}
                 onChange={(e) => onChange([...arr.slice(0, i), e.target.value, ...arr.slice(i + 1)])} />
        </label>
      ))}
    </div>
  )
}

export function AiToolForm({ tool, label, fields, values, errors, disabled, edl, playhead, onChange, onSubmit }: Props) {
  const onEnter = submitOnEnter(onSubmit)

  const control = (f: Field, id: string, errId: string | undefined) => {
    const v = values[f.name]
    const isNull = f.nullable && v === null
    const off = disabled || !!isNull
    const text = isNull || v === undefined ? '' : String(v)
    const common = { id, disabled: off, 'aria-describedby': errId, 'aria-invalid': errId ? true : undefined }
    switch (f.widget) {
      case 'checkbox':
        return <input {...common} type="checkbox" checked={!!v} onChange={(e) => onChange(f.name, e.target.checked)} />
      case 'number':
        return <input {...common} type="number" step={f.integer ? 1 : 'any'} min={f.min} max={f.max} value={text}
                      onChange={(e) => onChange(f.name, e.target.value)} onKeyDown={onEnter} />
      case 'time':
        return (
          <div className="ai-time">
            <input {...common} type="number" step="any" min={0} value={text}
                   onChange={(e) => onChange(f.name, e.target.value)} onKeyDown={onEnter} />
            <button type="button" disabled={off} title="Use the current playhead time"
                    onClick={() => onChange(f.name, Math.round(playhead * 1000) / 1000)}>◀ playhead</button>
          </div>
        )
      case 'select': {
        // A required select is seeded to its first option (lib/schemaForm
        // seedValue), so the blank row only exists where '' is a real choice.
        const blank = !f.required && (f.default === undefined || f.default === '')
        return (
          <select {...common} value={text} onChange={(e) => onChange(f.name, e.target.value)}>
            {blank && <option value="">—</option>}
            {(f.options ?? []).map((o) => <option key={String(o)} value={String(o)}>{String(o)}</option>)}
          </select>
        )
      }
      case 'clipSelect': {
        const opts = clipOptions(edl, f.clipFilter ?? 'video')
        return (
          <select {...common} value={text} onChange={(e) => onChange(f.name, e.target.value)}>
            <option value="">{opts.length ? '— choose —' : `no ${f.clipFilter === 'overlay' ? 'sticker or text overlays' : 'video clips'} on the timeline`}</option>
            {opts.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
          </select>
        )
      }
      case 'list':
        return <textarea {...common} rows={2} value={text} placeholder="One per line, or comma-separated"
                         onChange={(e) => onChange(f.name, e.target.value)} />
      case 'mapping':
        return <textarea {...common} rows={3} value={text} placeholder={'KEY=VALUE\none per line'}
                         onChange={(e) => onChange(f.name, e.target.value)} />
      case 'bbox':
        return <BboxField id={id} tool={tool} label={`${label} box`} value={v} disabled={off} errId={errId}
                          onChange={(arr) => onChange(f.name, arr)} />
      case 'path':
        // No open-file dialog exists in the packaged bridge (desktop.py::_Api).
        return <input {...common} type="text" value={text} placeholder="Absolute path on this machine"
                      onChange={(e) => onChange(f.name, e.target.value)} onKeyDown={onEnter} />
      case 'file':
        return <input {...common} type="file" accept={f.accept}
                      onChange={(e) => onChange(f.name, e.target.files?.[0] ?? '')} />
      default:
        return <input {...common} type="text" value={text}
                      onChange={(e) => onChange(f.name, e.target.value)} onKeyDown={onEnter} />
    }
  }

  return (
    <div className="ai-form">
      {fields.map((f) => {
        const id = `${tool}-${f.name}`
        const err = errorText(f, errors[f.name])
        const errId = err ? `${id}-err` : undefined
        const caption = <>{f.label}{f.required && <em aria-hidden="true">*</em>}</>
        const labelEl = f.widget === 'bbox'
          ? <span id={`${id}-label`} className="ai-label">{caption}</span>
          : <label htmlFor={id} className="ai-label">{caption}</label>
        return (
          <div key={f.name} className="ai-field">
            {/* A checkbox sits on one row with its label, in DOM order (box,
                then label); help / error text wraps underneath at full width
                like every other field's. */}
            {f.widget === 'checkbox'
              ? <div className="ai-check-row">{control(f, id, errId)}{labelEl}</div>
              : <>{labelEl}{control(f, id, errId)}</>}
            {f.nullable && (
              <label className="ai-check">
                <input type="checkbox" checked={values[f.name] === null} disabled={disabled}
                       onChange={(e) => onChange(f.name, e.target.checked ? null : (f.default ?? ''))} />
                {f.nullable.label}
              </label>
            )}
            {f.description && !err && <span className="ai-field-help">{f.description}</span>}
            {err && <span id={errId} className="ai-field-err" role="alert">{err}</span>}
          </div>
        )
      })}
    </div>
  )
}
