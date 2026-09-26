// Select + flash a clip an insert just created, so the Inspector opens on it
// and it can be adjusted at once (QA-128: a new sticker stayed unselected
// while new text was selected — StickerPanel threw the result away).
import { useStore } from '../store'

/** The new clip's id from an insert's result: `add_text`/`apply_text_template`
 *  answer `id`, `add_sticker` and the sticker upload answer `sticker_id`. */
export function newClipIdOf(result: unknown): string | null {
  if (!result || typeof result !== 'object') return null
  const r = result as { id?: unknown; sticker_id?: unknown }
  const id = typeof r.id === 'string' && r.id ? r.id : typeof r.sticker_id === 'string' ? r.sticker_id : ''
  return id || null
}

/** Refreshes the EDL at once (not the ~120 ms debounced refresh dispatch()
 *  queued) so the Inspector can find the clip the moment it is selected. */
export async function selectNewClip(result: unknown): Promise<void> {
  const id = newClipIdOf(result)
  if (!id) return
  const s = useStore.getState()
  await s.refresh()
  s.setSelection(id)
  s.flashClip(id)
}
