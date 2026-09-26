import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { ICON_STROKE } from '../lib/icons'
import { MediaToolsBannerView } from './MediaToolsBanner'

const problem = { missing: ['ffmpeg', 'ffprobe'], command: 'brew install ffmpeg',
  message: "Video AI Editor can't find ffmpeg and ffprobe. Install it with brew install ffmpeg, then reopen the app." }
const noop = () => undefined

// REVIEW-C7-BANNER-DIRECT-LUCIDE: the banner imported TriangleAlert/Copy/
// RefreshCw straight from lucide-react, skipping the icon map's 1.75 stroke
// and data-icon. COHERENCE: it had no way to close it.
describe('MediaToolsBanner', () => {
  const html = renderToStaticMarkup(createElement(MediaToolsBannerView, {
    problem, copied: false, checking: false, onCopy: noop, onCheck: noop, onDismiss: noop,
  }))

  it('draws every icon through the app icon map', () => {
    const svgs = html.match(/<svg[^>]*>/g) ?? []
    expect(svgs.length).toBe(4)
    for (const svg of svgs) {
      expect(svg).toMatch(/data-icon="(warning|copy|refresh|close)"/)
      expect(svg).toContain(`stroke-width="${ICON_STROKE}"`)
    }
  })

  it('can be hidden, with a name on the icon-only control', () => {
    expect(html).toMatch(/<button[^>]*aria-label="Hide this notice"/)
  })
})
