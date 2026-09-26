// The chord the LIVE keymap binds to a command (the active preset plus the
// user's overrides — the engine's own merge rule, as keymap/useCommandKey), in
// the two shapes the rail needs: the key cap ("⌥1") and the ARIA
// `aria-keyshortcuts` value ("Alt+1"). Both are '' while nothing binds the
// command, so a tooltip or header never names a key that does nothing.
import { chordLabel, IS_MAC, useKeymapStore } from '../../keymap/engine'
import { PRESETS } from '../../keymap/presets'
import { ariaKeyshortcuts } from './railModel'

export function useRailChord(commandId: string): { label: string; aria: string } {
  const presetId = useKeymapStore((s) => s.presetId)
  const overrides = useKeymapStore((s) => s.overrides)
  const chord = (overrides[commandId] ?? PRESETS[presetId].map[commandId] ?? [])[0] ?? ''
  return { label: chordLabel(chord), aria: ariaKeyshortcuts(chord, IS_MAC) }
}
