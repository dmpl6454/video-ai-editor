// Adjustment (design §2c; brief §5 "Color adjustment"): Basic · HSL · Curves
// · Color wheel · Mask, with Apply to all and Save as preset anchored at the
// bottom. Basic is the clip's REAL colour grade (`color_grade`: brightness,
// contrast, saturation, temperature, tint — render/effects.py, with a live
// CSS stand-in while a slider moves) and its LUT (`apply_lut` from the
// bundled looks, with intensity). The rows the engine has no parameter for
// (Highlight, Shadow, Whites; HSL per hue; curves; wheels) are drawn where
// the reference draws them, disabled, with the reason — never a slider that
// changes nothing.
import { useEffect, useState } from 'react'
import { api } from '../../api'
import { useStore } from '../../store'
import { toast } from '../../toast'
import { isMediaClip } from '../../types'
import { lutApplyArgs } from '../../lib/lutActions'
import { lutDisplayName } from '../../lib/lutName'
import { saveAdjustPreset } from '../../lib/adjustPresets'
import { Segmented } from '../ui/Segmented'
import { SliderRow } from '../ui/SliderRow'
import { Toggle } from '../ui/Toggle'
import { Checkbox } from '../ui/Checkbox'
import { Icon } from '../Icon'
import { PanelState } from '../ui/Skeleton'
import { useSubTab } from './useSubTab'
import { grade } from '../../lib/gradeScale'

type Sub = 'Basic' | 'HSL' | 'Curves' | 'Color wheel' | 'Mask'
const GRADE_DEFAULTS = { brightness: 0, contrast: 1, saturation: 1, temp: 0, tint: 0 }

function useClipGrade(clipId: string) {
  const edl = useStore((s) => s.edl)
  const clip = edl?.tracks.flatMap((t) => t.clips).find((c) => c.id === clipId)
  const effects = clip && isMediaClip(clip) ? ((clip as unknown as { effects?: { type: string; params?: Record<string, unknown> }[] }).effects ?? []) : []
  const color = effects.find((e) => e.type === 'color' || e.type === 'color_grade')?.params ?? {}
  const lutIdx = effects.findIndex((e) => e.type === 'lut')
  const lut = lutIdx >= 0 ? effects[lutIdx] : null
  return { effects, color: color as Record<string, number>, lut, lutIdx }
}

export function AdjustmentPanel({ clipId }: { clipId: string }) {
  const [sub, setSub] = useSubTab<Sub>('Adjustment', 'Basic')
  return (
    <>
      <div className="in-pad-x">
        <Segmented label="Adjustment" value={sub} onChange={setSub} size="small"
                   options={[{ id: 'Basic', label: 'Basic' }, { id: 'HSL', label: 'HSL' }, { id: 'Curves', label: 'Curves' }, { id: 'Color wheel', label: 'Color wheel' }, { id: 'Mask', label: 'Mask' }]} />
      </div>
      {sub === 'Basic' && <BasicAdjust clipId={clipId} />}
      {sub === 'HSL' && <Hsl />}
      {sub === 'Curves' && <Curves />}
      {sub === 'Color wheel' && <ColorWheel />}
      {sub === 'Mask' && <AdjustMask />}
    </>
  )
}

function BasicAdjust({ clipId }: { clipId: string }) {
  const sid = useStore((s) => s.sessionId)
  const dispatch = useStore((s) => s.dispatch)
  const setLiveFilter = useStore((s) => s.setLiveFilter)
  const { color, lut, lutIdx } = useClipGrade(clipId)
  const [luts, setLuts] = useState<string[] | null>(null)
  const [lutError, setLutError] = useState<string | null>(null)
  useEffect(() => {
    if (!sid) return
    let live = true
    api.dispatch<{ luts?: string[] }>(sid, 'list_luts', {})
      .then((r) => { if (live) setLuts(r.result?.luts ?? []) })
      .catch(() => { if (live) { setLuts([]); setLutError('Could not list the bundled looks') } })
    return () => { live = false }
  }, [sid])
  const commit = (p: Record<string, number>) => { void dispatch('color_grade', { clip_id: clipId, ...p }) }
  const live = (p: { brightness?: number; contrast?: number; saturation?: number }) =>
    setLiveFilter({ clipId, brightness: color.brightness ?? 0, contrast: color.contrast ?? 1, saturation: color.saturation ?? color.sat ?? 1, ...p })
  const lutSrc = (lut?.params?.src as string | undefined) ?? ''
  const lutName = lutSrc ? lutDisplayName(lutSrc) : ''
  const lutOn = !!lut
  const intensity = Math.round(((lut?.params?.intensity as number | undefined) ?? 1) * 100)
  const applyLut = (name: string, pct = intensity) => { void dispatch('apply_lut', lutApplyArgs({ src: name, intensity: pct / 100, clipId })) }
  const rows: { label: string; key: keyof typeof GRADE_DEFAULTS; liveKey?: 'brightness' | 'contrast' | 'saturation' }[] = [
    { label: 'Temp', key: 'temp' }, { label: 'Tint', key: 'tint' }, { label: 'Saturation', key: 'saturation', liveKey: 'saturation' },
  ]
  const light: { label: string; key: keyof typeof GRADE_DEFAULTS; liveKey?: 'brightness' | 'contrast' | 'saturation' }[] = [
    { label: 'Exposure', key: 'brightness', liveKey: 'brightness' }, { label: 'Contrast', key: 'contrast', liveKey: 'contrast' },
  ]
  const row = (r: typeof rows[number]) => (
    <SliderRow key={r.key} label={r.label} min={-100} max={100} step={1} dp={0}
               value={grade.toSlider[r.key](color[r.key] ?? (r.key === 'saturation' ? color.sat : undefined) ?? GRADE_DEFAULTS[r.key])}
               fieldWidth={48} keyed={null}
               onLive={r.liveKey ? (v) => live({ [r.liveKey as string]: grade.fromSlider[r.key](v) }) : undefined}
               onChange={(v) => commit({ [r.key]: Number(grade.fromSlider[r.key](v).toFixed(3)) })} />
  )
  return (
    <div className="in-pad in-stack">
      <div className="in-two">
        <button type="button" className="ui-btn-secondary" disabled title="Not available in this build: automatic exposure and white balance need an analysis pass this build does not ship"><Icon name="magicWand" />Auto adjust</button>
        <button type="button" className="ui-btn-secondary" disabled title="Not available in this build: matching another clip's colour needs a reference analysis this build does not ship"><Icon name="canvasColor" />Color match</button>
      </div>
      <div className="ui-row-between"><span className="ui-heading">Color correction</span></div>
      <div className="ui-row-between"><span>LUT</span>
        <Toggle on={lutOn} label="LUT" title={lutOn ? 'Remove the look' : luts?.length ? 'Apply the first bundled look' : 'No looks to apply'}
                disabled={!lutOn && !luts?.length}
                onChange={(on) => { if (!on && lutIdx >= 0) void dispatch('remove_effect', { clip_id: clipId, index: lutIdx }); else if (on && luts?.length) applyLut(luts[0]) }} /></div>
      <div className={`in-stack${lutOn ? '' : ' is-dim'}`}>
        <div className="in-row-70">
          <span className="ui-muted">LUT Name</span>
          <select className="ui-select" value={luts?.includes(lutSrc) ? lutSrc : lutSrc ? '__custom' : ''} disabled={!lutOn || !luts}
                  aria-label="LUT name" onChange={(e) => { if (e.target.value && e.target.value !== '__custom') applyLut(e.target.value) }}>
            {!lutOn && <option value="">—</option>}
            {lutSrc && !luts?.includes(lutSrc) && <option value="__custom">{lutName}</option>}
            {(luts ?? []).map((n) => <option key={n} value={n}>{lutDisplayName(n)}</option>)}
          </select>
        </div>
        {lutError && <span className="ui-faint">{lutError}</span>}
        <SliderRow label="Intensity" min={0} max={100} step={1} dp={0} value={intensity} fieldWidth={48} disabled={!lutOn} editable={false}
                   onChange={(v) => { if (lutSrc) applyLut(lutSrc, v) }} />
        <div className="ui-row-between" style={{ minHeight: 24 }}><span>Protect skin tone</span>
          <Toggle on={false} label="Protect skin tone" disabled title="Not available in this build: skin-tone protection needs a skin mask the LUT stage does not compute" onChange={() => undefined} /></div>
      </div>
      <div className="ui-hairline" />
      <div className="ui-row-between"><span className="ui-heading">Adjust</span>
        <button type="button" className="ui-icon-btn is-small" aria-label="Reset adjustments" title="Reset Temp, Tint, Saturation, Exposure and Contrast"
                onClick={() => commit(GRADE_DEFAULTS)}><Icon name="reset" /></button></div>
      {rows.map(row)}
      <span className="ui-heading" style={{ marginTop: 4 }}>Lightness</span>
      {light.map(row)}
      {['Highlight', 'Shadow', 'Whites'].map((l) => (
        <SliderRow key={l} label={l} min={-100} max={100} step={1} dp={0} value={0} fieldWidth={48} keyed={null} disabled editable={false} onChange={() => undefined} />
      ))}
      <span className="ui-faint">Highlight, Shadow and Whites are not in this build's grade (it has exposure, contrast, saturation, temperature and tint).</span>
    </div>
  )
}

function Hsl() {
  const colors = ['#ff453a', '#ff9f0a', '#ffd60a', '#30d158', '#64d2ff', '#0a84ff', '#bf5af2', '#ff375f']
  return (
    <div className="in-pad in-stack" aria-disabled="true">
      <div className="in-hsl-row">
        <span className="ui-icon-btn is-small is-raised" aria-hidden="true"><Icon name="eyedropper" /></span>
        <div className="in-hsl-dots">{colors.map((c, i) => <span key={c} className={`in-hsl-dot${i === 0 ? ' is-selected' : ''}`} style={{ background: c }} />)}</div>
      </div>
      {['Hue', 'Saturation', 'Brightness'].map((l) => <SliderRow key={l} label={l} min={-100} max={100} step={1} value={0} disabled editable={false} fieldWidth={48} onChange={() => undefined} />)}
      <PanelState kind="unavailable" title="Per-colour HSL is not available in this build" body="The grade adjusts the whole picture (Basic). Hue-range adjustments need a secondary-colour stage the engine does not have yet." />
    </div>
  )
}

function Curves() {
  return (
    <div className="in-pad in-stack" aria-disabled="true">
      <div className="ui-row-between"><span className="ui-heading">All</span><Checkbox checked onChange={() => undefined} disabled /></div>
      <div className="ui-row-between"><span>Luma</span><span className="in-curve-tools"><span className="ui-icon-btn is-small is-raised"><Icon name="eyedropper" /></span><span className="ui-icon-btn is-small is-raised"><Icon name="reset" /></span></span></div>
      <div className="in-curve"><svg viewBox="0 0 100 100" preserveAspectRatio="none"><path d="M0 100 C 30 70 70 30 100 0" fill="none" stroke="#f5f5f7" strokeWidth="1.2" vectorEffect="non-scaling-stroke" /><circle cx="0" cy="100" r="2.2" fill="#fff" /><circle cx="50" cy="50" r="2.2" fill="#fff" /><circle cx="100" cy="0" r="2.2" fill="#fff" /></svg></div>
      <div className="ui-row-between"><span className="in-curve-ch"><span className="in-curve-dot" style={{ background: '#ff453a' }} />Red (R)</span><span className="in-curve-tools"><span className="ui-icon-btn is-small is-raised"><Icon name="eyedropper" /></span><span className="ui-icon-btn is-small is-raised"><Icon name="reset" /></span></span></div>
      <div className="in-curve"><svg viewBox="0 0 100 100" preserveAspectRatio="none"><path d="M0 100 L100 0" fill="none" stroke="#ff453a" strokeWidth="1.2" vectorEffect="non-scaling-stroke" /></svg></div>
      <PanelState kind="unavailable" title="Curves are not available in this build" body="Editable tone curves need a per-channel LUT stage the engine does not have yet. Exposure and Contrast under Basic shape the tone." />
    </div>
  )
}

function ColorWheel() {
  const wheels = ['Shadows', 'Middle grey', 'Tint', 'Offset']
  return (
    <div className="in-pad in-stack" aria-disabled="true">
      <Segmented label="Color wheel mode" value="Primary" onChange={() => undefined} disabled options={[{ id: 'Primary', label: 'Primary' }, { id: 'Log', label: 'Log' }]} />
      <SliderRow label="Intensity" min={0} max={100} step={1} value={100} disabled editable={false} fieldWidth={48} onChange={() => undefined} />
      <div className="in-wheels">
        {wheels.map((w) => (
          <div key={w} className="in-wheel">
            <span className="ui-faint">{w}</span>
            <span className="in-wheel-disc"><span className="in-wheel-dot" /></span>
            <span className="in-wheel-fields"><span>0</span><span>0</span><span>0</span><span className="in-wheel-reset"><Icon name="reset" /></span></span>
          </div>
        ))}
      </div>
      <PanelState kind="unavailable" title="Colour wheels are not available in this build" body="Lift / gamma / gain wheels need a three-way colour stage the engine does not have yet. Temp and Tint under Basic shift the balance." />
    </div>
  )
}

function AdjustMask() {
  const [loading, setLoading] = useState(true)
  useEffect(() => { const id = window.setTimeout(() => setLoading(false), 400); return () => window.clearTimeout(id) }, [])
  return (
    <div className="in-pad in-stack">
      <div className="ui-row-between"><span className="ui-heading">Mask</span><Checkbox checked={false} onChange={() => undefined} disabled /></div>
      {loading ? <PanelState kind="loading" title="Loading mask shapes…" /> : (
        <>
          <button type="button" className="ui-btn-secondary ab-self-start" disabled title="Not available in this build: a colour-adjustment mask needs a masked grade stage the engine does not have yet"><Icon name="plus" />Add mask</button>
          <span className="ui-faint">Limits color adjustments to a region. Separate from the Video mask. Not available in this build.</span>
        </>
      )}
    </div>
  )
}

AdjustmentPanel.Footer = function Footer({ clipId }: { clipId: string }) {
  const edl = useStore((s) => s.edl)
  const dispatch = useStore((s) => s.dispatch)
  const { color, lut } = useClipGrade(clipId)
  const applyAll = () => {
    if (!edl) return
    const params = { ...GRADE_DEFAULTS, ...color }
    const v1 = edl.tracks.find((t) => t.id === 'v1')
    let n = 0
    for (const c of v1?.clips ?? []) {
      if (c.id === clipId || !isMediaClip(c)) continue
      void dispatch('color_grade', { clip_id: c.id, ...params })
      if (lut) void dispatch('apply_lut', lutApplyArgs({ src: lut.params?.src as string, intensity: (lut.params?.intensity as number | undefined) ?? 1, clipId: c.id }))
      n++
    }
    toast.info(n ? `Applied to ${n} other main-track clip${n === 1 ? '' : 's'}` : 'No other main-track clips to apply to')
  }
  const savePreset = () => {
    const name = window.prompt('Preset name', 'My look')
    if (!name) return
    saveAdjustPreset(name.trim() || 'My look', { ...GRADE_DEFAULTS, ...color })
    toast.success(`Saved “${name}” — find it under Adjustment › Yours`)
  }
  return (
    <div className="in-foot">
      <button type="button" className="ui-btn-secondary" onClick={applyAll} title="Put this clip's grade (and look) on every other main-track clip">Apply to all</button>
      <button type="button" className="ui-btn-secondary" onClick={savePreset} title="Keep these settings as a preset under Adjustment › Yours">Save as preset</button>
    </div>
  )
}
