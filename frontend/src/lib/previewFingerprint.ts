// What makes a preview RENDER stale: a fingerprint that changes only for
// video/audio-relevant edits (text and sticker edits draw client-side and
// never need a render). Shared by both preview engines — the server preview
// re-renders on it, the client engine renders its bakes on it (spec §4.1
// step 8). The reasoning for every inclusion and exclusion is in the comments.

import type { EDL } from '../types'
import { pipIsClientDrawn } from './pipDraw'

// Serializes the WHOLE clip object on video/audio-family tracks rather than
// hand-picking fields (id/src/in/out/start): the backend Clip schema also
// carries speed, effects (color grade, chromakey, mask…), transform
// (x/y/scale/rotation/opacity, incl. keyframes) and audio (gain/fade/mute),
// which types.ts's frontend Clip interface doesn't declare — Properties.tsx
// reaches them via `as unknown as {...}` casts. A hand-picked field list
// silently goes stale every time a new video-affecting property is added
// (that's exactly how speed/color/transform/audio edits used to commit to
// the EDL but never trigger a preview re-render). Hashing the full clip
// mirrors how the backend itself decides "did anything render-relevant
// change" — edl.hash() in schema.py hashes the entire EDL, not a field
// subset — so this fingerprint can't drift out of sync with the schema again.
export function videoFingerprintOf(edl: EDL | null): string {
  if (!edl) return ''
  // Sticker tracks are EXCLUDED, exactly like text: StickerLayer now draws
  // every sticker client-side each frame and build_overlay_chain(preview)
  // no longer bakes them (see StickerLayer's pixel-ownership rule). Leaving
  // them in would fire a full ffmpeg re-render for an edit whose result is
  // already on screen — and it was that re-render round-trip which produced
  // the "sticker disappears, then leaves a copy at the old position" gap.
  // NOTE: this is only safe while the preview genuinely skips stickers. If
  // baking ever comes back, sticker tracks must come back here too, or
  // sticker edits stop producing any visual result at all.
  const vidTracks = edl.tracks.filter(t =>
    t.type === 'video' || t.type === 'audio' || t.type === 'music' || t.type === 'vo')
  return JSON.stringify({
    canvas: edl.canvas,
    // Track-LEVEL props matter too: transitions and mute live on the track,
    // not a clip — omitting them left the preview stale after adding a
    // transition (surfaced the day transitions got a UI). `z` is the
    // compositing order (PIP/sticker stacking) — also render-relevant.
    tracks: vidTracks.map(t => ({
      id: t.id,
      z: t.z,
      muted: t.muted,
      transitions: (t as unknown as { transitions?: unknown }).transitions,
      // A PIP lane's clips are reduced to what still affects the RENDER: its
      // audio (pip.py keeps mixing that) and the timing that positions it.
      // Placement, size, shape and framing are the client's now — pip.py's
      // `preview` branch skips baking the picture and StickerLayer paints it
      // — so including them fired a full ffmpeg re-render for a change that
      // was already on screen. That round-trip IS the reported lag: "the
      // video doesn't follow the blue box… it reacts very late".
      //
      // Exactly the same reduction, and the same caveat, as the sticker
      // tracks above: only safe while the preview genuinely skips the PIP
      // picture. If baking ever returns, restore the whole clip here or PIP
      // edits stop producing any visual result.
      clips: t.type === 'video' && t.id !== 'v1'
        ? t.clips.map((c) => {
          const k = c as unknown as {
            id: string; start: number; in?: number; out?: number
            speed?: unknown; audio?: unknown; src?: string; chromakey?: unknown
          }
          // A chromakey'd PIP is the one kind still baked in preview (see
          // pipIsClientDrawn), so it must keep its FULL clip here — reducing
          // it would mean moving or resizing it changed nothing on screen at
          // all, since no client draw is coming to show it.
          if (!pipIsClientDrawn(k)) return c
          return { id: k.id, src: k.src, start: k.start, in: k.in, out: k.out,
                   speed: k.speed, audio: k.audio }
        })
        : t.clips,
    })),
  })
}
