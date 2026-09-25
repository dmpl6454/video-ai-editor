// THE app's icon set: monochrome 16 px line icons drawn in `currentColor`, so
// the surrounding text token colours them — no emoji and no palette of their
// own. Started as the timeline toolbar's set (QA-053); promoted so every
// surface uses ONE set (wave-B review: colour emoji 💾 📂 🎵 😀 ✨ 🎞️ and a ⌨
// glyph sat next to these SVGs). Decorative by default (aria-hidden): the
// button that holds one carries the name.

export type IconName =
  | 'split' | 'delete' | 'duplicate' | 'snap' | 'zoomOut' | 'zoomIn' | 'fit'
  | 'play' | 'pause' | 'undo' | 'redo'
  | 'save' | 'open' | 'keyboard' | 'music' | 'sticker' | 'effects' | 'film'
  | 'plus' | 'addToTimeline' | 'check' | 'image'

const PATHS: Record<IconName, string> = {
  // Blade: two handles and crossed blades.
  split: 'M4.5 3.5a1.75 1.75 0 1 0 0 3.5a1.75 1.75 0 0 0 0-3.5ZM4.5 9a1.75 1.75 0 1 0 0 3.5a1.75 1.75 0 0 0 0-3.5ZM6 6.2l7 5.3M6 9.8l7-5.3',
  delete: 'M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8.5h5.8l.6-8.5M7 7v4M9 7v4',
  duplicate: 'M5.5 5.5h7v7h-7zM3.5 10.5v-7h7',
  // Magnet: a U with its two pole caps.
  snap: 'M4 3v5a4 4 0 0 0 8 0V3M4 5.5h2.5M9.5 5.5H12M6.5 3v5a1.5 1.5 0 0 0 3 0V3',
  zoomOut: 'M7 2.5a4.5 4.5 0 1 1 0 9a4.5 4.5 0 0 1 0-9ZM10.3 10.3l3.2 3.2M5 7h4',
  zoomIn: 'M7 2.5a4.5 4.5 0 1 1 0 9a4.5 4.5 0 0 1 0-9ZM10.3 10.3l3.2 3.2M5 7h4M7 5v4',
  // Fit: two brackets with inward arrows.
  fit: 'M2.5 4v8M13.5 4v8M4.5 8h2.5M5.8 6.7 4.5 8l1.3 1.3M11.5 8H9M10.2 6.7 11.5 8l-1.3 1.3',
  play: 'M5 3.5v9l7-4.5z',
  pause: 'M5 3.5v9M11 3.5v9',
  undo: 'M5.5 4 3 6.5 5.5 9M3 6.5h6.5a3.5 3.5 0 0 1 0 7H7',
  redo: 'M10.5 4 13 6.5 10.5 9M13 6.5H6.5a3.5 3.5 0 0 0 0 7H9',
  // Save: an arrow down into a tray.
  save: 'M8 2.5v7M5 6.5l3 3 3-3M2.5 10.5v3h11v-3',
  // Open: a folder.
  open: 'M2.5 4h4l1.5 1.5h5.5v7h-11zM2.5 7h11',
  keyboard: 'M1.5 4.5h13v7h-13zM4 7h.5M6.5 7h.5M9 7h.5M11.5 7h.5M5 9.5h6',
  // A beamed pair of notes.
  music: 'M6 11.5V4l7-1.5V10M6 11.5a1.75 1.75 0 1 1-3.5 0a1.75 1.75 0 0 1 3.5 0ZM13 10a1.75 1.75 0 1 1-3.5 0a1.75 1.75 0 0 1 3.5 0ZM6 6.5l7-1.5',
  // A sticker: a square with its corner peeled.
  sticker: 'M3 3h10v6l-4 4H3zM9 13v-4h4M6 6.5h.5M9.5 6.5h.5',
  // A four-point spark.
  effects: 'M8 2l1.2 3.8L13 7l-3.8 1.2L8 12l-1.2-3.8L3 7l3.8-1.2z',
  // A film frame with sprockets.
  film: 'M3 2.5h10v11H3zM3 5h10M3 11h10M5.5 2.5V5M10.5 2.5V5M5.5 11v2.5M10.5 11v2.5',
  plus: 'M8 3v10M3 8h10',
  // Add to timeline: a plus over two lanes.
  addToTimeline: 'M8 2v6M5 5h6M2.5 10.5h11M2.5 13.5h11',
  check: 'M3.5 8.5l3 3 6-7',
  image: 'M2.5 3.5h11v9h-11zM2.5 10.5l3-3 3 3 2-2 3 3M10.5 6h.5',
}

export function Icon({ name, size = 16 }: { name: IconName; size?: number }) {
  const filled = name === 'play'
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" aria-hidden="true" focusable="false"
      className="icon" fill={filled ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth={1.4}
      strokeLinecap="round" strokeLinejoin="round">
      <path d={PATHS[name]} />
    </svg>
  )
}
