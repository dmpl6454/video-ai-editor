// The keyframe diamond of the design: 10×10 rotated 45°, 1.5 px
// rgba(255,255,255,.45) outline; filled #0A84FF when a key sits at the
// playhead; dimmed while the property is not animated. A real button.
export function KeyDiamond({ state, onClick, title, label }: {
  /** null = not animated (dim), false = animated, no key here (hollow), true = key here (filled). */
  state: boolean | null
  onClick?: () => void
  title?: string
  label: string
}) {
  return (
    <button type="button" className={`ui-key${state === true ? ' is-on' : state === false ? ' is-anim' : ''}`}
            aria-pressed={state === true} aria-label={label} title={title ?? (state ? 'Remove keyframe' : 'Add keyframe')}
            disabled={!onClick} onClick={onClick}>
      <span aria-hidden="true" />
    </button>
  )
}
