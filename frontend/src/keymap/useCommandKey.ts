import { chordLabel, useKeymapStore } from './engine'
import { PRESETS } from './presets'

/**
 * The label of the first chord the LIVE keymap binds to `commandId` (active
 * preset + the user's overrides, the engine's own merge rule), or '' when it
 * has none. For hints outside Help ("⌘B splits the clip…"), which used to
 * hardcode the CapCut chord — wrong under Premiere (⌘K) or after a rebind.
 */
export function useCommandKey(commandId: string): string {
  const presetId = useKeymapStore((s) => s.presetId)
  const overrides = useKeymapStore((s) => s.overrides)
  const chords = overrides[commandId] ?? PRESETS[presetId].map[commandId] ?? []
  return chords[0] ? chordLabel(chords[0]) : ''
}
