import { useEffect, useId } from 'react'
import { flushSync } from 'react-dom'
import { useLayoutStore } from '../../lib/layoutStore'
import { useAiRuns } from '../../lib/aiRuns'
import type { AiGroup } from '../../lib/aiCatalog'
import { Icon } from '../Icon'
import {
  DEEP_LINKS, allToolsLabel, catalogEntry, deepLinkKey, deepLinkStatus, rememberReturn, takeReturn,
  type DeepLinkPanel,
} from './deepLinks'
import { railItem, railPanelId, railTabId, type RailId } from './railModel'
import './deepLinks.css'

// The AI deep links a tool panel carries (docs/design/LEFT_RAIL_SPEC.md §2.5,
// M5; R5), and the AI panel's back chip that returns from one.
//
// A row is ONE catalogue tool: its label is the catalogue's, its status is the
// card's (the same lib/aiRuns sources AiToolCard reads), and a click asks the
// layout store for a jump (`jumpToAi`) that AiPanel consumes — select AI, clear
// its search, expand THAT card, scroll it to the top and focus its toggle. The
// row never renders a card of its own, so the form the user fills is the one
// the AI panel keeps.
//
// The back chip returns to the origin panel, restores that panel's scroll and
// focuses the row that was clicked. It goes away on the next tab change.

type Target = { tool: string; group?: undefined } | { tool?: undefined; group: AiGroup }

function jump(from: DeepLinkPanel, target: Target, el: HTMLElement) {
  const scroller = el.closest<HTMLElement>('.tool-tabpanel')
  rememberReturn({ from, key: deepLinkKey(target), scrollTop: scroller?.scrollTop ?? 0 })
  useLayoutStore.getState().jumpToAi({ ...target, from })
}

// The AI catalogue (/api/tools, /api/features, /api/downloads) is fetched the
// first time something needs it (lib/aiRuns guards the repeat). A panel with
// deep links needs it while it shows, so its rows can say "Not installed" or
// what a first run downloads; the Media panel shows at launch, so its rows
// wait for the pointer or focus instead of adding the ~2 s feature probe to
// every start.
const warmCatalog = () => { void useAiRuns.getState().loadCatalog() }

export function DeepLinkRow({ from, tool }: { from: DeepLinkPanel; tool: string }) {
  const entry = catalogEntry(tool)
  const run = useAiRuns((s) => s.runs[tool])
  const features = useAiRuns((s) => s.features)
  const downloads = useAiRuns((s) => s.downloads)
  const tools = useAiRuns((s) => s.tools)
  const id = useId()
  if (!entry) return null
  const status = deepLinkStatus(entry, { run, features, downloads, tools })
  return (
    <button
      type="button"
      className="deep-link"
      data-deep-link={deepLinkKey({ tool })}
      aria-labelledby={`${id}-l`}
      aria-describedby={status ? `${id}-s` : undefined}
      onClick={(e) => jump(from, { tool }, e.currentTarget)}
    >
      <Icon name="ai" className="deep-link-ico" />
      {/* The status sits under the label, never beside it: "Downloads 3.0 GB
          first" beside the label cut "Translate captions" to "Translate cap…"
          in a 220 px panel. */}
      <span className="deep-link-text">
        <span id={`${id}-l`} className="deep-link-label">{entry.label}</span>
        {status && <span id={`${id}-s`} className={`deep-link-status is-${status.tone}`}>{status.text}</span>}
      </span>
      <Icon name="chevronRight" className="deep-link-chev" />
    </button>
  )
}

export function DeepLinkGroupLink({ from, group }: { from: DeepLinkPanel; group: AiGroup }) {
  return (
    <button
      type="button"
      className="deep-group-link"
      data-deep-link={deepLinkKey({ group })}
      onClick={(e) => jump(from, { group }, e.currentTarget)}
    >
      {allToolsLabel(group)}
      <Icon name="chevronRight" />
    </button>
  )
}

/** A panel's AI section: its heading, one row per tool, then the group link.
 *  `active` (the panel is on screen) loads the catalogue for the statuses. */
export function DeepLinks({ from, active = false }: { from: DeepLinkPanel; active?: boolean }) {
  const spec = DEEP_LINKS[from]
  const id = useId()
  useEffect(() => { if (active) warmCatalog() }, [active])
  return (
    <div className="deep-links" role="group" aria-labelledby={`${id}-h`}
         onPointerEnter={warmCatalog} onFocus={warmCatalog}>
      <h3 id={`${id}-h`} className="section-label tool-section-label">{spec.heading}</h3>
      <div className="deep-link-list">
        {spec.tools.map((tool) => <DeepLinkRow key={tool} from={from} tool={tool} />)}
      </div>
      {spec.group && <DeepLinkGroupLink from={from} group={spec.group} />}
    </div>
  )
}

/** Back from the AI panel to the panel a deep link came from. */
function goBack(from: RailId) {
  const point = takeReturn(from)
  const layout = useLayoutStore.getState()
  // Commit the switch now, so the origin panel is laid out (no longer
  // `hidden`) before its scroll is restored and its row focused. The tool
  // panel's focus rescue runs inside this commit (focus on the chip, now
  // hidden, goes to the origin's rail tab); the row then takes it.
  flushSync(() => {
    layout.clearAiJump()
    layout.showTab(from)
  })
  const panel = document.getElementById(railPanelId(from))
  const row = point
    ? [...(panel?.querySelectorAll<HTMLElement>('[data-deep-link]') ?? [])].find((el) => el.dataset.deepLink === point.key)
    : undefined
  ;(row ?? document.getElementById(railTabId(from)))?.focus({ preventScroll: true })
  if (!panel || !point) return
  panel.scrollTop = point.scrollTop
  if (row) holdScroll(panel, row, point.scrollTop)
}

/** Events that mean the user is scrolling or acting in the panel. */
const USER_INPUT = ['wheel', 'touchstart', 'pointerdown', 'keydown'] as const
/** How long a restored scroll is defended against the engine's own reveal. */
const HOLD_SCROLL_MS = 400

/** WebKit (WKWebView, measured) still reveals a row that is partly out of
 *  view after a preventScroll focus in a panel that was display:none a moment
 *  ago — in the next rendering update, or (under load) the one after it, so
 *  one rAF was not enough (review RD3: the Text panel landed at 27 instead of
 *  0 in 2 of 3 runs). For a short while, while focus stays on the row and
 *  the user does nothing in the panel, any scroll is put back. */
function holdScroll(panel: HTMLElement, row: HTMLElement, top: number) {
  let live = true
  const restore = () => {
    if (live && document.activeElement === row && panel.scrollTop !== top) panel.scrollTop = top
  }
  const stop = () => {
    if (!live) return
    live = false
    panel.removeEventListener('scroll', restore)
    for (const t of USER_INPUT) panel.removeEventListener(t, stop, true)
  }
  panel.addEventListener('scroll', restore)
  for (const t of USER_INPUT) panel.addEventListener(t, stop, true)
  requestAnimationFrame(() => { restore(); requestAnimationFrame(restore) })
  setTimeout(stop, HOLD_SCROLL_MS)
}

export function AiBackChip() {
  const aiJump = useLayoutStore((s) => s.aiJump)
  if (!aiJump || aiJump.from === 'ai') return null
  const { from } = aiJump
  return (
    <button type="button" className="ai-back-chip" onClick={() => goBack(from)}>
      <Icon name="chevronLeft" />
      <span className="deep-sr-only">Back to </span>{railItem(from).label}
    </button>
  )
}
