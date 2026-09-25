// The Text ▾ style gallery (QA-078): one-click looks that use what both
// renderers now draw — a background box, alignment, line spacing, a shadow
// override and an animation length — each inserted by ONE `add_text` (one
// undo step). The four old presets (countdown, callout, #hashtag, @handle)
// stay beside them.

export interface TextStylePreset {
  id: string
  label: string
  /** add_text args besides text/start/end (never x/y: the role places it). */
  args: Record<string, unknown>
  /** How the gallery chip previews it. */
  sample: { color: string; background?: string; fontFamily?: string; fontWeight?: number }
}

export const TEXT_STYLE_PRESETS: readonly TextStylePreset[] = [
  { id: 'title_box', label: 'Title box',
    args: { role: 'super', background: '#000000B3', stroke_w: 0, shadow_on: false, anim_in: 'fade', anim_out: 'fade' },
    sample: { color: '#fff', background: 'rgba(0,0,0,0.7)', fontFamily: 'Anton' } },
  { id: 'subtitle_band', label: 'Subtitle band',
    args: { role: 'label', background: '#000000CC', stroke_w: 0, shadow_on: false, size: 56 },
    sample: { color: '#fff', background: 'rgba(0,0,0,0.8)', fontFamily: 'Inter', fontWeight: 700 } },
  { id: 'yellow_pop', label: 'Yellow pop',
    args: { role: 'hook', color: '#FFD400', anim_in: 'pop', anim_dur: 0.45 },
    sample: { color: '#ffd400', fontFamily: 'Bebas Neue' } },
  { id: 'side_label', label: 'Side label',
    args: { role: 'label', align: 'left', background: '#FFFFFFE6', color: '#111111', stroke_w: 0, shadow_on: false },
    sample: { color: '#111', background: 'rgba(255,255,255,0.9)', fontFamily: 'Inter', fontWeight: 700 } },
  { id: 'quote', label: 'Quote',
    args: { role: 'default', font: 'Montserrat-Bold', line_spacing: 1.3, anim_in: 'fade', anim_out: 'fade', anim_dur: 0.8 },
    sample: { color: '#fff', fontFamily: 'Montserrat', fontWeight: 700 } },
  { id: 'neon', label: 'Neon',
    args: { role: 'super', color: '#00F0FF', stroke: '#001018', stroke_w: 6, shadow_on: false },
    sample: { color: '#00f0ff', fontFamily: 'Anton' } },
]

/** The add_text call for a gallery look. */
export function textStyleArgs(p: TextStylePreset, text: string, start: number, end: number): Record<string, unknown> {
  return { ...p.args, text: text.trim() || 'Your text', start, end, allow_stack: true }
}
