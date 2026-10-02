// The 38×22 switch of the design (knob 18, --knob-shadow): a real
// role="switch" button, so Space toggles it and a screen reader names it.
export function Toggle({ on, onChange, label, disabled, id, title }: {
  on: boolean
  onChange: (on: boolean) => void
  /** The accessible name when no visible <label> is associated. */
  label?: string
  disabled?: boolean
  id?: string
  title?: string
}) {
  return (
    <button
      type="button"
      id={id}
      role="switch"
      aria-checked={on}
      aria-label={label}
      title={title}
      disabled={disabled}
      className={`ui-toggle${on ? ' is-on' : ''}`}
      onClick={() => onChange(!on)}
    >
      <span className="ui-toggle-knob" aria-hidden="true" />
    </button>
  )
}
