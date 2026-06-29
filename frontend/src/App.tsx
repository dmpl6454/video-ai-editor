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
import { useKeymap } from './keymap/engine'

// The 3-pane editor holds a 900px floor (see .app in styles.css) and scrolls
// horizontally below it; this banner nudges the user to a wider window.
const MIN_EDITOR_WIDTH = 900

export default function App() {
  const init = useStore((s) => s.init)
  useEffect(() => { void init() }, [init])
  useKeymap()  // customizable CapCut / Premiere / Final Cut keymaps

  const [viewportWidth, setViewportWidth] = useState(() =>
    typeof window === 'undefined' ? MIN_EDITOR_WIDTH : window.innerWidth)
  const [narrowDismissed, setNarrowDismissed] = useState(false)
  useEffect(() => {
    const onResize = () => setViewportWidth(window.innerWidth)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])
  const showNarrowWarning = viewportWidth < MIN_EDITOR_WIDTH && !narrowDismissed

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
    <div className="app">
      <TopBar />
      <aside className="sidebar left">
        <MediaBin />
      </aside>
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
        <div className="timeline-pane">
          <Timeline />
        </div>
      </main>
      <aside className="sidebar right">
        <Properties />
        <OpsLog />
      </aside>
      <ChatOverlay />
      <Help />
      <ShortcutsSettings />
      <FileDropOverlay />
    </div>
    </>
  )
}
