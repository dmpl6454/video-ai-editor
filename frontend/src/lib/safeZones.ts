// Safe-zone guides: where each short-video app's OWN chrome sits over a 9:16
// picture, so captions, hooks and lower-thirds can be placed where the status
// bar, the like/comment/share rail and the caption+nav band will not cover
// them. Pure data + math, kept out of the component so the numbers are
// unit-testable and the AI panel can share the coordinate convention.
//
// Every rect is a 0..1 fraction of the canvas (x, y, w, h from the top-left),
// the same convention lib/guideRects.ts uses, so the overlay maps them with a
// single multiply whatever the canvas size or preview zoom (`toPx`).
//
// These are APPROXIMATIONS, and the toggle says so. The apps move their chrome
// between releases, per device (notch, home indicator), per surface (For You
// feed vs. a profile grid) and per post (a long caption grows the bottom
// band), and none of the three publishes an authoritative overlay spec for
// ORGANIC posts — the numbers were derived from each app's layout at
// 1080×1920 and cross-checked against the ad-placement guidance where one
// exists (cited on each platform below, with the date it was checked). Treat
// a zone as "keep the important thing out of here", not as pixel truth.

export type SafePlatform = 'tiktok' | 'reels' | 'shorts'
export type SafeZoneMode = 'off' | SafePlatform

export interface Rect { x: number; y: number; w: number; h: number }
export interface SafeZone { id: string; label: string; rect: Rect }
export interface PlatformZones {
  id: SafePlatform
  label: string
  zones: SafeZone[]
  /** Where the numbers were cross-checked. */
  source: string
  /** ISO date of that check — bump it when the rects are re-derived. */
  fetched: string
}

export const SAFE_ZONES: Record<SafePlatform, PlatformZones> = {
  // TikTok, 1080×1920: ≈170 px of status bar + the Following / For You tabs
  // (top 9 %); the action rail — avatar, like, comment, save, share — runs
  // ≈150 px wide down the right edge from y≈800 to y≈1650; the bottom ≈420 px
  // carries handle, caption, sound ticker and the nav bar. TikTok's ad
  // safe-zone article (the `source` URL) is the only published guidance and
  // the help centre served an error page for it on the check date, so these
  // are the app-layout derivation, not a quoted figure.
  tiktok: {
    id: 'tiktok',
    label: 'TikTok',
    source: 'https://ads.tiktok.com/help/article/tiktok-video-ads-safe-zone',
    fetched: '2026-09-03',
    zones: [
      { id: 'top', label: 'Top bar', rect: { x: 0, y: 0, w: 1, h: 0.09 } },
      { id: 'rail', label: 'Action rail', rect: { x: 0.86, y: 0.42, w: 0.14, h: 0.44 } },
      { id: 'bottom', label: 'Caption & nav', rect: { x: 0, y: 0.78, w: 1, h: 0.22 } },
    ],
  },
  // Instagram Reels, 1080×1920: Meta's Reels ad guide (the `source` URL,
  // checked 2026-09-03) says to keep "at least 14% of the top, 35% of the
  // bottom, and 6% on each side" free. The top 14 % is used as-is. The 35 %
  // bottom includes the ad CTA band an organic Reel does not have; organic
  // chrome (handle, caption, audio pill, nav) ends ≈20 % up, which is what
  // the guide draws. The right rail (like / comment / share / more) is the
  // app's own layout — Meta only prescribes the 6 % side margin.
  reels: {
    id: 'reels',
    label: 'Reels',
    source: 'https://www.facebook.com/business/ads-guide/update/video/instagram-reels',
    fetched: '2026-09-03',
    zones: [
      { id: 'top', label: 'Top bar', rect: { x: 0, y: 0, w: 1, h: 0.14 } },
      { id: 'rail', label: 'Action rail', rect: { x: 0.85, y: 0.45, w: 0.15, h: 0.35 } },
      { id: 'bottom', label: 'Caption & nav', rect: { x: 0, y: 0.80, w: 1, h: 0.20 } },
    ],
  },
  // YouTube Shorts, 1080×1920: YouTube publishes no safe-zone spec (the
  // `source` help article, checked 2026-09-03, has none), so this is the
  // app's layout: ≈130 px of status + search/camera row (top 7 %), the like /
  // dislike / comment / share / remix rail on the right from mid-frame down,
  // and the channel, title, sound and nav band across the bottom 20 %.
  shorts: {
    id: 'shorts',
    label: 'Shorts',
    source: 'https://support.google.com/youtube/answer/10059070',
    fetched: '2026-09-03',
    zones: [
      { id: 'top', label: 'Top bar', rect: { x: 0, y: 0, w: 1, h: 0.07 } },
      { id: 'rail', label: 'Action rail', rect: { x: 0.85, y: 0.50, w: 0.15, h: 0.30 } },
      { id: 'bottom', label: 'Caption & nav', rect: { x: 0, y: 0.80, w: 1, h: 0.20 } },
    ],
  },
}

export const SAFE_ZONE_MODES: readonly SafeZoneMode[] = ['off', 'tiktok', 'reels', 'shorts']

export function isSafeZoneMode(v: unknown): v is SafeZoneMode {
  return typeof v === 'string' && (SAFE_ZONE_MODES as readonly string[]).includes(v)
}

const NINE_BY_SIXTEEN = 9 / 16
// 1080×1920 and 720×1280 are exactly 0.5625; a 1080×1350 (4:5) canvas is 0.8
// and 1:1 is 1.0, so the tolerance only has to absorb odd-pixel rounding.
const ASPECT_TOLERANCE = 0.01

export const NOT_916_HINT = 'Safe zones apply to a 9:16 canvas only — switch to 9:16 to see them'

export function isVertical916(canvas: { w: number; h: number }): boolean {
  if (!(canvas.h > 0)) return false
  return Math.abs(canvas.w / canvas.h - NINE_BY_SIXTEEN) < ASPECT_TOLERANCE
}

/** The zones to draw for a mode on a canvas. Empty on 'off'; empty WITH a
 *  hint on a non-9:16 canvas — the rects describe the vertical feed layout
 *  and would be nonsense stretched over a 16:9 frame. */
export function zonesFor(
  mode: SafeZoneMode, canvas: { w: number; h: number },
): { zones: SafeZone[]; hint: string | null } {
  if (mode === 'off') return { zones: [], hint: null }
  if (!isVertical916(canvas)) return { zones: [], hint: NOT_916_HINT }
  return { zones: SAFE_ZONES[mode].zones, hint: null }
}

/** Normalised rect → CSS px inside a box of `boxW`×`boxH` (the preview stage). */
export function toPx(
  rect: Rect, boxW: number, boxH: number,
): { left: number; top: number; width: number; height: number } {
  return { left: rect.x * boxW, top: rect.y * boxH, width: rect.w * boxW, height: rect.h * boxH }
}

// Persisted per browser profile, not per project: which app someone is
// cutting for is a habit of theirs, not a property of the timeline.
const STORE_KEY = 'vai.safeZones'

export function readStoredMode(): SafeZoneMode {
  try {
    const v = localStorage.getItem(STORE_KEY)
    if (isSafeZoneMode(v)) return v
  } catch { /* node (vitest), private mode, storage disabled — 'off' is the safe default */ }
  return 'off'
}

export function writeStoredMode(mode: SafeZoneMode): void {
  try {
    localStorage.setItem(STORE_KEY, mode)
  } catch { /* a toggle that forgets across reloads still works for this session */ }
}
