import React from 'react'
import './speed.css'
import { Icon } from '../Icon'
import { curvePoints } from '../../lib/preview/timeline/speedCurve'
import {
  CUSTOM_START, addPoint, curveDuration, curvePath, matchPreset, normalizeForCommit, widestGapMid, type Curve,
} from '../../lib/speed/curveMath'
import { useSpeedCatalog, type SpeedPreset } from '../../lib/speed/speedCatalog'
import { KEEP_PITCH_TITLE } from '../../lib/audioChannels'
import { SpeedCurveEditor } from './SpeedCurveEditor'

// The Inspector's Speed section, CapCut's layout (wave D, lane S2): a
// Normal | Curve segmented control; Normal is the constant-speed slider the
// section always had (passed in by Properties), Curve is the preset menu —
// None, Montage, Hero, Bullet, Jump Cut, Flash In, Flash Out, Custom — and an
// editable graph of the clip's curve. Under both: the clip's resulting
// length and, where the sound is retimed, Keep pitch.
//
// Presets come from the server (lib/speed/speedCatalog → edl/speed_presets
// .py), the one table set_speed and the agent read. Which preset a clip has
// is the stored curve's `name` — the server derives it from the points, so
// a dragged Hero point reads Custom here without the client deciding.

type Send = (tool: string, args: Record<string, unknown>) => unknown

export interface SpeedSectionProps {
  clipId: string
  /** The clip's stored `speed` (number, null or a curve). */
  speed: unknown
  /** A freeze frame's hold (the clip is a still), or null. */
  freeze: number | null
  /** Source seconds the clip consumes (out − in). */
  sourceSeconds: number
  keepPitch: boolean
  /** Curves are offered on the Main video lane only (PIP renders at 1x). */
  curveAllowed: boolean
  /** Where the playhead sits across the clip, 0..1, or null outside it. */
  playheadFrac: number | null
  /** The constant-speed slider (Properties' own Slider). */
  normalControl: React.ReactNode
  send: Send
}

function secs(v: number): string {
  return `${v.toFixed(v < 10 ? 2 : 1)}s`
}

/** A preset's shape as a picture, not an icon: an SVG used as a CSS MASK
 *  over `currentColor`, so it follows the button's text colour while the
 *  app's one icon rule (every `button svg` is a 16 px lucide glyph) holds. */
function thumbMask(points: Curve): string {
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="-1 -1 42 22" preserveAspectRatio="none">'
    + '<path d="M0 10 H40" stroke="#000" stroke-opacity=".35" stroke-dasharray="2 2" fill="none"/>'
    + `<path d="${curvePath(points, 40, 20)}" stroke="#000" stroke-width="1.6" stroke-linejoin="round" fill="none"/></svg>`
  return `url("data:image/svg+xml,${encodeURIComponent(svg)}")`
}

function Thumb({ points }: { points: Curve }) {
  const mask = thumbMask(points)
  return <span className="speed-thumb" aria-hidden="true" style={{ maskImage: mask, WebkitMaskImage: mask }} />
}

export function SpeedSection(p: SpeedSectionProps) {
  const cat = useSpeedCatalog()
  const stored = curvePoints(p.speed)
  const curve: Curve | null = stored ? stored.map(([x, r]) => [x, r] as const) : null
  const storedName = (p.speed && typeof p.speed === 'object' ? (p.speed as { name?: unknown }).name : undefined)
  const factor = typeof p.speed === 'number' && p.speed > 0 ? p.speed : 1
  // The tab follows the clip (a curve shows Curve) until the user picks one.
  const [picked, setMode] = React.useState<'normal' | 'curve' | null>(null)
  const mode = picked ?? (curve ? 'curve' : 'normal')
  // The live curve while a point is dragged, for the duration readout;
  // keyed by the stored curve so a commit (or undo) retires it.
  const storedKey = JSON.stringify(stored)
  const [live, setLive] = React.useState<{ key: string; pts: Curve } | null>(null)
  const draft = live && live.key === storedKey ? live.pts : null
  const setDraft = (pts: Curve) => setLive({ key: storedKey, pts })
  const [base, setBase] = React.useState<Curve | null>(null)

  const presets: SpeedPreset[] = cat.status === 'ready' ? cat.catalog.presets.filter((q) => q.menu) : []
  const named = typeof storedName === 'string' ? storedName : null
  const active = !curve ? 'none' : (named && presets.some((q) => q.id === named) ? named
    : matchPreset(curve, presets)?.id ?? 'custom')

  if (p.freeze !== null) {
    return (
      <div className="speed-section">
        <p className="speed-note"><Icon name="freeze" /> Freeze frame — one frame held for {secs(p.freeze)}.
          Change the hold with Duration above or by dragging the clip&apos;s edge.</p>
      </div>
    )
  }

  const choose = (id: string) => {
    if (id === 'none') { void p.send('set_speed', { clip_id: p.clipId, factor: 1 }); return }
    if (id === 'custom') {
      if (active === 'custom') return
      // From a preset: the same shape plus one point ON it (the widest gap),
      // so nothing moves yet but the curve is no longer the preset's — the
      // server names it custom. From no curve: flat 1x with handles.
      const from = curve ? addPoint(curve, widestGapMid(curve)) : null
      const pts = from ? normalizeForCommit(from.curve) : normalizeForCommit(CUSTOM_START)
      setBase(curve ?? CUSTOM_START)
      void p.send('set_speed', { clip_id: p.clipId, curve: pts })
      return
    }
    const preset = presets.find((q) => q.id === id)
    if (preset) setBase(preset.points)
    void p.send('set_speed', { clip_id: p.clipId, preset: id })
  }

  const options = [
    { id: 'none', label: 'None', hint: 'No curve: the clip plays at its Normal speed', points: [[0, 1], [1, 1]] as Curve },
    ...presets.map((q) => ({ id: q.id, label: q.label, hint: q.hint, points: q.points as Curve })),
    { id: 'custom', label: 'Custom', hint: 'Draw your own curve: drag, add and remove points', points: curve && active === 'custom' ? curve : CUSTOM_START },
  ]
  const onRadioKey = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const keys = ['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp']
    if (!keys.includes(e.key)) return
    e.preventDefault()
    e.stopPropagation()
    const btns = Array.from(e.currentTarget.querySelectorAll<HTMLButtonElement>('[role="radio"]'))
    const at = btns.findIndex((b) => b === document.activeElement)
    const d = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : -1
    btns[(at + d + btns.length) % btns.length]?.focus()
  }

  const shown = draft ?? curve
  const length = shown ? curveDuration(shown, p.sourceSeconds) : p.sourceSeconds / factor
  const retimed = !!curve || Math.abs(factor - 1) > 1e-6
  const presetOf = active !== 'custom' && active !== 'none' ? presets.find((q) => q.id === active) : null
  const resetTo: Curve = presetOf ? presetOf.points : base ?? CUSTOM_START

  return (
    <div className="speed-section">
      {p.curveAllowed && (
        <div className="speed-modes" role="radiogroup" aria-label="Speed mode" onKeyDown={onRadioKey}>
          {(['normal', 'curve'] as const).map((m) => (
            <button key={m} type="button" role="radio" aria-checked={mode === m} tabIndex={mode === m ? 0 : -1}
                    onClick={() => setMode(m)}>
              <Icon name={m === 'normal' ? 'speedNormal' : 'speedCurve'} /> {m === 'normal' ? 'Normal' : 'Curve'}
            </button>
          ))}
        </div>
      )}
      {(mode === 'normal' || !p.curveAllowed) && (
        <>
          {curve && (
            <p className="speed-note">This clip plays a {presetOf ? presetOf.label : 'custom'} curve —
              moving the slider replaces it with one constant speed.</p>
          )}
          {p.normalControl}
        </>
      )}
      {mode === 'curve' && p.curveAllowed && (
        <>
          {cat.status === 'loading' && <p className="speed-note">Loading presets…</p>}
          {cat.status === 'error' && (
            <p className="speed-note speed-error" role="alert">Speed presets are unavailable: the engine did not answer ({cat.message}).</p>
          )}
          {cat.status === 'ready' && (
            <div className="speed-presets" role="radiogroup" aria-label="Speed curve" onKeyDown={onRadioKey}>
              {options.map((o) => (
                <button key={o.id} type="button" role="radio" className="speed-preset"
                        aria-checked={active === o.id} tabIndex={active === o.id ? 0 : -1}
                        title={o.hint} data-preset={o.id} onClick={() => choose(o.id)}>
                  <Thumb points={o.points} />
                  <span>{o.label}</span>
                </button>
              ))}
            </div>
          )}
          {curve && (
            <SpeedCurveEditor points={curve} playheadFrac={p.playheadFrac} resetTo={resetTo}
              onDraft={setDraft}
              onCommit={(pts) => { void p.send('set_speed', { clip_id: p.clipId, curve: pts }) }} />
          )}
        </>
      )}
      <div className="speed-duration" aria-live="polite">
        <span className="speed-duration-label">Duration</span>
        <span className="speed-duration-value">
          {retimed || draft ? <>{secs(p.sourceSeconds)} <Icon name="chevronRight" /> {secs(length)}</> : secs(p.sourceSeconds)}
        </span>
      </div>
      {retimed && (
        <label className="speed-pitch" title={KEEP_PITCH_TITLE}>
          <input type="checkbox" checked={p.keepPitch}
            onChange={(e) => void p.send('set_speed', curve
              ? { clip_id: p.clipId, curve: curve.map(([x, r]) => [x, r]), keep_pitch: e.target.checked }
              : { clip_id: p.clipId, factor, keep_pitch: e.target.checked })} />
          Keep pitch
        </label>
      )}
    </div>
  )
}
