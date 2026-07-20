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
      <div style={{ color: 'var(--text-dim)', fontSize: 12, lineHeight: 1.8 }}>
        Nothing selected.
        <br />• Click a clip on the timeline to edit it here
        <br />• Drag a selected clip's edges to trim it
        <br />• ⌘B splits the clip at the playhead
      </div>
    </div>
  )

  const clip = findClip(edl, sel)
  if (!clip) return (
    <div className="props"><h2>Properties</h2><div style={{ color: 'var(--text-dim)' }}>Clip not found.</div></div>
  )

  const c = clip.c
  if (clip.t.type === 'sticker') {
    return (
      <StickerProps
        c={c as unknown as StickerLike}
        trackLabel={clip.t.label ?? clip.t.id}
        dispatch={dispatch}
      />
    )
  }
  if (!isMediaClip(c)) {
    // Text clip (sticker tracks were handled above) — full editable inspector.
    return (
      <TextProps
        c={c as unknown as TextClipLike}
        trackLabel={clip.t.label ?? clip.t.id}
        canvas={edl.canvas}
        dispatch={dispatch}
      />
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

  const effects = (c as unknown as { effects?: { type: string; params?: Record<string, number> }[] }).effects
  const colorEffect = effects?.find((e) => e.type === 'color' || e.type === 'color_grade')
  const colorParams = colorEffect?.params ?? {}

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

      <Section label="Speed" onReset={() => dispatch('set_speed', { clip_id: c.id, factor: 1 })}>
        <Slider min={0.25} max={4} step={0.05} value={speed}
          format={(v) => `${v.toFixed(2)}×`}
          onChange={(v) => dispatch('set_speed', { clip_id: c.id, factor: v })} />
      </Section>

      <Section label="Color" onReset={() => dispatch('color_grade', {
        clip_id: c.id, brightness: 0, contrast: 1, saturation: 1, temp: 0, tint: 0,
      })}>
        <ColorPanel clipId={c.id} dispatch={dispatch} current={colorParams} />
      </Section>

      <Section label="Audio" onReset={() => {
        dispatch('set_volume', { target: c.id, db: 0 })
        dispatch('add_fade', { clip_id: c.id, in_s: 0, out_s: 0 })
      }}>
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

      <Section label="Transform" onReset={() => dispatch('set_clip_transform', {
        clip_id: c.id, x: 0, y: 0, scale: 1, rotation: 0, opacity: 1,
      })}>
        <div className="row" style={{ alignItems: 'center', gap: 6 }}>
          <KFKey prop="scale" value={tx?.scale} fallback={1} />
          <Slider min={0.1} max={4} step={0.05} value={scale}
            format={(v) => `scale ${v.toFixed(2)}`}
            onLive={(v) => setLiveTransform({ clipId: c.id, scale: v })}
            onChange={(v) => dispatch('set_clip_transform', { clip_id: c.id, scale: v })} />
        </div>
        <div className="row" style={{ alignItems: 'center', gap: 6 }}>
          <KFKey prop="rotation" value={tx?.rotation} fallback={0} />
          <Slider min={-180} max={180} step={1} value={rotation}
            format={(v) => `rotation ${v.toFixed(0)}°`}
            onLive={(v) => setLiveTransform({ clipId: c.id, rotation: v })}
            onChange={(v) => dispatch('set_clip_transform', { clip_id: c.id, rotation: v })} />
        </div>
        <div className="row" style={{ alignItems: 'center', gap: 6 }}>
          <KFKey prop="opacity" value={tx?.opacity} fallback={1} />
          <Slider min={0} max={1} step={0.05} value={opacity}
            format={(v) => `opacity ${v.toFixed(2)}`}
            onLive={(v) => setLiveTransform({ clipId: c.id, opacity: v })}
            onChange={(v) => dispatch('set_clip_transform', { clip_id: c.id, opacity: v })} />
        </div>
        <div className="row" style={{ alignItems: 'center', gap: 6 }}>
          <KFKey prop="x" value={tx?.x} fallback={xVal} />
          <label style={{ fontSize: 10, color: 'var(--text-dim)', minWidth: 80, display: 'flex', alignItems: 'center', gap: 4 }}>
            x:
            <input type="number" key={`mx${xVal.toFixed(0)}`} defaultValue={xVal.toFixed(0)}
              style={{ width: 56 }}
              onBlur={(e) => dispatch('set_clip_transform', { clip_id: c.id, x: Number(e.target.value) })} />
            {isKeyframed(tx?.x) ? '· animated' : ''}
          </label>
          <KFKey prop="y" value={tx?.y} fallback={yVal} />
          <label style={{ fontSize: 10, color: 'var(--text-dim)', display: 'flex', alignItems: 'center', gap: 4 }}>
            y:
            <input type="number" key={`my${yVal.toFixed(0)}`} defaultValue={yVal.toFixed(0)}
              style={{ width: 56 }}
              onBlur={(e) => dispatch('set_clip_transform', { clip_id: c.id, y: Number(e.target.value) })} />
            {isKeyframed(tx?.y) ? '· animated' : ''}
          </label>
        </div>
      </Section>

      <div className="row" style={{ marginTop: 8 }}>
        <button
          title="Add a copy of this clip right after it (⌘D)"
          onClick={() => dispatch('duplicate_clip', { clip_id: c.id })}
        >Duplicate</button>
        <button
          title="Remove this clip and close the gap (⌫)"
          onClick={() => dispatch('ripple_delete', { clip_id: c.id })}
        >Delete</button>
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

function StickerProps({ c, trackLabel, dispatch }: {
  c: StickerLike
  trackLabel: string
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
        {trackLabel} · {c.label ? `${c.label} ` : ''}Sticker · {c.id}
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
        <Slider label min={0.1} max={4} step={0.05} value={scale}
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
        <button
          title="Remove this overlay from the timeline (⌫)"
          onClick={() => dispatch('ripple_delete', { clip_id: c.id })}
        >Delete</button>
      </div>
    </div>
  )
}

// The backend TextClip schema (edl/schema.py) always carries style/transform
// (pydantic default factories), so the dotted set_property paths below always
// resolve — including on auto_caption's caption cues. types.ts's TextClip
// interface deliberately omits transform ("M1 frontend ignores transform"),
// hence the local cast shape.
interface TextClipLike {
  id: string
  text: string
  start: number
  end: number
  role?: string | null
  style?: { font?: string; size?: number; color?: string }
  transform?: { x?: unknown; y?: unknown }
}

function TextProps({ c, trackLabel, canvas, dispatch }: {
  c: TextClipLike
  trackLabel: string
  canvas: { w: number; h: number }
  dispatch: ReturnType<typeof useStore.getState>['dispatch']
}) {
  const x = asScalar(c.transform?.x, canvas.w / 2)
  const y = asScalar(c.transform?.y, canvas.h * 0.85)
  const size = c.style?.size ?? 96
  const rawColor = c.style?.color ?? '#FFFFFF'
  // <input type=color> only speaks #rrggbb — drop an alpha suffix if present.
  const color = /^#[0-9a-fA-F]{6}/.test(rawColor) ? rawColor.slice(0, 7) : '#ffffff'
  const start = c.start
  const end = c.end

  // Editing an existing TextClip = `set_property` (dispatch.py's generic
  // dotted-path mutator): paths `text`, `style.size`, `style.color`,
  // `transform.x`, `transform.y`. Timing goes through `set_clip_timing`
  // instead — it enforces end > start (clamps to a 0.1s minimum span) and
  // re-sorts the track, which a raw set_property on start/end would skip.
  const setProp = (path: string, value: unknown) =>
    dispatch('set_property', { clip_id: c.id, path, value })
  const setTiming = (p: { start?: number; end?: number }) =>
    dispatch('set_clip_timing', { clip_id: c.id, ...p })

  const commitText = (v: string) => {
    // Skip blank commits — an empty TextClip renders nothing everywhere and
    // is only recoverable through this same (now-empty-looking) inspector.
    if (v.trim() && v !== c.text) void setProp('text', v)
  }
  // Shared number-commit guard: NaN-proof, optional floor, and same-value
  // skip against the SEEDED display value (mirrors Slider's `commit` guard)
  // so a focus-then-blur without edits never appends a junk op to history.
  const commitNumber = (path: string, raw: string, seeded: number, min?: number) => {
    const n = Number(raw)
    if (!Number.isFinite(n)) return
    const v = min != null ? Math.max(min, n) : n
    if (v !== seeded) void setProp(path, v)
  }

  return (
    <div className="props">
      <h2>Properties</h2>
      <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 8 }}>
        {trackLabel} · {c.role ?? 'default'} · {c.id}
      </div>

      <Section label="Text">
        {/* Uncontrolled + key-seeded like the sticker inputs: typing stays
            local; commit fires once on blur (or Cmd/Ctrl+Enter, routed
            through blur so there's a single commit path); an external change
            (chat edit, undo) re-seeds via the key. */}
        <textarea
          key={`t${c.id}:${c.text}`}
          defaultValue={c.text}
          rows={3}
          onBlur={(e) => commitText(e.target.value)}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
              e.preventDefault()
              ;(e.target as HTMLTextAreaElement).blur()
            }
          }}
          style={{ width: '100%', resize: 'vertical', fontSize: 12,
                   fontFamily: 'inherit', boxSizing: 'border-box' }}
        />
      </Section>

      <Section label="Style">
        <div className="row two">
          <div className="field">
            <label>Size (px)</label>
            <input type="number" step="1" min={8} key={`sz${size}`} defaultValue={size}
              onBlur={(e) => commitNumber('style.size', e.target.value, size, 8)} />
          </div>
          <div className="field">
            <label>Color</label>
            <input type="color" key={`c${color}`} defaultValue={color}
              title="Text fill color (#ffffff means: use the role's preset style)"
              onBlur={(e) => { const v = e.target.value; if (v !== color) void setProp('style.color', v) }}
              style={{ width: '100%', padding: 0, height: 24 }} />
          </div>
        </div>
      </Section>

      <Section label={`Position (canvas px, ${canvas.w}×${canvas.h})`}>
        {/* TextClip transform x/y are ABSOLUTE CANVAS PIXELS (clip centre),
            not relative units — 540/1700 is bottom-centre on a 1080×1920. */}
        <div className="row two">
          <div className="field">
            <label>X</label>
            <input type="number" key={`x${Math.round(x)}`} defaultValue={Math.round(x)}
              onBlur={(e) => commitNumber('transform.x', e.target.value, Math.round(x))} />
          </div>
          <div className="field">
            <label>Y</label>
            <input type="number" key={`y${Math.round(y)}`} defaultValue={Math.round(y)}
              onBlur={(e) => commitNumber('transform.y', e.target.value, Math.round(y))} />
          </div>
        </div>
      </Section>

      <Section label="Timing">
        <div className="row two">
          <div className="field">
            <label>Start (s)</label>
            <input type="number" step="0.1" min={0} key={`s${start.toFixed(2)}`} defaultValue={start.toFixed(2)}
              onBlur={(e) => {
                const n = Number(e.target.value)
                if (Number.isFinite(n) && n !== start) void setTiming({ start: Math.max(0, n) })
              }} />
          </div>
          <div className="field">
            <label>End (s)</label>
            <input type="number" step="0.1" key={`e${end.toFixed(2)}`} defaultValue={end.toFixed(2)}
              onBlur={(e) => {
                const n = Number(e.target.value)
                if (Number.isFinite(n) && n !== end) void setTiming({ end: n })
              }} />
          </div>
        </div>
      </Section>

      <div className="row" style={{ marginTop: 8 }}>
        <button
          title="Remove this overlay from the timeline (⌫)"
          onClick={() => dispatch('ripple_delete', { clip_id: c.id })}
        >Delete</button>
      </div>
    </div>
  )
}

function Section({ label, children, onReset }: {
  label: string; children: React.ReactNode; onReset?: () => void;
}) {
  return (
    <div style={{ marginTop: 10 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                    margin: '8px 0 4px' }}>
        <div style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: 0.08 * 10 + 'em',
                      color: 'var(--text-dim)' }}>{label}</div>
        {onReset && (
          <button
            onClick={onReset}
            title={`Reset ${label.toLowerCase()} to default`}
            style={{
              fontSize: 10, padding: '1px 6px', background: 'transparent',
              border: '1px solid var(--line)', borderRadius: 3, color: 'var(--text-dim)',
              cursor: 'pointer',
            }}
          >Reset</button>
        )}
      </div>
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

function ColorPanel({ clipId, dispatch, current }: {
  clipId: string;
  dispatch: ReturnType<typeof useStore.getState>['dispatch'];
  current: Record<string, number>;
}) {
  // Local sliders for shadows / mids / highlights gain + temp/tint. The
  // commit-on-release pattern (debounced via onPointerUp) avoids dispatching
  // a new effect for every pixel of slider drag. The backend merges each
  // commit into the clip's single "color" effect (dispatch.py color_grade),
  // so repeated adjustments settle on a final value instead of stacking.
  const setLiveFilter = useStore((s) => s.setLiveFilter)
  const commit = (params: Record<string, number>) => {
    dispatch('color_grade', { clip_id: clipId, ...params })
  }
  // Live CSS preview during a drag (the Color mirror of liveTransform). The
  // filter always carries all three mappable params seeded from the clip's
  // CURRENT grade — with the dragged one overriding — so dragging one slider
  // doesn't visually drop another's just-committed value while that value's
  // re-render is still in flight. Values stay in eq-param space; Preview.tsx
  // converts to CSS.
  const live = (p: { brightness?: number; contrast?: number; saturation?: number }) =>
    setLiveFilter({
      clipId,
      brightness: current.brightness ?? 0,
      contrast: current.contrast ?? 1,
      saturation: current.saturation ?? current.sat ?? 1,
      ...p,
    })
  return (
    <>
      <ColorSlider label="Brightness" min={-0.5} max={0.5} step={0.02} commit={(v) => commit({ brightness: v })}
        onLive={(v) => live({ brightness: v })}
        value={current.brightness} init={0}
        format={(v) => `${v >= 0 ? '+' : ''}${v.toFixed(2)}`} />
      <ColorSlider label="Contrast"   min={0.5}  max={2.0} step={0.02} commit={(v) => commit({ contrast: v })}
        onLive={(v) => live({ contrast: v })}
        value={current.contrast} init={1}
        format={(v) => `${v.toFixed(2)}×`} />
      <ColorSlider label="Saturation" min={0}    max={3.0} step={0.02} commit={(v) => commit({ saturation: v })}
        onLive={(v) => live({ saturation: v })}
        value={current.saturation ?? current.sat} init={1}
        format={(v) => `${v.toFixed(2)}×`} />
      {/* Temp/Tint stay commit-only (no onLive): the backend maps them to
          band-weighted colorbalance on midtones (render/effects.py), which
          CSS filter() has no faithful equivalent for — a wrong live preview
          would be worse than none. */}
      <ColorSlider label="Temp"       min={-1}   max={1}   step={0.02} commit={(v) => commit({ temp: v })}
        value={current.temp} init={0}
        format={(v) => `${v >= 0 ? '+' : ''}${Math.round(v * 100)}`} />
      <ColorSlider label="Tint"       min={-1}   max={1}   step={0.02} commit={(v) => commit({ tint: v })}
        value={current.tint} init={0}
        format={(v) => `${v >= 0 ? '+' : ''}${v.toFixed(2)}`} />
    </>
  )
}

function ColorSlider({ label, min, max, step, commit, onLive, value, init = 0, format }: {
  label: string; min: number; max: number; step: number;
  commit: (v: number) => void;          // committed value — dispatched to server
  onLive?: (v: number) => void;         // live value during drag — client-side only
  value?: number; init?: number; format?: (v: number) => string;
}) {
  // Controlled so the value readout tracks the thumb live; commit only fires
  // on release. `onPointerUp` (not `onMouseUp`) is the reliable cross-input
  // release event — mouse-only handlers can silently miss a release on some
  // touch/pen/trackpad interactions, which used to mean the color change
  // never got dispatched at all ("brightness works only sometimes").
  const seeded = value ?? init
  const [local, setLocal] = React.useState(seeded)
  const dragging = React.useRef(false)
  // Re-seed from the stored value when it changes from outside (switching
  // clips, undo/redo, chat edits) — but never mid-drag.
  React.useEffect(() => { if (!dragging.current) setLocal(seeded) }, [seeded])

  const release = (e: { target: EventTarget | null }) => {
    dragging.current = false
    // Same-value guard (mirrors Slider's `commit`): release fires from
    // pointerup AND the later blur — without the guard the blur re-commits
    // the identical value, appending a junk op to undo history.
    const v = Number((e.target as HTMLInputElement).value)
    if (v !== seeded) commit(v)
  }
  return (
    <div className="row" style={{ alignItems: 'center', gap: 6 }}>
      <span style={{ fontSize: 10, color: 'var(--text-dim)', minWidth: 64 }}>{label}</span>
      <input type="range" min={min} max={max} step={step} value={local}
        onChange={(e) => {
          const v = Number(e.target.value)
          dragging.current = true
          setLocal(v)
          onLive?.(v)
        }}
        onPointerUp={release}
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
