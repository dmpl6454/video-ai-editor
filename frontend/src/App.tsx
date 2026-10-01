import { useEffect, useRef, useState } from 'react'
import { useStore, startSessionWatch } from './store'
import { ConnectionBanner } from './components/ConnectionBanner'
import { MediaToolsBanner } from './components/MediaToolsBanner'
import { TopBar } from './components/TopBar'
import { RailFoot, ToolRail } from './components/rail/ToolRail'
import { ToolPanel } from './components/rail/ToolPanel'
import { RailTooltip } from './components/rail/RailTooltip'
import { RightPanel } from './components/RightPanel'
import { Preview } from './components/Preview'
import { PromptBar } from './components/PromptBar'
import { ErrorBoundary } from './components/ErrorBoundary'
import { Timeline } from './components/Timeline'
import { Help } from './components/Help'
import { FileDropOverlay } from './components/FileDropOverlay'
import { ShortcutsSettings } from './components/ShortcutsSettings'
import { SettingsDialog } from './components/SettingsDialog'
import { ExportModal } from './components/ExportModal'
import { CaptionStylePanel } from './components/CaptionStylePanel'
import { ToastHost } from './components/Toast'
import { Splitter } from './components/Splitter'
import { Icon } from './components/Icon'
import { useLayoutStore } from './lib/layoutStore'
import { useBrainFlag } from './lib/brainFlag'
import { useKeymap } from './keymap/engine'

// The 3-pane editor holds a 900px floor (see .app in styles.css) and scrolls
// horizontally below it; this banner nudges the user to a wider window.
const MIN_EDITOR_WIDTH = 900

export default function App() {
  const init = useStore((s) => s.init)
  useEffect(() => { void init() }, [init])
  useKeymap()  // customizable CapCut / Premiere / Final Cut keymaps
  // QA-105/109: notice edits made in another window (focus, visibility, a
  // light poll) and whether the engine is still there.
  useEffect(() => startSessionWatch(window), [])
  // Editor Brain (EB1): whether its surfaces show (`brain.enabled`, off by
  // default); read once, the components render nothing brain-shaped until then.
  const loadBrainFlag = useBrainFlag((s) => s.load)
  useEffect(() => { void loadBrainFlag() }, [loadBrainFlag])

  // The shell's layout (lib/layoutStore, LEFT_RAIL_SPEC §6.1): which tool
  // panel shows, whether each side is open, and any DRAGGED side width. A
  // width is written inline only once the user has dragged it (non-null);
  // until then the media-query defaults in styles.css size the columns.
  // The timeline height stays in store.ts.
  const leftOpen = useLayoutStore((s) => s.leftOpen)
  const leftW = useLayoutStore((s) => s.leftW)
  const rightW = useLayoutStore((s) => s.rightW)
  const rightOpen = useLayoutStore((s) => s.rightOpen)
  const setPanelWidth = useLayoutStore((s) => s.setPanelWidth)
  const timelineH = useStore((s) => s.timelineH)
  const setPanelSize = useStore((s) => s.setPanelSize)
  const toolPanelRef = useRef<HTMLElement>(null)
  // A drag's running width, seeded at pointer-down from the DRAWN width (the
  // stored one can be null or wider than the grid draws) and advanced by
  // every delta — read live, never from this render's closure: a real drag
  // fires many mousemoves per React commit, and a stale base loses all but
  // the last-flushed delta.
  const dragBase = useRef(0)

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
    ...(leftW !== null ? { '--left-w': `${leftW}px` } : {}),
    ...(rightW !== null ? { '--right-w': `${rightW}px` } : {}),
    '--timeline-h': `${timelineH}px`,
  } as React.CSSProperties
  const seedDrag = (el: HTMLElement | null) => {
    dragBase.current = el?.getBoundingClientRect().width ?? 0
  }

  return (
    <>
      {showNarrowWarning && (
        <div className="narrow-warning" role="status">
          <span className="nw-icon"><Icon name="flipH" /></span>
          <span className="nw-msg">
            Please use a wider window (min {MIN_EDITOR_WIDTH}px) for the best experience.
          </span>
          <button className="nw-dismiss" onClick={() => setNarrowDismissed(true)}>
            Dismiss
          </button>
        </div>
      )}
    <div className={`app${leftOpen ? '' : ' left-collapsed'}${rightOpen ? '' : ' right-collapsed'}`} style={appVars}>
      <TopBar />
      {/* DOM order is rail, tool panel, centre, right (LEFT_RAIL_SPEC §2.1):
          Tab from a rail tab goes into its panel, as the APG tabs pattern
          expects. */}
      <ToolRail />
      <ToolPanel ref={toolPanelRef} />
      <Splitter
        orientation="vertical"
        className="splitter-left"
        style={{ gridArea: 'lsplit' }}
        disabled={!leftOpen}
        onStart={() => seedDrag(toolPanelRef.current)}
        // Verified live before the base was kept here: a 10-step 80px drag
        // only moved the panel 8px when every delta re-read a stale width.
        onDelta={(d) => { dragBase.current = setPanelWidth('left', dragBase.current + d) ?? dragBase.current }}
      />
      <main className="center">
        {/* One sentence → a verified, single-undo edit. Above the picture,
            never over it; the .center grid's first (auto) row is its home
            (styles.css). Tools, chat and the phone all share the session
            lock with it — see lib/promptStore.ts. The missing-ffmpeg notice
            shares that row, in flow above it, so it pushes the bar down
            instead of covering the sidebars (wave C review). */}
        <div className="center-head">
          <MediaToolsBanner />
          <PromptBar />
        </div>
        <div className="preview-pane">
          <ErrorBoundary
            fallback={(err) => (
              <div className="preview-empty" style={{ padding: 16, textAlign: 'center' }}>
                <div style={{ marginBottom: 6, color: 'var(--warn)' }}><Icon name="warning" size={24} /></div>
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
        className="splitter-right"
        style={{ gridArea: 'rsplit' }}
        // Dragging right moves the mouse away from the right panel, which
        // should shrink it — the delta sign is negated relative to the left.
        onStart={() => seedDrag(document.getElementById('right-panel'))}
        onDelta={(d) => { dragBase.current = setPanelWidth('right', dragBase.current - d) ?? dragBase.current }}
        // Collapsed, the column is a 36 px rail with no splitter track;
        // dragging never silently un-collapses the panel.
        disabled={!rightOpen}
      />
      <RightPanel />
      {/* The rail foot (Help, Shortcuts, Settings): its own grid item, LAST in
          the DOM so it is last in the focus order (LEFT_RAIL_SPEC §2.1, R3). */}
      <RailFoot />
      <Help />
      <ShortcutsSettings />
      <SettingsDialog />
      <FileDropOverlay />
      <ConnectionBanner />
    </div>
    <ExportModal />
    <CaptionStylePanel />
    <ToastHost />
    <RailTooltip />
    </>
  )
}
