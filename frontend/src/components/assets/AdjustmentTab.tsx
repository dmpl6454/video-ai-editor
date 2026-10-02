// Adjustment (design §2a; brief §2 table): the "Custom adjustment" card opens
// the selected clip's Adjustment inspector (or the clip at the playhead);
// LUT imports a .cube; Yours lists the saved adjustment presets.
import { useRef, useState } from 'react'
import { api } from '../../api'
import { errorMessage, useStore } from '../../store'
import { toast } from '../../toast'
import { videoClipUnder } from '../../lib/aiCatalog'
import { lutApplyArgs } from '../../lib/lutActions'
import { readAdjustPresets, type AdjustPreset } from '../../lib/adjustPresets'
import { useInspector } from '../inspector/inspectorStore'
import { Icon } from '../Icon'
import { PanelState, Tile } from './Catalogue'

export function AdjustmentTab({ sub }: { sub: string }) {
  const sid = useStore((s) => s.sessionId)
  const dispatch = useStore((s) => s.dispatch)
  const cube = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)
  const openAdjust = () => {
    const st = useStore.getState()
    const id = st.selection ?? (st.edl ? videoClipUnder(st.edl, st.playhead) : null)
    if (!id) { toast.info('Add a video to the timeline and select a clip to adjust it.'); return }
    st.setSelection(id)
    useInspector.getState().setClipTab('Adjustment')
  }
  const importLut = async (f: File) => {
    if (!sid) return
    setBusy(true)
    try {
      const up = await api.uploadLut(sid, f)
      const st = useStore.getState()
      const id = st.selection ?? (st.edl ? videoClipUnder(st.edl, st.playhead) : null)
      if (!id) { toast.success(`Imported ${f.name} — select a clip and apply it from the inspector's Adjustment tab`); return }
      await dispatch('apply_lut', lutApplyArgs({ src: up.path, intensity: 1, clipId: id }))
    } catch (e) { toast.error(`Couldn't import ${f.name}: ${errorMessage(e)}`) } finally { setBusy(false) }
  }
  switch (sub) {
    case 'Add adjustment':
      return (
        <div className="ui-tiles" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(112px, 1fr))' }}>
          <Tile label="Custom adjustment" aspect="16 / 9" onClick={openAdjust} title="Open the selected clip's colour adjustments">
            <span className="ab-tile-stack"><Icon name="inspector" size={22} /><span className="ui-faint">Custom adjustment</span></span>
          </Tile>
        </div>
      )
    case 'Yours': return <YourPresets onApply={(p) => {
      const st = useStore.getState()
      const id = st.selection ?? (st.edl ? videoClipUnder(st.edl, st.playhead) : null)
      if (!id) { toast.info('Select a clip to apply the preset to.'); return }
      void dispatch('color_grade', { clip_id: id, ...p.params })
    }} />
    case 'LUT':
      return (
        <div className="ab-stack">
          <span className="ui-heading">LUT</span>
          <span className="ui-faint">Apply your own 3D LUT (.cube) to the selected clip. The bundled looks are under Filters.</span>
          <input ref={cube} type="file" accept=".cube" hidden onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ''; if (f) void importLut(f) }} />
          <button type="button" className="ui-btn-primary ab-self-start" disabled={busy || !sid} onClick={() => cube.current?.click()}>
            <Icon name="upload" />{busy ? 'Importing…' : 'Import LUT (.cube)…'}
          </button>
        </div>
      )
    default: return null
  }
}

function YourPresets({ onApply }: { onApply: (p: AdjustPreset) => void }) {
  const [presets] = useState(() => readAdjustPresets())
  if (presets.length === 0) return <PanelState kind="empty" title="No saved adjustments yet" body="Save as preset at the bottom of a clip's Adjustment tab keeps its colour settings here." />
  return (
    <div className="ui-tiles">
      {presets.map((p) => <Tile key={p.id} label={p.name} hue={p.hue} onClick={() => onApply(p)} title={`Apply “${p.name}” to the selected clip`} />)}
    </div>
  )
}
