// React binding for lib/sliderCommit: the range-input handlers every
// inspector slider shares, so a keyboard burst or a drag is one op (QA-087).

import { createContext, useContext, useEffect, useMemo, useRef, type RefObject } from 'react'
import { createCommitter, type Committer } from './sliderCommit'

/** What the sliders below are editing (the selected clip id). The inspector
 *  reuses the SAME slider instances when the selection changes, so a keyboard
 *  commit still waiting for the idle delay must land on the clip it was made
 *  on — a new scope flushes it with the old clip's handler first. */
export const SliderScope = createContext<string>('')

export interface SliderCommitHandlers {
  onPointerUp: (e: { currentTarget: HTMLInputElement }) => void
  onKeyUp: (e: { currentTarget: HTMLInputElement }) => void
  onBlur: () => void
  /** Call from onChange with the live value. */
  change: (v: number) => void
}

/**
 * One committer per scope, living in a ref and touched only from effects and
 * event handlers (never during render). Effect order is what makes the scope
 * switch safe: every cleanup runs before any setup, so the old committer's
 * flush still calls the PREVIOUS `commit` closure (commitRef is refreshed by a
 * setup), i.e. a waiting value lands on the clip it was made on.
 */
function useCommitter<T>(stored: T, commit: (v: T) => void, valid: (v: T) => boolean): RefObject<Committer<T> | null> {
  const scope = useContext(SliderScope)
  const cRef = useRef<Committer<T> | null>(null)
  const commitRef = useRef(commit)
  const storedRef = useRef(stored)
  useEffect(() => { commitRef.current = commit; storedRef.current = stored })
  useEffect(() => {
    const c = createCommitter<T>(storedRef.current, (v) => commitRef.current(v), { valid })
    cRef.current = c
    return () => { c.flush(); if (cRef.current === c) cRef.current = null }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- one committer per scope; `valid` is a constant
  }, [scope])
  useEffect(() => { cRef.current?.sync(stored) }, [stored])
  return cRef
}

const finite = (v: number) => Number.isFinite(v)
const always = () => true

export function useSliderCommit(stored: number, commit: (v: number) => void): SliderCommitHandlers {
  const cRef = useCommitter<number>(stored, commit, finite)
  return useMemo(() => ({
    onPointerUp: (e) => cRef.current?.release(Number(e.currentTarget.value)),
    onKeyUp: (e) => cRef.current?.nudge(Number(e.currentTarget.value)),
    onBlur: () => cRef.current?.flush(),
    change: (v) => cRef.current?.change(v),
  }), [cRef])
}

/** A value that commits once it stops changing (idle) or on blur — the colour
 *  wells (QA-078): an <input type=color> fires `input` on every tick of the
 *  picker, and used to commit only on blur, so the preview never followed. */
export function useIdleCommit<T>(stored: T, commit: (v: T) => void): { input: (v: T) => void; flush: () => void } {
  const cRef = useCommitter<T>(stored, commit, always)
  return useMemo(() => ({
    input: (v: T) => cRef.current?.nudge(v),
    flush: () => cRef.current?.flush(),
  }), [cRef])
}
