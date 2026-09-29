// "Add music…" in the Audio panel (final sweep 2).
//
// It called `uploadAudio(file)` with no options, so the import followed the
// Media panel's "Add imports to the timeline" checkbox — a setting that lives
// in another panel since the left-rail move. Unticked (normal when importing a
// clip to drag onto a PIP lane), the file went to Media only: no Music clip,
// no toast, and every retry imported another copy. The button's name — like
// "Import audio file as voiceover" beside it — promises the timeline, so it
// always asks for the Music lane.
import type { ImportOptions } from '../store'

export type UploadAudio = (file: File, opts?: ImportOptions) => Promise<void>

export function addMusicFile(file: File, uploadAudio: UploadAudio): Promise<void> {
  return uploadAudio(file, { addToTimeline: true })
}
