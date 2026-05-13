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
        <div className="row">
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
        <Slider label={`${speed.toFixed(2)}×`} min={0.25} max={4} step={0.05} value={speed}
          onChange={(v) => dispatch('set_speed', { clip_id: c.id, factor: v })} />
      </Section>

      <Section label="Color">
        <ColorPanel clipId={c.id} dispatch={dispatch} />
      </Section>

      <Section label="Audio">
        <Slider label={`${gain.toFixed(1)} dB`} min={-30} max={6} step={0.5} value={gain}
          onChange={(v) => dispatch('set_volume', { target: c.id, db: v })} />
        <div className="row">
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
          <Slider label={`scale ${scale.toFixed(2)}`} min={0.1} max={4} step={0.05} value={scale}
            onChange={(v) => dispatch('set_clip_transform', { clip_id: c.id, scale: v })} />
        </div>
        <div className="row" style={{ alignItems: 'center', gap: 6 }}>
          <KFKey prop="rotation" value={tx?.rotation} fallback={0} />
          <Slider label={`rotation ${rotation.toFixed(0)}°`} min={-180} max={180} step={1} value={rotation}
            onChange={(v) => dispatch('set_clip_transform', { clip_id: c.id, rotation: v })} />
        </div>
        <div className="row" style={{ alignItems: 'center', gap: 6 }}>
          <KFKey prop="opacity" value={tx?.opacity} fallback={1} />
          <Slider label={`opacity ${opacity.toFixed(2)}`} min={0} max={1} step={0.05} value={opacity}
            onChange={(v) => dispatch('set_clip_transform', { clip_id: c.id, opacity: v })} />
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

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginTop: 10 }}>
      <div style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: 0.08 * 10 + 'em',
                    color: 'var(--text-dim)', margin: '8px 0 4px' }}>{label}</div>
      {children}
    </div>
  )
}

function Slider({ label, min, max, step, value, onChange }: {
  label: string; min: number; max: number; step: number; value: number;
  onChange: (v: number) => void;
}) {
  return (
    <div className="row" style={{ alignItems: 'center', gap: 6 }}>
      <input type="range" min={min} max={max} step={step} value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        style={{ flex: 1 }} />
      <span style={{ fontSize: 10, color: 'var(--text-dim)', minWidth: 70, textAlign: 'right' }}>
        {label}
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
      <ColorSlider label="Brightness" min={-0.5} max={0.5} step={0.02} commit={(v) => commit({ brightness: v })} />
      <ColorSlider label="Contrast"   min={0.5}  max={2.0} step={0.02} commit={(v) => commit({ contrast: v })} init={1} />
      <ColorSlider label="Saturation" min={0}    max={3.0} step={0.02} commit={(v) => commit({ saturation: v })} init={1} />
      <ColorSlider label="Temp"       min={-1}   max={1}   step={0.02} commit={(v) => commit({ temp: v })} />
      <ColorSlider label="Tint"       min={-1}   max={1}   step={0.02} commit={(v) => commit({ tint: v })} />
    </>
  )
}

function ColorSlider({ label, min, max, step, commit, init = 0 }: {
  label: string; min: number; max: number; step: number;
  commit: (v: number) => void; init?: number;
}) {
  // Slider that reports only on release (mouse up / touch end / blur) so we
  // don't dispatch hundreds of effect chains while dragging.
  return (
    <div className="row" style={{ alignItems: 'center', gap: 6 }}>
      <span style={{ fontSize: 10, color: 'var(--text-dim)', minWidth: 70 }}>{label}</span>
      <input type="range" min={min} max={max} step={step} defaultValue={init}
        onMouseUp={(e) => commit(Number((e.target as HTMLInputElement).value))}
        onTouchEnd={(e) => commit(Number((e.target as HTMLInputElement).value))}
        onBlur={(e) => commit(Number((e.target as HTMLInputElement).value))}
        style={{ flex: 1 }} />
    </div>
  )
}
