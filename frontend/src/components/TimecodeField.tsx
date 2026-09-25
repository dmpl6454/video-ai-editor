import React from 'react'
import { useStore } from '../store'
import { formatTimecode, frameIndex, parseTimecode } from '../lib/timecode'
import { TIMELINE_MAX_SECONDS } from '../lib/dispatchErrors'
import { toast } from '../toast'

/** "Start must be within 6 hours" — the field's own label, the bound in words. */
export function outOfRangeMessage(label: string | undefined, max: number): string {
  const what = label || 'Time'
  if (max === TIMELINE_MAX_SECONDS) return `${what} must be within 6 hours`
  const h = max / 3600
  return Number.isInteger(h) ? `${what} must be within ${h} hour${h === 1 ? '' : 's'}` : `${what} is out of range`
}

/**
 * A timing field that shows SMPTE timecode and accepts it typed (QA-048) —
 * HH:MM:SS:FF, a right-aligned short form (3:04 = 3 s 4 f), seconds (12.5 or
 * 12.5s), clock time (1:05.5) or a frame count (90f); lib/timecode parses.
 *
 * Same contract as Properties' NumberField: re-seeds from the EDL but never
 * while focused, reverts on anything unparseable, and commits only a real
 * change — compared by FRAME, so re-blurring an unchanged field is no op.
 * `fps` defaults to the open project's rate (edl.canvas.fps).
 */
export function TimecodeField({ value, fps: fpsProp, min, max = TIMELINE_MAX_SECONDS, onCommit, title, ariaLabel, className }: {
  value: number
  fps?: unknown
  min?: number
  /** Upper bound (default: the 6 h every time argument allows, QA-041). A
   *  value past it is refused HERE, with the field restored — never sent. */
  max?: number
  /** A falsy/null resolution (store.dispatch's "did not land") restores the
   *  field to the current value instead of leaving the refused text in it. */
  onCommit: (seconds: number) => unknown
  title?: string
  ariaLabel?: string
  className?: string
}) {
  const projectFps = useStore((s) => s.edl?.canvas?.fps)
  const fps = fpsProp ?? projectFps
  const seeded = formatTimecode(value, fps)
  const ref = React.useRef<HTMLInputElement>(null)
  const [local, setLocal] = React.useState(seeded)
  React.useEffect(() => {
    if (document.activeElement !== ref.current) setLocal(seeded)
  }, [seeded])

  const seededRef = React.useRef(seeded)
  seededRef.current = seeded
  const commit = () => {
    let v = parseTimecode(local, fps)
    if (v == null) {
      setLocal(seeded)
      return
    }
    if (min != null) v = Math.max(min, v)
    if (max != null && v > max) {
      // A typo'd 100000 s is refused on the client in editor words, and the
      // field goes back to what the clip really has (QA-041).
      setLocal(seeded)
      toast.info(outOfRangeMessage(ariaLabel, max))
      return
    }
    if (frameIndex(v, fps) === frameIndex(value, fps)) {
      setLocal(seeded)
      return
    }
    const r = onCommit(v)
    void Promise.resolve(r).then((res) => {
      // Refused server-side: the value did not change, so no re-seed will
      // come — restore the field ourselves.
      if (r !== undefined && !res && document.activeElement !== ref.current) setLocal(seededRef.current)
    })
  }

  return (
    <input ref={ref} type="text" inputMode="decimal" spellCheck={false} autoComplete="off"
      className={className ? `tc-field ${className}` : 'tc-field'}
      title={title ?? 'Timecode HH:MM:SS:FF — or type seconds (12.5), 1:05.5, or frames (90f)'}
      aria-label={ariaLabel}
      value={local}
      onChange={(e) => setLocal(e.target.value)}
      onFocus={(e) => e.currentTarget.select()}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === 'Enter') e.currentTarget.blur()
        else if (e.key === 'Escape') { setLocal(seeded); requestAnimationFrame(() => ref.current?.blur()) }
      }} />
  )
}
