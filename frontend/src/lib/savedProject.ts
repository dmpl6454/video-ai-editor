// The toolbar's "↓ .vae" link: which session's project file it points at, and
// whether the timeline has moved on since it was written.
//
// WHY THE SESSION ID IS PART OF THE RECORD
// ----------------------------------------
// The link used to be two loose pieces of TopBar state — `savedUrl` and
// `savedGen` — while the bridge call read the LIVE `sessionId`. `savedUrl` was
// cleared only at the start of the next Save, so `onLoadProject`,
// `switchSession` and `newSession` all left it on screen pointing at the
// previous project. In a browser that at least downloaded the old file; through
// the packaged app's native bridge it asked `save_export(<new sid>,
// "<new sid>.vae")` for a file that does not exist, desktop.py returned None,
// nativeSave mapped that to `cancelled`, and TopBar deliberately says nothing
// about a cancel — so the visible link did nothing at all, with no dialog, no
// toast and no navigation.
//
// Keeping url + sid + generation in ONE immutable record makes that class of
// bug unrepresentable: the link is shown only while it belongs to the session
// on screen, so "which sid does the bridge get" can no longer disagree with
// "which file does the href point at". It also means switching back to the
// session you saved from brings the link back, instead of losing it to a reset.
export interface SavedProject {
  /** `/api/sessions/<sid>/files/exports/<sid>.vae` as save_project returned it. */
  readonly url: string
  /** The session the file was written from — what the native bridge must be given. */
  readonly sid: string
  /** `ops.length` at the moment of saving, for the staleness marker. */
  readonly opsAtSave: number
}

export const savedProject = (sid: string, url: string, opsAtSave: number): SavedProject =>
  ({ url, sid, opsAtSave })

/** The record, but only while it belongs to the session currently on screen. */
export function visibleSavedProject(
  saved: SavedProject | null,
  sid: string | null,
): SavedProject | null {
  if (!saved || !sid) return null
  return saved.sid === sid ? saved : null
}

/** True once history has advanced past the generation the file was written at.
 *  The link stays — you can still grab the last save — but it is marked, so
 *  nobody ships a .vae that predates their latest edits by mistake. */
export const isSavedProjectStale = (saved: SavedProject, opsLen: number): boolean =>
  opsLen > saved.opsAtSave
