// What Enter does inside a clarify card (QA-019, rule 3 of lib/promptFocus).
// Kept free of imports: ClarifyCard is presentational and must not pull in the
// prompt store, which lib/promptFocus needs for isBusy.

export type ClarifyEnter = 'native' | 'submit' | 'ignore'

/**
 * What Enter does inside a clarify card, by the element it was pressed on:
 * `native` lets the focused button click itself, `submit` answers the card
 * with what is selected/typed, `ignore` leaves the key alone (a newline).
 * Chips are `role="radio"` buttons: picking one is arrows/click, and Enter on
 * a picked chip answers the card, as the Chips comment promises.
 */
export function clarifyEnterAction(target: { tagName: string; role?: string | null }): ClarifyEnter {
  const tag = target.tagName.toUpperCase()
  if (tag === 'TEXTAREA') return 'ignore'
  if (tag === 'BUTTON' && target.role !== 'radio') return 'native'
  return 'submit'
}
