// The inspector's slider row of the design: `70px 1fr 64px 20px` — name,
// range, numeric field (or a read-only value) and the keyframe diamond.
// Commit-on-release through lib/useSliderCommit (one op per gesture, QA-087);
// the readout tracks the thumb live.
import { useEffect, useRef, useState } from 'react'
import { useSliderCommit } from '../../lib/useSliderCommit'
import { KeyDiamond } from './KeyDiamond'

export function SliderRow({ label, min, max, step, value, onChange, onLive, format, unit, dp = 0,
                            editable = true, keyed, onKey, keyTitle, disabled, labelWidth, fieldWidth }: {
  label: string
  min: number
  max: number
  step: number
  value: number
  onChange: (v: number) => void
  onLive?: (v: number) => void
  /** The read-only display when the field is not editable. */
  format?: (v: number) => string
  /** A suffix inside the field ("%"). */
  unit?: string
  dp?: number
  editable?: boolean
  /** The keyframe column: undefined = no diamond, null = dim (not animated),
   *  false = hollow, true = filled. */
  keyed?: boolean | null
  onKey?: () => void
  keyTitle?: string
  disabled?: boolean
  labelWidth?: number
  fieldWidth?: number
}) {
  const [local, setLocal] = useState(value)
  const dragging = useRef(false)
  useEffect(() => { if (!dragging.current) setLocal(value) }, [value])
  const h = useSliderCommit(value, (v) => { dragging.current = false; onChange(v) })
  const [text, setText] = useState<string | null>(null)
  const shown = text ?? local.toFixed(dp)

  const commitText = () => {
    if (text === null) return
    const n = Number(text)
    setText(null)
    if (!Number.isFinite(n)) return
    const v = Math.min(max, Math.max(min, n))
    if (v.toFixed(dp) === value.toFixed(dp)) return
    setLocal(v)
    onChange(v)
  }

  const cols = `${labelWidth ?? 70}px minmax(0, 1fr) ${fieldWidth ?? 64}px${keyed === undefined ? '' : ' 20px'}`
  return (
    <div className={`ui-slider-row${disabled ? ' is-disabled' : ''}`} style={{ gridTemplateColumns: cols }}>
      <span className="ui-slider-name">{label}</span>
      <input type="range" min={min} max={max} step={step} value={local} disabled={disabled}
             aria-label={label} aria-valuetext={format ? format(local) : `${local.toFixed(dp)}${unit ?? ''}`}
             onChange={(e) => {
               const v = Number(e.target.value)
               dragging.current = true
               setLocal(v)
               h.change(v)
               onLive?.(v)
             }}
             onPointerUp={(e) => { dragging.current = false; h.onPointerUp(e) }}
             onPointerCancel={() => { dragging.current = false }}
             onKeyUp={h.onKeyUp}
             onBlur={() => { dragging.current = false; h.onBlur() }} />
      {editable ? (
        <span className="ui-field">
          <input type="number" value={shown} min={min} max={max} step={step} disabled={disabled}
                 aria-label={`${label} value`}
                 onChange={(e) => setText(e.target.value)}
                 onBlur={commitText}
                 onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); e.currentTarget.blur() } }} />
          {unit && <span className="ui-field-unit">{unit}</span>}
        </span>
      ) : (
        <span className="ui-field ui-field-ro">{format ? format(local) : `${local.toFixed(dp)}${unit ?? ''}`}</span>
      )}
      {keyed !== undefined && <KeyDiamond state={keyed} onClick={onKey} title={keyTitle} label={`${label} keyframe`} />}
    </div>
  )
}
