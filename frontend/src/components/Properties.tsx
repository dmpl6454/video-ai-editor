import React from 'react'
import { useStore } from '../store'
import { isMediaClip } from '../types'

function isKeyframed(v: unknown): boolean {
  if (!v || typeof v !== 'object') return false
  const o = v as { keyframes?: unknown[] }
  return Array.isArray(o.keyframes) && o.keyframes.length > 0
}

function asScalar(v: unknown, fallback: number): number {
  if (typeof v === 'number') return v
  if (v && typeof v === 'object') {
    const kfs = (v as { keyframes?: unknown[] }).keyframes
    if (Array.isArray(kfs) && kfs.length) {
      const last = kfs[kfs.length - 1] as [number, number]
      return last?.[1] ?? fallback
    }
  }
  return fallback
}

export function Properties() {
  const edl = useStore((s) => s.edl)
  const sel = useStore((s) => s.selection)
  const dispatch = useStore((s) => s.dispatch)
  // Hooks MUST be called in the same order on every render. Pull playhead
  // here at the top so the count stays stable across the early-return paths
  // below (selecting a clip would otherwise add a hook → React #310).
  const playhead = useStore((s) => s.playhead)
  const setLiveTransform = useStore((s) => s.setLiveTransform)

  if (!sel || !edl) return (
    <div className="props">
      <h2>Properties</h2>
      <div style={{ color: 'var(--text-dim)', fontSize: 12 }}>Select a clip to inspect.</div>
    </div>
  )

  const clip = findClip(edl, sel)
  if (!clip) return (
    <div className="props"><h2>Properties</h2><div style={{ color: 'var(--text-dim)' }}>Clip not found.</div></div>
  )

  const c = clip.c
  if (clip.t.type === 'sticker') {
    return <StickerProps c={c as unknown as StickerLike} dispatch={dispatch} />
  }
  if (!isMediaClip(c)) {
    // Text clip — minimal view
    return (
      <div className="props">
        <h2>Properties</h2>
        <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 8 }}>{clip.t.label} · {c.id}</div>
        <div className="field">
          <label>Text</label>
          <input value={('text' in c ? c.text : '') as string} readOnly />
        </div>
        <div className="row">
          <button onClick={() => dispatch('ripple_delete', { clip_id: c.id })}>Delete</button>
        </div>
      </div>
    )
  }

  // Media clip — full inspector
  const speedRaw = (c as unknown as { speed?: number | null }).speed
  const speed = typeof speedRaw === 'number' ? speedRaw : 1.0
  const audio = (c as unknown as { audio?: { gain_db?: number; fade_in?: number; fade_out?: number; mute?: boolean } }).audio
  const tx = (c as unknown as { transform?: { x?: unknown; y?: unknown; rotation?: unknown; scale?: unknown; opacity?: unknown } }).transform
  const rotation = asScalar(tx?.rotation, 0)
  const scale = asScalar(tx?.scale, 1)
  const opacity = asScalar(tx?.opacity, 1)
  const xVal = asScalar(tx?.x, 0)
  const yVal = asScalar(tx?.y, 0)
  const gain = audio?.gain_db ?? 0
  const fadeIn = audio?.fade_in ?? 0
  const fadeOut = audio?.fade_out ?? 0
  const muted = !!audio?.mute

  const clipStart = (c as unknown as { start?: number }).start ?? 0
  const localT = Math.max(0, playhead - clipStart)
  const KFKey = ({ prop, value, fallback }: { prop: string; value: unknown; fallback: number }) => (
    <button
      title={`${isKeyframed(value) ? 'Add another' : 'Animate'} keyframe at playhead (${localT.toFixed(2)}s in clip)`}
      onClick={() => dispatch('add_keyframe', {
        clip_id: c.id, prop, time: localT, value: asScalar(value, fallback),
      })}
      style={{
        background: isKeyframed(value) ? 'var(--accent)' : 'var(--bg-3)',
        border: '1px solid var(--line)', padding: '0 6px', fontSize: 11,
        borderRadius: 3, cursor: 'pointer', color: 'inherit',
      }}
    >◆</button>
  )

  return (
    <div className="props">
      <h2>Properties</h2>
      <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 8 }} title={c.src}>
        {clip.t.label} · {c.src.split('/').pop()}
      </div>

      <Section label="Timing">
        <div className="row two">
          <div className="field">
            <label>In (s)</label>
            <input type="number" step="0.1" defaultValue={c.in.toFixed(2)}
              onBlur={(e) => dispatch('trim_clip', { clip_id: c.id, in: Number(e.target.value) })} />
          </div>
          <div className="field">
            <label>Out (s)</label>
            <input type="number" step="0.1" defaultValue={c.out.toFixed(2)}
              onBlur={(e) => dispatch('trim_clip', { clip_id: c.id, out: Number(e.target.value) })} />
          </div>
        </div>
        <div className="field">
          <label>Start on timeline (s)</label>
          <input type="number" step="0.1" defaultValue={c.start.toFixed(2)}
            onBlur={(e) => dispatch('move_clip', { clip_id: c.id, new_start: Number(e.target.value) })} />
        </div>
      </Section>

      <Section label="Speed">
        <Slider min={0.25} max={4} step={0.05} value={speed}
          format={(v) => `${v.toFixed(2)}×`}
          onChange={(v) => dispatch('set_speed', { clip_id: c.id, factor: v })} />
      </Section>

      <Section label="Color">
        <ColorPanel clipId={c.id} dispatch={dispatch} />
      </Section>

      <Section label="Audio">
        <Slider min={-30} max={6} step={0.5} value={gain}
          format={(v) => `${v.toFixed(1)} dB`}
          onChange={(v) => dispatch('set_volume', { target: c.id, db: v })} />
        <div className="row two">
          <div className="field">
            <label>Fade in</label>
            <input type="number" step="0.05" min={0} max={5} defaultValue={fadeIn.toFixed(2)}
              onBlur={(e) => dispatch('add_fade', { clip_id: c.id, in_s: Number(e.target.value) })} />
          </div>
          <div className="field">
            <label>Fade out</label>
            <input type="number" step="0.05" min={0} max={5} defaultValue={fadeOut.toFixed(2)}
              onBlur={(e) => dispatch('add_fade', { clip_id: c.id, out_s: Number(e.target.value) })} />
          </div>
        </div>
        <label style={{ fontSize: 11, color: 'var(--text-dim)' }}>
          <input type="checkbox" checked={muted} onChange={() => {
            // Mute = set audio.mute; achieved by toggling clip volume tag.
            // No dedicated mute_clip tool yet; using set_volume to -∞ as proxy:
            dispatch('set_volume', { target: c.id, db: muted ? 0 : -60 })
          }} style={{ marginRight: 4 }} />
          Mute clip
        </label>
      </Section>

      <Section label="Transform">
        <div className="row" style={{ alignItems: 'center', gap: 6 }}>
          <KFKey prop="scale" value={tx?.scale} fallback={1} />
          <Slider min={0.1} max={4} step={0.05} value={scale}
            format={(v) => `scale ${v.toFixed(2)}`}
            onLive={(v) => setLiveTransform({ clipId: c.id, scale: v })}
            onChange={(v) => { setLiveTransform(null); dispatch('set_clip_transform', { clip_id: c.id, scale: v }) }} />
        </div>
        <div className="row" style={{ alignItems: 'center', gap: 6 }}>
          <KFKey prop="rotation" value={tx?.rotation} fallback={0} />
          <Slider min={-180} max={180} step={1} value={rotation}
            format={(v) => `rotation ${v.toFixed(0)}°`}
            onLive={(v) => setLiveTransform({ clipId: c.id, rotation: v })}
            onChange={(v) => { setLiveTransform(null); dispatch('set_clip_transform', { clip_id: c.id, rotation: v }) }} />
        </div>
        <div className="row" style={{ alignItems: 'center', gap: 6 }}>
          <KFKey prop="opacity" value={tx?.opacity} fallback={1} />
          <Slider min={0} max={1} step={0.05} value={opacity}
            format={(v) => `opacity ${v.toFixed(2)}`}
            onLive={(v) => setLiveTransform({ clipId: c.id, opacity: v })}
            onChange={(v) => { setLiveTransform(null); dispatch('set_clip_transform', { clip_id: c.id, opacity: v }) }} />
        </div>
        <div className="row" style={{ alignItems: 'center', gap: 6 }}>
          <KFKey prop="x" value={tx?.x} fallback={xVal} />
          <span style={{ fontSize: 10, color: 'var(--text-dim)', minWidth: 80 }}>
            x: {xVal.toFixed(0)} {isKeyframed(tx?.x) ? '· animated' : ''}
          </span>
          <KFKey prop="y" value={tx?.y} fallback={yVal} />
          <span style={{ fontSize: 10, color: 'var(--text-dim)' }}>
            y: {yVal.toFixed(0)} {isKeyframed(tx?.y) ? '· animated' : ''}
          </span>
        </div>
      </Section>

      <div className="row" style={{ marginTop: 8 }}>
        <button onClick={() => dispatch('duplicate_clip', { clip_id: c.id })}>Duplicate</button>
        <button onClick={() => dispatch('ripple_delete', { clip_id: c.id })}>Delete</button>
      </div>
    </div>
  )
}

interface StickerLike {
  id: string
  label?: string | null
  start: number
  end: number
  transform?: { x?: unknown; y?: unknown; scale?: unknown; rotation?: unknown; opacity?: unknown }
}

function StickerProps({ c, dispatch }: {
  c: StickerLike
  dispatch: ReturnType<typeof useStore.getState>['dispatch']
}) {
  const tx = c.transform ?? {}
  const x = asScalar(tx.x, 0), y = asScalar(tx.y, 0)
  const scale = asScalar(tx.scale, 1)
  const rotation = asScalar(tx.rotation, 0)
  const opacity = asScalar(tx.opacity, 1)
  const start = c.start ?? 0
  const duration = Math.max(0.1, (c.end ?? start + 3) - start)
  const setTx = (p: Record<string, number>) => dispatch('set_clip_transform', { clip_id: c.id, ...p })
  const setTiming = (p: { start?: number; end?: number }) =>
    dispatch('set_clip_timing', { clip_id: c.id, ...p })

  return (
    <div className="props">
      <h2>Properties</h2>
      <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 8 }}>
        {c.label ? `${c.label} ` : ''}Sticker · {c.id}
      </div>

      <Section label="Position">
        {/* keys force the input to re-seed when canvas drag changes x/y */}
        <div className="row two">
          <div className="field">
            <label>X</label>
            <input type="number" key={`x${Math.round(x)}`} defaultValue={Math.round(x)}
              onBlur={(e) => setTx({ x: Number(e.target.value) })} />
          </div>
          <div className="field">
            <label>Y</label>
            <input type="number" key={`y${Math.round(y)}`} defaultValue={Math.round(y)}
              onBlur={(e) => setTx({ y: Number(e.target.value) })} />
          </div>
        </div>
      </Section>

      <Section label="Transform">
        <Slider min={0.1} max={4} step={0.05} value={scale}
          format={(v) => `scale ${v.toFixed(2)}`} onChange={(v) => setTx({ scale: v })} />
        <Slider min={-180} max={180} step={1} value={rotation}
          format={(v) => `rotation ${v.toFixed(0)}°`} onChange={(v) => setTx({ rotation: v })} />
        <Slider min={0} max={1} step={0.05} value={opacity}
          format={(v) => `opacity ${v.toFixed(2)}`} onChange={(v) => setTx({ opacity: v })} />
      </Section>

      <Section label="Timing">
        <div className="row two">
          <div className="field">
            <label>Start (s)</label>
            <input type="number" step="0.1" key={`s${start.toFixed(2)}`} defaultValue={start.toFixed(2)}
              onBlur={(e) => { const ns = Math.max(0, Number(e.target.value)); setTiming({ start: ns, end: ns + duration }) }} />
          </div>
          <div className="field">
            <label>Duration (s)</label>
            <input type="number" step="0.1" min={0.1} key={`d${duration.toFixed(2)}`} defaultValue={duration.toFixed(2)}
              onBlur={(e) => { const nd = Math.max(0.1, Number(e.target.value)); setTiming({ end: start + nd }) }} />
          </div>
        </div>
      </Section>

      <div className="row" style={{ marginTop: 8 }}>
        <button onClick={() => dispatch('ripple_delete', { clip_id: c.id })}>Delete</button>
      </div>
    </div>
  )
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginTop: 10 }}>
      <div style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: 0.08 * 10 + 'em',
                    color: 'var(--text-dim)', margin: '8px 0 4px' }}>{label}</div>
      {children}
    </div>
  )
}

function Slider({ label, min, max, step, value, onChange, onLive, format }: {
  label?: string; min: number; max: number; step: number; value: number;
  onChange: (v: number) => void;        // committed value — dispatched to server
  onLive?: (v: number) => void;         // live value during drag — client-side only
  format?: (v: number) => string;       // optional live label formatter
}) {
  // Commit-on-release: the thumb + label track every drag tick locally (0ms),
  // but the server `onChange` fires ONCE on pointer-up / blur / key-release.
  // Dragging used to fire a dispatch + full preview render per tick — dozens of
  // HTTP round-trips and render jobs for one gesture. Now it's exactly one.
  const [local, setLocal] = React.useState(value)
  const dragging = React.useRef(false)
  // Keep local in sync when the prop changes from outside (undo, chat, etc.)
  // but never stomp the value mid-drag.
  React.useEffect(() => { if (!dragging.current) setLocal(value) }, [value])

  const commit = (v: number) => { if (v !== value) onChange(v) }
  return (
    <div className="row" style={{ alignItems: 'center', gap: 6 }}>
      <input type="range" min={min} max={max} step={step} value={local}
        onChange={(e) => {
          const v = Number(e.target.value)
          dragging.current = true
          setLocal(v)
          onLive?.(v)
        }}
        onPointerUp={(e) => { dragging.current = false; commit(Number((e.target as HTMLInputElement).value)) }}
        onPointerCancel={() => { dragging.current = false }}
        onKeyUp={(e) => { dragging.current = false; commit(Number((e.target as HTMLInputElement).value)) }}
        onBlur={(e) => { dragging.current = false; commit(Number((e.target as HTMLInputElement).value)) }}
        style={{ flex: 1 }} />
      <span style={{ fontSize: 10, color: 'var(--text-dim)', minWidth: 70, textAlign: 'right' }}>
        {format ? format(local) : label}
      </span>
    </div>
  )
}

function findClip(edl: ReturnType<typeof useStore.getState>['edl'], id: string) {
  if (!edl) return null
  for (const t of edl.tracks) {
    for (const c of t.clips) {
      if (c.id === id) return { t, c }
    }
  }
  return null
}

function ColorPanel({ clipId, dispatch }: {
  clipId: string;
  dispatch: ReturnType<typeof useStore.getState>['dispatch'];
}) {
  // Local sliders for shadows / mids / highlights gain + temp/tint. The
  // commit-on-release pattern (debounced via onMouseUp) avoids dispatching
  // a new effect for every pixel of slider drag.
  const commit = (params: Record<string, number>) => {
    // We add a single new color effect each time. The renderer evaluates the
    // chain in order, so the latest one wins for non-additive properties. For
    // an even cleaner UX we'd dedupe — left for a follow-up.
    dispatch('color_grade', { clip_id: clipId, ...params })
  }
  return (
    <>
      <ColorSlider label="Brightness" min={-0.5} max={0.5} step={0.02} commit={(v) => commit({ brightness: v })}
        format={(v) => `${v >= 0 ? '+' : ''}${v.toFixed(2)}`} />
      <ColorSlider label="Contrast"   min={0.5}  max={2.0} step={0.02} commit={(v) => commit({ contrast: v })} init={1}
        format={(v) => `${v.toFixed(2)}×`} />
      <ColorSlider label="Saturation" min={0}    max={3.0} step={0.02} commit={(v) => commit({ saturation: v })} init={1}
        format={(v) => `${v.toFixed(2)}×`} />
      <ColorSlider label="Temp"       min={-1}   max={1}   step={0.02} commit={(v) => commit({ temp: v })}
        format={(v) => `${v >= 0 ? '+' : ''}${Math.round(v * 100)}`} />
      <ColorSlider label="Tint"       min={-1}   max={1}   step={0.02} commit={(v) => commit({ tint: v })}
        format={(v) => `${v >= 0 ? '+' : ''}${v.toFixed(2)}`} />
    </>
  )
}

function ColorSlider({ label, min, max, step, commit, init = 0, format }: {
  label: string; min: number; max: number; step: number;
  commit: (v: number) => void; init?: number; format?: (v: number) => string;
}) {
  // Controlled so the value readout tracks the thumb live; commit only fires on
  // release (mouse up / touch end / key up / blur) so we don't dispatch hundreds
  // of effect chains while dragging.
  const [local, setLocal] = React.useState(init)
  const release = (e: { target: EventTarget | null }) =>
    commit(Number((e.target as HTMLInputElement).value))
  return (
    <div className="row" style={{ alignItems: 'center', gap: 6 }}>
      <span style={{ fontSize: 10, color: 'var(--text-dim)', minWidth: 64 }}>{label}</span>
      <input type="range" min={min} max={max} step={step} value={local}
        onChange={(e) => setLocal(Number(e.target.value))}
        onMouseUp={release}
        onTouchEnd={release}
        onKeyUp={release}
        onBlur={release}
        style={{ flex: 1 }} />
      <span style={{ fontSize: 10, color: 'var(--text)', minWidth: 46, textAlign: 'right',
                     fontVariantNumeric: 'tabular-nums' }}>
        {format ? format(local) : local.toFixed(2)}
      </span>
    </div>
  )
}
