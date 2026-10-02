// The Player (design §2b; brief §6 "Player"): header "Player" + mode, the
// black stage with the project's composite (components/Preview — the same
// render engine, overlays, scrubber and crop view as before) or a previewed
// SOURCE file, the transform bounding box over a selected video clip, then
// the two footer rows: timecode · skip/play/skip, and Quality · Zoom · Ratio ·
// fullscreen.
//
// Honest where the engine stops: the preview renders at one resolution, so
// the Quality menu offers Full and names the others as not available; Zoom is
// a VIEWING zoom (it scales the stage, never a clip — brief §6); Ratio writes
// the project canvas through `set_aspect_ratio` / `set_canvas`, the same
// state Project settings edits.
import { useEffect, useRef, useState } from 'react'
import { useStore } from '../../store'
import { toast } from '../../toast'
import { formatTimecode } from '../../lib/timecode'
import { COMMAND_BY_ID } from '../../keymap/commands'
import { useInspector } from '../inspector/inspectorStore'
import { PZOOMS, QUALITIES, RATIOS, ratioCommand, ratioOf, stageSize, zoomFactor, type PZoom, type RatioId } from '../../lib/playerControls'
import { Preview } from '../Preview'
import { MediaToolsBanner } from '../MediaToolsBanner'
import { PromptBar } from '../PromptBar'
import { TimecodeField } from '../TimecodeField'
import { Dropdown } from '../ui/Dropdown'
import { Icon } from '../Icon'
import { SourcePreview } from './SourcePreview'
import { TransformBox } from './TransformBox'
import { openProjectSettings } from '../../lib/dialogOpeners'
import './player.css'

export function PlayerPanel() {
  const edl = useStore((s) => s.edl)
  const playhead = useStore((s) => s.playhead)
  const isPlaying = useStore((s) => s.isPlaying)
  const setPlaying = useStore((s) => s.setPlaying)
  const setPlayhead = useStore((s) => s.setPlayhead)
  const dispatch = useStore((s) => s.dispatch)
  const preview = useInspector((s) => s.preview)
  const [zoom, setZoom] = useState<PZoom>('Fit')
  const [quality, setQuality] = useState<typeof QUALITIES[number]>('Full')
  const stageRef = useRef<HTMLDivElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const [stageBox, setStageBox] = useState({ w: 0, h: 0 })
  const fps = edl?.canvas.fps ?? 30
  const duration = edl?.duration ?? 0
  const sourceMode = !!preview

  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const ro = new ResizeObserver(([e]) => setStageBox({ w: e.contentRect.width, h: e.contentRect.height }))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const canvas = edl?.canvas ?? { w: 1080, h: 1920, fps: 30 }
  const box = stageSize(canvas, stageBox, zoom)
  const ratio = ratioOf(canvas)

  const onRatio = async (id: RatioId) => {
    if (id === 'Custom') { openProjectSettings(); return }
    const cmd = ratioCommand(id, canvas)
    if (!cmd) { toast.info('Pick a size under Project settings › Resolution.'); return }
    await dispatch(cmd.tool, cmd.args)
  }
  const fullscreen = () => {
    const el = stageRef.current
    if (!el) return
    if (document.fullscreenElement) void document.exitFullscreen()
    else void el.requestFullscreen?.()
  }

  return (
    <div className="pl">
      <div className="ed-panel-head">
        <span>Player</span>
        <span className="ed-panel-mode">{sourceMode ? 'Source preview' : 'Timeline'}</span>
      </div>
      {/* One sentence → a verified, single-undo edit: this product's own
          control, above the picture and never over it (its run log opens
          below the row while a run is live). */}
      <PromptBar />
      <MediaToolsBanner />
      <div ref={stageRef} className="pl-stage">
        <div ref={scrollRef} className={`pl-scroll${zoom === 'Fit' ? '' : ' is-zoomed'}`}>
          <div className="pl-canvas-wrap" style={{ width: box.w, height: box.h }}>
            {sourceMode ? (
              <SourcePreview asset={preview} width={box.w} height={box.h} />
            ) : (
              <>
                <div className="preview-pane pl-preview"><Preview /></div>
                <TransformBox stageW={box.w} stageH={box.h} />
              </>
            )}
          </div>
        </div>
      </div>
      <div className="pl-foot">
        <div className="pl-row1">
          <span className="pl-tc">
            {sourceMode ? (
              <span className="pl-tc-cur">{formatTimecode(0, fps)}</span>
            ) : (
              <TimecodeField value={playhead} fps={fps} min={0} max={duration || undefined} ariaLabel="Playhead timecode" className="pl-tc-field"
                             title="Playhead — type a timecode (HH:MM:SS:FF), seconds or frames and press Enter to jump"
                             onCommit={(t) => { setPlaying(false); setPlayhead(t) }} />
            )}
            <span className="pl-tc-sep">/</span>
            <span className="pl-tc-tot">{formatTimecode(sourceMode ? (preview?.duration ?? 0) : duration, fps)}</span>
          </span>
          <div className="pl-transport">
            <button type="button" className="ui-icon-btn" aria-label="Previous frame" title="Previous frame (←)" disabled={sourceMode}
                    onClick={() => void COMMAND_BY_ID.frameBack.run(useStore.getState())}><Icon name="skipBack" /></button>
            <button type="button" className="pl-play" aria-label={isPlaying ? 'Pause' : 'Play'} title={isPlaying ? 'Pause (Space)' : 'Play (Space)'}
                    aria-keyshortcuts="Space" disabled={sourceMode || !duration}
                    onClick={() => { useStore.getState().replayFromStart(); setPlaying(!isPlaying) }}>
              <Icon name={isPlaying ? 'pause' : 'play'} filled />
            </button>
            <button type="button" className="ui-icon-btn" aria-label="Next frame" title="Next frame (→)" disabled={sourceMode}
                    onClick={() => void COMMAND_BY_ID.frameForward.run(useStore.getState())}><Icon name="skipForward" /></button>
          </div>
          <span />
        </div>
        <div className="pl-row2">
          <Dropdown label="Playback quality" value={quality} placement="above" width={150}
                    items={QUALITIES.map((q) => ({ id: q, label: q, disabled: q !== 'Full', title: q === 'Full' ? undefined : 'Not available: the preview renders at one quality in this build' }))}
                    onChange={setQuality} note="Playback only. Export uses full quality."
                    trigger={<>{quality}<Icon name="chevronDown" /></>} />
          <Dropdown label="Viewing zoom" value={zoom} placement="above" width={120}
                    items={PZOOMS.map((z) => ({ id: z, label: z }))} onChange={setZoom}
                    trigger={<><Icon name="zoomIn" />{zoom}</>} />
          <Dropdown label="Ratio" value={ratio} placement="above" width={150}
                    items={RATIOS.map((r) => ({ id: r.id, label: r.label, title: r.title }))} onChange={(id) => void onRatio(id)}
                    trigger={<><Icon name="crop" />{RATIOS.find((r) => r.id === ratio)?.label ?? ratio}</>} />
          <button type="button" className="ui-icon-btn" aria-label="Fullscreen" title="Fullscreen (⇧⌘F)" onClick={fullscreen}><Icon name="fullscreen" /></button>
        </div>
      </div>
      {!sourceMode && !duration && (
        <div className="pl-empty" aria-hidden="true">Import media to start</div>
      )}
      {zoomFactor(zoom) !== null && <span className="pl-sr-only">Viewing zoom {zoom}</span>}
    </div>
  )
}
