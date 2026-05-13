import { useStore } from '../store'

export function OpsLog() {
  const ops = useStore((s) => s.ops)
  return (
    <div className="ops-log">
      <h2 style={{ fontSize: 11, color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: '.08em', margin: '8px 0' }}>History</h2>
      {ops.length === 0 && <div>No edits yet.</div>}
      {[...ops].reverse().map((op) => (
        <div key={op.seq} className="op">
          <b>{op.tool}</b> — {op.summary}
        </div>
      ))}
    </div>
  )
}
