// The Inspector's Mirror / Flip toggles (wave E, lane F4b) — CapCut's
// Transform > Mirror. Two buttons, each a toggle with its state in
// aria-pressed; one press is one `flip_clip` op (one undo step), which
// toggles `Transform.flip_h` / `flip_v` on the clip or sticker. The render
// side (export, server preview, engine, pipDraw / StickerLayer) is lane
// F4a's; "upside down" is a 180° rotation, not a flip (the Rotation slider).
import { Icon } from '../Icon'
import { flipState, type FlipAxis } from '../../lib/flip'
import './transform.css'

type Send = (tool: string, args: Record<string, unknown>) => unknown

const AXES: readonly { axis: FlipAxis; label: string; icon: 'mirrorH' | 'mirrorV'; hint: string }[] = [
  { axis: 'horizontal', label: 'Flip horizontal', icon: 'mirrorH', hint: 'Mirror the picture left to right' },
  { axis: 'vertical', label: 'Flip vertical', icon: 'mirrorV', hint: 'Flip the picture top to bottom' },
]

export function FlipButtons({ clipId, transform, effects, send, disabled = false }: {
  clipId: string
  transform: unknown
  /** The clip's effect chain: a legacy Effects-panel Flip H / V mirrors it too. */
  effects?: readonly { type?: unknown }[] | null
  send: Send
  disabled?: boolean
}) {
  const state = flipState(transform, effects)
  return (
    <div className="flip-buttons" role="group" aria-label="Mirror">
      {AXES.map(({ axis, label, icon, hint }) => {
        const on = axis === 'horizontal' ? state.h : state.v
        return (
          <button key={axis} type="button" className="flip-button" aria-pressed={on} disabled={disabled}
            title={`${label} — ${hint}${on ? ' (on)' : ''}`}
            onClick={() => { void send('flip_clip', { clip_id: clipId, axis }) }}>
            <Icon name={icon} />
            <span>{label}</span>
          </button>
        )
      })}
    </div>
  )
}
