// First-run model downloads, disclosed and asked about BEFORE they start
// (QA-065). GET /api/downloads (ai/weights.py) says, per tool, what would be
// fetched, how big it is and whether it is already on disk. The Prompt
// Editor always asked first; the Captions button and the AI cards did not.

export interface DownloadInfo { what: string; bytes: number; cached: boolean }
export type DownloadReport = Record<string, DownloadInfo>

/** The download key a tool run needs, from its tool id and args. */
export function downloadKeyFor(tool: string, args: Record<string, unknown> = {}): string | null {
  switch (tool) {
    case 'auto_caption': {
      const m = typeof args.model === 'string' && args.model ? args.model : 'large-v3'
      return `captions:${m}`
    }
    case 'translate_captions': return args.target_lang === 'hinglish' ? null : 'translate'
    case 'tts_voiceover': return 'tts'
    case 'remove_background': return 'bg_remove'
    case 'object_erase': return 'object_erase'
    case 'vocal_isolate': case 'instrumental_isolate': return 'stems'
    default: return null
  }
}

/** What this run would download, or null when nothing (cached, or unknown). */
export function pendingDownload(report: DownloadReport | null | undefined, key: string | null): DownloadInfo | null {
  if (!report || !key) return null
  const d = report[key]
  return d && !d.cached ? d : null
}

/** "1.6 GB" / "176 MB" — sizes as a person says them. */
export function sizeLabel(bytes: number): string {
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(bytes >= 1e10 ? 0 : 1)} GB`
  return `${Math.max(1, Math.round(bytes / 1e6))} MB`
}

/** The badge an uncached option wears, e.g. "Downloads 1.6 GB first". */
export function downloadBadge(d: DownloadInfo | null): string | null {
  return d ? `Downloads ${sizeLabel(d.bytes)} first` : null
}

/** The consent question, one sentence. */
export function consentText(d: DownloadInfo): string {
  return `The first run downloads ${d.what} (${sizeLabel(d.bytes)}, once). It stays on this Mac for next time.`
}
