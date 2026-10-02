// Transform (design §2c "Video › Basic"): Scale (10–400 %, a % field), the
// Uniform scale switch, Scale Y, Position X / Y, Rotation (−180…180, step
// .5, "0.00°"), the six-way Align — each with a keyframe diamond — and reset.
// Values come from the clip AT THE PLAYHEAD (lib/overlay.sampleKF: the pose
// on screen, keys included); every commit carries `time`, so on an animated
// property it writes a key at the playhead instead of flattening the curve
// (the rule components/Properties' Transform section documents).
//
// Scale Y is drawn where the reference draws it and is dimmed: the render
// engine has ONE scale (edl.schema.Transform.scale), so uniform scaling is
// always on and the switch says so.
import { useStore } from '../../store'
import { isMediaClip, type AnyClip } from '../../types'
import { sampleKF, keyEps, type KFNum } from '../../lib/overlay'
import { clipLocalTime } from '../../lib/timelineLayout'
import { SliderRow } from '../ui/SliderRow'
import { Toggle } from '../ui/Toggle'
import { Icon } from '../Icon'

type Tx = { x?: KFNum; y?: KFNum; scale?: KFNum; rotation?: KFNum; opacity?: KFNum }

function keyed(v: KFNum | undefined): boolean {
  return !!v && typeof v === 'object' && Array.isArray((v as { keyframes?: unknown[] }).keyframes) && ((v as { keyframes: unknown[] }).keyframes.length > 0)
}
function keyAt(v: KFNum | undefined, t: number, fps: number): boolean {
  if (!keyed(v)) return false
  return (v as { keyframes: [number, number][] }).keyframes.some((k) => Math.abs(k[0] - t) < keyEps(fps))
}

export function TransformGroup({ clipId, trackId }: { clipId: string; trackId: string }) {
  const edl = useStore((s) => s.edl)
  const playhead = useStore((s) => s.playhead)
  const dispatch = useStore((s) => s.dispatch)
  const setLiveTransform = useStore((s) => s.setLiveTransform)
  const track = edl?.tracks.find((t) => t.id === trackId)
  const clip = track?.clips.find((c) => c.id === clipId) as AnyClip | undefined
  if (!edl || !track || !clip || !isMediaClip(clip)) return null
  const tx = (clip as unknown as { transform?: Tx }).transform
  const t = clipLocalTime(edl, trackId, clip, playhead)
  const fps = edl.canvas.fps
  const scale = sampleKF(tx?.scale, t, 1)
  const x = sampleKF(tx?.x, t, 0)
  const y = sampleKF(tx?.y, t, 0)
  const rot = sampleKF(tx?.rotation, t, 0)
  const halfW = Math.round(edl.canvas.w / 2), halfH = Math.round(edl.canvas.h / 2)
  const send = (args: Record<string, unknown>) => { void dispatch('set_clip_transform', { clip_id: clipId, time: t, ...args }) }
  const keyState = (v: KFNum | undefined) => (keyAt(v, t, fps) ? true : keyed(v) ? false : null)
  const toggleKey = (prop: 'scale' | 'x' | 'y' | 'rotation') => {
    const here = keyAt(tx?.[prop], t, fps)
    void dispatch(here ? 'remove_keyframe' : 'add_keyframe', { clip_id: clipId, prop, time: t })
  }
  // Align: the picture's edges against the frame. At scale ≤ 1 the base
  // layer already fills the frame, so left/right/top/bottom are the centre.
  const align = (which: 'l' | 'cx' | 'r' | 't' | 'cy' | 'b') => {
    const mx = Math.max(0, Math.round((edl.canvas.w * (scale - 1)) / 2))
    const my = Math.max(0, Math.round((edl.canvas.h * (scale - 1)) / 2))
    if (which === 'l') send({ x: mx }); else if (which === 'r') send({ x: -mx }); else if (which === 'cx') send({ x: 0 })
    else if (which === 't') send({ y: my }); else if (which === 'b') send({ y: -my }); else send({ y: 0 })
  }
  return (
    <div className="in-pad in-transform" data-section="Transform">
      <div className="ui-row-between">
        <span className="ui-heading">Transform</span>
        <button type="button" className="ui-icon-btn is-small" aria-label="Reset transform" title="Reset scale, position and rotation"
                onClick={() => send({ x: 0, y: 0, scale: 1, rotation: 0 })}><Icon name="reset" /></button>
      </div>
      <SliderRow label="Scale" min={10} max={400} step={1} value={Math.round(scale * 100)} unit="%"
                 onLive={(v) => setLiveTransform({ clipId, scale: v / 100 })}
                 onChange={(v) => send({ scale: Math.max(0.1, Math.min(4, v / 100)) })}
                 keyed={keyState(tx?.scale)} onKey={() => toggleKey('scale')} />
      <div className="ui-row-between">
        <span className="ui-muted">Uniform scale</span>
        <Toggle on label="Uniform scale" disabled onChange={() => undefined} title="Always on: the render engine scales both axes together" />
      </div>
      <SliderRow label="Scale Y" min={10} max={400} step={1} value={Math.round(scale * 100)} unit="%" disabled onChange={() => undefined} keyed={null} />
      <SliderRow label="Position X" min={-halfW} max={halfW} step={1} value={Math.round(x)} dp={0}
                 onLive={(v) => setLiveTransform({ clipId, dx: v - x })} onChange={(v) => send({ x: v })}
                 keyed={keyState(tx?.x)} onKey={() => toggleKey('x')} />
      <SliderRow label="Position Y" min={-halfH} max={halfH} step={1} value={Math.round(y)} dp={0}
                 onLive={(v) => setLiveTransform({ clipId, dy: v - y })} onChange={(v) => send({ y: v })}
                 keyed={keyState(tx?.y)} onKey={() => toggleKey('y')} />
      <SliderRow label="Rotation" min={-180} max={180} step={0.5} value={rot} dp={2} editable={false} format={(v) => `${v.toFixed(2)}°`}
                 onLive={(v) => setLiveTransform({ clipId, rotation: v })} onChange={(v) => send({ rotation: v })}
                 keyed={keyState(tx?.rotation)} onKey={() => toggleKey('rotation')} />
      <div className="in-align-row">
        <span className="ui-muted">Align</span>
        <div className="in-align" role="group" aria-label="Align">
          <button type="button" className="ui-icon-btn is-small" aria-label="Align left" title="Align left" onClick={() => align('l')}><Icon name="alignLeftEdge" /></button>
          <button type="button" className="ui-icon-btn is-small" aria-label="Center horizontally" title="Center horizontally" onClick={() => align('cx')}><Icon name="alignCenterH" /></button>
          <button type="button" className="ui-icon-btn is-small" aria-label="Align right" title="Align right" onClick={() => align('r')}><Icon name="alignRightEdge" /></button>
          <span className="in-align-sep" />
          <button type="button" className="ui-icon-btn is-small" aria-label="Align top" title="Align top" onClick={() => align('t')}><Icon name="alignTop" /></button>
          <button type="button" className="ui-icon-btn is-small" aria-label="Center vertically" title="Center vertically" onClick={() => align('cy')}><Icon name="alignCenterV" /></button>
          <button type="button" className="ui-icon-btn is-small" aria-label="Align bottom" title="Align bottom" onClick={() => align('b')}><Icon name="alignBottom" /></button>
        </div>
      </div>
    </div>
  )
}
