// How a project is named on screen (QA-099).
//
// The chip, the picker and the delete dialog showed raw session ids
// ("s_327a167d67 ▾", "Delete project s_398e1ebaba?"), and two reopened copies
// of one project were told apart only by their ids. The server now names new
// projects "Untitled project N" and reopened copies "<name> (opened <date>)";
// these helpers cover what is already on disk and the picker's second line.

const SESSION_ID = /^s_[A-Za-z0-9]{6,64}$/

/** A project's display name — never its session id. */
export function projectLabel(name: string | null | undefined, id?: string | null): string {
  const n = (name ?? '').trim()
  if (!n || n === id || SESSION_ID.test(n)) return 'Untitled project'
  return n
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/**
 * The picker's "when" line from a unix time in SECONDS (the server's
 * `modified_at`): "edited just now", "edited 5 min ago", "edited 3 h ago",
 * "edited Sep 24", "edited Sep 24, 2025".
 */
export function editedLabel(unixSeconds: number | null | undefined, nowMs = Date.now()): string {
  if (typeof unixSeconds !== 'number' || !Number.isFinite(unixSeconds) || unixSeconds <= 0) return ''
  const ms = unixSeconds * 1000
  const ago = Math.max(0, nowMs - ms) / 1000
  if (ago < 60) return 'edited just now'
  if (ago < 3600) return `edited ${Math.floor(ago / 60)} min ago`
  if (ago < 24 * 3600) return `edited ${Math.floor(ago / 3600)} h ago`
  const d = new Date(ms)
  const sameYear = d.getFullYear() === new Date(nowMs).getFullYear()
  return `edited ${MONTHS[d.getMonth()]} ${d.getDate()}${sameYear ? '' : `, ${d.getFullYear()}`}`
}
