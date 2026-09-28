// The order text overlays are drawn in, bottom first — the export's own
// (render/text_overlay.build_overlay_chain sorts by track z, then start;
// later composites on top). Review RE: TextLayer sorted by ROLE, so a text
// drew OVER a caption that the export draws on top of it (the captions track
// is z 13, the text tracks 10-12).
export interface TextDrawItem { z: number; c: { start: number } }

export function textDrawOrder(a: TextDrawItem, b: TextDrawItem): number {
  return (a.z - b.z) || (a.c.start - b.c.start)
}
