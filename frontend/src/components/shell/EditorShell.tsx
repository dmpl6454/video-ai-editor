// The four-panel editor (design handoff §2 "Editor shell"; brief §1 "Desktop
// shell"): a 44 px top bar, then a 6 px-padded grid whose top row holds the
// asset browser, the Player and the inspector as fluid fr columns and whose
// second row is the full-width timeline. The columns and rows come from the
// workspace layout (lib/workspaceLayout: Default / Media / Attributes /
// Vertical, each adjustable by the splitters between the panels and reset
// from the Layout menu).
//
// Switching an asset tab never replaces the player or the timeline; selecting
// a clip changes the inspector; deselecting returns it to project Details
// (InspectorPanel). The whole editor keeps the engine's connection banner,
// the missing-ffmpeg banner, the dialogs and the toasts it always had.
import { useCallback, useEffect, useRef } from 'react'
import { useStore } from '../../store'
import { useWorkspace, gridColumns, gridRows, resizeCols, resizeRows } from '../../lib/workspaceLayout'
import { EditorTopBar } from './EditorTopBar'
import { AssetBrowser } from '../assets/AssetBrowser'
import { PlayerPanel } from '../player/PlayerPanel'
import { InspectorPanel } from '../inspector/InspectorPanel'
import { Timeline } from '../Timeline'
import { Splitter } from '../Splitter'
import { ErrorBoundary } from '../ErrorBoundary'
import { Icon } from '../Icon'
import { ProjectSettingsDialog } from '../dialogs/ProjectSettingsDialog'
import { ShareDialog } from '../dialogs/ShareDialog'
import { ShortcutsDialog } from '../dialogs/ShortcutsDialog'
import { Help } from '../Help'
import { SettingsDialog } from '../SettingsDialog'
import { FileDropOverlay } from '../FileDropOverlay'
import { ConnectionBanner } from '../ConnectionBanner'
import { CaptionStylePanel } from '../CaptionStylePanel'
import { RailTooltip } from '../rail/RailTooltip'
import './shell.css'

export function EditorShell() {
  const layout = useWorkspace((s) => s.layout)
  const overrides = useWorkspace((s) => s.overrides)
  const setSpec = useWorkspace((s) => s.setSpec)
  const spec = useWorkspace.getState().spec()
  const topRef = useRef<HTMLDivElement>(null)
  const bodyRef = useRef<HTMLDivElement>(null)
  // The spec is derived from (layout, overrides); reading it through getState
  // inside the render keeps one source of truth without a selector that
  // returns a fresh object every time.
  void layout; void overrides

  // A splitter drag moves fractions, not pixels: the delta in px is converted
  // through the row's / column's drawn size, so a drag feels 1:1 whatever the
  // window is (the brief's "panels are CSS grid tracks, never fixed widths").
  const dragCols = useCallback((i: 0 | 1, dx: number) => {
    const row = topRef.current
    if (!row) return
    const s = useWorkspace.getState().spec()
    const total = s.cols.reduce((a, b) => a + b, 0)
    const px = row.getBoundingClientRect().width - 12   // two 6 px gaps
    if (px <= 0) return
    setSpec({ ...s, cols: resizeCols(s.cols, i, (dx / px) * total) })
  }, [setSpec])
  const dragRows = useCallback((dy: number) => {
    const body = bodyRef.current
    if (!body) return
    const s = useWorkspace.getState().spec()
    const total = s.rows.reduce((a, b) => a + b, 0)
    const px = body.getBoundingClientRect().height - 6
    if (px <= 0) return
    setSpec({ ...s, rows: resizeRows(s.rows, (dy / px) * total) })
  }, [setSpec])

  // The old shell's timeline height (store.timelineH) is no longer a pixel
  // value the grid reads; keep the store's field consistent for the parts of
  // Timeline.tsx that still measure their pane.
  const setPanelSize = useStore((s) => s.setPanelSize)
  useEffect(() => {
    const el = bodyRef.current
    if (!el) return
    const ro = new ResizeObserver(() => {
      const tl = el.querySelector<HTMLElement>('.ed-timeline')
      if (tl) setPanelSize('timelineH', Math.round(tl.getBoundingClientRect().height))
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [setPanelSize])

  return (
    <div className="ed" data-screen="editor">
      <EditorTopBar />
      <div ref={bodyRef} className="ed-body" style={{ gridTemplateRows: gridRows(spec) }}>
        <div ref={topRef} className="ed-top" style={{ gridTemplateColumns: gridColumns(spec) }}>
          <section className="ed-panel ed-assets" aria-label="Asset browser" data-screen-label="Asset browser">
            <ErrorBoundary fallback={(err) => <PanelError err={err} />}><AssetBrowser /></ErrorBoundary>
          </section>
          <Splitter orientation="vertical" className="ed-split ed-split-v ed-split-1" onDelta={(d) => dragCols(0, d)} />
          <section className="ed-panel ed-player" aria-label="Player" data-screen-label="Player">
            <ErrorBoundary fallback={(err) => <PanelError err={err} />}><PlayerPanel /></ErrorBoundary>
          </section>
          <Splitter orientation="vertical" className="ed-split ed-split-v ed-split-2" onDelta={(d) => dragCols(1, d)} />
          <section className="ed-panel ed-inspector" aria-label="Inspector" data-screen-label="Inspector" id="right-panel">
            <ErrorBoundary fallback={(err) => <PanelError err={err} />}><InspectorPanel /></ErrorBoundary>
          </section>
        </div>
        <Splitter orientation="horizontal" className="ed-split ed-split-h" onDelta={dragRows} />
        <section className="ed-panel ed-timeline timeline-pane" aria-label="Timeline" data-screen-label="Timeline">
          <ErrorBoundary fallback={(err) => <PanelError err={err} />}><Timeline /></ErrorBoundary>
        </section>
      </div>
      <ProjectSettingsDialog />
      <ShareDialog />
      <ShortcutsDialog />
      <Help />
      <SettingsDialog />
      <FileDropOverlay />
      <ConnectionBanner />
      <CaptionStylePanel />
      <RailTooltip />
    </div>
  )
}

function PanelError({ err }: { err: Error }) {
  return (
    <div className="ed-panel-error" role="alert">
      <span style={{ color: 'var(--warn)' }}><Icon name="warning" size={22} /></span>
      <div>This panel hit an error and was paused.</div>
      <div className="ui-faint">{err.message}</div>
    </div>
  )
}
