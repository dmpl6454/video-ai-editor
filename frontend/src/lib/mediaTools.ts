// Is the video engine's toolchain installed? (QA-108)
//
// Without ffmpeg/ffprobe every import, preview and export fails. The backend
// reports it on GET /api/health as `media_tools` (ingest/tools.py) and every
// route that needs them answers one 503 `ffmpeg_missing` whose message says
// what to install. This module reads the health field; MediaToolsBanner shows
// it once, stickily, instead of letting each gesture fail on its own.

export interface MediaToolsStatus {
  ok: boolean
  missing: string[]
  install_command: string
  message: string | null
}

/** What the banner shows, or null when there is nothing to fix. */
export interface MediaToolsProblem {
  missing: string[]
  command: string
  message: string
}

/**
 * The problem described by a /api/health body, or null. An older backend
 * without `media_tools` (or a malformed body) is not a problem: the banner
 * must never cry wolf over a field it cannot read.
 */
export function mediaToolsProblem(health: unknown): MediaToolsProblem | null {
  if (!health || typeof health !== 'object') return null
  const tools = (health as { media_tools?: unknown }).media_tools
  if (!tools || typeof tools !== 'object') return null
  const t = tools as Partial<MediaToolsStatus>
  if (t.ok !== false) return null
  const missing = Array.isArray(t.missing) ? t.missing.filter((m): m is string => typeof m === 'string') : []
  const command = typeof t.install_command === 'string' && t.install_command ? t.install_command : 'brew install ffmpeg'
  const message = typeof t.message === 'string' && t.message
    ? t.message
    : `Video AI Editor can't find ${missing.join(' and ') || 'ffmpeg'}. Install it with ${command}, then reopen the app.`
  return { missing, command, message }
}

/** The sentence without the command spelled inline — the banner shows the
 *  command in its own copyable box, so repeating it reads as noise. */
export function bannerSentence(problem: MediaToolsProblem): string {
  const withoutCmd = problem.message.replace(problem.command, 'the command below').replace(/\s{2,}/g, ' ')
  return withoutCmd.trim()
}
