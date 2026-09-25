// Where keyboard focus goes around the Prompt bar and its clarify card (QA-019).
//
// THE LOOP THIS PREVENTS
// ----------------------
// When a plan paused for a clarification, ClarifyCard focused its first control
// on mount — and then PromptBar's "a run just ended" effect, which runs AFTER
// the child's (React runs child effects first), saw status leave `planning`,
// found focus "inside the bar" (the card lives in the bar's <form>) and put it
// back on the textarea. So the card never had focus: typing `@qaeditor` for a
// brand-handle question appended it to the PROMPT ("add my brand kit@qaeditor"),
// and Enter — meant as "Start ↵" — submitted the prompt again, which re-planned,
// paused on the same question, and so on (an automated pass looped 316 times
// with zero /prompt/answer calls). Worse, one re-plan burned the whole sentence
// into the video as the watermark handle.
//
// Three rules, each a pure function so the loop is testable without a DOM:
//   1. a `clarify` pause never pulls focus back to the input — the card owns it;
//   2. while a card is up, Enter in the input does not re-run the prompt;
//   3. inside the card, Enter on a focused action button activates THAT button
//      (Start / Skip / Cancel), Enter elsewhere answers the card, and Enter in a
//      multi-line field is a newline. Escape is the card's cancel.

import { isBusy } from './promptStore'
import type { PromptStatus } from './promptEvents'

/** Where focus was when the status changed. */
export type FocusSpot = 'none' | 'inside-bar' | 'elsewhere'

/**
 * After a status change, should the prompt input take focus? Only when a run
 * has just ENDED (busy → not busy), not when it paused to ask something, and
 * only if the user had not put focus somewhere else on purpose.
 */
export function shouldRefocusPrompt(prev: PromptStatus, next: PromptStatus, focus: FocusSpot): boolean {
  if (!isBusy(prev) || isBusy(next)) return false
  if (next === 'clarify') return false
  return focus === 'none' || focus === 'inside-bar'
}

/** May the input's Enter / Run start a new prompt right now? */
export function canSubmitPrompt(status: PromptStatus, opts: { disabled: boolean; text: string }): boolean {
  if (isBusy(status) || opts.disabled) return false
  // A card is waiting for an answer: Enter must not re-plan over it.
  if (status === 'clarify') return false
  return opts.text.trim().length > 0
}

// Rule 3 lives in lib/clarifyKeys.ts: ClarifyCard is presentational and must
// not import the prompt store (this module does, for isBusy).
