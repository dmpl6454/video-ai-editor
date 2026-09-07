// ONE reader for the backend's error bodies.
//
// api/hardening.py rewrites EVERY 4xx/5xx into
//     {"error": {"code", "message", "request_id", "details"?}}
// and — this is the whole defect — hardcodes `message: "request failed"` for any
// HTTPException whose detail is a dict, moving the real dict under
// `error.details`. There is no `detail` key on the wire at all.
//
// api.ts's hand-rolled multipart handlers each read `body.detail.error`, a key
// that envelope never emits, so upload / audio upload / voiceover / sticker
// upload / project load collapsed every failure to a bare
// "422 Unprocessable Entity" — including the one that matters most on a machine
// with no ffmpeg, where the server had written a sentence saying exactly what
// was wrong. store.ts's errorMessage() read the same wire correctly. Two
// readers of one format and only one of them right is a drift that will happen
// again; this module is the single reader both now share.

import { stripExceptionPrefix } from './dispatchErrors'

/** Longest raw (non-JSON) body we will paste into a message — a proxy's HTML
 *  error page is not a sentence and must not become the toast. */
const RAW_BODY_MAX = 300

interface Envelope {
  error?: { message?: string; details?: unknown }
  detail?: unknown
}

/** The human sentence inside an `error.details` / `detail` payload.
 *
 *  A dict detail is written by main.py as `{error: <slug>, message: <sentence>,
 *  detail: <raw tail>}` — but the older `{error: "<sentence>"}` shape is still
 *  live (vo_record, audio_upload), which is why `error` is read LAST rather
 *  than not at all: it is a machine slug when `message` is present and the only
 *  human text when it isn't.
 */
function messageFromDetails(d: unknown): string | undefined {
  if (typeof d === 'string') return d.trim() || undefined
  if (Array.isArray(d)) {
    // RequestValidationError → `exc.errors()`, a list of {loc, msg, type}.
    for (const item of d) {
      if (!item || typeof item !== 'object') continue
      const { msg, loc } = item as { msg?: unknown; loc?: unknown }
      if (typeof msg !== 'string' || !msg) continue
      const field = Array.isArray(loc)
        ? loc.filter((p) => typeof p === 'string' && p !== 'body').join('.')
        : ''
      return field ? `${field}: ${msg}` : msg
    }
    return undefined
  }
  if (!d || typeof d !== 'object') return undefined
  const o = d as { message?: unknown; detail?: unknown; error?: unknown }
  for (const v of [o.message, o.detail, o.error]) {
    if (typeof v === 'string' && v.trim()) return v.trim()
  }
  return undefined
}

/** The human sentence inside a parsed error body, or undefined if it holds
 *  none (in which case the caller should fall back to the status line). */
export function messageFromBody(body: unknown): string | undefined {
  if (typeof body === 'string') return body.trim() || undefined
  if (!body || typeof body !== 'object') return undefined
  const b = body as Envelope
  // details BEFORE error.message: the envelope's message is the "request
  // failed" sentinel precisely when details carries the real explanation.
  const fromDetails = messageFromDetails(b.error?.details)
  if (fromDetails) return fromDetails
  const envMsg = b.error?.message
  if (typeof envMsg === 'string' && envMsg.trim()) return envMsg.trim()
  // Plain FastAPI shape ({"detail": …}) — anything that bypasses the envelope
  // (StaticFiles, an older backend) still lands here.
  return messageFromDetails(b.detail)
}

/** Human text for a thrown value.
 *
 *  Tolerates an Error whose message has a JSON body appended (`"422 …: {…}"`),
 *  which is how a failed fetch used to reach the UI, and drops the
 *  `RuntimeError: ` class prefix api/jobs.py records for a failed job. */
export function errorMessage(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e)
  const jsonStart = raw.indexOf('{')
  if (jsonStart !== -1) {
    try {
      const msg = messageFromBody(JSON.parse(raw.slice(jsonStart)))
      if (msg) return stripExceptionPrefix(msg)
    } catch {
      // not a JSON tail — fall through to the raw text
    }
  }
  return stripExceptionPrefix(raw)
}

/** The Error a failed `fetch` should throw.
 *
 *  Reads the body ONCE (a Response body can only be consumed once), so every
 *  caller must `throw await responseError(res)` rather than also reading it. */
export async function responseError(res: Response): Promise<Error> {
  const status = `${res.status} ${res.statusText}`.trim()
  let text = ''
  try {
    text = await res.text()
  } catch {
    // body already consumed or the connection died mid-read
  }
  const trimmed = text.trim()
  if (trimmed) {
    try {
      const msg = messageFromBody(JSON.parse(trimmed))
      // Bound the JSON-extracted sentence too, not just the non-JSON body:
      // some 422s carry a raw ffmpeg stderr tail (vo_record ships 800 chars),
      // and an unbounded toast pushes the actionable first line off screen.
      if (msg) return new Error(stripExceptionPrefix(msg).slice(0, RAW_BODY_MAX))
    } catch {
      // Not JSON (an HTML error page, a proxy) — keep the status line, which is
      // the only thing that identifies the failure, and a bounded tail.
      return new Error(`${status}: ${trimmed.slice(0, RAW_BODY_MAX)}`)
    }
  }
  return new Error(status)
}
