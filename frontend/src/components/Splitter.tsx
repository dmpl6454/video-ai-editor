import { useCallback, useRef } from 'react'

type Props = {
  orientation: 'vertical' | 'horizontal'   // vertical = drags left/right; horizontal = drags up/down
  onDelta: (deltaPx: number) => void
  /** Called at pointer-down, before the first delta — lets the owner seed its
   *  drag base from the panel's DRAWN width (layoutStore widths can be null or
   *  larger than what the grid draws). */
  onStart?: () => void
  onCommit?: () => void
  style?: React.CSSProperties   // used to place the handle on its named grid-area
  disabled?: boolean   // when true, mousedown is a no-op (e.g. a collapsed panel's rail)
  className?: string
}

/** A thin drag handle. Uses window-level listeners so a drag that leaves the
 *  handle bounds still resolves (same pattern as the timeline playhead drag). */
export function Splitter({ orientation, onDelta, onStart, onCommit, style, disabled, className }: Props) {
  const startRef = useRef(0)
  const onDown = useCallback((e: React.MouseEvent) => {
    if (disabled) return
    e.preventDefault()
    onStart?.()
    startRef.current = orientation === 'vertical' ? e.clientX : e.clientY
    const move = (ev: MouseEvent) => {
      const pos = orientation === 'vertical' ? ev.clientX : ev.clientY
      onDelta(pos - startRef.current)
      startRef.current = pos
    }
    const up = () => {
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', up)
      onCommit?.()
    }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
  }, [orientation, onDelta, onStart, onCommit, disabled])

  return (
    <div
      className={`splitter splitter-${orientation}${disabled ? ' splitter-disabled' : ''}${className ? ` ${className}` : ''}`}
      style={style}
      onMouseDown={onDown}
      role="separator"
      aria-orientation={orientation === 'vertical' ? 'vertical' : 'horizontal'}
    />
  )
}
