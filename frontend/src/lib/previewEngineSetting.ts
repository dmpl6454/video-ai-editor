/**
 * The `preview.engine` setting as the frontend sees it (wave D,
 * INSTANT_PREVIEW_SPEC §1 G6, §7, §12). The backend owns it — settings.json
 * `preview.engine`, overridden by `VAI_PREVIEW_ENGINE` — and exposes it
 * read-only at GET /api/settings/preview. This milestone ships it defaulting
 * to `server`, so nothing a user sees changes until it is flipped.
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
