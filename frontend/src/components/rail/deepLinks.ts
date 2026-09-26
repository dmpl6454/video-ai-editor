// The tool panels' AI deep links (docs/design/LEFT_RAIL_SPEC.md §2.5, M5; R5):
// which catalogue tools each panel links to, the "All ‹group› tools (n)" link,
// the status a row mirrors from its AI card, and the return point the AI
// panel's back chip goes back to. Pure: no React, no store — the row component
// is DeepLinkRow.tsx, the jump itself is consumed by AiPanel.tsx.
//
// One target per row: a row names exactly ONE `lib/aiCatalog.ts` tool id and
// takes its label from the catalogue, so a row can never promise a tool the AI
// panel does not have under that name. A jump expands the EXISTING card in
// AiPanel; nothing outside AiPanel renders an AiToolCard (form state would
// split — spec risk 12, asserted by deepLinks.test.ts).
import { AI_CATALOG, gateFor, type AiGroup, type CatalogEntry } from '../../lib/aiCatalog'
import { featureCopy } from '../../lib/featureCopy'
import { downloadBadge, downloadKeyFor, pendingDownload, type DownloadReport } from '../../lib/modelDownloads'
import type { FeatureReport } from '../../api'
import type { RunState } from '../../lib/aiRuns'
import type { RailId } from './railModel'

/** The panels that carry deep links. */
export type DeepLinkPanel = Extract<RailId, 'media' | 'audio' | 'text' | 'effects' | 'captions'>

export interface DeepLinkSection {
  /** The section heading (a visible h3 in the panel). */
  heading: string
  /** One catalogue tool id per row, in display order. */
  tools: readonly string[]
  /** The AI group the "All ‹group› tools (n)" link opens; none when every
   *  tool of the group is already a row (Audio). */
  group?: AiGroup
}

export const DEEP_LINKS: Readonly<Record<DeepLinkPanel, DeepLinkSection>> = {
  media: { heading: 'Find & search', tools: ['search_media', 'find_broll'], group: 'Find & search' },
  audio: { heading: 'AI audio', tools: ['noise_reduce', 'vocal_isolate', 'instrumental_isolate', 'tts_voiceover'] },
  text: {
    heading: 'AI text & brand',
    tools: ['add_hook_overlay', 'generate_hook', 'add_lower_third', 'apply_brand_kit'],
    group: 'Text & brand',
  },
  effects: { heading: 'Cutout & effects (AI)', tools: ['remove_background', 'chroma_key'], group: 'Cutout & effects' },
  captions: {
    heading: 'Captions & speech (AI)',
    tools: ['translate_captions', 'diarize', 'name_speakers', 'import_srt', 'export_srt'],
    group: 'Captions & speech',
  },
}

/** The catalogue entry a row targets (undefined only for a stale id, which
 *  the unit test rules out). */
export function catalogEntry(tool: string): CatalogEntry | undefined {
  return AI_CATALOG.find((e) => e.tool === tool)
}

/** How many catalogue tools a group holds — the "(n)" of the All link. */
export function groupCount(group: AiGroup): number {
  return AI_CATALOG.filter((e) => e.group === group).length
}

export function allToolsLabel(group: AiGroup): string {
  return `All ${group} tools (${groupCount(group)})`
}

/** The AI panel's group heading id (AiPanel renders it; a group link focuses it). */
export function aiGroupId(group: string): string {
  return `ai-group-${group.replace(/\W+/g, '-').toLowerCase()}`
}

/** The card's own toggle in AiPanel: AiToolCard's head button controls
 *  `ai-body-‹tool›`. The jump presses THIS button, the same path a click takes
 *  (it seeds the form from the playhead and marks at that moment). */
export function aiCardToggleSelector(tool: string): string {
  return `button.ai-card-head[aria-controls="ai-body-${tool}"]`
}

/** What a row carries in `data-deep-link`, so the back chip can refocus it. */
export function deepLinkKey(target: { tool?: string; group?: string }): string {
  return target.tool ? `tool:${target.tool}` : `group:${target.group ?? ''}`
}

export type DeepLinkTone = 'run' | 'warn' | 'done' | 'error' | 'dim'
export interface DeepLinkStatus { text: string; tone: DeepLinkTone }

export interface StatusSources {
  run: RunState | undefined
  features: FeatureReport | null
  downloads: DownloadReport | null
  /** The /api/tools list, null until it loads. */
  tools: readonly { name: string }[] | null
}

/**
 * The status a row mirrors, from the sources AiToolCard reads (lib/aiRuns:
 * the run, the feature report, the download report). Null means "ready" and
 * shows nothing. Order: a live or failed run beats everything (it is what the
 * user is waiting on); then whether the tool can run at all; then a first-run
 * download; then a finished run.
 */
export function deepLinkStatus(entry: CatalogEntry, s: StatusSources): DeepLinkStatus | null {
  const run = s.run
  if (run?.status === 'running') {
    if (run.cancelling) return { text: 'Stopping…', tone: 'run' }
    const pct = Math.round(run.progress * 100)
    return { text: run.reportsProgress && pct > 0 ? `Running ${pct}%` : 'Running…', tone: 'run' }
  }
  if (run?.status === 'error') return run.cancelled ? { text: 'Cancelled', tone: 'dim' } : { text: 'Failed', tone: 'error' }
  if (s.tools && !s.tools.some((t) => t.name === entry.tool)) return { text: 'Not available', tone: 'dim' }
  // The card judges the gate on its seeded form values; a row has none, so a
  // tool that is only gated for one form value (auto_reframe) reads as the
  // card does before it is opened.
  const gate = gateFor(entry, s.features)
  if (!gate.ok) return { text: featureCopy(gate).badge, tone: 'warn' }
  const dl = downloadBadge(pendingDownload(s.downloads, downloadKeyFor(entry.tool, {})))
  if (dl) return { text: dl, tone: 'warn' }
  if (run?.status === 'done') return { text: 'Done', tone: 'done' }
  return null
}

/**
 * How far to scroll a tool panel so a card's top sits `gap` px under the AI
 * panel's sticky head ("scrolls the card to the top"). Both are measured
 * viewport tops/bottoms. Positive = scroll down. The caller measures twice:
 * where a sticky head settles against the scroller's padding differs between
 * engines, so the second pass corrects with the head where it actually stuck.
 */
export function scrollDeltaToTop(cardTop: number, headBottom: number, gap = 6): number {
  return Math.round(cardTop - (headBottom + gap))
}

/** Where the back chip returns to: the origin panel, its scroll offset at
 *  the moment of the jump, and the row that was clicked. */
export interface ReturnPoint { from: RailId; key: string; scrollTop: number }

let returnPoint: ReturnPoint | null = null

export function rememberReturn(p: ReturnPoint): void {
  returnPoint = { ...p }
}

/** The return point for a jump from `from`, consumed (a second press has
 *  nothing to return to). Null when the last jump came from elsewhere. */
export function takeReturn(from: RailId): ReturnPoint | null {
  const p = returnPoint
  returnPoint = null
  return p && p.from === from ? p : null
}

export function forgetReturn(): void {
  returnPoint = null
}
