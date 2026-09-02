// Shaping backend failure text before it reaches a toast or the AI panel.
//
// Two sources feed store.dispatch()'s catch: the sync path throws the
// hardening envelope (already a human sentence), but a job that fails is
// recorded by api/jobs.py as `f"{type(e).__name__}: {e}"`, so an async upscale
// that refuses a text clip arrives as "RuntimeError: upscale only supports
// media clips". The class name is noise to an editor and made every async
// failure read like a stack trace — strip it, leave everything else alone.

export function stripExceptionPrefix(msg: string): string {
  return msg.replace(/^\w+(Error|Exception): /, '')
}

// runDispatchJob throws `${tool} was cancelled` when the job ends `cancelled`
// (store.ts). That is the one failure the user asked for, so callers use this
// to show it neutrally instead of as a red error.
export function isCancelMessage(msg: string): boolean {
  return msg.endsWith(' was cancelled')
}
