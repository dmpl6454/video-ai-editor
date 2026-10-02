// The Clip inspector (design §2c "Clip"; brief §3–§5): a tab row per clip
// kind — a video clip: Video · Audio · Speed · Animation · Adjustment · AI
// stylize; an audio clip: Basic · Voice changer · Speed; text and stickers
// keep their own full inspector — and a sub-segmented control inside a tab.
//
// The editors are the project's REAL ones: components/Properties' sections
// (timing, speed curves, animations, voice effects, framing, canvas, blend,
// the PIP shape) filtered per tab through its `sections` prop, the design's
// Transform rows on top (TransformGroup), the 14 feature groups mapped to the
// engine's tools (FeatureGroups), the colour adjustment (AdjustmentPanel) and
// AI stylize on the Prompt bar's look recipes.
import { useEffect, useMemo } from 'react'
import { useStore } from '../../store'
import { isMediaClip, type AnyClip, type Track } from '../../types'
import { Properties } from '../Properties'
import { Segmented } from '../ui/Segmented'
import { useInspector, type ClipTab } from './inspectorStore'
import { TransformGroup } from './TransformGroup'
import { FeatureGroups } from './FeatureGroups'
import { RemoveBg, VideoMask, Retouch } from './VideoSubs'
import { AudioBasic, VoiceChanger } from './AudioBasic'
import { AdjustmentPanel } from './AdjustmentPanel'
import { AiStylize } from './AiStylize'
import { PanelState } from '../ui/Skeleton'
import { useSubTab } from './useSubTab'

function findClip(tracks: readonly Track[], id: string): { t: Track; c: AnyClip } | null {
  for (const t of tracks) for (const c of t.clips) if (c.id === id) return { t, c }
  return null
}

const VIDEO_TABS: ClipTab[] = ['Video', 'Audio', 'Speed', 'Animation', 'Adjustment', 'AI stylize']
const AUDIO_TABS: ClipTab[] = ['Basic', 'Voice changer', 'Speed']

export function ClipInspector() {
  const edl = useStore((s) => s.edl)
  const sel = useStore((s) => s.selection)
  const tab = useInspector((s) => s.clipTab)
  const setTab = useInspector((s) => s.setClipTab)
  const found = edl && sel ? findClip(edl.tracks, sel) : null
  const kind: 'video' | 'audio' | 'text' | 'sticker' | null = !found ? null
    : found.t.type === 'sticker' ? 'sticker' : !isMediaClip(found.c) ? 'text'
    : ['audio', 'music', 'vo'].includes(found.t.type) ? 'audio' : 'video'
  const tabs = useMemo<ClipTab[]>(() => kind === 'video' ? VIDEO_TABS : kind === 'audio' ? AUDIO_TABS : kind === 'text' ? ['Text'] : ['Sticker'], [kind])
  const active: ClipTab = tab && tabs.includes(tab) ? tab : tabs[0]
  // A tab that the new clip kind does not have falls back to its first.
  useEffect(() => { if (tab && !tabs.includes(tab)) setTab(null) }, [tab, tabs, setTab])

  if (!found || !kind) {
    return <div className="in"><div className="ed-panel-head">Clip</div><PanelState kind="empty" title="Clip not found" /></div>
  }
  const c = found.c
  return (
    <div className="in in-clip" data-clip-id={c.id} data-clip-kind={kind}>
      <div className="in-tabs" role="tablist" aria-label="Clip inspector">
        {tabs.map((t) => (
          <button key={t} type="button" role="tab" aria-selected={active === t} className={`in-tab${active === t ? ' is-active' : ''}`} onClick={() => setTab(t)}>{t}</button>
        ))}
      </div>
      <div className="in-scroll" key={`${c.id}:${active}`}>
        {kind === 'video' && <VideoClipTab tab={active} track={found.t} clip={c} />}
        {kind === 'audio' && <AudioClipTab tab={active} />}
        {(kind === 'text' || kind === 'sticker') && (
          // The text / sticker inspector (every field, QA-076 sentinels and all).
          <div className="in-pad in-legacy"><Properties bare /></div>
        )}
      </div>
      {kind === 'video' && active === 'Adjustment' && <AdjustmentFooter clipId={c.id} />}
    </div>
  )
}

type VideoSub = 'Basic' | 'Remove BG' | 'Mask' | 'Retouch'
type AudioSub = 'Basic' | 'Voice changer'
type SpeedSub = 'Standard' | 'Curve' | 'Velocity'

function VideoClipTab({ tab, track, clip }: { tab: ClipTab; track: Track; clip: AnyClip }) {
  if (tab === 'Video') return <VideoTab track={track} clip={clip} />
  if (tab === 'Audio') return <AudioTab clip={clip} />
  if (tab === 'Speed') return <SpeedTab />
  if (tab === 'Animation') return <div className="in-pad in-legacy"><Properties bare sections={['Animation']} /></div>
  if (tab === 'Adjustment') return <AdjustmentPanel clipId={clip.id} />
  return <AiStylize clipId={clip.id} />
}

function VideoTab({ track, clip }: { track: Track; clip: AnyClip }) {
  const [sub, setSub] = useSubTab<VideoSub>('Video', 'Basic')
  {
    return (
      <>
        <div className="in-pad-x">
          <Segmented<VideoSub> label="Video" value={sub} onChange={setSub}
                     options={[{ id: 'Basic', label: 'Basic' }, { id: 'Remove BG', label: 'Remove BG' }, { id: 'Mask', label: 'Mask' }, { id: 'Retouch', label: 'Retouch' }]} />
        </div>
        {sub === 'Basic' && (
          <>
            <TransformGroup clipId={clip.id} trackId={track.id} />
            <div className="ui-hairline in-mx" />
            <FeatureGroups clipId={clip.id} track={track} clip={clip} />
            <div className="ui-hairline in-mx" />
            <div className="in-pad in-legacy"><Properties bare sections={['Timing', 'Framing', 'Video fade']} /></div>
          </>
        )}
        {sub === 'Remove BG' && <RemoveBg clipId={clip.id} track={track} clip={clip} />}
        {sub === 'Mask' && <VideoMask clipId={clip.id} track={track} clip={clip} />}
        {sub === 'Retouch' && <Retouch />}
      </>
    )
  }
}

function AudioTab({ clip }: { clip: AnyClip }) {
  const [sub, setSub] = useSubTab<AudioSub>('Audio', 'Basic')
  return (
    <>
      <div className="in-pad-x">
        <Segmented<AudioSub> label="Audio" value={sub} onChange={setSub}
                   options={[{ id: 'Basic', label: 'Basic' }, { id: 'Voice changer', label: 'Voice changer' }]} />
      </div>
      {sub === 'Voice changer' ? <VoiceChanger /> : <AudioBasic clipId={clip.id} kind="video" />}
    </>
  )
}

function AudioClipTab({ tab }: { tab: ClipTab }) {
  if (tab === 'Voice changer') return <VoiceChanger />
  if (tab === 'Speed') return <SpeedTab audio />
  return <AudioBasic kind="audio" />
}

function SpeedTab({ audio = false }: { audio?: boolean }) {
  const [sub, setSub] = useSubTab<SpeedSub>('Speed', 'Standard')
  return (
    <>
      <div className="in-pad-x">
        <Segmented<SpeedSub> label="Speed" value={sub} onChange={setSub}
                   options={[{ id: 'Standard', label: 'Standard' }, { id: 'Curve', label: 'Curve', disabled: audio, title: audio ? 'Speed curves apply to video lanes' : undefined }, { id: 'Velocity', label: 'Velocity' }]} />
      </div>
      {sub === 'Velocity' ? (
        <PanelState kind="unavailable" title="Velocity effects are not available in this build"
                    body="Preset velocity ramps need a catalogue this build does not ship. Curve has the eight editable speed ramps; Standard sets a constant speed." />
      ) : (
        <div className="in-pad in-legacy" data-speed-sub={sub}><Properties bare sections={['Speed']} /></div>
      )}
    </>
  )
}

function AdjustmentFooter({ clipId }: { clipId: string }) {
  return <AdjustmentPanel.Footer clipId={clipId} />
}

