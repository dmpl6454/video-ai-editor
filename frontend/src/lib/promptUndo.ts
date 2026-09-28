// Whether the Prompt run log may offer "Undo" (the whole prompt, one history
// step). Undo restores the newest snapshot, so it undoes the PROMPT only while
// the prompt's op is still the newest state of the timeline (Final QA: after a
// later edit it undid that edit instead, and it stayed after History had
// already undone the prompt).

export interface PromptOpRef {
  /** The op's sequence number in the project's ops log. */
  seq: number | null
  /** The EDL hash right after the prompt's op. */
  hashAfter: string | null
}

/** Which op a prompt made, from its `op` event / saved run record. */
export function promptOpRef(op: unknown): PromptOpRef {
  const o = (op && typeof op === 'object' ? op : {}) as { seq?: unknown; edl_hash_after?: unknown }
  return {
    seq: typeof o.seq === 'number' ? o.seq : null,
    hashAfter: typeof o.edl_hash_after === 'string' && o.edl_hash_after ? o.edl_hash_after : null,
  }
}

/** The timeline's current state: its EDL hash and the ops log (newest last). */
export interface TimelineNow {
  edlHash: string | null
  ops: ReadonlyArray<{ seq: number }>
}

export function promptUndoAvailable(opSeen: boolean, ref: PromptOpRef | null, now: TimelineNow): boolean {
  if (!opSeen || !ref) return false
  // The hash is exact: any later edit AND an undo made elsewhere both change it.
  if (ref.hashAfter && now.edlHash) return now.edlHash === ref.hashAfter
  const last = now.ops.length ? now.ops[now.ops.length - 1].seq : null
  if (ref.seq !== null && last !== null) return last === ref.seq
  return false
}
