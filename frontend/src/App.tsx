import { useEffect, useState } from 'react'
import { useStore } from './store'
import { TopBar } from './components/TopBar'
import { MediaBin } from './components/MediaBin'
import { Preview } from './components/Preview'
import { ErrorBoundary } from './components/ErrorBoundary'
import { Timeline } from './components/Timeline'
import { Properties } from './components/Properties'
import { OpsLog } from './components/OpsLog'
import { ChatOverlay } from './components/ChatOverlay'
import { Help } from './components/Help'
import { FileDropOverlay } from './components/FileDropOverlay'
import { ShortcutsSettings } from './components/ShortcutsSettings'
import { ExportModal } from './components/ExportModal'
import { ToastHost } from './components/Toast'
import { Splitter } from './components/Splitter'
import { useKeymap } from './keymap/engine'

// The 3-pane editor holds a 900px floor (see .app in styles.css) and scrolls
// horizontally below it; this banner nudges the user to a wider window.
const MIN_EDITOR_WIDTH = 900

export default function App() {
  const init = useStore((s) => s.init)
  useEffect(() => { void init() }, [init])
  useKeymap()  // customizable CapCut / Premiere / Final Cut keymaps

  // Resizable panel sizes (Task 9) — persisted in the store (localStorage-
  // backed); drive them onto the .app/.center grids as CSS custom properties
  // so styles.css's `var(--left-w, 220px)` etc. pick them up.
  const leftW = useStore((s) => s.leftW)
  const rightW = useStore((s) => s.rightW)
  const timelineH = useStore((s) => s.timelineH)
  const setPanelSize = useStore((s) => s.setPanelSize)

  const [viewportWidth, setViewportWidth] = useState(() =>
    typeof window === 'undefined' ? MIN_EDITOR_WIDTH : window.innerWidth)
  const [narrowDismissed, setNarrowDismissed] = useState(false)
  useEffect(() => {
    const onResize = () => setViewportWidth(window.innerWidth)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])
  const showNarrowWarning = viewportWidth < MIN_EDITOR_WIDTH && !narrowDismissed

  const appVars = {
    '--left-w': `${leftW}px`,
    '--right-w': `${rightW}px`,
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
        <MediaBin />
      </aside>
      <Splitter
        orientation="vertical"
        style={{ gridArea: 'lsplit' }}
        onDelta={(d) => setPanelSize('leftW', leftW + d)}
      />
      <main className="center">
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
          onDelta={(d) => setPanelSize('timelineH', timelineH - d)}
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
        onDelta={(d) => setPanelSize('rightW', rightW - d)}
      />
      <aside className="sidebar right">
        <Properties />
        <OpsLog />
      </aside>
      <ChatOverlay />
      <Help />
      <ShortcutsSettings />
      <FileDropOverlay />
    </div>
    <ExportModal />
    <ToastHost />
    </>
  )
}
