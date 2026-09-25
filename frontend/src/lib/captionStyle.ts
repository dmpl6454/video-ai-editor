// Caption looks for the Caption style panel (QA-075). Each preset is the FULL
// set of CaptionLook fields `set_caption_style` takes, so picking one replaces
// the look rather than layering onto the last (null = the caption default).

import type { CSSProperties } from 'react'

export interface CaptionLook {
  font?: string | null; color?: string | null; size?: number | null; stroke?: string | null
  stroke_w?: number | null; background?: string | null; shadow_on?: boolean | null; upper?: boolean | null
}

const FIELDS = ['font', 'color', 'size', 'stroke', 'stroke_w', 'background', 'shadow_on', 'upper'] as const

/** A look with every field present (missing → null). */
export function captionLookOf(look: CaptionLook | null | undefined): Required<CaptionLook> {
  const out = {} as Record<string, unknown>
  for (const f of FIELDS) out[f] = look?.[f] ?? null
  return out as Required<CaptionLook>
}

export interface CaptionPreset {
  id: string; label: string; hint: string
  args: Required<CaptionLook>
  sample: CSSProperties
  matches(look: Required<CaptionLook>): boolean
}

function preset(id: string, label: string, hint: string, look: CaptionLook, sample: CSSProperties): CaptionPreset {
  const args = captionLookOf(look)
  return {
    id, label, hint, args, sample,
    matches: (l) => FIELDS.every((f) => String(l[f] ?? '').toLowerCase() === String(args[f] ?? '').toLowerCase()),
  }
}

export const CAPTION_PRESETS: readonly CaptionPreset[] = [
  preset('classic', 'Classic', 'White with a black outline', {},
    { color: '#fff', WebkitTextStroke: '1px #000' }),
  preset('yellow', 'Yellow', 'Broadcast yellow with a black outline', { color: '#FFD400' },
    { color: '#ffd400', WebkitTextStroke: '1px #000' }),
  preset('boxed', 'Boxed', 'White on a dark box, no outline', { background: '#000000B3', stroke_w: 0, shadow_on: false },
    { color: '#fff', background: 'rgba(0,0,0,0.7)', borderRadius: 3, padding: '0 4px' }),
  preset('bold', 'Bold', 'Anton in capitals', { font: 'Anton-Regular', upper: true },
    { color: '#fff', fontFamily: 'Anton', WebkitTextStroke: '1px #000' }),
  preset('clean', 'Clean', 'No outline, a soft drop shadow', { stroke_w: 0, shadow_on: true },
    { color: '#fff', textShadow: '1px 2px 0 rgba(0,0,0,0.55)' }),
]
