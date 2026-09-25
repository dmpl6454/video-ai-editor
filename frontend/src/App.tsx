import { useEffect, useState } from 'react'
import { useStore, startSessionWatch } from './store'
import { ConnectionBanner } from './components/ConnectionBanner'
import { TopBar } from './components/TopBar'
import { LeftPane } from './components/LeftPane'
import { Preview } from './components/Preview'
import { PromptBar } from './components/PromptBar'
import { ErrorBoundary } from './components/ErrorBoundary'
import { Timeline } from './components/Timeline'
import { Properties } from './components/Properties'
import { OpsLog } from './components/OpsLog'
import { ChatOverlay } from './components/ChatOverlay'
import { Help } from './components/Help'
import { FileDropOverlay } from './components/FileDropOverlay'
import { ShortcutsSettings } from './components/ShortcutsSettings'
import { ExportModal } from './components/ExportModal'
import { CaptionStylePanel } from './components/CaptionStylePanel'
import { ToastHost } from './components/Toast'
import { Splitter } from './components/Splitter'
import { browserStorage, readRightTab, writeRightTab, type RightTab } from './lib/rightTab'
import { useKeymap } from './keymap/engine'

// The 3-pane editor holds a 900px floor (see .app in styles.css) and scrolls
// horizontally below it; this banner nudges the user to a wider window.
const MIN_EDITOR_WIDTH = 900

// Width of the right sidebar's collapsed rail (Task 4b) — just enough for the
// re-expand tab, so the center pane reclaims the rest without a jarring
// reflow (the column shrinks to a fixed rail rather than to 0).
const RIGHT_RAIL_W = 28

export default function App() {
  const init = useStore((s) => s.init)
  useEffect(() => { void init() }, [init])
  useKeymap()  // customizable CapCut / Premiere / Final Cut keymaps
  // QA-105/109: notice edits made in another window (focus, visibility, a
  // light poll) and whether the engine is still there.
  useEffect(() => startSessionWatch(window), [])

  // Resizable panel sizes (Task 9) — persisted in the store (localStorage-
  // backed); drive them onto the .app/.center grids as CSS custom properties
  // so styles.css's `var(--left-w, 220px)` etc. pick them up.
  const leftW = useStore((s) => s.leftW)
  const rightW = useStore((s) => s.rightW)
  const timelineH = useStore((s) => s.timelineH)
  const setPanelSize = useStore((s) => s.setPanelSize)
  const rightPanelOpen = useStore((s) => s.rightPanelOpen)
  const setRightPanelOpen = useStore((s) => s.setRightPanelOpen)

  const [viewportWidth, setViewportWidth] = useState(() =>
    typeof window === 'undefined' ? MIN_EDITOR_WIDTH : window.innerWidth)
  const [narrowDismissed, setNarrowDismissed] = useState(false)
  useEffect(() => {
    const onResize = () => setViewportWidth(window.innerWidth)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])
  const showNarrowWarning = viewportWidth < MIN_EDITOR_WIDTH && !narrowDismissed

  // The right sidebar's tab: Inspector (default) or the docked Chat (QA-061).
  // Remembered per browser, so closing Chat stays closed across reloads.
  const [rightTab, setRightTabState] = useState<RightTab>(() => readRightTab(browserStorage()))
  const setRightTab = (t: RightTab) => { setRightTabState(t); writeRightTab(browserStorage(), t) }
  const onTabKey = (e: React.KeyboardEvent) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight' && e.key !== 'Home' && e.key !== 'End') return
    e.preventDefault()
    const next: RightTab = e.key === 'Home' ? 'inspect' : e.key === 'End' ? 'chat' : rightTab === 'chat' ? 'inspect' : 'chat'
    setRightTab(next)
    document.getElementById(`right-tab-${next}`)?.focus()
  }

  const appVars = {
    '--left-w': `${leftW}px`,
    // Collapsed: shrink the grid column to a thin rail instead of hiding it
    // outright — avoids a reflow jump and leaves room for the re-expand tab.
    '--right-w': rightPanelOpen ? `${rightW}px` : `${RIGHT_RAIL_W}px`,
    '--timeline-h': `${timelineH}px`,
  } as React.CSSProperties

  return (
    <>
      {showNarrowWarning && (
        <div className="narrow-warning" role="status">
          <span className="nw-icon" aria-hidden="true">↔</span>
          <span className="nw-msg">
            Please use a wider window (min {MIN_EDITOR_WIDTH}px) for the best experience.
          </span>
          <button className="nw-dismiss" onClick={() => setNarrowDismissed(true)}>
            Dismiss
          </button>
        </div>
      )}
    <div className="app" style={appVars}>
      <TopBar />
      <aside className="sidebar left">
        <LeftPane />
      </aside>
      <Splitter
        orientation="vertical"
        style={{ gridArea: 'lsplit' }}
        // Reads the live value via getState() rather than the `leftW` closed
        // over by this render: a real drag fires many mousemove events per
        // React commit, so every one of them would otherwise add its delta
        // to the SAME stale base — losing all but the last-flushed delta
        // (verified live: a 10-step 80px drag only moved the panel 8px, and
        // a genuine Playwright mouse drag could even net-shrink the panel).
        onDelta={(d) => setPanelSize('leftW', useStore.getState().leftW + d)}
      />
      <main className="center">
        {/* One sentence → a verified, single-undo edit. Above the picture,
            never over it; the .center grid's first (auto) row is its home
            (styles.css). Tools, chat and the phone all share the session
            lock with it — see lib/promptStore.ts. */}
        <PromptBar />
        <div className="preview-pane">
          <ErrorBoundary
            fallback={(err) => (
              <div className="preview-empty" style={{ padding: 16, textAlign: 'center' }}>
                <div style={{ fontSize: 24, marginBottom: 6 }}>⚠️</div>
                <div>Preview hit an error and was paused.</div>
                <div style={{ marginTop: 6, fontSize: 11, color: 'var(--text-dim)' }}>
                  {err.message}
                </div>
              </div>
            )}
          >
            <Preview />
          </ErrorBoundary>
        </div>
        <Splitter
          orientation="horizontal"
          onDelta={(d) => setPanelSize('timelineH', useStore.getState().timelineH - d)}
        />
        <div className="timeline-pane">
          <Timeline />
        </div>
      </main>
      <Splitter
        orientation="vertical"
        style={{ gridArea: 'rsplit' }}
        // Dragging right moves the mouse away from the right sidebar, which
        // should shrink it — the delta sign is negated relative to leftW.
        onDelta={(d) => setPanelSize('rightW', useStore.getState().rightW - d)}
        // While collapsed the rail is only 28px — dragging it shouldn't
        // silently un-collapse the panel; only the explicit tab does that.
        disabled={!rightPanelOpen}
      />
      <aside className={`sidebar right${rightPanelOpen ? '' : ' collapsed'}${rightTab === 'chat' ? ' is-chat' : ''}`}>
        <button
          className="right-panel-toggle"
          onClick={() => setRightPanelOpen(!rightPanelOpen)}
          title={rightPanelOpen ? 'Collapse panel' : 'Expand panel'}
          aria-label={rightPanelOpen ? 'Collapse properties panel' : 'Expand properties panel'}
          aria-expanded={rightPanelOpen}
        >
          <span aria-hidden="true">{rightPanelOpen ? '›' : '‹'}</span>
        </button>
        <div className="right-panel-content">
          {/* Chat is DOCKED here as a tab (QA-061) — it used to float over the
              timeline tracks and History, open on every load. */}
          <div className="right-tabs" role="tablist" aria-label="Right panel" data-keymap-ignore onKeyDown={onTabKey}>
            <button type="button" role="tab" id="right-tab-inspect" aria-controls="right-panel-inspect"
                    aria-selected={rightTab === 'inspect'} tabIndex={rightTab === 'inspect' ? 0 : -1}
                    onClick={() => setRightTab('inspect')}>Inspector</button>
            <button type="button" role="tab" id="right-tab-chat" aria-controls="right-panel-chat"
                    aria-selected={rightTab === 'chat'} tabIndex={rightTab === 'chat' ? 0 : -1}
                    onClick={() => setRightTab('chat')}>Chat</button>
          </div>
          <div role="tabpanel" id="right-panel-inspect" aria-labelledby="right-tab-inspect" hidden={rightTab !== 'inspect'}>
            <Properties />
            <OpsLog />
          </div>
          {/* Kept mounted while hidden: a turn keeps streaming, and the
              conversation is still there when the tab is reopened. */}
          <div role="tabpanel" id="right-panel-chat" aria-labelledby="right-tab-chat" className="right-chat" hidden={rightTab !== 'chat'}>
            <ChatOverlay onClose={() => setRightTab('inspect')} />
          </div>
        </div>
      </aside>
      <Help />
      <ShortcutsSettings />
      <FileDropOverlay />
      <ConnectionBanner />
    </div>
    <ExportModal />
    <CaptionStylePanel />
    <ToastHost />
    </>
  )
}
