// Templates (design §2a; brief §2 table): the built-in show templates
// (`apply_template`: outfit_breakdown, tech_tip, explainer — hook + caption
// style + labels) and the shows saved from this project (`apply_show_template`).
import { useEffect, useState } from 'react'
import { api } from '../../api'
import { errorMessage, useStore } from '../../store'
import { toast } from '../../toast'
import { CatalogueFrame, PanelState, SkeletonGrid, Tile } from './Catalogue'

const BUILT_IN: Record<string, { label: string; hue: number; title: string }> = {
  outfit_breakdown: { label: 'Outfit breakdown', hue: 330, title: 'Hook, chunky captions and item labels for a fashion breakdown' },
  tech_tip: { label: 'Tech tip', hue: 210, title: 'Hook, clean captions and step labels for a quick tip' },
  explainer: { label: 'Explainer', hue: 150, title: 'Hook, captions and lower-third labels for an explainer' },
}

export function TemplatesTab({ sub }: { sub: string }) {
  const sid = useStore((s) => s.sessionId)
  const hasVideo = useStore((s) => !!s.edl?.duration)
  const dispatch = useStore((s) => s.dispatch)
  const [list, setList] = useState<{ templates: string[]; shows: string[] } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tick, setTick] = useState(0)
  const [query, setQuery] = useState('')
  useEffect(() => {
    if (!sid) return
    let live = true
    api.dispatch<{ templates?: string[]; shows?: string[] }>(sid, 'list_templates', {})
      .then((r) => { if (live) { setError(null); setList({ templates: r.result?.templates ?? Object.keys(BUILT_IN), shows: r.result?.shows ?? [] }) } })
      .catch((e) => { if (live) { setList(null); setError(errorMessage(e)) } })
    return () => { live = false }
  }, [sid, tick])

  if (sub === 'Favorites') {
    return <PanelState kind="empty" title="No favourite templates yet" body="Favourites are not part of this build yet — every template is under Templates." />
  }
  const q = query.trim().toLowerCase()
  const templates = (list?.templates ?? []).filter((t) => !q || (BUILT_IN[t]?.label ?? t).toLowerCase().includes(q))
  const shows = (list?.shows ?? []).filter((t) => !q || t.toLowerCase().includes(q))
  return (
    <CatalogueFrame name="templates" query={query} onQuery={setQuery}>
      {error && <PanelState kind="error" title="Couldn't load the templates" body={error} action={() => setTick((n) => n + 1)} actionLabel="Click to try again" />}
      {!error && !list && <SkeletonGrid count={6} />}
      {list && (
        <div className="ab-stack">
          <div className="ui-tiles">
            {templates.map((t) => {
              const b = BUILT_IN[t] ?? { label: t, hue: 40, title: t }
              return <Tile key={t} label={b.label} hue={b.hue} title={hasVideo ? b.title : 'Add a video to the timeline first'} disabled={!hasVideo}
                           onClick={() => { void dispatch('apply_template', { name: t }).then((r) => { if (r) toast.success(`Applied ${b.label}`) }) }} />
            })}
          </div>
          {shows.length > 0 && (
            <>
              <span className="ui-heading">Saved shows</span>
              <div className="ui-tiles">
                {shows.map((s) => <Tile key={s} label={s} hue={280} title={`Re-apply the saved show “${s}”`} disabled={!hasVideo}
                                        onClick={() => { void dispatch('apply_show_template', { name: s }) }} />)}
              </div>
            </>
          )}
          {templates.length === 0 && shows.length === 0 && <PanelState kind="empty" title={q ? `Nothing matches “${query}”` : 'No templates'} />}
        </div>
      )}
    </CatalogueFrame>
  )
}
