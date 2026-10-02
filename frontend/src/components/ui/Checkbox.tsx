// The 15 px r4 checkbox of the design, drawn with CSS over the native input
// (keyboard, label association and form semantics stay native).
import type { ReactNode } from 'react'

export function Checkbox({ checked, onChange, children, disabled, title, indeterminate }: {
  checked: boolean
  onChange: (on: boolean) => void
  children?: ReactNode
  disabled?: boolean
  title?: string
  indeterminate?: boolean
}) {
  return (
    <label className={`ui-check${disabled ? ' is-disabled' : ''}`} title={title} aria-disabled={disabled || undefined}>
      <input type="checkbox" checked={checked} disabled={disabled}
             ref={(el) => { if (el) el.indeterminate = !!indeterminate }}
             onChange={(e) => onChange(e.target.checked)} />
      {children && <span className="ui-check-label">{children}</span>}
    </label>
  )
}
