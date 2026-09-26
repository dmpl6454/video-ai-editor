import { useEffect, useState } from 'react'
import { clockLabel, spokenDuration, useActivityStore } from '../../lib/activityStore'
import { useLayoutStore } from '../../lib/layoutStore'
import { Icon } from '../Icon'
import './activityChip.css'

// What is running, in the top bar (docs/design/LEFT_RAIL_SPEC.md §2.8): the
// voiceover recording's Stop and the auto-captions' Cancel, reachable whatever
// tool panel shows — or with the tool panel collapsed. The rail's dots are a
// secondary cue; this chip is the primary one.
//
// ALWAYS rendered: its polite live region exists from the first paint, before
// anything runs, because a live region that appears together with its first
// message is not announced. The region speaks only lib/activityStore's
// throttled `liveMessage` (state changes and 25 / 50 / 75 %); the ticking
// clocks go only into the buttons' aria-labels.
//
// Each activity is two 28 px buttons: the body opens the panel that owns it
// (Audio / Captions), the second stops it through the SAME stop / cancel the
// panel uses (VoRecorder's stop, lib/captionRun's cancel).

/** Re-render once a second while `on` (the recording clock). */
function useSecondTick(on: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!on) return
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [on])
  return now
}

export function ActivityChip() {
  const recording = useActivityStore((s) => s.recording)
  const captions = useActivityStore((s) => s.captions)
  const liveMessage = useActivityStore((s) => s.liveMessage)
  const showTab = useLayoutStore((s) => s.showTab)
  const now = useSecondTick(!!recording)

  const recS = recording ? Math.max(0, (now - recording.startedAt) / 1000) : 0
  const pct = captions?.progress != null ? Math.round(captions.progress * 100) : null

  return (
    <div className="activity" data-activity>
      <span className="activity-sr-only" role="status" aria-live="polite">{liveMessage}</span>
      {recording && (
        <span className="act act-rec">
          <button
            type="button"
            className="act-body"
            aria-label={`Recording voiceover, ${clockLabel(recS)}. Open the Audio panel`}
            data-tip="Recording a voiceover. Open Audio"
            onClick={() => showTab('audio')}
          >
            <span className="act-rec-dot" aria-hidden="true" />
            <span className="act-word">Rec</span>
            <span className="act-num">{clockLabel(recS)}</span>
          </button>
          <button
            type="button"
            className="act-stop"
            aria-label="Stop recording"
            data-tip="Stop recording"
            onClick={() => recording.stop()}
          >
            <Icon name="stop" />
          </button>
        </span>
      )}
      {captions && (
        <span className="act act-cc">
          <button
            type="button"
            className="act-body"
            aria-label={captions.cancelling
              ? 'Captions stopping. Open the Captions panel'
              : `Captions${pct != null ? ` ${pct}%` : ''}${captions.etaS != null
                ? `, about ${spokenDuration(captions.etaS)} left` : ''}. Open the Captions panel`}
            data-tip="Transcribing captions. Open Captions"
            onClick={() => showTab('captions')}
          >
            <Icon name="loading" className="icon-spin" />
            <span className="act-word">Captions</span>
            {captions.cancelling
              ? <span className="act-dim">Stopping…</span>
              : <>
                  {pct != null && <span className="act-num">{pct}%</span>}
                  <span className="act-word act-dim act-num">
                    {' · '}{captions.etaS != null ? `${clockLabel(captions.etaS)} left` : `${captions.elapsedS}s`}
                  </span>
                </>}
          </button>
          <button
            type="button"
            className="act-stop"
            aria-label="Cancel captions"
            data-tip={captions.cancelling ? 'Stopping at the end of the current chunk' : 'Stop transcribing'}
            disabled={captions.cancelling}
            onClick={() => captions.cancel()}
          >
            <Icon name="close" />
          </button>
        </span>
      )}
    </div>
  )
}
