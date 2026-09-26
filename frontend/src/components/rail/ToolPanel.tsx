import { forwardRef, lazy, Suspense, useEffect, useState, type ReactNode } from 'react'
import { useLayoutStore } from '../../lib/layoutStore'
import { Icon } from '../Icon'
import { MediaBin } from '../MediaBin'
import { AudioPanel } from '../panels/AudioPanel'
import { TextPanel } from '../panels/TextPanel'
import { CaptionsPanel } from '../panels/CaptionsPanel'
import { StickerPanel } from '../StickerPanel'
import { EffectsPanel } from '../EffectsPanel'
import { TransitionsPanel } from '../TransitionsPanel'
import { AiPanel } from '../AiPanel'
import { DeepLinks } from './DeepLinkRow'
import { RAIL_ITEMS, railItem, railPanelId, railTabId, type RailId } from './railModel'
import { useFocusRescue } from './focusRescue'
import { useRailChord } from './useRailChord'
import { loadPhonePairing, usePhonePairing } from './phonePairing'
import '../aiPanel.css'
import './rail.css'

// The tool panel the rail drives (docs/design/LEFT_RAIL_SPEC.md §2.1, §2.5,
// §2.7). It replaces LeftPane's three-tab strip: a 36 px header (the panel's
// name as an h2, its chord while one is bound, the collapse button), then one
// tabpanel per rail item.
//
// Every tabpanel stays MOUNTED and the inactive ones are `hidden`; a collapsed
// panel hides the whole <section>. The Audio panel hosts VoRecorder, which owns
// a live MediaRecorder (or the native capture in the packaged app): unmounting
// it mid-recording would orphan the recording. Hiding also keeps the AI cards'
// open forms and typed values, the sticker search and each panel's scroll
// position (each tabpanel scrolls on its own).
//
// `active` (= open AND selected) is how a hidden-but-mounted panel learns it is
// off screen: AiPanel takes its guide rectangles off the preview, and the
// Stickers / Effects / Transitions panels defer their first fetch until shown,
// and Captions re-reads which caption models are on disk each time it shows.

// The pairing panel's code loads on the first open only: with the flag off it
// is never fetched, so nothing of the LAN feature reaches the page (R3).
const PhonePanel = lazy(() => import('../PhonePanel').then((m) => ({ default: m.PhonePanel })))

/** The Media panel's header action (§2.5, R3), rendered ONLY while this build
 *  reports `phone_pairing: true` — absent from the markup otherwise, not
 *  hidden. The panel shows a live credential, so it is mounted only while
 *  open (a merely hidden one is one stylesheet mistake from a code on screen).
 *  Its word hides when the tool panel is under 260 px (@container). */
function FromIPhone() {
  const enabled = usePhonePairing((s) => s.enabled)
  const [open, setOpen] = useState(false)
  useEffect(() => { void loadPhonePairing() }, [])
  if (!enabled) return null
  return (
    <>
      <button
        type="button"
        className="tool-panel-act"
        aria-label="From iPhone"
        data-tip="Connect an iPhone to this Mac — the phone edits, this Mac does the work"
        onClick={() => setOpen(true)}
      >
        <Icon name="phone" /><span className="tool-panel-act-word" aria-hidden="true">From iPhone</span>
      </button>
      {open && <Suspense fallback={null}><PhonePanel onClose={() => setOpen(false)} /></Suspense>}
    </>
  )
}

function panelContent(id: RailId, active: boolean): ReactNode {
  switch (id) {
    case 'media': return <MediaBin />
    case 'audio': return <AudioPanel active={active} />
    case 'text': return <TextPanel active={active} />
    case 'stickers': return <StickerPanel active={active} />
    // The Cutout & effects (AI) deep links sit under EffectsPanel here, not
    // inside it (EffectsPanel.tsx belongs to instant preview Phase 2; R5).
    case 'effects': return <><EffectsPanel active={active} /><DeepLinks from="effects" active={active} /></>
    case 'transitions': return <TransitionsPanel active={active} />
    case 'captions': return <CaptionsPanel active={active} />
    case 'ai': return <AiPanel active={active} />
    default: return null
  }
}

export const ToolPanel = forwardRef<HTMLElement>(function ToolPanel(_props, ref) {
  const leftTab = useLayoutStore((s) => s.leftTab)
  const leftOpen = useLayoutStore((s) => s.leftOpen)
  const setLeftOpen = useLayoutStore((s) => s.setLeftOpen)
  const item = railItem(leftTab)
  const chord = useRailChord(item.command)

  const selectedTab = () => document.getElementById(railTabId(useLayoutStore.getState().leftTab))
  // Focus inside the panel when it switches or collapses goes to the selected
  // rail tab (the newly chosen one on a switch); focus elsewhere stays put.
  const rescue = useFocusRescue(selectedTab, [leftTab, leftOpen])

  const collapse = () => {
    setLeftOpen(false)
    selectedTab()?.focus()
  }

  return (
    <section
      ref={ref}
      className="tool-panel"
      id="tool-panel"
      aria-labelledby="tool-panel-title"
      hidden={!leftOpen}
      onFocus={rescue.onFocus}
      onBlur={rescue.onBlur}
    >
      <div className="tool-panel-head">
        <h2 id="tool-panel-title">{item.label}</h2>
        {chord.label && <span className="tool-panel-kbd" aria-hidden="true">{chord.label}</span>}
        <span className="tool-panel-grow" />
        {leftTab === 'media' && <FromIPhone />}
        <button
          type="button"
          className="tool-panel-collapse"
          aria-label="Hide the tool panel"
          aria-controls="tool-panel"
          aria-expanded={leftOpen}
          data-tip="Hide the tool panel"
          onClick={collapse}
        >
          <Icon name="panelLeftClose" />
        </button>
      </div>
      {RAIL_ITEMS.map((r) => (
        <div
          key={r.id}
          role="tabpanel"
          id={railPanelId(r.id)}
          aria-labelledby={railTabId(r.id)}
          className="tool-tabpanel"
          data-panel={r.id}
          hidden={leftTab !== r.id}
        >
          {panelContent(r.id, leftOpen && leftTab === r.id)}
        </div>
      ))}
    </section>
  )
})
