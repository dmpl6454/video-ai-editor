// How a file in a session's exports/ dir reaches the user's disk from inside
// the packaged app — the ONE place that knows the pywebview bridge.
//
// In a real browser an `<a download>` click saves natively. The packaged app
// runs inside pywebview's WKWebView/WebView2, which IGNORES the `download`
// attribute and NAVIGATES to the href instead (TopBar's export button comment
// records the borderless-fullscreen-player trap that caused for .mp4). So when
// `window.pywebview.api.save_export` exists we call it: desktop.py's
// `_Api.save_export(session_id, filename)` pops a native Save-As with
// `filename` pre-filled, copies `<session>/exports/<filename>` to the chosen
// path and returns that path — or None when the dialog was cancelled, the
// session id is invalid, the name is not a bare leaf, or the file is missing.
//
// Exports (store.downloadExport) and the saved .vae project (TopBar's "Saved"
// link) both live in exports/, so both go through here. The project link used
// to be a bare anchor: in the app it either did nothing or — because the
// backend serves .vae as text/plain;attachment and WebKit appends ".txt" to a
// text attachment with an unknown extension — produced "<sid>.vae.txt", which
// POST /load_project's filename gate then refused with 415.

export type SaveExportFn = (sessionId: string, filename: string) => Promise<string | null>

// Narrow shape of what desktop.py's `_Api` exposes over pywebview's js_api —
// only the one method this module calls, not the whole class.
export interface PywebviewBridge {
  pywebview?: { api?: { save_export?: SaveExportFn } }
}

export type NativeSaveOutcome =
  | { kind: 'saved'; path: string }
  | { kind: 'cancelled' }
  | { kind: 'failed'; error: unknown }

// Bridge detection. `host` is `window` in the app (globalThis === window
// there); injectable so tests need no DOM. Browser-dev mode has no
// `window.pywebview`, so callers fall through to their anchor path.
export function saveExportBridge(host: unknown = globalThis): SaveExportFn | null {
  const api = (host as PywebviewBridge).pywebview?.api
  const fn = api?.save_export
  if (!fn) return null
  // Called as a method: pywebview installs `api` as a proxy and its members
  // expect that receiver.
  return (sessionId, filename) => api!.save_export!(sessionId, filename)
}

// The leaf save_project writes — main.py: `sd / "exports" / f"{sd.name}.vae"`.
// The session id IS the file name, so the link's filename never has to be
// threaded through state.
export const projectFilename = (sessionId: string): string => `${sessionId}.vae`

// Runs the native save when the bridge is present. `null` means "no bridge
// (or no session) — let the anchor do its job". The returned promise never
// rejects: a throwing bridge call becomes `failed` so each caller picks its
// own fallback (exports retry the anchor; the project link toasts).
export function nativeSave(
  sessionId: string | null,
  filename: string,
  host: unknown = globalThis,
): Promise<NativeSaveOutcome> | null {
  const bridge = saveExportBridge(host)
  if (!bridge || !sessionId) return null
  return bridge(sessionId, filename)
    .then((path): NativeSaveOutcome => (path ? { kind: 'saved', path } : { kind: 'cancelled' }))
    .catch((error: unknown): NativeSaveOutcome => ({ kind: 'failed', error }))
}

// Anchor-click adapter. With a bridge: claims the click (preventDefault, so
// the WKWebView never navigates to the file) and returns the outcome. Without
// one: returns null WITHOUT touching the event, so a plain browser's
// `<a download>` proceeds. preventDefault has to happen synchronously —
// before any await — which is why detection and the call are split from the
// toasting the caller does afterwards.
export function claimClickForNativeSave(
  evt: { preventDefault(): void },
  sessionId: string | null,
  filename: string,
  host: unknown = globalThis,
): Promise<NativeSaveOutcome> | null {
  const pending = nativeSave(sessionId, filename, host)
  if (!pending) return null
  evt.preventDefault()
  return pending
}
