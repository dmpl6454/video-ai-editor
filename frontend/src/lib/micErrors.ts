// The voiceover recorder's errors in the editor's words (wave C review).
//
// getUserMedia / MediaRecorder reject with a DOMException whose `message` is
// the browser's own terse string — WebKit says "Not supported", Chromium
// "Permission denied" — and that string reached the panel verbatim. Every
// known `name` maps to a sentence that says what to do next; an Error the app
// itself threw (already a sentence) passes through.

const ALLOW = 'Microphone access is blocked. Allow Video AI Editor in System Settings › Privacy & Security › '
  + 'Microphone (in a browser, allow it in the site settings), then try again.'

const BY_NAME: Record<string, string> = {
  NotAllowedError: ALLOW,
  SecurityError: ALLOW,
  NotFoundError: 'No microphone was found. Connect one (or choose it in System Settings › Sound › Input) and try again.',
  NotReadableError: 'The microphone is in use by another app or could not be opened. Close the other app and try again.',
  NotSupportedError: 'This window can’t record audio in a format the editor can use. Import an audio file instead.',
  OverconstrainedError: 'This window can’t record audio in a format the editor can use. Import an audio file instead.',
  AbortError: 'Recording stopped before it began. Try again.',
}

/** Shown when the window exposes no media capture at all. */
export const MIC_UNAVAILABLE = 'Recording isn’t available in this window. Allow microphone access for Video AI Editor '
  + 'in System Settings › Privacy & Security › Microphone, or import an audio file instead.'

/** A recorder failure as one plain sentence. */
export function micErrorMessage(e: unknown): string {
  const name = (e as { name?: unknown } | null)?.name
  if (typeof name === 'string' && BY_NAME[name]) return BY_NAME[name]
  const msg = e instanceof Error ? e.message : String(e ?? '')
  return msg.trim() || 'Recording failed. Try again.'
}
