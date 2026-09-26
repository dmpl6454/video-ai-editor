// The Prompt bar's length limit (QA-124). Pure and import-free: the prompt
// store maps a refused run through it, and the store must not import the bar's
// focus rules (lib/promptFocus imports the store).
//
// The server refuses a prompt over 4000 characters
// (api/prompt_routes.py PromptRequest.message max_length). The bar had no limit
// and no counter: a 4700-character paste went out, came back as a bare
// "invalid request", and the status effect then wiped the text. Now the bar
// counts as the limit nears, refuses to send past it, and keeps the text.

/** Mirrors `PromptRequest.message` max_length on the server. */
export const PROMPT_MAX_CHARS = 4000
/** The counter appears from here on, so it never nags a normal sentence. */
const COUNTER_FROM = 3600

const fmt = (n: number) => n.toLocaleString('en-US')

/** The counter / refusal under the input, or null while it is not worth showing. */
export function promptLengthNote(text: string): { over: boolean; text: string } | null {
  const n = text.trim().length
  if (n > PROMPT_MAX_CHARS) {
    return { over: true, text: `Prompt is too long (${fmt(n)} / ${fmt(PROMPT_MAX_CHARS)} characters) — shorten it to run it.` }
  }
  if (n >= COUNTER_FROM) return { over: false, text: `${fmt(n)} / ${fmt(PROMPT_MAX_CHARS)} characters` }
  return null
}

/**
 * The run's failure in words when the server refused the prompt for its
 * length (a 422 on `message`), else null. Covers a client that did not know
 * the limit (an older build, the phone).
 */
export function promptTooLongError(raw: string): string | null {
  const at = raw.indexOf('{')
  if (!raw.startsWith('422') || at < 0) return null
  try {
    const body = JSON.parse(raw.slice(at)) as { error?: { details?: unknown } }
    const details = Array.isArray(body.error?.details) ? body.error.details as Record<string, unknown>[] : []
    const hit = details.find((d) => Array.isArray(d.loc) && (d.loc as unknown[]).includes('message')
      && String(d.type ?? '').includes('too_long'))
    if (!hit) return null
    // `input` is echoed capped at 200 characters (api/hardening.py), so the
    // length is not recoverable here; the limit is (ctx.max_length).
    const ctx = (hit.ctx ?? {}) as { max_length?: unknown }
    const limit = typeof ctx.max_length === 'number' ? ctx.max_length : PROMPT_MAX_CHARS
    return `Prompt is too long — the limit is ${fmt(limit)} characters. Shorten it and run it again.`
  } catch {
    return null
  }
}
