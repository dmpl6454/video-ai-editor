// The catalogue frame every browsing tab shares (design §2a "Catalogues"):
// a search field and a filter button over tiles, and the honest states the
// brief asks for — skeleton while loading, an error contained in the panel
// with "Click to try again", an empty message, and an "unavailable" card for
// a feature this build does not ship (never a dead control).
import { useState, type ReactNode } from 'react'
import { Icon } from '../Icon'
import { PanelState, SkeletonGrid } from '../ui/Skeleton'

export function CatalogueFrame({ name, children, onFilter, query, onQuery, actions }: {
  name: string
  children: ReactNode
  onFilter?: () => void
  query?: string
  onQuery?: (q: string) => void
  actions?: ReactNode
}) {
  const [local, setLocal] = useState('')
  const q = query ?? local
  const set = onQuery ?? setLocal
  return (
    <div className="ab-catalogue">
      <div className="ab-toolbar">
        <label className="ui-search ab-search">
          <Icon name="search" />
          <input value={q} onChange={(e) => set(e.target.value)} placeholder={`Search ${name}`} aria-label={`Search ${name}`} />
        </label>
        {actions}
        <button type="button" className="ui-icon-btn is-raised" aria-label="Filter" title={onFilter ? 'Filter' : 'No filters for this catalogue yet'}
                disabled={!onFilter} onClick={onFilter}><Icon name="filter" /></button>
      </div>
      {children}
    </div>
  )
}

export function Unavailable({ title, body, action, actionLabel }: { title: string; body: string; action?: () => void; actionLabel?: string }) {
  return <PanelState kind="unavailable" title={title} body={body} action={action} actionLabel={actionLabel} />
}

export { PanelState, SkeletonGrid }

/** A simple tile with a gradient placeholder or an icon, and a label. */
export function Tile({ label, onClick, selected, children, hue, title, disabled, aspect }: {
  label: string
  onClick?: () => void
  selected?: boolean
  children?: ReactNode
  hue?: number
  title?: string
  disabled?: boolean
  aspect?: string
}) {
  const bg = hue != null ? `linear-gradient(135deg, hsl(${hue} 40% 28%), hsl(${(hue + 30) % 360} 35% 16%))` : undefined
  return (
    <button type="button" className={`ui-tile${selected ? ' is-selected' : ''}`} onClick={onClick} title={title ?? label} disabled={disabled}>
      <span className="ui-tile-art" style={{ background: bg, aspectRatio: aspect }}>{children}</span>
      <span className="ui-tile-label">{label}</span>
    </button>
  )
}
