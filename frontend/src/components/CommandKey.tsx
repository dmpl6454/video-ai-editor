import { useCommandKey } from '../keymap/useCommandKey'

/** A key cap showing what the live keymap binds to a command (nothing when
 *  unbound) — so a hint can never name a key that does not do it. */
export function CommandKey({ id, className = 'kbd' }: { id: string; className?: string }) {
  const k = useCommandKey(id)
  return k ? <span className={className}>{k}</span> : null
}
