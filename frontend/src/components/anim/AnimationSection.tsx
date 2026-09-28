import React from 'react'
import './anim.css'
import { Icon, type IconName } from '../Icon'
import { useAnimCatalog } from '../../lib/anim/animCatalog'
import {
  durationOf, planOf, poseAt, type AnimFields, type AnimKind, type AnimPresetJson, type AnimTable,
} from '../../lib/anim/clipAnim'
import { useSliderCommit } from '../../lib/useSliderCommit'
import { useReducedMotion } from '../../lib/useReducedMotion'

// The Inspector's Animation section, CapCut's layout (wave E, F1): In | Out |
// Combo tabs, a grid of presets — each tile a small looping preview of what
// the preset does (a static glyph when the viewer asks for reduced motion) —
// and, for In and Out, the animation's duration. A Combo loops over the whole
// clip and replaces In and Out (CapCut); picking an In or Out removes a Combo.
//
// Presets come from the server (lib/anim/animCatalog → edl/clip_animations.py),
// the one table set_animation, the agent and the renderers read. One commit
// per choice (set_animation) — one undo step.

type Send = (tool: string, args: Record<string, unknown>) => unknown

export interface AnimationSectionProps {
  clipId: string
  anim: AnimFields
  /** The clip's on-screen length (seconds): a side is capped at 40 % of it. */
  clipSeconds: number
  send: Send
}

const KINDS: AnimKind[] = ['in', 'out', 'combo']
const TAB_LABEL: Record<AnimKind, string> = { in: 'In', out: 'Out', combo: 'Combo' }
const TAB_ICON: Record<AnimKind, IconName> = { in: 'animIn', out: 'animOut', combo: 'animCombo' }
const FIELD: Record<AnimKind, keyof AnimFields> = { in: 'anim_in', out: 'anim_out', combo: 'anim_combo' }


/** A preview loop long enough to see the preset once, then rest. */
function loopSeconds(p: AnimPresetJson): number {
  if (p.kind !== 'combo') return 1.6
  const hz = Object.values(p.waves).flatMap((w) => w.terms.map((t) => t[1])).filter((h) => h > 0)
  const slow = hz.length ? Math.min(...hz) : 1
  return Math.ceil(1.5 * slow) / slow
}

/** WAAPI keyframes of a preset's pose over its preview loop (the same plan
 *  the renderers draw, from clipAnim — never a hand-made CSS copy). */
export function previewKeyframes(p: AnimPresetJson, frameW = 40, frameH = 24): Keyframe[] {
  const L = loopSeconds(p)
  const fields: AnimFields = p.kind === 'combo' ? { anim_combo: p.id }
    : p.kind === 'in' ? { anim_in: p.id, anim_dur: 0.6 } : { anim_out: p.id, anim_out_dur: 0.6 }
  const plan = planOf(fields, L)
  const n = 32
  const out: Keyframe[] = []
  for (let i = 0; i <= n; i++) {
    const pose = poseAt(plan, (i / n) * L)
    out.push({
      offset: i / n,
      transform: `translate(${(pose.dx * frameW).toFixed(2)}px, ${(pose.dy * frameH).toFixed(2)}px) `
        + `rotate(${pose.rotation.toFixed(2)}deg) scale(${pose.scale.toFixed(4)})`,
      opacity: pose.alpha.toFixed(3),
      filter: `blur(${(pose.blur * 2.5).toFixed(2)}px)`,
    })
  }
  return out
}

function Tile({ preset, still }: { preset: AnimPresetJson | null; still: boolean }) {
  const ref = React.useRef<HTMLSpanElement>(null)
  React.useEffect(() => {
    const el = ref.current
    if (!el || !preset || still || typeof el.animate !== 'function') return
    const a = el.animate(previewKeyframes(preset), { duration: loopSeconds(preset) * 1000, iterations: Infinity })
    return () => a.cancel()
  }, [preset, still])
  if (!preset) {
    return <span className="anim-tile anim-tile-none" aria-hidden="true"><Icon name="voiceNone" /></span>
  }
  if (still) {
    return <span className="anim-tile" aria-hidden="true"><Icon name={preset.icon as IconName} /></span>
  }
  return (
    <span className="anim-tile" aria-hidden="true">
      <span ref={ref} className="anim-tile-picture" data-anim-preview={preset.id} />
    </span>
  )
}

function onRovingKey(e: React.KeyboardEvent<HTMLDivElement>, role: string, select?: (el: HTMLElement) => void) {
  const keys = ['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp', 'Home', 'End']
  if (!keys.includes(e.key)) return
  e.preventDefault()
  e.stopPropagation()
  const items = Array.from(e.currentTarget.querySelectorAll<HTMLElement>(`[role="${role}"]`))
  const at = items.findIndex((b) => b === document.activeElement)
  let next: number
  if (e.key === 'Home') next = 0
  else if (e.key === 'End') next = items.length - 1
  else next = (at + (e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length
  items[next]?.focus()
  if (select && items[next]) select(items[next])
}

function Duration({ kind, clipId, want, clipSeconds, table, send }: {
  kind: 'in' | 'out'; clipId: string; want: number | null | undefined; clipSeconds: number
  table: AnimTable; send: Send
}) {
  const [lo, hi] = table.dur_range
  const cap = Math.max(lo, Math.min(hi, Math.floor(clipSeconds * table.share * 10) / 10))
  const shown = durationOf(want ?? null, clipSeconds)
  const [local, setLocal] = React.useState(shown)
  const dragging = React.useRef(false)
  React.useEffect(() => { if (!dragging.current) setLocal(shown) }, [shown])
  const key = kind === 'in' ? 'in_duration' : 'out_duration'
  const h = useSliderCommit(shown, (v) => { dragging.current = false; void send('set_animation', { clip_id: clipId, [key]: v }) })
  const label = `${TAB_LABEL[kind]} duration`
  return (
    <div className="anim-duration">
      <span className="anim-duration-label" aria-hidden="true">Duration</span>
      <input type="range" min={lo} max={cap} step={0.1} value={Math.min(local, cap)}
        aria-label={label} aria-valuetext={`${local.toFixed(1)} seconds`} disabled={cap <= lo}
        onChange={(e) => { const v = Number(e.target.value); dragging.current = true; setLocal(v); h.change(v) }}
        onPointerUp={(e) => { dragging.current = false; h.onPointerUp(e) }}
        onPointerCancel={() => { dragging.current = false }}
        onKeyUp={h.onKeyUp}
        onBlur={() => { dragging.current = false; h.onBlur() }} />
      <span className="anim-duration-value">{local.toFixed(1)}s</span>
    </div>
  )
}

export function AnimationSection(p: AnimationSectionProps) {
  const cat = useAnimCatalog()
  const uid = React.useId()
  const initial: AnimKind = p.anim.anim_combo ? 'combo' : p.anim.anim_out && !p.anim.anim_in ? 'out' : 'in'
  const [tab, setTab] = React.useState<AnimKind>(initial)
  const still = useReducedMotion()          // live: an OS switch stops the loops (review RE)

  if (cat.status === 'loading') return <p className="anim-note">Loading animations…</p>
  if (cat.status === 'error') {
    return <p className="anim-note anim-error" role="alert">Animations are unavailable: the engine did not answer ({cat.message}).</p>
  }
  const table = cat.table
  const presets = table[tab]
  const active = (p.anim[FIELD[tab]] as string | null | undefined) ?? null
  const choose = (id: string | null) => {
    if (id === active) return
    void p.send('set_animation', { clip_id: p.clipId, [tab]: id ?? 'none' })
  }
  const labelOf = (kind: AnimKind) => {
    const id = p.anim[FIELD[kind]] as string | null | undefined
    return table[kind].find((q) => q.id === id)?.label ?? null
  }
  const selectTab = (el: HTMLElement) => {
    const k = el.dataset.animTab as AnimKind | undefined
    if (k) setTab(k)
  }
  return (
    <div className="anim-section">
      <div className="anim-tabs" role="tablist" aria-label="Animation" onKeyDown={(e) => onRovingKey(e, 'tab', selectTab)}>
        {KINDS.map((k) => {
          const set = labelOf(k)
          return (
            <button key={k} type="button" role="tab" id={`${uid}-tab-${k}`} aria-controls={`${uid}-panel`}
              aria-selected={tab === k} tabIndex={tab === k ? 0 : -1} data-anim-tab={k}
              aria-label={set ? `${TAB_LABEL[k]} (${set})` : TAB_LABEL[k]} onClick={() => setTab(k)}>
              <Icon name={TAB_ICON[k]} /> {TAB_LABEL[k]}
              {set && <span className="anim-tab-dot" aria-hidden="true" />}
            </button>
          )
        })}
      </div>
      <div role="tabpanel" id={`${uid}-panel`} aria-labelledby={`${uid}-tab-${tab}`} className="anim-panel">
        {tab === 'combo' && (p.anim.anim_in || p.anim.anim_out) && (
          <p className="anim-note">A Combo loops over the whole clip and replaces In and Out.</p>
        )}
        {tab !== 'combo' && p.anim.anim_combo && (
          <p className="anim-note">Choosing an {TAB_LABEL[tab]} animation removes the Combo.</p>
        )}
        <div className="anim-presets" role="radiogroup" aria-label={`${TAB_LABEL[tab]} animation`}
          onKeyDown={(e) => onRovingKey(e, 'radio')}>
          {[null, ...presets].map((q) => {
            const id = q?.id ?? null
            const on = active === id
            const focusable = on || (active === null ? id === null : false)
            return (
              <button key={id ?? 'none'} type="button" role="radio" className="anim-preset"
                aria-checked={on} tabIndex={focusable ? 0 : -1} data-anim={id ?? 'none'}
                title={q ? q.hint : `No ${TAB_LABEL[tab]} animation`} onClick={() => choose(id)}>
                <Tile preset={q} still={still} />
                <span className="anim-preset-label">{q ? q.label : 'None'}</span>
              </button>
            )
          })}
        </div>
        {tab !== 'combo' && active && (
          <Duration kind={tab} clipId={p.clipId} clipSeconds={p.clipSeconds} table={table} send={p.send}
            want={tab === 'in' ? p.anim.anim_dur : p.anim.anim_out_dur} />
        )}
      </div>
    </div>
  )
}
