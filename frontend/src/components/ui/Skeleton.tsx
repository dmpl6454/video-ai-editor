// Loading / empty / error states for a catalogue (design §2a "Catalogues",
// brief §2 "Catalogue loading failures must be contained inside the affected
// panel with Reload or Click to try again").
import { Icon } from '../Icon'

export function SkeletonGrid({ count = 8, aspect = '1', min = 96 }: { count?: number; aspect?: string; min?: number }) {
  return (
    <div className="ui-tiles" style={{ gridTemplateColumns: `repeat(auto-fill, minmax(${min}px, 1fr))` }} aria-hidden="true">
      {Array.from({ length: count }, (_, i) => <div key={i} className="ui-skeleton" style={{ aspectRatio: aspect }} />)}
    </div>
  )
}

export function PanelState({ kind, title, body, action, actionLabel, icon }: {
  kind: 'empty' | 'error' | 'offline' | 'unavailable' | 'loading'
  title: string
  body?: string
  action?: () => void
  actionLabel?: string
  icon?: 'warning' | 'cloudOff' | 'info' | 'loading'
}) {
  const glyph = icon ?? (kind === 'error' ? 'warning' : kind === 'offline' ? 'cloudOff' : kind === 'loading' ? 'loading' : 'info')
  return (
    <div className={`ui-state ui-state-${kind}`} role={kind === 'error' ? 'alert' : 'status'}>
      <span className="ui-state-icon"><Icon name={glyph} size={22} className={glyph === 'loading' ? 'icon-spin' : undefined} /></span>
      <span className="ui-state-title">{title}</span>
      {body && <span className="ui-state-body">{body}</span>}
      {action && <button type="button" className="ui-btn-secondary" onClick={action}>{actionLabel ?? 'Reload'}</button>}
    </div>
  )
}
