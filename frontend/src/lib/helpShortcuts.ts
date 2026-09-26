// The Help overlay's shortcut list, GENERATED from the command registry and
// the live keymap (QA-110). It used to be a hand-written table of 17 rows
// that drifted from the keymap: N (snap), ⌘\ (fit), Home/End, ⌥←/⌥→ (nudge),
// ⌘C/⌘V and ⌘A were all bound and none of them was listed. Now a command
// exists in Help exactly when it exists in keymap/commands.ts, and its keys
// are whatever the active preset plus the user's overrides say — so a new
// command, a preset switch or a rebind can never leave Help behind again.
//
// Pure: the caller passes the registry, the category order and the effective
// map, so a test can build the list for every preset without a DOM.

import type { KeyMap } from '../keymap/presets'

export interface HelpCommand { id: string; label: string; category: string }
export interface HelpRow { id: string; label: string; keys: string[] }
export interface HelpGroup { title: string; rows: HelpRow[] }

/** Mouse and trackpad gestures — the only rows that are not keymap commands. */
export function gestureRows(isMac: boolean): HelpRow[] {
  return [
    // Timeline selection and wheel rows: lane C1 (QA-116 / QA-117) — they
    // say exactly what lib/marquee and lib/timelineWheel do.
    { id: 'gesture:shiftClick', label: 'Add a clip to the selection, or remove it',
      keys: ['Shift-click', `${isMac ? '⌘' : 'Ctrl'}-click`] },
    { id: 'gesture:marquee', label: 'Select the clips in a box', keys: ['Drag on empty track space'] },
    { id: 'gesture:zoomWheel', label: 'Zoom the timeline', keys: [`${isMac ? '⌘' : 'Ctrl'} + scroll`] },
    { id: 'gesture:pinch', label: 'Zoom the timeline (trackpad)', keys: ['Pinch'] },
    { id: 'gesture:panWheel', label: 'Pan the timeline left / right', keys: ['Shift + scroll', 'Swipe sideways'] },
    { id: 'gesture:scrollWheel', label: 'Scroll the tracks up / down (pans when every track fits)', keys: ['Scroll'] },
    { id: 'gesture:contextMenu', label: 'Clip menu (split, mute, lock, delete)', keys: ['Right-click'] },
    { id: 'gesture:trim', label: 'Trim a clip', keys: ['Drag a clip edge'] },
    { id: 'gesture:help', label: 'Show or hide this list', keys: ['?'] },
  ]
}

/**
 * Every command, grouped by category in `categories` order, each with the
 * chords the effective keymap binds to it (labelled by `label`). A command
 * with no key is still listed, with `keys: []` — Help says it is unbound
 * rather than hiding a feature the Keyboard settings can give a key to.
 * Categories absent from `categories` come last, in registry order.
 */
export function helpGroups(
  commands: readonly HelpCommand[],
  categories: readonly string[],
  keymap: KeyMap,
  label: (chord: string) => string,
): HelpGroup[] {
  const order = [...categories]
  for (const c of commands) if (!order.includes(c.category)) order.push(c.category)
  return order
    .map((title) => ({
      title,
      rows: commands
        .filter((c) => c.category === title)
        .map((c) => ({
          id: c.id,
          label: c.label,
          keys: (keymap[c.id] ?? []).map(label).filter(Boolean),
        })),
    }))
    .filter((g) => g.rows.length > 0)
}
