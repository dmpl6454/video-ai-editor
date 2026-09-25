import { useStore } from '../store'
import { displayNameFor, itemsFor, namesBySrc, useMediaNames } from '../lib/mediaNames'

/** The user's name for a media file (QA-045) — the media library's, never the
 *  sanitised disk name, never a path (the tooltip used to be the absolute
 *  `/private/tmp/…/uploads/…` path). */
export function MediaName({ src }: { src: string }) {
  const sid = useStore((s) => s.sessionId)
  const items = useMediaNames((s) => itemsFor(s, sid))
  const name = displayNameFor(src, namesBySrc(items))
  return <span title={name}>{name}</span>
}

/** Hook form, for a label that is built into a larger string. */
export function useMediaNameMap(): ReadonlyMap<string, string> {
  const sid = useStore((s) => s.sessionId)
  return namesBySrc(useMediaNames((s) => itemsFor(s, sid)))
}
