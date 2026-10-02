// The Home / projects screen's card facts (design §1, brief §2): a project's
// real name, duration pill, storage line and "edited" phrase, plus the search
// filter. Pure, so the formatting is unit-tested.

export interface ProjectRow {
  id: string
  name: string
  modified_at?: number
  poster?: string | null
  duration?: number | null
  size_bytes?: number | null
}

/** 646.2 → "10:46", 3725 → "1:02:05"; null → "". */
export function durationPill(sec: number | null | undefined): string {
  if (sec == null || !Number.isFinite(sec) || sec <= 0) return ''
  const s = Math.round(sec)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const r = String(s % 60).padStart(2, '0')
  return h ? `${h}:${String(m).padStart(2, '0')}:${r}` : `${String(m).padStart(2, '0')}:${r}`
}

/** 1.8e9 → "1.8 GB", 412e6 → "412 MB", 0 → "No media". */
export function sizeLabel(bytes: number | null | undefined): string {
  if (!bytes || bytes <= 0) return 'No media'
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB`
  if (bytes >= 1e6) return `${Math.round(bytes / 1e6)} MB`
  return `${Math.max(1, Math.round(bytes / 1e3))} KB`
}

/** "Edited today" / "Edited yesterday" / "Edited 3 days ago" / a date. */
export function editedPhrase(unixSeconds: number | null | undefined, nowMs = Date.now()): string {
  if (!unixSeconds) return 'Edited —'
  const then = new Date(unixSeconds * 1000)
  const now = new Date(nowMs)
  const dayStart = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()
  const days = Math.round((dayStart(now) - dayStart(then)) / 86_400_000)
  if (days <= 0) return 'Edited today'
  if (days === 1) return 'Edited yesterday'
  if (days < 7) return `Edited ${days} days ago`
  return `Edited ${then.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: then.getFullYear() === now.getFullYear() ? undefined : 'numeric' })}`
}

export function filterProjects<T extends { name: string; id: string }>(rows: readonly T[], query: string): T[] {
  const q = query.trim().toLowerCase()
  if (!q) return [...rows]
  return rows.filter((r) => r.name.toLowerCase().includes(q) || r.id.toLowerCase().includes(q))
}

/** A stable placeholder hue per project, for a card with no poster yet. */
export function placeholderHue(id: string): number {
  let h = 0
  for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) % 360
  return h
}
