// The 14 collapsible feature groups under Transform (design §2c; brief §3
// "Basic feature inventory"): Blend, Canvas, Stabilize, Enhance image, Reduce
// image noise, Optical flow, AI expand, AI remove, Eye contact, Lip sync,
// Relight, Auto reframe, Remove flickers, Motion blur — in that order, each a
// 34 px row with a checkbox, the name and a caret.
//
// Every group maps to what the engine really does:
//   • Blend / Canvas are the clip's own editors (components/canvas) — the
//     checkbox reflects whether a mode / background is set;
//   • the AI groups open the matching AI tool (lib/aiCatalog's card: a real
//     job with progress, cancel and a result on the timeline). Their checkbox
//     never claims a result the job has not produced (brief §3): it is read-
//     only and stays unchecked until the tool's output is on the clip;
//   • the rest are not in this build and say so, disabled, with the reason.
import { useState } from 'react'
import { useStore } from '../../store'
import { useLayoutStore } from '../../lib/layoutStore'
import type { AnyClip, Track } from '../../types'
import { CanvasSection } from '../canvas/CanvasSection'
import { BlendSection } from '../canvas/BlendSection'
import { Checkbox } from '../ui/Checkbox'
import { Icon } from '../Icon'

type GroupKind = 'blend' | 'canvas' | 'ai' | 'unavailable'
interface Group { key: string; label: string; kind: GroupKind; desc: string; tool?: string; applyAll?: boolean }

const GROUPS: Group[] = [
  { key: 'blend', label: 'Blend', kind: 'blend', desc: 'How this layer mixes with the picture beneath it (overlay clips).', applyAll: true },
  { key: 'canvas', label: 'Canvas', kind: 'canvas', desc: 'What fills the bars of a letterboxed main-track clip: a colour, a blur of the picture or an image.', applyAll: true },
  { key: 'stabilize', label: 'Stabilize', kind: 'ai', tool: 'stabilize', desc: 'Smooth camera shake (two-pass vidstab). Renders on apply.' },
  { key: 'enhance', label: 'Enhance image', kind: 'ai', tool: 'upscale', desc: 'AI upscale (Real-ESRGAN, 2–4×). Renders a new source on apply.' },
  { key: 'denoise', label: 'Reduce image noise', kind: 'unavailable', desc: 'Not available in this build: no video denoiser is shipped. Reduce noise under Audio handles the sound.' },
  { key: 'optical', label: 'Optical flow', kind: 'ai', tool: 'smooth_slow_motion', desc: 'Frame interpolation (RIFE) for smooth slow motion. Renders on apply.' },
  { key: 'expand', label: 'AI expand', kind: 'unavailable', desc: 'Not available in this build: outpainting needs a generative model this build does not ship.' },
  { key: 'remove', label: 'AI remove', kind: 'ai', tool: 'object_erase', desc: 'Erase an object inside a box, tracked across the clip. Renders on apply.' },
  { key: 'eye', label: 'Eye contact', kind: 'unavailable', desc: 'Not available in this build: gaze correction needs a face model this build does not ship.' },
  { key: 'lipsync', label: 'Lip sync', kind: 'unavailable', desc: 'Not available in this build.' },
  { key: 'relight', label: 'Relight', kind: 'unavailable', desc: 'Not available in this build.' },
  { key: 'reframe', label: 'Auto reframe', kind: 'ai', tool: 'auto_reframe', desc: 'Re-crop to the project canvas, following the subject. Applies to the timeline.' },
  { key: 'flicker', label: 'Remove flickers', kind: 'unavailable', desc: 'Not available in this build.' },
  { key: 'mblur', label: 'Motion blur', kind: 'unavailable', desc: 'Not available in this build.' },
]

export function FeatureGroups({ clipId, track, clip }: { clipId: string; track: Track; clip: AnyClip }) {
  const [open, setOpen] = useState<Record<string, boolean>>({})
  const sessionId = useStore((s) => s.sessionId)
  const dispatch = useStore((s) => s.dispatch)
  const jumpToAi = useLayoutStore((s) => s.jumpToAi)
  const isV1 = track.id === 'v1'
  const isOverlay = track.type === 'video' && !isV1
  const blend = (clip as unknown as { blend?: string }).blend
  const canvasBg = (clip as unknown as { canvas_bg?: unknown }).canvas_bg
  const toggle = (k: string) => setOpen((o) => ({ ...o, [k]: !o[k] }))
  return (
    <div className="in-groups">
      {GROUPS.map((g) => {
        const on = g.kind === 'blend' ? !!blend && blend !== 'normal' : g.kind === 'canvas' ? !!canvasBg : false
        const disabledRow = g.kind === 'unavailable' || (g.kind === 'blend' && !isOverlay) || (g.kind === 'canvas' && !isV1)
        const checkTitle = g.kind === 'ai' ? 'Reflects a finished result; run the tool below to produce one'
          : g.kind === 'unavailable' ? g.desc : g.kind === 'blend' && !isOverlay ? 'Blend applies to overlay (picture-in-picture) clips'
          : g.kind === 'canvas' && !isV1 ? 'Canvas applies to main-track clips' : undefined
        return (
          <div key={g.key} className={`in-group${open[g.key] ? ' is-open' : ''}`} data-feature={g.key}>
            <div className="in-group-row">
              <Checkbox checked={on} disabled={disabledRow || g.kind === 'ai'} title={checkTitle}
                        onChange={(next) => {
                          if (g.kind === 'blend') void dispatch('set_blend_mode', { clip_id: clipId, mode: next ? 'multiply' : 'normal' })
                          else if (g.kind === 'canvas') void dispatch('set_canvas_background', { clip_id: clipId, type: next ? 'blur' : 'none', ...(next ? { blur: 2 } : {}) })
                        }} />
              <button type="button" className="in-group-name" aria-expanded={!!open[g.key]} onClick={() => toggle(g.key)}>
                <span>{g.label}</span>
                <Icon name={open[g.key] ? 'chevronUp' : 'chevronDown'} className="in-group-caret" />
              </button>
            </div>
            {open[g.key] && (
              <div className="in-group-body">
                <span className="in-group-desc">{g.desc}</span>
                {g.kind === 'blend' && isOverlay && <BlendSection clipId={clipId} clip={clip} send={dispatch} />}
                {g.kind === 'canvas' && isV1 && <CanvasSection clipId={clipId} clip={clip} sessionId={sessionId} send={dispatch} />}
                {g.kind === 'ai' && g.tool && (
                  <button type="button" className="ui-small-btn is-primary" onClick={() => jumpToAi({ tool: g.tool, from: 'media' })}
                          title="Opens the tool with its options, progress and cancel">Apply…</button>
                )}
                {g.applyAll && !disabledRow && (
                  <button type="button" className="ui-small-btn" title={g.kind === 'canvas' ? 'Put this canvas background on every main-track clip' : 'Put this blend mode on every overlay clip'}
                          onClick={() => {
                            const edl = useStore.getState().edl
                            if (!edl) return
                            if (g.kind === 'canvas') {
                              const bg = canvasBg as Record<string, unknown> | null
                              for (const c of edl.tracks.find((t) => t.id === 'v1')?.clips ?? []) {
                                if (c.id === clipId || !('src' in c)) continue
                                void dispatch('set_canvas_background', { clip_id: c.id, ...(bg ?? { type: 'none' }) })
                              }
                            } else {
                              for (const t of edl.tracks) {
                                if (t.type !== 'video' || t.id === 'v1') continue
                                for (const c of t.clips) if (c.id !== clipId && 'src' in c) void dispatch('set_blend_mode', { clip_id: c.id, mode: blend ?? 'normal' })
                              }
                            }
                          }}>Apply to all</button>
                )}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
