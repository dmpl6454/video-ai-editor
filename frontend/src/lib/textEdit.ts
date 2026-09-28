// "Edit this title's words": select a text (or caption) clip and put the
// caret in the Inspector's Text box with its contents selected, so what the
// user types next IS the text. CapCut starts editing a title on a
// double-click in the canvas or on the timeline; the ⌥T "add text" chord
// lands the new clip in the same field (keymap/commands focusNewText).
//
// The DOM/store side is injected (`TextFieldEnv`, built in keymap/commands)
// so the wait-then-focus rule is testable without a browser.
import type { EDL } from '../types'

/** `id` when it is a text or caption clip in `edl`, else null. */
export function textClipId(edl: EDL | null | undefined, id: string | null | undefined): string | null {
  if (!edl || !id) return null
  return edl.tracks.some((t) => t.clips.some((c) => c.id === id && 'text' in c)) ? id : null
}

export interface TextFieldEnv {
  /** Resolves on the next frame (React commits between frames). */
  frame: () => Promise<unknown>
  selection: () => string | null
  inspectorOpen: () => boolean
  openInspector: () => void
  /** The Inspector's Text box, once it is on the page. */
  field: () => { focus: () => void; select: () => void } | null
  timeoutMs?: number
}

/** Wait for the Inspector to show `id` and focus its Text box. False when the
 *  selection moved on (the user clicked elsewhere) or it never appeared. */
export async function focusTextField(id: string, env: TextFieldEnv): Promise<boolean> {
  const t0 = Date.now()
  const limit = env.timeoutMs ?? 3000
  while (Date.now() - t0 < limit) {
    await env.frame()
    if (env.selection() !== id) return false
    if (!env.inspectorOpen()) { env.openInspector(); continue }
    const field = env.field()
    if (!field) continue
    field.focus()
    field.select()
    return true
  }
  return false
}
