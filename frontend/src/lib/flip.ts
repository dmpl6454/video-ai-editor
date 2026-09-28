// Flip / mirror state of a clip's or sticker's Transform (wave E). The
// fields are `flip_h` / `flip_v` (edl/schema.py Transform, lane F4a); an EDL
// from before them has neither, which reads as not flipped.
export type FlipAxis = 'horizontal' | 'vertical'

export interface FlipState { h: boolean; v: boolean }

/** The mirror the picture SHOWS: the Transform field XOR an odd count of the
 *  Effects panel's legacy Flip H / V effects (`hflip` / `vflip`, review RE —
 *  one mirror model; `flip_clip` folds those effects away when pressed). */
export function flipState(transform: unknown, effects?: readonly { type?: unknown }[] | null): FlipState {
  const t = (transform ?? {}) as { flip_h?: unknown; flip_v?: unknown }
  const odd = (kind: string) => (effects ?? []).filter((e) => e?.type === kind).length % 2 === 1
  return { h: (t.flip_h === true) !== odd('hflip'), v: (t.flip_v === true) !== odd('vflip') }
}

/** What one press does: the other value of that axis (flip_clip toggles). */
export function toggled(state: FlipState, axis: FlipAxis): FlipState {
  return axis === 'horizontal' ? { ...state, h: !state.h } : { ...state, v: !state.v }
}
