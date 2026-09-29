// What a timing field shows once its commit has settled (TimecodeField).
//
// The field re-seeds from the EDL only when the seeded value CHANGES, so an
// answer that changed nothing — refused (null), or accepted as a no-op (a
// clamp to what the clip already has: final sweep 2, a Duration typed past a
// Flash-In clip's source read 00:00:02:12 while Start/End said 1.93 s) —
// never re-seeds it. Once settled, the field shows the clip's real value
// again unless the user is typing in it; when the value did change, the
// re-seed shows the same thing.
export function afterCommit(
  r: unknown, isFocused: () => boolean, restore: () => void,
): Promise<void> {
  const settle = () => { if (!isFocused()) restore() }
  return Promise.resolve(r).then(settle, (err: unknown) => { settle(); throw err })
}
