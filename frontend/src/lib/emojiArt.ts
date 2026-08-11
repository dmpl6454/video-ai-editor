// Emoji artwork for canvas layers.
//
// Preview and export must show the SAME pixels, and a Mac and a Windows user
// opening the same project must too — so an emoji is never painted with the OS
// font (Apple Color Emoji vs Segoe UI Emoji vs the Fluent 3D art the server
// bakes are three different designs). The browser fetches the identical artwork
// the bake uses from `GET /api/emoji/{codepoints}.png`.
//
// Lives here, rather than inside the component that draws it, so the arrival
// signal below is unit-testable: it is the whole bug.

/** `null` = known-unavailable (404), so we stop asking. */
const CACHE = new Map<string, HTMLImageElement | 'loading' | null>()

let generation = 0

/** Bumped every time a fetch LANDS (loaded or failed).
 *
 *  Artwork is requested DURING a draw and arrives after it. A canvas layer that
 *  repaints only when something it knows about changed — TextLayer skips the
 *  frame unless the playhead moved — therefore paints exactly one frame for a
 *  paused preview: the one where the image is still in flight, so the emoji's
 *  box is reserved and left blank. Nothing repaints it, and the gap is
 *  permanent ("it left an empty space in place of the emoji I entered in the
 *  text"). Comparing this counter across frames makes an arriving image a
 *  redraw trigger in its own right, the same role `fontsReady` plays for a
 *  late-loading font.
 */
export function emojiGeneration(): number { return generation }

/** Codepoint sequence used by the artwork route: 'a-b-c', lowercase hex. */
export function codepointSeq(cluster: string): string {
  return Array.from(cluster).map((ch) => ch.codePointAt(0)!.toString(16)).join('-')
}

/** The artwork for one emoji cluster, or null while loading / if unavailable.
 *  Kicks off the fetch on first ask; safe to call every frame. */
export function emojiImage(cluster: string): HTMLImageElement | null {
  const cached = CACHE.get(cluster)
  // Not `instanceof HTMLImageElement`: the only non-image entries are the two
  // sentinels, and a narrower check keeps this usable wherever Image is stubbed.
  if (cached !== undefined) return cached === 'loading' || cached === null ? null : cached
  CACHE.set(cluster, 'loading')
  const img = new Image()
  img.onload = () => { CACHE.set(cluster, img); generation++ }
  img.onerror = () => { CACHE.set(cluster, null); generation++ }
  img.src = `/api/emoji/${codepointSeq(cluster)}.png`
  return null
}
