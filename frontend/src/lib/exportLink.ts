// The toolbar's "↓ MP4" link: which project's file it is, which timeline it
// was rendered from, and whether the timeline has moved on since (QA-026).
//
// WHY A RECORD, AND WHY A HASH
// ----------------------------
// The link used to be global store fields — `exportUrl`, `exportFilename`,
// `exportGen` — that no session switch cleared. Export in project A, switch to
// B with the picker, click "↓ MP4": A's 1080×1350 file downloaded, in B. The
// staleness test was `ops.length > exportGen`, and Undo SHORTENS history, so
// "change the canvas → export → Undo" left an out-of-date file looking current.
//
// Same cure as lib/savedProject for the .vae link: one immutable record that
// names its session, kept PER SESSION and shown only while that session is on
// screen (switching back brings it back, even after exporting elsewhere), plus
// the EDL hash the file was rendered from. The
// file is current exactly when that hash is the timeline's hash now — so an
// Undo marks it outdated and a Redo back to the exported state un-marks it,
// which no history counter can express.

export interface ExportLink {
  /** Session the file was rendered from — what the native save bridge is given. */
  readonly sid: string
  /** `/api/sessions/<sid>/files/exports/<file>` as the export job returned it. */
  readonly url: string
  /** Leaf name, for the native Save-As dialog. */
  readonly filename: string
  /** EDL hash of the rendered timeline; null only for a backend that does not say. */
  readonly edlHash: string | null
}

// render_export names the file `export_<edl hash>.<ext>`, so an older backend
// whose payload has no `edl_hash` still tells us which timeline it rendered.
const HASH_IN_NAME = /^export_([0-9a-f]{16})\.\w+$/

export function exportLink(sid: string, result: { url: string; filename?: string; edl_hash?: string }): ExportLink {
  const filename = result.filename || result.url.split('/').pop() || 'export.mp4'
  const edlHash = result.edl_hash ?? HASH_IN_NAME.exec(filename)?.[1] ?? null
  return { sid, url: result.url, filename, edlHash }
}

/** The link, but only while it belongs to the session currently on screen. */
export function visibleExport(link: ExportLink | null, sid: string | null): ExportLink | null {
  if (!link || !sid) return null
  return link.sid === sid ? link : null
}

/** True when the timeline on screen is not the one the file was rendered from.
 *  An unknown current hash (not loaded yet) is not evidence of staleness. */
export function isExportStale(link: ExportLink, currentHash: string | null): boolean {
  if (!currentHash) return false
  return link.edlHash !== currentHash
}

/** The file extension shown on the link ("MP4" / "MOV"). */
export const exportKind = (link: ExportLink): string =>
  (link.filename.split('.').pop() || 'mp4').toUpperCase()

/** Each session's latest export, keyed by session id. Replaced, never mutated. */
export type ExportLinks = Readonly<Record<string, ExportLink>>

export const withExport = (links: ExportLinks, link: ExportLink): ExportLinks => ({ ...links, [link.sid]: link })

export function withoutExport(links: ExportLinks, sid: string): ExportLinks {
  if (!(sid in links)) return links
  const next = { ...links }
  delete next[sid]
  return next
}

/** The export link of the session on screen, if it has one. */
export const exportFor = (links: ExportLinks, sid: string | null): ExportLink | null =>
  (sid ? visibleExport(links[sid] ?? null, sid) : null)

/** What the toolbar shows for the project on screen: nothing, or the link with
 *  its label and staleness. TopBar renders exactly this. */
export function exportLinkView(
  links: ExportLinks,
  sid: string | null,
  currentHash: string | null,
): { link: ExportLink; stale: boolean; label: string } | null {
  const here = exportFor(links, sid)
  if (!here) return null
  const stale = isExportStale(here, currentHash)
  return { link: here, stale, label: `↓ ${exportKind(here)}${stale ? ' (outdated)' : ''}` }
}
