// What the Settings dialog SAYS (QA-063-SETTINGS, QA-106-CACHE-UI): the key's
// status line, the model/voice rows and the render-cache line, derived from
// the backend's answers. Pure, so every sentence is pinned by a test and the
// component only lays them out.
//
// The key itself never enters this module: the backend answers with a masked
// suffix (`sk-ant-…WxYz`) and a source, never the value.

import type { DownloadReport } from './modelDownloads'
import { humanBytes } from './promptEvents'

export type KeySource = 'env' | 'keychain' | 'none' | 'disabled'

/** GET /api/settings/anthropic-key. */
export interface KeyStatus {
  configured: boolean
  source: KeySource
  masked: string | null
  keychain_available: boolean
  can_edit: boolean
}

/** Mirrors keychain.KEY_RE on the backend: `sk-ant-` then base64url. */
const KEY_RE = /^sk-ant-[A-Za-z0-9_-]{20,400}$/

/** Why a pasted key can't be saved yet, or null when it can. Empty → null
 *  (nothing to complain about before the person has typed). */
export function keyInputProblem(raw: string): string | null {
  const v = raw.trim()
  if (!v) return null
  if (!v.startsWith('sk-ant-')) return 'An Anthropic API key starts with “sk-ant-”.'
  if (/\s/.test(v)) return 'The key has a space or line break in it — copy it again.'
  if (!KEY_RE.test(v)) return 'That key looks incomplete — copy it again from console.anthropic.com.'
  return null
}

export function canSaveKey(raw: string): boolean {
  return raw.trim().length > 0 && keyInputProblem(raw) === null
}

export type Tone = 'ok' | 'muted' | 'warn'

/** The one line under "Anthropic API key". */
export function keyStatusLine(s: KeyStatus): { text: string; tone: Tone } {
  switch (s.source) {
    case 'keychain':
      // Saved is not the same as accepted: "Test key" says whether Anthropic takes it.
      return { text: `Saved in your Keychain (${s.masked ?? 'hidden'}).`, tone: 'ok' }
    case 'env':
      return { text: `Using the key this app was started with (${s.masked ?? 'hidden'}). Change it where it was set.`, tone: 'ok' }
    case 'disabled':
      return { text: 'This app was started with Claude switched off (ANTHROPIC_API_KEY is empty), so a saved key is not used.', tone: 'warn' }
    default:
      return s.keychain_available
        ? { text: 'Not set up. Chat and the Prompt bar run on this Mac without it.', tone: 'muted' }
        : { text: 'This computer has no Keychain to keep a key in. Set ANTHROPIC_API_KEY before starting the app instead.', tone: 'warn' }
  }
}

/** "412 MB of 1.0 GB" — the render cache against its budget. */
export function cacheLine(u: { bytes: number; budget_bytes: number } | null): string {
  if (!u) return 'Checking…'
  if (u.bytes <= 0) return u.budget_bytes > 0 ? `Empty · keeps up to ${humanBytes(u.budget_bytes)}` : 'Empty'
  return u.budget_bytes > 0 ? `${humanBytes(u.bytes)} of ${humanBytes(u.budget_bytes)}` : humanBytes(u.bytes)
}

/** The toast after Clear: "Freed 380 MB" — or that there was nothing to free. */
export function freedMessage(bytes: number): string {
  return bytes > 0 ? `Freed ${humanBytes(bytes)}` : 'Nothing to clear — only the preview on screen is left'
}

/** One row of "Models & voices". */
export interface WeightRow {
  key: string
  name: string
  usedBy: string
  size: string
  cached: boolean
  state: string
}

const USED_BY: Record<string, string> = {
  'captions:large-v3': 'Captions (Accurate)',
  'captions:large-v3-turbo': 'Captions (Fastest)',
  translate: 'Translate captions',
  tts: 'AI voiceover',
  bg_remove: 'Remove background',
  object_erase: 'Erase object',
  stems: 'Isolate vocals / music',
}

/** Rows for every tool whose first run downloads weights (GET /api/downloads). */
export function weightRows(report: DownloadReport | null | undefined): WeightRow[] {
  if (!report) return []
  return Object.entries(report).map(([key, d]) => {
    const what = d.what.replace(/^the /, '')
    return {
      key,
      name: what.charAt(0).toUpperCase() + what.slice(1),
      usedBy: USED_BY[key] ?? key,
      size: humanBytes(d.bytes),
      cached: d.cached,
      state: d.cached ? 'On this Mac' : 'Downloads the first time you use it — you’ll be asked first',
    }
  })
}

/** The consent question for the local model download. */
export function modelConsentText(size: number | null | undefined, free: number | null | undefined): string {
  const s = size ? ` (${humanBytes(size)})` : ''
  const f = typeof free === 'number' ? ` ${humanBytes(free)} is free on this Mac.` : ''
  return `Download the local model${s} from Hugging Face? It is fetched once and then runs on this Mac with no internet.${f}`
}
