import { useEffect, useRef, useState } from 'react'
import { useStore } from '../../store'
import { displayNameFor, itemsFor, namesBySrc, useMediaNames } from '../../lib/mediaNames'
import { formatDb } from '../../lib/dbFormat'
import { useSliderCommit } from '../../lib/useSliderCommit'
import { Icon } from '../Icon'
import { VoRecorder } from '../VoRecorder'
import './panels.css'

// The Audio tool panel (docs/design/LEFT_RAIL_SPEC.md §2.5): music, voiceover
// and the mix, moved out of the Media panel in R1 with their behaviour
// unchanged — "Add music…" (upload + onto the Music lane, or library only),
// VoRecorder (Record voiceover / Import audio file as voiceover) and the music
// on the timeline (per-clip volume, Duck under speech). The AI audio rows
// (Reduce noise, Isolate vocals, …) arrive with the deep links in R5.
//
// VoRecorder owns a live MediaRecorder: ToolPanel keeps this panel mounted
// while hidden, so switching panels mid-recording never orphans a take.

export function AudioPanel() {
  const uploadAudio = useStore((s) => s.uploadAudio)
  const addToTimeline = useStore((s) => s.importAddToTimeline)
  const audioRef = useRef<HTMLInputElement>(null)
  return (
    <div className="audio-panel">
      <h3 className="section-label tool-section-label">Music</h3>
      <button
        className="panel-btn"
        onClick={() => audioRef.current?.click()}
        title={addToTimeline
          ? 'Pick an audio file — it lands on the Music track'
          : 'Pick an audio file — it goes to the media list only'}
      >
        <Icon name="music" /> Add music…
      </button>
      <input
        ref={audioRef}
        type="file"
        accept="audio/*,.mp3,.wav,.m4a,.aac,.flac,.ogg,.mp4,.m4v,.mov"
        hidden
        onChange={(e) => {
          const f = e.target.files?.[0]
          // Cleared so picking the same file again still imports.
          e.target.value = ''
          if (f) void uploadAudio(f)
        }}
      />
      <MusicPanel />
      <h3 className="section-label tool-section-label">Voiceover</h3>
      <VoRecorder />
    </div>
  )
}

// QA-083: one row per music clip. The panel used to control only the FIRST
// music clip while its slider set every music clip's gain (`target: 'music'`),
// so a second song could not be adjusted or removed on its own. Ducking is a
// lane setting and stays one checkbox for the lane.
function MusicPanel() {
  const edl = useStore((s) => s.edl)
  const sid = useStore((s) => s.sessionId)
  const dispatch = useStore((s) => s.dispatch)
  const library = useMediaNames((s) => itemsFor(s, sid))
  const music = edl?.tracks.find((t) => t.id === 'music')
  const clips = (music?.clips ?? []).filter((c) => 'src' in c) as unknown as MusicClip[]
  const ducking = !!(music as unknown as { duck?: unknown })?.duck
  if (!clips.length) return null
  const names = namesBySrc(library)
  return (
    <div className="item music-panel">
      <div className="music-panel-head section-label"><Icon name="music" /> On the timeline</div>
      {clips.map((clip) => (
        <MusicClipRow key={clip.id} clip={clip} name={displayNameFor(clip.src, names)}
                      onlyOne={clips.length === 1} />
      ))}
      <div className="row" style={{ marginTop: 6, gap: 6, alignItems: 'center' }}>
        <label
          style={{ fontSize: 11, color: 'var(--text-dim)' }}
          title="Automatically lower the music whenever someone is speaking"
        >
          <input
            type="checkbox" checked={ducking}
            onChange={() => dispatch('set_duck', { track: 'music', enabled: !ducking })}
            style={{ marginRight: 4 }}
          />
          Duck under speech
        </label>
      </div>
    </div>
  )
}

interface MusicClip { id: string; src: string; start: number; audio?: { gain_db?: number } }

function MusicClipRow({ clip, name, onlyOne }: { clip: MusicClip; name: string; onlyOne: boolean }) {
  const dispatch = useStore((s) => s.dispatch)
  const gain = clip.audio?.gain_db ?? -12
  // Commit-on-release, same pattern as Properties' sliders: the thumb +
  // readout track the drag locally, but set_volume dispatches ONCE on
  // pointer-up / key-release / blur. This used to dispatch (and kick a
  // preview re-render) on every onChange tick of the drag.
  const [localGain, setLocalGain] = useState(gain)
  const draggingGain = useRef(false)
  // Re-seed from the stored value when it changes from outside (undo, chat
  // edits) — but never stomp the value mid-drag.
  useEffect(() => { if (!draggingGain.current) setLocalGain(gain) }, [gain])
  // One op per gesture — an arrow-key burst commits once idle (QA-087).
  // This clip only (QA-083): the target is its id, not the whole lane.
  const gainCommit = useSliderCommit(gain, (v) => {
    draggingGain.current = false
    void dispatch('set_volume', { target: clip.id, db: v })
  })
  const inputId = `music-gain-${clip.id}`
  return (
    <div className="music-clip" data-music-clip={clip.id}>
      <div className="music-clip-name" title={name}>{name}</div>
      {/* A real flex row that never wraps (QA-088): the old `.row` here had no
          rule outside `.props`, so it was a block and the readout wrapped,
          orphaning the '-' of "-12 dB" at the end of the line. */}
      <div className="music-gain-row">
        <label htmlFor={inputId}>Vol</label>
        <input
          id={inputId}
          type="range" min={-30} max={6} step={0.5} value={localGain}
          aria-valuetext={formatDb(localGain)}
          onChange={(e) => { const v = Number(e.target.value); draggingGain.current = true; setLocalGain(v); gainCommit.change(v) }}
          onPointerUp={(e) => { draggingGain.current = false; gainCommit.onPointerUp(e) }}
          onPointerCancel={() => { draggingGain.current = false }}
          onKeyUp={gainCommit.onKeyUp}
          onBlur={() => { draggingGain.current = false; gainCommit.onBlur() }}
        />
        <output htmlFor={inputId}>{formatDb(localGain)}</output>
      </div>
      <button
        style={{ marginTop: 6, width: '100%', fontSize: 11 }}
        title="Remove this music from the timeline (the file stays uploaded)"
        aria-label={`Remove ${name} from the timeline`}
        onClick={() => dispatch('ripple_delete', { clip_id: clip.id })}
      >
        {onlyOne ? 'Remove music' : 'Remove'}
      </button>
    </div>
  )
}
