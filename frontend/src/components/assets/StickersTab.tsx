// Stickers (design §2a): the emoji / artwork picker (components/StickerPanel,
// 3,700+ Apple-style tiles, searchable) and, under Yours, the stickers already
// on this timeline.
import { useStore } from '../../store'
import { StickerPanel } from '../StickerPanel'
import { PanelState } from './Catalogue'

export function StickersTab({ sub }: { sub: string }) {
  const edl = useStore((s) => s.edl)
  const selection = useStore((s) => s.selection)
  const setSelection = useStore((s) => s.setSelection)
  if (sub === 'Yours') {
    const mine = (edl?.tracks ?? []).filter((t) => t.type === 'sticker').flatMap((t) => t.clips)
    if (mine.length === 0) return <PanelState kind="empty" title="No stickers on this timeline yet" body="Pick one under Stickers — it lands at the playhead and can be dragged on the Player." />
    return (
      <div className="ab-list">
        {mine.map((c) => {
          const label = (c as unknown as { label?: string | null }).label || 'Sticker'
          return (
            <button key={c.id} type="button" className={`ab-list-row${selection === c.id ? ' is-active' : ''}`} onClick={() => setSelection(c.id)}>
              <span className="ab-list-emoji" aria-hidden="true">{label}</span>
              <span className="ab-list-text">{label}</span>
              <span className="ui-faint">{c.start.toFixed(1)}–{('end' in c ? c.end : c.start).toFixed(1)} s</span>
            </button>
          )
        })}
      </div>
    )
  }
  return <div className="ab-wrapped"><StickerPanel active /></div>
}
