// The transform bounding box over a selected main-track video clip (design
// §2b "Bounding box"; brief §3 "Dragging the bounding box and editing
// inspector fields must update the same properties"): 1.5 px white border,
// eight 9×9 handles, a rotation stem and ring. Dragging the box commits
// `set_clip_transform` x/y, a corner commits scale, the ring rotation — the
// very tool the inspector's Transform rows send, with the live CSS stand-in
// (store.liveTransform) the sliders use, so a drag previews instantly and
// the bake lands on the same numbers.
//
// Only a main-track (v1) media clip: PIP, text and sticker clips already own
// their boxes in StickerLayer, which must keep the pointer there.
import { useRef } from 'react'
import { useStore } from '../../store'
import { isMediaClip } from '../../types'
import { sampleKF, type KFNum } from '../../lib/overlay'
import { clipLocalTime } from '../../lib/timelineLayout'

type Handle = 'nw' | 'n' | 'ne' | 'e' | 'se' | 's' | 'sw' | 'w'
const HANDLES: { id: Handle; x: number; y: number; cursor: string }[] = [
  { id: 'nw', x: 0, y: 0, cursor: 'nwse-resize' }, { id: 'n', x: 0.5, y: 0, cursor: 'ns-resize' }, { id: 'ne', x: 1, y: 0, cursor: 'nesw-resize' },
  { id: 'e', x: 1, y: 0.5, cursor: 'ew-resize' }, { id: 'se', x: 1, y: 1, cursor: 'nwse-resize' }, { id: 's', x: 0.5, y: 1, cursor: 'ns-resize' },
  { id: 'sw', x: 0, y: 1, cursor: 'nesw-resize' }, { id: 'w', x: 0, y: 0.5, cursor: 'ew-resize' },
]

export function TransformBox({ stageW, stageH }: { stageW: number; stageH: number }) {
  const edl = useStore((s) => s.edl)
  const selection = useStore((s) => s.selection)
  const playhead = useStore((s) => s.playhead)
  const framing = useStore((s) => s.framing)
  const isPlaying = useStore((s) => s.isPlaying)
  const live = useStore((s) => s.liveTransform)
  const drag = useRef<{ kind: 'move' | 'scale' | 'rotate'; x0: number; y0: number; sx: number; sy: number; scale0: number; rot0: number; cx: number; cy: number } | null>(null)
  if (!edl || !selection || stageW <= 0 || isPlaying) return null
  const v1 = edl.tracks.find((t) => t.id === 'v1')
  const clip = v1?.clips.find((c) => c.id === selection)
  if (!clip || !isMediaClip(clip) || framing?.clipId === clip.id) return null
  const tx = (clip as unknown as { transform?: { x?: KFNum; y?: KFNum; scale?: KFNum; rotation?: KFNum } }).transform
  const keyframed = (['x', 'y', 'scale', 'rotation'] as const).some((k) => tx?.[k] !== undefined && typeof tx?.[k] !== 'number')
  const t = clipLocalTime(edl, 'v1', clip, playhead)
  const scale0 = sampleKF(tx?.scale, t, 1)
  const x0 = sampleKF(tx?.x, t, 0)
  const y0 = sampleKF(tx?.y, t, 0)
  const rot0 = sampleKF(tx?.rotation, t, 0)
  const k = stageW / edl.canvas.w
  const scale = live?.clipId === clip.id && live.scale != null ? live.scale : scale0
  const rot = live?.clipId === clip.id && live.rotation != null ? live.rotation : rot0
  const dx = live?.clipId === clip.id && live.dx != null ? live.dx : 0
  const dy = live?.clipId === clip.id && live.dy != null ? live.dy : 0
  // v1 is the base layer: its picture fills the frame at scale 1 and the pan
  // moves it (the renderer's crop/translate), so the box is the frame scaled
  // about its centre, offset by x/y.
  const w = stageW * scale, h = stageH * scale
  const left = (stageW - w) / 2 + (x0 + dx) * k
  const top = (stageH - h) / 2 + (y0 + dy) * k

  const begin = (kind: 'move' | 'scale' | 'rotate', e: React.PointerEvent, hx = 1, hy = 1) => {
    if (keyframed) return
    e.preventDefault(); e.stopPropagation()
    const target = e.currentTarget as HTMLElement
    target.setPointerCapture(e.pointerId)
    drag.current = { kind, x0: e.clientX, y0: e.clientY, sx: hx, sy: hy, scale0, rot0, cx: left + w / 2, cy: top + h / 2 }
  }
  const move = (e: React.PointerEvent) => {
    const d = drag.current
    if (!d) return
    const st = useStore.getState()
    if (d.kind === 'move') {
      st.setLiveTransform({ clipId: clip.id, dx: (e.clientX - d.x0) / k, dy: (e.clientY - d.y0) / k })
    } else if (d.kind === 'scale') {
      // Distance from the centre, relative to the grab point's distance.
      const rect = (e.currentTarget as HTMLElement).closest('.pl-canvas-wrap')?.getBoundingClientRect()
      if (!rect) return
      const px = e.clientX - rect.left, py = e.clientY - rect.top
      const r0 = Math.hypot(d.x0 - (rect.left + d.cx), d.y0 - (rect.top + d.cy)) || 1
      const r1 = Math.hypot(px - d.cx, py - d.cy)
      const s = Math.min(4, Math.max(0.1, d.scale0 * (r1 / r0)))
      st.setLiveTransform({ clipId: clip.id, scale: s })
    } else {
      const rect = (e.currentTarget as HTMLElement).closest('.pl-canvas-wrap')?.getBoundingClientRect()
      if (!rect) return
      const a0 = Math.atan2(d.y0 - (rect.top + d.cy), d.x0 - (rect.left + d.cx))
      const a1 = Math.atan2(e.clientY - (rect.top + d.cy), e.clientX - (rect.left + d.cx))
      let deg = d.rot0 + ((a1 - a0) * 180) / Math.PI
      if (e.shiftKey) deg = Math.round(deg / 15) * 15
      deg = ((deg + 180) % 360 + 360) % 360 - 180
      st.setLiveTransform({ clipId: clip.id, rotation: Math.round(deg * 2) / 2 })
    }
  }
  const end = (e: React.PointerEvent) => {
    const d = drag.current
    drag.current = null
    if (!d) return
    const st = useStore.getState()
    const lv = st.liveTransform
    if (!lv || lv.clipId !== clip.id) { st.setLiveTransform(null); return }
    const args: Record<string, unknown> = { clip_id: clip.id, time: t }
    if (d.kind === 'move') { args.x = Math.round(x0 + (lv.dx ?? 0)); args.y = Math.round(y0 + (lv.dy ?? 0)) }
    else if (d.kind === 'scale') args.scale = Number((lv.scale ?? scale0).toFixed(3))
    else args.rotation = lv.rotation ?? rot0
    const unchanged = (d.kind === 'move' && args.x === Math.round(x0) && args.y === Math.round(y0))
      || (d.kind === 'scale' && args.scale === Number(scale0.toFixed(3))) || (d.kind === 'rotate' && args.rotation === rot0)
    if (unchanged) { st.setLiveTransform(null); return }
    void st.dispatch('set_clip_transform', args)
    ;(e.currentTarget as HTMLElement).releasePointerCapture?.(e.pointerId)
  }

  return (
    <div className={`pl-bbox${keyframed ? ' is-keyframed' : ''}`} style={{ left, top, width: w, height: h, transform: `rotate(${rot}deg)` }}
         title={keyframed ? 'This clip is animated — edit its keyframes in the inspector' : 'Drag to move · corners to scale · the ring to rotate'}
         onPointerDown={(e) => begin('move', e)} onPointerMove={move} onPointerUp={end} onPointerCancel={end}>
      {HANDLES.map((hd) => (
        <span key={hd.id} className="pl-handle" style={{ left: `${hd.x * 100}%`, top: `${hd.y * 100}%`, cursor: keyframed ? 'default' : hd.cursor }}
              onPointerDown={(e) => begin('scale', e, hd.x, hd.y)} onPointerMove={move} onPointerUp={end} onPointerCancel={end} />
      ))}
      <span className="pl-stem" aria-hidden="true" />
      <span className="pl-ring" title="Drag to rotate (⇧ snaps to 15°)" style={{ cursor: keyframed ? 'default' : 'grab' }}
            onPointerDown={(e) => begin('rotate', e)} onPointerMove={move} onPointerUp={end} onPointerCancel={end} />
    </div>
  )
}
