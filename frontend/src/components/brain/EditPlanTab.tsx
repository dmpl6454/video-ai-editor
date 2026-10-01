// Editor Brain (EB1-F): the preview card's Plan tab — what the brain decided
// and WHY, read-only (lib/brainDecisions). The summary line (with the length
// the run leaves), the rung that answered, the hook quote with a seek button,
// the decisions grouped by kind with their reasons — each one with a seek
// button when it has a source range, a decision the resolver dropped or the
// diff does not bear out is listed as not applied, never as done — and "Not
// done this time", the wave's honesty list, said in this ONE place. Nothing
// here changes anything: Apply is the card's, and the Changes tab remains the
// EDL diff. Ids and graph keys are never shown (brainDecisions.cleanText).

import {
  cleanText, deferredLines, groupDecisions, seekLabel, summaryLine, type BrainCardInfo, type BrainDecision,
} from '../../lib/brainDecisions'
import { formatTimecode } from '../../lib/timecode'
import { Icon } from '../Icon'
import './brain.css'

function SeekButton({ t, fps, onSeek, what }: { t: number; fps: unknown; onSeek: (t: number) => void; what: string }) {
  const tc = formatTimecode(t, fps)
  return (
    <button type="button" className="brain-seek brain-seek-tc" aria-label={`${seekLabel(t, fps)}: ${what}`}
            title="Move the playhead here (in the current timeline)" onClick={() => onSeek(t)}>
      <Icon name="play" /> {tc}
    </button>
  )
}

function DecisionRow({ d, fps, onSeek }: { d: BrainDecision; fps: unknown; onSeek?: (t: number) => void }) {
  const text = cleanText(d.text || d.code)
  return (
    <li className={d.applied === false ? 'is-dropped' : undefined}>
      <span className="brain-plan-text">{text}</span>
      {d.timelineT !== null && onSeek && <SeekButton t={d.timelineT} fps={fps} onSeek={onSeek} what={text} />}
      {d.applied === false && (
        <span className="brain-plan-dim"> — not applied{d.note ? `: ${cleanText(d.note)}` : ''}</span>
      )}
    </li>
  )
}

export function EditPlanTab({ info, fps, onSeek, id, labelledBy }: {
  info: BrainCardInfo
  fps: unknown
  onSeek?: (t: number) => void
  id?: string
  labelledBy?: string
}) {
  const hook = info.summary.hook
  const groups = groupDecisions(info.decisions)
  const deferred = deferredLines(info)
  return (
    <div role="tabpanel" id={id} aria-labelledby={labelledBy} className="brain-plan" tabIndex={0}
         data-testid="brain-plan-tab">
      <p className="brain-plan-summary">{summaryLine(info)}</p>
      {info.rungs.line && <p className="brain-plan-rungs">{info.rungs.line}</p>}
      {hook && hook.quote && (
        <blockquote className="brain-plan-hook">
          <span className="brain-plan-hook-kicker">Opens on</span>
          <q>{cleanText(hook.quote)}</q>
          {hook.timelineT !== null && onSeek && (
            <button type="button" className="brain-seek" onClick={() => onSeek(hook.timelineT as number)}>
              <Icon name="play" /> {seekLabel(hook.timelineT, fps)}
              <span className="brain-plan-dim"> (in the current timeline)</span>
            </button>
          )}
        </blockquote>
      )}
      {groups.map((g) => (
        <section key={g.kind} className="brain-plan-group">
          <h4>
            {g.label} <span className="brain-plan-count">{g.items.length}</span>
            {g.dropped > 0 && <span className="brain-plan-dim"> · {g.dropped} not applied</span>}
          </h4>
          <ul>
            {g.items.map((d) => <DecisionRow key={d.id} d={d} fps={fps} onSeek={onSeek} />)}
          </ul>
        </section>
      ))}
      <section className="brain-plan-group brain-plan-deferred">
        <h4>Not done this time</h4>
        {deferred.length ? (
          <ul>{deferred.map((line, i) => <li key={i}>{line}</li>)}</ul>
        ) : <p className="brain-plan-dim">Everything asked for was planned.</p>}
      </section>
    </div>
  )
}
