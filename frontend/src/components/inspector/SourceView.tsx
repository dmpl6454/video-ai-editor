// Source (design §2c "Source"): the previewed asset's name, kind, duration and
// the hint — the file is not on the timeline, add it to edit it.
import { useInspector } from './inspectorStore'
import { clockDuration } from '../../lib/mediaLibrary'
import { appendToTimeline } from '../../lib/mediaTabLib'
import { Icon } from '../Icon'

export function SourceView() {
  const a = useInspector((s) => s.preview)
  const previewAsset = useInspector((s) => s.previewAsset)
  if (!a) return null
  const kind = a.still ? 'Photo' : a.kind === 'audio' ? 'Audio' : `Video${a.width && a.height ? ` · ${a.width} × ${a.height}` : ''}`
  return (
    <div className="in in-source">
      <div className="ed-panel-head"><span>Source</span><span className="ed-grow" />
        <button type="button" className="ui-icon-btn is-small" aria-label="Close source preview" title="Back to the timeline" onClick={() => previewAsset(null)}><Icon name="close" /></button>
      </div>
      <div className="in-body in-source-body">
        <span className="in-source-name">{a.name}</span>
        <span className="ui-muted">{kind}</span>
        <span className="ui-muted" style={{ fontVariantNumeric: 'tabular-nums' }}>{a.still ? '5 s as a clip' : clockDuration(a.duration) || '—'}</span>
        <span className="ui-faint" style={{ marginTop: 6 }}>Previewing the source file. Add it to the timeline to edit.</span>
        <button type="button" className="ui-btn-primary ab-self-start" style={{ marginTop: 8 }}
                onClick={() => void appendToTimeline({ id: a.id, src: a.src, name: a.name, kind: a.kind, duration: a.duration, width: a.width, height: a.height, uses: 0, clipIds: [], missing: false, still: a.still })}>
          <Icon name="plus" />Add to timeline
        </button>
      </div>
    </div>
  )
}
