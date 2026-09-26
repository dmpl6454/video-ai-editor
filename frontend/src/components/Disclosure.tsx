// A native <details> whose marker is the app's chevron (COHERENCE, wave C
// review). A bare <summary> draws the browser's own blue ▶ triangle — a
// second icon approach next to lucide — while every other disclosure in the
// app (Stickers, Effects) shows chevronRight / chevronDown. styles.css hides
// the UA marker globally; this component supplies the icon and tracks open.

import { useState, type ReactNode } from 'react'
import { Icon } from './Icon'

export function Disclosure({ summary, children, className, defaultOpen = false }: {
  summary: ReactNode
  children: ReactNode
  className?: string
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <details className={className ? `disclosure ${className}` : 'disclosure'} open={open}
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}>
      <summary><Icon name={open ? 'chevronDown' : 'chevronRight'} /> {summary}</summary>
      {children}
    </details>
  )
}
