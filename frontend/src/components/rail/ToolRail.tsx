import { useRef, type KeyboardEvent } from 'react'
import { useLayoutStore } from '../../lib/layoutStore'
import { useAiRuns } from '../../lib/aiRuns'
import { isBusy, usePromptStore } from '../../lib/promptStore'
import { useActivityStore } from '../../lib/activityStore'
import { Icon } from '../Icon'
import { openHelp } from '../Help'
import { openShortcuts } from '../ShortcutsSettings'
import { openSettings } from '../../lib/settingsOpen'
import { RAIL_ITEMS, railKeyStep, railPanelId, railTabId, type RailId, type RailItem } from './railModel'
import { useRailChord } from './useRailChord'
import './rail.css'

// The left tool rail (docs/design/LEFT_RAIL_SPEC.md §2): one vertical tablist
// that drives the tool panel. Each item is an icon over its label; the label is
// the tab's exact accessible name (tests find tabs by it). Below 1280 px the
// label is visually hidden and the portalled tooltip (RailTooltip) shows it.
//
// Keyboard (APG tabs, vertical, automatic activation): the list is ONE tab
// stop (roving tabindex); ↑/↓/Home/End move AND show that panel, opening a
// collapsed one; Enter or Space on the active tab collapses or re-opens the
// panel exactly like a click (§2.4). The rail is NOT a `data-keymap-ignore`
// scope (review RD1): that scope silenced every global shortcut — ⌘Z, J/K/L,
// N — while a rail tab had focus, which the LeftPane tabs it replaced never
// did. Instead the keymap engine leaves a focused tab its own Space and Enter
// (engine.ts `shouldRun`, R4) and ↑/↓/Home/End (a focused button's keys), and
// runs every other chord.
//
// A mouse click does not move focus onto a tab (mousedown preventDefault): a
// tab left focused by the pointer would otherwise take the next Space as a
// "click" in engines where the keymap does not run first.

/** sr-only descriptions for the live-state dots (aria-describedby; never part
 *  of the tab's name, and not `aria-description`, whose WebKit support is
 *  uneven). */
const DOT_TEXT: Partial<Record<RailId, string>> = {
  audio: 'Recording in progress',
  captions: 'Captions are being generated',
  ai: 'An AI tool is running',
}

/** A dot's look: `rec` (--accent) while a voiceover records, `busy`
 *  (--accent-2) while captions transcribe or an AI run holds the lock. */
type Dot = 'rec' | 'busy'

export function ToolRail() {
  const leftTab = useLayoutStore((s) => s.leftTab)
  const leftOpen = useLayoutStore((s) => s.leftOpen)
  const showTab = useLayoutStore((s) => s.showTab)
  // The AI dot (§2.3): a run holds the session lock — the Prompt bar's run
  // (lib/promptStore: planning/running/verifying, while /dispatch answers
  // 409) — or an AI tool card's job is running (lib/aiRuns), which the cards
  // show with their own progress.
  const promptBusy = usePromptStore((s) => isBusy(s.status))
  const cardBusy = useAiRuns((s) => Object.values(s.runs).some((r) => r.status === 'running'))
  const aiBusy = promptBusy || cardBusy
  const tabs = useRef<Partial<Record<RailId, HTMLButtonElement | null>>>({})

  const selected = Math.max(0, RAIL_ITEMS.findIndex((r) => r.id === leftTab))

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const next = railKeyStep(e.key, selected, RAIL_ITEMS.length)
    if (next < 0) return
    e.preventDefault()
    const id = RAIL_ITEMS[next].id
    showTab(id)
    tabs.current[id]?.focus()
  }

  // The Audio and Captions dots (§2.3) come from lib/activityStore — the same
  // run the top bar's activity chip shows, which is the primary cue.
  const recording = useActivityStore((s) => !!s.recording)
  const transcribing = useActivityStore((s) => !!s.captions)
  const dots: Partial<Record<RailId, Dot>> = {
    ...(recording ? { audio: 'rec' as const } : {}),
    ...(transcribing ? { captions: 'busy' as const } : {}),
    ...(aiBusy ? { ai: 'busy' as const } : {}),
  }

  return (
    <nav className="rail" aria-label="Tools">
      <div
        className="rail-tabs"
        role="tablist"
        aria-orientation="vertical"
        aria-label="Tool panels"
        onKeyDown={onKeyDown}
        style={{ '--sel': selected } as React.CSSProperties}
      >
        <span className="rail-indicator" aria-hidden="true" />
        {RAIL_ITEMS.map((item) => (
          <RailTab
            key={item.id}
            item={item}
            selected={item.id === leftTab}
            open={leftOpen}
            dot={dots[item.id]}
            tabRef={(el) => { tabs.current[item.id] = el }}
            onClick={() => {
              showTab(item.id, { toggle: true })
              // Keyboard focus already on another rail tab follows the click
              // (roving tabindex): left behind on a tab that is no longer
              // selected, the next Space or Enter would activate THAT tab.
              if (document.activeElement?.closest('nav.rail [role="tablist"]')) tabs.current[item.id]?.focus()
            }}
          />
        ))}
      </div>
      {/* `hidden`: out of the reading order (a screen reader walking the nav
          must not hear "An AI tool is running" when none is), yet still the
          aria-describedby target the busy tab points at. */}
      {Object.entries(DOT_TEXT).map(([id, text]) => (
        <span key={id} id={`rail-desc-${id}`} className="rail-sr-only" hidden>{text}</span>
      ))}
    </nav>
  )
}

function RailTab({ item, selected, open, dot, tabRef, onClick }: {
  item: RailItem
  selected: boolean
  open: boolean
  dot: Dot | undefined
  tabRef: (el: HTMLButtonElement | null) => void
  onClick: () => void
}) {
  const chord = useRailChord(item.command)
  return (
    <button
      ref={tabRef}
      type="button"
      role="tab"
      id={railTabId(item.id)}
      className="rail-item"
      aria-selected={selected}
      aria-controls={railPanelId(item.id)}
      // aria-expanded only on the SELECTED tab (a supported state on `tab`):
      // it says whether the panel that tab owns is open or collapsed.
      aria-expanded={selected ? open : undefined}
      aria-keyshortcuts={chord.aria || undefined}
      aria-describedby={dot ? `rail-desc-${item.id}` : undefined}
      tabIndex={selected ? 0 : -1}
      data-tip={`${item.label} — ${item.tip}`}
      data-kbd={chord.label || undefined}
      onMouseDown={(e) => e.preventDefault()}
      onClick={onClick}
    >
      <span className="rail-chip"><Icon name={item.icon} /></span>
      <span className="rail-label">{item.label}</span>
      {dot && <span className={`rail-dot rail-dot-${dot}`} aria-hidden="true" />}
    </button>
  )
}

// The rail's foot (§2.2, R3): Help, Customize keyboard shortcuts and Settings,
// the utilities that left the top bar. Its OWN grid item (`foot`), mounted by
// App last in the DOM: Tab from a rail tab goes into its panel (APG tabs), and
// the utilities come last in the focus order and in F6's region walk. Each is
// a 36 px icon button named exactly as before — "Keyboard shortcuts" opens the
// dialog of that title (M1) — with its live chord in aria-keyshortcuts and the
// tooltip, which opens to the right like the rail's own.
export function RailFoot() {
  const shortcuts = useRailChord('openShortcuts')
  const settings = useRailChord('openSettings')
  return (
    <nav className="rail-foot" aria-label="Help and settings">
      {/* `?` is Help's own document listener (Help.tsx), not a keymap command. */}
      <button type="button" className="rail-foot-btn" aria-label="Keyboard shortcuts" aria-keyshortcuts="?"
              data-tip="Help and keyboard shortcuts" data-kbd="?" onClick={openHelp}>
        <Icon name="help" />
      </button>
      <button type="button" className="rail-foot-btn" aria-label="Customize keyboard shortcuts"
              aria-keyshortcuts={shortcuts.aria || undefined}
              data-tip="Customize keyboard shortcuts (CapCut / Premiere / Final Cut)" data-kbd={shortcuts.label || undefined}
              onClick={openShortcuts}>
        <Icon name="keyboard" />
      </button>
      <button type="button" className="rail-foot-btn" aria-label="Settings"
              aria-keyshortcuts={settings.aria || undefined}
              data-tip="Settings" data-kbd={settings.label || undefined} onClick={openSettings}>
        <Icon name="settings" />
      </button>
    </nav>
  )
}
