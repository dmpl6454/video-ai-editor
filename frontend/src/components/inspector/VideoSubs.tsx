// Video › Remove BG, Mask, Retouch (design §2c; brief §3 "Remove BG and
// retouch"). Remove BG's Auto removal and Chroma key are real AI tools
// (rembg / a colour key) opened with their options and progress; Custom
// removal (a brush) is not in this build. Mask offers the shapes the engine
// renders (a circle or a rounded rectangle, on an overlay clip; the PIP shape
// in components/Properties) and names the rest. Retouch is shown as the
// reference shows it and says it is not available.
import { useEffect, useState } from 'react'
import { useStore } from '../../store'
import { useLayoutStore } from '../../lib/layoutStore'
import type { AnyClip, Track } from '../../types'
import { Properties } from '../Properties'
import { Segmented } from '../ui/Segmented'
import { Checkbox } from '../ui/Checkbox'
import { SliderRow } from '../ui/SliderRow'
import { Icon } from '../Icon'
import { PanelState } from '../ui/Skeleton'

export function RemoveBg({ clipId, track, clip }: { clipId: string; track: Track; clip: AnyClip }) {
  const jumpToAi = useLayoutStore((s) => s.jumpToAi)
  const key = (clip as unknown as { chromakey?: unknown }).chromakey
  void clipId; void track
  return (
    <div className="in-pad in-stack">
      <button type="button" className="in-option" onClick={() => jumpToAi({ tool: 'remove_background', from: 'media' })}>
        <span className="in-option-check" aria-hidden="true" /><span className="in-option-name">Auto removal</span><span className="ui-faint">Processes on apply</span>
      </button>
      <button type="button" className="in-option" disabled title="Not available in this build: a brush-painted subject mask needs an interactive segmentation model this build does not ship">
        <span className="in-option-check" aria-hidden="true" /><span className="in-option-name">Custom removal</span><span className="ui-faint">Brush subject</span>
      </button>
      <button type="button" className={`in-option${key ? ' is-on' : ''}`} onClick={() => jumpToAi({ tool: 'chroma_key', from: 'media' })}>
        <span className={`in-option-check${key ? ' is-checked' : ''}`} aria-hidden="true">{!!key && <Icon name="check" />}</span>
        <span className="in-option-name">Chroma key</span><span className="in-swatch" style={{ background: '#30d158' }} />
      </button>
      <span className="ui-faint">Auto removal cuts the subject out with an AI matte and renders a new source; Chroma key removes one colour live.</span>
    </div>
  )
}

export function VideoMask({ clipId, track, clip }: { clipId: string; track: Track; clip: AnyClip }) {
  const dispatch = useStore((s) => s.dispatch)
  const [loading, setLoading] = useState(true)
  useEffect(() => { const id = window.setTimeout(() => setLoading(false), 400); return () => window.clearTimeout(id) }, [])
  const mask = (clip as unknown as { mask?: { type?: string } | null }).mask?.type ?? null
  const overlay = track.type === 'video' && track.id !== 'v1'
  const shapes: { id: string; icon: 'minus' | 'record' | 'stop' | 'custom' | 'rename'; label: string; type: string | null; ok: boolean }[] = [
    { id: 'line', icon: 'minus', label: 'Line', type: null, ok: false },
    { id: 'circle', icon: 'record', label: 'Circle', type: 'circle', ok: overlay },
    { id: 'rect', icon: 'stop', label: 'Rectangle', type: 'rounded', ok: overlay },
    { id: 'heart', icon: 'custom', label: 'Heart', type: null, ok: false },
    { id: 'pen', icon: 'rename', label: 'Pen', type: null, ok: false },
  ]
  return (
    <div className="in-pad in-stack">
      <div className="ui-row-between"><span className="ui-heading">Video mask</span>
        <Checkbox checked={!!mask} disabled={!overlay} title={overlay ? 'Clear the mask' : 'Masks render on overlay (picture-in-picture) clips'}
                  onChange={(on) => { if (!on) void dispatch('remove_mask', { clip_id: clipId }) }} /></div>
      {loading ? (
        <PanelState kind="loading" title="Loading mask shapes…" />
      ) : (
        <>
          <div className="in-shapes" role="group" aria-label="Mask shape">
            {shapes.map((s) => (
              <button key={s.id} type="button" className={`in-shape${mask === s.type && s.type ? ' is-active' : ''}`} disabled={!s.ok}
                      title={s.ok ? s.label : overlay ? `${s.label}: not rendered by the engine` : 'Masks render on overlay (picture-in-picture) clips'}
                      aria-label={s.label}
                      onClick={() => { if (s.type) void dispatch('add_mask', { clip_id: clipId, type: s.type, feather: 0 }) }}>
                <Icon name={s.icon} size={20} />
              </button>
            ))}
          </div>
          <button type="button" className="ui-btn-secondary ab-self-start" disabled={!overlay}
                  title={overlay ? 'Add a circle mask' : 'Masks render on overlay (picture-in-picture) clips — move this clip to an overlay lane first'}
                  onClick={() => void dispatch('add_mask', { clip_id: clipId, type: 'circle', feather: 0 })}><Icon name="plus" />Add mask</button>
          {overlay && <div className="in-legacy"><Properties bare sections={['PIP shape']} /></div>}
        </>
      )}
    </div>
  )
}

export function Retouch() {
  const [mode, setMode] = useState<'Face' | 'Body' | 'Face presets' | 'Body presets'>('Face')
  const rows = ['Clear', 'Smooth', 'Whitening', 'Clear blemishes', 'Dewrinkle', 'Even', 'Plump']
  return (
    <div className="in-pad in-stack" aria-disabled="true">
      <Segmented label="Retouch" value={mode} onChange={setMode} size="small"
                 options={[{ id: 'Face', label: 'Face' }, { id: 'Body', label: 'Body' }, { id: 'Face presets', label: 'Face presets' }, { id: 'Body presets', label: 'Body presets' }]} />
      <div className="ui-row-between"><span className="ui-muted">Face</span><span className="in-face-chip" /><span className="in-face-add"><Icon name="plus" /></span></div>
      <span className="ui-heading">Skin</span>
      {rows.map((r) => <SliderRow key={r} label={r} min={0} max={100} step={1} value={0} disabled editable={false} onChange={() => undefined} labelWidth={90} fieldWidth={48} />)}
      <PanelState kind="unavailable" title="Retouch is not available in this build" body="Face and body retouching need a face-landmark model this build does not ship. The controls are shown where the reference shows them." />
    </div>
  )
}
