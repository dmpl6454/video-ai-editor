// When may a Prompt-bar card (the preview card, a clarify card) take keyboard
// focus as it appears? (final sweep 3 r2, CRITICAL)
//
// A slow dry run (captions, silences, stabilise) can finish while the person
// is typing somewhere else — the Playhead timecode, the Chat box, an Inspector
// field. The card used to move focus onto its Apply button whatever had it, so
// the Enter meant for the seek pressed Apply and changed the timeline with no
// deliberate Apply — against "nothing changes until the person presses Apply".
//
// The rule is the Prompt bar's own (lib/promptFocus `shouldRefocusPrompt`):
// take focus only when nobody holds it (the page body) or it is already inside
// the card's host (the Prompt bar's form, or the Chat pane the card sits in).
// Anywhere else the card waits; the live region announces it, and a click, Tab
// or `/` then Enter reaches it.

/** The element a card lives in: the Prompt bar, or the Chat pane. */
export const CARD_HOST_SELECTOR = '.prompt-bar, .chat-pane'

export type CardFocusSpot = 'none' | 'inside-host' | 'elsewhere'

/** Where focus is, relative to the card's host. */
export function cardFocusSpot(active: Element | null, card: Element | null, body: Element | null): CardFocusSpot {
  if (!active || active === body || active.tagName === 'HTML') return 'none'
  const host = card?.closest?.(CARD_HOST_SELECTOR) ?? null
  if (host && host.contains(active)) return 'inside-host'
  return 'elsewhere'
}

/** May the card move focus onto itself now? Never away from a field the
 *  person is using outside the card's own host. */
export function cardMayTakeFocus(spot: CardFocusSpot): boolean {
  return spot !== 'elsewhere'
}

/** Convenience for the components: decide from the live document. */
export function cardMayTakeFocusNow(card: Element | null): boolean {
  if (typeof document === 'undefined') return false
  return cardMayTakeFocus(cardFocusSpot(document.activeElement, card, document.body))
}
