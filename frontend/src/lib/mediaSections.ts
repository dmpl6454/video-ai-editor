/** The media-clip inspector's sections (components/Properties), by name, in
 *  the order they render; the clip inspector shows a subset per tab. */
export const MEDIA_SECTIONS = ['Timing', 'Speed', 'Animation', 'Color', 'Video fade', 'Audio', 'Voice effects', 'Framing', 'Canvas', 'PIP shape', 'Blend', 'Transform'] as const
export type MediaSection = typeof MEDIA_SECTIONS[number]
