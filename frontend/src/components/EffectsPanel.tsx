import { useEffect, useRef, useState } from 'react'
import { errorMessage, useStore } from '../store'
import { toast } from '../toast'
import { isCubeFile, lutApplyArgs, lutSrcFor } from '../lib/lutActions'
import { lutDisplayName } from '../lib/lutName'
import { api } from '../api'
import { isMediaClip, clipEnd, type Clip } from '../types'
import { useSliderCommit } from '../lib/useSliderCommit'
import './EffectsPanel.css'
import { Icon, type IconName } from './Icon'
import { effectsTargetLine } from '../lib/effectsTarget'
import { useMediaNameMap } from './MediaName'
import { flipState } from '../lib/flip'

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
const EFFECT_PRESETS: { type: string; label: string; icon: IconName; params: Record<string, unknown>; hint: string }[] = [
  { type: 'blur',      label: 'Blur',      icon: 'blur',     params: { radius: 8 },     hint: 'Gaussian blur (radius 8)' },
  { type: 'sharpen',   label: 'Sharpen',   icon: 'sharpen',  params: { amount: 1.0 },   hint: 'Unsharp mask (amount 1.0)' },
  { type: 'vignette',  label: 'Vignette',  icon: 'vignette', params: {},                hint: 'Darkened corners' },
  { type: 'grain',     label: 'Grain',     icon: 'grain',    params: { strength: 20 },  hint: 'Film grain noise (strength 20)' },
  { type: 'vintage',   label: 'Vintage',   icon: 'vintage',  params: {},                hint: 'Warm faded look with grain + vignette' },
  { type: 'vhs',       label: 'VHS',       icon: 'vhs',      params: {},                hint: 'Desaturated, noisy tape look' },
  { type: 'glow',      label: 'Glow',      icon: 'glow',     params: { strength: 0.4 }, hint: 'Soft glow / bloom (strength 0.4)' },
  { type: 'rgb_split', label: 'RGB Split', icon: 'rgbSplit', params: { offset: 6 },     hint: 'Chromatic aberration (offset 6px)' },
  { type: 'hflip',     label: 'Flip H',    icon: 'flipH',    params: {},                hint: 'Mirror horizontally' },
  { type: 'vflip',     label: 'Flip V',    icon: 'flipV',    params: {},                hint: 'Mirror vertically' },
]

/** The two presets that are the Transform's Mirror, not a stacked effect. */
const MIRROR_AXIS: Record<string, 'horizontal' | 'vertical'> = { hflip: 'horizontal', vflip: 'vertical' }

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

function chipLabel(e: EffectEntry): string {
  if (e.type === 'lut') return `LUT · ${lutDisplayName(String(e.params?.src ?? ''))}`
  return EFFECT_LABELS[e.type] ?? e.type
}

/** The Effects tool panel's content. Always open (the rail tab is the
 *  disclosure now, LEFT_RAIL_SPEC §2.7); `active` is whether the panel is on
 *  screen, and the look list is fetched on its first show. */
/** `show`: the redesigned asset browser splits this panel across two tabs —
 *  Filters (the LUT looks) and Effects › Video effects (the effect presets);
 *  'all' is the pre-redesign panel. */
export function EffectsPanel({ active = true, show = 'all' }: { active?: boolean; show?: 'all' | 'effects' | 'luts' }) {
  const showLuts = show !== 'effects'
  const showEffects = show !== 'luts'
  const sid = useStore((s) => s.sessionId)
  const edl = useStore((s) => s.edl)
  const selection = useStore((s) => s.selection)
  const dispatch = useStore((s) => s.dispatch)
  const mediaNames = useMediaNameMap()      // the clip's name as the Media panel shows it

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
  const mirror = flipState((clip as unknown as { transform?: unknown } | null)?.transform, effects)

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

  // Fetch the bundled LUT list (and valid effect types) once, on first show.
  // Read-only listings go through api.dispatch directly: store.dispatch never
  // returns the tool's result payload to its caller.
  // One fetch per first show/Retry, tracked by ref — NOT by `loading` in the deps:
  // setLoading(true) inside an effect that depends on `loading` re-fires the
  // effect, whose cleanup flipped `cancelled` on the in-flight fetch, so no
  // setState (including setLoading(false)) ever ran → "Loading…" forever.
  const fetchStartedRef = useRef(false)
  useEffect(() => {
    if (!active || !sid || fetchStartedRef.current) return
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
    // No cancellation: hiding doesn't unmount the panel, and a dep-change
    // cleanup here is exactly what wedged the original "Loading…" state.
  }, [active, sid, listError])

  const presets = filters
    ? EFFECT_PRESETS.filter((p) => filters.includes(p.type))
    : EFFECT_PRESETS

  // All mutations go through store.dispatch — it already toasts on failure,
  // tracks pendingOps and debounce-refreshes the EDL. No extra toast here.
  const applyLut = async (name: string) => {
    if (!clip) return
    await dispatch('apply_lut', { clip_id: clip.id, src: name, intensity: localIntensity / 100 })
  }

  // Final QA: the user's own .cube (uploaded into the session, never a typed
  // path) onto the target clip, and the look on the target clip onto every
  // main-track clip — both swapping an existing LUT rather than stacking.
  const cubeInput = useRef<HTMLInputElement>(null)
  const [importing, setImporting] = useState(false)
  const importLut = async (file: File) => {
    if (!clip || !sid) return
    if (!isCubeFile(file.name)) { toast.error('Pick a .cube LUT file.'); return }
    setImporting(true)
    try {
      const up = await api.uploadLut(sid, file)
      await dispatch('apply_lut', lutApplyArgs({ src: up.path, intensity: localIntensity / 100, clipId: clip.id }))
    } catch (e) {
      toast.error(`Couldn't import the LUT: ${errorMessage(e)}`)
    } finally {
      setImporting(false)
    }
  }
  const v1Count = ((edl?.tracks ?? []).find((t) => t.id === 'v1')?.clips ?? []).filter(isMediaClip).length
  const applyLookToAll = async () => {
    if (!appliedLutSrc) return
    await dispatch('apply_lut', lutApplyArgs({ src: lutSrcFor(appliedLutSrc, luts), intensity: localIntensity / 100 }))
  }

  const removeEffect = async (index: number) => {
    if (!clip) return
    await dispatch('remove_effect', { clip_id: clip.id, index })
  }

  const addEffect = async (type: string, params: Record<string, unknown>) => {
    if (!clip) return
    // Flip H / V are the Transform's Mirror (review RE: one mirror model) —
    // a toggle of flip_h / flip_v, never a stacked `hflip` effect the
    // Inspector's Flip buttons could not see.
    const axis = MIRROR_AXIS[type]
    if (axis) {
      await dispatch('flip_clip', { clip_id: clip.id, axis })
      return
    }
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
    <div className="effects-panel">
      {disabled && (
        <div className="fx-hint">
          Add a video to the timeline first — looks and effects apply to a clip.
        </div>
      )}
      {!disabled && (
        <div className="fx-target" title={targetIsFallback
          ? 'No clip selected — applying to the clip at the playhead. Click a clip to target it.'
          : 'Applying to the selected clip.'}>
          <Icon name="film" /> {effectsTargetLine(clip!.src, mediaNames, targetIsFallback)}
        </div>
      )}
      {listError && (
        <div className="fx-error">
          <span>{listError}</span>
          <button onClick={() => { fetchStartedRef.current = false; setListError(null) }}>Retry</button>
        </div>
      )}

      {showLuts && (<>
      <div className="fx-subhead section-label">Filters · LUT looks</div>
      <div className="fx-slider-row">
        <label>Intensity</label>
        {/* Keyed by clip: a commit still waiting on its idle delay lands
            on the clip it was made on (lib/useSliderCommit, QA-087). */}
        <IntensityRange
          key={clip?.id ?? ''}
          value={localIntensity}
          stored={appliedIntensity ?? 100}
          disabled={disabled}
          onLive={(v) => { draggingIntensity.current = true; setLocalIntensity(v) }}
          onEnd={() => { draggingIntensity.current = false }}
          onCommit={(v) => { draggingIntensity.current = false; void commitIntensity(v) }}
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
              {applied && <Icon name="check" />}{lutDisplayName(name)}
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
      <div className="fx-lut-actions">
        <input ref={cubeInput} type="file" accept=".cube" hidden
               onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ''; if (f) void importLut(f) }} />
        <button type="button" disabled={disabled || importing || !sid}
                title="Apply your own 3D LUT (.cube) to this clip — it replaces any look already on it"
                onClick={() => cubeInput.current?.click()}>
          {importing ? 'Importing…' : 'Import LUT (.cube)…'}
        </button>
        <button type="button" disabled={disabled || !appliedLutSrc || v1Count < 2}
                title={!appliedLutSrc ? 'Apply a look to this clip first, then copy it to every clip'
                  : `Put ${lutDisplayName(appliedLutSrc)} on all ${v1Count} main-track clips (replaces their looks)`}
                onClick={() => void applyLookToAll()}>
          Apply look to all clips
        </button>
      </div>
      </>)}

      {showEffects && (<>
      <div className="fx-subhead section-label" style={{ marginTop: 10 }}>Effects</div>
      <div className="fx-grid">
        {presets.map((p) => (
          <button
            key={p.type}
            className="fx-btn"
            disabled={disabled}
            aria-pressed={MIRROR_AXIS[p.type] ? (MIRROR_AXIS[p.type] === 'horizontal' ? mirror.h : mirror.v) : undefined}
            title={MIRROR_AXIS[p.type]
              ? `${p.hint} — the same toggle as Mirror in the Inspector's Transform`
              : `${p.hint} — effects stack; remove from the chips below.`}
            onClick={() => void addEffect(p.type, p.params)}
          >
            <Icon name={p.icon} />{p.label}
          </button>
        ))}
      </div>
      </>)}

      {clip && effects.length > 0 && (
        <>
          <div className="fx-subhead section-label" style={{ marginTop: 10 }}>
            {targetIsFallback ? 'On the clip at the playhead' : 'On the selected clip'}
          </div>
          <div className="fx-chips">
            {effects.map((e, i) => (
              <span key={`${e.type}-${i}`} className="fx-chip">
                {chipLabel(e)}
                <button title={`Remove ${chipLabel(e)}`} aria-label={`Remove ${chipLabel(e)}`} onClick={() => void removeEffect(i)}><Icon name="close" /></button>
              </span>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

/** The LUT intensity range: one commit per gesture (QA-087). */
function IntensityRange({ value, stored, disabled, onLive, onEnd, onCommit }: {
  value: number; stored: number; disabled: boolean
  onLive: (v: number) => void; onEnd: () => void; onCommit: (v: number) => void
}) {
  const h = useSliderCommit(stored, onCommit)
  return (
    <input
      type="range" min={0} max={100} step={1} value={value} disabled={disabled}
      aria-label="Look intensity"
      onChange={(e) => { const v = Number(e.target.value); onLive(v); h.change(v) }}
      onPointerUp={(e) => { onEnd(); h.onPointerUp(e) }}
      onPointerCancel={onEnd}
      onKeyUp={h.onKeyUp}
      onBlur={() => { onEnd(); h.onBlur() }}
    />
  )
}
