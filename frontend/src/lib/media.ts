// Shared helpers for reaching a session's raw media files from the browser,
// used by both StickerLayer (sticker/text image previews) and CropReposition
// (the raw, uncropped source shown while repositioning a cover-fit clip).

// Server src path → session file URL. Uploads land under <session>/uploads/…
// (stickers under uploads/stickers/<name>, media clips under
// uploads/<stem>/<file>) and serve_session_file streams
// /api/sessions/{sid}/files/uploads/<subpath> (the `name` segment may include
// subdirs; there is also an rglob-by-name fallback one level deeper). NOTE:
// /thumb is deliberately NOT used for stickers — it re-encodes to JPEG, which
// drops the alpha channel a PNG sticker needs.
export function sessionFileUrl(src: string, sid: string): string | null {
  const norm = src.replace(/\\/g, '/')
  const i = norm.indexOf('/uploads/')
  const name = i >= 0 ? norm.slice(i + '/uploads/'.length) : norm.split('/').pop()
  if (!name) return null
  const encoded = name.split('/').map(encodeURIComponent).join('/')
  return `/api/sessions/${encodeURIComponent(sid)}/files/uploads/${encoded}`
}

// Native pixel dimensions of a clip's SOURCE file (before any fit/crop/trim),
// keyed by `src`. Nothing in the EDL schema carries this — trim (in/out) is
// time-only, so a clip's own fields say nothing about its frame size — so
// it's probed client-side via a throwaway hidden <video>, cached so repeated
// calls (every draw-loop tick, or every CropReposition mount) don't re-probe.
const SRC_DIMS_CACHE = new Map<string, { w: number; h: number } | 'loading' | 'error'>()

export function srcDimsFor(src: string, sid: string | null): { w: number; h: number } | 'loading' | 'error' {
  const cached = SRC_DIMS_CACHE.get(src)
  if (cached) return cached
  if (!sid) return 'error'
  const url = sessionFileUrl(src, sid)
  if (!url) {
    SRC_DIMS_CACHE.set(src, 'error')
    return 'error'
  }
  SRC_DIMS_CACHE.set(src, 'loading')
  const probe = document.createElement('video')
  probe.preload = 'metadata'
  probe.muted = true
  probe.onloadedmetadata = () => {
    SRC_DIMS_CACHE.set(src, probe.videoWidth > 0 && probe.videoHeight > 0
      ? { w: probe.videoWidth, h: probe.videoHeight } : 'error')
  }
  probe.onerror = () => SRC_DIMS_CACHE.set(src, 'error')
  probe.src = url
  return 'loading'
}
