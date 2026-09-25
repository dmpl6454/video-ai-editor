import { useStore } from '../store'
import { opLabel, type LabelContext } from '../lib/opLabels'
import { historyRows } from '../lib/undoHorizon'
import { useMediaNameMap } from './MediaName'

// History speaks editor language (QA-101): "Split — at 00:00:05:00 on Video",
// not "split_at — Split at 5.00s on v1 (1 clip(s) split)"; media by the
// library's names, lanes by their timeline names, times as timecode. The raw
// tool and summary stay in the row's hover title for bug reports.
export function OpsLog() {
  const ops = useStore((s) => s.ops)
  const undoDepth = useStore((s) => s.undoDepth)
  const edl = useStore((s) => s.edl)
  const names = useMediaNameMap()
  const ctx: LabelContext = { fps: edl?.canvas?.fps, names, tracks: edl?.tracks }
  // QA-046: edits past the server's undo horizon stay listed, greyed, below a
  // divider. The project's own creation ("init") is never undoable and is not
  // an edit — it is the list's footer, and never puts the divider up alone.
  const { edits, footer, horizonAt } = historyRows(ops, undoDepth)
  return (
    <div className="ops-log">
      <h2 style={{ fontSize: 11, color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: '.08em', margin: '8px 0' }}>History</h2>
      {edits.length === 0 && <div>No edits yet.</div>}
      {edits.map((op, i) => {
        const l = opLabel(op, ctx)
        const reachable = horizonAt < 0 || i < horizonAt
        return (
          <div key={op.seq}>
            {i === horizonAt && (
              <div className="undo-horizon" role="note">Older edits cannot be undone</div>
            )}
            <div className={reachable ? 'op' : 'op past-horizon'} title={l.raw}>
              <b>{l.title}</b>{l.detail && <> — {l.detail}</>}
            </div>
          </div>
        )
      })}
      {footer.map((op) => {
        const l = opLabel(op, ctx)
        return <div key={op.seq} className="op op-origin" title={l.raw}>{l.title}</div>
      })}
    </div>
  )
}
