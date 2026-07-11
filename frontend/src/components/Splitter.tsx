import { useCallback, useRef } from 'react'

type Props = {
  orientation: 'vertical' | 'horizontal'   // vertical = drags left/right; horizontal = drags up/down
  onDelta: (deltaPx: number) => void
  onCommit?: () => void
  style?: React.CSSProperties   // used to place the handle on its named grid-area
  disabled?: boolean   // when true, mousedown is a no-op (e.g. a collapsed panel's rail)
}

/** A thin drag handle. Uses window-level listeners so a drag that leaves the
 *  handle bounds still resolves (same pattern as the timeline playhead drag). */
export function Splitter({ orientation, onDelta, onCommit, style, disabled }: Props) {
  const startRef = useRef(0)
  const onDown = useCallback((e: React.MouseEvent) => {
    if (disabled) return
    e.preventDefault()
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
  }, [orientation, onDelta, onCommit, disabled])

  return (
    <div
      className={`splitter splitter-${orientation}${disabled ? ' splitter-disabled' : ''}`}
      style={style}
      onMouseDown={onDown}
      role="separator"
      aria-orientation={orientation === 'vertical' ? 'vertical' : 'horizontal'}
    />
  )
}
