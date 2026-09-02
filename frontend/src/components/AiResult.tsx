import { useStore } from '../store'
import { toast } from '../toast'
import { resultView, type ResultRow } from '../lib/aiResults'

// The result view for read-only tools (find_moments, make_shorts, search_media,
// generate_hook, find_broll, diarize, audit_aesthetic, match_style). Rows come
// from lib/aiResults.ts; this file only decides what a row's button does:
// a range → in/out marks + playhead, a hook line → add_hook_overlay.

const fmt = (t: number) => `${t.toFixed(2).replace(/\.?0+$/, '')}s`

function safeJson(v: unknown): string {
  try { return JSON.stringify(v ?? null, null, 2) } catch { return String(v) }
}

function goTo(start: number, end: number) {
  const s = useStore.getState()
  s.setPlayhead(start)
  if (end > start) { s.setInMark(start); s.setOutMark(end) }
}

async function applyHook(text: string) {
  const res = await useStore.getState().dispatch('add_hook_overlay', { text })
  if (res) toast.success('Hook added')
}

function Row({ row, tool }: { row: ResultRow; tool: string }) {
  switch (row.kind) {
    case 'range': {
      const ranged = row.end > row.start
      return (
        <li className="ai-result-row">
          <time>{fmt(row.start)}{ranged ? `–${fmt(row.end)}` : ''}</time>
          <span className="grow">{row.text}{row.score !== undefined && <small> · {row.score.toFixed(2)}</small>}</span>
          <button type="button" onClick={() => goTo(row.start, row.end)}
                  title={ranged ? 'Set the In/Out marks to this range and move the playhead' : 'Move the playhead here'}>
            {ranged ? 'Set in/out' : 'Go to'}
          </button>
        </li>
      )
    }
    case 'text':
      return (
        <li className="ai-result-row">
          <span className="grow">{row.text}</span>
          {tool === 'generate_hook' && (
            <button type="button" onClick={() => { void applyHook(row.text) }} title="Add this line as the hook overlay">Use</button>
          )}
        </li>
      )
    case 'path':
      return <li className="ai-result-row"><code className="grow" title={row.path}>{row.text}</code></li>
    case 'speaker':
      return (
        <li className="ai-result-row">
          <time>{fmt(row.start)}–{fmt(row.end)}</time>
          <span className="grow">{row.speaker}</span>
        </li>
      )
    case 'issue':
      return (
        <li className={`ai-result-row ai-issue ${row.level}`}>
          <span className="ai-issue-level">{row.level}</span>
          <span className="grow">{row.text}</span>
        </li>
      )
  }
}

export function AiResult({ tool, label, result }: { tool: string; label: string; result: unknown }) {
  const view = resultView(tool, result, label)
  return (
    <div className="ai-result">
      <div className="ai-result-head">{view.headline}</div>
      {view.note && <p className="ai-result-note">{view.note}</p>}
      {view.rows.length > 0 && (
        <ul className="ai-result-rows">
          {view.rows.map((r, i) => <Row key={i} row={r} tool={tool} />)}
        </ul>
      )}
      <details className="ai-raw">
        <summary>Raw JSON</summary>
        <pre>{safeJson(view.raw)}</pre>
      </details>
    </div>
  )
}
