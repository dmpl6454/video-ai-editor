// Shared bits of the Prompt bar's preview card (components/PromptPreviewCard):
// its root class (PromptBar focuses Apply through it) and the capped-list row.

export const PREVIEW_CARD_CLASS = 'prompt-preview'

/** The last row when the backend capped the list: "and 12 more changes". */
export function moreLine(n: number): string {
  return `and ${n} more change${n === 1 ? '' : 's'}`
}

/**
 * The character a key would TYPE while the preview card has focus, or null.
 * Apply takes focus a frame after the card appears, so a person rephrasing
 * ("no wait", or `/` then a new sentence) pressed Apply at the first space
 * and committed the old plan (final sweep 3, CRITICAL). Any printable key —
 * Space included — goes to the prompt instead; only Enter or a click applies.
 */
export function typeAheadKey(e: { key: string; metaKey: boolean; ctrlKey: boolean; altKey: boolean }): string | null {
  if (e.metaKey || e.ctrlKey || e.altKey) return null
  return e.key.length === 1 ? e.key : null
}

/** The prompt's text after a key typed over the card: `/` keeps the
 *  sentence (the prompt takes focus with it selected), any other character
 *  starts a new one. */
export function typedOverCard(key: string, text: string): string {
  return key === '/' ? text : key
}

/** What a reply typed in the Prompt bar over an open preview card means:
 *  the card's own text says "Reply **yes** to apply or **no** to change it",
 *  and typing that dropped the card and asked "Which of these did you mean?"
 *  (final sweep 3 r2). Only a WHOLE-message answer; anything else is a new
 *  sentence. "undo" / "go back" over a card is Change — nothing was applied,
 *  so there is nothing to undo (the server reads it the same way). */
export type PreviewReply = 'apply' | 'change'
const APPLY_REPLIES = new Set(['yes', 'y', 'yep', 'yeah', 'sure', 'ok', 'okay', 'apply', 'apply it', 'apply them',
  'do it', 'go', 'go ahead'])
const CHANGE_REPLIES = new Set(['no', 'n', 'nope', 'change', 'change it', 'cancel', 'discard', "don't apply", 'dont apply',
  'undo', 'undo that', 'undo it', 'go back', 'revert', 'revert that', 'revert it', 'take that back'])

export function previewReplyOf(text: string): PreviewReply | null {
  const t = (text ?? '').trim().toLowerCase().replace(/[’]/g, "'").replace(/[.!?]+$/, '').replace(/\s+/g, ' ')
  if (APPLY_REPLIES.has(t)) return 'apply'
  if (CHANGE_REPLIES.has(t)) return 'change'
  return null
}
