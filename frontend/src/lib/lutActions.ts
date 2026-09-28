// The Effects panel's LUT actions beyond "Apply to this clip" (Final QA):
// importing the user's own .cube and applying a look to every main-track
// clip. Both pass `replace: true` to apply_lut, so a second application swaps
// the look instead of stacking a second LUT under the first (the Prompt-bar
// route stacked). No clip id = every v1 clip (agent/dispatch.apply_lut).

export interface LutApply { src: string; intensity: number; clipId?: string }

/** apply_lut's args for one clip (clipId) or every main-track clip (none). */
export function lutApplyArgs({ src, intensity, clipId }: LutApply): Record<string, unknown> {
  const args: Record<string, unknown> = { src, intensity: Math.min(1, Math.max(0, intensity)), replace: true }
  if (clipId) args.clip_id = clipId
  return args
}

const baseName = (p: string) => p.split(/[\\/]/).pop() ?? p

/** The `src` to re-apply a stored look with: a bundled look by its bare
 *  name (list_luts), an imported .cube by its full session path. */
export function lutSrcFor(storedSrc: string, bundled: readonly string[] | null): string {
  const name = baseName(storedSrc)
  return bundled?.includes(name) ? name : storedSrc
}

/** Whether a picked file is a .cube LUT (what /lut_upload accepts). */
export function isCubeFile(name: string): boolean {
  return /.\.cube$/i.test(name.trim())
}
