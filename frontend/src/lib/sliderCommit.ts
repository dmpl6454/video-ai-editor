// One slider gesture → ONE committed value → one undo step (QA-087).
//
// Every inspector slider used to commit from `onKeyUp`, and a key nudge is one
// keyup per press: twelve Right presses on the Audio slider were twelve
// `set_volume` ops, twelve undo steps and twelve preview renders, dispatched
// as twelve concurrent fetches that could land in any order. A pointer drag
// was already one commit (on release); the keyboard is now the same gesture:
//
//   * `release` (pointer-up)   → commit now;
//   * `nudge`   (key-up)       → commit once the keys have been idle for
//                                `idleMs`, so a burst of presses is one op;
//   * `flush`   (blur/unmount) → commit anything still waiting, now;
//   * `sync`    (a new stored value arrived from undo/chat/refresh)
//                              → becomes the baseline while idle.
//
// A value equal to the last one committed (or to the stored value) is never
// sent, so pointer-up followed by the blur that always trails it is still one
// op — the old guard compared against the stored value, which does not update
// until the dispatch round-trip finishes, so the blur re-sent it.
//
// Pure (injectable timers) so the coalescing is unit-tested without a DOM.

export const SLIDER_IDLE_MS = 450

export interface SliderTimers {
  set: (fn: () => void, ms: number) => unknown
  clear: (handle: unknown) => void
}

const realTimers: SliderTimers = {
  set: (fn, ms) => setTimeout(fn, ms),
  clear: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
}

export interface Committer<T> {
  /** The thumb moved (drag tick or key step) — remembered, not sent. */
  change(v: T): void
  /** Pointer released: send `v` now. */
  release(v: T): void
  /** A key was released (or a colour well moved): send once idle. */
  nudge(v: T): void
  /** Focus left / component unmounting: send whatever is waiting, now. */
  flush(): void
  /** The stored value changed from outside; ignored while a commit waits. */
  sync(stored: T): void
  /** True while an idle commit is scheduled. */
  pending(): boolean
}
export type SliderCommitter = Committer<number>

/** One committed value per gesture, for any value type (QA-087, and the text
 *  colour wells of QA-078, which committed only on blur). */
export function createCommitter<T>(
  initial: T,
  commit: (v: T) => void,
  { idleMs = SLIDER_IDLE_MS, timers = realTimers, valid = () => true }:
    { idleMs?: number; timers?: SliderTimers; valid?: (v: T) => boolean } = {},
): Committer<T> {
  let baseline = initial        // the last value known to be stored or sent
  let latest: T | null = null
  let timer: unknown = null

  const cancelTimer = () => {
    if (timer !== null) { timers.clear(timer); timer = null }
  }
  const send = (v: T) => {
    cancelTimer()
    latest = null
    if (!valid(v) || v === baseline) return
    baseline = v
    commit(v)
  }

  return {
    change(v) { latest = v },
    release(v) { send(v) },
    nudge(v) {
      latest = v
      cancelTimer()
      timer = timers.set(() => { timer = null; if (latest !== null) send(latest) }, idleMs)
    },
    flush() { if (latest !== null) send(latest); else cancelTimer() },
    sync(stored) { if (timer === null) { baseline = stored; latest = null } },
    pending: () => timer !== null,
  }
}

export function createSliderCommitter(
  initial: number,
  commit: (v: number) => void,
  opts: { idleMs?: number; timers?: SliderTimers } = {},
): SliderCommitter {
  return createCommitter<number>(initial, commit, { ...opts, valid: (v) => Number.isFinite(v) })
}
