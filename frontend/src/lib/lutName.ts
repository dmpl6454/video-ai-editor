// The name a LUT is shown by — the Effects panel's chips, its look buttons and
// the "Apply look to all clips" tooltip.
import { prettyDiskName } from './mediaNames'

function baseName(path: string): string {
  // Handles both POSIX and Windows separators (params.src is an absolute path).
  const parts = path.split(/[\\/]/)
  return parts[parts.length - 1] ?? ''
}

/** `…/uploads/luts/warm_teal_81e3e270.cube` → "Warm Teal". An imported .cube
 *  is stored under a unique name (`_<uuid8>`, main._unique_upload_path), and
 *  that suffix leaked into the chip as "Warm Teal 81e3e270" — the same leak an
 *  imported voiceover had (final QA). `prettyDiskName` is the one rule that
 *  drops it everywhere else. */
export function lutDisplayName(fileOrPath: string): string {
  const stem = prettyDiskName(baseName(fileOrPath)).replace(/\.cube$/i, '')
  return stem
    .split(/[_-]+/)
    .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
    .join(' ')
}
