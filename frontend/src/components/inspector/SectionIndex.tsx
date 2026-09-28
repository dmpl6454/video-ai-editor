// A jump list at the top of the media clip Inspector (review RE): the
// Inspector is one ~2,200 px scroll (Canvas and Transform two screens down
// at 1440×900), and CapCut users expect Video / Audio / Speed / Animation
// tabs. The list names the sections that are actually there (read from the
// rendered `data-section` blocks) and moves focus into the one chosen —
// keyboard operable, and a plain jump when the viewer reduces motion.
import React from 'react'
import { useReducedMotion } from '../../lib/useReducedMotion'
import { scrollParentOf, sectionScrollTop } from '../../lib/sectionJump'
import './sectionIndex.css'

/** The DOM id of a section (Properties.tsx `Section`). */
export function sectionId(label: string): string {
  return `props-sec-${label.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`
}

/** Short names for the chips. */
const SHORT: Record<string, string> = { 'Voice effects': 'Voice', 'Video fade': 'Fade', 'PIP shape': 'Shape' }

export function SectionIndex({ clipId }: { clipId: string }) {
  const ref = React.useRef<HTMLElement>(null)
  const [labels, setLabels] = React.useState<string[]>([])
  const still = useReducedMotion()
  React.useLayoutEffect(() => {
    const props = ref.current?.closest('.props')
    if (!props) return
    const found = Array.from(props.querySelectorAll<HTMLElement>('[data-section]')).map((el) => el.dataset.section ?? '')
    setLabels((prev) => (prev.join('|') === found.join('|') ? prev : found))
  }, [clipId])
  const go = (label: string) => {
    const el = document.getElementById(sectionId(label))
    if (!el) return
    // Scroll the Inspector's OWN container so the section lands below the
    // sticky bar (it wraps to 1–3 rows; lib/sectionJump). scrollIntoView
    // hid the header under the bar and nudged the document as well.
    const box = scrollParentOf(el)
    const bar = ref.current
    if (box && bar) {
      const top = sectionScrollTop({
        containerTop: box.getBoundingClientRect().top,
        clientTop: box.clientTop,
        scrollTop: box.scrollTop,
        sectionTop: el.getBoundingClientRect().top,
        barHeight: bar.getBoundingClientRect().height,
      })
      box.scrollTo({ top, behavior: still ? 'auto' : 'smooth' })
    }
    el.focus({ preventScroll: true })
  }
  if (labels.length < 4) return <nav ref={ref} aria-hidden="true" className="section-index section-index-empty" />
  return (
    <nav ref={ref} className="section-index" aria-label="Jump to an Inspector section">
      {labels.map((l) => (
        <button key={l} type="button" className="section-index-chip" onClick={() => go(l)}
                aria-label={`Jump to ${l}`}>{SHORT[l] ?? l}</button>
      ))}
    </nav>
  )
}
