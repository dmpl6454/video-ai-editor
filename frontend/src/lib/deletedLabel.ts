import type { EDL } from '../types'

/** What to call the thing a delete is about to remove.
 *
 *  Reported as: "When I remove or delete emoji or text, it says 'clip deleted'
 *  even though they are not clips... this should be specific for all layers".
 *  A sticker, a caption line and a piece of text are not clips, and calling
 *  everything a clip makes the confirmation read like it removed the wrong
 *  thing — worst of all on the emoji, where the word matches nothing on screen.
 *
 *  Resolved from the TRACK, not the clip object: the track already distinguishes
 *  every layer the timeline shows (stickers/captions/text/music/voiceover/audio),
 *  whereas a clip only tells you media-vs-text. Falls back to "Clip" for an
 *  unknown track type, which is what a future lane would most likely be.
 */
const DELETED_LABEL: Record<string, [string, string]> = {
  sticker:  ['Sticker', 'Stickers'],
  captions: ['Caption', 'Captions'],
  text:     ['Text', 'Text items'],
  music:    ['Music clip', 'Music clips'],
  vo:       ['Voiceover', 'Voiceovers'],
  audio:    ['Audio clip', 'Audio clips'],
  video:    ['Clip', 'Clips'],
}

export function deletedLabel(edl: EDL | null, ids: string[]): string {
  const n = ids.length
  if (!edl || n === 0) return n > 1 ? `${n} clips deleted` : 'Clip deleted'
  const kinds = new Set<string>()
  for (const t of edl.tracks) {
    for (const c of t.clips) {
      if (ids.includes(c.id)) kinds.add(DELETED_LABEL[t.type] ? t.type : 'video')
    }
  }
  // A mixed multi-delete has no single honest noun, so it stays generic rather
  // than naming whichever kind happened to be found first.
  if (kinds.size !== 1) return n > 1 ? `${n} items deleted` : 'Item deleted'
  const [one, many] = DELETED_LABEL[[...kinds][0]]
  return n > 1 ? `${n} ${many.toLowerCase()} deleted` : `${one} deleted`
}
