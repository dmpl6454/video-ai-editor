import { useCallback, useRef } from 'react'

type Props = {
  orientation: 'vertical' | 'horizontal'   // vertical = drags left/right; horizontal = drags up/down
  onDelta: (deltaPx: number) => void
  onCommit?: () => void
  style?: React.CSSProperties   // used to place the handle on its named grid-area
}

/** A thin drag handle. Uses window-level listeners so a drag that leaves the
 *  handle bounds still resolves (same pattern as the timeline playhead drag). */
export function Splitter({ orientation, onDelta, onCommit, style }: Props) {
  const startRef = useRef(0)
  const onDown = useCallback((e: React.MouseEvent) => {
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
  }, [orientation, onDelta, onCommit])

  return (
    <div
      className={`splitter splitter-${orientation}`}
      style={style}
      onMouseDown={onDown}
      role="separator"
      aria-orientation={orientation === 'vertical' ? 'vertical' : 'horizontal'}
    />
  )
}
