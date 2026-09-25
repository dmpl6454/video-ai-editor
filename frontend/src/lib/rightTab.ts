// Which tab the right sidebar shows: the Inspector (Properties + History) or
// the docked Chat (QA-061).
//
// Chat used to be a floating 360x215 panel, `useState(true)` on every load,
// covering the timeline's right-hand tracks and the History list, and closing
// it was forgotten on reload. It is now a tab of the right sidebar — docked,
// never over the tracks — CLOSED by default (the Prompt bar is the primary
// way to ask for an edit), and the last choice is remembered per browser.

export type RightTab = 'inspect' | 'chat'

export const RIGHT_TAB_KEY = 'vai.rightTab'

interface KV { getItem(k: string): string | null; setItem(k: string, v: string): void }

/** The remembered tab; the Inspector when nothing (or garbage) is stored or
 *  storage is unavailable (private window, blocked site data). */
export function readRightTab(storage: KV | null | undefined): RightTab {
  try {
    return storage?.getItem(RIGHT_TAB_KEY) === 'chat' ? 'chat' : 'inspect'
  } catch {
    return 'inspect'
  }
}

export function writeRightTab(storage: KV | null | undefined, tab: RightTab): void {
  try { storage?.setItem(RIGHT_TAB_KEY, tab) } catch { /* storage blocked: the choice lasts this load only */ }
}

export function browserStorage(): KV | null {
  try { return typeof localStorage === 'undefined' ? null : localStorage } catch { return null }
}
