// The Effects panel's "which clip does this change" line (QA-101 / QA-045,
// wave C review). It printed the disk file of the editing copy —
// "› selected clip: clip20.normalized.mp4" — behind a chevron that looked like
// a disclosure control. The media library's name, in a sentence.
import { displayNameFor } from './mediaNames'

export function effectsTargetLine(src: string, names: ReadonlyMap<string, string>, atPlayhead: boolean): string {
  const name = displayNameFor(src, names)
  return atPlayhead ? `Applies to the clip at the playhead: ${name}` : `Applies to: ${name}`
}
