// QA-046: Undo is bound to what the SERVER can still undo, not to how many
// ops History lists. Snapshots are bounded (edl/snapshot.py MAX_UNDO and a
// byte budget), the ops log is not, and a fresh project's "init" op is never
// undoable — so ops.length said "Undo is available" long after it was not.

/** The undo depth a session payload reports. An older backend without the
 *  field falls back to the ops count, which is what the button used before. */
export function undoDepthOf(info: { undo_depth?: number; ops?: unknown[] }): number {
  const d = info.undo_depth
  if (typeof d === 'number' && Number.isFinite(d)) return Math.max(0, Math.floor(d))
  return info.ops?.length ?? 0
}

/** True when the op `fromNewest` steps back (0 = the latest) can still be
 *  undone. History greys the rest instead of implying they can be reached. */
export function isWithinUndoHorizon(fromNewest: number, depth: number): boolean {
  return fromNewest >= 0 && fromNewest < depth
}

/** The toast shown when Undo is refused, naming why. */
export function undoRefusedMessage(opsListed: number): string {
  return opsListed > 0
    ? 'Nothing left to undo. Older History entries are past the undo limit.'
    : 'Nothing to undo yet.'
}

/** Undo button tooltip. */
export function undoTitle(depth: number, chord: string): string {
  if (depth <= 0) return 'Nothing to undo'
  return `Undo last edit (${chord}), ${depth} step${depth === 1 ? '' : 's'} available`
}

/** History's rows, newest first (QA-046, wave-B review): the EDITS, where the
 *  undo divider falls among them (-1 = every edit is undoable), and the
 *  project's own creation op as a footer. `init` is never undoable, so
 *  counting it put "Older edits cannot be undone" above "New project" on
 *  every project — as if edits had been lost. */
export function historyRows<T extends { tool: string }>(ops: readonly T[], depth: number)
  : { edits: T[]; footer: T[]; horizonAt: number } {
  const newestFirst = [...ops].reverse()
  const edits = newestFirst.filter((op) => op.tool !== 'init')
  const footer = newestFirst.filter((op) => op.tool === 'init')
  const horizonAt = edits.findIndex((_, i) => !isWithinUndoHorizon(i, depth))
  return { edits, footer, horizonAt }
}
