// Project settings (design §4; brief §7): Details · Performance. Details:
// Name (→ rename), Save to (the project folder — this build manages it),
// Imported media (a project policy), Aspect ratio + Resolution + Frame rate
// (→ the canvas, one `set_canvas` on Save: the same state the Player's Ratio
// writes), Color space (Rec. 709 SDR — the one the render pipeline outputs),
// Arrange layers (one-way, with the reference's warning). Performance: the
// Proxy switch (→ the instant-preview engine, which edits from proxies and
// exports from the originals). Cancel discards; Save validates and persists.
import { useEffect, useId, useState } from 'react'
import { useStore } from '../../store'
import { toast } from '../../toast'
import { FRAME_RATES, sameRate } from '../../lib/exportOptions'
import { RATIOS, ratioOf, type RatioId } from '../../lib/playerControls'
import { projectLabel } from '../../lib/projectName'
import { useProjectSettings } from '../../lib/projectSettings'
import { Dialog } from '../Dialog'
import { Segmented } from '../ui/Segmented'
import { Toggle } from '../ui/Toggle'
import { Icon } from '../Icon'
import './dialogs.css'

import { registerOpener } from '../../lib/dialogOpeners'

export function ProjectSettingsDialog() {
  const [open, setOpen] = useState(false)
  useEffect(() => { registerOpener('projectSettings', () => setOpen(true)); return () => registerOpener('projectSettings', null) }, [])
  if (!open) return null
  return <ProjectSettingsForm onClose={() => setOpen(false)} />
}

function ProjectSettingsForm({ onClose }: { onClose: () => void }) {
  const id = useId()
  const edl = useStore((s) => s.edl)
  const sid = useStore((s) => s.sessionId)
  const sessionName = useStore((s) => s.sessionName)
  const dispatch = useStore((s) => s.dispatch)
  const previewEngine = useStore((s) => s.previewEngine)
  const setPreviewEngineSetting = useStore((s) => s.setPreviewEngineSetting)
  const settings = useProjectSettings((s) => s.forSession(sid))
  const save = useProjectSettings((s) => s.save)
  const c = edl?.canvas ?? { w: 1080, h: 1920, fps: 30 }
  const [tab, setTab] = useState<'Details' | 'Performance'>('Details')
  const [name, setName] = useState(projectLabel(sessionName, sid))
  const [policy, setPolicy] = useState(settings.importPolicy)
  const [ratio, setRatio] = useState<RatioId>(ratioOf(c))
  const [res, setRes] = useState<'Adapted' | 'Custom'>(ratioOf(c) === 'Custom' ? 'Custom' : 'Adapted')
  const [w, setW] = useState(String(c.w))
  const [h, setH] = useState(String(c.h))
  const [fps, setFps] = useState(String(FRAME_RATES.find((r) => sameRate(r.fps, c.fps))?.fps ?? c.fps))
  const [arrange, setArrange] = useState(settings.arrangeLayers)
  const [proxy, setProxy] = useState(previewEngine === 'client' || settings.proxy)
  const [saving, setSaving] = useState(false)

  const onSave = async () => {
    if (!sid) return
    setSaving(true)
    try {
      const nextName = name.replace(/\s+/g, ' ').trim()
      if (nextName && nextName !== sessionName) await useStore.getState().renameSession(nextName)
      // The canvas: a named ratio keeps the engine's sizes; Custom takes W × H.
      let nw = c.w, nh = c.h
      if (res === 'Custom') {
        nw = Math.round(Number(w)); nh = Math.round(Number(h))
        if (!Number.isFinite(nw) || !Number.isFinite(nh) || nw < 16 || nh < 16 || nw > 7680 || nh > 7680) {
          toast.error('Width and height must be between 16 and 7680 pixels.'); setSaving(false); return
        }
      } else {
        const r = RATIOS.find((x) => x.id === ratio)
        if (r?.w && r.h) { nw = r.w; nh = r.h }
      }
      const nfps = Number(fps)
      const args: Record<string, unknown> = {}
      if (nw !== c.w || nh !== c.h) { args.w = nw; args.h = nh }
      if (Number.isFinite(nfps) && !sameRate(nfps, c.fps)) args.fps = nfps
      if (Object.keys(args).length) {
        const r = await dispatch('set_canvas', args)
        if (!r) { setSaving(false); return }
      }
      save(sid, { importPolicy: policy, arrangeLayers: arrange, proxy })
      if (proxy !== (previewEngine === 'client')) await setPreviewEngineSetting(proxy ? 'client' : 'server')
      onClose()
    } finally { setSaving(false) }
  }

  return (
    <Dialog open title="Project settings" labelId={`${id}-title`} onClose={onClose} className="dlg dlg-settings" showClose={false}
            footer={<><span className="spacer" /><button type="button" className="ui-btn-secondary dlg-btn" onClick={onClose}>Cancel</button>
              <button type="button" className="ui-btn-primary dlg-btn" disabled={saving} onClick={() => void onSave()}>Save</button></>}>
      <div className="dlg-head-tabs">
        <Segmented label="Project settings section" value={tab} onChange={setTab} className="ui-seg-fit" options={[{ id: 'Details', label: 'Details' }, { id: 'Performance', label: 'Performance' }]} />
      </div>
      {tab === 'Details' ? (
        <div className="dlg-form">
          <label className="dlg-row"><span className="dlg-label">Name</span><input className="ui-input on-modal" value={name} onChange={(e) => setName(e.target.value)} maxLength={120} /></label>
          <div className="dlg-row"><span className="dlg-label">Save to</span>
            <div className="dlg-inline"><input className="ui-input on-modal" value={sid ? `workdir/${sid}` : ''} readOnly aria-label="Save to" />
              <button type="button" className="ui-btn-secondary" disabled title="This build keeps every project in its own folder under the app's work directory; choosing another location is not available">Choose…</button></div></div>
          <div className="dlg-row dlg-row-top"><span className="dlg-label">Imported media</span>
            <div className="dlg-radios" role="radiogroup" aria-label="Imported media">
              <label className="ui-radio"><input type="radio" name={`${id}-policy`} checked={policy === 'copy'} onChange={() => setPolicy('copy')} />Copy media to project</label>
              <label className="ui-radio" title="Imports are always copied into the project folder in this build; this choice is remembered for a future build that can reference files in place"><input type="radio" name={`${id}-policy`} checked={policy === 'stay'} onChange={() => setPolicy('stay')} />Stay in original location</label>
            </div></div>
          <label className="dlg-row"><span className="dlg-label">Aspect ratio</span>
            <select className="ui-select on-modal" value={ratio} onChange={(e) => { const v = e.target.value as RatioId; setRatio(v); if (v === 'Custom') setRes('Custom'); else if (v !== 'Original') setRes('Adapted') }}>
              {RATIOS.filter((r) => r.id !== 'Original').map((r) => <option key={r.id} value={r.id} title={r.title}>{r.label}</option>)}
            </select></label>
          <div className="dlg-row"><span className="dlg-label">Resolution</span>
            <div className="dlg-inline">
              <Segmented label="Resolution" value={res} onChange={(v) => { setRes(v); if (v === 'Custom') setRatio('Custom') }} className="ui-seg-fit" options={[{ id: 'Adapted', label: 'Adapted' }, { id: 'Custom', label: 'Custom' }]} />
              {res === 'Custom' && (<>
                <input className="ui-input on-modal dlg-num" type="number" min={16} max={7680} value={w} onChange={(e) => setW(e.target.value)} aria-label="Width" />
                <span className="ui-faint">×</span>
                <input className="ui-input on-modal dlg-num" type="number" min={16} max={7680} value={h} onChange={(e) => setH(e.target.value)} aria-label="Height" />
              </>)}
              {res === 'Adapted' && <span className="ui-faint">{RATIOS.find((r) => r.id === ratio)?.w ?? c.w} × {RATIOS.find((r) => r.id === ratio)?.h ?? c.h}</span>}
            </div></div>
          <label className="dlg-row"><span className="dlg-label">Frame rate</span>
            <select className="ui-select on-modal" value={fps} onChange={(e) => setFps(e.target.value)}>
              {FRAME_RATES.map((r) => <option key={r.fps} value={r.fps}>{r.label}</option>)}
              {!FRAME_RATES.some((r) => sameRate(r.fps, c.fps)) && <option value={c.fps}>{c.fps} fps (this project)</option>}
            </select></label>
          <label className="dlg-row"><span className="dlg-label">Color space</span>
            <select className="ui-select on-modal" value="rec709" onChange={() => undefined} title="The render pipeline composes and exports in BT.709 SDR"><option value="rec709">Rec. 709 SDR</option></select></label>
          <div className="dlg-row dlg-row-top"><span className="dlg-label">Arrange layers</span>
            <div className="dlg-stack">
              <Toggle on={arrange} label="Arrange layers" disabled={settings.arrangeLayers} onChange={setArrange} />
              <span className="ui-warn"><Icon name="warning" />Cannot be turned off after activation.</span>
            </div></div>
        </div>
      ) : (
        <div className="dlg-form">
          <div className="dlg-row dlg-row-top"><span className="dlg-label">Proxy</span>
            <div className="dlg-stack">
              <Toggle on={proxy} label="Proxy" onChange={setProxy} />
              <span className="ui-faint" style={{ lineHeight: 1.4 }}>Creates lightweight copies for smoother editing. Final export always uses the original media, so output quality is unchanged.</span>
            </div></div>
        </div>
      )}
    </Dialog>
  )
}
