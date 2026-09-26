/**
 * The `preview.engine` setting as the frontend sees it (wave D,
 * INSTANT_PREVIEW_SPEC §1 G6, §7, §12). The backend owns it — settings.json
 * `preview.engine`, overridden by `VAI_PREVIEW_ENGINE` — reads it at GET
 * /api/settings/preview and writes it at PUT /api/settings/preview (loopback,
 * same origin, JSON only; Settings' "Instant preview (beta)" Auto / Always /
 * Off). It ships defaulting to `server`, so nothing a user sees changes until
 * it is flipped.
 */

export type PreviewEngine = 'auto' | 'client' | 'server'

export const PREVIEW_ENGINES: readonly PreviewEngine[] = ['auto', 'client', 'server']
export const DEFAULT_PREVIEW_ENGINE: PreviewEngine = 'server'

/** What the route answers. */
export interface PreviewSettingsWire {
  engine: string
  source: 'env' | 'settings' | 'default' | string
  choices?: string[]
  default?: string
  eager_proxies?: boolean
}

export interface PreviewSettings {
  engine: PreviewEngine
  source: 'env' | 'settings' | 'default'
  eagerProxies: boolean
}

function isEngine(v: unknown): v is PreviewEngine {
  return typeof v === 'string' && (PREVIEW_ENGINES as readonly string[]).includes(v)
}

/**
 * Normalise the wire answer. Anything unexpected — an older backend without
 * the route (null), an engine name this build does not know — reads as the
 * default, `server`: the one engine every build can run.
 */
export function parsePreviewSettings(raw: unknown): PreviewSettings {
  const fallback: PreviewSettings = { engine: DEFAULT_PREVIEW_ENGINE, source: 'default', eagerProxies: false }
  if (!raw || typeof raw !== 'object') return fallback
  const r = raw as Partial<PreviewSettingsWire>
  if (!isEngine(r.engine)) return fallback
  const source = r.source === 'env' || r.source === 'settings' ? r.source : 'default'
  return { engine: r.engine, source, eagerProxies: r.eager_proxies === true }
}

/**
 * Whether the instant-preview engine should be attempted. `auto` defers to
 * the caller's capability probe (MediaSource + WebGL2, spec §7).
 */
export function wantsClientEngine(s: PreviewSettings, capable: boolean): boolean {
  if (s.engine === 'client') return true
  if (s.engine === 'auto') return capable
  return false
}

// ------------------------------------------------------ capability + resolve

/** What the page offers the client engine (spec §7 engine-level fallback,
 *  §10): Media Source Extensions (or ManagedMediaSource), WebGL2 and an
 *  AudioContext. Probed once per page. */
export interface PreviewCapabilities {
  mse: boolean
  webgl2: boolean
  audio: boolean
}

export function probePreviewCapabilities(win: (Window & typeof globalThis) | undefined =
  typeof window === 'undefined' ? undefined : window): PreviewCapabilities {
  if (!win) return { mse: false, webgl2: false, audio: false }
  const mse = 'MediaSource' in win || 'ManagedMediaSource' in win
  let webgl2: boolean
  try {
    webgl2 = !!win.document.createElement('canvas').getContext('webgl2')
  } catch {
    webgl2 = false
  }
  const audio = 'AudioContext' in win || 'webkitAudioContext' in win
  return { mse, webgl2, audio }
}

/** Why the client engine cannot run on these capabilities, or null. */
export function missingCapability(c: PreviewCapabilities): string | null {
  if (!c.mse) return 'no-mse'
  if (!c.webgl2) return 'no-webgl2'
  if (!c.audio) return 'no-audio'
  return null
}

export type ResolvedPreview = { mode: 'client' | 'server'; reason: string | null }

/**
 * The engine the app runs (spec §7, §12): `server` → server; `auto` → client
 * when every capability is there AND the project rate is one MSE can hold on
 * its 240 kHz grid (R1: `rateOk`); `client` ("Always") → client unless the
 * page cannot run it at all (no MSE / WebGL2 / AudioContext, or the rate) —
 * the engine would only fall back itself a moment later.
 */
export function resolvePreviewMode(s: PreviewSettings, caps: PreviewCapabilities, rateOk: boolean): ResolvedPreview {
  if (s.engine === 'server') return { mode: 'server', reason: 'setting' }
  const missing = missingCapability(caps)
  if (missing) return { mode: 'server', reason: missing }
  if (!rateOk) return { mode: 'server', reason: 'rate' }
  return { mode: 'client', reason: null }
}

/** The Settings control's three choices, in the words the dialog uses. */
export const PREVIEW_ENGINE_CHOICES: ReadonlyArray<{ engine: PreviewEngine; label: string }> = [
  { engine: 'auto', label: 'Auto' },
  { engine: 'client', label: 'Always' },
  { engine: 'server', label: 'Off' },
]

/** The one plain line under "Instant preview (beta)". */
export const INSTANT_PREVIEW_HELP =
  'Shows cuts, trims and moves the moment you make them, drawn on this Mac instead of waiting for a new render; exports are not affected.'

/** What the dialog says about the current state (null: nothing to add). */
export function previewEngineNote(s: PreviewSettings, resolved: ResolvedPreview | null): string | null {
  if (s.source === 'env') return 'Set for this run by VAI_PREVIEW_ENGINE; change it where the app is started.'
  if (!resolved || s.engine === 'server') return null
  if (resolved.mode === 'client') return 'On: edits show instantly.'
  switch (resolved.reason) {
    case 'no-mse':
    case 'no-webgl2':
    case 'no-audio':
      return 'This window cannot run it, so the rendered preview is used.'
    case 'rate':
      return 'This project’s frame rate is not supported yet, so the rendered preview is used.'
    default:
      return 'Paused after a problem, so the rendered preview is used.'
  }
}
