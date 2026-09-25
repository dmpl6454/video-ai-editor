// QA-105: two windows on one project.
//
// Each window sends the EDL hash its view was built from with every edit
// (`base_hash`); the server answers 409 `stale_edl` instead of applying an edit
// — or worse, an Undo of the OTHER window's edit — to a timeline this window
// is not showing. The store then refreshes and says so in one line.

export const STALE_VIEW_MESSAGE =
  'The timeline changed in another window (or an assistant edited it), so that edit was not applied. It has been refreshed — try again.'

/** A dispatch failure that is the server's `stale_edl` 409. api.ts throws
 *  `Error("409 Conflict: {envelope}")`; the code lives under error.details. */
export function isStaleEdlError(e: unknown): boolean {
  const raw = e instanceof Error ? e.message : String(e ?? '')
  return /^409\b/.test(raw) && raw.includes('"stale_edl"')
}
