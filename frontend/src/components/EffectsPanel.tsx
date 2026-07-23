import { useEffect, useRef, useState } from 'react'
import { useStore } from '../store'
import { api } from '../api'
import { isMediaClip, clipEnd, type Clip } from '../types'
import './EffectsPanel.css'

// The backend Effect model isn't declared on types.ts's Clip ("M1 frontend
// ignores transform/effects/etc.") — read it via a cast, same pattern as
// Properties.tsx.
interface EffectEntry {
  type: string
  params?: Record<string, unknown>
}

// Curated effect presets — one per builder in render/effects.py, with that
// builder's own default params passed explicitly so the applied chain is
// self-describing. `color`/`color_grade` are deliberately skipped (the
// Properties panel already has grading sliders) and `lut` has its own section.
const EFFECT_PRESETS: { type: string; label: string; icon: string; params: Record<string, unknown>; hint: string }[] = [
  { type: 'blur',      label: 'Blur',      icon: '🌫️', params: { radius: 8 },     hint: 'Gaussian blur (radius 8)' },
  { type: 'sharpen',   label: 'Sharpen',   icon: '🔪', params: { amount: 1.0 },   hint: 'Unsharp mask (amount 1.0)' },
  { type: 'vignette',  label: 'Vignette',  icon: '🌒', params: {},                hint: 'Darkened corners' },
  { type: 'grain',     label: 'Grain',     icon: '🎞️', params: { strength: 20 },  hint: 'Film grain noise (strength 20)' },
  { type: 'vintage',   label: 'Vintage',   icon: '📸', params: {},                hint: 'Warm faded look with grain + vignette' },
  { type: 'vhs',       label: 'VHS',       icon: '📼', params: {},                hint: 'Desaturated, noisy tape look' },
  { type: 'glow',      label: 'Glow',      icon: '✨', params: { strength: 0.4 }, hint: 'Soft glow / bloom (strength 0.4)' },
  { type: 'rgb_split', label: 'RGB Split', icon: '🔴', params: { offset: 6 },     hint: 'Chromatic aberration (offset 6px)' },
  { type: 'hflip',     label: 'Flip H',    icon: '↔️', params: {},                hint: 'Mirror horizontally' },
  { type: 'vflip',     label: 'Flip V',    icon: '↕️', params: {},                hint: 'Mirror vertically' },
]

// Friendly names for chips of effects that can arrive via chat/MCP too.
const EFFECT_LABELS: Record<string, string> = {
  blur: 'Blur', sharpen: 'Sharpen', vignette: 'Vignette', grain: 'Grain',
  vintage: 'Vintage', vhs: 'VHS', glow: 'Glow', rgb_split: 'RGB Split',
  hflip: 'Flip H', vflip: 'Flip V', color: 'Color', color_grade: 'Color Grade',
}

function baseName(path: string): string {
  // Handles both POSIX and Windows separators (params.src is an absolute path).
  const parts = path.split(/[\\/]/)
  return parts[parts.length - 1] ?? ''
}

function lutDisplayName(fileOrPath: string): string {
  const stem = baseName(fileOrPath).replace(/\.cube$/i, '')
  return stem
    .split(/[_-]+/)
    .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
    .join(' ')
}

function chipLabel(e: EffectEntry): string {
  if (e.type === 'lut') return `LUT · ${lutDisplayName(String(e.params?.src ?? ''))}`
  return EFFECT_LABELS[e.type] ?? e.type
}

export function EffectsPanel() {
  const sid = useStore((s) => s.sessionId)
  const edl = useStore((s) => s.edl)
  const selection = useStore((s) => s.selection)
  const dispatch = useStore((s) => s.dispatch)

  const [open, setOpen] = useState(false)
  const [luts, setLuts] = useState<string[] | null>(null)
  const [filters, setFilters] = useState<string[] | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  // Resolve the target clip. Selection wins; with nothing selected, fall
  // back to the v1 clip under the playhead, then the first v1 clip — the
  // CapCut model. A selection-required grid that silently disables read as
  // "none of the effects work" in user testing. Effects only apply to MEDIA
  // clips (backend rejects text/sticker targets).
  const playhead = useStore((s) => s.playhead)
  let clip: Clip | null = null
  let targetIsFallback = false
  for (const t of edl?.tracks ?? []) {
    for (const c of t.clips) {
      if (c.id === selection && isMediaClip(c)) clip = c
    }
  }
  if (!clip) {
    const v1 = (edl?.tracks ?? []).find((t) => t.id === 'v1')
    const media = (v1?.clips ?? []).filter(isMediaClip)
    clip =
      // clipEnd = start + (out-in)/speed — the clip's EFFECTIVE timeline end.
      // Raw source width made a retimed clip's phantom tail capture the
      // playhead past its drawn extent, targeting the wrong clip here.
      media.find((c) => c.start <= playhead && playhead < clipEnd(c)) ??
      media[0] ?? null
    targetIsFallback = clip != null
  }
  const disabled = !clip
  const effects: EffectEntry[] = clip
    ? ((clip as unknown as { effects?: EffectEntry[] }).effects ?? [])
    : []

  // Map bundled-LUT filename → its index in the selected clip's chain
  // (remove_effect works BY INDEX). Later duplicates win, matching "the most
  // recently applied instance is the one you'd want to remove".
  const lutIndexByName = new Map<string, number>()
  effects.forEach((e, i) => {
    if (e.type === 'lut') lutIndexByName.set(baseName(String(e.params?.src ?? '')), i)
  })
  // Most recent lut effect on the clip — the one the intensity slider retunes.
  let lastLutIdx = -1
  for (let i = effects.length - 1; i >= 0; i--) {
    if (effects[i].type === 'lut') { lastLutIdx = i; break }
  }
  const appliedLut = lastLutIdx >= 0 ? effects[lastLutIdx] : null
  const appliedLutSrc = appliedLut ? String(appliedLut.params?.src ?? '') : ''
  const appliedIntensity = appliedLut != null
    ? Math.round(Math.max(0, Math.min(1, Number(appliedLut.params?.intensity ?? 1))) * 100)
    : null

  // Intensity slider: commit-on-release, mirroring MediaBin's volume slider —
  // the thumb tracks the drag locally; dispatch happens ONCE on release.
  const [localIntensity, setLocalIntensity] = useState(100)
  const draggingIntensity = useRef(false)
  // Re-seed from the applied LUT when it changes from outside (undo, chat
  // edits, selection change) — but never stomp the value mid-drag.
  useEffect(() => {
    if (!draggingIntensity.current) setLocalIntensity(appliedIntensity ?? 100)
  }, [appliedIntensity, selection])

  // Fetch the bundled LUT list (and valid effect types) once, on first expand.
  // Read-only listings go through api.dispatch directly: store.dispatch never
  // returns the tool's result payload to its caller.
  // One fetch per open/Retry, tracked by ref — NOT by `loading` in the deps:
  // setLoading(true) inside an effect that depends on `loading` re-fires the
  // effect, whose cleanup flipped `cancelled` on the in-flight fetch, so no
  // setState (including setLoading(false)) ever ran → "Loading…" forever.
  const fetchStartedRef = useRef(false)
  useEffect(() => {
    if (!open || !sid || fetchStartedRef.current) return
    fetchStartedRef.current = true
    setLoading(true)
    Promise.all([
      api.dispatch<{ luts?: string[] }>(sid, 'list_luts', {}),
      // list_filters is a nice-to-have (filters the preset grid to types the
      // backend actually supports) — its failure must not block the panel.
      api.dispatch<{ filters?: string[] }>(sid, 'list_filters', {}).catch(() => null),
    ])
      .then(([lutRes, filterRes]) => {
        setLuts(lutRes.result?.luts ?? [])
        setFilters(filterRes?.result?.filters ?? null)
      })
      .catch((e) => {
        setListError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => setLoading(false))
    // No cancellation: collapse doesn't unmount the panel, and a dep-change
    // cleanup here is exactly what wedged the original "Loading…" state.
  }, [open, sid, listError])

  const presets = filters
    ? EFFECT_PRESETS.filter((p) => filters.includes(p.type))
    : EFFECT_PRESETS

  // All mutations go through store.dispatch — it already toasts on failure,
  // tracks pendingOps and debounce-refreshes the EDL. No extra toast here.
  const applyLut = async (name: string) => {
    if (!clip) return
    await dispatch('apply_lut', { clip_id: clip.id, src: name, intensity: localIntensity / 100 })
  }

  const removeEffect = async (index: number) => {
    if (!clip) return
    await dispatch('remove_effect', { clip_id: clip.id, index })
  }

  const addEffect = async (type: string, params: Record<string, unknown>) => {
    if (!clip) return
    await dispatch('add_effect', { clip_id: clip.id, type, params })
  }

  const commitIntensity = async (v: number) => {
    // Only meaningful when a LUT is already on the clip; otherwise the slider
    // just stores the intensity used by the next Apply.
    if (!clip || lastLutIdx < 0 || !appliedLutSrc) return
    if (appliedIntensity != null && v === appliedIntensity) return
    // No update-effect tool exists, so retuning = remove the applied LUT entry
    // and re-apply the same LUT at the new intensity (two ops / two undo steps).
    // Bundled LUTs re-resolve by bare name; a custom .cube keeps its full path.
    const name = baseName(appliedLutSrc)
    const src = luts?.includes(name) ? name : appliedLutSrc
    await dispatch('remove_effect', { clip_id: clip.id, index: lastLutIdx })
    await dispatch('apply_lut', { clip_id: clip.id, src, intensity: v / 100 })
  }

  return (
    <div className="effects-panel" style={{ marginTop: 16 }}>
      <button
        style={{ width: '100%', fontSize: 11 }}
        onClick={() => setOpen((o) => !o)}
        title="Filters, effects & LUT looks"
      >
        {open ? '▼' : '▶'} ✨ Effects
      </button>
      {open && (
        <div style={{ marginTop: 8 }}>
          {disabled && (
            <div className="fx-hint">
              Add a video to the timeline first — looks and effects apply to a clip.
            </div>
          )}
          {!disabled && (
            <div className="fx-target" title={targetIsFallback
              ? 'No clip selected — applying to the clip at the playhead. Click a clip to target it.'
              : 'Applying to the selected clip.'}>
              → {targetIsFallback ? 'clip at playhead' : 'selected clip'}:{' '}
              {clip!.src.split('/').pop()?.split('\\').pop()}
            </div>
          )}
          {listError && (
            <div className="fx-error">
              <span>{listError}</span>
              <button onClick={() => { fetchStartedRef.current = false; setListError(null) }}>Retry</button>
            </div>
          )}

          <div className="fx-subhead">Looks (LUTs)</div>
          <div className="fx-slider-row">
            <label>Intensity</label>
            <input
              type="range" min={0} max={100} step={1} value={localIntensity} disabled={disabled}
              onChange={(e) => { draggingIntensity.current = true; setLocalIntensity(Number(e.target.value)) }}
              onPointerUp={(e) => { draggingIntensity.current = false; void commitIntensity(Number((e.target as HTMLInputElement).value)) }}
              onPointerCancel={() => { draggingIntensity.current = false }}
              onKeyUp={(e) => { draggingIntensity.current = false; void commitIntensity(Number((e.target as HTMLInputElement).value)) }}
              onBlur={(e) => { draggingIntensity.current = false; void commitIntensity(Number((e.target as HTMLInputElement).value)) }}
            />
            <span>{localIntensity}%</span>
          </div>
          {loading && <div className="fx-hint">Loading looks…</div>}
          {(luts ?? []).map((name) => {
            const appliedIdx = lutIndexByName.get(name)
            const applied = appliedIdx !== undefined
            return (
              <div key={name} className={`fx-lut-row${applied ? ' applied' : ''}`}>
                <span className="fx-lut-name" title={name}>
                  {applied ? '✓ ' : ''}{lutDisplayName(name)}
                </span>
                <button
                  disabled={disabled}
                  onClick={() => {
                    if (appliedIdx !== undefined) void removeEffect(appliedIdx)
                    else void applyLut(name)
                  }}
                >
                  {applied ? 'Remove' : 'Apply'}
                </button>
              </div>
            )
          })}
          {luts !== null && luts.length === 0 && (
            <div className="fx-hint">No bundled LUTs found.</div>
          )}

          <div className="fx-subhead" style={{ marginTop: 10 }}>Effects</div>
          <div className="fx-grid">
            {presets.map((p) => (
              <button
                key={p.type}
                className="fx-btn"
                disabled={disabled}
                title={`${p.hint} — effects stack; remove from the chips below.`}
                onClick={() => void addEffect(p.type, p.params)}
              >
                <span className="fx-icon">{p.icon}</span>{p.label}
              </button>
            ))}
          </div>

          {clip && effects.length > 0 && (
            <>
              <div className="fx-subhead" style={{ marginTop: 10 }}>Applied to selected clip</div>
              <div className="fx-chips">
                {effects.map((e, i) => (
                  <span key={`${e.type}-${i}`} className="fx-chip">
                    {chipLabel(e)}
                    <button title={`Remove ${chipLabel(e)}`} onClick={() => void removeEffect(i)}>×</button>
                  </span>
                ))}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}
