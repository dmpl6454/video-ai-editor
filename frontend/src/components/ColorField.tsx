// A colour well that previews while you pick and commits ONE op when the
// picker settles (QA-078). The text inspector's wells were uncontrolled and
// committed only on blur, so the preview stayed white until focus left.

import { useState } from 'react'
import { useIdleCommit } from '../lib/useSliderCommit'

export function ColorField({ value, onCommit, ariaLabel, title }: {
  /** #rrggbb (an alpha suffix is dropped for the well and kept by the caller). */
  value: string
  onCommit: (hex: string) => void
  ariaLabel: string
  title?: string
}) {
  const [local, setLocal] = useState(value)
  // Re-seed when the stored value changes (undo, another client) — derived
  // during render, the React way, rather than in an effect.
  const [seen, setSeen] = useState(value)
  if (seen !== value) { setSeen(value); setLocal(value) }
  const c = useIdleCommit(value, onCommit)
  return (
    <input
      type="color"
      aria-label={ariaLabel}
      title={title}
      value={local}
      onChange={(e) => { setLocal(e.target.value); c.input(e.target.value) }}
      onBlur={c.flush}
      style={{ width: '100%', padding: 0, height: 24 }}
    />
  )
}
