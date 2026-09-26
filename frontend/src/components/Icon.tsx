// Renders one icon from THE app's set — lib/icons.ts holds the name map and
// the rules (lucide-react, 16 px, currentColor, decorative by default; QA-125).
import { ICONS, ICON_SIZE, ICON_STROKE, type IconName } from '../lib/icons'

export type { IconName }

export function Icon({ name, size = ICON_SIZE, filled = false, label, className }: {
  name: IconName
  size?: number
  /** Fill the outline too (play, a set keyframe). */
  filled?: boolean
  /** Makes the icon itself announce this (role=img); omit for decorative. */
  label?: string
  className?: string
}) {
  const C = ICONS[name]
  const fill = filled || name === 'play' ? 'currentColor' : 'none'
  const a11y = label
    ? { role: 'img' as const, 'aria-label': label }
    : { 'aria-hidden': true as const, focusable: 'false' as const }
  return (
    <C size={size} strokeWidth={ICON_STROKE} fill={fill} data-icon={name}
       className={className ? `icon ${className}` : 'icon'} {...a11y} />
  )
}

