// The segmented control of the design: a `rgba(118,118,128,.24)` track with
// r8 and 2 px padding, 24 px options, the active one `#636366`. A radiogroup:
// ←/→ move the choice, so it is one Tab stop.
import type { KeyboardEvent } from 'react'

export function Segmented<T extends string>({ options, value, onChange, label, size = 'normal', className, disabled }: {
  options: readonly { id: T; label: string; title?: string; disabled?: boolean }[]
  value: T
  onChange: (id: T) => void
  label: string
  size?: 'normal' | 'small'
  className?: string
  disabled?: boolean
}) {
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight' && e.key !== 'Home' && e.key !== 'End') return
    e.preventDefault()
    const i = options.findIndex((o) => o.id === value)
    const n = options.length
    const next = e.key === 'Home' ? 0 : e.key === 'End' ? n - 1
      : e.key === 'ArrowRight' ? (i + 1) % n : (i - 1 + n) % n
    onChange(options[next].id)
    const el = (e.currentTarget.querySelectorAll('[role=radio]')[next] as HTMLElement | undefined)
    el?.focus()
  }
  return (
    <div className={`ui-seg${size === 'small' ? ' ui-seg-small' : ''}${className ? ` ${className}` : ''}`}
         role="radiogroup" aria-label={label} onKeyDown={onKey}>
      {options.map((o) => (
        <button key={o.id} type="button" role="radio" aria-checked={value === o.id} title={o.title}
                tabIndex={value === o.id ? 0 : -1} disabled={disabled || o.disabled}
                className={`ui-seg-opt${value === o.id ? ' is-active' : ''}`}
                onClick={() => onChange(o.id)}>{o.label}</button>
      ))}
    </div>
  )
}
